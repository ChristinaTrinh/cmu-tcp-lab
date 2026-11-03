/**
 * Copyright (C) 2022 Carnegie Mellon University
 *
 * This file is part of the TCP in the Wild course project developed for the
 * Computer Networks course (15-441/641) taught at Carnegie Mellon University.
 *
 * No part of the project may be copied and/or distributed without the express
 * permission of the 15-441/641 course staff.
 *
 *
 * This file implements the CMU-TCP backend. The backend runs in a different
 * thread and handles all the socket operations separately from the application.
 *
 * This is where most of your code should go. Feel free to modify any function
 * in this file.
 */

#include "backend.h"

#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <time.h>

#include "cmu_packet.h"
#include "cmu_tcp.h"

#define MIN(X, Y) (((X) < (Y)) ? (X) : (Y))
#define MAX(X, Y) (((X) > (Y)) ? (X) : (Y))
#define DATA_TIMEOUT 0
#define DATA_RECEIVED 1
#define DATA_IGNORED 2
#define DATA_ERROR -1

// how many milliseconds have passed since the input time
static long ms_since(const struct timespec *start)
{
	struct timespec now;
	clock_gettime(CLOCK_MONOTONIC, &now);
	return (now.tv_sec - start->tv_sec) * 1000 +
		   (now.tv_nsec - start->tv_nsec) / 1000000;
}

static uint64_t now_us(void)
{
	struct timespec now;
	clock_gettime(CLOCK_MONOTONIC, &now);

	return (uint64_t)now.tv_sec * 1000000ULL +
		   (uint64_t)now.tv_nsec / 1000ULL;
}

// Create & send a CMU-TCP packet that contains no application data
// (eg. SYN, SYN + ACK, ACK)
static int send_ctrl_packet(cmu_socket_t *sock, uint32_t seq, uint32_t ack,
							uint8_t flags)
{
	uint16_t hlen = sizeof(cmu_tcp_header_t);
	uint16_t adv_window = MAX_NETWORK_BUFFER - sock->buffered_bytes;
	sock->last_adv_sent = adv_window;
	uint8_t *pkt =
		create_packet(sock->my_port, ntohs(sock->conn.sin_port), seq, ack, hlen,
					  hlen, flags, adv_window, 0, NULL, NULL, 0);
	if (pkt == NULL)
	{
		fprintf(stderr, "send_ctrl_packet: create_packet failed\n");
		return EXIT_ERROR;
	}
	ssize_t n = sendto(sock->socket, pkt, hlen, 0, (struct sockaddr *)&sock->conn,
					   sizeof(sock->conn));
	free(pkt);
	if (n != (ssize_t)hlen)
	{
		perror("send_ctrl_packet: sendto");
		return EXIT_ERROR;
	}
	clock_gettime(CLOCK_MONOTONIC, &sock->last_send_time);
	return EXIT_SUCCESS;
}

static uint8_t *create_extended_packet(
	uint16_t src,
	uint16_t dst,
	uint32_t seq,
	uint32_t ack,
	uint8_t flags,
	uint16_t adv_window,
	uint8_t *ext_data,
	uint16_t ext_len,
	uint8_t *payload,
	uint16_t payload_len)
{
	uint16_t hlen = sizeof(cmu_tcp_header_t) + ext_len;
	uint16_t plen = hlen + payload_len;

	uint8_t *packet = malloc(plen);
	if (packet == NULL)
	{
		return NULL;
	}

	cmu_tcp_header_t *header =
		(cmu_tcp_header_t *)packet;

	set_header(header,
			   src,
			   dst,
			   seq,
			   ack,
			   hlen,
			   plen,
			   flags,
			   adv_window,
			   ext_len,
			   ext_data);

	if (payload_len > 0)
	{
		memcpy(packet + hlen,
			   payload,
			   payload_len);
	}

	return packet;
}

void retransmit_segment(cmu_socket_t *sock)
{
	unacked_node_t *head = sock->unacked_packet_queue.head;

	if (head == NULL)
	{
		return;
	}

	sendto(sock->socket, head->pkt.msg, head->pkt.plen, 0,
		   (struct sockaddr *)&sock->conn, sizeof(sock->conn));

	clock_gettime(CLOCK_MONOTONIC, &sock->retransmit_time);
}

// Resend lost data during loss recovery, up to cwnd bytes counted from the
// first unacked byte. The receiver discards out-of-order segments, so every
// segment sent before `recover` has to be resent, not only the head. Resending
// only the head costs one full timeout per lost segment.
static void retransmit_window(cmu_socket_t *sock)
{
	uint32_t limit = MAX(sock->cwnd, (uint32_t)MSS);

	for (unacked_node_t *node = sock->unacked_packet_queue.head; node != NULL;
		 node = node->next)
	{
		if (!before(node->pkt.seq, sock->recover))
		{
			break;
		}
		if (before(node->pkt.seq, sock->rexmit_next))
		{
			continue; // already resent in this recovery
		}
		uint32_t end = node->pkt.seq + node->pkt.payload_len;
		if (end - sock->window.last_ack_received > limit)
		{
			break;
		}
		sendto(sock->socket, node->pkt.msg, node->pkt.plen, 0,
			   (struct sockaddr *)&sock->conn, sizeof(sock->conn));
		sock->rexmit_next = end;
		clock_gettime(CLOCK_MONOTONIC, &sock->retransmit_time);
	}
}

