#!/usr/bin/env python3
"""Offline verification tests for GitHub Actions workflows in this repository."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
SCRAPE_WORKFLOW = WORKFLOWS_DIR / "scrape.yml"
KEEP_ALIVE_WORKFLOW = WORKFLOWS_DIR / "keep-alive.yml"
CI_WORKFLOW = WORKFLOWS_DIR / "ci.yml"


def extract_step_run_script(workflow_text: str, step_name: str) -> str:
    """Extract the dedented multi-line `run: |` script for a named workflow step."""
    lines = workflow_text.splitlines()
    step_marker = f"- name: {step_name}"

    step_index = next(
        (idx for idx, line in enumerate(lines) if line.strip() == step_marker),
        None,
    )
    if step_index is None:
        raise AssertionError(f"Step {step_name!r} not found in workflow.")

    step_indent = len(lines[step_index]) - len(lines[step_index].lstrip(" "))
    run_index: int | None = None
    run_indent = 0
    for idx in range(step_index + 1, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= step_indent and stripped.startswith("- "):
            break
        if stripped == "run: |":
            run_index = idx
            run_indent = indent
            break

    if run_index is None:
        raise AssertionError(f"Multi-line `run: |` block not found for step {step_name!r}.")

    body_lines: list[str] = []
    for idx in range(run_index + 1, len(lines)):
        line = lines[idx]
        if not line.strip():
            body_lines.append("")
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= run_indent:
            break
        body_lines.append(line)

    return textwrap.dedent("\n".join(body_lines)).strip() + "\n"


def run_bash(
    script: str,
    *,
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Execute a bash script with an isolated environment."""
    merged_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(cwd)),
        **env,
    }
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        text=True,
        check=False,
    )


