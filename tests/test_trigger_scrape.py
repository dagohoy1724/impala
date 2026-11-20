#!/usr/bin/env python3
"""Unit and local-HTTP integration tests for scripts/trigger_scrape.py."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from scripts.trigger_scrape import (
    HttpResponse,
    ScrapeTriggerError,
    TriggerConfig,
    main,
    parse_dates,
    poll_scrape_all_status,
    run_scrape_workflow,
    start_scrape_all,
    wait_for_service_health,
)


class FakeClock:
    """Deterministic monotonic clock and sleep recorder for fast tests."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TriggerConfigTests(unittest.TestCase):
    def test_requires_base_url(self) -> None:
        with self.assertRaises(ScrapeTriggerError) as ctx:
            TriggerConfig.from_env({"API_KEY": "secret"})
        self.assertIn("Missing RENDER_API_URL", str(ctx.exception))

    def test_requires_api_key_by_default(self) -> None:
        with self.assertRaises(ScrapeTriggerError):
            TriggerConfig(base_url="https://scraper.example.com", api_key="")

    def test_rejects_non_http_url(self) -> None:
        with self.assertRaises(ScrapeTriggerError):
            TriggerConfig(base_url="ftp://scraper.example.com", api_key="secret")

    def test_normalizes_trailing_slash(self) -> None:
        cfg = TriggerConfig(
            base_url="https://scraper.example.com/",
            api_key="secret",
        )
        self.assertEqual(cfg.base_url, "https://scraper.example.com")

    def test_parse_dates_accepts_valid_iso_dates(self) -> None:
        self.assertEqual(
            parse_dates("2026-11-19, 2026-11-20"),
            ("2026-11-19", "2026-11-20"),
        )

    def test_parse_dates_rejects_invalid_format(self) -> None:
        with self.assertRaises(ScrapeTriggerError):
            parse_dates("11/19/2026")

    def test_parse_dates_rejects_impossible_calendar_date(self) -> None:
        with self.assertRaises(ScrapeTriggerError):
            parse_dates("2026-02-30")


class HealthWakeTests(unittest.TestCase):
    def test_retries_transient_errors_until_health_ok(self) -> None:
        clock = FakeClock()
        responses = [
            urllib.error.URLError("cold start timeout"),
            HttpResponse(status=502, body="Bad Gateway"),
            HttpResponse(status=503, body="Service Unavailable"),
            HttpResponse(status=200, body=json.dumps({"status": "ok", "turso_configured": True})),
        ]
        calls: list[tuple[str, str]] = []

        def fake_request(
            method: str,
            url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            calls.append((method, url))
            nxt = responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="test-key",
            health_timeout_seconds=30.0,
            health_interval_seconds=5.0,
        )
        payload = wait_for_service_health(
            cfg,
            request_fn=fake_request,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(calls), 4)
        self.assertEqual(clock.sleeps, [5.0, 5.0, 5.0])

    def test_fails_when_health_budget_exhausted(self) -> None:
        clock = FakeClock()

        def always_503(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return HttpResponse(status=503, body="Booting")

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="test-key",
            health_timeout_seconds=10.0,
            health_interval_seconds=5.0,
        )
        with self.assertRaises(ScrapeTriggerError) as ctx:
            wait_for_service_health(
                cfg,
                request_fn=always_503,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )
        self.assertNotIn("scraper.example.com", str(ctx.exception))


