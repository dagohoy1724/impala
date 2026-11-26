# Impala — ScholarshipPH Scraper Scheduler

Dedicated public GitHub Actions scheduler repository that checks out and runs [`kebbbnnn/scholarship_scraper`](https://github.com/kebbbnnn/scholarship_scraper) daily, paired with two-layer anti-deactivation workflows to prevent GitHub from pausing scheduled actions on public repositories after 60 days of commit inactivity.

See [`kebbbnnn/scholarship_scraper`'s `docs/github-actions.md`](https://github.com/kebbbnnn/scholarship_scraper/blob/main/docs/github-actions.md) for upstream scraper behavior, connectors, and CLI options.

> [!IMPORTANT]
> **Avoid duplicate concurrent scrapes:** `kebbbnnn/scholarship_scraper` also contains its own `.github/workflows/scrape.yml` on a 12-hour schedule. If you configure `impala` to persist into the same Turso database, disable or remove the schedule in `scholarship_scraper` so the two repositories do not scrape concurrently into the same database (cross-repo runs do not share a `concurrency` group).

## How It Works

### 1. Daily Scholarship Collection Workflow ([`.github/workflows/scrape.yml`](.github/workflows/scrape.yml))

- **Schedule:** `7 18 * * *` (`18:07 UTC` = `02:07 AM PHT` daily), pre-shifted ~7 hours before `09:00 AM PHT` to compensate for GitHub Actions' `00:00–02:00 UTC` cron queue lag so the daily collection completes by ~`09:00 AM PHT`.
- **Concurrency & Timeout:** Uses `concurrency: group: scholarship-collection` with `cancel-in-progress: false` so overlapping scheduled/manual runs queue instead of interrupting an active database write. Timeout: `20 minutes`.
- **Execution Flow:**
  1. **Checks out `kebbbnnn/scholarship_scraper`** directly into the runner workspace (`SCRAPER_REPO` / `SCRAPER_REF`, defaulting to `kebbbnnn/scholarship_scraper@main`). Works unauthenticated for public repos, or authenticates with a one-shot masked `SCRAPER_REPO_TOKEN` (no persisted git credentials) if the scraper repo is private.
  2. **Sets up `uv` and Python 3.12** (`astral-sh/setup-uv@v10.4.0` with lockfile caching).
  3. **Validates Turso / SQLite storage configuration** before installing browsers or scraping (fails fast on half-configured Turso secrets).
  4. **Installs dependencies and caches browser binaries** (`invisible-playwright` Firefox on `ubuntu-latest` x86_64 by default, or standard Playwright Firefox when `browser_engine=playwright`; skipped entirely in `seed-curated` mode).
  5. **Initializes the schema and runs `scholarship-scraper`** (`live`, `seed-curated`, or `health`), appending Markdown tables to `$GITHUB_STEP_SUMMARY`.
  6. **Collects DB statistics and uploads artifacts** (`scholarships-export-<run_number>-<run_attempt>`, 14-day retention) containing `exports/scholarships.json` (plus `exports/scholarships.db` in zero-secret local SQLite mode).

#### Storage Modes

| `TURSO_DATABASE_URL` | `TURSO_AUTH_TOKEN` | Resulting Behavior |
| --- | --- | --- |
| Remote (`libsql://...` or `https://...`) | Set | **Turso Cloud mode:** initializes schema and upserts directly into Turso; exports `exports/scholarships.json` as an artifact. |
| Empty / unset | Empty / unset | **Local SQLite + Artifacts mode:** writes to `file:scholarships.db`, adds a note banner to the Job Summary, and uploads both `scholarships.json` and `scholarships.db`. |
| Remote (`libsql://...` or `https://...`) | Empty / unset | **Fails fast** asking for `TURSO_AUTH_TOKEN`. |
| Empty / unset | Set | **Fails fast** asking for `TURSO_DATABASE_URL`. |

#### Manual Runs (`workflow_dispatch`)

| Input | Type | Default | Options / Format | Behavior |
| --- | --- | --- | --- | --- |
| `mode` | choice | `live` | `live`, `seed-curated`, `health` | `live` scrapes live URLs; `seed-curated` runs offline fixture snapshots (skips browser install); `health` checks URL reachability & layout landmarks. |
| `sources` | string | `""` (all 8) | Comma-separated slugs or adapter keys | Limits execution to specific connectors (`ched`, `dost-sei`, `unifast`, `tesda`, `up`, `ateneo`, `dlsu`, `sm-foundation`). |
| `force_browser` | boolean | `false` | `true` / `false` | Forces headless browser rendering for HTML pages (`live` mode only). |
| `no_skip_unchanged` | boolean | `false` | `true` / `false` | Re-extracts documents even if their SHA-256 `content_hash` already exists in `source_documents`. |
| `browser_engine` | choice | `auto` | `auto`, `invisible`, `playwright` | `auto` selects `invisible-playwright` on `ubuntu-latest` (`x86_64`). Switch to `playwright` as a fallback. |
| `fail_on_error` | boolean | `false` | `true` / `false` | Fails the job if any source fails/hits `blocked_challenge` (`scrape`), or is unhealthy (`health`). Stats & exports still run for succeeded sources. |
| `scraper_ref` | string | `""` | Branch, tag, or full 40-char SHA | Overrides `vars.SCRAPER_REF` / `main` for a single manual run. |

Common `gh` CLI triggers:

```bash
# Run all 8 live sources
gh workflow run scrape.yml

# Zero-secret smoke test using offline curated snapshots (~20s, skips browser downloads)
gh workflow run scrape.yml -f mode=seed-curated -f fail_on_error=true

# Scrape specific sources with forced headless browser rendering
gh workflow run scrape.yml -f mode=live -f sources=ched,dost-sei -f force_browser=true

# Run source health & landmark checks
gh workflow run scrape.yml -f mode=health -f fail_on_error=true

# Run against a pinned tag or commit SHA of scholarship_scraper
gh workflow run scrape.yml -f scraper_ref=main
```

### 2. Anti-Deactivation Measures (Public Repository)

GitHub automatically disables `on: schedule` workflows in **public** repositories after **60 days without repository commits**. This repo prevents hitting that cutoff automatically and includes an optional external fallback:

1. **In-Repo Heartbeat Keep-Alive ([`.github/workflows/keep-alive.yml`](.github/workflows/keep-alive.yml)):**
   - Runs weekly (`17 5 * * 1`, Mondays at `05:17 UTC` / `01:17 PM PHT`; off-minute to avoid `:00` contention) and manually via `workflow_dispatch` (with an optional `force` toggle).
   - Checks the latest commit age (`git log -1 --format=%ct`). Whenever the repo has had no commits for `>= 45` days, it pushes a lightweight empty commit (`chore: keep-alive heartbeat [skip ci]`) via `GITHUB_TOKEN` (`permissions: contents: write`).
2. **Optional External Cloudflare Worker Cron ([`cloudflare-worker/`](cloudflare-worker/)):**
   - Runs independently of GitHub's scheduler on a Cron Trigger (`0 5 1 * *`, monthly / ~every 30 days) as an optional backup.
   - Queries `GET /repos/{owner}/{repo}/actions/workflows` and calls `PUT /repos/{owner}/{repo}/actions/workflows/{id}/enable` for workflows in `disabled_inactivity` state (leaving `disabled_manually` workflows untouched).

> [!NOTE]
> - Ensure **Settings -> Actions -> General -> Workflow permissions** allows **Read and write permissions** so `keep-alive.yml` can push heartbeat commits.

---

## Setup

> [!TIP]
> For a step-by-step walkthrough covering zero-secret smoke testing, Turso secrets, private scraper checkout tokens, workflow permissions, and Cloudflare deployment, see the [Setup Guide](SETUP_GUIDE.md).

### 1. GitHub Repository Secrets & Variables

Configure these under **Settings -> Secrets and variables -> Actions**.

#### Secrets

| Secret Name | Required | Accepted as Variable? | Description |
| --- | --- | --- | --- |
| `TURSO_DATABASE_URL` | For Turso persistence | Yes (`vars.TURSO_DATABASE_URL`) | Remote Turso URL (`libsql://<db-name>-<org>.turso.io` or `https://...`). Omit for local SQLite + artifacts mode. |
| `TURSO_AUTH_TOKEN` | When using remote Turso | No (Secret only) | Turso database JWT token. |
| `SCRAPER_REPO_TOKEN` | Only if scraper repo is private | No (Secret only) | Fine-grained PAT with **Contents: Read-only** on `kebbbnnn/scholarship_scraper`. Not needed while the scraper repository is public. |
| `SCRAPER_PROXY_SERVER` | No | Yes (`vars.SCRAPER_PROXY_SERVER`) | Optional proxy endpoint (`socks5://host:port` or `http://host:port`) forwarded to the browser engine. |
| `SCRAPER_PROXY_USERNAME` | No | No (Secret only) | Optional proxy authentication username. |
| `SCRAPER_PROXY_PASSWORD` | No | No (Secret only) | Optional proxy authentication password. |

#### Variables (`vars.*`)

| Variable Name | Default | Description |
| --- | --- | --- |
| `SCRAPER_REPO` | `kebbbnnn/scholarship_scraper` | Target GitHub repository (`owner/repo`) to check out and run. |
| `SCRAPER_REF` | `main` | Default branch, tag, or full 40-char commit SHA of `scholarship_scraper` to check out. |
| `SCRAPER_TIMEZONE` | `Asia/Manila` | IANA timezone passed to `invisible-playwright` and deadline evaluation. |
| `SCRAPER_HTTP_TIMEOUT_SECONDS` | `25` | Timeout in seconds for HTTP requests and browser navigation. |
| `SCRAPER_RATE_LIMIT_DELAY_SECONDS` | `1.0` | Delay in seconds between requests. |
| `SCRAPER_BROWSER_SEED` | *(random per run)* | Optional integer seed for deterministic `invisible-playwright` fingerprints. |

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

Run the offline workflow verification tests (Python stdlib only, no external dependencies):

```bash
python3 -m unittest discover -s tests -v
```

Run the Cloudflare Worker unit tests (Node 20+ built-in test runner):

```bash
node --test cloudflare-worker/test/*.test.js
```
