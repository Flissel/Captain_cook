from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from typing import BinaryIO

import pytest

from agenten.execution import codex_supervisor
from agenten.execution.codex_policy import AuthorizedCodexRun, FrozenEnvironment
from agenten.execution.codex_supervisor import PowerShellCodexRunner


FUTURE_DEADLINE = datetime(2100, 1, 1, tzinfo=timezone.utc)


class _CompletedProcess:
    def __init__(
        self,
        stdout_chunks: tuple[bytes, ...] = (),
        *,
        stdout: object | None = None,
    ) -> None:
        self.returncode: int | None = None
        if stdout is None:
            stream = asyncio.StreamReader()
            for chunk in stdout_chunks:
                stream.feed_data(chunk)
            stream.feed_eof()
            stdout = stream
        self.stdout = stdout
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class _RunningProcess:
    def __init__(self, stdout: object) -> None:
        self.returncode: int | None = None
        self.stdout = stdout
        self.stderr = asyncio.StreamReader()
        self._stopped = asyncio.Event()
        self.active_waiters = 0

    @property
    def alive(self) -> bool:
        return not self._stopped.is_set()

    async def wait(self) -> int:
        self.active_waiters += 1
        try:
            await self._stopped.wait()
            assert self.returncode is not None
            return self.returncode
        finally:
            self.active_waiters -= 1

    def stop(self, exit_code: int = -9) -> None:
        self.returncode = exit_code
        self._stopped.set()
        self.stderr.feed_eof()

    def kill(self) -> None:
        self.stop()


class _FailingReadStream:
    async def read(self, size: int) -> bytes:
        del size
        raise OSError("private stdout read detail")


class _ChunkedStream:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = iter(chunks)

    async def read(self, size: int) -> bytes:
        chunk = next(self._chunks, b"")
        assert len(chunk) <= size
        return chunk


class _FailingJournal:
    def __init__(self, journal: BinaryIO) -> None:
        self._journal = journal

    def __enter__(self) -> _FailingJournal:
        self._journal.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self._journal.__exit__(*args)

    def write(self, data: bytes) -> int:
        del data
        raise OSError("private journal write detail")

    def flush(self) -> None:
        self._journal.flush()

    def fileno(self) -> int:
        return self._journal.fileno()


def _runner(
    tmp_path: Path,
    *,
    max_jsonl_record_bytes: int | None = None,
) -> PowerShellCodexRunner:
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(exist_ok=True)
    kwargs: dict[str, object] = {}
    if max_jsonl_record_bytes is not None:
        kwargs["max_jsonl_record_bytes"] = max_jsonl_record_bytes
    return PowerShellCodexRunner(
        pwsh_path=Path(pwsh),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=Path(r"C:\Windows\System32\timeout.exe"),
        session_id="jsonl-stream-test",
        state_path=tmp_path / "process-state.json",
        journal_path=tmp_path / "private" / "session.jsonl",
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
        timeout_seconds=5,
        **kwargs,
    )


def _authorized(tmp_path: Path) -> AuthorizedCodexRun:
    return AuthorizedCodexRun(
        workspace=tmp_path,
        command=("codex", "exec", "--json", "harmless stream test"),
        environment=FrozenEnvironment({"PATH": "safe"}),
    )


