# Local Delivery Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect Captain Cook to the existing VibeMind n8n instance and provide repository-owned Mailpit and MariaDB services for local development.

**Architecture:** VibeMind remains the sole owner and launcher of n8n, including its existing volume and credentials. Captain Cook owns a separate Compose project containing Mailpit and MariaDB, and reaches n8n through host port 15678.

**Tech Stack:** Docker Desktop 29.5.3, Docker Compose 5.1.4, n8n (external VibeMind service), Mailpit, MariaDB 11.8, PowerShell, pytest

## Global Constraints

- Do not mount, rename, migrate, or delete `voice_vibemind-n8n-data`.
- Do not define a second n8n service in Captain Cook's Compose file.
- Host-side n8n URL is `http://localhost:15678`; container-side URL is `http://host.docker.internal:15678`.
- Mailpit publishes web/API port `8025` and SMTP port `1025`.
- MariaDB publishes port `3306`, uses database `ledger`, and persists in `ledger_data`.
- Use timezone `Europe/Berlin`.
- Never commit real credentials or the root `.env`.
- Never remove Docker volumes during setup or verification.

---

## File Structure

- `docker-compose.yml`: Captain-owned Mailpit and MariaDB services, volumes, healthchecks, and required environment interpolation.
- `.env.example`: committed configuration contract with non-secret defaults and explicit secret placeholders.
- `.env`: local secrets and URLs; remains gitignored.
- `scripts/start_delivery_stack.ps1`: starts external VibeMind n8n, validates it, then starts Captain services.
- `scripts/verify_delivery_stack.ps1`: performs live health and connectivity checks without mutating persistent data.
- `tests/test_delivery_stack_docs.py`: static regression checks for service ownership, ports, safety rules, and documentation.
- `README.md`: operator quickstart, endpoints, shutdown semantics, and data-safety warning.
- `C:\Users\User\.Codex\projects\C--Users-User-myBrain\memory\MEMORY.md`: writable durable memory entry for the cross-project n8n ownership decision.

### Task 1: Static delivery-stack contract

**Files:**
- Create: `tests/test_delivery_stack_docs.py`
- Create: `docker-compose.yml`
- Modify: `.env.example`

**Interfaces:**
- Consumes: the approved local-delivery-stack design.
- Produces: Compose services `mailpit` and `mariadb`, volume `ledger_data`, and the environment-variable contract used by scripts and documentation.

- [ ] **Step 1: Write failing static contract tests**

Create tests that parse files as text and assert:

```python
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_compose_owns_mailpit_and_mariadb_but_not_n8n() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "  mailpit:" in compose
    assert "  mariadb:" in compose
    assert "  n8n:" not in compose
    assert "ledger_data:/var/lib/mysql" in compose


def test_env_example_documents_external_n8n_and_local_ports() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for entry in (
        "N8N_URL=http://localhost:15678",
        "N8N_CONTAINER_URL=http://host.docker.internal:15678",
        "MAILPIT_WEB_PORT=8025",
        "MAILPIT_SMTP_PORT=1025",
        "MARIADB_PORT=3306",
    ):
        assert entry in env
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest tests/test_delivery_stack_docs.py -q`

Expected: failure because `docker-compose.yml` does not exist.

- [ ] **Step 3: Add the minimal Compose stack**

Create `docker-compose.yml` with pinned Mailpit and MariaDB 11.8 images, required `${VARIABLE:?message}` interpolation for passwords, healthchecks, `restart: unless-stopped`, ports from `.env`, and named volume `ledger_data`. Do not define n8n or reference either existing n8n volume.

- [ ] **Step 4: Extend the environment template**

Preserve existing OpenAI entries and add the two n8n URLs, Mailpit ports/URLs, MariaDB port/database/user, password placeholders, and `GENERIC_TIMEZONE=Europe/Berlin`.

- [ ] **Step 5: Validate static and Compose contracts**

Run:

```powershell
python -m pytest tests/test_delivery_stack_docs.py -q
docker compose --env-file .env config --quiet
```

Expected: tests pass and Compose exits 0 without printing secrets.

- [ ] **Step 6: Commit the contract**

```powershell
git add docker-compose.yml .env.example tests/test_delivery_stack_docs.py
git commit -m "feat: add local delivery services"
```

### Task 2: Safe startup and live verification

**Files:**
- Create: `scripts/start_delivery_stack.ps1`
- Create: `scripts/verify_delivery_stack.ps1`
- Modify: `tests/test_delivery_stack_docs.py`

**Interfaces:**
- Consumes: environment names and Compose services from Task 1, plus VibeMind Compose file `C:\Users\User\Desktop\Vibemind_V1\vibemind-os\voice\docker-compose.n8n.yml`.
- Produces: a single startup command and a read-only verification command that exits nonzero on a missing dependency.

