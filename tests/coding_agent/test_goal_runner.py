import json
from decimal import Decimal

import pytest

from agenten.coding_agent.contracts import AgentGoalV1
from agenten.coding_agent.goal_runner import (
    ClaudeGoalRunner,
    ClaudeRunnerSettings,
    build_claude_env,
    build_claude_shell,
)
from tests.coding_agent.fakes import FakeSandbox, ok

SETTINGS = ClaudeRunnerSettings(
    relay_url="https://relay.example/anthropic", model="nvidia/super", small_model="nvidia/nano"
)
TOKEN = "run-token-secret"


def _goal(**overrides):
    values = dict(
        goal_id="g-1", objective="make tests pass", acceptance_commands=("pytest -q",),
        allowed_paths=("src/",), max_rounds=3, max_turns_per_round=10,
        budget_usd=Decimal("1"), timeout_seconds=600,
    )
    values.update(overrides)
    return AgentGoalV1(**values)


def _stream(session="s-1", subtype="success"):
    records = [{"type": "system", "subtype": "init", "session_id": session}]
    if subtype is not None:
        records.append({"type": "result", "subtype": subtype, "is_error": subtype != "success",
                        "num_turns": 2, "session_id": session, "usage": {"input_tokens": 5, "output_tokens": 3}})
    return "\n".join(json.dumps(r) for r in records)


class _Budget:
    def __init__(self, exhausted=False):
        self.value = exhausted

    def exhausted(self):
        return self.value


def _manifest(entries):
    return "".join(f"{digest}  ./{path}\0" for path, digest in sorted(entries.items()))


class _Script:
    """Answers claude runs, workspace manifests and acceptance by command shape."""

    def __init__(self, acceptance_exit_codes, extra_after=None, claude_stdout=None, timed_out=False):
        self.acceptance = list(acceptance_exit_codes)
        self.extra_after = dict(extra_after or {})
        self.claude_stdout = claude_stdout or _stream()
        self.timed_out = timed_out
        self.claude_runs = 0
        self.acceptance_images = []

    def __call__(self, image, command):
        if command.shell.startswith("cd /work && claude "):
            self.claude_runs += 1
            return ok(self.claude_stdout, image=f"img-r{self.claude_runs}", timed_out=self.timed_out)
        if "sha256sum" in command.shell:
            if image == "img-0":
                entries = {"src/app.py": "a" * 64}
            else:
                entries = {"src/app.py": "b" * 64, **self.extra_after}
            return ok(_manifest(entries), image="discard")
        self.acceptance_images.append(image)
        return ok("tail", image="discard", exit_code=self.acceptance.pop(0))


def _runner(script, budget=None, clock=None):
    sandbox = FakeSandbox(script)
    runner = ClaudeGoalRunner(sandbox=sandbox, settings=SETTINGS, budget=budget or _Budget(),
                              clock=clock or (lambda: 0.0))
    return runner, sandbox


def test_shell_and_env_are_exact():
    shell = build_claude_shell(SETTINGS, max_turns=7, resume_session="s-9")
    assert shell == (
        "cd /work && claude -p --output-format stream-json --verbose --model nvidia/super "
        "--max-turns 7 --dangerously-skip-permissions --allowedTools Bash,Read,Edit,Write,Glob,Grep "
        "--setting-sources '' --strict-mcp-config --resume s-9"
    )
    assert build_claude_env(SETTINGS, TOKEN) == {
        "ANTHROPIC_BASE_URL": "https://relay.example/anthropic",
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "ANTHROPIC_MODEL": "nvidia/super",
        "ANTHROPIC_SMALL_FAST_MODEL": "nvidia/nano",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    }


@pytest.mark.parametrize("url", ["http://relay.example", "ftp://x", "https://"])
def test_settings_reject_unsafe_relay_urls(url):
    with pytest.raises(ValueError):
        ClaudeRunnerSettings(relay_url=url, model="m", small_model="n")


def test_settings_allow_local_http_for_development():
    ClaudeRunnerSettings(relay_url="http://127.0.0.1:8787", model="m", small_model="n")


