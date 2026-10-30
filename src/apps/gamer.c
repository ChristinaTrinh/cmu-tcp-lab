/**
 * Copyright (C) 2026 Carnegie Mellon University
 *
 * This file is part of the TCP in the Wild course project developed for the
 * Computer Networks course (15-441/641) taught at Carnegie Mellon University.
 *
 * gamer: the Bufferbloat challenge's online game. It sends a 100-byte "move"
 * every few milliseconds through your CMU-TCP; each move carries the time it
 * was written. The receiver logs when each move is read, so the difference
 * is the lag the player feels.
 *
 *   gamer send <port> <interval ms> <count>
 *   gamer recv <port> <lag.csv>          (writes "idx,send_us,recv_us" rows after a header)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "common.h"

#define MOVE_LEN 100
#define MOVE_MAGIC 0x47414D45u  // "GAME"

static void put64(uint8_t* p, uint64_t v) {
  for (int i = 7; i >= 0; i--, v >>= 8) p[i] = (uint8_t)v;
}
static uint64_t get64(const uint8_t* p) {
  uint64_t v = 0;
  for (int i = 0; i < 8; i++) v = (v << 8) | p[i];
  return v;
}

int main(int argc, char** argv) {
  if (argc >= 5 && !strcmp(argv[1], "send")) {
    cmu_socket_t sock;
    connect_or_die(&sock, atoi(argv[2]));
    uint64_t interval = (uint64_t)atoi(argv[3]) * 1000ull;
    uint32_t count = (uint32_t)strtoul(argv[4], NULL, 10);
    uint64_t next = now_us();
    for (uint32_t i = 0; i < count; i++) {
      while (now_us() < next) usleep(200);
      uint8_t move[MOVE_LEN];
      memset(move, 0, sizeof(move));
      put64(move, ((uint64_t)MOVE_MAGIC << 32) | i);
      put64(move + 8, now_us());
      cmu_write(&sock, move, MOVE_LEN);
      next += interval;
    }
    park_forever();
  }
  if (argc >= 4 && !strcmp(argv[1], "recv")) {
    cmu_socket_t sock;
    listen_or_die(&sock, atoi(argv[2]));
    FILE* out = fopen(argv[3], "w");
    fprintf(out, "idx,send_us,recv_us\n");
    fflush(out);
    uint8_t buf[MOVE_LEN * 64];
    uint8_t move[MOVE_LEN];
    int have = 0;
    for (;;) {
      int n = cmu_read(&sock, buf, sizeof(buf), NO_FLAG);
      uint64_t t = now_us();
      for (int i = 0; i < n; i++) {
        move[have++] = buf[i];
        if (have < MOVE_LEN) continue;
        have = 0;
        uint64_t head = get64(move);
        if ((uint32_t)(head >> 32) != MOVE_MAGIC) {
          fprintf(out, "corrupt,0,%llu\n", (unsigned long long)t);
          continue;
        }
        fprintf(out, "%u,%llu,%llu\n", (uint32_t)head,
                (unsigned long long)get64(move + 8), (unsigned long long)t);
      }
      fflush(out);
    }
  }
  fprintf(stderr,
          "usage: gamer send <port> <interval ms> <count>\n"
          "       gamer recv <port> <lag.csv>\n");
  return 1;
}
