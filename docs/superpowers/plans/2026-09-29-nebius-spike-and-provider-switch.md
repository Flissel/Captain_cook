# Nebius-Spike und Provider-Umstellung (S + M0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Klären, ob Claude Code über einen Relay mit Nemotron auf Token Factory und in ConTree-Sandboxes trägt (Spike), und alle direkten LLM-Aufrufe von Captain auf Nebius Token Factory mit einem Nemotron-Modell umstellen (M0).

**Architecture:** Eine einzige Provider-Auflösung (`config/llm_provider.py`) entscheidet aus der Umgebung, welcher OpenAI-kompatible Endpunkt, welcher Schlüssel und welches Modell gelten (`nebius` als Standard, `openai` und `custom` als Opt-in). `build_model_client`, die Evaluation-CLI, der Business-Benchmark und der Hermes-Adapter lesen daraus, statt `openai` fest zu verdrahten. Der LLM-Householder-Executor wird per Opt-in in die Demo gehängt; die deterministische Demo bleibt Standard.

**Tech Stack:** Python 3.11, AutoGen (`autogen_ext.models.openai.OpenAIChatCompletionClient`), pytest, `contree-sdk` (nur Spike), Claude Code CLI (nur Spike).

**Spec:** `docs/superpowers/specs/2026-09-29-nebius-codex-replacement-design.md`

**Folgepläne:** M1 (Relay + ClaudeSandboxRunner) wird nach Abschluss des Spikes auf Basis von `docs/superpowers/plans/2026-09-29-nebius-spike-findings.md` geschrieben; M2–M7 und Hosting folgen danach.

## Global Constraints

- Python 3.11, Projekt-`.venv`; Tests mit `.\.venv\Scripts\python.exe -m pytest` aus dem Worktree-Root (der Worktree hat kein eigenes `.venv`; nutze `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe`).
- Token-Factory-Basis-URL exakt `https://api.tokenfactory.nebius.com/v1`.
- Nemotron-Standardmodell: die in S1 per `GET /v1/models` bestätigte ID von Nemotron 3 Super (Kandidat `nvidia/Nemotron-3-Super-120b-a12b`).
- Secrets nur in der ignorierten `.env`; `NEBIUS_API_KEY` wird nie geloggt, nie in Tests, Fixtures, Commits oder Spike-Protokolle geschrieben.
- Offline-Pfad bleibt deterministisch: `pytest -m "not live"` braucht weder Netz noch Schlüssel.
- Live-Aufrufe (Token Factory, ConTree) kosten Geld und laufen nur nach ausdrücklicher Freigabe durch den User im Chat; Live-Tests tragen `@pytest.mark.live`.
- `artifacts/demo-run.json` nicht neu schreiben.
- Conventional Commits, Branch `feat/nebius-claude-sandbox`, kein Push ohne Auftrag; Commit-Trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Bekannte, schon vor dem Merge rote Tests (nicht in diesem Plan zu reparieren): `test_hermes_skill_evaluation::test_absolute_deadline_cancels_final_store_operations_before_success[*]`, `demo_orchestrator/test_configuration.py::test_real_d11_manifest_builder_is_compatible_with_d12_loader`, im Worktree ohne `.venv` zusätzlich `test_generate_runtime_adapter_manifest.py` (3).

## Review Focus

- `.env` setzt noch `OPENAI_API_KEY`/`CAPTAIN_MODEL=gpt-5.6` aus der Build-Week, aber kein `CAPTAIN_LLM_PROVIDER`: Captain muss dann Nebius wählen und ein OpenAI-Modell unter Nebius **ablehnen** statt es an Token Factory zu schicken (Test in Task 1).
- `CAPTAIN_LLM_PROVIDER=nebius` ohne `NEBIUS_API_KEY`: Fehler beim Bauen des Clients mit klarer Meldung, nicht ein 401 mitten im Lauf (Test in Task 2).
- Tippfehler/Groß-Kleinschreibung im Provider (`Nebius`, `nebius `): wird normalisiert; Unbekanntes (`nebuis`) wird abgelehnt (Test in Task 1).
- `CAPTAIN_LLM_BASE_URL` gesetzt **und** `CAPTAIN_LLM_PROVIDER=nebius`: widersprüchlich → Ablehnung statt stillem Gewinner (Test in Task 1).
- Hermes-Endpunkt ist Token Factory, aber `NEBIUS_API_KEY` fehlt: Ablehnung vor dem Prozessstart statt Platzhalter-Key an Nebius (Test in Task 5).

---

## Teil S — Spike (Wegwerf-Code, Ergebnis ist ein Befund)

Spike-Skripte liegen im Session-Scratchpad bzw. `E:\Temp\...\scratchpad\nebius-spike\`, **nicht** im Repo. Committet wird nur der Befund.

### Task S1: Token-Factory-Probe (Modelle, Tool-Calling, Structured Output)

**Files:**
- Create (Scratch, nicht committen): `<scratchpad>/nebius-spike/s1_probe.py`
- Create: `docs/superpowers/plans/2026-09-29-nebius-spike-findings.md`

**Voraussetzung:** Der User hat `NEBIUS_API_KEY` in `C:\Users\User\Desktop\Captain_cook\.env` eingetragen und im Chat die Live-Aufrufe freigegeben (Kosten: wenige Cent).

- [ ] **Step 1: Probe-Skript schreiben**

```python
"""S1: Token Factory capabilities probe. Throwaway; never commit."""
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

KEY = dotenv_values(Path(r"C:\Users\User\Desktop\Captain_cook\.env")).get("NEBIUS_API_KEY") or os.environ.get("NEBIUS_API_KEY")
if not KEY:
    sys.exit("NEBIUS_API_KEY missing")
BASE = "https://api.tokenfactory.nebius.com/v1"
H = {"Authorization": f"Bearer {KEY}"}
client = httpx.Client(timeout=120)

models = client.get(f"{BASE}/models", headers=H, params={"verbose": "true"}).json()
nemotron = sorted(m["id"] for m in models.get("data", []) if "nemotron" in m["id"].lower())
print("NEMOTRON_IDS", json.dumps(nemotron, indent=1))