@pytest.mark.asyncio
async def test_red_then_green_resumes_the_same_session():
    script = _Script(acceptance_exit_codes=[1, 0])
    runner, sandbox = _runner(script)

    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)

    assert result.status == "achieved"
    assert [r.round for r in result.rounds] == [1, 2]
    assert result.final_image_id == "img-r2"
    claude_calls = [c for _, c in sandbox.calls if " claude " in c.shell]
    assert "--resume" not in claude_calls[0].shell
    assert claude_calls[1].shell.endswith("--resume s-1")
    assert "make tests pass" in claude_calls[0].stdin
    assert "`pytest -q` exited 1" in claude_calls[1].stdin
    assert sandbox.calls[0][0] == "img-0"
    assert TOKEN not in result.model_dump_json()
    assert TOKEN not in repr(result)
    assert script.acceptance_images == ["img-r1", "img-r2"]
    assert "img-0" not in script.acceptance_images
    for _, command in sandbox.calls:
        assert TOKEN not in command.shell
        if not command.shell.startswith("cd /work && claude "):
            assert dict(command.env) == {}
    assert dict(claude_calls[0].env)["ANTHROPIC_AUTH_TOKEN"] == TOKEN


@pytest.mark.asyncio
async def test_exhausts_after_max_rounds():
    runner, _ = _runner(_Script(acceptance_exit_codes=[1, 1]))
    result = await runner.run(_goal(max_rounds=2), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "exhausted"
    assert len(result.rounds) == 2


@pytest.mark.asyncio
async def test_changes_outside_allowed_paths_stop_without_acceptance():
    script = _Script(acceptance_exit_codes=[], extra_after={"tests/test_app.py": "c" * 64})
    runner, _ = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "policy_violation"
    assert result.rounds[0].policy_violations == ("tests/test_app.py",)
    assert result.rounds[0].acceptance is None
    assert "tests/test_app.py" in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_protected_judge_file_is_a_violation_even_under_dot():
    script = _Script(acceptance_exit_codes=[], extra_after={"conftest.py": "c" * 64})
    runner, _ = _runner(script)
    result = await runner.run(_goal(allowed_paths=(".",)), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "policy_violation"
    assert result.rounds[0].policy_violations == ("conftest.py",)
    assert result.rounds[0].acceptance is None


@pytest.mark.asyncio
async def test_protected_file_exempt_when_listed_exactly():
    script = _Script(acceptance_exit_codes=[0], extra_after={"pyproject.toml": "c" * 64})
    runner, _ = _runner(script)
    result = await runner.run(
        _goal(allowed_paths=("src/", "pyproject.toml")), workspace_image_id="img-0", run_token=TOKEN
    )
    assert result.status == "achieved"


@pytest.mark.asyncio
async def test_round_records_changed_paths_between_input_and_output_images():
    script = _Script(acceptance_exit_codes=[0])
    runner, sandbox = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    first = result.rounds[0]
    assert first.changed_paths == ("src/app.py",)
    assert first.input_image_id == "img-0"
    assert first.output_image_id == "img-r1"
    manifest_images = [image for image, c in sandbox.calls if "sha256sum" in c.shell]
    assert "img-0" in manifest_images
    assert "img-r1" in manifest_images


@pytest.mark.asyncio
async def test_stream_without_session_fails_without_further_rounds():
    script = _Script(acceptance_exit_codes=[], claude_stdout="relay error: unauthorized")
    runner, _ = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "failed"
    assert len(result.rounds) == 1
    assert script.claude_runs == 1


@pytest.mark.asyncio
async def test_claude_timeout_ends_the_goal():
    runner, _ = _runner(_Script(acceptance_exit_codes=[], timed_out=True))
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "timeout"
    assert result.rounds[0].timed_out is True


@pytest.mark.asyncio
async def test_budget_exhausted_before_first_round():
    script = _Script(acceptance_exit_codes=[])
    runner, _ = _runner(script, budget=_Budget(exhausted=True))
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "budget_exhausted"
    assert result.rounds == ()
    assert result.final_image_id == "img-0"
    assert script.claude_runs == 0


@pytest.mark.asyncio
async def test_goal_timeout_stops_before_next_round():
    times = iter([0.0, 0.0, 700.0])
    runner, _ = _runner(_Script(acceptance_exit_codes=[1]), clock=lambda: next(times))
    result = await runner.run(_goal(timeout_seconds=600), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "timeout"
    assert len(result.rounds) == 1


@pytest.mark.parametrize("model", ["-rf", "--help", "-x/y"])
def test_settings_reject_model_ids_that_look_like_flags(model):
    with pytest.raises(ValueError):
        ClaudeRunnerSettings(relay_url="https://relay.example", model=model, small_model="n")
    with pytest.raises(ValueError):
        ClaudeRunnerSettings(relay_url="https://relay.example", model="m", small_model=model)


@pytest.mark.parametrize("url", ["https://relay.example/x#frag", "https://relay.example/x#"])
def test_settings_reject_relay_urls_with_fragment(url):
    with pytest.raises(ValueError):
        ClaudeRunnerSettings(relay_url=url, model="m", small_model="n")


@pytest.mark.asyncio
async def test_token_in_changed_path_is_a_violation_and_is_redacted():
    script = _Script(acceptance_exit_codes=[], extra_after={"src/" + TOKEN: "c" * 64})
    runner, sandbox = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "policy_violation"
    assert result.rounds[0].acceptance is None
    assert result.rounds[0].policy_violations == ("src/<redacted>",)
    assert "src/<redacted>" in result.rounds[0].changed_paths
    dumped = result.model_dump_json()
    assert TOKEN not in dumped
    assert TOKEN not in repr(result)
    assert "<redacted>" in dumped
    assert TOKEN not in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_token_in_stream_session_is_redacted_and_ends_the_goal():
    script = _Script(acceptance_exit_codes=[], claude_stdout=_stream(session="s-" + TOKEN))
    runner, sandbox = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "policy_violation"
    assert result.rounds[0].session_id == "s-<redacted>"
    assert script.claude_runs == 1
    assert script.acceptance_images == []
    assert TOKEN not in result.model_dump_json()
    assert TOKEN not in repr(result)
    assert all(TOKEN not in command.shell for _, command in sandbox.calls)


@pytest.mark.asyncio
async def test_token_in_stream_result_subtype_is_redacted():
    script = _Script(acceptance_exit_codes=[1, 1], claude_stdout=_stream(subtype="error_" + TOKEN))
    runner, _ = _runner(script)
    result = await runner.run(_goal(max_rounds=2), workspace_image_id="img-0", run_token=TOKEN)
    assert TOKEN not in result.model_dump_json()
    assert TOKEN not in repr(result)
    assert result.rounds[0].result_subtype == "error_<redacted>"


@pytest.mark.asyncio
async def test_token_in_acceptance_output_is_redacted():
    class _Leaky(_Script):
        def __call__(self, image, command):
            if "sha256sum" in command.shell or command.shell.startswith("cd /work && claude "):
                return super().__call__(image, command)
            self.acceptance_images.append(image)
            return ok("leak " + TOKEN, image="discard", exit_code=self.acceptance.pop(0))

    script = _Leaky(acceptance_exit_codes=[1, 0])
    runner, _ = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "achieved"
    assert TOKEN not in result.model_dump_json()
    assert "leak <redacted>" in result.rounds[0].acceptance.checks[0].output_tail


# --- I1: symlinks in the workspace --------------------------------------------------------


class _LinkScript(_Script):
    """Adds symlink records to the manifests (before: base_links, after: base_links + new_links)."""

    def __init__(self, acceptance_exit_codes, base_links=(), new_links=()):
        super().__init__(acceptance_exit_codes)
        self.base_links = tuple(base_links)
        self.new_links = tuple(new_links)

    def __call__(self, image, command):
        result = super().__call__(image, command)
        if "sha256sum" not in command.shell:
            return result
        links = self.base_links + (self.new_links if image != "img-0" else ())
        records = "".join(f"L\0./{path}\0{kind}\0{target}\0" for path, target, kind in links)
        return ok(result.stdout + records, image="discard")


@pytest.mark.asyncio
async def test_preexisting_directory_symlink_does_not_break_the_goal():
    script = _LinkScript([0], base_links=[(".venv/lib64", "lib", "d")])
    runner, _ = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "achieved"
    assert result.rounds[0].changed_paths == ("src/app.py",)


@pytest.mark.asyncio
async def test_new_symlink_escaping_the_workdir_is_a_violation():
    script = _LinkScript([], new_links=[("src/evil", "/usr/lib/python3/site-packages", "d")])
    runner, _ = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "policy_violation"
    assert result.rounds[0].policy_violations == ("src/evil",)
    assert result.rounds[0].acceptance is None
    assert script.acceptance_images == []


@pytest.mark.asyncio
async def test_new_relative_file_symlink_inside_allowed_paths_is_fine():
    script = _LinkScript([0], new_links=[("src/alias.py", "app.py", "f")])
    runner, _ = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "achieved"
    assert result.rounds[0].changed_paths == ("src/alias.py", "src/app.py")