class StartAndPollTests(unittest.TestCase):
    def test_unauthorized_401_fails_immediately_without_retries(self) -> None:
        clock = FakeClock()
        attempts = 0

        def fake_request(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            nonlocal attempts
            attempts += 1
            return HttpResponse(
                status=401,
                body=json.dumps({"detail": "Invalid or missing API key."}),
            )

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="wrong-key",
            trigger_retries=3,
        )
        with self.assertRaises(ScrapeTriggerError) as ctx:
            start_scrape_all(cfg, request_fn=fake_request, sleep_fn=clock.sleep)

        self.assertEqual(attempts, 1)
        self.assertIn("401", str(ctx.exception))

    def test_redacts_api_key_if_echoed_in_error_body(self) -> None:
        secret = "super-secret-api-key-123"

        def fake_request(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return HttpResponse(status=400, body=f"Bad header Bearer {secret}")

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key=secret,
        )
        with self.assertRaises(ScrapeTriggerError) as ctx:
            start_scrape_all(cfg, request_fn=fake_request)

        self.assertNotIn(secret, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_sends_dates_payload_when_configured(self) -> None:
        captured: dict[str, Any] = {}

        def fake_request(
            method: str,
            url: str,
            headers: dict[str, str],
            body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json.loads(body.decode("utf-8")) if body else None
            return HttpResponse(
                status=202,
                body=json.dumps({"status": "accepted", "run_id": "abc123", "coalesced": False}),
            )

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="secret",
            dates=("2026-11-19", "2026-11-20"),
        )
        start_scrape_all(cfg, request_fn=fake_request)

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "https://scraper.example.com/scrape-all?background=true")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(
            captured["body"],
            {"background": True, "dates": ["2026-11-19", "2026-11-20"]},
        )

    def test_poll_raises_on_failed_job_status(self) -> None:
        clock = FakeClock()
        responses = [
            HttpResponse(status=200, body=json.dumps({"run_id": "r1", "status": "running"})),
            HttpResponse(
                status=200,
                body=json.dumps({"run_id": "r1", "status": "failed", "error": "Upstream 502"}),
            ),
        ]

        def fake_request(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return responses.pop(0)

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="secret",
            poll_timeout_seconds=60.0,
            poll_interval_seconds=2.0,
        )
        with self.assertRaises(ScrapeTriggerError) as ctx:
            poll_scrape_all_status(
                cfg,
                "r1",
                "/scrape-all/r1",
                request_fn=fake_request,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )

        self.assertIn("Upstream 502", str(ctx.exception))

    def test_poll_raises_on_turso_fallback_error_message(self) -> None:
        clock = FakeClock()

        def fake_request(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return HttpResponse(
                status=200,
                body=json.dumps(
                    {
                        "run_id": "r-db",
                        "status": "failed",
                        "error_message": "Turso sync timed out",
                    }
                ),
            )

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="secret",
        )
        with self.assertRaises(ScrapeTriggerError) as ctx:
            poll_scrape_all_status(
                cfg,
                "r-db",
                request_fn=fake_request,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )

        self.assertIn("Turso sync timed out", str(ctx.exception))

    def test_poll_raises_when_db_configured_but_not_saved(self) -> None:
        clock = FakeClock()

        def fake_request(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return HttpResponse(
                status=200,
                body=json.dumps(
                    {
                        "run_id": "r2",
                        "status": "completed",
                        "theaters_count": 79,
                        "db_configured": True,
                        "db_saved": False,
                    }
                ),
            )

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="secret",
        )
        with self.assertRaises(ScrapeTriggerError) as ctx:
            poll_scrape_all_status(
                cfg,
                "r2",
                request_fn=fake_request,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )

        self.assertIn("Turso", str(ctx.exception))

    def test_poll_raises_when_completed_with_zero_theaters(self) -> None:
        clock = FakeClock()

        def fake_request(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return HttpResponse(
                status=200,
                body=json.dumps(
                    {
                        "run_id": "r3",
                        "status": "completed",
                        "theaters_count": 0,
                        "db_configured": True,
                        "db_saved": True,
                    }
                ),
            )

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="secret",
        )
        with self.assertRaises(ScrapeTriggerError) as ctx:
            poll_scrape_all_status(
                cfg,
                "r3",
                request_fn=fake_request,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )

        self.assertIn("0 theaters", str(ctx.exception))

    def test_poll_recovers_after_transient_503_then_completes(self) -> None:
        clock = FakeClock()
        responses = [
            HttpResponse(status=503, body="Temporary hiccup"),
            HttpResponse(
                status=200,
                body=json.dumps(
                    {
                        "run_id": "r4",
                        "status": "completed",
                        "theaters_count": 79,
                        "showtimes_count": 1480,
                        "db_configured": True,
                        "db_saved": True,
                    }
                ),
            ),
        ]

        def fake_request(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return responses.pop(0)

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="secret",
            poll_timeout_seconds=60.0,
            poll_interval_seconds=2.0,
            max_poll_transient_errors=2,
        )
        result = poll_scrape_all_status(
            cfg,
            "r4",
            request_fn=fake_request,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )

        self.assertEqual(result["showtimes_count"], 1480)

    def test_poll_aborts_when_consecutive_transient_errors_exceed_limit(self) -> None:
        clock = FakeClock()

        def always_502(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return HttpResponse(status=502, body="Bad Gateway")

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="secret",
            poll_timeout_seconds=60.0,
            poll_interval_seconds=1.0,
            max_poll_transient_errors=2,
        )
        with self.assertRaises(ScrapeTriggerError) as ctx:
            poll_scrape_all_status(
                cfg,
                "r5",
                request_fn=always_502,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )

        self.assertIn("3 consecutive transient errors", str(ctx.exception))

    def test_poll_raises_immediately_on_404(self) -> None:
        clock = FakeClock()

        def not_found(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return HttpResponse(status=404, body=json.dumps({"detail": "Run not found"}))

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="secret",
            poll_timeout_seconds=60.0,
        )
        with self.assertRaises(ScrapeTriggerError) as ctx:
            poll_scrape_all_status(
                cfg,
                "missing-id",
                request_fn=not_found,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )

        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertEqual(clock.sleeps, [])

    def test_poll_raises_when_timeout_exceeded(self) -> None:
        clock = FakeClock()

        def always_running(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return HttpResponse(status=200, body=json.dumps({"run_id": "r6", "status": "running"}))

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="secret",
            poll_timeout_seconds=10.0,
            poll_interval_seconds=4.0,
        )
        with self.assertRaises(ScrapeTriggerError) as ctx:
            poll_scrape_all_status(
                cfg,
                "r6",
                request_fn=always_running,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )

        self.assertIn("Timed out polling", str(ctx.exception))

    def test_run_scrape_workflow_handles_coalesced_job(self) -> None:
        clock = FakeClock()
        responses = [
            HttpResponse(status=200, body=json.dumps({"status": "ok", "turso_configured": True})),
            HttpResponse(
                status=202,
                body=json.dumps(
                    {
                        "status": "accepted",
                        "run_id": "existing-run",
                        "coalesced": True,
                        "status_url": "/scrape-all/existing-run",
                    }
                ),
            ),
            HttpResponse(
                status=200,
                body=json.dumps(
                    {
                        "run_id": "existing-run",
                        "status": "completed",
                        "theaters_count": 79,
                        "showtimes_count": 900,
                    }
                ),
            ),
        ]

        def fake_request(
            _method: str,
            _url: str,
            _headers: dict[str, str],
            _body: bytes | None,
            _timeout: float,
        ) -> HttpResponse:
            return responses.pop(0)

        cfg = TriggerConfig(
            base_url="https://scraper.example.com",
            api_key="secret",
        )
        result = run_scrape_workflow(
            cfg,
            request_fn=fake_request,
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )

        self.assertEqual(result["run_id"], "existing-run")
        self.assertEqual(result["showtimes_count"], 900)


