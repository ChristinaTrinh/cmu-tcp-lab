"""Gradescope grading: runs a suite of tests and writes results.json."""

import json
import os
import tempfile
import time

from . import build, metrics, output, runner
from .scenarios import KINDS

FULL_CREDIT_SCORE = 60.0
RUNS_PER_TEST = 3  # each test counts the median of this many runs


def submitted_scenarios(root):
    """Challenges whose folder (root/<challenge>/src) exists."""
    return [k for k in KINDS if os.path.isdir(os.path.join(root, k, "src"))]


def run_one(sc, bins, out_dir):
    info = runner.run(sc, bins, out_dir)
    res = metrics.hide(sc, metrics.score(sc, info))  # students never see the parameters
    return res, output.format_result(sc["kind"], sc, res, hidden=True)


def grade(root, suite_path, results_path, work=None, grading_h=None, log=print):
    with open(suite_path) as f:
        suite = json.load(f)
    work = work or tempfile.mkdtemp(prefix="labgrade-")
    listed = submitted_scenarios(root)
    tests, board = [], []
    total = 0.0
    log(f"Challenge folders found: {', '.join(listed) or '(none)'}")
    for kind in KINDS:
        entries = [t for t in suite["tests"] if t["kind"] == kind]
        if not entries:
            continue
        bins, build_error = None, None
        if kind in listed:
            try:
                bins = build.build(os.path.join(root, kind), os.path.join(work, kind), grading_h)
            except build.BuildError as e:
                build_error = str(e)
        for t in entries:
            weight = float(t["points"])
            if kind not in listed:
                score, text = 0.0, f"{t['id']} ({kind}): not submitted (no {kind}/ folder)."
            elif build_error:
                score, text = 0.0, f"{t['id']} ({kind}): build failed\n{build_error}"
            else:
                started = time.time()
                runs = sorted((run_one(t, bins, os.path.join(work, kind, t["id"], str(r)))
                               for r in range(t.get("runs", RUNS_PER_TEST))),
                              key=lambda rt: rt[0]["score"])
                res, text = runs[len(runs) // 2]
                score = res["score"]
                all_scores = ", ".join(f"{r['score']:.1f}" for r, _ in runs)
                text = f"{t['id']}: " + text + f"\n  (median of {len(runs)} runs: {all_scores})"
                log(f"[staff] {t['id']} took {time.time() - started:.1f} s")
            points = weight * min(1.0, score / FULL_CREDIT_SCORE)
            total += score
            log(text + "\n")
            tests.append({"name": f"{t['id']} {t['title']}", "score": round(points, 2),
                          "max_score": weight, "output": text,
                          "visibility": "visible"})
            board.append({"name": t["id"], "value": round(score, 1)})
    board.append({"name": "Total", "value": round(total, 1), "order": "desc"})
    result = {"tests": tests, "leaderboard": board,
              "output": ("Each test: points = weight x min(1, score / 60). The leaderboard"
                         " shows raw scores (0-100 per test).")}
    with open(results_path, "w") as f:
        json.dump(result, f, indent=1)
    return result
