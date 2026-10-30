/**
 * Copyright (C) 2026 Carnegie Mellon University
 *
 * This file is part of the TCP in the Wild course project developed for the
 * Computer Networks course (15-441/641) taught at Carnegie Mellon University.
 *
 * Shared helpers for the lab applications.
 */

#include "common.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

uint64_t now_us(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000ull + (uint64_t)ts.tv_nsec / 1000ull;
}

uint8_t pattern_byte(uint32_t seed, uint64_t offset) {
  uint64_t x = offset * 0x9E3779B97F4A7C15ull + (uint64_t)seed * 0xBF58476D1CE4E5B9ull;
  x ^= x >> 31;
  x *= 0x94D049BB133111EBull;
  x ^= x >> 29;
  return (uint8_t)x;
}

void pattern_fill(uint8_t* buf, uint32_t len, uint32_t seed, uint64_t offset) {
  for (uint32_t i = 0; i < len; i++) buf[i] = pattern_byte(seed, offset + i);
}

void connect_or_die(cmu_socket_t* sock, int port) {
  if (cmu_socket(sock, TCP_INITIATOR, port, LAB_HOST_IP) < 0) {
    fprintf(stderr, "lab app: cmu_socket(initiator, %d) failed\n", port);
    exit(2);
  }
}

void listen_or_die(cmu_socket_t* sock, int port) {
  if (cmu_socket(sock, TCP_LISTENER, port, LAB_HOST_IP) < 0) {
    fprintf(stderr, "lab app: cmu_socket(listener, %d) failed\n", port);
    exit(2);
  }
}

void send_pattern(cmu_socket_t* sock, uint32_t bytes, uint32_t seed) {
  uint8_t* buf = malloc(bytes > 0 ? bytes : 1);
  pattern_fill(buf, bytes, seed, 0);
  cmu_write(sock, buf, (int)bytes);
  free(buf);
}

int recv_pattern(cmu_socket_t* sock, uint32_t bytes, uint32_t seed,
                 uint64_t* first_us, uint64_t* done_us,
                 void (*on_chunk)(cmu_socket_t*, uint32_t)) {
  uint8_t buf[65536];
  uint64_t got = 0;
  int ok = 1;
  *first_us = 0;
  while (got < bytes) {
    uint32_t want = (uint32_t)(bytes - got);
    if (want > sizeof(buf)) want = sizeof(buf);
    int n = cmu_read(sock, buf, (int)want, NO_FLAG);
    if (n <= 0) continue;
    if (*first_us == 0) *first_us = now_us();
    for (int i = 0; i < n && ok; i++)
      if (buf[i] != pattern_byte(seed, got + (uint64_t)i)) ok = 0;
    got += (uint64_t)n;
    if (on_chunk != NULL) on_chunk(sock, (uint32_t)n);
  }
  *done_us = now_us();
  return ok;
}

void park_forever(void) {
  for (;;) pause();
}

/*
 * Bulk transfer:
 *   <app> send <port> <bytes> <seed>
 *   <app> recv <port> <bytes> <seed> <result.json>
 */
int bulk_main(const char* name, int argc, char** argv) {
  if (argc >= 5 && !strcmp(argv[1], "send")) {
    cmu_socket_t sock;
    connect_or_die(&sock, atoi(argv[2]));
    send_pattern(&sock, (uint32_t)strtoul(argv[3], NULL, 10),
                 (uint32_t)strtoul(argv[4], NULL, 10));
    park_forever();
  }
  if (argc >= 6 && !strcmp(argv[1], "recv")) {
    cmu_socket_t sock;
    listen_or_die(&sock, atoi(argv[2]));
    uint32_t bytes = (uint32_t)strtoul(argv[3], NULL, 10);
    uint64_t first, done;
    int ok = recv_pattern(&sock, bytes, (uint32_t)strtoul(argv[4], NULL, 10),
                          &first, &done, NULL);
    FILE* f = fopen(argv[5], "w");
    fprintf(f, "{\"first_us\": %llu, \"done_us\": %llu, \"bytes\": %u, \"ok\": %s}\n",
            (unsigned long long)first, (unsigned long long)done, bytes,
            ok ? "true" : "false");
    fclose(f);
    _exit(0);
  }
  fprintf(stderr,
          "usage: %s send <port> <bytes> <seed>\n"
          "       %s recv <port> <bytes> <seed> <result.json>\n",
          name, name);
  return 1;
}
