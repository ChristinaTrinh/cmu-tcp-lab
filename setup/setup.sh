#!/usr/bin/env bash
# Installs everything the lab needs (Ubuntu 22.04).
set -e
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential gdb valgrind \
    python3 tcpdump tshark vim tmux git
