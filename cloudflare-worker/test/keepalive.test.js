import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { reenableDisabledWorkflows, validateConfig } from "../src/keepalive.js";

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("validateConfig", () => {
  it("throws when GITHUB_TOKEN is missing", () => {
    assert.throws(
      () => validateConfig({ GITHUB_OWNER: "example-owner", GITHUB_REPO: "impala" }),
      /GITHUB_TOKEN/,
    );
  });

  it("throws when owner or repo is missing", () => {
    assert.throws(() => validateConfig({ GITHUB_TOKEN: "pat" }), /GITHUB_OWNER/);
  });

  it("throws when owner is still a placeholder", () => {
    assert.throws(
      () =>
        validateConfig({
          GITHUB_TOKEN: "pat",
          GITHUB_OWNER: "REPLACE_WITH_GITHUB_OWNER",
          GITHUB_REPO: "impala",
        }),
      /GITHUB_OWNER/,
    );
  });
});

describe("reenableDisabledWorkflows", () => {
  const baseEnv = {
    GITHUB_TOKEN: "test-gh-pat",
    GITHUB_OWNER: "example-owner",
    GITHUB_REPO: "impala",
  };

  it("re-enables only workflows with state disabled_inactivity", async () => {
    const calls = [];
    const mockFetch = async (url, init) => {
      calls.push({ url, method: init.method, headers: init.headers });
      if (init.method === "GET") {
        return jsonResponse(200, {
          total_count: 3,
          workflows: [
            { id: 101, name: "Daily Scrape", state: "disabled_inactivity" },
            { id: 102, name: "Manual Off", state: "disabled_manually" },
            { id: 103, name: "Keep Alive", state: "active" },
          ],
        });
      }
      if (init.method === "PUT") {
        return new Response(null, { status: 204 });
      }
      throw new Error(`Unexpected method: ${init.method}`);
    };

    const result = await reenableDisabledWorkflows(baseEnv, mockFetch);

    assert.equal(result.totalWorkflows, 3);
    assert.equal(result.disabledCount, 1);
    assert.deepEqual(result.reenabledWorkflows, [{ id: 101, name: "Daily Scrape" }]);
    assert.equal(calls.length, 2);
    assert.equal(calls[1].method, "PUT");
    assert.equal(
      calls[1].url,
      "https://api.github.com/repos/example-owner/impala/actions/workflows/101/enable",
    );
    assert.equal(calls[1].headers.Authorization, "Bearer test-gh-pat");
  });

  it("makes zero PUT calls when no workflows are disabled_inactivity", async () => {
    const calls = [];
    const mockFetch = async (url, init) => {
      calls.push(init.method);
      return jsonResponse(200, {
        total_count: 1,
        workflows: [{ id: 101, name: "Daily Scrape", state: "active" }],
      });
    };

    const result = await reenableDisabledWorkflows(baseEnv, mockFetch);

    assert.equal(result.disabledCount, 0);
    assert.deepEqual(result.reenabledWorkflows, []);
    assert.deepEqual(calls, ["GET"]);
  });

  it("paginates through multiple pages of workflows", async () => {
    const pageOne = Array.from({ length: 100 }, (_, i) => ({
      id: i + 1,
      name: `wf-${i + 1}`,
      state: "active",
    }));
    const pageTwo = [
      { id: 101, name: "Daily Scrape", state: "disabled_inactivity" },
    ];
    const requestedUrls = [];

    const mockFetch = async (url, init) => {
      requestedUrls.push(`${init.method} ${url}`);
      if (init.method === "PUT") {
        return new Response(null, { status: 204 });
      }
      const page = new URL(url).searchParams.get("page");
      if (page === "1") {
        return jsonResponse(200, { total_count: 101, workflows: pageOne });
      }
      if (page === "2") {
        return jsonResponse(200, { total_count: 101, workflows: pageTwo });
      }
      throw new Error(`Unexpected request: ${url}`);
    };

    const result = await reenableDisabledWorkflows(baseEnv, mockFetch);

    assert.equal(result.totalWorkflows, 101);
    assert.deepEqual(result.reenabledWorkflows, [{ id: 101, name: "Daily Scrape" }]);
    assert.equal(requestedUrls.length, 3);
  });

  it("throws clear error when GitHub list API fails", async () => {
    const mockFetch = async () =>
      jsonResponse(401, { message: "Bad credentials" });

    await assert.rejects(
      () => reenableDisabledWorkflows(baseEnv, mockFetch),
      /GitHub list workflows failed \(401/,
    );
  });

  it("attempts remaining workflows even if one enable call fails, then throws", async () => {
    const putIds = [];
    const mockFetch = async (url, init) => {
      if (init.method === "GET") {
        return jsonResponse(200, {
          total_count: 2,
          workflows: [
            { id: 201, name: "First", state: "disabled_inactivity" },
            { id: 202, name: "Second", state: "disabled_inactivity" },
          ],
        });
      }
      putIds.push(url.split("/").at(-2));
      if (url.includes("/201/")) {
        return jsonResponse(403, { message: "Forbidden" });
      }
      return new Response(null, { status: 204 });
    };

    await assert.rejects(
      () => reenableDisabledWorkflows(baseEnv, mockFetch),
      /Re-enabled 1\/2 disabled workflows; errors: Failed to enable workflow 201/,
    );
    assert.deepEqual(putIds, ["201", "202"]);
  });
});