class LocalHttpIntegrationTests(unittest.TestCase):
    """End-to-end tests using real urllib HTTP requests against a local ThreadingHTTPServer."""

    def _start_mock_server(
        self, state: dict[str, Any]
    ) -> tuple[ThreadingHTTPServer, str, threading.Thread]:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _write_json(self, code: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    state["health_calls"] += 1
                    if state["health_calls"] <= state.get("health_503s", 0):
                        self._write_json(503, {"detail": "starting"})
                        return
                    self._write_json(200, {"status": "ok", "turso_configured": True})
                    return

                if self.path == "/scrape-all/run-xyz":
                    if self.headers.get("Authorization") != f"Bearer {state['api_key']}":
                        self._write_json(401, {"detail": "Invalid or missing API key."})
                        return
                    state["poll_calls"] += 1
                    if state["poll_calls"] < state.get("polls_before_terminal", 2):
                        self._write_json(200, {"run_id": "run-xyz", "status": "running"})
                        return
                    self._write_json(200, state["terminal_payload"])
                    return

                self._write_json(404, {"detail": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/scrape-all?background=true":
                    self._write_json(404, {"detail": "not found"})
                    return
                if self.headers.get("Authorization") != f"Bearer {state['api_key']}":
                    self._write_json(401, {"detail": "Invalid or missing API key."})
                    return
                state["post_calls"] += 1
                self._write_json(
                    202,
                    {
                        "status": "accepted",
                        "run_id": "run-xyz",
                        "job_status": "running",
                        "coalesced": False,
                        "db_configured": True,
                        "status_url": "/scrape-all/run-xyz",
                    },
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{port}", thread

    def test_full_end_to_end_success_writes_github_summary(self) -> None:
        state: dict[str, Any] = {
            "api_key": "local-test-secret",
            "health_calls": 0,
            "health_503s": 1,
            "post_calls": 0,
            "poll_calls": 0,
            "polls_before_terminal": 2,
            "terminal_payload": {
                "run_id": "run-xyz",
                "status": "completed",
                "theaters_count": 79,
                "sites_scraped": 79,
                "sites_with_showtimes": 74,
                "dates_count": 10,
                "showtimes_count": 1200,
                "failed_site_ids": ["9999"],
                "db_configured": True,
                "db_saved": True,
            },
        }
        server, base_url, thread = self._start_mock_server(state)
        try:
            with tempfile.NamedTemporaryFile("r+", encoding="utf-8") as summary_file:
                env = {
                    "RENDER_API_URL": base_url,
                    "API_KEY": "local-test-secret",
                    "HEALTH_INTERVAL_SECONDS": "0.05",
                    "POLL_INTERVAL_SECONDS": "0.05",
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_STEP_SUMMARY": summary_file.name,
                }
                stdout_buf = io.StringIO()
                with redirect_stdout(stdout_buf):
                    exit_code = main([], env=env)

                summary_file.seek(0)
                summary_text = summary_file.read()

            self.assertEqual(exit_code, 0)
            self.assertEqual(state["health_calls"], 2)
            self.assertEqual(state["post_calls"], 1)
            self.assertEqual(state["poll_calls"], 2)
            self.assertIn("::warning::Partial scrape failures for site IDs: 9999", stdout_buf.getvalue())
            self.assertIn("| Showtimes Count | `1200` |", summary_text)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_full_end_to_end_returns_exit_code_1_when_job_fails(self) -> None:
        state: dict[str, Any] = {
            "api_key": "local-test-secret",
            "health_calls": 0,
            "health_503s": 0,
            "post_calls": 0,
            "poll_calls": 0,
            "polls_before_terminal": 1,
            "terminal_payload": {
                "run_id": "run-xyz",
                "status": "failed",
                "error": "Upstream 502 Bad Gateway",
            },
        }
        server, base_url, thread = self._start_mock_server(state)
        try:
            env = {
                "RENDER_API_URL": base_url,
                "API_KEY": "local-test-secret",
                "POLL_INTERVAL_SECONDS": "0.05",
            }
            stderr_buf = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr_buf):
                exit_code = main([], env=env)

            self.assertEqual(exit_code, 1)
            self.assertIn("Upstream 502 Bad Gateway", stderr_buf.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
