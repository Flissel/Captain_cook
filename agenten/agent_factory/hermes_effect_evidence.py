"""Private, write-once evidence for one Hermes provider process effect."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from agenten.agent_factory.skill_store import reject_sensitive_data
from agenten.agent_runtime.contracts import ArtifactRef


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HermesUsageSnapshot:
    content: bytes
    estimated_cost_usd: Decimal
    cost_status: str
    model: str
    provider: str
    api_calls: int
    failed: bool


def parse_hermes_usage(
    content: bytes,
    *,
    provider: str,
    model: str,
) -> HermesUsageSnapshot:
    try:
        raw = json.loads(content, parse_float=Decimal)
        if not isinstance(raw, dict):
            raise ValueError
        cost = Decimal(str(raw["estimated_cost_usd"]))
        api_calls = raw["api_calls"]
        failed = raw["failed"]
        cost_status = raw["cost_status"]
    except (
        KeyError,
        TypeError,
        ValueError,
        InvalidOperation,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("Hermes usage evidence is missing or invalid") from exc
    if (
        not cost.is_finite()
        or cost < 0
        or isinstance(api_calls, bool)
        or not isinstance(api_calls, int)
        or api_calls < 1
        or not isinstance(failed, bool)
        or not isinstance(cost_status, str)
        or not cost_status
        or raw.get("model") != model
        or raw.get("provider") != provider
    ):
        raise ValueError("Hermes usage evidence does not match its pin")
    reject_sensitive_data(raw, "Hermes usage evidence")
    return HermesUsageSnapshot(
        content=content,
        estimated_cost_usd=cost,
        cost_status=cost_status,
        model=model,
        provider=provider,
        api_calls=api_calls,
        failed=failed,
    )


class FilesystemHermesProviderEffectStore:
    """Persist output and usage before typed artifact parsing can fail."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def persist(
        self,
        *,
        effect_identity: str,
        stdout: bytes,
        stderr: bytes,
        usage_content: bytes | None,
        usage: HermesUsageSnapshot | None,
        return_code: int,
        cost_ceiling_exceeded: bool,
    ) -> ArtifactRef:
        if _SHA256.fullmatch(effect_identity) is None:
            raise ValueError("Hermes effect identity must be a SHA-256 digest")
        stdout_ref, stdout_quarantined = self._persist_output(
            effect_identity,
            "stdout",
            stdout,
        )
        stderr_ref, stderr_quarantined = self._persist_output(
            effect_identity,
            "stderr",
            stderr,
        )
        usage_ref = None
        usage_quarantined = False
        if usage_content is not None:
            usage_ref, usage_quarantined = self._persist_output(
                effect_identity,
                "usage",
                usage_content,
                media_type="application/json",
            )
        payload: dict[str, Any] = {
            "schema": "captain.hermes-provider-effect.v1",
            "effect_identity": effect_identity,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "usage_sha256": (
                None
                if usage_content is None
                else hashlib.sha256(usage_content).hexdigest()
            ),
            "stdout_ref": None if stdout_ref is None else stdout_ref.model_dump(mode="json"),
            "stderr_ref": None if stderr_ref is None else stderr_ref.model_dump(mode="json"),
            "usage_ref": None if usage_ref is None else usage_ref.model_dump(mode="json"),
            "estimated_cost_usd": (
                None if usage is None else format(usage.estimated_cost_usd, "f")
            ),
            "cost_status": None if usage is None else usage.cost_status,
            "model": None if usage is None else usage.model,
            "provider": None if usage is None else usage.provider,
            "api_calls": None if usage is None else usage.api_calls,
            "provider_reported_failed": None if usage is None else usage.failed,
            "return_code": return_code,
            "cost_ceiling_exceeded": cost_ceiling_exceeded,
            "sensitive_output_quarantined": (
                stdout_quarantined or stderr_quarantined or usage_quarantined
            ),
        }
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        path = self._root / "effects" / f"{effect_identity}.json"
        _write_once(path, content)
        return ArtifactRef(
            uri=f"artifact://factory/hermes-provider-effects/{digest}",
            sha256=digest,
            media_type="application/json",
        )

    def _persist_output(
        self,
        effect_identity: str,
        label: str,
        content: bytes,
        media_type: str = "application/octet-stream",
    ) -> tuple[ArtifactRef | None, bool]:
        try:
            decoded = content.decode("utf-8")
            try:
                structured: object = json.loads(decoded)
            except json.JSONDecodeError:
                structured = decoded
            reject_sensitive_data(structured, f"Hermes {label}")
        except (UnicodeDecodeError, ValueError):
            return None, True
        return (
            self._persist_blob(
                effect_identity,
                label,
                content,
                media_type,
            ),
            False,
        )

    def _persist_blob(
        self,
        effect_identity: str,
        label: str,
        content: bytes,
        media_type: str,
    ) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        path = self._root / "blobs" / effect_identity / f"{label}-{digest}.bin"
        _write_once(path, content)
        return ArtifactRef(
            uri=(
                f"artifact://factory/hermes-provider-effect/"
                f"{effect_identity}/{label}/{digest}"
            ),
            sha256=digest,
            media_type=media_type,
        )


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError("Hermes provider effect evidence conflicts")
        return
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