- [ ] **Step 1: Add failing script-safety tests**

Add assertions that both scripts exist, the startup script references the VibeMind Compose file and `docker compose up -d`, and neither script contains `down -v`, `volume rm`, or `docker rm`.

- [ ] **Step 2: Run the tests and confirm failure**

Run: `python -m pytest tests/test_delivery_stack_docs.py -q`

Expected: failure because the scripts do not exist.

- [ ] **Step 3: Implement the startup script**

The script must use `$ErrorActionPreference = "Stop"`, verify `docker info`, verify the external Compose file exists, start VibeMind n8n with its own Compose file, poll `http://localhost:15678/healthz`, start Captain services with `docker compose up -d --wait`, and invoke the verification script. It must never stop or recreate unrelated services.

- [ ] **Step 4: Implement the verification script**

The script must load URLs and credentials from the root `.env` without printing secret values, check n8n `/healthz`, check Mailpit `/api/v1/info`, check SMTP with `Test-NetConnection`, execute `SELECT 1` through `docker compose exec -T mariadb mariadb`, and test container-to-host n8n access using a disposable curl container on the Captain default network.

- [ ] **Step 5: Run static tests and PowerShell syntax checks**

Run:

```powershell
python -m pytest tests/test_delivery_stack_docs.py -q
[scriptblock]::Create((Get-Content -Raw scripts/start_delivery_stack.ps1)) | Out-Null
[scriptblock]::Create((Get-Content -Raw scripts/verify_delivery_stack.ps1)) | Out-Null
```

Expected: tests pass and both parsers exit without an exception.

- [ ] **Step 6: Commit scripts**

```powershell
git add scripts/start_delivery_stack.ps1 scripts/verify_delivery_stack.ps1 tests/test_delivery_stack_docs.py
git commit -m "feat: automate delivery stack startup"
```

### Task 3: Operator documentation and durable memory

**Files:**
- Modify: `README.md`
- Modify: `tests/test_delivery_stack_docs.py`
- Create: `C:\Users\User\.Codex\projects\C--Users-User-myBrain\memory\MEMORY.md`

**Interfaces:**
- Consumes: commands and endpoints produced by Tasks 1 and 2.
- Produces: a user-facing quickstart and durable cross-project ownership record.

- [ ] **Step 1: Add failing README assertions**

Assert that README includes `scripts/start_delivery_stack.ps1`, all three local URLs, `docker compose down`, and a warning against `docker compose down -v`.

- [ ] **Step 2: Run the test and confirm failure**

Run: `python -m pytest tests/test_delivery_stack_docs.py -q`

Expected: failure because the quickstart is absent.

- [ ] **Step 3: Document the operator workflow**

Add a concise README section with prerequisites, `.env` preparation, one-command startup, endpoint table, verification command, ordinary shutdown, and explicit volume ownership warnings.

- [ ] **Step 4: Write durable project memory**

Create the missing writable memory directory and `MEMORY.md`. Record the date, Captain Cook repo path, external VibeMind n8n Compose path, host/container URLs, Captain ownership of Mailpit/MariaDB, and the rule that neither n8n volume may be deleted or adopted. Do not copy secrets from either `.env`.

- [ ] **Step 5: Run documentation tests**

Run: `python -m pytest tests/test_delivery_stack_docs.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit repository documentation only**

```powershell
git add README.md tests/test_delivery_stack_docs.py
git commit -m "docs: add delivery stack quickstart"
```

The user-level `MEMORY.md` remains outside the repository and is not committed.

### Task 4: Live stack verification and regression suite

**Files:**
- Modify local-only: `.env`

**Interfaces:**
- Consumes: all artifacts from Tasks 1–3.
- Produces: a running, healthy stack and verification evidence in command output.

- [ ] **Step 1: Populate missing local `.env` values**

Preserve all existing values. Add generated local MariaDB application/root passwords and the documented non-secret URLs/ports. Never print or stage the resulting file.

- [ ] **Step 2: Validate rendered Compose configuration**

Run: `docker compose --env-file .env config --quiet`

Expected: exit code 0.

- [ ] **Step 3: Start and verify the stack**

Run: `powershell -ExecutionPolicy Bypass -File scripts/start_delivery_stack.ps1`

Expected: VibeMind n8n responds on 15678; Captain Mailpit and MariaDB report healthy; SMTP, SQL, and container-to-host checks pass.

- [ ] **Step 4: Run repository regression tests**

Run: `python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Inspect final state**

Run:

```powershell
docker compose ps
docker ps --filter name=vibemind-n8n
git status --short
```

Expected: Captain services are healthy, VibeMind n8n is running, `.env` is not staged, and only intentional files are changed.
