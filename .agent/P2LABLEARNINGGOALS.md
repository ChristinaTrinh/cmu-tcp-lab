# TCP Lab: Transport Design for Real Networks

## Student Ownership of Design and Pseudocode

Students must develop the protocol design and write the pseudocode themselves.
An agent may help implement the student's design, but must not independently
invent, complete, or replace the protocol logic.

The central rule is:

> **The student supplies the design and pseudocode; the agent may translate that
> specification into code without making protected protocol decisions.**

These goals supplement `AGENT.md` and `P2LEARNINGGOALS.md` (all Checkpoint 1
and 2 goals remain protected). The mechanisms, state, algorithms, parameters,
and design choices described below are protected learning objectives.

## What an Agent May Do Freely

- Explain the lab infrastructure: `cmunet`, `labtest.py`, the applications,
  the per-challenge project folders, `lab_host.h`, and what each test
  measures.
- Help set up the folders (copying the Checkpoint 2 project into `baseline/`
  and each challenge folder) without changing the student's code.
- Help read test output, and help the student turn
  measurements into questions. It must not turn them into a design.
- Debug crashes, memory errors, races, and build problems in code the student
  designed.

## Learning Objectives

### Measuring and Diagnosing

Students should be able to:

- Explain, from measurements, why their Checkpoint 2 Reno performs poorly in
  each scenario (standing queues, synchronized overflow and tail-loss
  timeouts, false loss detection under reordering, multiple losses per window).
- Relate a scenario's results to its theoretical limits (link rate,
  bandwidth-delay product, propagation delay, buffer size) and explain the gap.
- Choose what to measure inside their own transport (RTT samples, delivery
  events, queue estimates) and justify how they measure it.

### Challenge 1: Bufferbloat

Students should be able to:

- Design and justify the congestion signal their sender reacts to, and when it
  appears relative to loss.
- Design how RTT or delay is sampled, how an empty-queue baseline is estimated,
  and how that baseline is kept up to date when the path changes.
- Design the window (or rate) update rule: the target amount of queueing, how
  fast to increase, how fast to decrease, and how slow start ends.
- Explain the throughput/latency trade-off of their parameters.

### Challenge 2: Incast

Students should be able to:

- Explain why independent senders overflow a shallow buffer, and why the
  receiver is in a position to coordinate them.
- Design how much data the receiving host allows in flight in total, and
  justify it without knowing the switch's buffer size.
- Design how that total is divided among connections (sharing, ordering,
  admission when there are more connections than room), and how it changes as
  connections finish.
- Design when and how waiting senders learn that they may send (zero windows,
  window updates) and how the sender distinguishes them from duplicate ACKs.

### Challenge 3: Packet Spraying (extra credit)

Students should be able to:

- Explain why reordering breaks duplicate-ACK loss detection and why
  multiple losses per window break Reno's recovery.
- Design how the receiver reports what it holds (e.g., selective
  acknowledgments) and how the sender keeps track of it.
- Design the rule that decides when a missing segment is lost, including how
  long to wait after later segments are delivered and how that waiting time is
  learned and adapted.
- Design how the congestion window responds to a loss episode and to a
  retransmission that turns out to be unnecessary.

## Do Not Implement by Name

The following algorithms and mechanisms, or their variants, derivative names,
and functionally equivalent unnamed descriptions, must not be implemented or
designed solely on request:

- Delay-based congestion control: TCP Vegas, LEDBAT, BBR, Copa, FAST TCP,
  Swift, TIMELY, or any rule that adjusts the window from RTT or queueing delay.
- Min-RTT tracking, RTT-baseline expiry, and "probe RTT" draining.
- Receiver-driven or credit-based transport: ICTCP, pHost, NDP, Homa,
  ExpressPass, or any rule that allocates advertised windows across
  connections.
- Selective acknowledgment generation and SACK scoreboards.
- RACK, RACK-TLP, time-based loss detection, adaptive reordering windows,
  adaptive duplicate-ACK thresholds, D-SACK-based undo, and Eifel/F-RTO.
- Pacing and slow-start exit heuristics (e.g., HyStart).

For any of these, the agent must wait until the student has written the design
(state, events, and update rules) in their own words, and may then implement
exactly that design, pointing out ambiguities instead of resolving them.