static void enter_recovery(cmu_socket_t *sock)
{
	sock->in_recovery = 1;
	sock->recover = sock->next_seq_to_send;
	sock->rexmit_next = sock->window.last_ack_received;
	retransmit_window(sock);
}

static uint32_t halved_ssthresh(const cmu_socket_t *sock)
{
	return sock->cwnd / 2; // as specified by the handout's Reno diagram
}

/**
 * Tells if a given sequence number has been acknowledged by the socket.
 *
 * @param sock The socket to check for acknowledgements.
 * @param seq Sequence number to check.
 *
 * @return 1 if the sequence number has been acknowledged, 0 otherwise.
 */
int has_been_acked(cmu_socket_t *sock, uint32_t seq)
{
	int result;
	result = after(sock->window.last_ack_received, seq);
	return result;
}

// TODO: add timeout and possible transmit of new segment
void tcp_reno(cmu_socket_t *sock, bool new_ACK)
{
	switch (sock->congest_state)
	{
	case (SLOW_START):
	{
		if (new_ACK)
		{
			sock->dupACKcount = 0;

			if (sock->base_rtt_us > 0 &&
				sock->latest_rtt_us >= sock->base_rtt_us &&
				sock->latest_rtt_us > 0)
			{
				uint32_t bytes_in_flight =
					sock->next_seq_to_send -
					sock->window.last_ack_received;

				uint64_t queued_bytes =
					((uint64_t)bytes_in_flight *
					 (sock->latest_rtt_us - sock->base_rtt_us)) /
					sock->latest_rtt_us;

				// if 2 packets are queued, stopped ss, switch to ca
				if (queued_bytes >= 0.5 * MSS)
				{
					sock->ssthresh = sock->cwnd;
					sock->congest_state = CONGEST_AVOID;
				}
				else
				{
					sock->cwnd += MSS;
				}
			}
			else
			{
				// No RTT measurement yet
				sock->cwnd += MSS;
			}

			if (sock->cwnd >= sock->ssthresh)
			{
				sock->congest_state = CONGEST_AVOID;
			}
		}
		else
		{
			sock->dupACKcount++;
		}
		// Dup ACKs that arrive while already recovering (e.g. after a timeout)
		// come from segments the receiver discarded. They must not start a new
		// loss event and halve ssthresh again.
		if (sock->dupACKcount == 3 && sock->unacked_packet_queue.count > 0 &&
			!sock->in_recovery)
		{
			sock->ssthresh = halved_ssthresh(sock);
			sock->cwnd = sock->ssthresh + 3 * MSS;
			sock->congest_state = FAST_RECOVERY;
			enter_recovery(sock);
		}
		break;
	}
	case (CONGEST_AVOID):
	{
		if (new_ACK)
		{
			sock->dupACKcount = 0;
			if (sock->base_rtt_us > 0 &&
				sock->latest_rtt_us >= sock->base_rtt_us &&
				sock->latest_rtt_us > 0)
			{
				uint32_t bytes_in_flight =
					sock->next_seq_to_send -
					sock->window.last_ack_received;

				uint64_t queued_bytes =
					((uint64_t)bytes_in_flight *
					 (sock->latest_rtt_us - sock->base_rtt_us)) /
					sock->latest_rtt_us;

				uint64_t queued_pkts = queued_bytes / MSS;

				if (queued_bytes < MSS / 2)
				{
					// queue is very small
					sock->cwnd += (MSS * MSS) / sock->cwnd;
				}
				else if (queued_bytes <= MSS)
				{
					// roughly around 1 queued packet
					// hold cwnd
				}
				else
				{
					uint32_t decrease = (MSS * MSS) / sock->cwnd;

					if (decrease == 0)
					{
						decrease = 1;
					}

					if (sock->cwnd > MSS + decrease)
					{
						sock->cwnd -= decrease;
					}
					else
					{
						sock->cwnd = MSS;
					}
				}
			}
			else
			{
				// No valid RTT sample yet:
				// fall back to normal Reno CA
				sock->cwnd += (MSS * MSS) / sock->cwnd;
			}
		}
		else
		{
			sock->dupACKcount++;
		}
		// Dup ACKs that arrive while already recovering (e.g. after a timeout)
		// come from segments the receiver discarded. They must not start a new
		// loss event and halve ssthresh again.
		if (sock->dupACKcount == 3 && sock->unacked_packet_queue.count > 0 &&
			!sock->in_recovery)
		{
			sock->ssthresh = halved_ssthresh(sock);
			sock->cwnd = sock->ssthresh + 3 * MSS;
			sock->congest_state = FAST_RECOVERY;
			enter_recovery(sock);
		}
		break;
	}
	case (FAST_RECOVERY):
	{
		if (new_ACK)
		{
			sock->cwnd = sock->ssthresh;
			sock->dupACKcount = 0;
			sock->congest_state = CONGEST_AVOID;
		}
		else
		{
			sock->cwnd += MSS;
		}
		break;
	}
	default:
	{
		break;
	}
	}
	log_cwnd(sock);
}

