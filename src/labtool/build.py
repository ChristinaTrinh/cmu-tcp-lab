"""Builds one of a student's project folders and links the lab apps to it.

A folder is a complete CMU-TCP project (src/, inc/). We compile every
src/*.c except the student's own client.c, server.c, and lab_host.c, add the
lab's src/lib/lab_host.c, and link the result with our applications. The student's
Makefile is not used. The folder is copied to a scratch directory first, so
building never touches the student's files.
"""

import glob
import os
import shutil
import subprocess

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # cmu-tcp-lab/src
LIB_DIR = os.path.join(SRC_DIR, "lib")
APPS_DIR = os.path.join(SRC_DIR, "apps")
CMUNET_SRC = os.path.join(SRC_DIR, "cmunet", "cmunet.c")

APPS = ["gdrive", "gamer", "incast", "spray"]
SKIP = {"client.c", "server.c", "lab_host.c"}  # the apps replace client/server
CFLAGS = ["-pthread", "-fPIC", "-g", "-DDEBUG"]  # the starter Makefile's flags


class BuildError(Exception):
    pass


# The starter create_packet() allocates no room for header extensions, so any
# packet with extension data overflows the heap. The lab fixes that line.
PACKET_BUG = "uint8_t* packet = malloc(sizeof(cmu_tcp_header_t) + payload_len);"
PACKET_FIX = "uint8_t* packet = malloc(sizeof(cmu_tcp_header_t) + ext_len + payload_len);"


def fix_create_packet(project):
    """Patches the starter create_packet() bug in place. Returns True if patched."""
    path = os.path.join(project, "src", "cmu_packet.c")
    if not os.path.exists(path):
        return False
    with open(path) as f:
        src = f.read()
    if PACKET_BUG not in src:
        return False
    with open(path, "w") as f:
        f.write(src.replace(PACKET_BUG, PACKET_FIX))
    return True


def _run(cmd, cwd, log):
    with open(log, "a") as f:
        f.write("$ " + " ".join(cmd) + "\n")
        f.flush()
        proc = subprocess.run(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)
    return proc.returncode


def _tail(path, n=25):
    try:
        with open(path) as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def build_cmunet(work):
    os.makedirs(work, exist_ok=True)
    out = os.path.join(work, "cmunet")
    if (not os.path.exists(out) or
            os.path.getmtime(out) < os.path.getmtime(CMUNET_SRC)):
        log = os.path.join(work, "cmunet-build.log")
        if _run(["gcc", "-O2", "-o", out, CMUNET_SRC], work, log) != 0:
            raise BuildError("could not build cmunet:\n" + _tail(log))
    return out


def build(folder, work, grading_h=None):
    """Builds the project in `folder` inside `work`.

    `grading_h`, if given, replaces the folder's inc/grading.h (Gradescope).
    Returns a dict mapping app name -> executable path.
    """
    if not os.path.isdir(os.path.join(folder, "src")):
        raise BuildError(f"{folder} is not a CMU-TCP project (no src/ folder)")
    os.makedirs(work, exist_ok=True)
    copy = os.path.join(work, "project")
    if os.path.exists(copy):
        shutil.rmtree(copy)
    shutil.copytree(folder, copy, ignore=shutil.ignore_patterns(
        ".git", "*.o", "*.a", "*.pcap", "*.dSYM", "handin.tar.gz"))
    if grading_h:
        shutil.copy(grading_h, os.path.join(copy, "inc", "grading.h"))
    fix_create_packet(copy)

    log = os.path.join(work, "build.log")
    if os.path.exists(log):
        os.remove(log)
    bins = {}
    try:
        includes = ["-I", os.path.join(copy, "inc"), "-I", LIB_DIR]
        sources = [p for p in sorted(glob.glob(os.path.join(copy, "src", "*.c")))
                   if os.path.basename(p) not in SKIP]
        sources.append(os.path.join(LIB_DIR, "lab_host.c"))
        objs = []
        for src in sources:
            obj = os.path.join(copy, os.path.basename(src)[:-2] + ".o")
            if _run(["gcc", *CFLAGS, *includes, "-c", src, "-o", obj], copy, log) != 0:
                raise BuildError(f"could not compile {os.path.basename(src)}:\n{_tail(log)}")
            objs.append(obj)
        for app in APPS:
            out = os.path.join(work, app)
            cmd = ["gcc", "-O2", "-pthread", *includes, "-I", APPS_DIR,
                   os.path.join(APPS_DIR, f"{app}.c"), os.path.join(APPS_DIR, "common.c"),
                   *objs, "-lm", "-o", out]
            if _run(cmd, work, log) != 0:
                raise BuildError(f"could not link the '{app}' app with your code:\n{_tail(log)}")
            bins[app] = out
    finally:
        # The apps contain everything they need; the scratch copy can go.
        shutil.rmtree(copy, ignore_errors=True)
    bins["cmunet"] = build_cmunet(work)
    return bins