def create_fake_uv(bin_dir: Path, log_file: Path) -> None:
    """Install a fake `uv` executable that appends its invoked argv as JSON lines."""
    uv_path = bin_dir / "uv"
    uv_path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import sys
            from pathlib import Path

            with Path({str(log_file)!r}).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(sys.argv[1:]) + "\\n")
            """
        ),
        encoding="utf-8",
    )
    uv_path.chmod(uv_path.stat().st_mode | stat.S_IXUSR)


def create_local_git_remote(base_dir: Path, owner: str, repo: str) -> tuple[Path, str]:
    """Create a local bare git repo laid out as `<base_dir>/<owner>/<repo>.git`."""
    bare_repo = base_dir / owner / f"{repo}.git"
    bare_repo.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(bare_repo)],
        check=True,
        capture_output=True,
    )

    work_dir = base_dir / "seed-work"
    work_dir.mkdir(parents=True, exist_ok=True)
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test Bot",
        "GIT_AUTHOR_EMAIL": "bot@example.com",
        "GIT_COMMITTER_NAME": "Test Bot",
        "GIT_COMMITTER_EMAIL": "bot@example.com",
    }
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work_dir)], check=True, env=git_env)
    (work_dir / "pyproject.toml").write_text('[project]\nname = "scholarship-scraper"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(work_dir), "add", "pyproject.toml"], check=True, env=git_env)
    subprocess.run(["git", "-C", str(work_dir), "commit", "--quiet", "-m", "initial"], check=True, env=git_env)
    subprocess.run(["git", "-C", str(work_dir), "tag", "v0.1.0"], check=True, env=git_env)
    subprocess.run(["git", "-C", str(work_dir), "remote", "add", "origin", str(bare_repo)], check=True, env=git_env)
    subprocess.run(["git", "-C", str(work_dir), "push", "--quiet", "origin", "main", "v0.1.0"], check=True, env=git_env)
    commit_sha = subprocess.run(
        ["git", "-C", str(work_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return bare_repo, commit_sha


class WorkflowStructureTests(unittest.TestCase):
    """Verify workflow layout, schedule, inputs, and removal of legacy Render artifacts."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scrape_text = SCRAPE_WORKFLOW.read_text(encoding="utf-8")
        cls.keep_alive_text = KEEP_ALIVE_WORKFLOW.read_text(encoding="utf-8")
        cls.ci_text = CI_WORKFLOW.read_text(encoding="utf-8")

    def test_scrape_workflow_exists_and_legacy_scrape_all_removed(self) -> None:
        self.assertTrue(SCRAPE_WORKFLOW.exists() and not (WORKFLOWS_DIR / "scrape-all.yml").exists())

    def test_scrape_workflow_keeps_preshifted_daily_cron(self) -> None:
        self.assertIn("- cron: '7 18 * * *'", self.scrape_text)

    def test_scrape_workflow_uses_non_cancelling_concurrency_group(self) -> None:
        self.assertIn("group: scholarship-collection\n  cancel-in-progress: false", self.scrape_text)

    def test_scrape_workflow_requests_least_privilege_read_permissions(self) -> None:
        self.assertIn("permissions:\n  contents: read", self.scrape_text)

    def test_scrape_workflow_exposes_all_documented_dispatch_inputs(self) -> None:
        expected_inputs = (
            "mode:",
            "extractor:",
            "target_url:",
            "sources:",
            "max_pages:",
            "max_depth:",
            "force_browser:",
            "no_skip_unchanged:",
            "browser_engine:",
            "fail_on_error:",
            "scraper_ref:",
        )
        self.assertTrue(all(token in self.scrape_text for token in expected_inputs))

    def test_scrape_workflow_wires_dynamic_llm_and_crawl_env_vars(self) -> None:
        expected_env_vars = (
            "SCRAPER_EXTRACTOR_MODE:",
            "GHOST_BASE_URL:",
            "SCRAPER_LLM_MODEL:",
            "SCRAPER_LLM_DELAY_SECONDS:",
            "SCRAPER_LLM_TIMEOUT_SECONDS:",
            "SCRAPER_LLM_MAX_CHARS:",
            "SCRAPER_MAX_PAGES_PER_SOURCE:",
            "SCRAPER_MAX_CRAWL_DEPTH:",
        )
        self.assertTrue(all(token in self.scrape_text for token in expected_env_vars))

    def test_scrape_workflow_uses_extended_timeout_for_dynamic_scraping(self) -> None:
        self.assertIn("timeout-minutes: 45", self.scrape_text)

    def test_workflows_do_not_interpolate_untrusted_inputs_into_run_scripts(self) -> None:
        unsafe_pattern = re.compile(r"^\s*[^#\n]*\$\{\{\s*inputs\.")
        violations: list[str] = []
        for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
            in_run_block = False
            run_indent = 0
            for line in workflow_path.read_text(encoding="utf-8").splitlines():
                stripped = line.lstrip()
                indent = len(line) - len(stripped)
                if in_run_block:
                    if stripped and indent <= run_indent:
                        in_run_block = False
                    elif unsafe_pattern.search(line):
                        violations.append(f"{workflow_path.name}: {line.strip()}")
                if stripped.startswith("run:"):
                    run_body = stripped.removeprefix("run:").strip()
                    if run_body in {"|", ">", "|-", ">-"}:
                        in_run_block = True
                        run_indent = indent
                    elif unsafe_pattern.search(run_body):
                        violations.append(f"{workflow_path.name}: {line.strip()}")
        self.assertEqual(violations, [])

    def test_scrape_workflow_defaults_to_kebbbnnn_scholarship_scraper(self) -> None:
        self.assertIn("vars.SCRAPER_REPO || 'kebbbnnn/scholarship_scraper'", self.scrape_text)

    def test_keep_alive_workflow_is_preserved(self) -> None:
        self.assertIn("chore: keep-alive heartbeat [skip ci]", self.keep_alive_text)

    def test_ci_workflow_runs_both_workflow_and_cloudflare_tests(self) -> None:
        self.assertTrue(
            "python3 -m unittest discover -s tests -v" in self.ci_text
            and "node --test cloudflare-worker/test/*.test.js" in self.ci_text
        )

    def test_workflows_contain_no_legacy_render_or_smcinema_references(self) -> None:
        combined = f"{self.scrape_text}\n{self.keep_alive_text}\n{self.ci_text}".lower()
        self.assertFalse(
            any(term in combined for term in ("render_api_url", "smcinema", "trigger_scrape", "scrape-all"))
        )