static int send_ack_with_timestamp(cmu_socket_t *sock,
								   uint32_t seq,
								   uint32_t ack,
								   uint64_t timestamp)
{
	uint16_t ext_len = sizeof(uint64_t);

	uint16_t hlen = sizeof(cmu_tcp_header_t) + ext_len;
	uint16_t plen = hlen;

	uint16_t adv_window =
		MAX_NETWORK_BUFFER - sock->buffered_bytes;

	sock->last_adv_sent = adv_window;

	uint8_t ext_data[sizeof(uint64_t)];
	memcpy(ext_data, &timestamp, sizeof(timestamp));

	uint8_t *pkt =
		create_extended_packet(sock->my_port, ntohs(sock->conn.sin_port),
							   seq, ack, ACK_FLAG_MASK, adv_window, ext_data, ext_len, NULL, 0);

	if (pkt == NULL)
	{
		fprintf(stderr,
				"send_ack_with_timestamp: create_packet failed\n");
		return EXIT_ERROR;
	}

	ssize_t n =
		sendto(sock->socket,
			   pkt,
			   plen,
			   0,
			   (struct sockaddr *)&sock->conn,
			   sizeof(sock->conn));

	free(pkt);

	if (n != (ssize_t)plen)
	{
		perror("send_ack_with_timestamp: sendto");
		return EXIT_ERROR;
	}

	clock_gettime(CLOCK_MONOTONIC, &sock->last_send_time);

	return EXIT_SUCCESS;
}

void handle_data(cmu_socket_t *sock, uint8_t *pkt)
{
	cmu_tcp_header_t *hdr = (cmu_tcp_header_t *)pkt;
	uint32_t seq = get_seq(hdr);
	uint16_t payload_len = get_payload_len(pkt);

	// Only accept this payload if it starts exactly where I expected the next
	// byte to start
	if (payload_len > 0 && seq == sock->window.next_seq_expected &&
		sock->buffered_bytes + payload_len <= MAX_NETWORK_BUFFER)
	{
		uint8_t *new_buf =
			realloc(sock->received_buf, sock->received_len + payload_len);
		if (new_buf != NULL)
		{
			sock->received_buf = new_buf;
			memcpy(sock->received_buf + sock->received_len, get_payload(pkt),
				   payload_len);
			sock->received_len += payload_len;
			sock->buffered_bytes += payload_len;
			sock->window.next_seq_expected = seq + payload_len;
		}
	}
	uint16_t ext_len = get_extension_length(hdr);

	if (ext_len == sizeof(uint64_t))
	{
		uint64_t sent_ts;
		memcpy(&sent_ts, get_extension_data(hdr), sizeof(sent_ts));

		send_ack_with_timestamp(sock, sock->window.last_ack_received,
								sock->window.next_seq_expected, sent_ts);
	}
	else
	{
		send_ctrl_packet(
			sock,
			sock->window.last_ack_received,
			sock->window.next_seq_expected,
			ACK_FLAG_MASK);
	}
}

static void remove_acked_packets(cmu_socket_t *sock, uint32_t ack)
{
	while (sock->unacked_packet_queue.head != NULL)
	{
		unacked_node_t *head = sock->unacked_packet_queue.head;
		uint32_t packet_end = head->pkt.seq + head->pkt.payload_len;
		if (after(packet_end, ack))
		{
			break;
		}
		sock->unacked_packet_queue.head = head->next;
		free(head->pkt.msg);
		free(head);
		sock->unacked_packet_queue.count--;
	}
	if (sock->unacked_packet_queue.head == NULL)
	{
		sock->unacked_packet_queue.tail = NULL;
	}
}

/**
 * Updates the socket information to represent the newly received packet.
 *
 * In the current stop-and-wait implementation, this function also sends an
 * acknowledgement for the packet.
 *
 * @param sock The socket used for handling packets received.
 * @param pkt The packet data received by the socket.
 */
