"""Scores a finished run from the switch log and the apps' result files.

Every metric comes from what the switch saw or what our apps measured, never
from the student's own logs.
"""

import csv
import json
import math

from .scenarios import MSS, WIRE

GAMER_WIRE_BYTES = 125  # 100-byte move + 25-byte header

# Targets that a strong design reaches; doing at least this well earns 100.
BB_LINK_TARGET = 0.95       # Bufferbloat: share of the available rate
BB_QUEUE_PKTS = 2           # Bufferbloat: queued packets allowed on top of the wire minimum
INCAST_SLACK = 1.2          # Incast: completion within 1.2x the ideal
SPRAY_LINK_TARGET = 0.6     # Spraying: share of the available rate
SPRAY_PRECISION_TARGET = 0.8
SPRAY_REPAIR_SLACK = 2.0    # Spraying: repair within 2x the sharp-design time


class Packet:
    __slots__ = ("t", "flow", "dir", "ev", "seq", "ack", "plen", "hlen",
                 "flags", "win", "info", "pid")


def load_log(path):
    rows = []
    with open(path) as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 12:
                continue
            p = Packet()
            p.t = int(row[0])
            p.flow = int(row[1])
            p.dir = int(row[2])
            p.ev = row[3]
            p.seq = int(row[4])
            p.ack = int(row[5])
            p.plen = int(row[6])
            p.hlen = int(row[7])
            p.flags = int(row[8])
            p.win = int(row[9])
            p.info = int(row[10])
            p.pid = int(row[11])
            rows.append(p)
    return rows


def is_data(p):
    return p.dir == 0 and p.plen > p.hlen and not (p.flags & 0x8)


