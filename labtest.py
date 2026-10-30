#!/usr/bin/env python3
"""CMU-TCP lab test tool.

Your projects live in this repository, one folder per challenge:
  baseline/      your Checkpoint 2 project, unchanged ("before" numbers)
  bufferbloat/   incast/   spraying/   a copy of it with your design

  ./labtest.py init [--from <dir>]             create baseline/, bufferbloat/, incast/ from your
                                               Checkpoint 2 project and test-build them
                                               (spraying is optional: cp -r baseline spraying)
  ./labtest.py run <challenge> [--build <folder>] [network options]
  ./labtest.py package                         make handin.tar.gz from your challenge folders

<challenge> is bufferbloat, incast, or spraying. `run` builds the folder with
the challenge's name; --build baseline runs your Checkpoint 2 code instead.
"""

import argparse
import copy
import os
import shutil
import sys
import tarfile
import tempfile

LAB_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(LAB_DIR, "src"))  # the lab tools live in src/

from labtool import build, grade, metrics, output, runner  # noqa: E402
from labtool.scenarios import KINDS, LOCAL  # noqa: E402

FOLDERS = ("baseline",) + KINDS
INIT_FOLDERS = ("baseline", "bufferbloat", "incast")  # spraying is optional: students copy it
# Where init copies your Checkpoint 2 project from: inside the lab container
# your cmu-tcp repository is mounted at /cmu-tcp; otherwise it sits next to this one.
DEFAULT_SOURCE = ("/cmu-tcp/project-2_15-441" if os.path.isdir("/cmu-tcp/project-2_15-441")
                  else os.path.join(os.path.dirname(LAB_DIR), "cmu-tcp", "project-2_15-441"))


def root_dir(args):
    """The directory that holds the challenge folders (staff: --tcp)."""
    return os.path.abspath(args.tcp) if getattr(args, "tcp", None) else LAB_DIR


def folder_or_exit(root, name):
    folder = os.path.join(root, name)
    if not os.path.isdir(os.path.join(folder, "src")):
        hint = ("start it from your baseline:  cp -r baseline spraying" if name == "spraying"
                else "run ./labtest.py init first")
        sys.exit(f"error: no project in {folder}; {hint}")
    return folder


# --------------------------------------------------------------------------
def cmd_init(args):
    root = root_dir(args)
    source = os.path.abspath(args.source or DEFAULT_SOURCE)
    if not os.path.isdir(os.path.join(source, "src")):
        sys.exit(f"error: no Checkpoint 2 project at {source}. Pass its path with --from.")
    for name in INIT_FOLDERS:
        folder = os.path.join(root, name)
        if os.path.exists(folder):
            print(f"  kept     {name}/  (already exists; init never overwrites your code)")
            continue
        shutil.copytree(source, folder, ignore=shutil.ignore_patterns(
            ".git", "*.o", "*.a", "*.pcap", "*.dSYM", "client", "server", "handin.tar.gz"))
        print(f"  created  {name}/  (copy of {source})")
    # Incast gets the host-information library, ready to #include.
    incast = os.path.join(root, "incast")
    for rel in ("inc/lab_host.h", "src/lab_host.c"):
        dst = os.path.join(incast, rel)
        if not os.path.exists(dst):
            shutil.copy(os.path.join(build.LIB_DIR, os.path.basename(rel)), dst)
            print(f"  added    incast/{rel}")
    print("\nTest-building every folder ...")
    work = tempfile.mkdtemp(prefix="labinit-")
    ok = True
    baseline_bins = None
    for name in INIT_FOLDERS:
        try:
            bins = build.build(os.path.join(root, name), os.path.join(work, name))
            if name == "baseline":
                baseline_bins = bins
            print(f"  [ok]   {name}/ builds")
        except build.BuildError as e:
            ok = False
            print(f"  [FAIL] {name}/ does not build\n{e}")
    if baseline_bins is not None:
        # A backend loop that spins at 100% CPU starves Incast's many receivers.
        try:
            cpu = runner.idle_cpu(baseline_bins, os.path.join(work, "idle"))
            if cpu > 0.5:
                print(f"\n  [warn] an idle connection of your code uses {cpu * 100:.0f}% of a CPU core:"
                      "\n         your backend loop spins. In Incast, dozens of connections run at once"
                      "\n         and starve each other of CPU; wait in poll() for ~1 ms instead.")
        except Exception:  # only a hint: never fail init because of it
            pass
    print("\nKeep baseline/ unchanged; put each design in its own folder."
          "\nSpraying (extra credit) is optional: start it with  cp -r baseline spraying"
          "\nNext: ./labtest.py run bufferbloat --build baseline" if ok
          else "\nFix the build errors above first.")


