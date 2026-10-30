# 15-441/641 Project 2 Lab: TCP Meets the Real World

In this lab you take your Checkpoint 2 TCP into three real-world networks and
make it work well in each:

- **Bufferbloat** (required): a big upload and an online game share a home
  router. Keep the upload fast and the game's lag low.
- **Incast** (required): many servers answer one query at the same instant
  through a small switch buffer. Make every answer arrive fast, without
  timeouts.
- **Packet spraying** (extra credit): the network spreads each flow's packets
  over several paths, so they arrive out of order. Tell late packets from lost
  ones and repair real losses quickly.


## Setup

The lab runs in a container (Docker + Vagrant), like the checkpoints. Clone
this repository **next to** your existing `cmu-tcp` repository, then:

```bash
git clone https://github.com/computer-networks/cmu-tcp-lab.git
cd cmu-tcp-lab
# pass your cmu-tcp dir that contains project-2_15-441 folder in CMU_TCP=
CMU_TCP=../cmu-tcp vagrant up --provider=docker  
vagrant ssh
cd /lab
./labtest.py init      # creates baseline/, bufferbloat/, incast/ and test-builds them
```

Inside the container, this repository is `/lab`, the same folder as
`cmu-tcp-lab` on your machine, so you can edit with any editor on the host.
Your old `cmu-tcp` repository is not used again: **from now on, all your edits
happen in this repository**, in the folders below.

## Your folders

`init` copies your Checkpoint 2 project (the `project-2_15-441` folder in the
`cmu-tcp` directory you passed in `CMU_TCP=`) into three folders in this
repository, then test-builds each one:

```
cmu-tcp/project-2_15-441  ->  cmu-tcp-lab/baseline/
                          ->  cmu-tcp-lab/bufferbloat/
                          ->  cmu-tcp-lab/incast/
```

Spraying is optional extra credit, so `init` does not create its folder. If you
attempt it, create it yourself: `cp -r baseline spraying`.

| Folder | What it is |
|---|---|
| `baseline/` | Your Checkpoint 2 project. Keep it unchanged: it gives the "before" numbers in your report. |
| `bufferbloat/`, `incast/` | One copy per challenge. Change anything you like in each. |
| `spraying/` | Optional (extra credit), created by you from `baseline/`. |

> **Incast: extra information for your receiver (`lab_host.h`)**
>
> Your `incast/` folder has a new file, `inc/lab_host.h` (with
> `src/lab_host.c`). Include it in the file that needs it:
>
> ```c
> #include "lab_host.h"
> ```
>
> It returns additional information that may help your design: how many
> connections on this host are still receiving and this connection's position
> among them, how many bytes this connection still has to deliver, the host's
> link rate, the round-trip time with empty queues, and the current total
> arrival rate. These functions are for the receiving side (your
> `TCP_LISTENER` sockets on the aggregator); on a worker's sending socket they
> return -1 or 0. The comments in the header describe each function. Using it
> is optional.

## Commands

```bash
./labtest.py run bufferbloat                   # build bufferbloat/, run the local test
./labtest.py run bufferbloat --build baseline  # same test with your Checkpoint 2 code
./labtest.py run incast --workers 12 --reply-kb 48 --buffer 16   # same challenge, in your cutomized network setting
./labtest.py run --help                        # all network options
./labtest.py package                           # create handin.tar.gz for submission
```

Each run prints a score, measurements, and tips, and saves them under
`results/<challenge>/<folder>/`: `summary.txt` (what was printed),
`metrics.json`, the output of every process (`<process>.log`, where your
`printf`s go), the compiler output (`build/build.log`), and for Spraying
`retransmissions.csv`.

## What is in this repository

| Path | Contents |
|---|---|
| `labtest.py` | The test runner (Python 3, standard library only). |
| `src/labtool/` | Building, running, scoring, and formatting. Scores are defined in `src/labtool/metrics.py`. |
| `src/cmunet/cmunet.c` | The network emulator: a bottleneck queue, delays, loss, and packet spraying, with a log of every packet that reaches the switch. |
| `src/apps/` | The applications: `gdrive.c` and `gamer.c` (Bufferbloat), `incast.c` (Incast), `spray.c` (Spraying). They only use the `cmu_socket`/`cmu_read`/`cmu_write`/`cmu_close` API. |
| `src/lib/` | The official `lab_host.h` and `lab_host.c` (Incast); `init` copies them into `incast/`, but the tests always link the official copy here. |
| `.agent/` | Instructions for AI agents (`AGENT.md`, `P2LABLEARNINGGOALS.md`). |
| `Vagrantfile`, `setup/` | The lab container. |
