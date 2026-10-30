"""Network scenarios for the CMU-TCP lab.

A scenario is a plain dict. The local tests below are the simplest version of
each challenge; Gradescope uses its own suite with the same keys.

Common keys:
  kind         "bufferbloat" | "incast" | "spraying"
  name         short label shown in the output
  title        human-readable title
  rate_bps     bottleneck link rate (bits/s)
  rtt_ms       base round-trip time (split evenly between the two directions)
  buffer_pkts  bottleneck buffer (packets)
  events       list of events. Timed ones, {"t_ms": ..., <event>: value} with
               t relative to the first data packet: rate_bps, fwd_delay_ms.
               Loss events are placed in the byte stream so the same packets
               are lost in every run: {"at_byte": offset, "burst_loss": n
               packets} or {"at_byte": offset, "path_down": [path, length in
               bytes]}.
  limit_s      wall-clock limit for the run
"""

LOCAL = {
    "bufferbloat": {
        "kind": "bufferbloat",
        "name": "local",
        "title": "Local test",
        "rate_bps": 4e6,
        "rtt_ms": 20,
        "buffer_pkts": 30,
        "bytes": 2_000_000,
        "gamer_interval_ms": 20,
        "events": [],
        "limit_s": 60,
    },
    "incast": {
        "kind": "incast",
        "name": "local",
        "title": "Local test",
        "rate_bps": 10e6,
        "rtt_ms": 10,
        "buffer_pkts": 24,
        "replies": [65536] * 8,
        "limit_s": 60,
    },
    "spraying": {
        "kind": "spraying",
        "name": "local",
        "title": "Local test",
        "rate_bps": 10e6,
        "rtt_ms": 10,
        "buffer_pkts": 100,
        "bytes": 2_000_000,
        "paths_ms": [0, 1.5, 3, 4.5],
        "ack_paths_ms": [],
        "loss_random": 0.0,
        "events": [{"at_byte": 1000000, "burst_loss": 5}],
        "limit_s": 60,
    },
}

KINDS = ("bufferbloat", "incast", "spraying")

# Payload bytes per full packet when no extension is used (MAX_LEN - header).
MSS = 1375
WIRE = 1400


def describe(sc):
    """One-line description of a scenario's network."""
    rate = sc["rate_bps"] / 1e6
    parts = [f"link {rate:g} Mbps", f"RTT {sc['rtt_ms']:g} ms"]
    if sc["kind"] == "incast":
        n = len(sc["replies"])
        sizes = sorted(set(sc["replies"]))
        size = (f"{sizes[0] // 1024} KB" if len(sizes) == 1 else
                f"{sizes[0] // 1024}-{sizes[-1] // 1024} KB")
        parts.insert(0, f"{n} workers x {size}")
        parts.append(f"switch buffer {sc['buffer_pkts']} pkts")
    elif sc["kind"] == "bufferbloat":
        parts.append(f"router buffer {sc['buffer_pkts']} pkts")
    else:
        paths = "/".join(f"{p:g}" for p in sc["paths_ms"])
        parts.insert(0, f"{len(sc['paths_ms'])} paths +{paths} ms")
    for ev in sc.get("events", []):
        if "at_byte" in ev:
            mb = ev["at_byte"] / 1e6
            if "burst_loss" in ev:
                parts.append(f"{ev['burst_loss']} packets lost at {mb:g} MB")
            if "path_down" in ev:
                parts.append(f"path {ev['path_down'][0]} down for "
                             f"{ev['path_down'][1] / 1e3:g} KB at {mb:g} MB")
            continue
        t = ev["t_ms"] / 1000
        if "rate_bps" in ev:
            parts.append(f"rate -> {ev['rate_bps'] / 1e6:g} Mbps @{t:g}s")
        if "fwd_delay_ms" in ev:
            parts.append(f"path delay changes @{t:g}s")
    if sc.get("loss_random"):
        parts.append(f"{sc['loss_random'] * 100:g}% random loss")
    return ", ".join(parts)
