from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "configure-official-n8n-skills.ps1"
PIN = "046c330c9308bbfc54ceab1adbe3d8fc6bebc8fa"
OFFICIAL_SKILLS = {
    "using-n8n-skills-official",
    "n8n-workflow-lifecycle-official",
    "n8n-node-configuration-official",
    "n8n-agents-official",
    "n8n-error-handling-official",
    "n8n-credentials-and-security-official",
    "n8n-debugging-official",
    "n8n-code-nodes-official",
    "n8n-expressions-official",
    "n8n-loops-official",
    "n8n-subworkflows-official",
    "n8n-binary-and-data-official",
    "n8n-data-tables-official",
    "n8n-extending-mcp-official",
}


FAKE_CODEX = r'''from __future__ import annotations
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["FAKE_CODEX_STATE"])
state.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
with (state / "calls.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")

if args == ["--version"]:
    print("codex-cli 0.144.5")
elif args == ["plugin", "marketplace", "list", "--json"]:
    marketplaces = []
    if (state / "marketplace").exists():
        marketplaces.append({
            "name": "n8n-io",
            "root": str(state / "marketplace"),
            "marketplaceSource": {
                "sourceType": "git",
                "source": "https://github.com/n8n-io/skills.git",
            },
        })
    print(json.dumps({"marketplaces": marketplaces}))
elif args[:3] == ["plugin", "marketplace", "add"]:
    (state / "marketplace").mkdir(exist_ok=True)
    skills = json.loads(os.environ["FAKE_OFFICIAL_SKILLS"])
    for skill in skills:
        directory = state / "marketplace" / "skills" / skill
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(f"---\nname: {skill}\n---\n", encoding="utf-8")
    print(json.dumps({"name": "n8n-io"}))
elif args == ["plugin", "list"]:
    if (state / "plugin").exists():
        print("n8n-skills@n8n-io installed, enabled 1.1.0 /fake")
    elif (state / "marketplace").exists():
        print("n8n-skills@n8n-io not installed /fake")
elif args[:2] == ["plugin", "add"]:
    (state / "plugin").write_text("installed", encoding="utf-8")
    print(json.dumps({"installed": True}))
elif args == ["mcp", "get", "n8n", "--json"]:
    url = os.environ.get("FAKE_N8N_MCP_URL", "http://localhost:5679/mcp-server/http")
    print(json.dumps({
        "name": "n8n",
        "enabled": True,
        "transport": {
            "type": "streamable_http",
            "url": url,
            "bearer_token_env_var": "N8N_MCP_TOKEN",
        },
    }))
else:
    print(f"unsupported fake codex call: {args}", file=sys.stderr)
    raise SystemExit(2)
'''

FAKE_HERMES = r'''from __future__ import annotations
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["FAKE_CODEX_STATE"])
args = sys.argv[1:]
hermes_home = os.environ.get("HERMES_HOME")
config = (Path(hermes_home) / "hermes_dirs.json") if hermes_home else (state / "hermes_dirs.json")
if args == ["config", "get", "skills.external_dirs", "--json"]:
    print(config.read_text(encoding="utf-8") if config.exists() else json.dumps(["C:/existing-skills"]))
elif args[:3] == ["config", "set", "skills.external_dirs"]:
    raw = args[3]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw.removeprefix("[").removesuffix("]").split(",")
    config.write_text(json.dumps(parsed), encoding="utf-8")
else:
    print(f"unsupported fake hermes call: {args}", file=sys.stderr)
    raise SystemExit(2)
'''

FAKE_GIT = r'''from __future__ import annotations
import os
print(os.environ["FAKE_MARKETPLACE_PIN"])
'''


def _fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    command = tmp_path / "codex.cmd"
    command.write_text(f'@"{os.fspath(Path(shutil.which("python") or "python"))}" "{script}" %*\n', encoding="utf-8")
    return command


def _fake_command(tmp_path: Path, name: str, source: str) -> Path:
    script = tmp_path / f"fake_{name}.py"
    script.write_text(source, encoding="utf-8")
    command = tmp_path / f"{name}.cmd"
    command.write_text(f'@"{os.fspath(Path(shutil.which("python") or "python"))}" "{script}" %*\n', encoding="utf-8")
    return command


def _run(
    tmp_path: Path, *, mcp_url: str, marketplace_pin: str = PIN
) -> subprocess.CompletedProcess[str]:
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is required")
    fake = _fake_codex(tmp_path)
    fake_hermes = _fake_command(tmp_path, "hermes", FAKE_HERMES)
    fake_git = _fake_command(tmp_path, "git", FAKE_GIT)
    env = dict(os.environ)
    env["FAKE_CODEX_STATE"] = str(tmp_path / "state")
    env["FAKE_N8N_MCP_URL"] = mcp_url
    env["FAKE_OFFICIAL_SKILLS"] = json.dumps(sorted(OFFICIAL_SKILLS))
    env["FAKE_MARKETPLACE_PIN"] = marketplace_pin
    return subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-RepositoryRoot",
            str(ROOT),
            "-CodexExecutable",
            str(fake),
            "-HermesExecutable",
            str(fake_hermes),
            "-GitExecutable",
            str(fake_git),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def test_configures_pinned_official_plugin_idempotently(tmp_path: Path) -> None:
    first = _run(tmp_path, mcp_url="http://localhost:5679/mcp-server/http")
    second = _run(tmp_path, mcp_url="http://localhost:5679/mcp-server/http")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    calls = [
        json.loads(line)
        for line in (tmp_path / "state" / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [
        "plugin",
        "marketplace",
        "add",
        "n8n-io/skills",
        "--ref",
        PIN,
        "--json",
    ] in calls
    assert calls.count(["plugin", "add", "n8n-skills@n8n-io", "--json"]) == 1
    hermes_dirs = json.loads((tmp_path / "state" / "hermes_dirs.json").read_text(encoding="utf-8"))
    assert hermes_dirs[0] == "C:/existing-skills"
    official_root = Path(hermes_dirs[1])
    assert official_root.name == "skills"
    assert {path.name for path in official_root.iterdir()} == OFFICIAL_SKILLS
    assert "already configured" in second.stdout.lower()


def test_rejects_existing_n8n_mcp_with_wrong_url(tmp_path: Path) -> None:
    result = _run(tmp_path, mcp_url="http://localhost:15678/mcp-server/http")

    assert result.returncode != 0
    assert "mcp url mismatch" in (result.stdout + result.stderr).lower()


def test_rejects_marketplace_checkout_at_wrong_commit(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        mcp_url="http://localhost:5679/mcp-server/http",
        marketplace_pin="f" * 40,
    )

    assert result.returncode != 0
    assert "marketplace commit mismatch" in (result.stdout + result.stderr).lower()
