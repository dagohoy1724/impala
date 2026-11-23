# Impala — Render Scraper Scheduler

Dedicated GitHub Actions repository for triggering `POST /scrape-all` on a hosted Render scraper service every day at **9:07 AM Philippine Time (`Asia/Manila`, UTC+8)**, paired with two-layer anti-deactivation workflows for public GitHub repositories.

## How It Works

### 1. Daily Scrape Workflow ([`.github/workflows/scrape-all.yml`](.github/workflows/scrape-all.yml))

- **Schedule:** `7 18 * * *` (`18:07 UTC` = `02:07 AM PHT` daily), pre-shifted ~7 hours before 09:00 AM PHT to avoid GitHub Actions' `00:00–02:00 UTC` cron backlog wave (~6.75–7h queue lag) so the daily scrape finishes by ~09:00 AM PHT.
- **Daily Idempotency Guard:** On `schedule` runs, `scripts/trigger_scrape.py` checks GitHub Actions workflow runs for the current `Asia/Manila` day first and skips if a scheduled scrape has already succeeded that day.
- **Manual Runs:** Supports `workflow_dispatch` with an optional comma-separated `dates` input (`YYYY-MM-DD`), which always runs immediately regardless of prior scheduled runs.
- **Execution ([`scripts/trigger_scrape.py`](scripts/trigger_scrape.py)):**
  1. Wakes the Render Free-tier container via `GET /health` (retrying `5xx` / connection timeouts while booting).
  2. Triggers `POST /scrape-all?background=true` (`202 Accepted` with `run_id`, authenticated via `Authorization: Bearer $API_KEY`).
  3. Polls `GET /scrape-all/{run_id}` until `status` reaches `"completed"` or `"failed"`.
  4. Fails the GitHub Actions job (exit code `1`) if the scrape fails, returns `0` theaters, encounters unrecoverable HTTP/auth errors, times out, or fails Turso persistence when `db_configured=true`.
  5. Writes a Markdown run table to `$GITHUB_STEP_SUMMARY` and emits a GitHub warning annotation if any `failed_site_ids` are reported.

### 2. Anti-Deactivation Measures (Public Repository)

GitHub automatically disables `on: schedule` workflows in **public** repositories after **60 days without repository commits**. This repo prevents hitting that cutoff automatically and includes an optional external fallback:

1. **In-Repo Heartbeat Keep-Alive ([`.github/workflows/keep-alive.yml`](.github/workflows/keep-alive.yml)):**
   - Runs weekly (`17 5 * * 1`, Mondays at 05:17 UTC / 01:17 PM PHT; off-minute to avoid `:00` contention) and manually via `workflow_dispatch` (with an optional `force` toggle).
   - Checks the latest commit age (`git log -1 --format=%ct`). Whenever the repo has had no commits for `>= 45` days, it pushes a lightweight empty commit (`chore: keep-alive heartbeat [skip ci]`) via `GITHUB_TOKEN` (`permissions: contents: write`).
   - Because the 60-day inactivity limit is never reached (with two weekly chances between day 45 and day 60), scheduled runs continue uninterrupted with zero external infrastructure (~8 empty commits/year).
2. **Optional External Cloudflare Worker Cron ([`cloudflare-worker/`](cloudflare-worker/)):**
   - Runs independently of GitHub's scheduler on a Cron Trigger (`0 5 1 * *`, monthly / ~every 30 days) as an optional backup.
   - Queries `GET /repos/{owner}/{repo}/actions/workflows` and calls `PUT /repos/{owner}/{repo}/actions/workflows/{id}/enable` for workflows in `disabled_inactivity` state (leaving `disabled_manually` workflows untouched).

> [!NOTE]
> - GitHub Actions `on: schedule` triggers are best-effort and can still experience minor queue delays during peak UTC load (both workflows use off-minute crons to avoid top-of-hour `:00` congestion).
> - Ensure **Settings -> Actions -> General -> Workflow permissions** allows **Read and write permissions** so `keep-alive.yml` can push heartbeat commits.

---

## Setup

> [!TIP]
> For a step-by-step walkthrough covering GitHub secrets, workflow permissions, manual testing, and Cloudflare deployment, see the [Setup Guide](SETUP_GUIDE.md).

### 1. GitHub Repository Secrets

In your GitHub repository under **Settings -> Secrets and variables -> Actions -> Secrets**:

| Kind | Name | Required | Value |
| --- | --- | --- | --- |
| Secret | `RENDER_API_URL` | Yes | Base URL of your hosted Render scraper service (e.g. `https://<your-service>.onrender.com`). Stored as a secret so it stays masked in public Actions logs. |
| Secret | `API_KEY` | Yes | The `API_KEY` configured on your Render service. |

### 2. Deploy the Cloudflare Worker (Optional External Keep-Alive)

1. Set `GITHUB_OWNER` (replacing `REPLACE_WITH_GITHUB_OWNER`) and verify `GITHUB_REPO` in [`cloudflare-worker/wrangler.toml`](cloudflare-worker/wrangler.toml) to match the GitHub account/repository where you push this project.
2. Create a GitHub **fine-grained Personal Access Token** on that GitHub account scoped to this repository with **Actions: Read and write** permission.
3. Deploy from `cloudflare-worker/`:

```bash
cd cloudflare-worker
npx wrangler login
npx wrangler secret put GITHUB_TOKEN
npx wrangler deploy
```

4. Test the scheduled handler locally with Wrangler:

```bash
cd cloudflare-worker
cp .dev.vars.example .dev.vars
# Set GITHUB_TOKEN in .dev.vars
npx wrangler dev --test-scheduled
# In another terminal:
curl "http://localhost:8787/cdn-cgi/local/scheduled"
```

---

### 3. Pushing from a Machine with Another Primary GitHub Account

If your global git config or macOS Keychain already stores another GitHub account, isolate this repository locally before committing and pushing:

```bash
# Set repo-local commit author identity
git config --local user.name "<separate-github-name>"
git config --local user.email "<separate-github-email>"
```

To avoid pushing with your primary GitHub credentials:

- **Option A (SSH host alias, recommended):** Create a dedicated SSH key for the separate account, add a `Host github-impala` entry in `~/.ssh/config` with `IdentityFile` and `IdentitiesOnly yes`, then push via:
  ```bash
  git remote add origin git@github-impala:<owner>/<repo>.git
  ```
- **Option B (HTTPS with per-repo credential isolation):** Enable path-scoped credentials locally so `osxkeychain` keeps this repo's credentials separate:
  ```bash
  git config --local credential.useHttpPath true
  git remote add origin https://github.com/<owner>/<repo>.git
  ```

---

## Running Tests Locally

Run the Python trigger tests (no external dependencies required):

```bash
python3 -m unittest discover -s tests -v
```

Run the Cloudflare Worker unit tests (Node 20+ built-in test runner):

```bash
node --test cloudflare-worker/test/*.test.js
```

Run [`scripts/trigger_scrape.py`](scripts/trigger_scrape.py) manually against Render:

```bash
export RENDER_API_URL="https://<your-service>.onrender.com"
export API_KEY="<your-render-api-key>"
python3 scripts/trigger_scrape.py
```
