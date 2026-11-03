#!/usr/bin/env python3

import time

from fabric import Connection
from scapy.all import AsyncSniffer, Raw, send, sr1

from common import (
    CMUTCP,
    ACK_MASK,
    SYN_MASK,
    TIMEOUT,
    IFNAME,
    START_TESTING_SERVER_CMD,
    STOP_TESTING_SERVER_CMD,
    TESTING_HOST_IP,
    TESTING_HOST_PORT,
    ip,
    udp,
)


# These are the known values for this local controlled test.
#
# TEST_WINDOW_SIZE must match the CP1 window size that your C code
# is currently compiled with for this test.
TEST_WINDOW_SIZE = 2000

# grading.h currently defines DEFAULT_TIMEOUT as 3000 ms.
DEFAULT_TIMEOUT_MS = 3000


def payload_len(pkt):
    return pkt[CMUTCP].plen - pkt[CMUTCP].hlen


def is_server_data_packet(pkt):
    """
    True only for data packets sent by testing_server.

    This avoids accidentally counting the one-byte trigger packet
    that this Python script sends to testing_server.
    """
    return (
        CMUTCP in pkt
        and pkt[CMUTCP].source_port == TESTING_HOST_PORT
        and payload_len(pkt) > 0
    )


def unique_by_seq(packets):
    """
    Return one packet for each distinct sequence number.
    """
    result = {}

    for pkt in packets:
        seq = pkt[CMUTCP].seq_num

        if seq not in result:
            result[seq] = pkt

    return result


def start_connection(conn):
    """
    Start testing_server and complete the three-way handshake.

    Important:
    This function does NOT send application data.

    testing_server therefore remains blocked in cmu_read() until
    trigger_server_send() is called later.
    """

    # Clean up a stale session if one exists.
    conn.run(
        STOP_TESTING_SERVER_CMD,
        warn=True,
        hide=True,
    )

    # Start testing_server.
    conn.run(START_TESTING_SERVER_CMD)
    conn.run("tmux has-session -t pytest_server")

    client_isn = 1000

    # Step 1:
    # client -> server
    # SYN
    syn_pkt = (
        ip
        / udp
        / CMUTCP(
            plen=25,
            seq_num=client_isn,
            flags=SYN_MASK,
        )
    )

    # Step 2:
    # server -> client
    # SYN + ACK
    syn_ack_pkt = sr1(
        syn_pkt,
        timeout=TIMEOUT,
        iface=IFNAME,
        verbose=False,
    )

    assert syn_ack_pkt is not None, (
        "Did not receive SYN-ACK"
    )

    assert CMUTCP in syn_ack_pkt, (
        "Response was not a CMU-TCP packet"
    )

    assert (
        syn_ack_pkt[CMUTCP].flags
        == (SYN_MASK | ACK_MASK)
    ), "Invalid SYN-ACK flags"

    assert (
        syn_ack_pkt[CMUTCP].ack_num
        == client_isn + 1
    ), "Invalid SYN-ACK acknowledgement number"

    server_isn = syn_ack_pkt[CMUTCP].seq_num

    # Step 3:
    # client -> server
    # plain ACK, NO payload
    final_ack = (
        ip
        / udp
        / CMUTCP(
            plen=25,
            seq_num=client_isn + 1,
            ack_num=server_isn + 1,
            flags=ACK_MASK,
        )
    )

    send(
        final_ack,
        iface=IFNAME,
        verbose=False,
    )

    return client_isn, server_isn


def trigger_server_send(client_isn, server_isn):
    """
    Send one byte to testing_server.

    testing_server is blocked in cmu_read(). Receiving this byte
    causes cmu_read() to return, after which testing_server begins
    sending random.input.
    """

    data_pkt = (
        ip
        / udp
        / CMUTCP(
            plen=26,
            seq_num=client_isn + 1,
            ack_num=server_isn + 1,
            flags=ACK_MASK,
        )
        / Raw(load=b"X")
    )

    send(
        data_pkt,
        iface=IFNAME,
        verbose=False,
    )


