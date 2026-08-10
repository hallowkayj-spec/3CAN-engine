#!/usr/bin/env python3
"""Small TCP relay for WSL agents reaching Windows-local 3CAN.

Use this when the terminal is not elevated enough to install a Windows
portproxy rule. Bind the listener to the WSL host/gateway address, not to
0.0.0.0, so 3CAN is not exposed beyond the local WSL virtual network.
"""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import threading
from contextlib import closing


BUFFER_SIZE = 64 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Relay WSL TCP traffic to Windows-local 3CAN.")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=9703)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=9700)
    return parser.parse_args()


def pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(BUFFER_SIZE)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle_client(client: socket.socket, peer: tuple[str, int], target: tuple[str, int]) -> None:
    logging.info("accepted %s:%s -> %s:%s", peer[0], peer[1], target[0], target[1])
    try:
        upstream = socket.create_connection(target, timeout=10)
    except OSError as exc:
        logging.warning("target connection failed for %s:%s: %s", peer[0], peer[1], exc)
        client.close()
        return

    left = threading.Thread(target=pump, args=(client, upstream), daemon=True)
    right = threading.Thread(target=pump, args=(upstream, client), daemon=True)
    left.start()
    right.start()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    listen = (args.listen_host, args.listen_port)
    target = (args.target_host, args.target_port)
    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logging.info("signal %s received; stopping relay", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(listen)
        server.listen(64)
        server.settimeout(1.0)
        logging.info("3CAN WSL relay listening on %s:%s -> %s:%s", *listen, *target)

        while not stop_event.is_set():
            try:
                client, peer = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if stop_event.is_set():
                    break
                logging.warning("accept failed: %s", exc)
                continue
            threading.Thread(target=handle_client, args=(client, peer, target), daemon=True).start()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
