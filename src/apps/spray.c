/**
 * Copyright (C) 2026 Carnegie Mellon University
 *
 * This file is part of the TCP in the Wild course project developed for the
 * Computer Networks course (15-441/641) taught at Carnegie Mellon University.
 *
 * spray: the Packet Spraying challenge's bulk transfer between two GPU
 * servers. The switch sprays its packets over several paths.
 *
 *   spray send <port> <bytes> <seed>
 *   spray recv <port> <bytes> <seed> <result.json>
 */

#include "common.h"

int main(int argc, char** argv) { return bulk_main("spray", argc, argv); }
