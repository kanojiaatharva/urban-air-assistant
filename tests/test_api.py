"""Black-box tests of the HTTP API: an in-process server on an ephemeral port."""
import http.client
import json
import threading
import unittest
from unittest import mock

from airassist import airdata
from airassist.llm import Assistant
from airassist.server import RateLimiter, make_server


class ServerCase(unittest.TestCase):
    limit = 1000

    @classmethod
    def setUpClass(cls):
        cls.httpd = make_server("127.0.0.1", 0, assistant=Assistant(api_key=""),
                                limiter=RateLimiter(limit=cls.limit))  # fmt: skip
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
        merged = {"Content-Type": "application/json", **(headers or {})} if method == "POST" else {}
        conn.request(method, path, payload, merged)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data


class ApiTests(ServerCase):
    def test_health_and_security_headers(self):
        res, body = self.call("GET", "/healthz")
        self.assertEqual(json.loads(body), {"status": "ok", "ai": False})
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("default-src 'self'", res.getheader("Content-Security-Policy"))
        self.assertEqual(res.getheader("Server"), "AirAssist")

    def test_serves_only_allowlisted_assets(self):
        for path, marker in (("/", b"<html"), ("/app.js", b"use strict"), ("/style.css", b"--page")):
            res, body = self.call("GET", path)
            self.assertEqual(res.status, 200)
            self.assertIn(marker, body)
        for path in ("/../README.md", "/airassist/server.py", "/static/app.js", "/%2e%2e/README.md"):
            self.assertEqual(self.call("GET", path)[0].status, 404, path)

    def test_plan_endpoint(self):
        res, body = self.call("POST", "/api/plan",
                              {"pm25": 220, "profile": {"children": True, "cooking": "biomass"}})  # fmt: skip
        data = json.loads(body)
        self.assertEqual(res.status, 200)
        self.assertEqual(data["plan"]["air"]["label"], "Very unhealthy")
        self.assertEqual(data["summary"]["source"], "offline")
        self.assertTrue(data["plan"]["actions"])

    def test_ask_endpoint(self):
        res, body = self.call("POST", "/api/ask", {"aqi": 180, "question": "Is cooking a problem?"})
        self.assertEqual(res.status, 200)
        self.assertIn("text", json.loads(body)["answer"])

    def test_validation_errors_are_400(self):
        cases = [{"pm25": -5}, {}, {"pm25": 10, "aqi": 10}, {"pm25": 10, "profile": {"hack": 1}},
                 {"pm25": 10, "extra": 1}, {"pm25": 10, "profile": None}, {"pm25": 10, "question": "x"},
                 [1, 2]]  # fmt: skip
        for payload in cases:
            with self.subTest(payload=payload):
                res, body = self.call("POST", "/api/plan", payload)
                self.assertEqual(res.status, 400)
                self.assertNotIn(b"Traceback", body)
        self.assertEqual(self.call("POST", "/api/ask", {"pm25": 10})[0].status, 400)  # no question

    def test_malformed_requests(self):
        self.assertEqual(self.call("POST", "/api/plan", b"{not json")[0].status, 400)
        self.assertEqual(self.call("POST", "/api/plan", b"{}", {"Content-Type": "text/plain"})[0].status, 415)
        self.assertEqual(self.call("POST", "/api/plan", b"x" * 5000)[0].status, 413)
        self.assertEqual(self.call("POST", "/api/nope", {})[0].status, 404)

    def test_air_lookup_maps_errors(self):
        with mock.patch.object(airdata, "lookup", return_value=airdata.Reading("Testville", 77.7)):
            res, body = self.call("GET", "/api/air?city=Testville")
            self.assertEqual((res.status, json.loads(body)["pm25"]), (200, 77.7))
        for exc, status in ((airdata.PlaceNotFound(), 404), (airdata.InvalidCity("bad"), 400),
                            (OSError("down"), 502)):  # fmt: skip
            with mock.patch.object(airdata, "lookup", side_effect=exc):
                self.assertEqual(self.call("GET", "/api/air?city=x")[0].status, status)


class RateLimitTests(ServerCase):
    limit = 3

    def test_blocks_after_limit(self):
        statuses = [self.call("GET", "/healthz")[0].status for _ in range(5)]
        self.assertEqual(statuses, [200, 200, 200, 429, 429])


class RateLimiterTests(unittest.TestCase):
    def test_window_resets_and_clients_are_independent(self):
        clock = [0.0]
        limiter = RateLimiter(limit=1, window_s=10, clock=lambda: clock[0])
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))
        clock[0] = 11
        self.assertTrue(limiter.allow("a"))

    def test_memory_is_bounded(self):
        limiter = RateLimiter(limit=1, max_clients=2, clock=lambda: 0.0)
        self.assertTrue(limiter.allow("a") and limiter.allow("b"))
        self.assertFalse(limiter.allow("c"))  # table full of live windows: fail closed


if __name__ == "__main__":
    unittest.main()
