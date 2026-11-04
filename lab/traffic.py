#!/usr/bin/env python3
"""UDP multicast sender / receiver, launched inside Mininet hosts."""
import argparse
import socket
import struct
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['send', 'receive'])
    parser.add_argument('--interface', required=True, help='local host IPv4')
    parser.add_argument('--group', default='239.1.1.1')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--mbps', type=float, default=1)
    args = parser.parse_args()
    if args.seconds <= 0 or args.mbps <= 0 or not 1 <= args.port <= 65535:
        parser.error('seconds、mbps 必须为正，port 为 1–65535')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    deadline, count, byte_count = time.monotonic() + args.seconds, 0, 0
    if args.mode == 'send':
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(args.interface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 16)
        interval = 1200 * 8 / (args.mbps * 1e6)
        next_packet = time.monotonic()
        while time.monotonic() < deadline:
            payload = struct.pack('!Q', count) + bytes(1192)
            try:
                sock.sendto(payload, (args.group, args.port))
            except (BlockingIOError, InterruptedError):
                # A congested virtual interface must not stall the experiment
                # beyond its requested duration.
                time.sleep(.01)
                continue
            count += 1
            byte_count += len(payload)
            next_packet = max(next_packet + interval, time.monotonic())
            time.sleep(max(0, next_packet - time.monotonic()))
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', args.port))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        socket.inet_aton(args.group) + socket.inet_aton(args.interface))
        sock.settimeout(1)
        sequences = set()
        duplicates = 0
        while time.monotonic() < deadline:
            try:
                payload, _ = sock.recvfrom(65535)
                sequence = struct.unpack('!Q', payload[:8])[0]
                duplicates += sequence in sequences
                sequences.add(sequence)
                count += 1
                byte_count += len(payload)
            except socket.timeout:
                pass
        print('duplicate packets:', duplicates, flush=True)
    print(f'{args.mode}: {count} packets, {byte_count} bytes', flush=True)
    sock.close()


if __name__ == '__main__':
    main()
