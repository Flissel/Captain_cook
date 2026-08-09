from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveInput,
    CapabilityLiveReservation,
)
from agenten.agent_factory.holdout_store import InMemoryPrivateHoldoutStore
from gateway.capability_assembly_admission import (
    CapabilityAssemblyAdmissionError,
    RepositoryCapabilityAssemblyInputAdmission,
)
from gateway.capability_assembly_contracts import (
    CapabilityAssemblyAdmittedInput,
    CapabilityAssemblyInputRequest,
    canonical_assembly_model_bytes,
)


_NAMESPACE = UUID("c2b38170-2694-5f3c-9bc2-366991714138")
_DEMO_INPUTS = (
    (
        "sales_pipeline_brief",
        "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
        "d53c45d5868f8e50a6621b027d94d756540203384480bb27f0604de001329c7d",
        "sequential",
        False,
        (),
    ),
    (
        "incident_command",
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
        "8aacc6bec6f1edf80fdaa9cc8fd2bb0e4d5fecf3c3d4c338780669337415b7f8",
        "selector_group_chat",
        True,
        ("observability", "audit_log"),
    ),
    (
        "claims_resolution",
        "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md",
        "9f5162497153e6a2a2225f28704a887435b9c23265511bbac96f30736ed4ae08",
        "swarm",
        True,
        ("claims_system",),
    ),
)


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _request(path: str, input_id: str = "canonical-input") -> CapabilityAssemblyInputRequest:
    run_id = uuid5(_NAMESPACE, "run")
    return CapabilityAssemblyInputRequest(
        input_id=input_id,
        repository_path=path,
        correlation_id=uuid5(_NAMESPACE, f"correlation:{input_id}"),
        reservation=CapabilityLiveReservation(
            reservation_id=uuid5(_NAMESPACE, f"reservation:{input_id}"),
            run_id=run_id,
            input_id=input_id,
            maximum_cost_usd=Decimal("1.00"),
        ),
    )


def _admission(
    root: Path,
    *,
    expected_sha256_by_path: dict[str, str],
    revision: str | None = None,
    store: InMemoryPrivateHoldoutStore | None = None,
) -> RepositoryCapabilityAssemblyInputAdmission:
    return RepositoryCapabilityAssemblyInputAdmission(
        repository_root=root,
        expected_repository_revision=revision or _git("rev-parse", "HEAD", cwd=root),
        expected_sha256_by_path=expected_sha256_by_path,
        holdout_store=store if store is not None else InMemoryPrivateHoldoutStore(),
        subject_version=1,
    )


def _init_repo(tmp_path: Path, source: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    target = root / "input" / "TO_BE_BUILT.md"
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)
    _git("init", cwd=root)
    _git("config", "user.email", "captain-tests@example.invalid", cwd=root)
    _git("config", "user.name", "Captain Tests", cwd=root)
    _git("add", "input/TO_BE_BUILT.md", cwd=root)
    _git("commit", "-m", "test: add canonical input", cwd=root)
    revision = _git("rev-parse", "HEAD", cwd=root)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return root, revision, digest


def test_admits_three_real_canonical_inputs_without_inventing_tool_intent() -> None:
    root = Path(__file__).resolve().parents[2]
    revision = _git("rev-parse", "HEAD", cwd=root)
    digests = {path: digest for _, path, digest, *_ in _DEMO_INPUTS}
    store = InMemoryPrivateHoldoutStore()
    admission = _admission(
        root,
        expected_sha256_by_path=digests,
        revision=revision,
        store=store,
    )

    admitted = tuple(
        admission.admit(_request(path, input_id))
        for input_id, path, *_ in _DEMO_INPUTS
    )

    assert tuple(item.input.conversation_pattern for item in admitted) == (
        "sequential",
        "selector_group_chat",
        "swarm",
    )
    for result, (_, path, digest, _, n8n_required, required_integrations) in zip(
        admitted, _DEMO_INPUTS, strict=True
    ):
        canonical = result.input
        assert result.repository_path == path
        assert canonical.source_ref.sha256 == digest
        assert canonical.document.input_ref == canonical.source_ref
        assert canonical.compiled_specification.source_ref == canonical.source_ref
        assert canonical.compiled_specification.subject_version == 1
        assert canonical.n8n_required is n8n_required
        assert tuple(
            item.integration_key
            for item in canonical.document.integrations
            if item.required
        ) == required_integrations
        assert canonical.required_tool_markers == ()
        assert canonical.document.authority_boundaries
        assert canonical.document.stop_conditions
        assert any(
            section.heading == "Tool policy"
            for section in canonical.document.extra_sections
        )
    assert len(store) == sum(
        len(item.input.compiled_specification.private_holdout_refs) for item in admitted
    )


def test_legacy_capability_live_input_keeps_strict_demo_semantics() -> None:
    source_ref = (
        _admission(
            Path(__file__).resolve().parents[2],
            expected_sha256_by_path={_DEMO_INPUTS[0][1]: _DEMO_INPUTS[0][2]},
        )
        .admit(_request(_DEMO_INPUTS[0][1], _DEMO_INPUTS[0][0]))
        .input.document.input_ref
    )
    with pytest.raises(ValueError, match="sequential demo input requires n8n"):
        CapabilityLiveInput(
            input_id="legacy-sequential",
            conversation_pattern="sequential",
            source_ref=source_ref,
            integration_intent="none",
            conflicting_input=False,
            human_escalation_required=False,
            required_tool_marker=None,
        )