# --------------------------------------------------------------------------
def scenario_from_args(args):
    sc = copy.deepcopy(LOCAL[args.scenario])
    if args.rate_mbps is not None:
        sc["rate_bps"] = args.rate_mbps * 1e6
    if args.rtt_ms is not None:
        sc["rtt_ms"] = args.rtt_ms
    if args.buffer is not None:
        sc["buffer_pkts"] = args.buffer
    if args.bytes is not None and "bytes" in sc:
        sc["bytes"] = args.bytes
    if args.workers is not None or args.reply_kb is not None:
        if sc["kind"] != "incast":
            sys.exit("--workers/--reply-kb only apply to incast")
        n = args.workers or len(sc["replies"])
        size = int(args.reply_kb * 1024) if args.reply_kb else sc["replies"][0]
        sc["replies"] = [size] * n
    if args.paths_ms is not None:
        sc["paths_ms"] = [float(x) for x in args.paths_ms.split(",")]
    if args.loss is not None:
        sc["loss_random"] = args.loss
    if args.no_events:
        sc["events"] = []
    if args.seed is not None:
        sc["seed"] = args.seed
    if any(v is not None for v in (args.rate_mbps, args.rtt_ms, args.buffer, args.bytes,
                                   args.workers, args.reply_kb, args.paths_ms, args.loss)):
        sc["name"], sc["title"] = "custom", "Custom network"
    return sc


def cmd_run(args):
    build_name = args.build or args.scenario
    folder = folder_or_exit(root_dir(args), build_name)
    sc = scenario_from_args(args)
    out_dir = os.path.join(LAB_DIR, "results", args.scenario, build_name)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    print(f"Building {build_name}/ ...")
    try:
        bins = build.build(folder, os.path.join(out_dir, "build"))
    except build.BuildError as e:
        sys.exit(f"error: {e}")
    print("Running ...\n")
    info = runner.run(sc, bins, out_dir)
    res = metrics.score(sc, info)
    text = output.format_result(sc["kind"], sc, res)
    print(text)
    with open(os.path.join(out_dir, "summary.txt"), "w") as f:
        f.write(text + "\n")
    metrics.save_json(os.path.join(out_dir, "metrics.json"),
                      {"build": build_name, "scenario": sc, "score": res["score"],
                       "metrics": res.get("metrics", {}), "error": res.get("error")})
    saved = output.save_artifacts(sc["kind"], res, out_dir)
    # The switch's raw log is only for scoring; students see the summary.
    if os.path.exists(os.path.join(out_dir, "switch.csv")):
        os.remove(os.path.join(out_dir, "switch.csv"))
    rel = os.path.relpath(out_dir, LAB_DIR)
    print(f"\nSaved to {rel}/: summary.txt, metrics.json, the log of every process"
          + "".join(f", {os.path.basename(p)}" for p in saved))


# --------------------------------------------------------------------------
def cmd_package(args):
    root = root_dir(args)
    listed = grade.submitted_scenarios(root)
    work = tempfile.mkdtemp(prefix="labpackage-")
    for name in listed:
        try:
            build.build(os.path.join(root, name), os.path.join(work, name))
            print(f"  [ok]   {name}/ builds")
        except build.BuildError as e:
            sys.exit(f"  [FAIL] {name}/ does not build\n{e}")
    out = os.path.join(root, "handin.tar.gz")
    if os.path.exists(out):
        os.remove(out)

    def keep(ti):
        skip = ("/.git", "/build/", ".o", ".a", ".pcap", ".dSYM")
        return None if any(s in ti.name for s in skip) else ti
    with tarfile.open(out, "w:gz") as tar:
        for name in listed:
            tar.add(os.path.join(root, name), arcname=name, filter=keep)
    print(f"\nCreated {out} with: {', '.join(f'{n}/' for n in listed) or '(nothing)'}"
          "\nA challenge without its folder scores 0."
          "\nUpload it to the Gradescope assignment 'Project 2 lab: code'.")


def cmd_grade(args):
    grade.grade(os.path.abspath(args.tcp), args.suite, args.results, work=args.work,
                grading_h=args.grading_h)


# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the folders from your Checkpoint 2 project")
    s.add_argument("--from", dest="source",
                   help=f"your Checkpoint 2 project-2_15-441 (default {DEFAULT_SOURCE})")
    s.add_argument("--tcp", help=argparse.SUPPRESS)  # staff: another root directory
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="run the local test of a scenario")
    s.add_argument("scenario", choices=KINDS)
    s.add_argument("--build", choices=FOLDERS,
                   help="which folder to build (default: the challenge's own folder)")
    s.add_argument("--tcp", help=argparse.SUPPRESS)  # staff: test another project
    s.add_argument("--rate-mbps", type=float, help="bottleneck rate")
    s.add_argument("--rtt-ms", type=float, help="base round-trip time")
    s.add_argument("--buffer", type=int, help="bottleneck buffer (packets)")
    s.add_argument("--bytes", type=int, help="transfer size (bufferbloat, spraying)")
    s.add_argument("--workers", type=int, help="incast: number of workers")
    s.add_argument("--reply-kb", type=float, help="incast: reply size (KB)")
    s.add_argument("--paths-ms", help="spraying: extra delay per path, e.g. 0,3,6,9")
    s.add_argument("--loss", type=float, help="random loss probability (e.g. 0.01)")
    s.add_argument("--no-events", action="store_true", help="drop scheduled events")
    s.add_argument("--seed", type=int)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("package", help="create handin.tar.gz")
    s.add_argument("--tcp", help=argparse.SUPPRESS)  # staff: test another project
    s.set_defaults(func=cmd_package)

    s = sub.add_parser("grade", help=argparse.SUPPRESS)
    s.add_argument("--tcp", required=True)
    s.add_argument("--suite", required=True)
    s.add_argument("--results", required=True)
    s.add_argument("--work")
    s.add_argument("--grading-h")
    s.set_defaults(func=cmd_grade)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
