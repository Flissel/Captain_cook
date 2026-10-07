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
async def test_changed_paths_detects_added_removed_modified():
    def handler(image, command):
        if "sha256sum" in command.shell:
            if image == "img-before":
                # src/old.py removed, src/app.py modified, tests/existing.py unchanged
                return ok("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2  ./src/old.py\0"
                          "0000000000000000000000000000000000000000000000000000000000000000  ./src/app.py\0"
                          "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff  ./tests/existing.py\0")
            else:  # img-after
                # src/app.py modified (different hash), tests/new.py added, tests/existing.py unchanged
                return ok("9999999999999999999999999999999999999999999999999999999999999999  ./src/app.py\0"
                          "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  ./tests/new.py\0"
                          "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff  ./tests/existing.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    paths = await changed_paths(sandbox, before_image_id="img-before", after_image_id="img-after", workdir="/work")
    # src/old.py (removed), src/app.py (modified), tests/new.py (added) — existing.py not included
    assert paths == ("src/app.py", "src/old.py", "tests/new.py")


@pytest.mark.asyncio
async def test_changed_paths_handles_renames_reports_both_paths():
    def handler(image, command):
        if "sha256sum" in command.shell:
            if image == "before":
                return ok("abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123abc1  ./old_name.py\0")
            else:  # after
                return ok("abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123abc1  ./new_name.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    paths = await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/app")
    # Rename: old_name.py is gone (removed), new_name.py is added — report both
    assert paths == ("new_name.py", "old_name.py")


@pytest.mark.asyncio
async def test_changed_paths_handles_special_filenames():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Filenames: space, literal " -> ", non-ASCII, double quote
            manifest = ("1111111111111111111111111111111111111111111111111111111111111111  ./file with spaces.py\0"
                        "2222222222222222222222222222222222222222222222222222222222222222  ./forbidden -> src/ok.py\0"
                        "3333333333333333333333333333333333333333333333333333333333333333  ./café.py\0"
                        "4444444444444444444444444444444444444444444444444444444444444444  ./\"quoted.txt\0")
            if image == "before":
                return ok(manifest)
            else:  # after: all changed
                return ok("")
        return ok()

    sandbox = FakeSandbox(handler)
    paths = await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")
    # All four files are removed (changed)
    assert sorted(paths) == sorted(("file with spaces.py", "forbidden -> src/ok.py", "café.py", "\"quoted.txt"))


@pytest.mark.asyncio
async def test_changed_paths_excludes_caches():
    def handler(image, command):
        if "sha256sum" in command.shell:
            if image == "before":
                return ok("")
            else:
                manifest = ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  ./__pycache__/foo.pyc\0"
                            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  ./src/app.pyc\0"
                            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc  ./src/.pytest_cache/v\0"
                            "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd  ./src/app.py\0")
                return ok(manifest)
        return ok()

    sandbox = FakeSandbox(handler)
    paths = await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")
    # Cache files (.pyc, __pycache__, .pytest_cache) are excluded; only app.py is reported
    assert paths == ("src/app.py",)


@pytest.mark.asyncio
async def test_changed_paths_validates_manifest_format():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Malformed: no "  " separator (only one space)
            return ok("abcd1234 ./file.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_validates_hash_length():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Malformed: hash is too short (not 64 hex chars)
            return ok("abc123  ./file.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_validates_path_prefix():
    def handler(image, command):
        if "sha256sum" in command.shell:
            # Malformed: path doesn't start with "./"
            return ok("1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef  file.py\0")
        return ok()

    sandbox = FakeSandbox(handler)
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_raises_on_manifest_exit_code():
    sandbox = FakeSandbox(lambda image, command: ok("error", exit_code=1) if "sha256sum" in command.shell else ok())
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_raises_on_manifest_timeout():
    sandbox = FakeSandbox(lambda image, command: ok(timed_out=True) if "sha256sum" in command.shell else ok())
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, before_image_id="before", after_image_id="after", workdir="/work")


@pytest.mark.asyncio
async def test_changed_paths_invokes_find_sha256sum_correctly():
    calls_made = []
    def handler(image, command):
        calls_made.append(command.shell)
        if "sha256sum" in command.shell:
            return ok("")
        return ok()

    sandbox = FakeSandbox(handler)
    await changed_paths(sandbox, before_image_id="img-1", after_image_id="img-2", workdir="/test/path")

    # Verify the exact manifest command is used
    manifest_calls = [c for c in calls_made if "sha256sum" in c]
    assert len(manifest_calls) == 2
    for call in manifest_calls:
        assert "find . -path ./.git -prune" in call
        assert "-print0" in call
        assert "sha256sum -z" in call
        assert "cd /test/path" in call


def test_protected_paths_identifies_judge_rigging_files():
    from agenten.coding_agent.acceptance import protected_paths

    changed = ("src/a.py", "tests/conftest.py", "pytest.ini", "lib/x.pth", "sitecustomize.py", "normal.py")
    protected = protected_paths(changed)
    assert sorted(protected) == sorted(("tests/conftest.py", "pytest.ini", "lib/x.pth", "sitecustomize.py"))


def test_protected_paths_handles_nested_paths():
    from agenten.coding_agent.acceptance import protected_paths

    changed = ("build/conftest.py", "src/pytest.ini", "lib/deep/x.pth")
    protected = protected_paths(changed)
    # Only the basename is checked
    assert sorted(protected) == sorted(("build/conftest.py", "src/pytest.ini", "lib/deep/x.pth"))


def test_sandbox_command_repr_hides_env_and_stdin():
    command = SandboxCommand(shell="claude -p", timeout_seconds=60, env={"ANTHROPIC_AUTH_TOKEN": "tok-secret"}, stdin="prompt")
    assert "tok-secret" not in repr(command)
    assert "prompt" not in repr(command)