class ValidateDatabaseConfigStepTests(unittest.TestCase):
    """Execute the `Validate database configuration` step verbatim in bash."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = extract_step_run_script(
            SCRAPE_WORKFLOW.read_text(encoding="utf-8"),
            "Validate database configuration",
        )

    def _run_config_check(
        self,
        url: str = "",
        token: str = "",
    ) -> tuple[subprocess.CompletedProcess[str], str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            github_output = tmp_path / "github_output"
            github_summary = tmp_path / "github_summary"
            github_output.touch()
            github_summary.touch()
            result = run_bash(
                self.script,
                env={
                    "TURSO_DATABASE_URL": url,
                    "TURSO_AUTH_TOKEN": token,
                    "GITHUB_OUTPUT": str(github_output),
                    "GITHUB_STEP_SUMMARY": str(github_summary),
                },
                cwd=tmp_path,
            )
            return (
                result,
                github_output.read_text(encoding="utf-8"),
                github_summary.read_text(encoding="utf-8"),
            )

    def test_libsql_url_with_token_selects_turso_mode(self) -> None:
        result, output, _ = self._run_config_check(
            url="libsql://scholarship-ph-org.turso.io",
            token="eyJ-test-token",
        )
        self.assertEqual((result.returncode, output.strip()), (0, "storage_mode=turso"))

    def test_https_url_with_token_selects_turso_mode(self) -> None:
        result, output, _ = self._run_config_check(
            url="  https://scholarship-ph-org.turso.io  ",
            token="  eyJ-test-token  ",
        )
        self.assertEqual((result.returncode, output.strip()), (0, "storage_mode=turso"))

    def test_local_http_url_without_token_is_allowed_in_turso_mode(self) -> None:
        result, output, _ = self._run_config_check(url="http://127.0.0.1:8080")
        self.assertEqual((result.returncode, output.strip()), (0, "storage_mode=turso"))

    def test_empty_credentials_selects_local_mode_and_writes_summary_note(self) -> None:
        result, output, summary = self._run_config_check(url="   ", token="   ")
        self.assertTrue(
            result.returncode == 0
            and output.strip() == "storage_mode=local"
            and "file:scholarships.db" in summary
        )

    def test_remote_libsql_url_without_token_fails_fast(self) -> None:
        result, _, _ = self._run_config_check(url="libsql://scholarship-ph-org.turso.io")
        self.assertTrue(result.returncode == 1 and "TURSO_AUTH_TOKEN" in result.stdout)

    def test_token_without_url_fails_fast(self) -> None:
        result, _, _ = self._run_config_check(token="eyJ-test-token")
        self.assertTrue(result.returncode == 1 and "TURSO_DATABASE_URL" in result.stdout)


class CheckoutScraperStepTests(unittest.TestCase):
    """Execute the `Checkout scholarship_scraper` step verbatim against a local git remote."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = extract_step_run_script(
            SCRAPE_WORKFLOW.read_text(encoding="utf-8"),
            "Checkout scholarship_scraper",
        )

    def _run_checkout(
        self,
        *,
        repo: str = "kebbbnnn/scholarship_scraper",
        ref: str = "main",
        token: str = "",
        remote_root: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path, str, str]:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp_path = Path(tmp_dir.name)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        github_output = tmp_path / "github_output"
        github_summary = tmp_path / "github_summary"
        github_output.touch()
        github_summary.touch()

        env: dict[str, str] = {
            "SCRAPER_REPO": repo,
            "SCRAPER_REF": ref,
            "SCRAPER_REPO_TOKEN": token,
            "GIT_TERMINAL_PROMPT": "0",
            "GITHUB_OUTPUT": str(github_output),
            "GITHUB_STEP_SUMMARY": str(github_summary),
        }
        if remote_root is not None:
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"url.file://{remote_root}/.insteadOf",
                    "GIT_CONFIG_VALUE_0": "https://github.com/",
                }
            )

        result = run_bash(self.script, env=env, cwd=workspace)
        return (
            result,
            workspace,
            github_output.read_text(encoding="utf-8"),
            github_summary.read_text(encoding="utf-8"),
        )

    def test_rejects_invalid_repository_format(self) -> None:
        result, _, _, _ = self._run_checkout(repo="https://evil.example/a/b")
        self.assertTrue(result.returncode == 1 and "Invalid SCRAPER_REPO" in result.stdout)

    def test_rejects_flag_like_ref_injection(self) -> None:
        result, _, _, _ = self._run_checkout(ref="--upload-pack=sh")
        self.assertTrue(result.returncode == 1 and "Invalid SCRAPER_REF" in result.stdout)

    def test_checks_out_public_repo_main_and_records_summary(self) -> None:
        with tempfile.TemporaryDirectory() as remote_tmp:
            remote_root = Path(remote_tmp)
            _, expected_sha = create_local_git_remote(remote_root, "kebbbnnn", "scholarship_scraper")
            result, workspace, output, summary = self._run_checkout(
                repo="kebbbnnn/scholarship_scraper.git",
                ref="main",
                remote_root=remote_root,
            )
            self.assertTrue(
                result.returncode == 0
                and (workspace / "pyproject.toml").is_file()
                and f"sha={expected_sha}" in output
                and "kebbbnnn/scholarship_scraper" in summary
            )

    def test_checks_out_pinned_tag_with_masked_token_and_no_persisted_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as remote_tmp:
            remote_root = Path(remote_tmp)
            create_local_git_remote(remote_root, "kebbbnnn", "scholarship_scraper")
            secret_token = "github_pat_example_long_token_" + ("x" * 60)
            expected_mask = base64.b64encode(f"x-access-token:{secret_token}".encode("utf-8")).decode("ascii")

            result, workspace, _, _ = self._run_checkout(
                repo="kebbbnnn/scholarship_scraper",
                ref="v0.1.0",
                token=secret_token,
                remote_root=remote_root,
            )
            git_config = (workspace / ".git" / "config").read_text(encoding="utf-8")
            self.assertTrue(
                result.returncode == 0
                and f"::add-mask::{expected_mask}" in result.stdout
                and secret_token not in git_config
                and "extraheader" not in git_config
            )