def percentile(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * q / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def rate_schedule(sc):
    """[(t_us relative to first data, rate_bps)] in time order."""
    sched = [(0, sc["rate_bps"])]
    for ev in sorted(sc.get("events", []), key=lambda e: e["t_ms"]):
        if "rate_bps" in ev:
            sched.append((ev["t_ms"] * 1000, ev["rate_bps"]))
    return sched


def delay_schedule(sc):
    """[(t_us relative to first data, one-way forward delay us)]."""
    sched = [(0, sc["rtt_ms"] * 500)]
    for ev in sorted(sc.get("events", []), key=lambda e: e["t_ms"]):
        if "fwd_delay_ms" in ev:
            sched.append((ev["t_ms"] * 1000, ev["fwd_delay_ms"] * 1000))
    return sched


def value_at(sched, t):
    v = sched[0][1]
    for ts, val in sched:
        if t >= ts:
            v = val
    return v


def time_avg(sched, t_start, t_end):
    """Time-weighted average of a step schedule over [t_start, t_end]."""
    if t_end <= t_start:
        return value_at(sched, t_start)
    total = 0.0
    points = [t_start] + [t for t, _ in sched if t_start < t < t_end] + [t_end]
    for a, b in zip(points, points[1:]):
        total += value_at(sched, a) * (b - a)
    return total / (t_end - t_start)


def queue_series(rows, buckets=60):
    """Max bottleneck occupancy (packets) in each time bucket of the transfer."""
    arr = [(p.t, p.info) for p in rows if p.ev == "A" and p.dir == 0]
    if not arr:
        return []
    t_start, t_end = arr[0][0], arr[-1][0]
    width = max(1, (t_end - t_start) // buckets + 1)
    series = [0] * buckets
    for t, q in arr:
        b = min(buckets - 1, (t - t_start) // width)
        series[b] = max(series[b], q)
    return series


def first_data_t(rows, flow=None):
    for p in rows:
        if p.ev == "A" and is_data(p) and (flow is None or p.flow == flow):
            return p.t
    return None


# --------------------------------------------------------------------------
# Bufferbloat
# --------------------------------------------------------------------------

def score_bufferbloat(sc, info):
    rows = load_log(info["switch_log"])
    res = info["result"]
    t0 = info["t0"]
    start = first_data_t(rows, flow=0)
    done = res["done_us"] - t0
    secs = (done - start) / 1e6
    goodput = res["bytes"] * 8 / secs
    rates = rate_schedule(sc)
    avg_rate = time_avg(rates, 0, done - start)
    gamer_bps = GAMER_WIRE_BYTES * 8 * 1000 / sc["gamer_interval_ms"]
    c_avail = avg_rate * MSS / WIRE - gamer_bps

    lags, lmins, targets = [], [], []
    delays = delay_schedule(sc)
    with open(info["gamer_log"]) as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) != 3 or parts[0] in ("idx", "corrupt"):
                continue
            send, recv = int(parts[1]) - t0, int(parts[2]) - t0
            if start <= send <= done:
                rel = send - start
                lags.append((recv - send) / 1000.0)
                # Wire minimum on a busy link: propagation + one full packet ahead.
                pkt_ms = WIRE * 8 * 1e3 / value_at(rates, rel)
                lmins.append(value_at(delays, rel) / 1000.0 + pkt_ms)
                targets.append(lmins[-1] + BB_QUEUE_PKTS * pkt_ms)
    if not lags:
        raise ValueError("no game moves arrived during the upload")
    lmin = sum(lmins) / len(lmins)
    target = sum(targets) / len(targets)
    l20, l50, l95 = (percentile(lags, q) for q in (20, 50, 95))
    lag_term = sum(min(1.0, target / l) for l in (l20, l50, l95)) / 3
    link_term = min(1.0, goodput / (BB_LINK_TARGET * c_avail))
    score = 100 * link_term * lag_term

    drops = sum(1 for p in rows if p.ev == "D")
    series = queue_series(rows)
    out = {
        "score": score,
        "lines": [
            f"gdrive goodput   {goodput / 1e6:.2f} / {c_avail / 1e6:.2f} Mbps",
            f"gamer lag        p20 {l20:.1f} ms   median {l50:.1f} ms   p95 {l95:.1f} ms"
            f"    (full marks at or below {target:.1f} ms)",
            f"router drops     {drops}",
        ],
        "tip_target": f"lag at or below ~{target:.0f} ms earns full marks on this link",
        "chart": ("router queue (packets)", series, sc["buffer_pkts"], secs),
        "metrics": {"goodput_bps": goodput, "c_avail_bps": c_avail, "lag_p20_ms": l20,
                    "lag_p50_ms": l50, "lag_p95_ms": l95, "lag_min_ms": lmin,
                    "lag_target_ms": target,
                    "drops": drops},
    }
    hints = []
    if drops > 0:
        first = next(p.t for p in rows if p.ev == "D")
        hints.append(f"the router dropped {drops} packets (first at t={(first - start) / 1e6:.2f}s):"
                     " your upload filled the whole buffer. Is loss the first signal you react to?")
    if link_term < 0.8:
        idle = sum(1 for q in series if q == 0) / max(1, len(series))
        hints.append(f"the upload used only {link_term * 100:.0f}% of the link"
                     + (f" and the queue was empty {idle * 100:.0f}% of the time" if idle > 0.3 else "")
                     + ": your sender is too cautious somewhere.")
    elif l95 > target:
        extra = f" ~{l50 - lmin:.0f} ms" if l50 - lmin >= 1 else ""
        hints.append(f"a standing queue adds{extra} delay to every move."
                     " How much queue does this link really need to stay busy?")
    if any("rate_bps" in e for e in sc.get("events", [])) and l95 > 2 * target:
        hints.append("the link slowed down mid-upload. How many RTTs does your sender need to notice?")
    out["hints"] = hints or [out["tip_target"]]
    return out


# --------------------------------------------------------------------------
# Incast
# --------------------------------------------------------------------------

def score_incast(sc, info):
    rows = load_log(info["switch_log"])
    res = info["result"]
    go = res["go_us"]
    t0 = info["t0"]
    dones = [(r["done_us"] - go) / 1e6 for r in res["replies"]]
    T = max(dones)
    total = sum(sc["replies"])
    c_eff = sc["rate_bps"] * MSS / WIRE
    t_ideal = total * 8 / c_eff + sc["rtt_ms"] / 1000
    needed = sum(math.ceil(b / MSS) for b in sc["replies"])
    go_rel = go - t0
    sent = sum(1 for p in rows if p.ev == "A" and is_data(p) and p.t >= go_rel - 1000)
    drops = sum(1 for p in rows if p.ev == "D" and is_data(p))
    score = 100 * min(1.0, INCAST_SLACK * t_ideal / T) * min(1.0, needed / max(1, sent))
    order = sorted(dones)
    shown = " ".join(f"{d:.2f}" for d in order) if len(order) <= 12 else (
        " ".join(f"{d:.2f}" for d in order[:6]) + " ... " +
        " ".join(f"{d:.2f}" for d in order[-4:]))
    out = {
        "score": score,
        "lines": [
            f"completion time   {T:.2f} s     ideal {t_ideal:.2f} s"
            f"   (full marks up to {INCAST_SLACK * t_ideal:.2f} s)",
            f"data packets      sent {sent}   needed {needed}     switch drops {drops}",
            f"per-reply completion (sorted, s)   {shown}",
        ],
        "chart": ("switch queue (packets)", queue_series(rows), sc["buffer_pkts"], T),
        "metrics": {"completion_s": T, "ideal_s": t_ideal, "sent": sent, "needed": needed,
                    "drops": drops, "replies_s": dones},
    }
    hints = []
    pipe = sc["rate_bps"] * sc["rtt_ms"] / 1000 / 8 / WIRE + sc["buffer_pkts"]
    if drops > 0:
        hints.append(f"{drops} drops -> at some moment the workers together had more in flight"
                     f" than the switch can hold (~{pipe:.0f} packets).")
    if T > 2 * t_ideal and order[-1] > 1.5 * order[len(order) // 2]:
        hints.append(f"one reply finished at {order[-1]:.2f}s, long after the median"
                     f" ({order[len(order) // 2]:.2f}s): what was that worker waiting for?")
    elif T > 1.2 * t_ideal and drops == 0:
        hints.append("no drops, but the link sat idle part of the time: are your windows too"
                     " small, or do waiting workers hear about free room too late?")
    if sent > needed * 1.1:
        hints.append(f"{sent - needed} extra packets were sent (retransmissions): each one"
                     " lowers your score.")
    out["hints"] = hints or (["near the ideal. Can you shave off the last few milliseconds?"]
                             if T <= 1.2 * t_ideal else
                             ["the link was not fully used: where did the time go?"])
    return out


# --------------------------------------------------------------------------
# Spraying
# --------------------------------------------------------------------------

class _Fenwick:
    def __init__(self, n):
        self.n = n
        self.t = [0] * (n + 1)

    def add(self, i):
        i += 1
        while i <= self.n:
            self.t[i] += 1
            i += i & -i

    def prefix(self, i):
        i += 1
        s = 0
        while i > 0:
            s += self.t[i]
            i -= i & -i
        return s


def score_spraying(sc, info):
    rows = load_log(info["switch_log"])
    res = info["result"]
    t0 = info["t0"]
    start = first_data_t(rows)
    done = res["done_us"] - t0
    secs = (done - start) / 1e6
    goodput = res["bytes"] * 8 / secs
    c_avail = sc["rate_bps"] * MSS / WIRE * (1 - sc.get("loss_random", 0.0))

    arrivals = [p for p in rows if p.ev == "A" and is_data(p)]
    isn = arrivals[0].seq if arrivals else 0
    fate = {}  # pid -> delivery time, or None if dropped
    for p in rows:
        if p.ev == "V" and is_data(p):
            fate[p.pid] = p.t
        elif p.ev == "D" and is_data(p):
            fate[p.pid] = None
    copies = {}  # seq -> [(arrival t, pid)]
    for p in arrivals:
        copies.setdefault(p.seq, []).append((p.t, p.pid))

    retx = []  # (t, seq, necessary, note)
    recoveries = []
    for seq, cs in copies.items():
        delivered_before = False
        first_delivery = None
        for i, (t, pid) in enumerate(cs):
            d = fate.get(pid)
            if i > 0:
                if delivered_before:
                    earlier = min(fate[q] for _, q in cs[:i] if fate.get(q) is not None)
                    gap = (earlier - t) / 1000
                    note = (f"original arrived {gap:.1f} ms after you resent it" if gap >= 0
                            else f"original had arrived {-gap:.1f} ms before")
                    retx.append((t, seq, False, note))
                else:
                    retx.append((t, seq, True, "earlier copy was dropped by the switch"))
            if d is not None:
                delivered_before = True
                if first_delivery is None or d < first_delivery:
                    first_delivery = d
        if any(fate.get(pid, 0) is None for _, pid in cs) and first_delivery is not None:
            recoveries.append((first_delivery - cs[0][0]) / 1000)
    retx.sort()
    nec = sum(1 for r in retx if r[2])
    precision = nec / len(retx) if retx else 1.0
    spread = max(sc["paths_ms"]) - min(sc["paths_ms"]) if sc.get("paths_ms") else 0
    r_star = 1.5 * sc["rtt_ms"] + spread
    r_target = SPRAY_REPAIR_SLACK * r_star
    r_med = percentile(recoveries, 50) if recoveries else None
    recovery_term = 1.0 if r_med is None else min(1.0, r_target / r_med)
    link_term = min(1.0, goodput / (SPRAY_LINK_TARGET * c_avail))
    precision_term = min(1.0, precision / SPRAY_PRECISION_TARGET)
    score = 50 * link_term + 25 * precision_term + 25 * recovery_term

    # Reordering seen by the receiver: packets sent later but delivered earlier.
    order = {p.pid: i for i, p in enumerate(arrivals)}
    deliveries = [p for p in rows if p.ev == "V" and is_data(p)]
    fw = _Fenwick(len(arrivals) + 1)
    hist = [0] * 6
    for k, p in enumerate(deliveries):
        idx = order.get(p.pid, 0)
        later_first = k - fw.prefix(idx)
        hist[min(5, later_first)] += 1
        fw.add(idx)
    drops = sum(1 for p in rows if p.ev == "D" and is_data(p))
    lines = [
        f"goodput           {goodput / 1e6:.2f} / {c_avail / 1e6:.2f} Mbps",
        f"retransmissions   {len(retx)} total: {nec} necessary, {len(retx) - nec} unnecessary",
        "repair time       " + (f"median {r_med:.0f} ms   (reference repair time {r_target:.0f} ms)"
                                if r_med is not None else "no packet was lost"),
        f"switch drops      {drops}",
        "reordering        packets grouped by how many later-sent packets beat them to"
        " the receiver",
        f"                  (e.g. \"1: {hist[1]}\" means {hist[1]} packets each had 1 packet"
        " sent after it (with a larger sequence number) arrive at the receiver first)",
        "                  " + "   ".join(
            f"{'0 (in order)' if i == 0 else i if i < 5 else '5+'}: {c}"
            for i, c in enumerate(hist)),
    ]
    if retx:
        lines.append("your retransmissions:   t(s)     seq (relative)   verdict")
        for t, seq, ok, note in retx[:8]:
            lines.append(f"                        {(t - start) / 1e6:6.3f}  {(seq - isn) % 2**32:>12,}"
                         f"   {'necessary  ' if ok else 'UNNECESSARY'}  {note}")
        if len(retx) > 8:
            lines.append(f"                        ... ({len(retx) - 8} more in retransmissions.csv)")
    out = {
        "score": score,
        "lines": lines,
        "chart": ("bottleneck queue (packets)", queue_series(rows), sc["buffer_pkts"], secs),
        "metrics": {"goodput_bps": goodput, "c_avail_bps": c_avail, "retx": len(retx),
                    "necessary": nec, "precision": precision, "repair_median_ms": r_med,
                    "repair_ideal_ms": r_star, "drops": drops},
        "retx_rows": [((t - start) / 1e6, (seq - isn) % 2**32, ok, note)
                      for t, seq, ok, note in retx],
    }
    hints = []
    worst = max((i for i, c in enumerate(hist) if c), default=0)
    if len(retx) - nec > max(2, nec):
        hints.append(f"{len(retx) - nec} of {len(retx)} retransmissions were unnecessary. Packets"
                     f" here are overtaken by up to {worst}{'+' if worst == 5 else ''} others:"
                     " when does your sender decide a packet is lost?")
    if r_med is not None and r_med > 1000:
        hints.append(f"repairing a lost packet took {r_med:.0f} ms (median); the reference"
                     f" repair time is {r_target:.0f} ms. Are you waiting for a timeout?")
    elif r_med is not None and r_med > r_target:
        hints.append(f"repairing a lost packet took {r_med:.0f} ms (median); the reference"
                     f" repair time is {r_target:.0f} ms. How long do you wait before declaring a loss,"
                     " and can you repair several holes in the same round trip?")
    if link_term < 0.8 and not hints:
        hints.append("the link was underused: does every retransmission shrink your window?")
    out["hints"] = hints or ["clean recovery. Can you push goodput closer to the link rate?"]
    return out


SCORERS = {"bufferbloat": score_bufferbloat, "incast": score_incast,
           "spraying": score_spraying}


def diagnose_stall(info):
    """Explains, from the switch log, why a run did not finish."""
    try:
        rows = load_log(info["switch_log"])
    except (OSError, KeyError):
        return []
    if not rows:
        return ["the switch saw no packets at all: did the handshake start?"]
    last_t = rows[-1].t
    hints = [f"the switch saw its last packet at t={last_t / 1e6:.1f}s; after that, nothing was sent."]
    acks = [p for p in rows if p.dir == 1]
    if acks and acks[-1].win < MSS:
        hints.append(f"the receiver's last ACK advertised a window of {acks[-1].win} bytes (less than"
                     " one packet) and no window update followed: a zero-window deadlock. Does your"
                     " receiver tell the sender when the application has read data?")
    return hints


def score(sc, info):
    if not info.get("finished"):
        return {"score": 0.0, "lines": [], "hints": diagnose_stall(info),
                "error": info.get("error", "did not finish")}
    try:
        out = SCORERS[sc["kind"]](sc, info)
    except (ValueError, KeyError, ZeroDivisionError, OSError) as e:
        return {"score": 0.0, "lines": [], "hints": [], "error": f"could not score the run: {e}"}
    res = info["result"]
    ok = res.get("ok", True) if "replies" not in res else all(r["ok"] for r in res["replies"])
    if not ok:
        out["error"] = "the receiving application got the wrong bytes -> score 0"
        out["score"] = 0.0
    return out


def hide(sc, res):
    """The same result with only relative numbers, for tests whose network must stay hidden
    (Gradescope): no rates, delays, sizes, times, or charts."""
    m = res.get("metrics")
    out = {"score": res["score"], "lines": [], "hints": []}
    if res.get("error"):
        out["error"] = res["error"]
    if m is None:
        if res.get("hints"):
            out["hints"] = ["the transfer stopped before it finished: after some point nothing"
                            " more was sent."] + res["hints"][1:]
        return out
    lines, hints = out["lines"], out["hints"]
    if sc["kind"] == "bufferbloat":
        link = m["goodput_bps"] / m["c_avail_bps"]
        rel = [m[k] / m["lag_target_ms"] for k in ("lag_p20_ms", "lag_p50_ms", "lag_p95_ms")]
        lines += [f"gdrive goodput   {link * 100:.0f}% of the available rate"
                  f"   (full marks from {BB_LINK_TARGET * 100:.0f}%)",
                  "gamer lag        p20 {:.1f}x   median {:.1f}x   p95 {:.1f}x the target"
                  "   (full marks at 1.0x or below)".format(*rel),
                  f"router drops     {m['drops']}"]
        if m["drops"] > 0:
            hints.append("the router dropped packets: your upload filled the whole buffer."
                         " Is loss the first signal you react to?")
        if link / BB_LINK_TARGET < 0.8:
            hints.append("the upload left much of the link unused: your sender is too cautious"
                         " somewhere.")
        elif rel[2] > 1:
            hints.append("a standing queue adds delay to every move. How much queue does this"
                         " link really need to stay busy?")
        if sc.get("events") and rel[2] > 2:
            hints.append("the network changed mid-upload. How many RTTs does your sender need"
                         " to notice?")
        hints[:] = hints or ["lag at or below the target earns full marks."]
    elif sc["kind"] == "incast":
        T, ideal = m["completion_s"], m["ideal_s"]
        extra = m["sent"] / m["needed"] - 1
        order = sorted(m["replies_s"])
        median = order[len(order) // 2]
        lines += [f"completion time   {T / ideal:.2f}x the ideal   (full marks up to"
                  f" {INCAST_SLACK:.2f}x)",
                  f"data packets      {max(0.0, extra) * 100:.0f}% more than needed"
                  f"     switch drops {m['drops']}",
                  f"slowest reply     {order[-1] / median:.1f}x the median reply's time"]
        if m["drops"] > 0:
            hints.append("at some moment the workers together had more in flight than the"
                         " switch can hold.")
        if T > 2 * ideal and order[-1] > 1.5 * median:
            hints.append("one reply finished long after the median: what was that worker"
                         " waiting for?")
        elif T > INCAST_SLACK * ideal and m["drops"] == 0:
            hints.append("no drops, but the link sat idle part of the time: are your windows"
                         " too small, or do waiting workers hear about free room too late?")
        if extra > 0.1:
            hints.append("extra packets were sent (retransmissions): each one lowers your score.")
        hints[:] = hints or (["near the ideal. Can you shave off the last few milliseconds?"]
                             if T <= INCAST_SLACK * ideal else
                             ["the link was not fully used: where did the time go?"])
    else:
        link = m["goodput_bps"] / m["c_avail_bps"]
        retx, nec, r_med = m["retx"], m["necessary"], m["repair_median_ms"]
        r_target = SPRAY_REPAIR_SLACK * m["repair_ideal_ms"]
        lines += [f"goodput           {link * 100:.0f}% of the available rate"
                  f"   (full marks from {SPRAY_LINK_TARGET * 100:.0f}%)",
                  f"retransmissions   {retx} total: {nec} necessary, {retx - nec} unnecessary",
                  "repair time       " + (f"median {r_med / r_target:.2f}x the reference repair time"
                                          if r_med is not None else "no packet was lost"),
                  f"switch drops      {m['drops']}"]
        if retx - nec > max(2, nec):
            hints.append(f"{retx - nec} of {retx} retransmissions were unnecessary: when does"
                         " your sender decide a packet is lost?")
        if r_med is not None and r_med > 1000:
            hints.append("repairing a lost packet took over a second (median). Are you waiting"
                         " for a timeout?")
        elif r_med is not None and r_med > r_target:
            hints.append("repairing a lost packet took longer than the reference repair time. How long do"
                         " you wait before declaring a loss, and can you repair several holes in"
                         " the same round trip?")
        if link / SPRAY_LINK_TARGET < 0.8 and not hints:
            hints.append("the link was underused: does every retransmission shrink your window?")
        hints[:] = hints or ["clean recovery. Can you push goodput closer to the link rate?"]
    return out


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, default=str)