void handle_message(cmu_socket_t *sock, uint8_t *pkt)
{
	cmu_tcp_header_t *hdr = (cmu_tcp_header_t *)pkt;
	uint8_t flags = get_flags(hdr);
	uint32_t seq = get_seq(hdr);
	uint32_t ack = get_ack(hdr);
	uint16_t payload_len = get_payload_len(pkt);
	uint16_t prev_adv_window = sock->adv_window;
	sock->adv_window = get_advertised_window(hdr);

	switch (flags)
	{
	case (SYN_FLAG_MASK):
	{
		// server receives SYN and send SYN+ACK back
		if (sock->handshake_state == CONN_LISTEN)
		{
			sock->peer_isn = seq;
			sock->window.next_seq_expected = seq + 1;
			if (send_ctrl_packet(sock, sock->window.last_ack_received,
								 sock->window.next_seq_expected,
								 SYN_FLAG_MASK | ACK_FLAG_MASK) == EXIT_SUCCESS)
			{
				sock->handshake_state = CONN_SYN_RCVD;
			}
		}
		// server receives dup SYN(in case the prev
		// SYN+ACK was lost, client will resend SYN+ACK)
		else if (sock->handshake_state == CONN_SYN_RCVD)
		{
			if (seq == sock->peer_isn)
			{
				send_ctrl_packet(sock, sock->my_isn, sock->peer_isn + 1,
								 SYN_FLAG_MASK | ACK_FLAG_MASK);
			}
		}
		break;
	}
	case (SYN_FLAG_MASK | ACK_FLAG_MASK):
	{
		// client receives SYN+ACK
		if (sock->handshake_state == CONN_SYN_SENT)
		{
			if (ack == sock->my_isn + 1)
			{
				sock->peer_isn = seq;
				sock->window.next_seq_expected = seq + 1;
				if (send_ctrl_packet(sock, sock->my_isn + 1, seq + 1,
									 ACK_FLAG_MASK) == EXIT_SUCCESS)
				{
					sock->window.last_ack_received = sock->my_isn + 1;
					sock->handshake_state = CONN_ESTABLISHED;
				}
			}
		}
		// when client's ACK is lost, server
		// resend SYN+ACK, so it resend ACK
		else if (sock->handshake_state == CONN_ESTABLISHED)
		{
			if (seq == sock->peer_isn && ack == sock->my_isn + 1)
			{
				send_ctrl_packet(sock, sock->my_isn + 1, sock->peer_isn + 1,
								 ACK_FLAG_MASK);
			}
		}
		break;
	}
	case ACK_FLAG_MASK:
	{
		uint16_t ext_len = get_extension_length(hdr);

		if (ext_len == sizeof(uint64_t))
		{
			uint64_t sent_ts;

			memcpy(&sent_ts,
				   get_extension_data(hdr),
				   sizeof(sent_ts));

			uint64_t sample_rtt_us = now_us() - sent_ts;

			if (sock->base_rtt_us == 0)
			{
				sock->base_rtt_us = sample_rtt_us;
				sock->high_rtt_count = 0;
			}
			else if (sample_rtt_us < sock->base_rtt_us)
			{
				// Found a better empty-path RTT
				sock->base_rtt_us = sample_rtt_us;
				sock->high_rtt_count = 0;
			}
			else
			{
				bool rtt_much_higher =
					sample_rtt_us > sock->base_rtt_us * 3 / 2;

				bool ack_rate_similar =
					sock->base_ack_gap_us > 0 &&
					sock->latest_ack_gap_us <
						sock->base_ack_gap_us * 3 / 2;

				if (rtt_much_higher && ack_rate_similar)
				{
					sock->high_rtt_count++;
				}
				else
				{
					sock->high_rtt_count = 0;
				}

				if (sock->high_rtt_count >= 5)
				{
					sock->base_rtt_us = sample_rtt_us;
					sock->high_rtt_count = 0;
				}
			}

			sock->latest_rtt_us = sample_rtt_us;
		}
		// server receives ACK from client
		if (sock->handshake_state == CONN_SYN_RCVD)
		{
			if (ack == sock->my_isn + 1)
			{
				sock->window.last_ack_received = ack;
				sock->handshake_state = CONN_ESTABLISHED;
				if (payload_len > 0)
				{
					handle_data(sock, pkt);
				}
			}
		}
		else if (sock->handshake_state == CONN_ESTABLISHED ||
				 sock->handshake_state == CONN_HALF_TERM ||
				 sock->handshake_state == CONN_FIN_WAIT)
		{
			if (after(ack, sock->window.last_ack_received))
			{
				// new ACK
				sock->window.last_ack_received = ack;
				remove_acked_packets(sock, ack);
				if (sock->unacked_packet_queue.head != NULL)
				{
					clock_gettime(CLOCK_MONOTONIC, &sock->retransmit_time);
				}
				// Measure spacing between NEW ACKs
				uint64_t now = now_us();

				if (sock->last_ack_time_us != 0)
				{
					uint64_t gap =
						now - sock->last_ack_time_us;

					sock->latest_ack_gap_us = gap;

					if (sock->base_ack_gap_us == 0 ||
						gap < sock->base_ack_gap_us)
					{
						sock->base_ack_gap_us = gap;
					}
				}

				sock->last_ack_time_us = now;
				tcp_reno(sock, true);
				if (sock->in_recovery)
				{
					if (!before(ack, sock->recover))
					{
						sock->in_recovery = 0; // everything lost has been acked
					}
					else
					{
						// partial ACK: the next segment was lost too, resend it now
						if (before(sock->rexmit_next, ack))
						{
							sock->rexmit_next = ack;
						}
						retransmit_window(sock);
					}
				}
			}
			else if (ack == sock->window.last_ack_received &&
					 sock->adv_window == prev_adv_window)
			{
				// Duplicate ACK (an ACK that only changes the window is an update)
				tcp_reno(sock, false);
				if (sock->in_recovery)
				{
					retransmit_window(sock);
				}
			}
			// only CONN ESTABLISHED should normally accept data
			if ((sock->handshake_state == CONN_ESTABLISHED ||
				 sock->handshake_state == CONN_FIN_WAIT) &&
				payload_len > 0)
			{
				handle_data(sock, pkt);
			}
		}
		else if (sock->handshake_state == CONN_CLIENT_FIN_SENT)
		{
			// when client receive an ack from server for its FIN
			// it will then change to the state waiting for server's FIN
			if (ack == sock->fin_seq + 1)
			{
				sock->handshake_state = CONN_FIN_WAIT;
				sock->window.last_ack_received = ack;
				if (payload_len > 0)
				{
					handle_data(sock, pkt);
				}
			}
		}
		else if (sock->handshake_state == CONN_SERVER_FIN_SENT)
		{
			if (ack == sock->fin_seq + 1)
			{
				sock->handshake_state = CONN_TERM;
				sock->window.last_ack_received = ack;
			}
		}
		break;
	}
	case (FIN_FLAG_MASK | ACK_FLAG_MASK):
	{
		if (sock->handshake_state == CONN_ESTABLISHED)
		{
			if (after(ack, sock->window.last_ack_received))
			{
				sock->window.last_ack_received = ack;
			}

			if (seq == sock->window.next_seq_expected)
			{
				sock->window.next_seq_expected = seq + 1;
				send_ctrl_packet(sock, sock->window.last_ack_received,
								 sock->window.next_seq_expected, ACK_FLAG_MASK);
				sock->handshake_state = CONN_HALF_TERM;
			}
		}
		else if (sock->handshake_state == CONN_CLIENT_FIN_SENT ||
				 sock->handshake_state == CONN_FIN_WAIT)
		{
			if (ack == sock->fin_seq + 1 && seq == sock->window.next_seq_expected)
			{
				sock->window.last_ack_received = ack;
				sock->window.next_seq_expected = seq + 1;
				send_ctrl_packet(sock, sock->window.last_ack_received,
								 sock->window.next_seq_expected, ACK_FLAG_MASK);
				sock->handshake_state = CONN_TERM;
			}
		}
		else if (sock->handshake_state == CONN_HALF_TERM)
		{
			// for the case client resend fin when server's ack is lost
			if (seq + 1 == sock->window.next_seq_expected)
			{
				send_ctrl_packet(sock, sock->window.last_ack_received,
								 sock->window.next_seq_expected, ACK_FLAG_MASK);
			}
		}
		break;
	}
	default:
	{
		if (sock->handshake_state == CONN_ESTABLISHED &&
			!(flags & SYN_FLAG_MASK) && payload_len > 0)
		{
			handle_data(sock, pkt);
		}
		break;
	}
	}
}