class PipelineCommandStepTests(unittest.TestCase):
    """Execute `Run scholarship pipeline` and `Collect database statistics and export dataset` with a stub uv."""

    @classmethod
    def setUpClass(cls) -> None:
        workflow_text = SCRAPE_WORKFLOW.read_text(encoding="utf-8")
        cls.pipeline_script = extract_step_run_script(workflow_text, "Run scholarship pipeline")
        cls.export_script = extract_step_run_script(
            workflow_text,
            "Collect database statistics and export dataset",
        )

    def _run_pipeline(self, env_overrides: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            log_file = tmp_path / "uv_calls.jsonl"
            summary_file = tmp_path / "summary.md"
            create_fake_uv(bin_dir, log_file)

            env = {
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "GITHUB_STEP_SUMMARY": str(summary_file),
                "RUN_MODE": "live",
                "RUN_EXTRACTOR": "",
                "RUN_TARGET_URL": "",
                "RUN_SOURCES": "",
                "RUN_MAX_PAGES": "",
                "RUN_MAX_DEPTH": "",
                "RUN_FORCE_BROWSER": "false",
                "RUN_NO_SKIP_UNCHANGED": "false",
                "RUN_FAIL_ON_ERROR": "false",
                **env_overrides,
            }
            result = run_bash(self.pipeline_script, env=env, cwd=tmp_path)
            calls = [
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ] if log_file.exists() else []
            return result, calls

    def test_scheduled_live_run_omits_cli_overrides_for_repo_vars_and_custom_sources(self) -> None:
        result, calls = self._run_pipeline({})
        self.assertEqual(
            (result.returncode, calls[0][:4], len(calls[0])),
            (0, ["run", "scholarship-scraper", "scrape", "--summary-file"], 5),
        )

    def test_live_mode_passes_all_enabled_flags(self) -> None:
        result, calls = self._run_pipeline(
            {
                "RUN_MODE": "live",
                "RUN_EXTRACTOR": "deterministic",
                "RUN_SOURCES": "ched,dost-sei",
                "RUN_MAX_PAGES": "4",
                "RUN_MAX_DEPTH": "1",
                "RUN_FORCE_BROWSER": "true",
                "RUN_NO_SKIP_UNCHANGED": "true",
                "RUN_FAIL_ON_ERROR": "true",
            }
        )
        self.assertEqual(
            (result.returncode, calls[0][:3], calls[0][5:]),
            (
                0,
                ["run", "scholarship-scraper", "scrape"],
                [
                    "--extractor",
                    "deterministic",
                    "--max-pages",
                    "4",
                    "--max-depth",
                    "1",
                    "--sources",
                    "ched,dost-sei",
                    "--force-browser",
                    "--no-skip-unchanged",
                    "--fail-on-error",
                ],
            ),
        )

    def test_live_mode_with_target_url_invokes_scrape_url_subcommand(self) -> None:
        result, calls = self._run_pipeline(
            {
                "RUN_MODE": "live",
                "RUN_EXTRACTOR": "dynamic",
                "RUN_TARGET_URL": "  https://example.org/scholarships  ",
                "RUN_SOURCES": "ched",
                "RUN_MAX_PAGES": "5",
                "RUN_MAX_DEPTH": "1",
                "RUN_FORCE_BROWSER": "true",
                "RUN_NO_SKIP_UNCHANGED": "true",
                "RUN_FAIL_ON_ERROR": "true",
            }
        )
        self.assertEqual(
            (result.returncode, calls[0][:3], calls[0][5:]),
            (
                0,
                ["run", "scholarship-scraper", "scrape-url"],
                [
                    "--extractor",
                    "dynamic",
                    "--max-pages",
                    "5",
                    "--max-depth",
                    "1",
                    "--force-browser",
                    "--no-skip-unchanged",
                    "--fail-on-error",
                    "--",
                    "https://example.org/scholarships",
                ],
            ),
        )

    def test_whitespace_only_target_url_falls_back_to_regular_scrape(self) -> None:
        result, calls = self._run_pipeline(
            {
                "RUN_MODE": "live",
                "RUN_TARGET_URL": "   ",
                "RUN_SOURCES": "up",
            }
        )
        self.assertEqual(
            (result.returncode, calls[0][:3], calls[0][5:]),
            (0, ["run", "scholarship-scraper", "scrape"], ["--sources", "up"]),
        )

    def test_seed_curated_mode_omits_live_only_flags_and_ignores_target_url(self) -> None:
        result, calls = self._run_pipeline(
            {
                "RUN_MODE": "seed-curated",
                "RUN_EXTRACTOR": "dynamic",
                "RUN_TARGET_URL": "https://example.org/scholarships",
                "RUN_MAX_PAGES": "5",
                "RUN_MAX_DEPTH": "1",
                "RUN_FORCE_BROWSER": "true",
                "RUN_FAIL_ON_ERROR": "true",
            }
        )
        self.assertEqual(
            (result.returncode, calls[0][:3], calls[0][5:]),
            (
                0,
                ["run", "scholarship-scraper", "scrape"],
                ["--seed-curated", "--fail-on-error"],
            ),
        )

    def test_health_mode_invokes_health_subcommand_with_fail_on_unhealthy(self) -> None:
        result, calls = self._run_pipeline(
            {
                "RUN_MODE": "health",
                "RUN_SOURCES": "up",
                "RUN_FAIL_ON_ERROR": "true",
            }
        )
        self.assertEqual(
            (result.returncode, calls[0][:3], calls[0][5:]),
            (
                0,
                ["run", "scholarship-scraper", "health"],
                ["--sources", "up", "--fail-on-unhealthy"],
            ),
        )

    def test_export_step_copies_sqlite_db_only_in_local_storage_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            log_file = tmp_path / "uv_calls.jsonl"
            create_fake_uv(bin_dir, log_file)
            (tmp_path / "exports").mkdir()
            (tmp_path / "scholarships.db").write_bytes(b"sqlite-test")

            local_res = run_bash(
                self.export_script,
                env={
                    "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                    "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
                    "STORAGE_MODE": "local",
                },
                cwd=tmp_path,
            )
            copied_local = (tmp_path / "exports" / "scholarships.db").is_file()
            (tmp_path / "exports" / "scholarships.db").unlink()

            turso_res = run_bash(
                self.export_script,
                env={
                    "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                    "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
                    "STORAGE_MODE": "turso",
                },
                cwd=tmp_path,
            )
            copied_turso = (tmp_path / "exports" / "scholarships.db").is_file()

            self.assertEqual(
                (local_res.returncode, copied_local, turso_res.returncode, copied_turso),
                (0, True, 0, False),
            )


if __name__ == "__main__":
    unittest.main()
