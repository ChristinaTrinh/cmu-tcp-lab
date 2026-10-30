/**
 * Copyright (C) 2026 Carnegie Mellon University
 *
 * This file is part of the TCP in the Wild course project developed for the
 * Computer Networks course (15-441/641) taught at Carnegie Mellon University.
 *
 * cmunet: a deterministic user-space network emulator for the CMU-TCP lab.
 *
 * cmunet relays CMU-TCP packets between initiators (clients) and listeners
 * (servers) over UDP on 127.0.0.1. Every flow has a "listen" port that the
 * client connects to and a "server" port where the real listener runs.
 *
 *   client --> [listen port] --> bottleneck queue --> sprayer/paths --> server
 *   client <-- [listen port] <------------- reverse path <-------------- server
 *
 * The forward direction (client -> server) crosses one bottleneck shared by
 * all flows: a FIFO of `buffer_pkts` packets drained at `rate_bps`, followed
 * by a propagation delay and (optionally) one of several parallel paths with
 * extra delay. The reverse direction (ACKs) only has a propagation delay and
 * optional per-path extra delay.
 *
 * Every packet that reaches the switch is logged, including dropped ones.
 *
 * Decisions are deterministic: they depend on the packet, not on when it
 * arrives. Sequence numbers are taken relative to the connection's SYN (stream
 * offsets), so a random initial sequence number changes nothing. A data
 * packet's path is a hash of (flow, offset), and each
 * retransmitted copy of the same bytes moves to the next path, as in spraying
 * fabrics that change a retransmission's path. Random loss is a hash of
 * (flow, offset, copy number), so the same packets are lost in every
 * run. ACK paths are a hash of (flow, n-th reverse packet).
 *
 * Usage: cmunet <config file> <log csv>
 *
 * Config file (one directive per line, '#' starts a comment):
 *   seed <n>
 *   rate_bps <bits per second>
 *   buffer_pkts <packets>
 *   fwd_delay_us <us>              one-way delay client -> server
 *   rev_delay_us <us>              one-way delay server -> client
 *   paths <us>,<us>,...            extra forward delay of each sprayed path
 *   ack_paths <us>,<us>,...        extra reverse delay of each sprayed path
 *   loss_random <probability>      random loss of forward data packets
 *   event <t_us> rate_bps <bps>             t is relative to the first data packet
 *   event <t_us> fwd_delay_us <us>
 *   seq_loss <start> <end>         lose the first copy of every data packet whose
 *                                  stream offset (bytes after the SYN) is in [start, end)
 *   seq_path_down <path> <start> <end>   that path drops every data packet whose
 *                                  stream offset is in [start, end)
 *   flow <id> <listen port> <server port>
 *
 * stdout: "READY", "T0 <monotonic us>", "ESTABLISHED <flow>", "FIRSTDATA <t>".
 *
 * Log columns: t_us,flow,dir,ev,seq,ack,plen,hlen,flags,win,info,pid
 *   pid = unique id of the packet copy (assigned when it reaches the switch)
 *   ev = A (data/ctrl arrived at switch), D (dropped; info = q|l|b|p),
 *        Q (left the queue; info = queueing delay us), P (path chosen; info),
 *        V (delivered to server), R (reverse packet from server).
 */

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define MAX_FLOWS 128
#define MAX_PATHS 16
#define MAX_EVENTS 64
#define MAX_PKT 2048
#define HDR_LEN 25

#define SYN 0x8
#define ACK 0x4

typedef struct {
  uint64_t t_us;
  int kind;  // 0 rate, 1 fwd_delay
  double v1;
  int fired;
} sched_event_t;

typedef struct {
  int id;
  int listen_fd;
  int up_fd;
  struct sockaddr_in client;
  int have_client;
  struct sockaddr_in server;
  int saw_syn, saw_synack, established;
  uint32_t isn;  // sequence number of the client's SYN
} flow_t;

typedef struct pkt {
  int flow;
  int dir;  // 0 forward (to server), 1 reverse (to client)
  uint16_t len;
  uint64_t enq_us;
  uint64_t due_us;
  uint64_t pid;
  uint32_t copy;  // 0 = first transmission of these bytes, 1 = first retransmission, ...
  uint8_t data[MAX_PKT];
  struct pkt* next;
} pkt_t;