static int is_valid_packet(uint8_t *pkt, ssize_t len)
{
	if (len < (ssize_t)sizeof(cmu_tcp_header_t))
		return EXIT_ERROR;

	cmu_tcp_header_t *hdr = (cmu_tcp_header_t *)pkt;
	if (ntohl(hdr->identifier) != IDENTIFIER)
		return EXIT_ERROR;

	uint16_t hlen = get_hlen(hdr);
	uint16_t plen = get_plen(hdr);
	if ((ssize_t)plen != len)
		return EXIT_ERROR;
	if (hlen < sizeof(cmu_tcp_header_t) || hlen > plen)
		return EXIT_ERROR;

	return EXIT_SUCCESS;
}

/**
 * Checks if the socket received any data.
 *
 * It first peeks at the header to figure out the length of the packet and then
 * reads the entire packet.
 *
 * @param sock The socket used for receiving data on the connection.
 * @param flags Flags that determine how the socket should wait for data. Check
 *             `cmu_read_mode_t` for more information.
 */
int check_for_data(cmu_socket_t *sock, cmu_read_mode_t flags)
{
	uint8_t pkt[MAX_LEN];
	struct sockaddr_in from;
	socklen_t from_len = sizeof(from);
	ssize_t len = -1;

	while (pthread_mutex_lock(&(sock->recv_lock)) != 0)
	{
	}
	switch (flags)
	{
	case NO_FLAG:
		len = recvfrom(sock->socket, pkt, sizeof(pkt), 0, (struct sockaddr *)&from,
					   &from_len);
		break;
	case TIMEOUT:
	{
		// Using `poll` here so that we can specify a timeout.
		struct pollfd ack_fd;
		ack_fd.fd = sock->socket;
		ack_fd.events = POLLIN;

		// Wake up when the oldest unacked segment's timer expires, not
		// DEFAULT_TIMEOUT after the last packet happened to arrive.
		int wait_ms = DEFAULT_TIMEOUT;
		if (sock->unacked_packet_queue.head != NULL)
		{
			long elapsed = ms_since(&sock->retransmit_time);
			wait_ms = elapsed >= DEFAULT_TIMEOUT ? 0 : DEFAULT_TIMEOUT - elapsed;
		}
		int poll_result = poll(&ack_fd, 1, wait_ms);
		if (poll_result == 0)
		{
			// actual timeout
			pthread_mutex_unlock(&(sock->recv_lock));
			return DATA_TIMEOUT;
		}
		if (poll_result < 0)
		{
			perror("poll");
			pthread_mutex_unlock(&(sock->recv_lock));
			return DATA_ERROR;
		}
		len = recvfrom(sock->socket, pkt, sizeof(pkt), MSG_DONTWAIT,
					   (struct sockaddr *)&from, &from_len);
		break;
	}
	// Fallthrough.
	case NO_WAIT:
		len = recvfrom(sock->socket, pkt, sizeof(pkt), MSG_DONTWAIT,
					   (struct sockaddr *)&from, &from_len);
		break;
	default:
		perror("ERROR unknown flag");
		pthread_mutex_unlock(&(sock->recv_lock));
		return DATA_ERROR;
	}

	if (len > 0 && is_valid_packet(pkt, len) == EXIT_SUCCESS)
	{
		// A listener learns who its peer is from the first valid packet.
		if (sock->handshake_state == CONN_LISTEN)
		{
			sock->conn = from;
		}
		handle_message(sock, pkt);
		pthread_mutex_unlock(&(sock->recv_lock));
		return DATA_RECEIVED;
	}
	if (len < 0)
	{
		pthread_mutex_unlock(&(sock->recv_lock));
		if (flags == NO_WAIT)
		{
			return DATA_IGNORED;
		}
		return DATA_ERROR;
	}
	pthread_mutex_unlock(&(sock->recv_lock));
	return DATA_IGNORED;
}

