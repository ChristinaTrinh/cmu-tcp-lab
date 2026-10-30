/**
 * Copyright (C) 2026 Carnegie Mellon University
 *
 * This file is part of the TCP in the Wild course project developed for the
 * Computer Networks course (15-441/641) taught at Carnegie Mellon University.
 *
 * Shared helpers for the lab applications. All times are CLOCK_MONOTONIC
 * microseconds, the same clock cmunet uses, so app and switch timestamps can
 * be compared directly.
 */

#ifndef LAB_APPS_COMMON_H_
#define LAB_APPS_COMMON_H_

#include <stdint.h>

#include "cmu_tcp.h"

#define LAB_HOST_IP "127.0.0.1"

uint64_t now_us(void);

/** Deterministic content of a transfer: byte `offset` of stream `seed`. */
uint8_t pattern_byte(uint32_t seed, uint64_t offset);

/** Fills `buf` with `len` pattern bytes starting at stream offset `offset`. */
void pattern_fill(uint8_t* buf, uint32_t len, uint32_t seed, uint64_t offset);

/** Connects to `port` on the local host (through cmunet). Exits on failure. */
void connect_or_die(cmu_socket_t* sock, int port);

/** Listens on `port`. Exits on failure. */
void listen_or_die(cmu_socket_t* sock, int port);

/** Writes all of `bytes` pattern bytes of stream `seed` to `sock`. */
void send_pattern(cmu_socket_t* sock, uint32_t bytes, uint32_t seed);

/**
 * Reads exactly `bytes` bytes from `sock` and checks them against stream
 * `seed`. Stores the time the first and last byte were read. Returns 1 if the
 * content matched. Calls `on_chunk` (if not NULL) after every read.
 */
int recv_pattern(cmu_socket_t* sock, uint32_t bytes, uint32_t seed,
                 uint64_t* first_us, uint64_t* done_us,
                 void (*on_chunk)(cmu_socket_t*, uint32_t));

/** Blocks forever (keeps the transport's backend thread alive). */
void park_forever(void);

/** Runs a bulk-transfer app (gdrive and spray share it). */
int bulk_main(const char* name, int argc, char** argv);

#endif  // LAB_APPS_COMMON_H_
