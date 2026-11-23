# GitHub & Deployment Setup Guide

This guide details the post-push setup required in GitHub and Cloudflare for the `impala` project to run scheduled scrapes and keep actions active.

---

## 1. Required GitHub Settings

Navigate to your repository: `https://github.com/dagohoy1724/impala`

### Step 1.1: Add Required Repository Secrets (`RENDER_API_URL` and `API_KEY`)

Protected endpoints on your Render scraper service require Bearer authentication, and storing the service URL as a **Secret** keeps it masked in public GitHub Actions logs.

1. In your GitHub repository, open **Settings**.
2. In the left sidebar, expand **Secrets and variables** and select **Actions**.
3. Under the **Repository secrets** tab, click **New repository secret** and add both secrets:
   - **`RENDER_API_URL`** — Your Render service base URL (e.g. `https://<your-service>.onrender.com`, without a trailing slash).
   - **`API_KEY`** — Your Render service's `API_KEY` (from Render Dashboard -> your service -> **Environment** tab).
4. Click **Add secret** after each entry.

### Step 1.2: Enable Workflow Write Permissions (Required for Keep-Alive)

The in-repo keepalive workflow (`keep-alive.yml`) checks weekly whether the latest commit is `>= 45` days old and pushes an empty heartbeat commit (`[skip ci]`) via `GITHUB_TOKEN` so the repository never hits GitHub's 60-day inactivity cutoff.

1. In your GitHub repository, open **Settings**.
2. In the left sidebar under **Code and automation**, click **Actions** -> **General**.
3. Scroll down to **Workflow permissions**.
4. Select **Read and write permissions**.
5. Click **Save**.

---

## 2. Test Workflows Manually

Verify that authentication and communication with Render work before waiting for the scheduled cron.

### Test 1: Daily Scrape All

1. Open the **Actions** tab in your repository.
2. In the left menu, click **Daily Scrape All**.
3. Click the **Run workflow** dropdown on the right side.
4. Leave the `dates` field empty (or enter specific test dates like `2026-11-20`) and click **Run workflow**.
5. Click into the newly triggered run to observe the logs:
   - **Phase 1 (Wake-up):** Pings `GET /health` with automatic retries if Render is waking from sleep.
   - **Phase 2 (Trigger):** Sends `POST /scrape-all?background=true` with Bearer auth, receiving a `202 Accepted` response with a `run_id`.
   - **Phase 3 (Polling):** Polls `GET /scrape-all/{run_id}` every 5 seconds until status is `completed`.
   - **Phase 4 (Summary):** Emits any warnings for failed site IDs and writes a formatted Markdown summary table to the **Summary** tab.

### Test 2: Keep Scheduled Workflows Alive

1. In the **Actions** tab, click **Keep Scheduled Workflows Alive**.
2. Click **Run workflow** -> leave `force` unchecked -> click **Run workflow**.
3. Verify that the job finishes with status `0` and outputs:
   ```text
   Last commit age: 0 day(s) (threshold: 45 days).
   Repository is active; no keep-alive commit needed.
   ```
4. *(Optional)* To verify push permissions end-to-end immediately, run it once with `force` checked and confirm a `chore: keep-alive heartbeat [skip ci]` commit is pushed to `main`.

---

## 3. Daily Schedule Reference

| Workflow | Trigger Schedule | Local Time | Purpose |
| --- | --- | --- | --- |
| `scrape-all.yml` | `7 18 * * *` | **02:07 AM PHT** (pre-shifted for **~09:00 AM PHT** execution after GitHub's ~7h cron queue lag) | Wakes Render, triggers full scrape, and records completion in Turso DB. |
| `keep-alive.yml` | `17 5 * * 1` | **01:17 PM PHT** (every Monday) | Checks latest commit age and pushes an empty `[skip ci]` heartbeat commit if `>= 45` days old. |

*Note: The Philippines (`Asia/Manila`, UTC+8) does not observe Daylight Saving Time (DST), so `01:07 UTC` is always `09:07 AM PHT` year-round. Off-minute crons are used to avoid top-of-the-hour (`:00`) GitHub Actions scheduler congestion.*

---

## 4. (Optional) External Cloudflare Worker Setup

This acts as a second, independent safety net against GitHub's 60-day inactivity policy for public repositories.

### Step 4.1: Update `wrangler.toml`

Edit `cloudflare-worker/wrangler.toml` and ensure `GITHUB_OWNER` matches your repository:

```toml
[vars]
GITHUB_OWNER = "dagohoy1724"
GITHUB_REPO = "impala"
```

Commit and push this change if not already done:
```bash
git add cloudflare-worker/wrangler.toml
git commit -m "chore: configure github owner for cloudflare worker"
git push origin main
```

### Step 4.2: Create a GitHub Personal Access Token (PAT)

1. Under your `dagohoy1724` account, navigate to **Settings -> Developer Settings -> Personal access tokens -> Fine-grained tokens**.
2. Click **Generate new token**.
3. Set:
   - **Token name:** `cloudflare-worker-keepalive`
   - **Repository access:** Selected repositories -> choose `dagohoy1724/impala`
   - **Permissions:** Under **Repository permissions**, set **Actions** to **Read and write**.
4. Generate and copy the token (`github_pat_...`).

### Step 4.3: Deploy the Worker via Wrangler

Run the following commands from your terminal:

```bash
cd cloudflare-worker

# Login to Cloudflare
npx wrangler login

# Store the PAT as an encrypted secret
npx wrangler secret put GITHUB_TOKEN
# (paste the copied github_pat_... token when prompted)

# Deploy the worker
npx wrangler deploy
```

### Step 4.4: Test the Scheduled Event Locally (Optional)

```bash
cp .dev.vars.example .dev.vars
# Add GITHUB_TOKEN=github_pat_... in .dev.vars
npx wrangler dev --test-scheduled
```

In a separate terminal:
```bash
curl "http://localhost:8787/cdn-cgi/local/scheduled"
```

---

## 5. Troubleshooting & FAQ

### `Error: Missing RENDER_API_URL`
- Cause: The `RENDER_API_URL` repository secret was not added or is empty.
- Solution: Add `RENDER_API_URL` under **Settings -> Secrets and variables -> Actions -> Repository secrets**.

### `Error: Missing API_KEY`
- Cause: The `API_KEY` repository secret was not added or has a typo.
- Solution: Double-check **Settings -> Secrets and variables -> Actions -> API_KEY**.

### `HTTP 401: Invalid or missing API key`
- Cause: The value pasted into the `API_KEY` secret does not match the `API_KEY` in Render's environment.
- Solution: Copy the key directly from Render Dashboard -> your service -> **Environment** and re-save in GitHub.

### Job took ~1-2 minutes on the first step
- Cause: Normal behavior for Render's Free tier. When inactive, the container spins down. `trigger_scrape.py` pings `GET /health` until the container boots and dependencies warm up.

### Schedule queue delay (cron didn't run at exactly 09:07:00 AM)
- Cause: GitHub Actions scheduled events (`on: schedule`) run on shared public infrastructure. Even with off-minute schedules, GitHub may delay scheduled workflow dispatch by several minutes during peak load.
