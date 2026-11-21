#!/usr/bin/env python3
"""Wake the hosted Render scraper service, run scrape-all, and verify completion."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping

DEFAULT_HEALTH_TIMEOUT_SECONDS = 180.0
DEFAULT_HEALTH_INTERVAL_SECONDS = 5.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_TRIGGER_RETRIES = 3
DEFAULT_TRIGGER_RETRY_DELAY_SECONDS = 5.0
DEFAULT_POLL_TIMEOUT_SECONDS = 300.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_POLL_TRANSIENT_ERRORS = 5
DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_WORKFLOW_FILE = "scrape-all.yml"
MANILA_TZ = timezone(timedelta(hours=8))

TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
TERMINAL_SUCCESS_STATUS = "completed"
TERMINAL_FAILURE_STATUS = "failed"
RUNNING_STATUSES = frozenset({"running", "accepted", "pending"})
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USER_AGENT = "impala-scrape-all-trigger/1.0"


class ScrapeTriggerError(RuntimeError):
    """Raised when waking, triggering, or polling the scrape-all job fails."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Minimal HTTP response representation returned by the transport."""

    status: int
    body: str


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    """Validated runtime settings for invoking POST /scrape-all on Render."""

    base_url: str
    api_key: str
    dates: tuple[str, ...] = ()
    health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS
    health_interval_seconds: float = DEFAULT_HEALTH_INTERVAL_SECONDS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    trigger_retries: int = DEFAULT_TRIGGER_RETRIES
    trigger_retry_delay_seconds: float = DEFAULT_TRIGGER_RETRY_DELAY_SECONDS
    poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_poll_transient_errors: int = DEFAULT_MAX_POLL_TRANSIENT_ERRORS
    allow_unauthenticated: bool = False

    def __post_init__(self) -> None:
        normalized_url = self.base_url.strip().rstrip("/")
        if not normalized_url:
            raise ScrapeTriggerError(
                "Missing RENDER_API_URL. Configure the RENDER_API_URL secret in GitHub Actions or pass --base-url."
            )
        if not normalized_url.startswith(("http://", "https://")):
            raise ScrapeTriggerError(
                "RENDER_API_URL must start with http:// or https://."
            )
        object.__setattr__(self, "base_url", normalized_url)

        if not self.allow_unauthenticated and not self.api_key.strip():
            raise ScrapeTriggerError(
                "Missing API_KEY. Configure the API_KEY secret in GitHub Actions or pass --allow-no-auth."
            )

        if self.trigger_retries < 1:
            raise ScrapeTriggerError("trigger_retries must be at least 1.")
        if self.health_timeout_seconds <= 0 or self.poll_timeout_seconds <= 0:
            raise ScrapeTriggerError("Timeouts must be positive numbers.")
        if self.request_timeout_seconds <= 0:
            raise ScrapeTriggerError("request_timeout_seconds must be positive.")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TriggerConfig":
        """Build configuration from environment variables with safe defaults."""
        source = os.environ if env is None else env
        raw_url = source.get("RENDER_API_URL", "").strip()
        raw_key = source.get("API_KEY", "").strip()
        raw_dates = source.get("SCRAPE_DATES", "").strip()
        allow_no_auth = source.get("ALLOW_UNAUTHENTICATED", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        return cls(
            base_url=raw_url,
            api_key=raw_key,
            dates=parse_dates(raw_dates),
            health_timeout_seconds=_parse_float(
                source.get("HEALTH_TIMEOUT_SECONDS"), DEFAULT_HEALTH_TIMEOUT_SECONDS
            ),
            health_interval_seconds=_parse_float(
                source.get("HEALTH_INTERVAL_SECONDS"), DEFAULT_HEALTH_INTERVAL_SECONDS
            ),
            request_timeout_seconds=_parse_float(
                source.get("REQUEST_TIMEOUT_SECONDS"), DEFAULT_REQUEST_TIMEOUT_SECONDS
            ),
            trigger_retries=_parse_int(
                source.get("TRIGGER_RETRIES"), DEFAULT_TRIGGER_RETRIES
            ),
            poll_timeout_seconds=_parse_float(
                source.get("POLL_TIMEOUT_SECONDS"), DEFAULT_POLL_TIMEOUT_SECONDS
            ),
            poll_interval_seconds=_parse_float(
                source.get("POLL_INTERVAL_SECONDS"), DEFAULT_POLL_INTERVAL_SECONDS
            ),
            max_poll_transient_errors=_parse_int(
                source.get("MAX_POLL_TRANSIENT_ERRORS"), DEFAULT_MAX_POLL_TRANSIENT_ERRORS
            ),
            allow_unauthenticated=allow_no_auth,
        )


HttpRequestFn = Callable[[str, str, dict[str, str], bytes | None, float], HttpResponse]


def _parse_float(raw: str | None, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ScrapeTriggerError(f"Invalid numeric setting: {raw!r}") from exc


def _parse_int(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ScrapeTriggerError(f"Invalid integer setting: {raw!r}") from exc


def parse_dates(raw_dates: str) -> tuple[str, ...]:
    """Parse and validate comma-separated YYYY-MM-DD business date filters."""
    if not raw_dates or not raw_dates.strip():
        return ()
    parsed: list[str] = []
    for item in raw_dates.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        if not DATE_PATTERN.match(cleaned):
            raise ScrapeTriggerError(
                f"Invalid business date {cleaned!r}; expected YYYY-MM-DD format."
            )
        try:
            date.fromisoformat(cleaned)
        except ValueError as exc:
            raise ScrapeTriggerError(
                f"Invalid business date {cleaned!r}; not a valid calendar date."
            ) from exc
        parsed.append(cleaned)
    return tuple(parsed)


def default_http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    """Execute an HTTP request using Python stdlib urllib without raising on HTTP error codes."""
    request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return HttpResponse(status=response.status, body=raw)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            return HttpResponse(status=exc.code, body=raw)
        finally:
            exc.close()


def _decode_json_object(body: str, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        preview = body[:300].strip()
        raise ScrapeTriggerError(f"{context} returned invalid JSON: {preview!r}") from exc
    if not isinstance(payload, dict):
        raise ScrapeTriggerError(f"{context} returned non-object JSON payload.")
    return payload


def _auth_headers(config: TriggerConfig, *, has_json_body: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    if has_json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _redact_sensitive(text: str, config: TriggerConfig | None = None) -> str:
    if config is None:
        return text
    redacted = text
    candidates = [config.api_key, config.base_url]
    hostname = urllib.parse.urlparse(config.base_url).hostname or ""
    if hostname and hostname not in {"localhost", "127.0.0.1"}:
        candidates.append(hostname)
    for secret in candidates:
        if secret:
            redacted = redacted.replace(secret, "***")
    return redacted


def _format_error_detail(response: HttpResponse, config: TriggerConfig | None = None) -> str:
    try:
        payload = json.loads(response.body)
        if isinstance(payload, dict) and "detail" in payload:
            return _redact_sensitive(str(payload["detail"]), config)
    except json.JSONDecodeError:
        pass
    preview = response.body[:300].strip() or "<empty body>"
    return _redact_sensitive(preview, config)


def _resolve_status_url(base_url: str, run_id: str, status_url: str | None) -> str:
    path = (status_url or f"/scrape-all/{run_id}").strip()
    if path.startswith(("http://", "https://")):
        return path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base_url}{path}"


def wait_for_service_health(
    config: TriggerConfig,
    *,
    request_fn: HttpRequestFn = default_http_request,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll GET /health until Render finishes booting or the wake budget expires."""
    url = f"{config.base_url}/health"
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    deadline = monotonic_fn() + config.health_timeout_seconds
    attempt = 0
    last_error = "no attempts completed"

    while True:
        attempt += 1
        try:
            response = request_fn("GET", url, headers, None, config.request_timeout_seconds)
            if response.status == 200:
                payload = _decode_json_object(response.body, "GET /health")
                if payload.get("status") == "ok":
                    print(
                        f"Service ready via GET /health (attempt {attempt}, "
                        f"turso_configured={payload.get('turso_configured')})."
                    )
                    return payload
                last_error = f"unexpected health status={payload.get('status')!r}"
            elif response.status in TRANSIENT_HTTP_STATUSES:
                last_error = f"HTTP {response.status}: {_format_error_detail(response, config)}"
            else:
                raise ScrapeTriggerError(
                    f"GET /health failed with HTTP {response.status}: {_format_error_detail(response, config)}"
                )
        except ScrapeTriggerError as exc:
            # Non-transient HTTP errors fail immediately; invalid JSON during proxy boot retries.
            if "GET /health failed with HTTP" in str(exc):
                raise
            last_error = _redact_sensitive(str(exc), config)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = _redact_sensitive(f"{type(exc).__name__}: {exc}", config)

        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            raise ScrapeTriggerError(
                f"Timed out waiting for GET /health after {config.health_timeout_seconds:.0f}s "
                f"(last error: {last_error})."
            )

        wait_seconds = min(config.health_interval_seconds, remaining)
        print(
            f"Waiting for Render wake-up (attempt {attempt}, retry in {wait_seconds:.1f}s): "
            f"{last_error}"
        )
        sleep_fn(wait_seconds)


def start_scrape_all(
    config: TriggerConfig,
    *,
    request_fn: HttpRequestFn = default_http_request,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Trigger POST /scrape-all?background=true with transient error retries."""
    url = f"{config.base_url}/scrape-all?background=true"
    body: bytes | None = None
    if config.dates:
        body = json.dumps({"background": True, "dates": list(config.dates)}).encode("utf-8")
    headers = _auth_headers(config, has_json_body=body is not None)

    last_error = "unknown failure"
    for attempt in range(1, config.trigger_retries + 1):
        try:
            response = request_fn("POST", url, headers, body, config.request_timeout_seconds)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = _redact_sensitive(f"{type(exc).__name__}: {exc}", config)
        else:
            if response.status in (200, 202):
                payload = _decode_json_object(response.body, "POST /scrape-all")
                if response.status == 202 and not payload.get("run_id"):
                    raise ScrapeTriggerError(
                        "POST /scrape-all returned 202 Accepted without run_id."
                    )
                print(
                    f"Triggered POST /scrape-all?background=true -> HTTP {response.status} "
                    f"(run_id={payload.get('run_id')}, coalesced={payload.get('coalesced')})."
                )
                return payload

            detail = _format_error_detail(response, config)
            if response.status not in TRANSIENT_HTTP_STATUSES:
                raise ScrapeTriggerError(
                    f"POST /scrape-all failed with HTTP {response.status}: {detail}"
                )
            last_error = f"HTTP {response.status}: {detail}"

        if attempt < config.trigger_retries:
            print(
                f"Transient POST /scrape-all failure (attempt {attempt}/{config.trigger_retries}): "
                f"{last_error}. Retrying in {config.trigger_retry_delay_seconds:.1f}s..."
            )
            sleep_fn(config.trigger_retry_delay_seconds)

    raise ScrapeTriggerError(
        f"POST /scrape-all failed after {config.trigger_retries} attempts: {last_error}"
    )


def _validate_completed_payload(payload: dict[str, Any]) -> None:
    if "db_configured" in payload and payload.get("db_configured") and payload.get("db_saved") is False:
        raise ScrapeTriggerError(
            f"Scrape run {payload.get('run_id')} completed scrape but failed to persist to Turso."
        )
    if payload.get("theaters_count") == 0:
        raise ScrapeTriggerError(
            f"Scrape run {payload.get('run_id')} completed with 0 theaters."
        )


def poll_scrape_all_status(
    config: TriggerConfig,
    run_id: str,
    status_url: str | None = None,
    *,
    request_fn: HttpRequestFn = default_http_request,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll GET /scrape-all/{run_id} until the job completes or fails."""
    url = _resolve_status_url(config.base_url, run_id, status_url)
    headers = _auth_headers(config)
    deadline = monotonic_fn() + config.poll_timeout_seconds
    consecutive_transient_errors = 0
    poll_count = 0

    while True:
        poll_count += 1
        transient_error: str | None = None
        try:
            response = request_fn("GET", url, headers, None, config.request_timeout_seconds)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            transient_error = _redact_sensitive(f"{type(exc).__name__}: {exc}", config)
        else:
            if response.status == 200:
                consecutive_transient_errors = 0
                payload = _decode_json_object(response.body, f"GET /scrape-all/{run_id}")
                job_status = str(payload.get("status", "")).lower()

                if job_status == TERMINAL_SUCCESS_STATUS:
                    _validate_completed_payload(payload)
                    return payload
                if job_status == TERMINAL_FAILURE_STATUS:
                    reason = _redact_sensitive(
                        str(
                            payload.get("error")
                            or payload.get("error_message")
                            or "unknown upstream error"
                        ),
                        config,
                    )
                    raise ScrapeTriggerError(f"Scrape run {run_id} failed: {reason}")
                if job_status not in RUNNING_STATUSES:
                    raise ScrapeTriggerError(
                        f"Scrape run {run_id} returned unexpected status {job_status!r}."
                    )

                print(
                    f"Polling run {run_id} (check {poll_count}): status={job_status}..."
                )
            elif response.status in TRANSIENT_HTTP_STATUSES:
                transient_error = (
                    f"HTTP {response.status}: {_format_error_detail(response, config)}"
                )
            else:
                raise ScrapeTriggerError(
                    f"GET /scrape-all/{run_id} failed with HTTP {response.status}: "
                    f"{_format_error_detail(response, config)}"
                )

        if transient_error is not None:
            consecutive_transient_errors += 1
            if consecutive_transient_errors > config.max_poll_transient_errors:
                raise ScrapeTriggerError(
                    f"Polling run {run_id} aborted after {consecutive_transient_errors} "
                    f"consecutive transient errors (last: {transient_error})."
                )
            print(
                f"Transient poll error ({consecutive_transient_errors}/"
                f"{config.max_poll_transient_errors}): {transient_error}"
            )

        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            raise ScrapeTriggerError(
                f"Timed out polling scrape run {run_id} after {config.poll_timeout_seconds:.0f}s."
            )

        sleep_fn(min(config.poll_interval_seconds, remaining))


def run_scrape_workflow(
    config: TriggerConfig,
    *,
    request_fn: HttpRequestFn = default_http_request,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Wake Render, start scrape-all in background mode, and await terminal status."""
    wait_for_service_health(
        config,
        request_fn=request_fn,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )
    accepted = start_scrape_all(config, request_fn=request_fn, sleep_fn=sleep_fn)

    if str(accepted.get("status", "")).lower() == TERMINAL_SUCCESS_STATUS:
        _validate_completed_payload(accepted)
        return accepted

    run_id = str(accepted["run_id"])
    return poll_scrape_all_status(
        config,
        run_id,
        accepted.get("status_url"),
        request_fn=request_fn,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )


def _parse_github_timestamp(raw_timestamp: str) -> datetime | None:
    cleaned = raw_timestamp.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def find_successful_scheduled_run_today(
    env: Mapping[str, str],
    *,
    request_fn: HttpRequestFn = default_http_request,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any] | None:
    """Return an earlier successful scheduled workflow run from today (Asia/Manila), if any.

    Used when multiple staggered backup cron slots are configured in GitHub Actions so
    later slots exit cleanly once the daily scrape has already succeeded. Fails open
    (returns None) if GitHub API metadata cannot be fetched.
    """
    if env.get("GITHUB_EVENT_NAME", "").strip() != "schedule":
        return None
    if env.get("SCRAPE_DATES", "").strip():
        return None

    repository = env.get("GITHUB_REPOSITORY", "").strip()
    if not repository or "/" not in repository:
        return None

    api_url = env.get("GITHUB_API_URL", DEFAULT_GITHUB_API_URL).strip().rstrip("/")
    workflow_file = env.get("GITHUB_WORKFLOW_FILE", DEFAULT_WORKFLOW_FILE).strip() or DEFAULT_WORKFLOW_FILE
    current_run_id = env.get("GITHUB_RUN_ID", "").strip()
    github_token = env.get("GITHUB_TOKEN", "").strip()
    timeout = _parse_float(env.get("REQUEST_TIMEOUT_SECONDS"), DEFAULT_REQUEST_TIMEOUT_SECONDS)

    owner, repo = repository.split("/", 1)
    runs_url = (
        f"{api_url}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
        f"/actions/workflows/{urllib.parse.quote(workflow_file)}/runs"
        "?event=schedule&status=completed&per_page=20"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        response = request_fn("GET", runs_url, headers, None, timeout)
        if response.status != 200:
            print(
                f"Warning: GitHub runs check returned HTTP {response.status}; proceeding with scrape.",
                file=sys.stderr,
            )
            return None
        payload = _decode_json_object(response.body, "GET GitHub workflow runs")
    except (ScrapeTriggerError, urllib.error.URLError, TimeoutError, OSError) as exc:
        print(
            f"Warning: Unable to check prior scheduled runs ({exc}); proceeding with scrape.",
            file=sys.stderr,
        )
        return None

    today_manila = now_fn().astimezone(MANILA_TZ).date()
    workflow_runs = payload.get("workflow_runs")
    if not isinstance(workflow_runs, list):
        return None

    for run in workflow_runs:
        if not isinstance(run, dict):
            continue
        run_id = str(run.get("id", "")).strip()
        if current_run_id and run_id == current_run_id:
            continue
        if str(run.get("event", "")).lower() != "schedule":
            continue
        if str(run.get("conclusion", "")).lower() != "success":
            continue
        created_at = _parse_github_timestamp(str(run.get("created_at", "")))
        if created_at is None:
            continue
        if created_at.astimezone(MANILA_TZ).date() == today_manila:
            return run

    return None


def report_skipped_schedule(
    prior_run: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> None:
    """Log and write a step summary when a backup cron slot skips an already-completed daily scrape."""
    source = os.environ if env is None else env
    prior_id = prior_run.get("id", "unknown")
    prior_created = prior_run.get("created_at", "unknown")
    print(
        f"Daily scheduled scrape already succeeded today in Asia/Manila "
        f"(workflow run {prior_id} at {prior_created}); skipping backup cron slot."
    )
    summary_path = source.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        lines = [
            "## Daily `scrape-all` Run Summary (Backup Slot Skipped)",
            "",
            f"Skipped backup schedule slot because a scheduled scrape already succeeded today (`run_id={prior_id}`, created at `{prior_created}`).",
            "",
        ]
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))


def format_summary_markdown(result: Mapping[str, Any]) -> str:
    """Format a completed scrape-all payload as a GitHub Actions Markdown summary."""
    failed_sites = result.get("failed_site_ids") or []
    failed_text = ", ".join(str(site_id) for site_id in failed_sites) if failed_sites else "None"
    db_saved = result.get("db_saved")
    db_saved_text = "Yes" if db_saved is True else ("No" if db_saved is False else "N/A (Turso fallback)")

    lines = [
        "## Daily `scrape-all` Run Summary",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Run ID | `{result.get('run_id', 'unknown')}` |",
        f"| Status | `{result.get('status', 'unknown')}` |",
        f"| Theaters | `{result.get('theaters_count', 'N/A')}` |",
        f"| Sites Scraped | `{result.get('sites_scraped', 'N/A')}` |",
        f"| Sites With Showtimes | `{result.get('sites_with_showtimes', 'N/A')}` |",
        f"| Dates Count | `{result.get('dates_count', 'N/A')}` |",
        f"| Showtimes Count | `{result.get('showtimes_count', 'N/A')}` |",
        f"| DB Saved | `{db_saved_text}` |",
        f"| Failed Site IDs | `{failed_text}` |",
        "",
    ]
    return "\n".join(lines)


def report_completion(
    result: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> None:
    """Print run results, emit partial-failure warning if needed, and write step summary."""
    source = os.environ if env is None else env
    print("Scrape completed successfully:")
    print(json.dumps(result, indent=2, sort_keys=True))

    failed_sites = result.get("failed_site_ids") or []
    if failed_sites:
        site_list = ", ".join(str(site_id) for site_id in failed_sites)
        if source.get("GITHUB_ACTIONS", "").lower() == "true":
            print(f"::warning::Partial scrape failures for site IDs: {site_list}")
        else:
            print(f"Warning: Partial scrape failures for site IDs: {site_list}", file=sys.stderr)

    summary_path = source.get("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(format_summary_markdown(result))


def build_arg_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser for manual or CI invocation."""
    parser = argparse.ArgumentParser(
        description="Trigger POST /scrape-all on Render and poll until completion."
    )
    parser.add_argument("--base-url", help="Scraper base URL (default: RENDER_API_URL env var)")
    parser.add_argument("--api-key", help="Bearer API key (default: API_KEY env var)")
    parser.add_argument(
        "--dates",
        help="Optional comma-separated YYYY-MM-DD dates (default: SCRAPE_DATES env var)",
    )
    parser.add_argument(
        "--allow-no-auth",
        action="store_true",
        help="Allow running without API_KEY (for unauthenticated local testing).",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    request_fn: HttpRequestFn = default_http_request,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    """CLI entrypoint returning 0 on success and 1 on error."""
    args = build_arg_parser().parse_args(argv)
    merged_env = dict(os.environ if env is None else env)
    if args.base_url:
        merged_env["RENDER_API_URL"] = args.base_url
    if args.api_key is not None:
        merged_env["API_KEY"] = args.api_key
    if args.dates is not None:
        merged_env["SCRAPE_DATES"] = args.dates
    if args.allow_no_auth:
        merged_env["ALLOW_UNAUTHENTICATED"] = "true"

    try:
        prior_run = find_successful_scheduled_run_today(
            merged_env,
            request_fn=request_fn,
            now_fn=now_fn,
        )
        if prior_run is not None:
            report_skipped_schedule(prior_run, merged_env)
            return 0

        config = TriggerConfig.from_env(merged_env)
        result = run_scrape_workflow(
            config,
            request_fn=request_fn,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
        report_completion(result, merged_env)
    except ScrapeTriggerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