/* ---- configuration ---- */
static uint64_t seed = 1;
static double rate_bps = 10e6;
static int buffer_pkts = 1000;
static uint64_t fwd_delay_us = 10000, rev_delay_us = 10000;
static int npaths = 0, nackpaths = 0;
static uint64_t path_us[MAX_PATHS], ackpath_us[MAX_PATHS];
static double loss_random = 0.0;
static sched_event_t events[MAX_EVENTS];
static int nevents = 0;
#define MAX_RANGES 16
typedef struct {
  int path;  // -1 for seq_loss
  uint32_t start, end;
} seq_range_t;
static seq_range_t ranges[MAX_RANGES];
static int nranges = 0;
static flow_t flows[MAX_FLOWS];
static int nflows = 0;

/* ---- runtime state ---- */
static volatile sig_atomic_t stop = 0;
static uint64_t t0_abs;
static FILE* logf;
static int64_t first_data_us = -1;
static uint64_t next_pid = 1;
static uint64_t rev_count[MAX_FLOWS];

/* ---- deterministic hashing ---- */
static uint64_t mix64(uint64_t x) {
  x += 0x9E3779B97F4A7C15ull;
  x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
  x = (x ^ (x >> 27)) * 0x94D049BB133111EBull;
  return x ^ (x >> 31);
}
static uint64_t hash3(uint64_t a, uint64_t b, uint64_t c) {
  return mix64(seed ^ mix64(a ^ mix64(b ^ mix64(c))));
}
static double hash_unit(uint64_t h) { return (double)(h >> 11) / (double)(1ull << 53); }

/* How many times each (flow, seq) has reached the switch before. */
#define COPY_SLOTS (1 << 17)
static struct { uint32_t used, flow, seq, count; } copies[COPY_SLOTS];
static uint32_t copy_index(int flow, uint32_t seq) {
  uint32_t i = (uint32_t)(hash3(1, (uint64_t)flow, seq) & (COPY_SLOTS - 1));
  for (int probe = 0; probe < COPY_SLOTS; probe++, i = (i + 1) & (COPY_SLOTS - 1)) {
    if (!copies[i].used) {
      copies[i].used = 1;
      copies[i].flow = (uint32_t)flow;
      copies[i].seq = seq;
      copies[i].count = 1;
      return 0;
    }
    if (copies[i].flow == (uint32_t)flow && copies[i].seq == seq) return copies[i].count++;
  }
  return 0;
}

static pkt_t *q_head = NULL, *q_tail = NULL;  // bottleneck FIFO (includes the
static int q_len = 0;                          // packet in transmission)
static uint64_t tx_done_us = 0;                // when the head finishes
static int tx_busy = 0;

static pkt_t* inflight = NULL;  // packets in propagation, sorted by due_us

static uint64_t abs_now_us(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000ull + (uint64_t)ts.tv_nsec / 1000ull;
}
static uint64_t now_us(void) { return abs_now_us() - t0_abs; }

static void on_signal(int sig) {
  (void)sig;
  stop = 1;
}

/* ---- header helpers (network byte order, packed layout) ---- */
static uint32_t rd32(const uint8_t* p) {
  return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
         ((uint32_t)p[2] << 8) | p[3];
}
static uint16_t rd16(const uint8_t* p) { return (uint16_t)((p[0] << 8) | p[1]); }

typedef struct {
  uint32_t seq, ack;
  uint16_t hlen, plen, win;
  uint8_t flags;
  int valid;
} hdr_t;

static hdr_t parse(const uint8_t* d, int len) {
  hdr_t h;
  memset(&h, 0, sizeof(h));
  if (len < HDR_LEN || rd32(d) != 15441) return h;
  h.seq = rd32(d + 8);
  h.ack = rd32(d + 12);
  h.hlen = rd16(d + 16);
  h.plen = rd16(d + 18);
  h.flags = d[20];
  h.win = rd16(d + 21);
  h.valid = 1;
  return h;
}

static void log_pkt(uint64_t t, int flow, int dir, char ev, const uint8_t* d,
                    int len, long long info, uint64_t pid) {
  hdr_t h = parse(d, len);
  fprintf(logf, "%llu,%d,%d,%c,%u,%u,%u,%u,%u,%u,%lld,%llu\n",
          (unsigned long long)t, flow, dir, ev, h.seq, h.ack, h.plen, h.hlen,
          h.flags, h.win, info, (unsigned long long)pid);
}

static int is_data(const hdr_t* h) {
  return h->valid && h->plen > h->hlen && !(h->flags & SYN);
}

