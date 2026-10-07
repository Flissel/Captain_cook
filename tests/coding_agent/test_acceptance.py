import pytest

from agenten.coding_agent.acceptance import changed_paths, run_acceptance
from agenten.coding_agent.sandbox import SandboxCommand, SandboxProtocolError
from tests.coding_agent.fakes import FakeSandbox, ok


@pytest.mark.asyncio
async def test_each_command_runs_in_workdir_and_is_judged_by_exit_code():
    def handler(image, command):
        if "fails" in command.shell:
            return ok("E" * 5000, exit_code=1)
        return ok("passed")

    sandbox = FakeSandbox(handler)
    report = await run_acceptance(
        sandbox, image_id="img-1", commands=("pytest -q a", "pytest -q fails"), workdir="/work", timeout_seconds=60
    )

    assert [c.passed for c in report.checks] == [True, False]
    assert report.passed is False
    assert len(report.checks[1].output_tail) == 2000
    assert all(img == "img-1" for img, _ in sandbox.calls)
    assert sandbox.calls[0][1].shell == "cd /work && pytest -q a"
    assert sandbox.calls[0][1].env == {}


@pytest.mark.asyncio
async def test_timeout_fails_the_check():
    sandbox = FakeSandbox(lambda image, command: ok(exit_code=0, timed_out=True))
    report = await run_acceptance(sandbox, image_id="i", commands=("x",), workdir="/work", timeout_seconds=5)
    assert report.checks[0].passed is False


@pytest.mark.asyncio
async def test_changed_paths_parses_porcelain_including_renames_and_quotes():
    porcelain = ' M src/app.py\n?? tests/test_new.py\nR  old.py -> src/new.py\n?? "weird name.txt"\n'
    sandbox = FakeSandbox(lambda image, command: ok(porcelain))
    paths = await changed_paths(sandbox, image_id="i", workdir="/work")
    assert paths == ("src/app.py", "src/new.py", "tests/test_new.py", "weird name.txt")
    assert sandbox.calls[0][1].shell == "git -C /work status --porcelain=v1 -uall"


@pytest.mark.asyncio
async def test_changed_paths_requires_a_git_workspace():
    sandbox = FakeSandbox(lambda image, command: ok("fatal: not a git repository", exit_code=128))
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, image_id="i", workdir="/work")


def test_sandbox_command_repr_hides_env_and_stdin():
    command = SandboxCommand(shell="claude -p", timeout_seconds=60, env={"ANTHROPIC_AUTH_TOKEN": "tok-secret"}, stdin="prompt")
    assert "tok-secret" not in repr(command)
    assert "prompt" not in repr(command)