static void handle_timeout(cmu_socket_t *sock)
{
	if (sock->unacked_packet_queue.head == NULL)
	{
		return;
	}

	sock->ssthresh = halved_ssthresh(sock);
	sock->cwnd = WINDOW_INITIAL_WINDOW_SIZE;
	sock->dupACKcount = 0;
	sock->congest_state = SLOW_START;
	log_cwnd(sock);

	// Resend from the first unacked byte; the segments after it were
	// discarded by the receiver and are resent as ACKs open up cwnd.
	enter_recovery(sock);
}
// cmu_read frees buffer space without any packet arriving. If the peer last
// saw a window smaller than one segment it has stopped sending, so no ACK will
// ever reopen the window unless we send an explicit update.
static void maybe_send_window_update(cmu_socket_t *sock)
{
	while (pthread_mutex_lock(&(sock->recv_lock)) != 0)
	{
	}
	if (sock->handshake_state == CONN_ESTABLISHED ||
		sock->handshake_state == CONN_HALF_TERM ||
		sock->handshake_state == CONN_FIN_WAIT)
	{
		uint32_t cur = MAX_NETWORK_BUFFER - sock->buffered_bytes;
		if (sock->last_adv_sent < MSS && cur >= MSS)
		{
			send_ctrl_packet(sock, sock->window.last_ack_received,
							 sock->window.next_seq_expected, ACK_FLAG_MASK);
		}
	}
	pthread_mutex_unlock(&(sock->recv_lock));
}

// Send the next unsent byte once, outside the retransmission queue and without
// advancing next_seq_to_send. A peer with room accepts it and ACKs it; a peer
// with a closed window drops it and replies with its current window. Nothing
// is left outstanding, so the sender resumes from the ACK point when the
// window reopens.
static void send_zero_window_probe(cmu_socket_t *sock, uint8_t *next_byte)
{
	uint16_t hlen = sizeof(cmu_tcp_header_t);
	uint16_t adv_window = MAX_NETWORK_BUFFER - sock->buffered_bytes;
	uint8_t *msg = create_packet(
		sock->my_port, ntohs(sock->conn.sin_port), sock->next_seq_to_send,
		sock->window.next_seq_expected, hlen, hlen + 1, ACK_FLAG_MASK, adv_window,
		0, NULL, next_byte, 1);
	if (msg == NULL)
	{
		return;
	}
	sendto(sock->socket, msg, hlen + 1, 0, (struct sockaddr *)&sock->conn,
		   sizeof(sock->conn));
	free(msg);
}

// Move whatever the application has written since the last call onto the end
// of the unsent data. `*owned` is the buffer we allocated (never the caller's).
static void pull_app_data(cmu_socket_t *sock, uint8_t **owned, uint8_t **offset,
						  int *buf_len)
{
	if (*buf_len >= MAX_NETWORK_BUFFER)
	{
		return; // plenty queued already; avoid copying the backlog repeatedly
	}
	while (pthread_mutex_lock(&(sock->send_lock)) != 0)
	{
	}
	int add = sock->sending_len;
	if (add > 0)
	{
		uint8_t *merged = malloc(*buf_len + add);
		if (merged != NULL)
		{
			memcpy(merged, *offset, *buf_len);
			memcpy(merged + *buf_len, sock->sending_buf, add);
			free(*owned);
			*owned = merged;
			*offset = merged;
			*buf_len += add;
			free(sock->sending_buf);
			sock->sending_buf = NULL;
			sock->sending_len = 0;
		}
	}
	pthread_mutex_unlock(&(sock->send_lock));
}

