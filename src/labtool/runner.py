"""Runs one scenario: starts cmunet and the apps, waits, and collects files."""

import json
import os
import random
import signal
import socket
import subprocess
import threading
import time

from .scenarios import MSS



class RunError(Exception):
    pass


def _free_port_block(n):
    """Returns a base port such that base .. base+n-1 are free (UDP).

    Ports come from 10000-30000, below the ephemeral ranges of Linux
    (32768+) and macOS (49152+), so client sockets never collide with them.
    """
    for _ in range(200):
        base = random.randint(10000, 30000 - n)
        socks = []
        try:
            for p in range(base, base + n):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind(("127.0.0.1", p))
                socks.append(s)
            return base
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise RunError("could not find free UDP ports")


class Emulator:
    def __init__(self, binary, sc, flows, out_dir):
        self.out_dir = out_dir
        self.config = os.path.join(out_dir, "cmunet.conf")
        self.log = os.path.join(out_dir, "switch.csv")
        half = sc["rtt_ms"] * 1000 / 2
        lines = [
            f"seed {sc.get('seed', 15441)}",
            f"rate_bps {sc['rate_bps']:.0f}",
            f"buffer_pkts {sc['buffer_pkts']}",
            f"fwd_delay_us {half:.0f}",
            f"rev_delay_us {half:.0f}",
        ]
        if sc.get("paths_ms"):
            lines.append("paths " + ",".join(f"{p * 1000:.0f}" for p in sc["paths_ms"]))
        if sc.get("ack_paths_ms"):
            lines.append("ack_paths " + ",".join(f"{p * 1000:.0f}" for p in sc["ack_paths_ms"]))
        if sc.get("loss_random"):
            lines.append(f"loss_random {sc['loss_random']}")
        for ev in sc.get("events", []):
            if "at_byte" in ev:  # deterministic events, placed in the byte stream
                start = int(ev["at_byte"])
                if "burst_loss" in ev:
                    lines.append(f"seq_loss {start} {start + int(ev['burst_loss']) * MSS}")
                if "path_down" in ev:
                    path, length = ev["path_down"]
                    lines.append(f"seq_path_down {path} {start} {start + int(length)}")
                continue
            t = ev["t_ms"] * 1000
            if "rate_bps" in ev:
                lines.append(f"event {t:.0f} rate_bps {ev['rate_bps']:.0f}")
            if "fwd_delay_ms" in ev:
                lines.append(f"event {t:.0f} fwd_delay_us {ev['fwd_delay_ms'] * 1000:.0f}")
        for fid, lport, sport in flows:
            lines.append(f"flow {fid} {lport} {sport}")
        with open(self.config, "w") as f:
            f.write("\n".join(lines) + "\n")
        self.proc = subprocess.Popen([binary, self.config, self.log],
                                     stdout=subprocess.PIPE, text=True)
        self.t0 = None
        self.first_data = None
        self.established = set()
        threading.Thread(target=self._reader, daemon=True).start()
        self.wait_for(lambda: self.t0 is not None, 5, "emulator did not start")

    def _reader(self):
        for line in self.proc.stdout:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "T0":
                self.t0 = int(parts[1])
            elif parts[0] == "FIRSTDATA":
                self.first_data = int(parts[1])
            elif parts[0] == "ESTABLISHED":
                self.established.add(int(parts[1]))

    def wait_for(self, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.01)
        raise RunError(what)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Apps:
    """Starts app processes with their output redirected to log files."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.procs = []
        self.names = {}

    def start(self, name, argv, env=None, pipe=False):
        log = open(os.path.join(self.out_dir, f"{name}.log"), "w")
        full_env = dict(os.environ)
        full_env.update(env or {})
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE if pipe else log,
                                stderr=log, env=full_env,
                                text=True if pipe else None)
        self.procs.append(proc)
        self.names[proc.pid] = name
        return proc

    def crashed(self):
        """Describes the first app that died abnormally, or returns None."""
        for p in self.procs:
            rc = p.poll()
            if rc is not None and rc != 0:
                name = self.names[p.pid]
                if rc < 0:
                    try:
                        what = f"signal {-rc} ({signal.Signals(-rc).name})"
                    except ValueError:
                        what = f"signal {-rc}"
                else:
                    what = f"exit code {rc}"
                return f"the '{name}' process crashed with {what}; see {name}.log"
        return None

    def stop(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                pass


def _wait_file(path, limit_s, apps):
    """Waits for a result file. Returns None on success, else an error."""
    end = time.time() + limit_s
    while time.time() < end:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            time.sleep(0.05)
            return None
        crash = apps.crashed()
        if crash:
            return crash
        time.sleep(0.02)
    return f"the transfer did not finish within {limit_s:g} s"


def _host_env(sc):
    return {"LAB_HOST_LINK_BPS": f"{sc['rate_bps']:.0f}",
            "LAB_HOST_BASE_RTT_US": f"{sc['rtt_ms'] * 1000:.0f}"}


def run(sc, bins, out_dir):
    """Runs scenario `sc`. Returns a dict describing what happened."""
    os.makedirs(out_dir, exist_ok=True)
    kind = sc["kind"]
    info = {"finished": False, "out_dir": out_dir}
    n = len(sc["replies"]) if kind == "incast" else (2 if kind == "bufferbloat" else 1)
    base = _free_port_block(2 * n)  # one block: n emulator ports, then n server ports
    flows = [(i, base + i, base + n + i) for i in range(n)]

    emu = Emulator(bins["cmunet"], sc, flows, out_dir)
    apps = Apps(out_dir)
    info["t0"] = emu.t0
    info["switch_log"] = emu.log
    started = time.time()
    try:
        if kind == "bufferbloat":
            _run_bufferbloat(sc, bins, emu, apps, flows, info)
        elif kind == "incast":
            _run_incast(sc, bins, emu, apps, flows, info)
        else:
            _run_spraying(sc, bins, emu, apps, flows, info)
    except RunError as e:
        info["error"] = str(e)
    finally:
        info["wall_s"] = time.time() - started
        apps.stop()
        emu.stop()
    return info


def _run_bulk(sc, bins, app, emu, apps, flow, info, extra_env=None):
    fid, lport, sport = flow
    result = os.path.join(info["out_dir"], f"{app}-result.json")
    apps.start(f"{app}-recv", [bins[app], "recv", str(sport), str(sc["bytes"]), "7", result],
               env=extra_env)
    time.sleep(0.15)
    apps.start(f"{app}-send", [bins[app], "send", str(lport), str(sc["bytes"]), "7"])
    return result


def _finish(info, result_path, limit_s, apps):
    error = _wait_file(result_path, limit_s, apps)
    if error is None:
        with open(result_path) as f:
            info["result"] = json.load(f)
        info["finished"] = True
    else:
        info["error"] = error


def _run_bufferbloat(sc, bins, emu, apps, flows, info):
    info["gamer_log"] = os.path.join(info["out_dir"], "gamer-lag.csv")
    apps.start("gamer-recv", [bins["gamer"], "recv", str(flows[1][2]), info["gamer_log"]])
    result = _run_bulk(sc, bins, "gdrive", emu, apps, flows[0], info)
    # The game starts once the upload is under way.
    try:
        emu.wait_for(lambda: emu.first_data is not None, 20,
                     "gdrive never sent any data (did the handshake fail?)")
    except RunError as e:
        info["error"] = str(e)
        return
    count = int(sc["limit_s"] * 1000 / sc["gamer_interval_ms"])
    apps.start("gamer-send", [bins["gamer"], "send", str(flows[1][1]),
                              str(sc["gamer_interval_ms"]), str(count)])
    _finish(info, result, sc["limit_s"], apps)


def _run_spraying(sc, bins, emu, apps, flows, info):
    result = _run_bulk(sc, bins, "spray", emu, apps, flows[0], info)
    _finish(info, result, sc["limit_s"], apps)


class _LineWatcher:
    """Collects the lines a set of processes print, to wait for markers."""

    def __init__(self, procs):
        self.counts = {}
        self.lock = threading.Lock()
        for p in procs:
            threading.Thread(target=self._read, args=(p,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            with self.lock:
                word = line.strip()
                self.counts[word] = self.counts.get(word, 0) + 1

    def count(self, word):
        with self.lock:
            return self.counts.get(word, 0)


def _start_receivers(sc, bins, apps, flows, out_dir, sizes):
    """Starts one receiving process per worker; they share host information
    (lab_host) through a file mapped into every one of them."""
    shm = os.path.join(out_dir, "lab_host.shm")
    with open(shm, "wb") as f:
        f.truncate(0)  # lab_host sizes and zero-fills it
    env = dict(_host_env(sc), LAB_HOST_SHM=shm)
    procs = []
    for i, (_, _, sport) in enumerate(flows):
        result = os.path.join(out_dir, f"receiver-{i}.json")
        procs.append(apps.start(f"receiver-{i}", [bins["incast"], "recv", str(sport),
                                                  str(sizes[i]), str(1000 + i), result],
                                env=env, pipe=True))
    return procs


def _run_incast(sc, bins, emu, apps, flows, info):
    n = len(flows)
    receivers = _start_receivers(sc, bins, apps, flows, info["out_dir"], sc["replies"])
    lines = _LineWatcher(receivers)
    emu.wait_for(lambda: lines.count("LISTENING") == n, 20, "the receivers did not start")
    time.sleep(0.2)
    workers = []
    for i, (fid, lport, sport) in enumerate(flows):
        workers.append(apps.start(f"worker-{i}", [bins["incast"], "worker", str(lport),
                                                  str(sc["replies"][i]), str(1000 + i)]))
        time.sleep(0.005)
    emu.wait_for(lambda: len(emu.established) == n, 20,
                 f"only {len(emu.established)} of {n} handshakes completed")
    emu.wait_for(lambda: lines.count("JOINED") == n, 10, "the receivers did not all start")
    time.sleep(0.05)
    for p in receivers + workers:
        p.send_signal(signal.SIGUSR1)
    # Wait for every receiver's result, then combine them.
    end = time.time() + sc["limit_s"]
    paths = [os.path.join(info["out_dir"], f"receiver-{i}.json") for i in range(n)]
    for path in paths:
        error = _wait_file(path, max(0.0, end - time.time()), apps)
        if error is not None:
            info["error"] = error
            return
    parts = []
    for path in paths:
        with open(path) as f:
            parts.append(json.load(f))
    info["result"] = {"go_us": min(p["go_us"] for p in parts),
                      "replies": [{"bytes": p["bytes"], "done_us": p["done_us"], "ok": p["ok"]}
                                  for p in parts]}
    with open(os.path.join(info["out_dir"], "incast-result.json"), "w") as f:
        json.dump(info["result"], f)
    info["finished"] = True


def _cpu_seconds(pid):
    """CPU time (user + system) used so far by process `pid`, in seconds."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except OSError:
        out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
        if not out:
            return 0.0
        secs = 0.0
        for part in out.replace("-", ":").split(":"):
            secs = secs * 60 + float(part)
        return secs


