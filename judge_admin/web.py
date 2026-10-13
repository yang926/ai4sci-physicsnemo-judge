"""Authenticated operator listeners; never mounted on student/projector APIs."""
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
from urllib.parse import urlsplit

from ai4sci_judge.web import ASSETS as BOARD_ASSETS
from .auth import CredentialError, validate_password

ASSETS = Path(__file__).parent / "static"
SESSION_SECONDS = 8 * 3600
SECURE_LINK_WORKERS = 8


def validate_secure_link(value):
    if value is None:
        return None
    required = {"hostname", "bind_address", "port", "allowed_peers"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Secure Link requires exactly hostname, bind_address, port and allowed_peers.")
    hostname = value["hostname"]
    if (not isinstance(hostname, str) or not hostname.isascii() or hostname != hostname.lower()
            or not 3 <= len(hostname) <= 253 or len(hostname.split(".")) < 2
            or any(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                   for label in hostname.split(".")) or hostname.endswith((".localhost", ".local"))):
        raise ValueError("Use the exact registered lowercase DNS hostname, without scheme, port or path.")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Secure Link hostname must not be an IP address.")

    def address(raw):
        if not isinstance(raw, str):
            raise ValueError("Use an exact private overlay IPv4 address.")
        try:
            parsed = ipaddress.IPv4Address(raw)
        except ipaddress.AddressValueError:
            raise ValueError("Use an exact private overlay IPv4 address, not a subnet.") from None
        if str(parsed) != raw or (raw != "127.0.0.1" and parsed not in ipaddress.IPv4Network("100.64.0.0/10")):
            raise ValueError("Only loopback or an exact private overlay IPv4 address is allowed.")
        return raw

    peers = value["allowed_peers"]
    if not isinstance(peers, list) or not 1 <= len(peers) <= 16:
        raise ValueError("List between one and sixteen exact allowed gateway IPv4 addresses.")
    checked = [address(peer) for peer in peers]
    if len(set(checked)) != len(checked):
        raise ValueError("Allowed gateway IPv4 addresses must not repeat.")
    if type(value["port"]) is not int or not 1 <= value["port"] <= 65535:
        raise ValueError("Choose one valid Secure Link listener port.")
    return {"hostname": hostname, "bind_address": address(value["bind_address"]),
            "port": value["port"], "allowed_peers": checked}


class SecureAdminServer(ThreadingHTTPServer):
    """Additional private-interface listener; no HTTP is read from other peers."""
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, settings, handler):
        self.secure_link_hostname = settings["hostname"]
        self.allowed_peers = frozenset(settings["allowed_peers"])
        self.slots = threading.BoundedSemaphore(SECURE_LINK_WORKERS)
        super().__init__((settings["bind_address"], settings["port"]), handler)

    def verify_request(self, request, client_address):
        return client_address[0] in self.allowed_peers

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
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

    def handle_error(self, *_args):
        pass


