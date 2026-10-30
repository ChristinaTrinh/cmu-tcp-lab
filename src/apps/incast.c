/**
 * Copyright (C) 2026 Carnegie Mellon University
 *
 * This file is part of the TCP in the Wild course project developed for the
 * Computer Networks course (15-441/641) taught at Carnegie Mellon University.
 *
 * incast: the Incast challenge's distributed query. The aggregator is one
 * receiving process per worker (so your TCP never has to share a process
 * between connections); they share host information through lab_host.
 *
 *   incast recv <port> <bytes> <seed> <result.json>
 *       One of the aggregator's receivers: listens on <port>, registers the
 *       connection with lab_host, and prints "LISTENING" and then "JOINED".
 *       On SIGUSR1 ("Go!") it records the start time, reads the whole reply,
 *       and writes {go_us, done_us, bytes, ok} to <result.json>.
 *
 *   incast worker <port> <bytes> <seed>
 *       A worker: connects, waits for SIGUSR1 ("Go!"), then writes its whole
 *       reply and closes.
 */

#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "common.h"
#include "lab_host.h"

typedef struct {
  int port;
  uint32_t bytes;
  uint32_t seed;
  cmu_socket_t sock;
  uint64_t first_us, done_us;
  int ok;
} reply_t;

static void reply_progress(cmu_socket_t* s, uint32_t n) { lab_host_on_data(s, n); }

/* cmu_socket() may block until the handshake is done in some implementations,
 * so the socket is created on this thread, not in main. */
static void* serve_reply(void* arg) {
  reply_t* r = (reply_t*)arg;
  listen_or_die(&r->sock, r->port);
  lab_host_join(&r->sock, r->bytes);
  printf("JOINED\n");
  fflush(stdout);
  r->ok = recv_pattern(&r->sock, r->bytes, r->seed, &r->first_us, &r->done_us,
                       reply_progress);
  lab_host_leave(&r->sock);
  return NULL;
}

static sigset_t go_set(void) {
  sigset_t set;
  sigemptyset(&set);
  sigaddset(&set, SIGUSR1);
  return set;
}

int main(int argc, char** argv) {
  if (argc >= 6 && !strcmp(argv[1], "recv")) {
    sigset_t set = go_set();
    pthread_sigmask(SIG_BLOCK, &set, NULL);  // inherited by every thread
    reply_t r;
    memset(&r, 0, sizeof(r));
    r.port = atoi(argv[2]);
    r.bytes = (uint32_t)strtoul(argv[3], NULL, 10);
    r.seed = (uint32_t)strtoul(argv[4], NULL, 10);
    pthread_t thread;
    pthread_create(&thread, NULL, serve_reply, &r);
    printf("LISTENING\n");
    fflush(stdout);
    int sig;
    sigwait(&set, &sig);
    uint64_t go = now_us();
    pthread_join(thread, NULL);
    FILE* f = fopen(argv[5], "w");
    fprintf(f, "{\"go_us\": %llu, \"done_us\": %llu, \"bytes\": %u, \"ok\": %s}\n",
            (unsigned long long)go, (unsigned long long)r.done_us, r.bytes,
            r.ok ? "true" : "false");
    fclose(f);
    _exit(0);
  }
  if (argc >= 5 && !strcmp(argv[1], "worker")) {
    sigset_t set = go_set();
    pthread_sigmask(SIG_BLOCK, &set, NULL);
    cmu_socket_t sock;
    connect_or_die(&sock, atoi(argv[2]));
    int sig;
    sigwait(&set, &sig);
    send_pattern(&sock, (uint32_t)strtoul(argv[3], NULL, 10),
                 (uint32_t)strtoul(argv[4], NULL, 10));
    cmu_close(&sock);
    park_forever();
  }
  fprintf(stderr,
          "usage: incast recv <port> <bytes> <seed> <result.json>\n"
          "       incast worker <port> <bytes> <seed>\n");
  return 1;
}
