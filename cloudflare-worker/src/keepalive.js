const GITHUB_API_BASE = "https://api.github.com";
const GITHUB_API_VERSION = "2022-11-28";
const DISABLED_INACTIVITY_STATE = "disabled_inactivity";
const DEFAULT_PER_PAGE = 100;
const MAX_PAGES = 20;
const USER_AGENT = "impala-cloudflare-keepalive/1.0";

function isPlaceholder(value) {
  return value.startsWith("REPLACE_") || (value.startsWith("<") && value.endsWith(">"));
}

/**
 * Validate required Worker environment variables and secrets.
 *
 * @param {Record<string, unknown>} env
 * @returns {{ token: string, owner: string, repo: string }}
 */
export function validateConfig(env) {
  const token = typeof env?.GITHUB_TOKEN === "string" ? env.GITHUB_TOKEN.trim() : "";
  const owner = typeof env?.GITHUB_OWNER === "string" ? env.GITHUB_OWNER.trim() : "";
  const repo = typeof env?.GITHUB_REPO === "string" ? env.GITHUB_REPO.trim() : "";

  if (!token) {
    throw new Error("Missing GITHUB_TOKEN secret.");
  }
  if (!owner || !repo || isPlaceholder(owner) || isPlaceholder(repo)) {
    throw new Error(
      "Missing or unconfigured GITHUB_OWNER or GITHUB_REPO in wrangler.toml.",
    );
  }

  return { token, owner, repo };
}

/**
 * Build standard GitHub REST API request headers.
 *
 * @param {string} token
 * @returns {Record<string, string>}
 */
export function buildGitHubHeaders(token) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "X-GitHub-Api-Version": GITHUB_API_VERSION,
    "User-Agent": USER_AGENT,
  };
}

async function readErrorBody(response) {
  try {
    const text = await response.text();
    return text.slice(0, 300).trim() || "<empty body>";
  } catch {
    return "<unreadable body>";
  }
}

/**
 * Fetch all workflows for the target GitHub repository across pages.
 *
 * @param {{ owner: string, repo: string, headers: Record<string, string> }} ctx
 * @param {typeof fetch} fetchImpl
 * @returns {Promise<Array<{ id: number, name: string, state: string, path?: string }>>}
 */
export async function listAllWorkflows({ owner, repo, headers }, fetchImpl = fetch) {
  const workflows = [];

  for (let page = 1; page <= MAX_PAGES; page += 1) {
    const url =
      `${GITHUB_API_BASE}/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}` +
      `/actions/workflows?per_page=${DEFAULT_PER_PAGE}&page=${page}`;
    const response = await fetchImpl(url, { method: "GET", headers });

    if (!response.ok) {
      const detail = await readErrorBody(response);
      throw new Error(
        `GitHub list workflows failed (${response.status} ${response.statusText}): ${detail}`,
      );
    }

    const payload = await response.json();
    const pageItems = Array.isArray(payload?.workflows) ? payload.workflows : [];
    workflows.push(...pageItems);

    const totalCount =
      typeof payload?.total_count === "number" ? payload.total_count : workflows.length;
    if (
      pageItems.length === 0 ||
      pageItems.length < DEFAULT_PER_PAGE ||
      workflows.length >= totalCount
    ) {
      break;
    }
  }

  return workflows;
}

/**
 * Re-enable a single GitHub Actions workflow via PUT /enable.
 *
 * @param {{ owner: string, repo: string, headers: Record<string, string> }} ctx
 * @param {{ id: number, name?: string }} workflow
 * @param {typeof fetch} fetchImpl
 * @returns {Promise<void>}
 */
export async function enableWorkflow({ owner, repo, headers }, workflow, fetchImpl = fetch) {
  const url =
    `${GITHUB_API_BASE}/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}` +
    `/actions/workflows/${encodeURIComponent(String(workflow.id))}/enable`;
  const response = await fetchImpl(url, { method: "PUT", headers });

  if (!response.ok) {
    const detail = await readErrorBody(response);
    throw new Error(
      `Failed to enable workflow ${workflow.id} (${workflow.name ?? "unnamed"}): ` +
        `HTTP ${response.status} ${detail}`,
    );
  }
}

/**
 * Find workflows disabled due to inactivity and re-enable them.
 *
 * @param {Record<string, unknown>} env
 * @param {typeof fetch} fetchImpl
 * @returns {Promise<{
 *   repository: string,
 *   totalWorkflows: number,
 *   disabledCount: number,
 *   reenabledWorkflows: Array<{ id: number, name: string }>
 * }>}
 */
export async function reenableDisabledWorkflows(env, fetchImpl = fetch) {
  const { token, owner, repo } = validateConfig(env);
  const headers = buildGitHubHeaders(token);
  const ctx = { owner, repo, headers };

  const workflows = await listAllWorkflows(ctx, fetchImpl);
  const disabled = workflows.filter((wf) => wf?.state === DISABLED_INACTIVITY_STATE);

  const reenabledWorkflows = [];
  const errors = [];

  for (const workflow of disabled) {
    try {
      await enableWorkflow(ctx, workflow, fetchImpl);
      reenabledWorkflows.push({ id: workflow.id, name: workflow.name ?? String(workflow.id) });
    } catch (error) {
      errors.push(error instanceof Error ? error.message : String(error));
    }
  }

  if (errors.length > 0) {
    throw new Error(
      `Re-enabled ${reenabledWorkflows.length}/${disabled.length} disabled workflows; ` +
        `errors: ${errors.join("; ")}`,
    );
  }

  return {
    repository: `${owner}/${repo}`,
    totalWorkflows: workflows.length,
    disabledCount: disabled.length,
    reenabledWorkflows,
  };
}