/* ---- config parsing ---- */
static void parse_list(const char* s, uint64_t* out, int* n) {
  *n = 0;
  char buf[512];
  strncpy(buf, s, sizeof(buf) - 1);
  buf[sizeof(buf) - 1] = 0;
  for (char* tok = strtok(buf, ","); tok && *n < MAX_PATHS;
       tok = strtok(NULL, ","))
    out[(*n)++] = strtoull(tok, NULL, 10);
}

static int load_config(const char* path) {
  FILE* f = fopen(path, "r");
  if (!f) {
    perror("config");
    return -1;
  }
  char line[512];
  while (fgets(line, sizeof(line), f)) {
    char* hash = strchr(line, '#');
    if (hash) *hash = 0;
    char key[64], a[256], b[64], c[64];
    a[0] = b[0] = c[0] = 0;
    int n = sscanf(line, "%63s %255s %63s %63s", key, a, b, c);
    if (n < 1) continue;
    if (!strcmp(key, "seed")) seed = strtoull(a, NULL, 10);
    else if (!strcmp(key, "rate_bps")) rate_bps = atof(a);
    else if (!strcmp(key, "buffer_pkts")) buffer_pkts = atoi(a);
    else if (!strcmp(key, "fwd_delay_us")) fwd_delay_us = strtoull(a, NULL, 10);
    else if (!strcmp(key, "rev_delay_us")) rev_delay_us = strtoull(a, NULL, 10);
    else if (!strcmp(key, "paths")) parse_list(a, path_us, &npaths);
    else if (!strcmp(key, "ack_paths")) parse_list(a, ackpath_us, &nackpaths);
    else if (!strcmp(key, "loss_random")) loss_random = atof(a);
    else if (!strcmp(key, "seq_loss") && nranges < MAX_RANGES) {
      ranges[nranges].path = -1;
      ranges[nranges].start = (uint32_t)strtoul(a, NULL, 10);
      ranges[nranges].end = (uint32_t)strtoul(b, NULL, 10);
      nranges++;
    } else if (!strcmp(key, "seq_path_down") && nranges < MAX_RANGES) {
      ranges[nranges].path = atoi(a);
      ranges[nranges].start = (uint32_t)strtoul(b, NULL, 10);
      ranges[nranges].end = (uint32_t)strtoul(c, NULL, 10);
      nranges++;
    }
    else if (!strcmp(key, "event") && nevents < MAX_EVENTS) {
      sched_event_t* e = &events[nevents++];
      memset(e, 0, sizeof(*e));
      e->t_us = strtoull(a, NULL, 10);
      if (!strcmp(b, "rate_bps")) e->kind = 0;
      else if (!strcmp(b, "fwd_delay_us")) e->kind = 1;
      else {
        fprintf(stderr, "unknown event %s\n", b);
        nevents--;
        continue;
      }
      e->v1 = atof(c);
    } else if (!strcmp(key, "flow") && nflows < MAX_FLOWS) {
      flow_t* fl = &flows[nflows++];
      memset(fl, 0, sizeof(*fl));
      fl->id = atoi(a);
      int lport = atoi(b), sport = atoi(c);
      fl->listen_fd = socket(AF_INET, SOCK_DGRAM, 0);
      fl->up_fd = socket(AF_INET, SOCK_DGRAM, 0);
      int big = 4 << 20;
      setsockopt(fl->listen_fd, SOL_SOCKET, SO_RCVBUF, &big, sizeof(big));
      setsockopt(fl->up_fd, SOL_SOCKET, SO_RCVBUF, &big, sizeof(big));
      struct sockaddr_in addr;
      memset(&addr, 0, sizeof(addr));
      addr.sin_family = AF_INET;
      addr.sin_addr.s_addr = inet_addr("127.0.0.1");
      addr.sin_port = htons((uint16_t)lport);
      if (bind(fl->listen_fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("bind listen port");
        fclose(f);
        return -1;
      }
      addr.sin_port = 0;
      bind(fl->up_fd, (struct sockaddr*)&addr, sizeof(addr));
      fl->server = addr;
      fl->server.sin_port = htons((uint16_t)sport);
    } else {
      fprintf(stderr, "cmunet: unknown directive '%s'\n", key);
    }
  }
  fclose(f);
  return 0;
}

/* ---- scheduling ---- */
static void inflight_insert(pkt_t* p) {
  pkt_t** pp = &inflight;
  while (*pp && (*pp)->due_us <= p->due_us) pp = &(*pp)->next;
  p->next = *pp;
  *pp = p;
}

static uint64_t tx_time_us(int len) {
  return (uint64_t)((double)len * 8.0 * 1e6 / rate_bps + 0.5);
}

static void start_tx(uint64_t start) {
  if (!q_head || tx_busy) return;
  tx_busy = 1;
  tx_done_us = start + tx_time_us(q_head->len);
}

static void fire_events(uint64_t now) {
  if (first_data_us < 0) return;
  for (int i = 0; i < nevents; i++) {
    sched_event_t* e = &events[i];
    if (e->fired || now < (uint64_t)first_data_us + e->t_us) continue;
    e->fired = 1;
    switch (e->kind) {
      case 0: rate_bps = e->v1; break;
      case 1: fwd_delay_us = (uint64_t)e->v1; break;
    }
  }
}

static void forward_arrival(int fi, uint8_t* d, int len, uint64_t now) {
  flow_t* fl = &flows[fi];
  hdr_t h = parse(d, len);
  if (h.valid) {
    if (h.flags & SYN) {
      fl->saw_syn = 1;
      fl->isn = h.seq;
    }
    else if ((h.flags & ACK) && fl->saw_synack && !fl->established) {
      fl->established = 1;
      printf("ESTABLISHED %d\n", fl->id);
      fflush(stdout);
    }
  }
  if (is_data(&h) && first_data_us < 0) {
    first_data_us = (int64_t)now;
    printf("FIRSTDATA %llu\n", (unsigned long long)now);
    fflush(stdout);
    fire_events(now);
  }
  uint64_t pid = next_pid++;
  uint32_t copy = is_data(&h) ? copy_index(fl->id, h.seq) : 0;
  log_pkt(now, fl->id, 0, 'A', d, len, q_len, pid);
  if (is_data(&h)) {
    uint32_t offset = h.seq - fl->isn - 1;
    for (int r = 0; r < nranges; r++) {
      if (ranges[r].path == -1 && copy == 0 && offset >= ranges[r].start &&
          offset < ranges[r].end) {
        log_pkt(now, fl->id, 0, 'D', d, len, 'b', pid);
        return;
      }
    }
    if (loss_random > 0 &&
        hash_unit(hash3(2, ((uint64_t)fl->id << 32) | offset, copy)) < loss_random) {
      log_pkt(now, fl->id, 0, 'D', d, len, 'l', pid);
      return;
    }
  }
  if (q_len >= buffer_pkts) {
    log_pkt(now, fl->id, 0, 'D', d, len, 'q', pid);
    return;
  }
  pkt_t* p = (pkt_t*)malloc(sizeof(pkt_t));
  p->pid = pid;
  p->copy = copy;
  p->flow = fi;
  p->dir = 0;
  p->len = (uint16_t)len;
  p->enq_us = now;
  p->next = NULL;
  memcpy(p->data, d, len);
  if (q_tail) q_tail->next = p;
  else q_head = p;
  q_tail = p;
  q_len++;
  start_tx(now);
}

static void reverse_arrival(int fi, uint8_t* d, int len, uint64_t now) {
  flow_t* fl = &flows[fi];
  hdr_t h = parse(d, len);
  if (h.valid && (h.flags & SYN) && (h.flags & ACK)) fl->saw_synack = 1;
  uint64_t pid = next_pid++;
  log_pkt(now, fl->id, 1, 'R', d, len, 0, pid);
  pkt_t* p = (pkt_t*)malloc(sizeof(pkt_t));
  p->pid = pid;
  p->flow = fi;
  p->dir = 1;
  p->len = (uint16_t)len;
  p->enq_us = now;
  uint64_t extra = 0;
  if (nackpaths > 0)
    extra = ackpath_us[hash3(3, (uint64_t)fl->id, rev_count[fi]++) % (uint64_t)nackpaths];
  p->due_us = now + rev_delay_us + extra;
  memcpy(p->data, d, len);
  inflight_insert(p);
}

static void tx_complete(uint64_t when) {
  pkt_t* p = q_head;
  q_head = p->next;
  if (!q_head) q_tail = NULL;
  q_len--;
  tx_busy = 0;
  flow_t* fl = &flows[p->flow];
  log_pkt(when, fl->id, 0, 'Q', p->data, p->len, (long long)(when - p->enq_us),
          p->pid);
  uint64_t extra = 0;
  if (npaths > 0) {
    hdr_t h = parse(p->data, p->len);
    uint64_t key = is_data(&h) ? (uint64_t)(h.seq - fl->isn - 1) : p->pid;
    int path = (int)((hash3(4, (uint64_t)fl->id, key) + p->copy) % (uint64_t)npaths);
    extra = path_us[path];
    int down = 0;
    uint32_t offset = h.seq - fl->isn - 1;
    for (int r = 0; r < nranges; r++)
      if (ranges[r].path == path && offset >= ranges[r].start && offset < ranges[r].end) down = 1;
    if (is_data(&h) && down) {
      log_pkt(when, fl->id, 0, 'D', p->data, p->len, 'p', p->pid);
      free(p);
      start_tx(when);
      return;
    }
    log_pkt(when, fl->id, 0, 'P', p->data, p->len, path, p->pid);
  }
  p->due_us = when + fwd_delay_us + extra;
  p->next = NULL;
  inflight_insert(p);
  start_tx(when);
}

static void deliver(pkt_t* p, uint64_t now) {
  flow_t* fl = &flows[p->flow];
  if (p->dir == 0) {
    sendto(fl->up_fd, p->data, p->len, 0, (struct sockaddr*)&fl->server,
           sizeof(fl->server));
    log_pkt(now, fl->id, 0, 'V', p->data, p->len, 0, p->pid);
  } else if (fl->have_client) {
    sendto(fl->listen_fd, p->data, p->len, 0, (struct sockaddr*)&fl->client,
           sizeof(fl->client));
  }
  free(p);
}

int main(int argc, char** argv) {
  if (argc != 3) {
    fprintf(stderr, "usage: %s <config> <log.csv>\n", argv[0]);
    return 1;
  }
  t0_abs = abs_now_us();
  if (load_config(argv[1]) < 0) return 1;
  logf = fopen(argv[2], "w");
  if (!logf) {
    perror("log");
    return 1;
  }
  fprintf(logf, "t_us,flow,dir,ev,seq,ack,plen,hlen,flags,win,info,pid\n");
  signal(SIGTERM, on_signal);
  signal(SIGINT, on_signal);
  printf("T0 %llu\nREADY\n", (unsigned long long)t0_abs);
  fflush(stdout);

  uint8_t buf[MAX_PKT];
  while (!stop) {
    uint64_t now = now_us();
    /* Run everything that is due. */
    int progressed = 1;
    while (progressed) {
      progressed = 0;
      now = now_us();
      fire_events(now);
      if (tx_busy && tx_done_us <= now) {
        tx_complete(tx_done_us);
        progressed = 1;
      }
      while (inflight && inflight->due_us <= now) {
        pkt_t* p = inflight;
        inflight = p->next;
        deliver(p, now);
        progressed = 1;
      }
    }
    /* Sleep until the next deadline or a packet arrives. */
    uint64_t next = now + 20000;
    if (tx_busy && tx_done_us < next) next = tx_done_us;
    if (inflight && inflight->due_us < next) next = inflight->due_us;
    uint64_t wait = next > now ? next - now : 0;
    fd_set rfds;
    FD_ZERO(&rfds);
    int maxfd = 0;
    for (int i = 0; i < nflows; i++) {
      FD_SET(flows[i].listen_fd, &rfds);
      FD_SET(flows[i].up_fd, &rfds);
      if (flows[i].listen_fd > maxfd) maxfd = flows[i].listen_fd;
      if (flows[i].up_fd > maxfd) maxfd = flows[i].up_fd;
    }
    struct timeval tv = {(time_t)(wait / 1000000), (suseconds_t)(wait % 1000000)};
    int r = select(maxfd + 1, &rfds, NULL, NULL, &tv);
    if (r < 0) {
      if (errno == EINTR) continue;
      perror("select");
      break;
    }
    if (r == 0) continue;
    for (int i = 0; i < nflows; i++) {
      flow_t* fl = &flows[i];
      if (FD_ISSET(fl->listen_fd, &rfds)) {
        struct sockaddr_in from;
        socklen_t fl_len = sizeof(from);
        ssize_t n;
        while ((n = recvfrom(fl->listen_fd, buf, sizeof(buf), MSG_DONTWAIT,
                             (struct sockaddr*)&from, &fl_len)) > 0) {
          fl->client = from;
          fl->have_client = 1;
          forward_arrival(i, buf, (int)n, now_us());
          fl_len = sizeof(from);
        }
      }
      if (FD_ISSET(fl->up_fd, &rfds)) {
        ssize_t n;
        while ((n = recvfrom(fl->up_fd, buf, sizeof(buf), MSG_DONTWAIT, NULL,
                             NULL)) > 0)
          reverse_arrival(i, buf, (int)n, now_us());
      }
    }
  }
  fflush(logf);
  fclose(logf);
  return 0;
}
