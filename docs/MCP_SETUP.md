# MCP setup and operating boundaries

Captain Cook uses MCPs as development-time tools. They do not change the
offline demo's requirements or imply that an external integration is working.

## Local environment

The root `.env` file is ignored by Git and loaded by `config/llm_config.py`
for the LLM-backed legacy command. Add secrets only to that file:

```dotenv
OPENAI_API_KEY=replace-locally
CAPTAIN_MODEL=gpt-5.6
```

`python main.py demo` never reads or needs an OpenAI key. Do not include `.env`
content in an issue, artifact, screenshot, video, prompt, or commit.

## Current Codex MCPs

| Server | Status | Purpose | Safe operating boundary |
| --- | --- | --- | --- |
| Playwright | Installed as `@playwright/mcp` | Browser automation for local UI acceptance checks and Devpost page inspection | Use only against intended URLs; do not expose browser profiles or credentials in captures |
| Context7 | Installed as `@upstash/context7-mcp` | Current primary-library documentation while implementing dependencies | Treat retrieved docs as reference material, then verify behavior locally |
| n8n MCP | Registered at `http://localhost:15678/mcp-server/http` | Build, inspect, test, and run workflows once the existing local VibeMind n8n instance enables instance-level MCP access | It is unavailable until n8n runs and authentication is configured; never assume registration proves connectivity |

Inspect the user-level setup at any time:

```powershell
codex mcp list
codex mcp get playwright
codex mcp get context7
codex mcp get n8n-mcp
```

Restart the Codex session after changing user-level MCP configuration so tool
discovery can refresh.

## Playwright

The official Microsoft server is launched with:

```powershell
codex mcp add playwright npx "@playwright/mcp@latest"
```

For this project, use it for reproducible browser checks only after a UI or
hosted page exists. The server has browser-control power; do not navigate it to
untrusted content with an authenticated profile or disable file-access limits.

Official reference: <https://github.com/microsoft/playwright-mcp>.

## Context7

Context7 resolves a library and retrieves current documentation. It works
without a local key at lower limits. To use a personal key, retain it in a
user-level MCP setup or in the local `.env`, not in Git:

```powershell
codex mcp add context7 -- npx -y @upstash/context7-mcp --api-key YOUR_LOCAL_KEY
```

The configured server must never become an application runtime dependency.
Official reference: <https://context7.com/docs/resources/all-clients>.

## n8n MCP

n8n's first-party instance-level MCP can search, create, edit, trigger, and
test enabled workflows. The default local endpoint is:

```text
http://localhost:15678/mcp-server/http
```

The preferred setup is OAuth after n8n is running and instance-level MCP
access is enabled:

```powershell
codex mcp add n8n-mcp --url http://localhost:15678/mcp-server/http
```

If the n8n instance uses a token instead, use a user-level Codex configuration
that reads the token from a secret environment variable; never place it in
`config.toml`, this repository, or an artifact. Before allowing delivery work,
verify that `codex mcp get n8n-mcp` sees the intended endpoint and perform one
non-destructive workflow listing call.

Official reference: <https://docs.n8n.io/advanced-ai/mcp/accessing-n8n-mcp-server/>.