TOOL = {"type": "function", "function": {"name": "write_file", "description": "Write a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}}
SCHEMA = {"type": "json_schema", "json_schema": {"name": "verdict", "strict": True, "schema": {
    "type": "object", "properties": {"accept": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["accept", "reason"], "additionalProperties": False}}}

for model in nemotron:
    row = {"model": model}
    r = client.post(f"{BASE}/chat/completions", headers=H, json={
        "model": model, "max_tokens": 512, "parallel_tool_calls": False, "tools": [TOOL],
        "messages": [{"role": "user", "content": "Create hello.txt containing 'hi'. Use the write_file tool."}]})
    row["tools_status"] = r.status_code
    if r.status_code == 200:
        msg = r.json()["choices"][0]["message"]
        row["tool_calls"] = [c["function"]["name"] for c in msg.get("tool_calls") or []]
        row["has_reasoning_content"] = bool(msg.get("reasoning_content"))
    else:
        row["tools_error"] = r.text[:200]
    r = client.post(f"{BASE}/chat/completions", headers=H, json={
        "model": model, "max_tokens": 256, "response_format": SCHEMA,
        "messages": [{"role": "user", "content": "Is 2+2=4? Answer as the schema says."}]})
    row["schema_status"] = r.status_code
    if r.status_code == 200:
        try:
            row["schema_ok"] = set(json.loads(r.json()["choices"][0]["message"]["content"])) == {"accept", "reason"}
        except (ValueError, TypeError):
            row["schema_ok"] = False
    r = client.post(f"{BASE}/chat/completions", headers=H, json={
        "model": model, "max_tokens": 64, "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "Say ok."}]})
    row["stream_usage"] = '"usage"' in r.text and '"prompt_tokens"' in r.text
    print("ROW", json.dumps(row))
```

- [ ] **Step 2: Ausführen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe <scratchpad>\nebius-spike\s1_probe.py`
Expected: eine `NEMOTRON_IDS`-Liste und je Modell eine `ROW`-Zeile. Ausgabe prüfen, dass kein Schlüssel darin steht.

- [ ] **Step 3: Befund notieren**

Lege `docs/superpowers/plans/2026-09-29-nebius-spike-findings.md` an mit genau diesen Abschnitten und fülle sie aus den ROW-Zeilen:

```markdown
# Nebius-Spike — Befunde (2026-09-29)

## S1 Token Factory
- Nemotron-IDs laut /v1/models: …
- Tool-Calling je Modell (Status, Tool-Call erzeugt ja/nein): …
- `parallel_tool_calls: false` akzeptiert: ja/nein
- `response_format: json_schema` je Modell: …
- Streaming liefert `usage` mit `include_usage`: ja/nein
- `reasoning_content` im Message-Objekt: ja/nein
- **Entscheidung:** NEMOTRON_DEFAULT_MODEL = `…`; NEMOTRON_SMALL_MODEL = `…`;
  NEMOTRON_TOOL_CALLING = True/False; NEMOTRON_STRUCTURED_OUTPUT = True/False

## S2 Claude Code → Relay → Nemotron (lokal)
## S3 ConTree-Fähigkeiten
## S4 Claude Code in ConTree
## Go/No-Go für M1
```

- [ ] **Step 4: Commit (nur der Befund)**

```bash
git add docs/superpowers/plans/2026-09-29-nebius-spike-findings.md
git commit -m "docs(plans): record Token Factory capability probe findings"
```

### Task S2: Claude Code → Relay → Nemotron, lokal

**Files:**
- Scratch: `<scratchpad>/nebius-spike/relay/` (Nebius-Cookbook-Relay), `<scratchpad>/nebius-spike/s2_workdir/`
- Modify: `docs/superpowers/plans/2026-09-29-nebius-spike-findings.md` (Abschnitt S2)

- [ ] **Step 1: Download-Freigabe einholen**

Frage den User im Chat, ob das Relay-Skript aus dem offiziellen Nebius-Cookbook (`https://dev.nebius.com/cookbook/claude-code-token-factory-relay`, Datei `scripts/nebius-relay.py`) in den Scratchpad geladen werden darf. Ohne Ja: Abschnitt S2 mit „übersprungen — kein Download freigegeben“ füllen und zu S3.

- [ ] **Step 2: Relay starten (lokal, Port 8787)**

```powershell
$env:NEBIUS_API_KEY = (Get-Content C:\Users\User\Desktop\Captain_cook\.env | Select-String '^NEBIUS_API_KEY=').ToString().Split('=',2)[1]
$env:NEBIUS_BASE_URL = 'https://api.tokenfactory.nebius.com/v1'
$env:NEBIUS_MODEL = '<NEMOTRON_DEFAULT_MODEL aus S1>'
python <scratchpad>\nebius-spike\relay\nebius-relay.py --port 8787
```
(Start im Hintergrund; Port und Aufrufsyntax an die README des Cookbooks anpassen.)

- [ ] **Step 3: Claude Code headless gegen das Relay**

```powershell
cd <scratchpad>\nebius-spike\s2_workdir
$env:ANTHROPIC_BASE_URL = 'http://127.0.0.1:8787'
$env:ANTHROPIC_AUTH_TOKEN = 'spike-local'
$env:ANTHROPIC_MODEL = '<NEMOTRON_DEFAULT_MODEL>'
$env:ANTHROPIC_SMALL_FAST_MODEL = '<NEMOTRON_SMALL_MODEL>'
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = '1'
claude -p "Create a file hello.py that prints 'hi', then run it with python and report the output." --output-format stream-json --verbose --max-turns 8 --dangerously-skip-permissions --allowedTools "Bash,Read,Edit,Write" --setting-sources "" --strict-mcp-config > s2.jsonl
```
Expected: `hello.py` existiert, `s2.jsonl` enthält `tool_use`-Einträge und ein `result` mit `subtype: "success"`.

- [ ] **Step 4: Wiederholen mit Ultra, falls Super scheitert**

Nur wenn Step 3 kein `success` liefert: dasselbe mit `ANTHROPIC_MODEL=<Ultra-ID aus S1>`.

- [ ] **Step 5: Befund S2 notieren**

In S2 festhalten: Modell, `result.subtype`, `num_turns`, Anzahl `tool_use`, ob der Datei-Write per Tool (nicht nur als Text) erfolgte, beobachtete Relay-Fehler (z. B. `output_config`, `?beta=true`, Tool-Namen), Dauer. Auch die Reihenfolge der `stream-json`-Eventtypen (`system/init`, `assistant`, `user`, `result`) mit Feldnamen notieren — sie ist die Vorlage für die M1-Fixtures. **Kein Inhalt, keine Schlüssel.**

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/plans/2026-09-29-nebius-spike-findings.md
git commit -m "docs(plans): record Claude Code via Nemotron relay spike"
```

### Task S3: ConTree-Fähigkeiten

**Files:**
- Scratch: `<scratchpad>/nebius-spike/.venv-spike`, `<scratchpad>/nebius-spike/s3_contree.py`
- Modify: Befund-Datei (Abschnitt S3)

- [ ] **Step 1: Spike-venv anlegen (nicht das Projekt-venv)**

```powershell
py -3.11 -m venv <scratchpad>\nebius-spike\.venv-spike
<scratchpad>\nebius-spike\.venv-spike\Scripts\python.exe -m pip install contree-sdk python-dotenv
```

- [ ] **Step 2: Probe schreiben**

```python
"""S3: ConTree capability probe. Throwaway; never commit."""
import asyncio
import time
from pathlib import Path

from dotenv import dotenv_values
from contree_sdk import Contree
from contree_client.httpx import ContreeAsyncClient

KEY = dotenv_values(Path(r"C:\Users\User\Desktop\Captain_cook\.env"))["NEBIUS_API_KEY"]


async def main() -> None:
    sdk = Contree(ContreeAsyncClient(KEY))
    findings: dict[str, object] = {}
    for tag in ("busybox:latest", "node:22-bookworm", "python:3.11-slim"):
        try:
            image = await sdk.images.use(tag)
            r = await image.run(shell="uname -a && (node --version || true) && (python3 --version || true)")
            findings[f"pull:{tag}"] = {"exit": r.exit_code, "stdout": r.stdout[:200]}
        except Exception as exc:  # spike: record every failure shape
            findings[f"pull:{tag}"] = f"ERROR {type(exc).__name__}: {str(exc)[:200]}"
    image = await sdk.images.use("node:22-bookworm")
    r1 = await image.run(shell="echo checkpoint > /tmp/marker && echo written")
    findings["checkpoint_new_image"] = getattr(r1, "uuid", None) is not None
    started = time.monotonic()
    r2 = await image.run(shell="head -c 12000000 /dev/zero | tr '\\0' 'a'")
    findings["stdout_12MB_len"] = len(r2.stdout or "")
    findings["stdout_12MB_seconds"] = round(time.monotonic() - started, 1)
    r3 = await image.run(shell="curl -s -o /dev/null -w '%{http_code}' https://api.tokenfactory.nebius.com/v1/models || wget -qO- https://api.tokenfactory.nebius.com/v1/models | head -c 50")
    findings["egress_to_tokenfactory"] = r3.stdout[:50]
    try:
        r4 = await image.run(shell="sleep 120", timeout=5)
        findings["timeout_5s"] = {"exit": r4.exit_code, "timed_out": getattr(r4, "timed_out", None)}
    except Exception as exc:
        findings["timeout_5s"] = f"ERROR {type(exc).__name__}: {str(exc)[:200]}"
    findings["sdk_surface"] = sorted(n for n in dir(sdk) if not n.startswith("_"))
    findings["image_surface"] = sorted(n for n in dir(image) if not n.startswith("_"))
    for key, value in findings.items():
        print(key, "=>", value)


asyncio.run(main())
```

- [ ] **Step 3: Ausführen (nach Live-Freigabe)**

Run: `<scratchpad>\nebius-spike\.venv-spike\Scripts\python.exe <scratchpad>\nebius-spike\s3_contree.py`
Expected: je Zeile ein Befund; Fehler werden notiert, nicht behoben.

- [ ] **Step 4: Upload/Download/Cancel in der SDK-Oberfläche suchen**

Aus `sdk_surface`/`image_surface` und der Doku `https://docs.tokenfactory.nebius.com/sandboxes/` klären: Datei-Upload (Name der Methode), Datei-Download aus einem Image, Cancel einer laufenden Operation, Egress-Beschränkung. Je Punkt: vorhanden (Methode/Endpunkt) oder nicht.

- [ ] **Step 5: Befund S3 notieren und committen**

Abschnitt S3 füllen: Public-Image-Pull je Tag, Checkpoint-UUID, stdout-Grenze bei 12 MB (abgeschnitten bei …), Egress zu Token Factory, Timeout-Verhalten, Upload/Download/Cancel/Egress-Kontrolle.

```bash
git add docs/superpowers/plans/2026-09-29-nebius-spike-findings.md
git commit -m "docs(plans): record ConTree sandbox capability findings"
```

### Task S4: Claude Code in ConTree

**Files:**
- Scratch: `<scratchpad>/nebius-spike/s4_claude_in_contree.py`
- Modify: Befund-Datei (Abschnitte S4, Go/No-Go)

**Voraussetzung:** S2 erfolgreich; S3 zeigt Public-Image-Pull und Egress. Die Sandbox braucht einen **öffentlich erreichbaren** Relay. Frage den User im Chat, welche Variante gilt, bevor irgendetwas exponiert wird:
(a) Relay kurzzeitig über einen Quick-Tunnel (z. B. `cloudflared tunnel --url http://127.0.0.1:8787`) mit zufälligem Token, nach dem Test sofort beendet, oder
(b) S4 bis zum Hosting (Plan D) verschieben und nur den Basis-Image-Bau prüfen.

- [ ] **Step 1: Basis-Image bauen**

```python
"""S4: build base image and run Claude Code inside ConTree. Throwaway."""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import dotenv_values
from contree_sdk import Contree
from contree_client.httpx import ContreeAsyncClient

KEY = dotenv_values(Path(r"C:\Users\User\Desktop\Captain_cook\.env"))["NEBIUS_API_KEY"]
RELAY_URL = os.environ.get("SPIKE_RELAY_URL")  # only for variant (a)
MODEL = os.environ["SPIKE_MODEL"]
SMALL = os.environ["SPIKE_SMALL_MODEL"]


async def main() -> None:
    sdk = Contree(ContreeAsyncClient(KEY))
    node = await sdk.images.use("node:22-bookworm")
    built = await node.run(shell="npm install -g @anthropic-ai/claude-code && apt-get update -qq && apt-get install -y -qq python3 git >/dev/null && claude --version")
    print("BUILD exit", built.exit_code, (built.stdout or "")[-200:])
    if RELAY_URL is None:
        return
    base = built  # the run result carries the new image version (see S3 finding)
    run = await base.run(
        shell=(
            "mkdir -p /work && cd /work && "
            "claude -p \"Create hello.py printing 'hi', run it, report output.\" "
            "--output-format stream-json --verbose --max-turns 8 --dangerously-skip-permissions "
            "--allowedTools 'Bash,Read,Edit,Write' --setting-sources '' --strict-mcp-config"
        ),
        env={
            "ANTHROPIC_BASE_URL": RELAY_URL,
            "ANTHROPIC_AUTH_TOKEN": "spike-tunnel",
            "ANTHROPIC_MODEL": MODEL,
            "ANTHROPIC_SMALL_FAST_MODEL": SMALL,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
        },
        timeout=600,
    )
    print("RUN exit", run.exit_code)
    out = run.stdout or ""
    print("RESULT_LINE", [line for line in out.splitlines() if '"type":"result"' in line][-1:][:1])
    sys.stdout.flush()


asyncio.run(main())
```
Die Aufrufform `base.run(...)` auf dem Ergebnis von `node.run(...)` und der Parametername `env` sind an die in S3 notierte SDK-Oberfläche anzupassen.

- [ ] **Step 2: Ausführen**

Run (Variante a): Relay + Tunnel starten, `SPIKE_RELAY_URL=<tunnel-url>`, `SPIKE_MODEL`, `SPIKE_SMALL_MODEL` setzen, Skript starten, danach Tunnel **sofort** beenden.
Run (Variante b): ohne `SPIKE_RELAY_URL` — nur der Build.
Expected: `BUILD exit 0` mit Claude-Version; bei (a) zusätzlich eine `result`-Zeile mit `success`.

- [ ] **Step 3: Go/No-Go festhalten und committen**

Abschnitte S4 und „Go/No-Go für M1“ füllen: gewähltes Modell, Image-Bau-Dauer, Lauf-Ergebnis, offene Risiken; Entscheidung „Go“ (M1 wie Spec) oder „No-Go“ mit Alternative (z. B. anderes Modell für die Coding-CLI, Nemotron nur für Planner/Judge).

```bash
git add docs/superpowers/plans/2026-09-29-nebius-spike-findings.md
git commit -m "docs(plans): record Claude-in-ConTree spike and go/no-go"
```

---

## Teil M0 — Provider-Umstellung (TDD, bleibender Code)

### Task 1: Provider-Auflösung

**Files:**
- Create: `config/llm_provider.py`
- Modify: `config/llm_config.py` (ganze Datei)
- Test: `tests/test_llm_provider.py` (neu), `tests/test_llm_config.py` (anpassen)

**Interfaces:**
- Produces:
  - `NEBIUS_BASE_URL: str`, `NEMOTRON_DEFAULT_MODEL: str`, `NEMOTRON_SMALL_MODEL: str`, `NEMOTRON_TOOL_CALLING: bool`, `NEMOTRON_STRUCTURED_OUTPUT: bool`, `OPENAI_DEFAULT_MODEL: str`, `CUSTOM_ENDPOINT_PLACEHOLDER_KEY: str`, `PROVIDER_SECRET_NAMES: Mapping[str, str]`
  - `class ProviderConfigurationError(ValueError)`
  - `@dataclass(frozen=True) class ProviderProfile: name: Literal["nebius","openai","custom"]; model: str; base_url: str | None; api_key: str | None; tool_calling: bool; structured_output: bool`
  - `def resolve_provider(environ: Mapping[str, str | None]) -> ProviderProfile`
  - `config.llm_config.PROFILE: ProviderProfile` plus die Alias-Konstanten `API_KEY`, `BASE_URL`, `MODEL`

- [ ] **Step 1: Failing tests schreiben** — `tests/test_llm_provider.py`

```python
import pytest

from config.llm_provider import (
    NEBIUS_BASE_URL,
    NEMOTRON_DEFAULT_MODEL,
    OPENAI_DEFAULT_MODEL,
    ProviderConfigurationError,
    resolve_provider,
)


def test_default_is_nebius_with_nemotron():
    profile = resolve_provider({"NEBIUS_API_KEY": "nb-test"})
    assert profile.name == "nebius"
    assert profile.base_url == NEBIUS_BASE_URL
    assert profile.model == NEMOTRON_DEFAULT_MODEL
    assert profile.api_key == "nb-test"


def test_provider_name_is_normalized():
    assert resolve_provider({"CAPTAIN_LLM_PROVIDER": " Nebius "}).name == "nebius"


def test_unknown_provider_is_rejected():
    with pytest.raises(ProviderConfigurationError, match="nebuis"):
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "nebuis"})


def test_openai_is_opt_in_and_keeps_its_default_model():
    profile = resolve_provider({"CAPTAIN_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"})
    assert profile.name == "openai"
    assert profile.base_url is None
    assert profile.model == OPENAI_DEFAULT_MODEL
    assert profile.api_key == "sk-test"


def test_openai_model_is_rejected_under_nebius():
    # A leftover Build-Week .env must not send gpt-* model names to Token Factory.
    with pytest.raises(ProviderConfigurationError, match="gpt-5.6"):
        resolve_provider({"CAPTAIN_MODEL": "gpt-5.6", "OPENAI_API_KEY": "sk-old"})


def test_nebius_does_not_fall_back_to_the_openai_key():
    profile = resolve_provider({"OPENAI_API_KEY": "sk-old"})
    assert profile.name == "nebius"
    assert profile.api_key is None


def test_base_url_alone_keeps_the_local_shim_path():
    profile = resolve_provider({"CAPTAIN_LLM_BASE_URL": "http://127.0.0.1:8114/v1"})
    assert profile.name == "custom"
    assert profile.base_url == "http://127.0.0.1:8114/v1"
    assert profile.tool_calling is False
    assert profile.structured_output is True


def test_base_url_with_nebius_is_contradictory():
    with pytest.raises(ProviderConfigurationError, match="CAPTAIN_LLM_BASE_URL"):
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "nebius", "CAPTAIN_LLM_BASE_URL": "http://x/v1"})


def test_custom_requires_a_base_url():
    with pytest.raises(ProviderConfigurationError, match="CAPTAIN_LLM_BASE_URL"):
        resolve_provider({"CAPTAIN_LLM_PROVIDER": "custom"})


def test_blank_values_count_as_unset():
    profile = resolve_provider({"CAPTAIN_LLM_PROVIDER": "", "CAPTAIN_MODEL": "  ", "NEBIUS_API_KEY": ""})
    assert profile.name == "nebius"
    assert profile.model == NEMOTRON_DEFAULT_MODEL
    assert profile.api_key is None


def test_api_key_is_not_in_repr():
    assert "nb-secret" not in repr(resolve_provider({"NEBIUS_API_KEY": "nb-secret"}))
```

- [ ] **Step 2: Tests laufen lassen, Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/test_llm_provider.py`
Expected: FAIL mit `ModuleNotFoundError: No module named 'config.llm_provider'`

- [ ] **Step 3: Implementieren** — `config/llm_provider.py`

Die vier `NEMOTRON_*`-Werte aus dem Abschnitt „Entscheidung“ von S1 in der Befund-Datei übernehmen (die unten stehenden Werte gelten nur, wenn S1 sie bestätigt).

```python
"""Resolve which OpenAI-compatible LLM endpoint Captain talks to.

One place decides provider, endpoint, key, and model so no caller hard-codes
a vendor. Nebius Token Factory with an NVIDIA Nemotron model is the default;
OpenAI and a custom local endpoint (the Claude Code shim) are opt-in.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

ProviderName = Literal["nebius", "openai", "custom"]

NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
# Confirmed against GET /v1/models in the 2026-09-29 spike (S1).
NEMOTRON_DEFAULT_MODEL = "nvidia/Nemotron-3-Super-120b-a12b"
NEMOTRON_SMALL_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
NEMOTRON_TOOL_CALLING = True
NEMOTRON_STRUCTURED_OUTPUT = True
OPENAI_DEFAULT_MODEL = "gpt-5.6"
CUSTOM_ENDPOINT_PLACEHOLDER_KEY = "captain-local-endpoint"

PROVIDER_SECRET_NAMES: Mapping[str, str] = MappingProxyType(
    {"nebius": "NEBIUS_API_KEY", "openai": "OPENAI_API_KEY"}
)

_OPENAI_ONLY_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4")


class ProviderConfigurationError(ValueError):
    """The environment names an unusable or contradictory LLM provider."""


@dataclass(frozen=True)
class ProviderProfile:
    name: ProviderName
    model: str
    base_url: str | None
    api_key: str | None = field(repr=False)
    tool_calling: bool
    structured_output: bool


def _value(environ: Mapping[str, str | None], name: str) -> str | None:
    raw = environ.get(name)
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def resolve_provider(environ: Mapping[str, str | None]) -> ProviderProfile:
    custom_url = _value(environ, "CAPTAIN_LLM_BASE_URL")
    requested = (_value(environ, "CAPTAIN_LLM_PROVIDER") or "").lower()
    if not requested:
        requested = "custom" if custom_url else "nebius"
    model = _value(environ, "CAPTAIN_MODEL")

    if requested in ("nebius", "openai") and custom_url:
        raise ProviderConfigurationError(
            "CAPTAIN_LLM_BASE_URL is only valid with CAPTAIN_LLM_PROVIDER=custom"
        )
    if requested == "nebius":
        if model is not None and model.lower().startswith(_OPENAI_ONLY_MODEL_PREFIXES):
            raise ProviderConfigurationError(
                f"CAPTAIN_MODEL={model} is an OpenAI model; Nebius needs a Token Factory model id"
            )
        return ProviderProfile(
            name="nebius",
            model=model or NEMOTRON_DEFAULT_MODEL,
            base_url=NEBIUS_BASE_URL,
            api_key=_value(environ, "NEBIUS_API_KEY"),
            tool_calling=NEMOTRON_TOOL_CALLING,
            structured_output=NEMOTRON_STRUCTURED_OUTPUT,
        )
    if requested == "openai":
        return ProviderProfile(
            name="openai",
            model=model or OPENAI_DEFAULT_MODEL,
            base_url=None,
            api_key=_value(environ, "OPENAI_API_KEY"),
            tool_calling=True,
            structured_output=True,
        )
    if requested == "custom":
        if custom_url is None:
            raise ProviderConfigurationError(
                "CAPTAIN_LLM_PROVIDER=custom requires CAPTAIN_LLM_BASE_URL"
            )
        return ProviderProfile(
            name="custom",
            model=model or OPENAI_DEFAULT_MODEL,
            base_url=custom_url,
            api_key=_value(environ, "OPENAI_API_KEY"),
            tool_calling=False,
            structured_output=True,
        )
    raise ProviderConfigurationError(
        f"unknown CAPTAIN_LLM_PROVIDER={requested!r}; use nebius, openai, or custom"
    )
```

- [ ] **Step 4: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/test_llm_provider.py`
Expected: 11 passed

- [ ] **Step 5: `config/llm_config.py` umstellen (ganze Datei ersetzen)**

```python
"""Configuration for LLM-backed workflows.

Values come from the process environment, falling back to a `.env` in the
working directory. Provider selection lives in `config.llm_provider`; the
module-level aliases below stay for existing callers.
"""
import os
from pathlib import Path

from dotenv import dotenv_values

from config.llm_provider import ProviderProfile, resolve_provider

_LOCAL_VALUES = dotenv_values(Path.cwd() / ".env")

PROFILE: ProviderProfile = resolve_provider({**_LOCAL_VALUES, **os.environ})
API_KEY = PROFILE.api_key
BASE_URL = PROFILE.base_url
MODEL = PROFILE.model
```

- [ ] **Step 6: `tests/test_llm_config.py` ersetzen**

```python
import importlib
import os

from config.llm_provider import NEMOTRON_DEFAULT_MODEL

_PROVIDER_VARS = (
    "CAPTAIN_LLM_PROVIDER",
    "CAPTAIN_LLM_BASE_URL",
    "CAPTAIN_MODEL",
    "NEBIUS_API_KEY",
    "OPENAI_API_KEY",
)


def _clean(monkeypatch, tmp_path):
    # The module also reads a .env from the working directory, so defaults are
    # only pinned once both sources are known to be empty.
    monkeypatch.chdir(tmp_path)
    for name in _PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)


def test_default_model_is_nemotron_on_nebius(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    import config.llm_config as llm_config

    reloaded = importlib.reload(llm_config)

    assert reloaded.PROFILE.name == "nebius"
    assert reloaded.MODEL == NEMOTRON_DEFAULT_MODEL


def test_local_env_file_supplies_openai_opt_in(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "CAPTAIN_LLM_PROVIDER=openai\nOPENAI_API_KEY=test-key\nCAPTAIN_MODEL=test-model\n",
        encoding="utf-8",
    )
    import config.llm_config as llm_config

    reloaded = importlib.reload(llm_config)

    assert reloaded.API_KEY == "test-key"
    assert reloaded.MODEL == "test-model"
    assert "OPENAI_API_KEY" not in os.environ
    assert "CAPTAIN_MODEL" not in os.environ


def test_local_env_file_supplies_nebius_key(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=nb-test\n", encoding="utf-8")
    import config.llm_config as llm_config

    reloaded = importlib.reload(llm_config)

    assert reloaded.API_KEY == "nb-test"
    assert "NEBIUS_API_KEY" not in os.environ
```

- [ ] **Step 7: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/test_llm_provider.py tests/test_llm_config.py`
Expected: 14 passed

- [ ] **Step 8: Commit**

```bash
git add config/llm_provider.py config/llm_config.py tests/test_llm_provider.py tests/test_llm_config.py
git commit -m "feat(config): resolve LLM provider with Nebius Nemotron as default"
```

### Task 2: `build_model_client` liest das Provider-Profil

**Files:**
- Modify: `agenten/llm/model_client.py` (Import + `build_model_client`)
- Test: `tests/llm/test_model_client.py` (die zwei bestehenden Endpoint-Tests ersetzen, neue ergänzen)

**Interfaces:**
- Consumes: `config.llm_config.PROFILE`, `config.llm_provider.{ProviderProfile, ProviderConfigurationError, CUSTOM_ENDPOINT_PLACEHOLDER_KEY}`
- Produces: `build_model_client(api_key: str | None = None, model: str | None = None, base_url: str | None = None, *, profile: ProviderProfile | None = None) -> ChatCompletionClient`

- [ ] **Step 1: Failing tests** — in `tests/llm/test_model_client.py` die Funktionen `test_openai_client_disables_sdk_retries` und `test_configured_endpoint_is_passed_explicitly` durch Folgendes ersetzen (übrige Tests der Datei bleiben):

```python
from config.llm_provider import (
    CUSTOM_ENDPOINT_PLACEHOLDER_KEY,
    NEBIUS_BASE_URL,
    ProviderConfigurationError,
    ProviderProfile,
)

_OPENAI = ProviderProfile("openai", "gpt-5.6", None, "sk-test", True, True)
_NEBIUS = ProviderProfile(
    "nebius", "nvidia/Nemotron-3-Super-120b-a12b", NEBIUS_BASE_URL, "nb-test", True, True
)
_SHIM = ProviderProfile("custom", "claude-code", "http://127.0.0.1:8114/v1", None, False, True)


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def build_stub(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(model_client_module, "OpenAIChatCompletionClient", build_stub)
    return captured


def test_openai_client_disables_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    model_client_module.build_model_client(profile=_OPENAI)

    assert captured == {
        "model": "gpt-5.6",
        "api_key": "sk-test",
        "max_retries": 0,
        "parallel_tool_calls": False,
    }


def test_nebius_profile_targets_token_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    model_client_module.build_model_client(profile=_NEBIUS)

    assert captured["base_url"] == NEBIUS_BASE_URL
    assert captured["api_key"] == "nb-test"
    assert captured["model"] == "nvidia/Nemotron-3-Super-120b-a12b"
    assert captured["model_info"]["function_calling"] is True
    assert captured["model_info"]["structured_output"] is True


def test_nebius_without_key_fails_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    keyless = ProviderProfile("nebius", "nvidia/x", NEBIUS_BASE_URL, None, True, True)

    with pytest.raises(ProviderConfigurationError, match="NEBIUS_API_KEY"):
        model_client_module.build_model_client(profile=keyless)


def test_configured_endpoint_is_passed_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured endpoint travels as an argument, not as ambient env."""
    captured = _capture(monkeypatch)

    model_client_module.build_model_client(profile=_SHIM)

    assert captured["base_url"] == "http://127.0.0.1:8114/v1"
    # Local shims ignore the key, but the SDK still requires one.
    assert captured["api_key"] == CUSTOM_ENDPOINT_PLACEHOLDER_KEY
    assert captured["model_info"]["function_calling"] is False


def test_explicit_arguments_override_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    model_client_module.build_model_client(api_key="nb-other", model="nvidia/other", profile=_NEBIUS)

    assert captured["api_key"] == "nb-other"
    assert captured["model"] == "nvidia/other"


def test_default_profile_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)
    monkeypatch.setattr(model_client_module, "PROFILE", _NEBIUS)

    model_client_module.build_model_client()

    assert captured["base_url"] == NEBIUS_BASE_URL
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/llm/test_model_client.py`
Expected: FAIL (`build_model_client() got an unexpected keyword argument 'profile'`)

- [ ] **Step 3: Implementieren** — in `agenten/llm/model_client.py` die Zeile `from config.llm_config import API_KEY, BASE_URL, MODEL` und die Funktion `build_model_client` ersetzen:

```python
from config.llm_config import PROFILE
from config.llm_provider import (
    CUSTOM_ENDPOINT_PLACEHOLDER_KEY,
    ProviderConfigurationError,
    ProviderProfile,
)


def build_model_client(
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    *,
    profile: Optional[ProviderProfile] = None,
) -> ChatCompletionClient:
    """Build a real `ChatCompletionClient` for the configured provider.

    Defaults come from `config.llm_config.PROFILE` (Nebius Token Factory with
    Nemotron unless the environment opts into OpenAI or a custom endpoint).
    Explicit arguments override the profile. SDK retries stay disabled so the
    Captain stage wrapper remains the sole retry owner.
    """
    active = profile if profile is not None else PROFILE
    resolved_model = model if model is not None else active.model
    resolved_api_key = api_key if api_key is not None else active.api_key
    resolved_base_url = base_url if base_url is not None else active.base_url

    if active.name == "nebius" and resolved_api_key is None:
        raise ProviderConfigurationError(
            "NEBIUS_API_KEY is not configured for the Nebius Token Factory provider"
        )

    kwargs: dict[str, object] = {
        "model": resolved_model,
        "max_retries": 0,
        "parallel_tool_calls": False,
    }
    if resolved_api_key is not None:
        kwargs["api_key"] = resolved_api_key

    # A configured endpoint is passed explicitly rather than left to the SDK's
    # OPENAI_BASE_URL lookup, so which path this client takes is visible here.
    if resolved_base_url:
        kwargs["base_url"] = resolved_base_url
        kwargs.setdefault("api_key", CUSTOM_ENDPOINT_PLACEHOLDER_KEY)
        # Non-OpenAI model names are unknown to the SDK, which refuses to
        # construct without declared capabilities. They come from the profile
        # so they match what the endpoint verifiably does.
        kwargs["model_info"] = ModelInfo(
            vision=False,
            function_calling=active.tool_calling,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=active.structured_output,
        )

    return OpenAIChatCompletionClient(**kwargs)
```

Den Modul-Docstring-Satz „defaulting `api_key`/`model` from the existing `config.llm_config` module (`API_KEY`, `MODEL`)“ ersetzen durch „defaulting provider, endpoint, key and model from `config.llm_config.PROFILE`“.

- [ ] **Step 4: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/llm/ tests/test_llm_provider.py tests/test_llm_config.py`
Expected: alle grün

- [ ] **Step 5: Andere Aufrufer von `BASE_URL`/`API_KEY` aus `model_client` suchen**

Run: `git grep -n "model_client_module, \"BASE_URL\"\|model_client_module, \"API_KEY\"\|from agenten.llm.model_client import .*API_KEY"`
Expected: keine Treffer. Treffer auf `PROFILE`-Monkeypatch umstellen wie in Step 1.

- [ ] **Step 6: Commit**

```bash
git add agenten/llm/model_client.py tests/llm/test_model_client.py
git commit -m "feat(llm): build model clients from the resolved provider profile"
```

### Task 3: Evaluation-CLI nutzt das Provider-Profil

**Files:**
- Modify: `agenten/evaluation/cli.py:36` (`DEFAULT_MODEL`), `:193-215` (`build_evaluation_model_client`), `:251-258` (Aufruf in `async_main`)
- Test: `tests/evaluation/test_cli_provider.py` (neu)

**Interfaces:**
- Consumes: `config.llm_provider.{resolve_provider, ProviderProfile, ProviderConfigurationError}`
- Produces: `build_evaluation_model_client(*, profile: ProviderProfile, model: str | None = None) -> ChatCompletionClient`

- [ ] **Step 1: Failing tests**

```python
from typing import Any

import pytest

from agenten.evaluation import cli as evaluation_cli
from config.llm_provider import NEBIUS_BASE_URL, ProviderConfigurationError, ProviderProfile

_NEBIUS = ProviderProfile("nebius", "nvidia/Nemotron-3-Super-120b-a12b", NEBIUS_BASE_URL, "nb-test", True, True)
_OPENAI = ProviderProfile("openai", "gpt-5.6", None, "sk-test", True, True)


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        evaluation_cli, "OpenAIChatCompletionClient", lambda **kw: captured.update(kw) or object()
    )
    return captured


def test_nebius_evaluation_client_uses_token_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    evaluation_cli.build_evaluation_model_client(profile=_NEBIUS)

    assert captured["base_url"] == NEBIUS_BASE_URL
    assert captured["api_key"] == "nb-test"
    assert captured["model"] == "nvidia/Nemotron-3-Super-120b-a12b"
    assert captured["model_info"]["function_calling"] is True
    assert "reasoning_effort" not in captured  # OpenAI-only parameter


def test_openai_evaluation_client_keeps_gpt5_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    evaluation_cli.build_evaluation_model_client(profile=_OPENAI)

    assert "base_url" not in captured
    assert captured["reasoning_effort"] == "none"
    assert captured["model_info"]["family"] == "gpt-5"


def test_model_argument_overrides_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch)

    evaluation_cli.build_evaluation_model_client(profile=_NEBIUS, model="nvidia/other")

    assert captured["model"] == "nvidia/other"


def test_evaluation_requires_a_tool_capable_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    shim = ProviderProfile("custom", "claude-code", "http://127.0.0.1:8114/v1", None, False, True)

    with pytest.raises(ProviderConfigurationError, match="tool calling"):
        evaluation_cli.build_evaluation_model_client(profile=shim)


def test_nebius_evaluation_without_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    keyless = ProviderProfile("nebius", "nvidia/x", NEBIUS_BASE_URL, None, True, True)

    with pytest.raises(ProviderConfigurationError, match="NEBIUS_API_KEY"):
        evaluation_cli.build_evaluation_model_client(profile=keyless)
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/evaluation/test_cli_provider.py`
Expected: FAIL (`unexpected keyword argument 'profile'`)

- [ ] **Step 3: Implementieren** — `build_evaluation_model_client` ersetzen, `DEFAULT_MODEL` löschen, Import ergänzen:

```python
from config.llm_provider import ProviderConfigurationError, ProviderProfile, resolve_provider


def build_evaluation_model_client(
    *,
    profile: ProviderProfile,
    model: str | None = None,
) -> ChatCompletionClient:
    """Build the real tool-capable provider with concurrent tool calls disabled."""

    if not profile.tool_calling:
        raise ProviderConfigurationError(
            f"evaluation needs tool calling; provider {profile.name} does not declare it"
        )
    if profile.api_key is None:
        secret = "NEBIUS_API_KEY" if profile.name == "nebius" else "OPENAI_API_KEY"
        raise ProviderConfigurationError(f"{secret} is not configured for evaluation")
    kwargs: dict[str, object] = {
        "model": model or profile.model,
        "api_key": profile.api_key,
        "max_retries": 0,
        "parallel_tool_calls": False,
    }
    if profile.name == "openai":
        kwargs["reasoning_effort"] = "none"
        kwargs["model_info"] = ModelInfo(
            vision=True,
            function_calling=True,
            json_output=True,
            family=ModelFamily.GPT_5,
            structured_output=True,
        )
    else:
        kwargs["base_url"] = profile.base_url
        kwargs["model_info"] = ModelInfo(
            vision=False,
            function_calling=True,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=profile.structured_output,
        )
    return OpenAIChatCompletionClient(**kwargs)
```

In `async_main` die Zeilen

```python
    model_identifier = args.model or os.environ.get("CAPTAIN_MODEL") or DEFAULT_MODEL
```
und
```python
        inner = model_client or build_evaluation_model_client(
            model=model_identifier,
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
```
ersetzen durch

```python
    try:
        profile = resolve_provider(os.environ)
    except ProviderConfigurationError:
        _print_summary({"error": "invalid_provider", "status": "failed"})
        return 1
    model_identifier = args.model or profile.model
```
und
```python
        inner = model_client or build_evaluation_model_client(
            profile=profile,
            model=model_identifier,
        )
```

- [ ] **Step 4: Weitere `DEFAULT_MODEL`-Nutzer anpassen**

Run: `git grep -n "DEFAULT_MODEL" agenten/evaluation tests/evaluation`
Expected: Treffer nur noch dort, wo Tests `evaluation_cli.DEFAULT_MODEL` verwenden; diese auf `resolve_provider({}).model` bzw. das explizite `--model` umstellen.

- [ ] **Step 5: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/evaluation/`
Expected: alle grün

- [ ] **Step 6: Commit**

```bash
git add agenten/evaluation/cli.py tests/evaluation/
git commit -m "feat(evaluation): run AgentFarm evaluation on the resolved provider"
```

### Task 4: Business-Benchmark erlaubt `nebius`

**Files:**
- Create: `agenten/agent_factory/benchmark_providers.py`
- Modify:
  - `agenten/agent_factory/business_benchmark_production_ports.py:249-400` (Client-Factory, Builder, Pricing-Source)
  - `agenten/agent_factory/business_benchmark_live.py:49` (`_PROVIDER_SECRET_NAMES`)
  - `agenten/agent_factory/business_benchmark_bootstrap.py:1711`
  - `gateway/business_benchmark_live_composition.py:585-590`
  - `gateway/agent_factory_live_composition.py:198`
  - `gateway/capability_live_production_builder.py:92`
- Test: `tests/agent_factory/test_benchmark_providers.py` (neu)

**Nicht in diesem Task:** `agenten/agent_runtime/contracts.py` `provider: Literal["openai"]` (Zeilen 347, 423) gehört zum Provider-Proxy/Runtime-Usage und wird in M1 (Relay) umgestellt.

**Interfaces:**
- Consumes: `config.llm_provider.{NEBIUS_BASE_URL, PROVIDER_SECRET_NAMES}`
- Produces:
  - `BENCHMARK_PROVIDERS: frozenset[str]` = `{"nebius", "openai"}`
  - `def require_benchmark_provider(provider: str) -> str` (wirft `ValueError("business benchmark provider must be one of: nebius, openai")`)
  - `def benchmark_provider_secret_name(provider: str) -> str`
  - `def build_benchmark_model_client(*, provider: str, api_key: str, model: str) -> ChatCompletionClient`

- [ ] **Step 1: Failing tests**

```python
from typing import Any

import pytest

from agenten.agent_factory import benchmark_providers
from agenten.agent_factory.benchmark_providers import (
    benchmark_provider_secret_name,
    build_benchmark_model_client,
    require_benchmark_provider,
)
from config.llm_provider import NEBIUS_BASE_URL


def test_nebius_and_openai_are_allowlisted():
    assert require_benchmark_provider("nebius") == "nebius"
    assert require_benchmark_provider("openai") == "openai"


def test_other_providers_are_rejected():
    with pytest.raises(ValueError, match="nebius, openai"):
        require_benchmark_provider("anthropic")


def test_secret_names_follow_the_provider():
    assert benchmark_provider_secret_name("nebius") == "NEBIUS_API_KEY"
    assert benchmark_provider_secret_name("openai") == "OPENAI_API_KEY"


def test_nebius_benchmark_client_targets_token_factory(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        benchmark_providers, "OpenAIChatCompletionClient", lambda **kw: captured.update(kw) or object()
    )

    build_benchmark_model_client(provider="nebius", api_key="nb-test", model="nvidia/x")

    assert captured["base_url"] == NEBIUS_BASE_URL
    assert captured["temperature"] == 0
    assert captured["max_retries"] == 0
    assert captured["model_info"]["function_calling"] is True


def test_openai_benchmark_client_is_unchanged(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        benchmark_providers, "OpenAIChatCompletionClient", lambda **kw: captured.update(kw) or object()
    )

    build_benchmark_model_client(provider="openai", api_key="sk-test", model="gpt-5.6")

    assert captured == {"api_key": "sk-test", "model": "gpt-5.6", "max_retries": 0, "temperature": 0}
```

Zusätzlich in `tests/agent_factory/test_benchmark_providers.py`:

```python
from agenten.agent_factory.business_benchmark_production_ports import (
    OpenAIBusinessBenchmarkModelClientBuilder,
)


def test_builder_reads_the_nebius_secret():
    builder = OpenAIBusinessBenchmarkModelClientBuilder.from_environment(
        {"CAPTAIN_BENCHMARK_PROVIDER": "nebius", "CAPTAIN_BENCHMARK_MODEL": "nvidia/x", "NEBIUS_API_KEY": "nb-test"}
    )
    assert builder.provider == "nebius"


def test_builder_does_not_accept_the_openai_secret_for_nebius():
    with pytest.raises(ValueError, match="NEBIUS_API_KEY"):
        OpenAIBusinessBenchmarkModelClientBuilder.from_environment(
            {"CAPTAIN_BENCHMARK_PROVIDER": "nebius", "CAPTAIN_BENCHMARK_MODEL": "nvidia/x", "OPENAI_API_KEY": "sk-old"}
        )
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/agent_factory/test_benchmark_providers.py`
Expected: FAIL (`No module named 'agenten.agent_factory.benchmark_providers'`)

- [ ] **Step 3: Modul anlegen** — `agenten/agent_factory/benchmark_providers.py`

```python
"""Allowlisted model providers for Captain business benchmarks."""
from __future__ import annotations

from autogen_core.models import ChatCompletionClient, ModelFamily, ModelInfo
from autogen_ext.models.openai import OpenAIChatCompletionClient

from config.llm_provider import NEBIUS_BASE_URL, PROVIDER_SECRET_NAMES

BENCHMARK_PROVIDERS = frozenset({"nebius", "openai"})


def require_benchmark_provider(provider: str) -> str:
    if provider not in BENCHMARK_PROVIDERS:
        raise ValueError("business benchmark provider must be one of: nebius, openai")
    return provider


def benchmark_provider_secret_name(provider: str) -> str:
    return PROVIDER_SECRET_NAMES[require_benchmark_provider(provider)]


def build_benchmark_model_client(*, provider: str, api_key: str, model: str) -> ChatCompletionClient:
    kwargs: dict[str, object] = {
        "api_key": api_key,
        "model": model,
        "max_retries": 0,
        "temperature": 0,
    }
    if require_benchmark_provider(provider) == "nebius":
        kwargs["base_url"] = NEBIUS_BASE_URL
        kwargs["model_info"] = ModelInfo(
            vision=False,
            function_calling=True,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=True,
        )
    return OpenAIChatCompletionClient(**kwargs)
```

- [ ] **Step 4: `business_benchmark_production_ports.py` umstellen**

1. `_build_openai_client` löschen; `BenchmarkModelClientFactory`-Default wird `build_benchmark_model_client` (Import aus `benchmark_providers`).
2. In `OpenAIBusinessBenchmarkModelClientBuilder.from_environment` und `from_environment_deferred`:

```python
        provider = require_benchmark_provider(_required(environment, "CAPTAIN_BENCHMARK_PROVIDER"))
        secret_name = benchmark_provider_secret_name(provider)
```
und `_api_key=_required(environment, secret_name)` bzw. `_api_key=environment.get(secret_name, "").strip()`.
3. In `__call__`: `self.provider != "openai"` → `self.provider not in BENCHMARK_PROVIDERS`; Fehlermeldungen „OpenAI provider secret is not present“ → „benchmark provider secret is not present“, „OpenAI model is not Captain-authorized“ → „benchmark model is not Captain-authorized“; Rückgabe `self._client_factory(provider=self.provider, api_key=self._api_key, model=self.model)`.
4. `ConfiguredBusinessBenchmarkPricingSource.__init__` und `.from_environment`: `if provider != "openai": raise ValueError(...)` → `require_benchmark_provider(provider)`.
5. Klassenname bleibt (Umbenennung ist M6).

- [ ] **Step 5: Übrige Allowlist-Stellen umstellen**

- `business_benchmark_live.py:49`: `_PROVIDER_SECRET_NAMES = {"openai": "OPENAI_API_KEY"}` → `from agenten.agent_factory.benchmark_providers import BENCHMARK_PROVIDERS` und `_PROVIDER_SECRET_NAMES = {name: PROVIDER_SECRET_NAMES[name] for name in BENCHMARK_PROVIDERS}` (Import `PROVIDER_SECRET_NAMES` aus `config.llm_provider`).
- `business_benchmark_bootstrap.py:1711` und `gateway/business_benchmark_live_composition.py:585-590`: Bedingung wird
  ```python
  if (
      canonical.provider not in BENCHMARK_PROVIDERS
      or canonical.provider_secret_name != benchmark_provider_secret_name(canonical.provider)
  ):
  ```
- `gateway/agent_factory_live_composition.py:198`: `provider != "openai"` → `provider not in BENCHMARK_PROVIDERS`.
- `gateway/capability_live_production_builder.py:92`: `values["benchmark_provider"] != "openai"` → `values["benchmark_provider"] not in BENCHMARK_PROVIDERS`.

- [ ] **Step 6: Tests anpassen, die alte Meldungen erwarten**

Run: `git grep -n "must be openai\|OpenAI provider secret is not present\|OpenAI model is not Captain-authorized" tests`
Expected: jede Fundstelle auf die neuen Meldungen aus Step 3/4 umstellen (`match="nebius, openai"`, `"benchmark provider secret is not present"`, `"benchmark model is not Captain-authorized"`).

Außerdem ruft der Builder seine `_client_factory` jetzt mit `provider=` auf. Run: `git grep -n "client_factory=" tests`. Jede dort injizierte Fake-Factory, die nur `api_key`/`model` annimmt, bekommt zusätzlich den Parameter `provider: str` (z. B. `lambda *, provider, api_key, model: fake`).

- [ ] **Step 7: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/agent_factory tests/gateway -k "benchmark or provider or capability_live or live_composition"`
Expected: alle grün außer den in Global Constraints genannten Altlasten

- [ ] **Step 8: Commit**

```bash
git add agenten/agent_factory/benchmark_providers.py agenten/agent_factory/business_benchmark_production_ports.py agenten/agent_factory/business_benchmark_live.py agenten/agent_factory/business_benchmark_bootstrap.py gateway/business_benchmark_live_composition.py gateway/agent_factory_live_composition.py gateway/capability_live_production_builder.py tests/agent_factory/test_benchmark_providers.py tests
git commit -m "feat(benchmark): allow Nebius Token Factory as benchmark provider"
```

### Task 5: Hermes auf Token Factory

**Files:**
- Modify: `agenten/agent_runtime/captain_production_adapters.py:155-159`
- Modify: `agenten/agent_factory/live_demo_entrypoint.py:71-86` (`build_live_demo_hermes`)
- Test: `tests/agent_runtime/test_captain_production_adapters.py` (neue Tests ans Ende), `tests/agent_factory/test_live_demo_entrypoint.py` (neu oder ergänzen)

**Interfaces:**
- Consumes: `config.llm_provider.NEBIUS_BASE_URL`, `RuntimeAdapterContext`, `create_runtime_adapters`, Test-Helfer `_skill_digest` aus derselben Testdatei
- Produces: `build_live_demo_hermes(..., environ: Mapping[str, str] | None = None)`; bei `None` wird `os.environ` gelesen.

- [ ] **Step 1: Failing tests für den Adapter** — ans Ende von `tests/agent_runtime/test_captain_production_adapters.py`:

```python
def _hermes_context(tmp_path: Path, extra: dict[str, str]) -> RuntimeAdapterContext:
    skill = tmp_path / "agenten" / "agent_factory" / "skills" / "captain-agent-factory-loop"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# released\n", encoding="utf-8")
    return RuntimeAdapterContext(
        repository_root=tmp_path,
        artifact_root=tmp_path / ".captain-cook" / "artifacts" / "sha256",
        environ={
            "CAPTAIN_HERMES_RUNTIME_SKILL_SHA256": _skill_digest(skill),
            "CAPTAIN_CODEX_EXECUTABLE": "codex",
            **extra,
        },
    )


def test_hermes_on_token_factory_uses_the_nebius_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    context = _hermes_context(
        tmp_path,
        {
            "CAPTAIN_HERMES_PROVIDER": "custom",
            "CAPTAIN_HERMES_MODEL": "nvidia/Nemotron-3-Super-120b-a12b",
            "CAPTAIN_HERMES_BASE_URL": "https://api.tokenfactory.nebius.com/v1",
            "NEBIUS_API_KEY": "nb-test",
        },
    )

    create_runtime_adapters(context)

    assert os.environ["OPENAI_BASE_URL"] == "https://api.tokenfactory.nebius.com/v1"
    assert os.environ["OPENAI_API_KEY"] == "nb-test"


def test_hermes_on_token_factory_without_key_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    context = _hermes_context(
        tmp_path,
        {
            "CAPTAIN_HERMES_PROVIDER": "custom",
            "CAPTAIN_HERMES_MODEL": "nvidia/x",
            "CAPTAIN_HERMES_BASE_URL": "https://api.tokenfactory.nebius.com/v1",
        },
    )

    with pytest.raises(ValueError, match="NEBIUS_API_KEY"):
        create_runtime_adapters(context)
    assert "OPENAI_API_KEY" not in os.environ


def test_hermes_local_shim_keeps_the_placeholder_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    context = _hermes_context(
        tmp_path,
        {
            "CAPTAIN_HERMES_PROVIDER": "custom",
            "CAPTAIN_HERMES_MODEL": "claude-code",
            "CAPTAIN_HERMES_BASE_URL": "http://127.0.0.1:8114/v1",
        },
    )

    create_runtime_adapters(context)

    assert os.environ["OPENAI_API_KEY"] == "captain-hermes-local-endpoint"
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/agent_runtime/test_captain_production_adapters.py -k "token_factory or local_shim"`
Expected: FAIL (`OPENAI_API_KEY == 'captain-hermes-local-endpoint'` statt `nb-test`; zweiter Test ohne Exception)

- [ ] **Step 3: Adapter implementieren** — in `captain_production_adapters.py` den Block

```python
        base_url = context.environ.get("CAPTAIN_HERMES_BASE_URL", "").strip()
        if base_url:
            os.environ["OPENAI_BASE_URL"] = base_url
            os.environ.setdefault("OPENAI_API_KEY", "captain-hermes-local-endpoint")
```
ersetzen durch

```python
        base_url = context.environ.get("CAPTAIN_HERMES_BASE_URL", "").strip()
        if base_url:
            # Token Factory needs the real Nebius key; a local shim ignores the
            # key but the OpenAI client inside Hermes still requires one.
            if base_url.rstrip("/") == NEBIUS_BASE_URL:
                nebius_key = context.environ.get("NEBIUS_API_KEY", "").strip()
                if not nebius_key:
                    raise ValueError(
                        "NEBIUS_API_KEY is required for the Token Factory Hermes endpoint"
                    )
                os.environ["OPENAI_BASE_URL"] = base_url
                os.environ["OPENAI_API_KEY"] = nebius_key
            else:
                os.environ["OPENAI_BASE_URL"] = base_url
                os.environ.setdefault("OPENAI_API_KEY", "captain-hermes-local-endpoint")
```
und oben `from config.llm_provider import NEBIUS_BASE_URL` ergänzen. Den Kommentar darüber („Hermes resolves a custom provider endpoint …“) unverändert lassen.

- [ ] **Step 4: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/agent_runtime/test_captain_production_adapters.py`
Expected: alle grün

- [ ] **Step 5: Failing test für die Live-Demo-Hermes-Einstellungen** — `tests/agent_factory/test_live_demo_hermes_provider.py`:

```python
from agenten.agent_factory import live_demo_entrypoint


def test_live_demo_hermes_settings_read_provider_and_model(monkeypatch, tmp_path):
    captured = {}

    class _Factory:
        def __init__(self, *, settings, **_):
            captured["settings"] = settings

        def validate_dispatch_configuration(self, dispatch):
            return None

    monkeypatch.setattr(live_demo_entrypoint, "HermesCliFactory", _Factory)
    monkeypatch.setattr(live_demo_entrypoint, "FilesystemReleasedFactorySkillCatalog", lambda root: object())
    monkeypatch.setattr(live_demo_entrypoint, "FilesystemFactorySkillReplayStore", lambda root: object())

    live_demo_entrypoint.build_live_demo_hermes(
        dispatch=object(),
        hermes_executable="hermes",
        skill_root=tmp_path,
        released_skill_root=tmp_path,
        evidence_root=tmp_path,
        clock=lambda: None,
        environ={"CAPTAIN_HERMES_PROVIDER": "custom", "CAPTAIN_HERMES_MODEL": "nvidia/x"},
    )

    assert captured["settings"].provider == "custom"
    assert captured["settings"].model == "nvidia/x"


def test_live_demo_hermes_without_provider_keeps_defaults(monkeypatch, tmp_path):
    captured = {}

    class _Factory:
        def __init__(self, *, settings, **_):
            captured["settings"] = settings

        def validate_dispatch_configuration(self, dispatch):
            return None

    monkeypatch.setattr(live_demo_entrypoint, "HermesCliFactory", _Factory)
    monkeypatch.setattr(live_demo_entrypoint, "FilesystemReleasedFactorySkillCatalog", lambda root: object())
    monkeypatch.setattr(live_demo_entrypoint, "FilesystemFactorySkillReplayStore", lambda root: object())

    live_demo_entrypoint.build_live_demo_hermes(
        dispatch=object(),
        hermes_executable="hermes",
        skill_root=tmp_path,
        released_skill_root=tmp_path,
        evidence_root=tmp_path,
        clock=lambda: None,
        environ={},
    )

    assert captured["settings"].provider is None
    assert captured["settings"].model is None
```

- [ ] **Step 6: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/agent_factory/test_live_demo_hermes_provider.py`
Expected: FAIL (`unexpected keyword argument 'environ'`)

- [ ] **Step 7: Implementieren** — in `build_live_demo_hermes` den Parameter `environ: Mapping[str, str] | None = None` ergänzen und die Settings so bauen:

```python
    values = os.environ if environ is None else environ
    provider = values.get("CAPTAIN_HERMES_PROVIDER", "").strip() or None
    model = values.get("CAPTAIN_HERMES_MODEL", "").strip() or None
    settings = HermesCliSettings(
        executable=hermes_executable,
        skill_root=skill_root,
        evidence_root=evidence_root,
        released_skill_root=released_skill_root,
        provider=provider,
        model=model,
    )
```
(`import os` und `from collections.abc import Mapping` ergänzen, falls nicht vorhanden.)

- [ ] **Step 8: Tests laufen lassen und committen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/agent_factory/test_live_demo_hermes_provider.py tests/agent_runtime/test_captain_production_adapters.py`
Expected: alle grün

```bash
git add agenten/agent_runtime/captain_production_adapters.py agenten/agent_factory/live_demo_entrypoint.py tests/agent_runtime/test_captain_production_adapters.py tests/agent_factory/test_live_demo_hermes_provider.py
git commit -m "feat(hermes): run Hermes on Token Factory with the Nebius key"
```

- [ ] **Step 9: Adapter-Manifest-Hinweis**

`captain_production_adapters.py` ist per Digest im Runtime-Adapter-Manifest gepinnt. Im Hauptcheckout (mit `.venv`) nach dem Merge dieses Branches `scripts/generate-runtime-adapter-manifest.py` laufen lassen und die neue SHA in die lokale `.env` (`CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256`) übernehmen. Das ist ein Betriebsschritt, kein Commit in diesem Task; im Abschlussbericht erwähnen.

### Task 6: LLM-Householder per Opt-in in der Demo

**Files:**
- Modify: `agenten/demo.py:85-92` (`run_demo`)
- Modify: `main.py:12-30` (Parser `demo`), `main.py:77-85` (Aufruf)
- Test: `tests/test_demo_householder_executor.py` (neu)

**Interfaces:**
- Consumes: `agenten.household.llm_executor.LlmHouseholderExecutor(model_client, *, timeout_seconds=30.0, max_attempts=2)`, `agenten.llm.model_client.build_model_client`, `create_householder_worker_factories(executor=...)`
- Produces: `run_demo(output_path: Path | None = None, *, householder_model_client: ChatCompletionClient | None = None) -> DemoSummary`; CLI `python main.py demo --householder-executor {deterministic,llm}`

- [ ] **Step 1: Failing tests**

```python
import json

import pytest

import main
from agenten.demo import run_demo
from agenten.llm.model_client import build_replay_model_client


def _report(role: str) -> str:
    return json.dumps(
        {"role": role, "decision": "accept", "artifacts": [], "evidence": ["checked"], "limitations": []}
    )


@pytest.mark.asyncio
async def test_demo_runs_householders_on_an_injected_model_client():
    # One reply per householder role; the replay client never touches the network.
    client = build_replay_model_client(
        [_report(r) for r in ("architect", "ledger-steward", "delivery-builder", "quality-warden")]
    )

    summary = await run_demo(householder_model_client=client)

    evidence = [
        item
        for sub in summary.subproblems
        for item in (sub.get("result") or {}).get("evidence", [])
    ]
    assert evidence and all(item.startswith("llm_reported:") for item in evidence)


@pytest.mark.asyncio
async def test_default_demo_stays_deterministic():
    summary = await run_demo()

    evidence = [
        item
        for sub in summary.subproblems
        for item in (sub.get("result") or {}).get("evidence", [])
    ]
    assert not any(item.startswith("llm_reported:") for item in evidence)


def test_cli_defaults_to_the_deterministic_executor():
    assert main.parse_args(["demo"]).householder_executor == "deterministic"


def test_cli_accepts_the_llm_executor():
    assert main.parse_args(["demo", "--householder-executor", "llm"]).householder_executor == "llm"
```

Vor Step 2 prüfen: `DemoSummary` hat ein Attribut mit den Subproblemen und deren `result`. Run: `git grep -n "class DemoSummary" -A12 agenten/demo.py`. Heißt das Feld anders als `subproblems` oder liegen die Reports anders, die zwei Evidence-Ausdrücke in den Tests entsprechend anpassen, **bevor** sie rot laufen. Ebenso das JSON-Format von `_report` gegen `HouseholderReportModel` in `agenten/household/llm_executor.py` prüfen und angleichen.

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/test_demo_householder_executor.py`
Expected: FAIL (`unexpected keyword argument 'householder_model_client'` bzw. `householder_executor`)

- [ ] **Step 3: `run_demo` implementieren**

```python
async def run_demo(
    output_path: Path | None = None,
    *,
    householder_model_client: ChatCompletionClient | None = None,
) -> DemoSummary:
    """Run the pipeline demo and optionally persist its evidence.

    Without a model client the householders stay deterministic and the demo
    needs no network. With one, all four householders report through
    `LlmHouseholderExecutor`, whose evidence is marked `llm_reported:`.
    """
    blockchain = Blockchain(storage=InMemoryStorage())
    executor = (
        LlmHouseholderExecutor(householder_model_client)
        if householder_model_client is not None
        else None
    )
    pipeline = build_pipeline(
        llm_decompose=_deterministic_decompose,
        llm_judge=_accept_all,
        blockchain=blockchain,
        worker_factories=create_householder_worker_factories(executor=executor),
    )
```
Imports: `from autogen_core.models import ChatCompletionClient`, `from agenten.household.llm_executor import LlmHouseholderExecutor`. Prüfe vorher mit `git grep -n "def create_householder_worker_factories" -A8 agenten/household/worker.py`, dass `executor=None` den deterministischen Default ergibt; sonst `executor` nur übergeben, wenn nicht `None`.

- [ ] **Step 4: CLI** — im `demo`-Subparser ergänzen:

```python
    demo_parser.add_argument(
        "--householder-executor",
        choices=("deterministic", "llm"),
        default="deterministic",
        help="llm runs the four householders on the configured provider (Nebius Nemotron by default)",
    )
```
und im `demo`-Zweig von `main()`:

```python
        model_client = None
        if args.householder_executor == "llm":
            from agenten.llm.model_client import build_model_client

            model_client = build_model_client()
        summary = asyncio.run(run_demo(args.output, householder_model_client=model_client))
```

- [ ] **Step 5: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q tests/test_demo_householder_executor.py tests/test_demo.py tests/test_householder_llm_executor.py`
Expected: alle grün

- [ ] **Step 6: Commit**

```bash
git add agenten/demo.py main.py tests/test_demo_householder_executor.py
git commit -m "feat(demo): opt-in LLM householders on the configured provider"
```

### Task 7: Konfiguration, Doku, Gate und Live-Smoke

**Files:**
- Modify: `.env.example` (Kopfblock Zeilen 1-5 und Hermes-Block)
- Modify: `README.md` (neuer Abschnitt „Running on Nebius Token Factory (NVIDIA Nemotron)“ direkt nach dem Quickstart)
- Test: `tests/test_live_nebius_provider.py` (neu, `@pytest.mark.live`)

- [ ] **Step 1: `.env.example` Kopfblock**

Die Zeilen `OPENAI_API_KEY=` und `CAPTAIN_MODEL=gpt-5.6` ersetzen durch:

```dotenv
# LLM provider: nebius (default, NVIDIA Nemotron on Nebius Token Factory),
# openai, or custom (CAPTAIN_LLM_BASE_URL, e.g. the local Claude Code shim).
CAPTAIN_LLM_PROVIDER=nebius
NEBIUS_API_KEY=
# Leave empty for the default Nemotron model; must be a Token Factory model id.
CAPTAIN_MODEL=
# Only for CAPTAIN_LLM_PROVIDER=openai
OPENAI_API_KEY=
```
Im Hermes-Block (Suche `CAPTAIN_HERMES_PROVIDER`) die Beispielwerte setzen auf:

```dotenv
CAPTAIN_HERMES_PROVIDER=custom
CAPTAIN_HERMES_MODEL=nvidia/Nemotron-3-Super-120b-a12b
CAPTAIN_HERMES_BASE_URL=https://api.tokenfactory.nebius.com/v1
```
(Modell-ID aus S1 übernehmen.)

- [ ] **Step 2: README-Abschnitt**

```markdown
## Running on Nebius Token Factory (NVIDIA Nemotron)

Captain's planner, judge, decomposition, Hermes fleet, evaluation, and
business benchmark call NVIDIA Nemotron through Nebius Token Factory's
OpenAI-compatible API by default.

1. Put `NEBIUS_API_KEY=<your key>` into the ignored `.env`.
2. Optional: set `CAPTAIN_MODEL` to another Token Factory model id; the
   default is Nemotron 3 Super.
3. Run the pipeline demo with LLM householders:
   `.\.venv\Scripts\python.exe main.py demo --householder-executor llm`

`CAPTAIN_LLM_PROVIDER=openai` or `=custom` (with `CAPTAIN_LLM_BASE_URL`)
switch back to OpenAI or a local endpoint. The offline demo
(`main.py demo`) needs no key.
```

- [ ] **Step 3: Live-Smoke-Test schreiben** — `tests/test_live_nebius_provider.py`

```python
import os

import pytest
from autogen_core.models import UserMessage

from agenten.llm.model_client import build_model_client
from config.llm_provider import resolve_provider

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_nemotron_answers_through_token_factory():
    profile = resolve_provider(os.environ)
    if profile.name != "nebius" or profile.api_key is None:
        pytest.skip("NEBIUS_API_KEY not configured")
    client = build_model_client(profile=profile)
    try:
        result = await client.create([UserMessage(content="Reply with the word ok.", source="user")])
    finally:
        await client.close()
    assert "ok" in str(result.content).lower()
```

- [ ] **Step 4: Volles Offline-Gate**

Run:
```powershell
C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m compileall -q agenten blockchain chats config gateway
C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q -m "not live" -p no:cacheprovider
C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe scripts/verify_submission.py
```
Expected: compileall ohne Ausgabe; Fitness/Boundary grün; Suite grün bis auf die Altlasten aus Global Constraints; `Submission evidence check passed.` Weitere rote Tests, die `gpt-5.6` als Default erwarten (`git grep -n "gpt-5.6" tests`), auf `NEMOTRON_DEFAULT_MODEL` bzw. explizites `CAPTAIN_LLM_PROVIDER=openai` umstellen und erneut laufen lassen.

- [ ] **Step 5: Live-Smoke (nur nach Freigabe im Chat)**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q --no-cov -m live tests/test_live_nebius_provider.py` aus dem Hauptcheckout-`.env`-Kontext (Arbeitsverzeichnis mit `.env`, die `NEBIUS_API_KEY` enthält).
Expected: 1 passed

- [ ] **Step 6: Commit**

```bash
git add .env.example README.md tests/test_live_nebius_provider.py
git commit -m "docs: document Nebius Token Factory setup and add live Nemotron smoke"
```
