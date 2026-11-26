"""Dependency-free HTTP server: JSON API plus the static web UI."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import SplitResult, parse_qs, urlsplit

from . import airdata
from .advisor import Profile, build_plan
from .aqi import classify
from .llm import Assistant

log = logging.getLogger("airassist")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ASSETS = {  # request path -> (file, content type); nothing else is ever served
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY = 4096
DRAIN_LIMIT = 64 * 1024  # most we read and discard from an oversized request
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}


class ApiError(Exception):
    """An error whose message is safe to show to the client."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class RateLimiter:
    """Fixed-window, per-client limiter. Memory is bounded by pruning expired windows."""

    def __init__(self, limit: int = 60, window_s: float = 60.0, max_clients: int = 4096,
                 clock: Callable[[], float] = time.monotonic):  # fmt: skip
        self._limit, self._window, self._max, self._clock = limit, window_s, max_clients, clock
        self._hits: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, client: str) -> bool:
        now = self._clock()
        with self._lock:
            start, count = self._hits.get(client, (now, 0))
            if now - start >= self._window:
                start, count = now, 0
            if count >= self._limit:
                return False
            if client not in self._hits and len(self._hits) >= self._max:
                self._hits = {c: h for c, h in self._hits.items() if now - h[0] < self._window}
                if len(self._hits) >= self._max:
                    return False
            self._hits[client] = (start, count + 1)
            return True


class Handler(BaseHTTPRequestHandler):
    server_version = "AirAssist"
    timeout = 10  # seconds; drops stalled clients

    def version_string(self) -> str:
        return self.server_version

    def do_GET(self) -> None:
        self._dispatch(self._get)

    def do_HEAD(self) -> None:
        self._dispatch(self._get)  # _send suppresses the body when command == "HEAD"

    def do_POST(self) -> None:
        self._dispatch(self._post)

    def _dispatch(self, route: Callable[[SplitResult], None]) -> None:
        try:
            if not self.server.limiter.allow(self.client_address[0]):
                raise ApiError(429, "Too many requests. Please wait a minute.")
            route(urlsplit(self.path))
        except ApiError as err:
            self._json({"error": str(err)}, err.status)
        except Exception:  # never leak internals to clients
            log.exception("unhandled error")
            self._json({"error": "Internal error."}, 500)

    # -- routes -----------------------------------------------------------
    def _get(self, url: SplitResult) -> None:
        if url.path in ASSETS:
            body, ctype = self.server.assets[url.path]
            return self._send(200, body, ctype, "public, max-age=300")
        if url.path == "/healthz":
            return self._json({"status": "ok", "ai": self.server.assistant.enabled})
        if url.path == "/api/air":
            city = parse_qs(url.query).get("city", [""])[0]
            return self._json(self._lookup(city))
        raise ApiError(404, "Not found.")

    def _post(self, url: SplitResult) -> None:
        body = self._read_body()  # always consume the body before replying
        if url.path not in ("/api/plan", "/api/ask"):
            raise ApiError(404, "Not found.")
        if self.headers.get_content_type() != "application/json":
            raise ApiError(415, "Send application/json.")
        try:
            data = json.loads(body)
        except ValueError:
            raise ApiError(400, "Invalid JSON.") from None
        asking = url.path == "/api/ask"
        if not isinstance(data, dict) or not set(data) <= {"pm25", "aqi", "profile", "question"}:
            raise ApiError(400, "Expected a JSON object with known fields only.")
        try:
            profile = Profile.from_dict(data.get("profile", {}))
            air = classify(pm25=data.get("pm25"), aqi=data.get("aqi"))
            # The plan is always recomputed here; clients never supply plan text.
            plan = build_plan(profile, air)
            if asking:
                reply = self.server.assistant.answer(plan, data.get("question"), profile.language)
                return self._json({"answer": reply})
            if "question" in data:
                raise ValueError("question is only accepted by /api/ask")
            return self._json({"plan": plan, "summary": self.server.assistant.summarize(plan, profile.language)})
        except ValueError as err:
            raise ApiError(400, str(err)) from None

    @staticmethod
    def _lookup(city: str) -> dict:
        try:
            reading = airdata.lookup(city)
        except airdata.InvalidCity as err:
            raise ApiError(400, str(err)) from None
        except airdata.PlaceNotFound:
            raise ApiError(404, "We could not find air data for that place.") from None
        except (OSError, ValueError, KeyError, TypeError):
            raise ApiError(502, "The live air-quality service is unavailable. Enter a reading instead.") from None
        return {"place": reading.place, "pm25": reading.pm25, "source": "Open-Meteo"}

    # -- plumbing ---------------------------------------------------------
    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "Content-Length required.") from None
        if length < 0:
            raise ApiError(400, "Bad Content-Length.")
        if length > MAX_BODY:
            self.rfile.read(min(length, DRAIN_LIMIT))
            raise ApiError(413, "Request too large.")
        return self.rfile.read(length)

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def log_request(self, code: object = "-", size: object = "-") -> None:
        # Path only: query strings can contain place names, which we never log.
        log.info("%s %s %s", self.command, urlsplit(self.path).path, code)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        log.warning(format, *args)


def make_server(host: str = "127.0.0.1", port: int = 8000, *, assistant: Assistant | None = None,
                limiter: RateLimiter | None = None) -> ThreadingHTTPServer:  # fmt: skip
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.assistant = assistant or Assistant()
    httpd.limiter = limiter or RateLimiter()
    httpd.assets = {
        path: ((STATIC_DIR / name).read_bytes(), ctype) for path, (name, ctype) in ASSETS.items()
    }  # read once at startup: no per-request disk I/O, no user-controlled paths
    return httpd


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    default_host = "0.0.0.0" if ("PORT" in os.environ or "RENDER" in os.environ) else "127.0.0.1"
    host, port = os.environ.get("HOST", default_host), int(os.environ.get("PORT", "8000"))
    httpd = make_server(host, port)
    log.info("Serving on http://%s:%d (AI %s)", host, port, "on" if httpd.assistant.enabled else "offline")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