@pytest.mark.asyncio
async def test_runner_persists_valid_jsonl_record_larger_than_streamreader_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = json.dumps(
        {"type": "item.completed", "payload": "x" * (70 * 1024)},
        separators=(",", ":"),
    )
    stream = asyncio.StreamReader()
    stream.feed_data(record.encode() + b"\n")
    process = _RunningProcess(stream)

    async def create_process(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return process

    monkeypatch.setattr(
        codex_supervisor.asyncio, "create_subprocess_exec", create_process
    )

    runner = _runner(tmp_path)
    run = asyncio.create_task(runner.run(_authorized(tmp_path)))
    expected_journal = record.encode() + b"\n"
    for _ in range(100):
        if (
            runner._journal_path.is_file()
            and runner._journal_path.read_bytes() == expected_journal
        ):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("large JSONL record was not persisted while child was active")

    assert process.alive is True
    process.stop(0)
    stream.feed_eof()
    result = await run

    assert result.terminal_status == "succeeded"
    assert result.jsonl_lines == (record,)
    assert result.journal_path.read_bytes() == expected_journal


@pytest.mark.asyncio
async def test_runner_handles_chunk_boundaries_multiple_records_and_crlf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = b'{"type":"thread.started","thread_id":"one"}'
    second = b'{"type":"turn.started"}'
    third = b'{"type":"turn.completed"}'
    process = _CompletedProcess(
        stdout=_ChunkedStream(
            (
                first[:7],
                first[7:] + b"\r",
                b"\n" + second + b"\n\n" + third[:4],
                third[4:] + b"\r\n",
            )
        )
    )

    async def create_process(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return process

    monkeypatch.setattr(
        codex_supervisor.asyncio, "create_subprocess_exec", create_process
    )

    result = await _runner(tmp_path).run(_authorized(tmp_path))

    assert result.jsonl_lines == tuple(item.decode() for item in (first, second, third))
    assert result.journal_path.read_bytes() == b"\n".join((first, second, third, b""))


@pytest.mark.asyncio
async def test_runner_accepts_exact_record_limit_with_crlf_terminator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = b"x" * 32
    process = _CompletedProcess((record + b"\r\n",))

    async def create_process(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return process

    monkeypatch.setattr(
        codex_supervisor.asyncio, "create_subprocess_exec", create_process
    )

    result = await _runner(tmp_path, max_jsonl_record_bytes=32).run(
        _authorized(tmp_path)
    )

    assert result.jsonl_lines == (record.decode(),)
    assert result.journal_path.read_bytes() == record + b"\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error_name", "message"),
    [
        (
            b"x" * 33 + b"\n",
            "CodexJsonlRecordTooLargeError",
            "Codex JSONL record exceeds configured safe limit",
        ),
        (
            b'{"type":"turn.started"}',
            "CodexJsonlUnterminatedRecordError",
            "Codex JSONL stream ended with an unterminated record",
        ),
    ],
    ids=("over-limit", "unterminated"),
)
async def test_runner_rejects_unsafe_or_unterminated_record_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    error_name: str,
    message: str,
) -> None:
    process = _CompletedProcess((payload,))

    async def create_process(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return process

    monkeypatch.setattr(
        codex_supervisor.asyncio, "create_subprocess_exec", create_process
    )
    error_type = getattr(codex_supervisor, error_name)

    with pytest.raises(error_type, match=message) as error:
        await _runner(tmp_path, max_jsonl_record_bytes=32).run(_authorized(tmp_path))

    assert payload.decode(errors="replace") not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("read", "write", "fsync"))
async def test_reader_or_journal_failure_cancels_running_process_and_leaks_no_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    stdout: object
    if failure == "read":
        stdout = _FailingReadStream()
        expected_error_name = "CodexOutputReadError"
    else:
        stream = asyncio.StreamReader()
        stream.feed_data(b'{"type":"turn.started"}\n')
        stdout = stream
        expected_error_name = "CodexJournalPersistenceError"
    process = _RunningProcess(stdout)
    cancellation_calls = 0

    async def create_process(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return process

    async def cancel_for_evidence_failure() -> bool:
        nonlocal cancellation_calls
        cancellation_calls += 1
        process.stop()
        return True

    monkeypatch.setattr(
        codex_supervisor.asyncio, "create_subprocess_exec", create_process
    )
    runner = _runner(tmp_path)
    monkeypatch.setattr(
        runner,
        "_attempt_verified_evidence_failure_cancellation",
        cancel_for_evidence_failure,
        raising=False,
    )
    if failure == "fsync":

        def fsync_fails(file_descriptor: int) -> None:
            del file_descriptor
            raise OSError("private journal fsync detail")

        monkeypatch.setattr(codex_supervisor.os, "fsync", fsync_fails)
    elif failure == "write":
        original_open = Path.open
        journal_path = runner._journal_path

        def open_with_write_failure(
            path: Path, *args: object, **kwargs: object
        ) -> BinaryIO | _FailingJournal:
            journal = original_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if path.resolve() == journal_path and mode == "ab":
                return _FailingJournal(journal)
            return journal

        monkeypatch.setattr(Path, "open", open_with_write_failure)

    error_type = getattr(codex_supervisor, expected_error_name)
    with pytest.raises(error_type) as error:
        await asyncio.wait_for(
            runner.run(_authorized(tmp_path)),
            timeout=1,
        )

    await asyncio.sleep(0)
    assert cancellation_calls == 1
    assert process.alive is False
    assert process.active_waiters == 0
    assert "private" not in str(error.value)
    leaked = {
        task.get_coro().__qualname__
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and (
            "_journal_stdout" in task.get_coro().__qualname__
            or "_drain_stderr" in task.get_coro().__qualname__
        )
    }
    assert leaked == set()
