"""Host-local, process-evidence recovery for Gateway-persisted Codex sessions."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agenten.delivery.codex_runs import (
    CodexCancellationCoordinator,
    CodexOutcome,
    GatewayCodexRunRepository,
)
from agenten.delivery.gateway_client import GatewayActiveCodexSession, GatewayDeliveryClient


SessionState = Literal["active", "lost", "identity_mismatch"]


@dataclass(frozen=True)
class WorkerRecoveryResult:
    terminalized_session_ids: tuple[str, ...]
    deferred_session_ids: tuple[str, ...]


class LocalCodexWorkerRecoveryDirector:
    """Terminalize only sessions whose host-local process identity is proven."""

    def __init__(self, *, client: GatewayDeliveryClient, state_dir: Path) -> None:
        self._client = client
        self._state_dir = state_dir.resolve()

    async def prepare(self, batch_id: str, iteration: int) -> WorkerRecoveryResult:
        sessions = await self._client.active_codex_sessions(batch_id)
        terminalized: list[str] = []
        deferred: list[str] = []
        for session in sessions:
            if session.iteration != iteration:
                deferred.append(session.session_id)
                continue
            state_path = self._state_path(session.session_id)
            try:
                status = await self._inspect(session.session_id, state_path)
                repository = GatewayCodexRunRepository(
                    client=self._client,
                    project_id=session.project_id,
                    run_id=session.run_id,
                    actor="captain-recovery",
                    now=lambda: session.started_at,
                )
                if status == "lost":
                    await repository.finish(session.session_id, CodexOutcome(classification="lost_process"))
                elif status == "active":
                    await self._canceller(repository, session, state_path).cancel(
                        session_id=session.session_id,
                        state_path=state_path,
                        reason="shutdown",
                    )
                else:
                    deferred.append(session.session_id)
                    continue
                terminalized.append(session.session_id)
            except Exception:
                deferred.append(session.session_id)
        return WorkerRecoveryResult(tuple(terminalized), tuple(deferred))

    async def ready_for_requeue(self, batch_id: str, iteration: int) -> bool:
        """Return true only after every active session has terminal evidence."""

        return not (await self.prepare(batch_id, iteration)).deferred_session_ids

    def _state_path(self, session_id: str) -> Path:
        candidate = (self._state_dir / f"{session_id}.json").resolve()
        if candidate.parent != self._state_dir:
            raise ValueError("Codex session id cannot escape the recovery state directory")
        return candidate

    @staticmethod
    def _tools() -> tuple[Path, Path]:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            raise RuntimeError("PowerShell 7 is required for Codex worker recovery")
        script = Path(__file__).resolve().parents[2] / "scripts" / "codex-session.ps1"
        return Path(pwsh), script

    async def _inspect(self, session_id: str, state_path: Path) -> SessionState:
        if not state_path.is_file():
            raise RuntimeError("Codex session state is unavailable")
        pwsh, script = self._tools()
        process = await asyncio.create_subprocess_exec(
            str(pwsh), "-NoProfile", "-File", str(script),
            "-InspectStatePath", str(state_path), "-SessionId", session_id,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError("Codex process inspection failed")
        payload = json.loads(stdout.decode("utf-8"))
        if payload.get("session_id") != session_id or payload.get("status") not in {"active", "lost", "identity_mismatch"}:
            raise RuntimeError("Codex process inspection returned invalid evidence")
        return payload["status"]

    @staticmethod
    def _canceller(
        repository: GatewayCodexRunRepository,
        session: GatewayActiveCodexSession,
        _: Path,
    ) -> CodexCancellationCoordinator:
        pwsh, script = LocalCodexWorkerRecoveryDirector._tools()
        return CodexCancellationCoordinator(
            repository=repository,
            worker_id=session.worker_id,
            pwsh_path=pwsh,
            script_path=script,
        )