/**
 * Breaks up the data into packets and sends a single packet at a time.
 *
 * You should most certainly update this function in your implementation.
 *
 * @param sock The socket to use for sending data.
 * @param data The data to be sent.
 * @param buf_len The length of the data being sent.
 */
void single_send(cmu_socket_t *sock, uint8_t *data, int buf_len)
{
	uint8_t *data_offset = data;
	// Buffer holding data pulled in from the application while sending. `data`
	// itself belongs to the caller.
	uint8_t *owned = NULL;
	size_t conn_len = sizeof(sock->conn);

	int sockfd = sock->socket;

	if (buf_len > 0)
	{
		while (1)
		{
			// Keep taking data the application wrote meanwhile. Otherwise the flight
			// drains to zero at the end of every batch and cwnd is never reached.
			pull_app_data(sock, &owned, &data_offset, &buf_len);
			// A zero-window probe the peer accepted is ACKed past next_seq_to_send.
			// That byte is delivered, so skip it in the unsent data.
			if (after(sock->window.last_ack_received, sock->next_seq_to_send))
			{
				uint32_t delta =
					sock->window.last_ack_received - sock->next_seq_to_send;
				if (delta > (uint32_t)buf_len)
				{
					delta = (uint32_t)buf_len;
				}
				data_offset += delta;
				buf_len -= delta;
				sock->next_seq_to_send += delta;
			}
			// stop when there is no unsent data and nothing unacknowledged
			if (buf_len <= 0 && sock->unacked_packet_queue.head == NULL)
			{
				break;
			}
			// how many bytes have sent but receiver has not acknowledged yet
			uint32_t bytes_in_flight =
				sock->next_seq_to_send - sock->window.last_ack_received;
			uint32_t send_window = MIN(sock->cwnd, sock->adv_window);
			while ((buf_len > 0) && bytes_in_flight < send_window)
			{
				uint16_t ext_len = sizeof(uint64_t);
				uint16_t data_mss = MSS - ext_len;

				uint16_t payload_len =
					MIN((uint32_t)buf_len, (uint32_t)data_mss);

				uint32_t window_remaining = send_window - bytes_in_flight;
				if (window_remaining < payload_len)
				{
					break;
				}

				uint16_t src = sock->my_port;
				uint16_t dst = ntohs(sock->conn.sin_port);
				uint32_t seq = sock->next_seq_to_send;
				uint32_t ack = sock->window.next_seq_expected;

				uint8_t flags = ACK_FLAG_MASK;
				uint16_t adv_window = MAX_NETWORK_BUFFER - sock->buffered_bytes;

				uint64_t send_time = now_us();
				uint8_t *ext_data = (uint8_t *)&send_time;

				uint16_t hlen =
					sizeof(cmu_tcp_header_t) + ext_len;

				uint16_t plen =
					hlen + payload_len;

				uint8_t *payload = data_offset;

				uint8_t *msg =
					create_extended_packet(src, dst, seq, ack, flags, adv_window,
										   ext_data, ext_len, payload, payload_len);

				if (msg == NULL)
				{
					perror("single_send: create_packet failed");
					free(owned);
					return;
				}

				bool was_empty = (sock->unacked_packet_queue.head == NULL);

				unacked_node_t *node = malloc(sizeof(unacked_node_t));
				node->pkt.msg = msg;
				node->pkt.plen = plen;
				node->pkt.seq = seq;
				node->pkt.payload_len = payload_len;
				node->next = NULL;

				if (sock->unacked_packet_queue.tail == NULL)
				{
					clock_gettime(CLOCK_MONOTONIC, &sock->retransmit_time);
					sock->unacked_packet_queue.head = node;
					sock->unacked_packet_queue.tail = node;
				}
				else
				{
					sock->unacked_packet_queue.tail->next = node;
					sock->unacked_packet_queue.tail = node;
				}
				sock->unacked_packet_queue.count++;

				if (was_empty)
				{
					clock_gettime(CLOCK_MONOTONIC, &sock->retransmit_time);
				}

				sendto(sockfd, msg, plen, 0, (struct sockaddr *)&(sock->conn), conn_len);

				buf_len -= payload_len;
				sock->next_seq_to_send += payload_len;
				bytes_in_flight += payload_len;
				data_offset += payload_len;
			}
			int result = check_for_data(sock, TIMEOUT);

			if (result == DATA_ERROR)
			{
				free(owned);
				return;
			}
			if (result == DATA_TIMEOUT && buf_len > 0 &&
				sock->unacked_packet_queue.head == NULL)
			{
				// Idle for a full timeout with data waiting and nothing in flight:
				// the peer's window is closed (or its update was lost).
				send_zero_window_probe(sock, data_offset);
			}
			if (sock->unacked_packet_queue.head != NULL &&
				ms_since(&sock->retransmit_time) >= DEFAULT_TIMEOUT)
			{
				handle_timeout(sock);
			}
		}
	}
	free(owned);
}

