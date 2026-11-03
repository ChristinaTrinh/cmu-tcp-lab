#!/usr/bin/env python3

import time

from fabric import Connection
from scapy.all import Raw, send, sr1, AsyncSniffer

from common import (
    CMUTCP,
    ACK_MASK,
    SYN_MASK,
    TIMEOUT,
    IFNAME,
    START_TESTING_SERVER_CMD,
    STOP_TESTING_SERVER_CMD,
    TESTING_HOST_IP,
    ip,
    udp,
)


def payload_len(pkt):
    return pkt[CMUTCP].plen - pkt[CMUTCP].hlen


def is_data_packet(pkt):
    return (
        CMUTCP in pkt
        and payload_len(pkt) > 0
    )

def trigger_server_send(client_isn, server_isn):
    data_pkt = (
        ip
        / udp
        / CMUTCP(
            plen=26,
            seq_num=client_isn + 1,
            ack_num=server_isn + 1,
            flags=ACK_MASK,
            advertised_window=65535,
        )
        / Raw(load=b"X")
    )

    send(
        data_pkt,
        iface=IFNAME,
        verbose=False,
    )

def start_connection(conn):
    """
    Start testing_server and complete the CMU-TCP handshake.

    Returns:
        client_isn
        server_isn
    """

    conn.run(
        STOP_TESTING_SERVER_CMD,
        warn=True,
        hide=True,
    )

    conn.run(START_TESTING_SERVER_CMD)
    conn.run("tmux has-session -t pytest_server")

    client_isn = 1000

    # -------------------------
    # 1. Send SYN
    # -------------------------

    syn_pkt = (
        ip
        / udp
        / CMUTCP(
            plen=25,
            seq_num=client_isn,
            flags=SYN_MASK,
            advertised_window=65535,
        )
    )

    syn_ack_pkt = sr1(
        syn_pkt,
        timeout=TIMEOUT,
        iface=IFNAME,
        verbose=False,
    )

    assert syn_ack_pkt is not None, (
        "Did not receive SYN-ACK"
    )

    assert CMUTCP in syn_ack_pkt

    assert (
        syn_ack_pkt[CMUTCP].flags
        == (SYN_MASK | ACK_MASK)
    )

    assert (
        syn_ack_pkt[CMUTCP].ack_num
        == client_isn + 1
    )

    server_isn = syn_ack_pkt[CMUTCP].seq_num

    # -------------------------
    # 2. Final ACK + 1 byte
    # -------------------------
    #
    # The 1-byte payload causes testing_server's
    # cmu_read() to return so it starts sending data.

    final_ack = (
        ip
        / udp
        / CMUTCP(
            plen=25,
            seq_num=client_isn + 1,
            ack_num=server_isn + 1,
            flags=ACK_MASK,
            advertised_window=65535,
        )
    )

    send(
        final_ack,
        iface=IFNAME,
        verbose=False,
    )

    return client_isn, server_isn


def test_fast_retransmit_three_duplicate_acks():
    print(
        "Running test_fast_retransmit_three_duplicate_acks()"
    )

    with Connection(
        host=TESTING_HOST_IP,
        user="vagrant",
        connect_kwargs={"password": "vagrant"},
    ) as conn:

        try:
            client_isn, server_isn = start_connection(conn)

            # Capture packets sent by testing_server.
            sniffer = AsyncSniffer(
                iface=IFNAME,
                lfilter=is_data_packet,
            )

            sniffer.start()

            time.sleep(0.05)
            trigger_server_send(client_isn, server_isn)
            
            result = conn.run(
				"tmux has-session -t pytest_server",
				warn=True,
				hide=False,
			)

            print("testing_server still running:", result.ok)


            # Give server some time to send its initial data.
            time.sleep(1.0)

            initial_packets = sniffer.stop()

            assert len(initial_packets) > 0, (
                "Server did not send any data packets"
            )

            # -----------------------------------
            # Find the first data packet
            # -----------------------------------

            data_packets = sorted(
                initial_packets,
                key=lambda pkt: pkt[CMUTCP].seq_num,
            )

            first_pkt = data_packets[0]

            first_seq = first_pkt[CMUTCP].seq_num

            print("First data packet seq =", first_seq)

            # We pretend this packet was lost.
            #
            # Therefore the receiver keeps ACKing:
            #
            #     ACK = first_seq
            #
            # This means:
            #
            #     "I am still waiting for first_seq"

            duplicate_ack = (
                ip
                / udp
                / CMUTCP(
                    plen=25,
                    seq_num=client_isn + 2,
                    ack_num=first_seq,
                    flags=ACK_MASK,
                    advertised_window=65535,
                )
            )

            # -----------------------------------
            # Start another sniffer so we can see
            # what happens after duplicate ACKs.
            # -----------------------------------

            retransmit_sniffer = AsyncSniffer(
                iface=IFNAME,
                lfilter=is_data_packet,
            )

            retransmit_sniffer.start()

            time.sleep(0.05)

            # -----------------------------------
            # Send three duplicate ACKs
            # -----------------------------------

            for i in range(3):
                print(
                    "Sending duplicate ACK",
                    i + 1,
                    "ACK =",
                    first_seq,
                )

                send(
                    duplicate_ack,
                    iface=IFNAME,
                    verbose=False,
                )

                time.sleep(0.05)

            # We do NOT want to wait for the normal
            # 3-second retransmission timeout.
            #
            # Fast retransmit should occur quickly.

            time.sleep(0.5)

            retransmitted_packets = (
                retransmit_sniffer.stop()
            )

            # -----------------------------------
            # Look for first_seq again
            # -----------------------------------

            found_retransmission = False

            for pkt in retransmitted_packets:
                seq = pkt[CMUTCP].seq_num

                print(
                    "Saw packet after dup ACKs: seq =",
                    seq,
                )

                if seq == first_seq:
                    found_retransmission = True

            assert found_retransmission, (
                "Fast retransmit failed: "
                f"packet seq={first_seq} was not "
                "retransmitted after 3 duplicate ACKs"
            )

            print("Test Passed")

        finally:
            conn.run(
                STOP_TESTING_SERVER_CMD,
                warn=True,
                hide=True,
            )


if __name__ == "__main__":
    test_fast_retransmit_three_duplicate_acks()