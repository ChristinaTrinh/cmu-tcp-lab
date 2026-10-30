/**
 * Copyright (C) 2026 Carnegie Mellon University
 *
 * This file is part of the TCP in the Wild course project developed for the
 * Computer Networks course (15-441/641) taught at Carnegie Mellon University.
 *
 * gdrive: the Bufferbloat challenge's cloud upload. It pushes a file through
 * your CMU-TCP as fast as your transport allows.
 *
 *   gdrive send <port> <bytes> <seed>
 *   gdrive recv <port> <bytes> <seed> <result.json>
 */

#include "common.h"

int main(int argc, char** argv) { return bulk_main("gdrive", argc, argv); }
