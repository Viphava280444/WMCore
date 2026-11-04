#!/usr/bin/env python3
"""In-container side of the firewall-free egress path: listens on a loopback
TCP port inside the stack's private network namespace and pipes every
connection into a unix socket that is bind-mounted from the host, where
ci-proxy.py serves it. The VM's nftables (FORWARD drop, INPUT drop) never
sees this traffic because a filesystem socket has no IP hooks. stdlib only.

Usage: ci-shim.py 127.0.0.1:3128 /tmp/wmci-egress.sock
"""
import select
import socket
import socketserver
import sys

UNIX_PATH = sys.argv[2] if len(sys.argv) > 2 else '/tmp/wmci-egress.sock'


class Pipe(socketserver.BaseRequestHandler):
    timeout = 300

    def handle(self):
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.connect(UNIX_PATH)
        conns = [self.request, upstream]
        try:
            while True:
                readable, _, _ = select.select(conns, [], [], 300)
                if not readable:
                    return
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is self.request else self.request).sendall(data)
        finally:
            upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    host, _, port = (sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1:3128').rpartition(':')
    server = Server((host or '127.0.0.1', int(port)), Pipe)
    print(f"ci-shim {host}:{port} -> {UNIX_PATH}", flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