def capture_initial_window(client_isn, server_isn):
    """
    Start sniffing BEFORE testing_server is allowed to send.

    This avoids the race where testing_server transmits its entire
    first window before sniff() starts.
    """

    sniffer = AsyncSniffer(
        iface=IFNAME,
        lfilter=is_server_data_packet,
    )

    sniffer.start()

    # Give AsyncSniffer a moment to become active.
    time.sleep(0.05)

    # Now wake testing_server.
    trigger_server_send(
        client_isn,
        server_isn,
    )

    # This is intentionally much shorter than DEFAULT_TIMEOUT,
    # so we capture the initial transmission rather than timeout
    # retransmissions.
    time.sleep(0.25)

    packets = sniffer.stop()

    return packets


def test_multiple_packets_before_ack():
    print(
        "Running test_multiple_packets_before_ack()"
    )

    with Connection(
        host=TESTING_HOST_IP,
        user="vagrant",
        connect_kwargs={"password": "vagrant"},
    ) as conn:

        try:
            client_isn, server_isn = start_connection(
                conn
            )

            packets = capture_initial_window(
                client_isn,
                server_isn,
            )

            unique = unique_by_seq(packets)

            print(
                "Unique data packets before ACK:",
                len(unique),
            )

            for seq, pkt in sorted(unique.items()):
                print(
                    "seq =",
                    seq,
                    "payload =",
                    payload_len(pkt),
                )

            assert len(unique) >= 2, (
                "Windowing failed: sender sent fewer "
                "than two different data packets "
                "before receiving an ACK"
            )

            print("Test Passed")

        finally:
            conn.run(
                STOP_TESTING_SERVER_CMD,
                warn=True,
                hide=True,
            )


def test_window_does_not_exceed_cp1_size():
    print(
        "Running "
        "test_window_does_not_exceed_cp1_size()"
    )

    print(
        "TEST_WINDOW_SIZE =",
        TEST_WINDOW_SIZE,
    )

    with Connection(
        host=TESTING_HOST_IP,
        user="vagrant",
        connect_kwargs={"password": "vagrant"},
    ) as conn:

        try:
            client_isn, server_isn = start_connection(
                conn
            )

            packets = capture_initial_window(
                client_isn,
                server_isn,
            )

            unique = unique_by_seq(packets)

            total_unacked_bytes = sum(
                payload_len(pkt)
                for pkt in unique.values()
            )

            print(
                "Unique packets:",
                len(unique),
            )

            for seq, pkt in sorted(unique.items()):
                print(
                    "seq =",
                    seq,
                    "payload =",
                    payload_len(pkt),
                )

            print(
                "Bytes sent before receiving ACK:",
                total_unacked_bytes,
            )

            assert len(unique) >= 2, (
                "Sender did not transmit multiple "
                "packets before an ACK"
            )

            assert (
                total_unacked_bytes
                <= TEST_WINDOW_SIZE
            ), (
                f"Sender exceeded window: "
                f"{total_unacked_bytes} bytes sent "
                f"with no ACK, but window is "
                f"{TEST_WINDOW_SIZE}"
            )

            print("Test Passed")

        finally:
            conn.run(
                STOP_TESTING_SERVER_CMD,
                warn=True,
                hide=True,
            )