def test_admitted_canonical_input_round_trips_through_gateway_json() -> None:
    root = Path(__file__).resolve().parents[2]
    admitted = _admission(
        root,
        expected_sha256_by_path={_DEMO_INPUTS[0][1]: _DEMO_INPUTS[0][2]},
    ).admit(_request(_DEMO_INPUTS[0][1], _DEMO_INPUTS[0][0]))

    restored = CapabilityAssemblyAdmittedInput.model_validate_json(
        canonical_assembly_model_bytes(admitted)
    )

    assert restored == admitted


@pytest.mark.parametrize(
    ("revision", "digest", "message"),
    (
        ("f" * 40, None, "revision mismatch"),
        (None, "0" * 64, "digest mismatch"),
    ),
)
def test_revision_and_digest_mismatch_fail_before_compiler_effects(
    tmp_path: Path,
    revision: str | None,
    digest: str | None,
    message: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / _DEMO_INPUTS[0][1]
    root, actual_revision, actual_digest = _init_repo(tmp_path, source)
    store = InMemoryPrivateHoldoutStore()
    admission = _admission(
        root,
        expected_sha256_by_path={"input/TO_BE_BUILT.md": digest or actual_digest},
        revision=revision or actual_revision,
        store=store,
    )

    with pytest.raises(CapabilityAssemblyAdmissionError, match=message):
        admission.admit(_request("input/TO_BE_BUILT.md"))

    assert len(store) == 0


def test_dirty_input_fails_before_compiler_effects(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / _DEMO_INPUTS[0][1]
    root, revision, digest = _init_repo(tmp_path, source)
    store = InMemoryPrivateHoldoutStore()
    admission = _admission(
        root,
        expected_sha256_by_path={"input/TO_BE_BUILT.md": digest},
        revision=revision,
        store=store,
    )
    with (root / "input" / "TO_BE_BUILT.md").open("ab") as stream:
        stream.write(b"\n<!-- dirty -->\n")

    with pytest.raises(CapabilityAssemblyAdmissionError, match="digest mismatch|dirty"):
        admission.admit(_request("input/TO_BE_BUILT.md"))

    assert len(store) == 0


def test_custom_git_environment_cannot_redirect_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path(__file__).resolve().parents[2] / _DEMO_INPUTS[0][1]
    root, revision, digest = _init_repo(tmp_path / "authority", source)
    attacker, _, _ = _init_repo(tmp_path / "attacker", source)
    monkeypatch.setenv("GIT_DIR", str(attacker / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(attacker))

    admitted = _admission(
        root,
        expected_sha256_by_path={"input/TO_BE_BUILT.md": digest},
        revision=revision,
    ).admit(_request("input/TO_BE_BUILT.md"))

    assert admitted.input.source_ref.sha256 == digest


def test_directory_link_is_rejected_before_compiler_effects(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / _DEMO_INPUTS[0][1]
    root, revision, digest = _init_repo(tmp_path, source)
    linked = root / "linked"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(root / "input")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("directory junctions are unavailable")
    else:
        linked.symlink_to(root / "input", target_is_directory=True)
    _git("add", "linked/TO_BE_BUILT.md", cwd=root)
    store = InMemoryPrivateHoldoutStore()
    admission = _admission(
        root,
        expected_sha256_by_path={"linked/TO_BE_BUILT.md": digest},
        revision=revision,
        store=store,
    )

    with pytest.raises(CapabilityAssemblyAdmissionError, match="link|final path"):
        admission.admit(_request("linked/TO_BE_BUILT.md"))

    assert len(store) == 0


def test_missing_ambiguous_and_credential_bearing_pattern_fail_before_effects(
    tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[2] / _DEMO_INPUTS[0][1]
    source_text = source.read_text(encoding="utf-8")
    variants = (
        source_text.replace(
            "Pattern key: sequential.",
            "Conversation behavior is fixed order.",
        ),
        source_text.replace(
            "Pattern key: sequential.",
            "Pattern key: sequential.\nPattern key: selector_group_chat.",
        ),
        source_text.replace(
            "## Helpful resources",
            "## Unsafe note\n"
            + "CAPTAIN_API_TOKEN"
            + "="
            + "secret-value\n\n## Helpful resources",
        ),
    )
    for index, body in enumerate(variants):
        case_root = tmp_path / str(index)
        path = case_root / "repo" / "input" / "TO_BE_BUILT.md"
        path.parent.mkdir(parents=True)
        path.write_text(body, encoding="utf-8", newline="\n")
        root = case_root / "repo"
        _git("init", cwd=root)
        _git("config", "user.email", "captain-tests@example.invalid", cwd=root)
        _git("config", "user.name", "Captain Tests", cwd=root)
        _git("add", "input/TO_BE_BUILT.md", cwd=root)
        _git("commit", "-m", "test: add invalid canonical input", cwd=root)
        revision = _git("rev-parse", "HEAD", cwd=root)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        store = InMemoryPrivateHoldoutStore()

        with pytest.raises(CapabilityAssemblyAdmissionError):
            _admission(
                root,
                expected_sha256_by_path={"input/TO_BE_BUILT.md": digest},
                revision=revision,
                store=store,
            ).admit(_request("input/TO_BE_BUILT.md"))

        assert len(store) == 0


def test_invalid_port_configuration_has_no_holdout_effects(tmp_path: Path) -> None:
    store = InMemoryPrivateHoldoutStore()
    with pytest.raises(ValueError, match="revision"):
        RepositoryCapabilityAssemblyInputAdmission(
            repository_root=tmp_path,
            expected_repository_revision="not-a-revision",
            expected_sha256_by_path={},
            holdout_store=store,
            subject_version=1,
        )
    assert len(store) == 0