void handshake_step(cmu_socket_t *sock)
{
	switch (sock->handshake_state)
	{
	case CONN_CLOSED:
	{
		if (send_ctrl_packet(sock, sock->window.last_ack_received, 0,
							 SYN_FLAG_MASK) == EXIT_SUCCESS)
		{
			sock->handshake_state = CONN_SYN_SENT;
		}
		break;
	}
	case CONN_SYN_SENT:
	{
		if (ms_since(&sock->last_send_time) >= DEFAULT_TIMEOUT)
		{
			send_ctrl_packet(sock, sock->window.last_ack_received, 0,
							 SYN_FLAG_MASK);
		}
		break;
	}
	case CONN_SYN_RCVD:
	{
		if (ms_since(&sock->last_send_time) >= DEFAULT_TIMEOUT)
		{
			send_ctrl_packet(sock, sock->window.last_ack_received,
							 sock->window.next_seq_expected,
							 SYN_FLAG_MASK | ACK_FLAG_MASK);
		}
		break;
	}
	default:
		break;
	}
}

void *begin_backend(void *in)
{
	cmu_socket_t *sock = (cmu_socket_t *)in;
	int death, buf_len, send_signal;
	uint8_t *data;

	while (1)
	{
		while (pthread_mutex_lock(&(sock->death_lock)) != 0)
		{
		}
		death = sock->dying;
		pthread_mutex_unlock(&(sock->death_lock));
		if (sock->handshake_state == CONN_TERM)
		{
			break;
		}
		if (sock->handshake_state == CONN_CLOSED ||
			sock->handshake_state == CONN_LISTEN ||
			sock->handshake_state == CONN_SYN_SENT ||
			sock->handshake_state == CONN_SYN_RCVD)
		{
			if (death)
			{
				while (pthread_mutex_lock(&(sock->send_lock)) != 0)
				{
				}
				int pending = sock->sending_len;
				pthread_mutex_unlock(&(sock->send_lock));
				if (pending == 0)
					break;
			}
			handshake_step(sock);
			check_for_data(sock, TIMEOUT);
			continue;
		}

		while (pthread_mutex_lock(&(sock->send_lock)) != 0)
		{
		}
		buf_len = sock->sending_len;

		if (death && buf_len == 0)
		{
			if (sock->handshake_state == CONN_ESTABLISHED)
			{
				sock->fin_seq = sock->window.last_ack_received;
				send_ctrl_packet(sock, sock->fin_seq, sock->window.next_seq_expected,
								 FIN_FLAG_MASK | ACK_FLAG_MASK);
				sock->handshake_state = CONN_CLIENT_FIN_SENT;
			}
			else if (sock->handshake_state == CONN_HALF_TERM)
			{
				// if the server has no more data to send and server app called
				// cmu_closed and it already received FIN from client before bc it's in
				// CONN_HALF_TERM state then it sends FIN NOW
				sock->fin_seq = sock->window.last_ack_received;
				send_ctrl_packet(sock, sock->fin_seq, sock->window.next_seq_expected,
								 FIN_FLAG_MASK | ACK_FLAG_MASK);
				sock->handshake_state = CONN_SERVER_FIN_SENT;
			}
			if (death && sock->handshake_state == CONN_CLOSED)
			{
				pthread_mutex_unlock(&(sock->send_lock));
				break;
			}
		}
		if (sock->handshake_state == CONN_CLIENT_FIN_SENT &&
			ms_since(&sock->last_send_time) >= DEFAULT_TIMEOUT)
		{
			send_ctrl_packet(sock, sock->fin_seq, sock->window.next_seq_expected,
							 FIN_FLAG_MASK | ACK_FLAG_MASK);
		}
		else if (sock->handshake_state == CONN_SERVER_FIN_SENT &&
				 ms_since(&sock->last_send_time) >= DEFAULT_TIMEOUT)
		{
			send_ctrl_packet(sock, sock->fin_seq, sock->window.next_seq_expected,
							 FIN_FLAG_MASK | ACK_FLAG_MASK);
		}

		if (buf_len > 0)
		{
			data = malloc(buf_len);
			memcpy(data, sock->sending_buf, buf_len);
			sock->sending_len = 0;
			free(sock->sending_buf);
			sock->sending_buf = NULL;
			pthread_mutex_unlock(&(sock->send_lock));
			single_send(sock, data, buf_len);
			free(data);
		}
		else
		{
			pthread_mutex_unlock(&(sock->send_lock));
		}

		check_for_data(sock, NO_WAIT);
		maybe_send_window_update(sock);

		while (pthread_mutex_lock(&(sock->recv_lock)) != 0)
		{
		}

		send_signal = sock->received_len > 0;

		pthread_mutex_unlock(&(sock->recv_lock));

		if (send_signal)
		{
			pthread_cond_signal(&(sock->wait_cond));
		}
	}

	pthread_exit(NULL);
	return NULL;
}
