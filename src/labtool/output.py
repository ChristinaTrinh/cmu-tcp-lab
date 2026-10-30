"""Formats test results as text and saves the retransmission list."""

import csv
import os

from .scenarios import describe


def ascii_chart(title, series, top, secs, width=60):
    if not series:
        return []
    top = max(top, max(series), 1)
    rows = []
    for level, label in ((top, f"{top:>4}"), (top / 2, f"{int(top / 2):>4}"), (0, "   0")):
        line = ""
        for v in series[:width]:
            if level == 0:
                line += "#" if v > 0 else "_"
            else:
                line += "#" if v >= level * 0.999 and v > 0 else " "
        rows.append(f"  {label} |{line}")
    rows.insert(0, f"  {title}")
    rows.append(f"       0s{' ' * (len(series[:width]) - 8)}{secs:.1f}s")
    return rows


def format_result(kind, sc, res, hidden=False):
    """hidden: describe the test by its blurb instead of its network parameters."""
    about = sc.get("blurb", "") if hidden else describe(sc)
    head = f"[{kind}/{sc['name']}] {sc.get('title', '')}: {about}"
    out = [head]
    if res.get("error"):
        out.append(f"  ERROR: {res['error']}")
    for line in res.get("lines", []):
        out.append("  " + line)
    out.append(f"  SCORE {res['score']:.1f} / 100")
    for h in res.get("hints", []):
        out.append(f"  tip: {h}")
    if res.get("chart"):
        out.extend(ascii_chart(*res["chart"]))
    return "\n".join(out)


def save_artifacts(kind, res, out_dir):
    """Writes the list of retransmissions (Spraying) as a CSV file."""
    saved = []
    if res.get("retx_rows"):
        path = os.path.join(out_dir, "retransmissions.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_s", "seq_relative", "necessary", "note"])
            w.writerows(res["retx_rows"])
        saved.append(path)
    return saved
