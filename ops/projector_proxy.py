#!/usr/bin/python3 -I
"""Restricted Secure Link adapter; Brev, not this process, authenticates viewers.

Only the configured HTTPS hostname may reach the existing read-only projector.
The listener defaults to loopback; an exact private overlay address and peer
allowlist may be configured for Brev's gateway. Upstream is always loopback.
The frozen judge package and its state are not imported or opened.
"""
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import ipaddress
import json
import re
import socket
import threading
from urllib.parse import urlsplit


LISTEN_PORT = 8093
UPSTREAM_PORT = 8091
SOCKET_TIMEOUT = 5
REQUEST_DEADLINE = 15
MAX_WORKERS = 16
MAX_BODY = 2 * 1024 * 1024
MAX_TARGET = 2048
MAX_HEADERS = 16384
ROUTES = frozenset({"/", "/display", "/display/", "/app.js", "/style.css", "/api/board"})
DEFAULT_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
OVERLAY_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")


def validated_address(value):
    if not isinstance(value, str):
        raise ValueError("Use an exact IPv4 address, not a hostname or subnet")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValueError("Use an exact IPv4 address, not a hostname or subnet") from None
    if str(address) != value or (value != "127.0.0.1" and address not in OVERLAY_NETWORK):
        raise ValueError("Only 127.0.0.1 or one exact private overlay IPv4 address is allowed")
    return value


def validated_peers(value):
    peers = value.split(",") if isinstance(value, str) else list(value)
    if not 1 <= len(peers) <= 16:
        raise ValueError("Supply between one and sixteen exact allowed peer IPv4 addresses")
    checked = [validated_address(peer) for peer in peers]
    if len(set(checked)) != len(checked):
        raise ValueError("Allowed peer IPv4 addresses must not be repeated")
    return frozenset(checked)


def validated_hostname(value):
    if not isinstance(value, str) or not value.isascii() or value != value.lower() or not 3 <= len(value) <= 253:
        raise ValueError("Supply the exact lowercase Secure Link DNS hostname without scheme, path or port")
    labels = value.split(".")
    label_pattern = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(labels) < 2 or any(re.fullmatch(label_pattern, label) is None for label in labels):
        raise ValueError("Supply one valid Secure Link DNS hostname")
    if value.endswith((".localhost", ".local")):
        raise ValueError("A local hostname is not a Secure Link hostname")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("An IP address is not a Secure Link hostname")


def close_socket(connection):
    if connection is not None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass


class HeaderReader:
    """Cap aggregate header bytes before the standard parser buffers them."""
    def __init__(self, reader):
        self.reader = reader
        self.remaining = MAX_HEADERS

    def readline(self, limit=-1):
        bounded = self.remaining + 1
        if limit >= 0:
            bounded = min(bounded, limit)
        line = self.reader.readline(bounded)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise HTTPException("Header block exceeded its byte limit")
        return line


class ProjectorServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    block_on_close = False

    def __init__(self, hostname, bind_address="127.0.0.1", allowed_peers=("127.0.0.1",)):
        self.hostname = validated_hostname(hostname)
        self.origin = "https://" + self.hostname
        self.allowed_peers = validated_peers(allowed_peers)
        bind_address = validated_address(bind_address)
        self.slots = threading.BoundedSemaphore(MAX_WORKERS)
        super().__init__((bind_address, LISTEN_PORT), ProjectorHandler)

    def process_request(self, request, client_address):
        if client_address[0] not in self.allowed_peers:
            # Inspect the actual socket peer before parsing any HTTP or
            # allocating a worker. Forwarded headers cannot grant admission.
            try:
                request.settimeout(.1)
                request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        if not self.slots.acquire(blocking=False):
            # Never queue unbounded worker threads or block the accept loop.
            try:
                request.settimeout(.1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, _request, _client_address):
        # No request headers, bearer credentials, cookies or URLs enter logs.
        pass


class ProjectorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(SOCKET_TIMEOUT)
        self.upstream_socket = None
        self.expired = threading.Event()
        super().setup()
        # A socket inactivity timeout alone does not stop slow-drip requests.
        # One watchdog per bounded worker caps headers, upstream and writes.
        self.watchdog = threading.Timer(REQUEST_DEADLINE, self.expire)
        self.watchdog.daemon = True
        self.watchdog.start()

    def expire(self):
        self.expired.set()
        close_socket(self.upstream_socket)
        close_socket(self.request)

    def finish(self):
        self.watchdog.cancel()
        self.watchdog.join()
        close_socket(self.upstream_socket)
        try:
            super().finish()
        except OSError:
            pass

    def log_message(self, *_args):
        pass

    def send_error(self, code, message=None, explain=None):
        # Do not reflect arbitrary parse errors or request material.
        self.reject(code)

    def respond(self, code, body, content_type="application/json; charset=utf-8", csp=DEFAULT_CSP):
        self.close_connection = True
        self.send_response_only(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", csp)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        if code == 405:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def reject(self, code):
        body = json.dumps({"error": "The scoreboard link could not serve this request."}).encode()
        try:
            self.respond(code, body)
        except OSError:
            pass

    def handle_expect_100(self):
        self.reject(417)
        return False

    def parse_request(self):
        original = self.rfile
        self.rfile = HeaderReader(original)
        try:
            parsed_ok = super().parse_request()
        finally:
            self.rfile = original
        if not parsed_ok:
            return False
        self.close_connection = True
        if self.command not in {"GET", "HEAD"}:
            self.reject(405)
            return False
        if self.request_version not in {"HTTP/1.0", "HTTP/1.1"}:
            self.reject(400)
            return False
        if len(self.headers) > 32 or sum(len(key) + len(value) + 4 for key, value in self.headers.items()) > MAX_HEADERS:
            self.reject(431)
            return False
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if hosts != [self.server.hostname] or len(origins) > 1 or (origins and origins != [self.server.origin]):
            self.reject(403)
            return False
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Expect") or lengths not in ([], ["0"]):
            self.reject(400)
            return False
        # BaseHTTPRequestHandler normalizes leading //; validate the original
        # target as well so absolute/network-path URL forms remain forbidden.
        words = self.raw_requestline.split()
        target = words[1] if len(words) == 3 else b""
        if len(target) > MAX_TARGET:
            self.reject(414)
            return False
        if not target.startswith(b"/") or target.startswith(b"//") or b"\\" in target or b"#" in target or any(byte < 33 or byte > 126 for byte in target):
            self.reject(400)
            return False
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.path not in ROUTES:
            self.reject(404)
            return False
        return True

    @staticmethod
    def upstream_header(response, name, default=None, limit=2048):
        values = response.headers.get_all(name, [])
        if not values:
            return default
        if len(values) != 1 or len(values[0]) > limit or any(ord(char) < 32 or ord(char) > 126 for char in values[0]):
            raise ValueError("Invalid upstream header")
        return values[0]

    def do_GET(self):
        connection = HTTPConnection("127.0.0.1", UPSTREAM_PORT, timeout=SOCKET_TIMEOUT)
        try:
            connection.connect()
            self.upstream_socket = connection.sock
            if self.expired.is_set():
                raise TimeoutError
            # Construct headers from scratch. No Cookie, Authorization,
            # Origin, Forwarded or X-* header from a viewer reaches the judge.
            connection.request("GET", self.path, headers={
                "Host": f"127.0.0.1:{UPSTREAM_PORT}",
                "Accept": "application/json" if urlsplit(self.path).path == "/api/board" else "*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            response = connection.getresponse()
            if not 200 <= response.status <= 599 or 300 <= response.status <= 399:
                raise ValueError("Unexpected upstream status")
            content_type = self.upstream_header(response, "Content-Type", limit=128)
            if content_type is None or re.fullmatch(r"(?:application/json|text/(?:html|css|javascript))(?:; charset=utf-8)?", content_type, re.I) is None:
                raise ValueError("Unexpected upstream content type")
            csp = self.upstream_header(response, "Content-Security-Policy", DEFAULT_CSP)
            declared = self.upstream_header(response, "Content-Length", limit=20)
            if declared is not None and (not declared.isdecimal() or int(declared) > MAX_BODY):
                raise ValueError("Unexpected upstream content length")
            transfers = response.headers.get_all("Transfer-Encoding", [])
            if transfers and (len(transfers) != 1 or transfers[0].lower() != "chunked" or declared is not None):
                raise ValueError("Ambiguous upstream response framing")
            if response.headers.get_all("Content-Encoding"):
                raise ValueError("Unexpected upstream content encoding")
            body = response.read(MAX_BODY + 1)
            if self.expired.is_set():
                raise TimeoutError
            if len(body) > MAX_BODY or (declared is not None and len(body) != int(declared)):
                raise ValueError("Invalid or oversized upstream response")
            self.respond(response.status, body, content_type, csp)
        except (TimeoutError, socket.timeout):
            self.reject(504)
        except (OSError, HTTPException, ValueError):
            self.reject(504 if self.expired.is_set() else 502)
        finally:
            connection.close()
            close_socket(self.upstream_socket)
            self.upstream_socket = None

    def do_HEAD(self):
        # The frozen projector implements GET but not HEAD. Fetch the same
        # bounded GET response, then omit its body while preserving its length.
        self.do_GET()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", required=True, help="Exact registered HTTPS hostname; no scheme or port")
    parser.add_argument("--bind-address", default="127.0.0.1", help="One exact loopback or private overlay IPv4 address")
    parser.add_argument("--allowed-peers", required=True, help="Comma-separated exact trusted peer IPv4 addresses; never a CIDR")
    args = parser.parse_args()
    try:
        hostname = validated_hostname(args.hostname)
        bind_address = validated_address(args.bind_address)
        allowed_peers = validated_peers(args.allowed_peers)
    except ValueError as error:
        parser.error(str(error))
    server = ProjectorServer(hostname, bind_address=bind_address, allowed_peers=allowed_peers)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