def idle_cpu(bins, out_dir, conns=4, seconds=2.0):
    """Opens `conns` idle connections (one receiving process each) and returns
    the fraction of one CPU core the receivers use while nothing is sent."""
    os.makedirs(out_dir, exist_ok=True)
    sc = {"kind": "incast", "rate_bps": 10e6, "rtt_ms": 10, "buffer_pkts": 1000,
          "replies": [1000] * conns}
    base = _free_port_block(2 * conns)
    flows = [(i, base + i, base + conns + i) for i in range(conns)]
    emu = Emulator(bins["cmunet"], sc, flows, out_dir)
    apps = Apps(out_dir)
    try:
        receivers = _start_receivers(sc, bins, apps, flows, out_dir, [1000] * conns)
        time.sleep(0.3)
        for i, (_, lport, _) in enumerate(flows):
            apps.start(f"worker-{i}", [bins["incast"], "worker", str(lport), "1000", str(i)])
        emu.wait_for(lambda: len(emu.established) == conns, 20, "handshakes did not complete")
        time.sleep(0.3)
        before = sum(_cpu_seconds(p.pid) for p in receivers)
        time.sleep(seconds)
        after = sum(_cpu_seconds(p.pid) for p in receivers)
        return (after - before) / seconds / conns
    finally:
        apps.stop()
        emu.stop()
