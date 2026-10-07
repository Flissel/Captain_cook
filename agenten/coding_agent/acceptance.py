"""Independent acceptance checks and change detection inside the sandbox."""
from __future__ import annotations

import shlex
from collections.abc import Sequence

from agenten.coding_agent.contracts import AcceptanceCheckV1, AcceptanceReportV1
from agenten.coding_agent.sandbox import SandboxCommand, SandboxPort, SandboxProtocolError

OUTPUT_TAIL_CHARS = 2000


async def run_acceptance(
    sandbox: SandboxPort,
    *,
    image_id: str,
    commands: Sequence[str],
    workdir: str,
    timeout_seconds: int,
) -> AcceptanceReportV1:
    checks: list[AcceptanceCheckV1] = []
    for command in commands:
        result = await sandbox.run(
            image_id,
            SandboxCommand(shell=f"cd {shlex.quote(workdir)} && {command}", timeout_seconds=timeout_seconds),
        )
        checks.append(
            AcceptanceCheckV1(
                command=command,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                passed=result.exit_code == 0 and not result.timed_out,
                output_tail=(result.stdout + result.stderr)[-OUTPUT_TAIL_CHARS:],
            )
        )
    return AcceptanceReportV1(checks=tuple(checks))


async def changed_paths(sandbox: SandboxPort, *, image_id: str, workdir: str) -> tuple[str, ...]:
    result = await sandbox.run(
        image_id,
        SandboxCommand(shell=f"git -C {shlex.quote(workdir)} status --porcelain=v1 -uall", timeout_seconds=60),
    )
    if result.exit_code != 0 or result.timed_out:
        raise SandboxProtocolError("workspace is not a readable git repository")
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path.strip('"'))
    return tuple(sorted(paths))
