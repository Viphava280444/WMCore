#!/usr/bin/env python3
"""Minimal forward proxy for CI containers on a host whose docker daemon
programs no NAT (iptables:false): bridge containers cannot reach the outside,
but they CAN reach the host. This proxy runs on the host during a CI job and
gives containers HTTPS (CONNECT tunnel) and plain-HTTP egress. stdlib only.

Usage: ci-proxy.py PORT   (binds 0.0.0.0:PORT; Ctrl-C / SIGTERM to stop)
"""
import select
import signal
import socket
import socketserver
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A proxy must relay 3xx to the client, not follow it: following an
    http->https redirect here would strip the client's own credentials
    (grid certificate) from the https hop. The client follows the Location
    itself and reaches https through the CONNECT tunnel with its cert."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class Handler(socketserver.StreamRequestHandler):
    timeout = 120

    def handle(self):
        try:
            request_line = self.rfile.readline(8192).decode('latin-1').strip()
            if not request_line:
                return
            method, target = request_line.split(' ', 2)[:2]
            headers = []
            while True:
                line = self.rfile.readline(8192)
                if line in (b'\r\n', b'\n', b''):
                    break
                headers.append(line)
            if method.upper() == 'CONNECT':
                self.tunnel(target)
            else:
                self.plain_http(method, target, headers)
        except Exception as exc:  # pylint: disable=broad-exception-caught; the proxy must never crash the job
            print(f"proxy error: {exc}", flush=True)

    def tunnel(self, target):
        host, _, port = target.rpartition(':')
        upstream = socket.create_connection((host, int(port or 443)), timeout=30)
        self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        self.wfile.flush()
        conns = [self.connection, upstream]
        try:
            while True:
                readable, _, _ = select.select(conns, [], [], 300)
                if not readable:
                    break
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is self.connection else self.connection).sendall(data)
        finally:
            upstream.close()

    def plain_http(self, method, target, headers):
        # Only proxy real web egress. build_opener() also registers urllib's
        # default file://, ftp:// and data:// handlers, so a container could
        # ask this host-side proxy to read a local file (e.g. the mounted grid
        # key). Refuse any non-web scheme before it reaches the opener.
        if urllib.parse.urlsplit(target).scheme.lower() not in ('http', 'https'):
            self.wfile.write(b'HTTP/1.1 403 Forbidden\r\n'
                             b'Content-Length: 0\r\nConnection: close\r\n\r\n')
            return
        req = urllib.request.Request(target, method=method)
        for raw in headers:
            try:
                name, value = raw.decode('latin-1').split(':', 1)
                if name.strip().lower() not in ('proxy-connection', 'connection', 'host'):
                    req.add_header(name.strip(), value.strip())
            except ValueError:
                continue
        try:
            resp = OPENER.open(req, timeout=60)
        except urllib.error.HTTPError as httperr:
            # a 3xx/4xx/5xx is a valid HTTP response the client must see:
            # tests like Requests_t:test404Error assert on the status code,
            # and swallowing it here turned into (52, 'Empty reply from
            # server'); 3xx additionally needs its Location header relayed
            resp = httperr
        with resp:
            body = resp.read()
            status = getattr(resp, 'status', None) or resp.code
            reason = getattr(resp, 'reason', '') or 'OK'
            self.wfile.write(f'HTTP/1.1 {status} {reason}\r\n'.encode('latin-1'))
            for name, value in resp.headers.items():
                if name.lower() in ('connection', 'keep-alive', 'proxy-connection',
                                    'proxy-authenticate', 'transfer-encoding',
                                    'content-length', 'upgrade'):
                    continue
                self.wfile.write(f'{name}: {value}\r\n'.encode('latin-1'))
            self.wfile.write(f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode('latin-1'))
            self.wfile.write(body)


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class UnixProxy(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else '3128'
    if '/' in target:
        # unix-socket mode: the VM's nftables drops all container<->host IP
        # traffic, but a bind-mounted filesystem socket bypasses netfilter
        import os
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        server = UnixProxy(target, Handler)
        print(f"ci-proxy listening on unix socket {target}", flush=True)
    else:
        server = Proxy(('0.0.0.0', int(target)), Handler)
        print(f"ci-proxy listening on 0.0.0.0:{target}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    server.serve_forever()


if __name__ == '__main__':
    main()