def test_window_slides_after_ack():
    print(
        "Running test_window_slides_after_ack()"
    )

    with Connection(
        host=TESTING_HOST_IP,
        user="vagrant",
        connect_kwargs={"password": "vagrant"},
    ) as conn:

        try:
            client_isn, server_isn = start_connection(
                conn
            )

            first_packets = capture_initial_window(
                client_isn,
                server_isn,
            )

            first_unique = unique_by_seq(
                first_packets
            )

            assert len(first_unique) >= 2, (
                "Need at least two packets in the "
                "initial window to test sliding"
            )

            ordered = sorted(
                first_unique.values(),
                key=lambda pkt:
                    pkt[CMUTCP].seq_num,
            )

            print("Initial window:")

            for pkt in ordered:
                print(
                    "seq =",
                    pkt[CMUTCP].seq_num,
                    "payload =",
                    payload_len(pkt),
                )

            first_pkt = ordered[0]

            first_seq = (
                first_pkt[CMUTCP].seq_num
            )

            first_len = payload_len(
                first_pkt
            )

            # ACK exactly the first packet.
            ack_number = (
                first_seq + first_len
            )

            ack_pkt = (
                ip
                / udp
                / CMUTCP(
                    plen=25,
                    seq_num=client_isn + 2,
                    ack_num=ack_number,
                    flags=ACK_MASK,
                )
            )

            print(
                "Sending ACK =",
                ack_number,
            )

            # Begin listening BEFORE sending the ACK,
            # because the sender may immediately transmit
            # another packet when its window moves.
            sniffer = AsyncSniffer(
                iface=IFNAME,
                lfilter=is_server_data_packet,
            )

            sniffer.start()
            time.sleep(0.05)

            send(
                ack_pkt,
                iface=IFNAME,
                verbose=False,
            )

            time.sleep(0.25)

            second_packets = sniffer.stop()

            second_unique = unique_by_seq(
                second_packets
            )

            old_sequences = set(
                first_unique.keys()
            )

            new_sequences = [
                seq
                for seq
                in second_unique.keys()
                if seq not in old_sequences
            ]

            print(
                "New sequence numbers after ACK:",
                sorted(new_sequences),
            )

            assert len(new_sequences) > 0, (
                "Window did not slide: sender "
                "received an ACK advancing the "
                "window but sent no new packet"
            )

            print("Test Passed")

        finally:
            conn.run(
                STOP_TESTING_SERVER_CMD,
                warn=True,
                hide=True,
            )


def test_go_back_n_retransmission():
    print(
        "Running test_go_back_n_retransmission()"
    )

    with Connection(
        host=TESTING_HOST_IP,
        user="vagrant",
        connect_kwargs={"password": "vagrant"},
    ) as conn:

        try:
            client_isn, server_isn = start_connection(
                conn
            )

            # Start capturing BEFORE waking the sender.
            sniffer = AsyncSniffer(
                iface=IFNAME,
                lfilter=is_server_data_packet,
            )

            sniffer.start()
            time.sleep(0.05)

            # Wake testing_server.
            trigger_server_send(
                client_isn,
                server_isn,
            )

            # Send absolutely NO ACKs.
            #
            # Wait until DEFAULT_TIMEOUT has expired,
            # plus a little extra time for retransmission.
            time.sleep(
                (DEFAULT_TIMEOUT_MS / 1000.0)
                + 0.5
            )

            packets = sniffer.stop()

            print(
                "Total captured data packets:",
                len(packets),
            )

            seq_counts = {}

            for pkt in packets:
                seq = pkt[CMUTCP].seq_num

                if seq not in seq_counts:
                    seq_counts[seq] = 0

                seq_counts[seq] += 1

            print(
                "Captured sequence numbers:"
            )

            for seq in sorted(
                seq_counts.keys()
            ):
                print(
                    "seq =",
                    seq,
                    "count =",
                    seq_counts[seq],
                )

            # There should have been multiple outstanding
            # packets in the original window.
            assert len(seq_counts) >= 2, (
                "Expected multiple packets in "
                "the initial sending window"
            )

            # Every outstanding sequence number should
            # appear at least twice:
            #
            # count 1 = original transmission
            # count 2 = timeout retransmission
            not_retransmitted = [
                seq
                for seq, count
                in seq_counts.items()
                if count < 2
            ]

            assert not not_retransmitted, (
                "Go-Back-N failed. These packets "
                "were not retransmitted after "
                f"timeout: {not_retransmitted}"
            )

            print("Test Passed")

        finally:
            conn.run(
                STOP_TESTING_SERVER_CMD,
                warn=True,
                hide=True,
            )


if __name__ == "__main__":
    test_multiple_packets_before_ack()
    test_window_does_not_exceed_cp1_size()
    test_window_slides_after_ack()
    test_go_back_n_retransmission()