def create_server(live, demo, password, port=8092, projector_url="http://localhost:8091/display", secure_link=None,
                  credential_store=None):
    if credential_store is None and (not isinstance(password, str) or len(password) < 24):
        raise ValueError("Use a randomly generated administrator password of at least 24 characters.")
    secure_link = validate_secure_link(secure_link)
    sessions, attempts, change_attempts = {}, deque(), deque()
    auth_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(210)

        def log_message(self, *_):
            pass

        @property
        def cookie_name(self):
            return f"ai4sci_admin_{self.server.server_port}"

        def session_cookie(self, key, max_age):
            secure = "; Secure" if getattr(self.server, "secure_link_hostname", None) else ""
            return f"{self.cookie_name}={key}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

        def response_headers(self, code, size, mime, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", mime + ("; charset=utf-8" if mime != "application/octet-stream" else ""))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()

        def send(self, code, value, mime="application/json", headers=None):
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if mime == "application/json" else value
            self.response_headers(code, len(data), mime, headers)
            self.wfile.write(data)

        def check_origin(self, mutation=False):
            # The private listener trusts its configured hostname, never
            # X-Forwarded-Host/Proto. Local HTTP behavior remains unchanged.
            hostname = getattr(self.server, "secure_link_hostname", None)
            authority_headers = ("Host", "Origin", "Cookie", "X-CSRF-Token", "Content-Length",
                                 "Transfer-Encoding", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest")
            if any(len(self.headers.get_all(name, [])) > 1 for name in authority_headers):
                self.send(403, {"error": "중복된 인증·요청 헤더는 허용하지 않습니다."})
                return False
            host = self.headers.get("Host", "")
            allowed = {hostname} if hostname else {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            expected_origin = "https://" + hostname if hostname else "http://" + host
            origin = self.headers.get("Origin")
            if host not in allowed or (origin is not None and origin != expected_origin) or (mutation and origin != expected_origin):
                self.send(403, {"error": "관리자 페이지와 같은 주소에서 요청해야 합니다."})
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                entry_navigation = (hostname and not mutation and self.command == "GET"
                                    and urlsplit(self.path).path in {"/", "/admin", "/demo-admin"}
                                    and self.headers.get("Sec-Fetch-Mode") == "navigate"
                                    and self.headers.get("Sec-Fetch-Dest") == "document")
                if not entry_navigation:
                    self.send(403, {"error": "외부 사이트의 요청은 허용하지 않습니다."})
                    return False
            return True

        def session(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                key = cookie[self.cookie_name].value if self.cookie_name in cookie else ""
            except Exception:
                return None
            with auth_lock:
                now = time.monotonic()
                for old in list(sessions):
                    if sessions[old]["expires"] <= now:
                        del sessions[old]
                entry = sessions.get(key)
            return (key, entry) if entry else None

        def authenticated(self):
            session = self.session()
            if not session:
                self.send(401, {"error": "관리자 로그인이 필요합니다."})
            return session

        def context(self):
            path = urlsplit(self.path).path
            if path.startswith("/api/demo/"):
                return demo, "demo", "/api/" + path[len("/api/demo/"):]
            return live, "live", path

        def status(self, storage, mode, session):
            return {**storage.status(), "mode": mode, "csrf_token": session[1]["csrf"],
                    "projector_url": "/demo" if mode == "demo" else projector_url,
                    "password_change_enabled": credential_store is not None,
                    "backups": storage.backups(), "audit": storage.audit()}

        def do_GET(self):
            if not self.check_origin():
                return
            storage, mode, path = self.context()
            static = {"/": "admin.html", "/admin": "admin.html", "/demo-admin": "admin.html",
                      "/admin.js": "admin.js", "/admin.css": "admin.css"}
            if path in static:
                filename = static[path]
                mime = "text/html" if filename.endswith("html") else "text/javascript" if filename.endswith("js") else "text/css"
                self.send(200, (ASSETS / filename).read_bytes(), mime)
                return
            if path in {"/brand-fonts.json", "/brand-logo.svg"}:
                html = (BOARD_ASSETS / "display.html").read_text()
                if path.endswith("json"):
                    fonts = re.search(r'<script id="brand-fonts" type="application/json">(.*?)</script>', html, re.S)
                    self.send(200, json.loads(fonts.group(1)))
                else:
                    logo = re.search(r'<svg class="nvidia-logo".*?</svg>', html, re.S).group(0)
                    # Original geometry/colors; this standalone SVG needs the existing CSS color.
                    logo = logo.replace('class="cls-1"', 'class="cls-1" fill="#76b900"')
                    self.send(200, logo.encode(), "image/svg+xml")
                return
            session = self.authenticated()
            if not session:
                return
            try:
                if path == "/api/status":
                    self.send(200, self.status(storage, mode, session))
                elif path.startswith("/api/backups/"):
                    identifier = path[len("/api/backups/"):]
                    file = storage.backup_file(identifier)
                    with file.open("rb") as snapshot:
                        self.response_headers(200, file.stat().st_size, "application/octet-stream",
                                              {"Content-Disposition": f'attachment; filename="{identifier}.sqlite3"'})
                        shutil.copyfileobj(snapshot, self.wfile, length=1024 * 1024)
                elif path in {"/demo", "/demo/"}:
                    html = (BOARD_ASSETS / "display.html").read_text().replace('src="/app.js"', 'src="/demo/app.js"')
                    html = html.replace('href="/style.css"', 'href="/demo/style.css"')
                    html = html.replace('<main>', '<main><p class="demo-banner">DEMO · Sample participants and scores, separate from live event records. <a href="/demo-admin">Demo administration</a></p>')
                    self.send(200, html.encode(), "text/html")
                elif path == "/demo/app.js":
                    js = (BOARD_ASSETS / "app.js").read_text().replace('"/api/board', '"/demo/api/board').replace('`/api/board', '`/demo/api/board')
                    self.send(200, js.encode(), "text/javascript")
                elif path == "/demo/style.css":
                    css = (BOARD_ASSETS / "style.css").read_bytes() + b'\n.demo-banner{background:#fff2ce;color:#4b3600;padding:12px 18px;font-weight:700;border-left:5px solid #bc8200}.demo-banner a{color:inherit;margin-left:20px}\n'
                    self.send(200, css, "text/css")
                elif path == "/demo/api/board":
                    from urllib.parse import parse_qs
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if not query:
                        self.send(200, demo.store.board())
                    elif set(query) == {"challenge"} and len(query["challenge"]) == 1:
                        self.send(200, demo.store.board(query["challenge"][0]))
                    else:
                        raise ValueError("Choose one Challenge.")
                else:
                    self.send(404, {"error": "Not found"})
            except (ValueError, FileNotFoundError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception:
                self.send(503, {"error": "관리 작업을 완료하지 못했습니다. 상태를 확인하고 다시 시도하세요."})

        def payload(self):
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json" or self.headers.get("Transfer-Encoding"):
                raise ValueError("Send application/json with Content-Length.")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("Request must be between 1 and 4096 bytes.")
            result = json.loads(self.rfile.read(size))
            if not isinstance(result, dict):
                raise ValueError("Request must be a JSON object.")
            return result

        def do_POST(self):
            if not self.check_origin(mutation=True):
                return
            storage, mode, path = self.context()
            try:
                payload = self.payload()
                if path == "/api/login" and mode == "live":
                    with auth_lock:
                        now = time.monotonic()
                        while attempts and attempts[0] < now - 60:
                            attempts.popleft()
                        if len(attempts) >= 10:
                            self.send(429, {"error": "로그인 시도가 많습니다. 1분 뒤 다시 시도하세요."})
                            return
                        candidate = payload.get("password")
                        valid = (set(payload) == {"password"} and isinstance(candidate, str)
                                 and (credential_store.verify(candidate) if credential_store is not None
                                      else secrets.compare_digest(candidate.encode(), password.encode())))
                        if not valid:
                            attempts.append(now)
                            self.send(401, {"error": "관리자 비밀번호가 맞지 않습니다."})
                            return
                        key, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                        if len(sessions) >= 32:
                            sessions.pop(next(iter(sessions)))
                        sessions[key] = {"csrf": csrf, "expires": now + SESSION_SECONDS}
                    self.send(200, {"csrf_token": csrf}, headers={"Set-Cookie": self.session_cookie(key, SESSION_SECONDS)})
                    return
                session = self.authenticated()
                if not session:
                    return
                if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), session[1]["csrf"]):
                    self.send(403, {"error": "페이지를 새로고침한 뒤 다시 시도하세요."})
                    return
                if path == "/api/password/change":
                    if mode != "live" or credential_store is None:
                        self.send(404, {"error": "Not found"})
                        return
                    if (set(payload) != {"current_password", "new_password", "confirmation"}
                            or any(not isinstance(value, str) for value in payload.values())):
                        raise ValueError("Invalid operation fields.")
                    new_password = validate_password(payload["new_password"])
                    if not secrets.compare_digest(new_password.encode(), payload["confirmation"].encode()):
                        raise ValueError("새 비밀번호와 확인 입력이 일치하지 않습니다.")
                    with auth_lock:
                        # A change in another request may have invalidated the
                        # session after authenticated() returned above.
                        if sessions.get(session[0]) is not session[1] or session[1]["expires"] <= time.monotonic():
                            self.send(401, {"error": "관리자 로그인이 필요합니다."})
                            return
                        now = time.monotonic()
                        while change_attempts and change_attempts[0] < now - 60:
                            change_attempts.popleft()
                        if len(change_attempts) >= 10:
                            self.send(429, {"error": "비밀번호 확인 시도가 많습니다. 1분 뒤 다시 시도하세요."})
                            return
                        if not credential_store.verify(payload["current_password"]):
                            change_attempts.append(now)
                            self.send(400, {"error": "현재 비밀번호가 맞지 않습니다."})
                            return
                        try:
                            credential_store.replace(new_password)
                        except CredentialError as exc:
                            if exc.committed:
                                # The atomic rename happened, but a durability
                                # check failed. Never retain old authenticated sessions.
                                sessions.clear()
                                self.send(503, {"error": "비밀번호 저장 상태를 확인하지 못했습니다. 새 비밀번호로 다시 로그인하세요.",
                                                "reauthenticate": True},
                                          headers={"Set-Cookie": self.session_cookie("", 0)})
                                return
                            raise
                        sessions.clear()
                        attempts.clear()
                        change_attempts.clear()
                    self.send(200, {"ok": True, "reauthenticate": True},
                              headers={"Set-Cookie": self.session_cookie("", 0)})
                    return
                schemas = {"/api/logout": set(), "/api/backup": set(), "/api/pause": set(), "/api/resume": set(),
                           "/api/reset": {"scope", "confirmation"}, "/api/restore": {"backup_id", "confirmation"},
                           "/api/challenges/action": {"challenge", "action"}}
                if path not in schemas:
                    self.send(404, {"error": "Not found"})
                    return
                if set(payload) != schemas[path] or any(not isinstance(v, str) for v in payload.values()):
                    raise ValueError("Invalid operation fields.")
                if path == "/api/logout":
                    with auth_lock:
                        sessions.pop(session[0], None)
                    self.send(200, {"ok": True}, headers={"Set-Cookie": self.session_cookie("", 0)})
                    return
                if path == "/api/backup":
                    result = storage.create_backup()
                elif path == "/api/pause":
                    result = storage.pause()
                elif path == "/api/resume":
                    result = storage.resume()
                elif path == "/api/reset":
                    result = storage.reset(payload["scope"], payload["confirmation"])
                elif path == "/api/challenges/action":
                    result = storage.challenge_action(payload["challenge"], payload["action"])
                else:
                    result = storage.restore(payload["backup_id"], payload["confirmation"])
                self.send(200, {"ok": True, "result": result})
            except CredentialError:
                self.send(503, {"error": "관리자 비밀번호 저장소를 사용할 수 없습니다. 서버 상태를 확인하세요."})
            except (ValueError, TypeError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception:
                self.send(503, {"error": "작업이 완료되지 않았습니다. 접수 상태와 백업 목록을 확인하세요. 자동으로 접수를 재개하지 않습니다."})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.secure_server = None
    if secure_link is not None:
        try:
            if secure_link["port"] == server.server_port:
                raise ValueError("Local and Secure Link administrator listeners need different ports.")
            server.secure_server = SecureAdminServer(secure_link, Handler)
        except BaseException:
            server.server_close()
            raise
    return server
