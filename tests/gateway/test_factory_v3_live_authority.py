from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from gateway.factory_v3_live_authority import (
    FactoryV3LiveAuthorityError,
    load_captain_factory_v3_live_authority,
    parse_signed_factory_v3_live_authority,
    verify_factory_v3_local_runtime,
)


ROOT = Path(__file__).parents[2]


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _unsigned_authority(runner_public_key: bytes) -> dict[str, object]:
    return {
        "schema": "captain.factory-v3-live-authority-binding.v1",
        "authority_id": "captain-factory-v3-production",
        "binding_version": 1,
        "services": {
            "gateway": {
                "origin": "https://gateway.example",
                "health_path": "/healthz",
                "service": "captain-gateway",
                "version": "v3",
                "capabilities": [
                    "factory-v3-live-authority",
                    "action-inbox-read",
                ],
                "credential_audience": "captain-gateway-live-v3",
            },
            "minibook": {
                "origin": "https://minibook.example",
                "health_path": "/healthz",
                "service": "minibook-creation",
                "version": "v3",
                "capabilities": ["creation-jobs-v3", "cas-readback-v1"],
                "credential_audience": "minibook-creation-live-v3",
            },
        },
        "credential_audiences": {
            "captain_token": "captain-gateway-live-v3",
            "worker_token": "minibook-creation-live-v3",
            "creation_api_key": "minibook-creation-api-v3",
        },
        "executables": {
            "codex": {"sha256": "1" * 64, "version": "codex-cli 1.2.3"},
            "pwsh": {"sha256": "2" * 64, "version": "7.5.2"},
            "hermes_python": {"sha256": "3" * 64, "version": "3.11.9"},
        },
        "codex_session": {
            "sha256": "4" * 64,
            "version": "captain.codex-session.v1",
        },
        "hermes_runtime": {
            "runtime_sha256": "5" * 64,
            "git_sha": "6" * 40,
            "distribution": "hermes-agent",
            "version": "0.9.0",
            "module_file_sha256": "7" * 64,
            "capabilities": [
                "factory-v3-build",
                "released-skill-execution",
            ],
        },
        "released_skill": {
            "name": "captain-factory-brief-codex",
            "sha256": "8" * 64,
        },
        "runner_attestation": {
            "key_id": "captain-factory-v3-runner-2026-01",
            "public_key_base64": base64.b64encode(runner_public_key).decode("ascii"),
        },
    }


def _signed_authority(
    private_key: Ed25519PrivateKey,
    runner_public_key: bytes,
) -> dict[str, object]:
    payload = _unsigned_authority(runner_public_key)
    signature = private_key.sign(_canonical(payload))
    payload["signature"] = {
        "algorithm": "Ed25519",
        "key_id": "captain-factory-v3-authority-2026-01",
        "value_base64": base64.b64encode(signature).decode("ascii"),
    }
    return payload


def test_signed_authority_binds_origins_audiences_runtime_and_runner_key() -> None:
    authority_key = Ed25519PrivateKey.generate()
    runner_key = Ed25519PrivateKey.generate()
    authority_public = authority_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    runner_public = runner_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    binding = parse_signed_factory_v3_live_authority(
        _signed_authority(authority_key, runner_public),
        verification_public_key=authority_public,
    )

    assert binding.gateway.origin == "https://gateway.example"
    assert binding.minibook.origin == "https://minibook.example"
    assert binding.credential_audiences.creation_api_key == "minibook-creation-api-v3"
    assert binding.executables.codex.sha256 == "1" * 64
    assert binding.hermes_runtime.runtime_sha256 == "5" * 64
    assert binding.runner_attestation.public_key == runner_public
    assert binding.sha256 == hashlib.sha256(_canonical(_unsigned_authority(runner_public))).hexdigest()


@pytest.mark.parametrize("mutation", ("origin", "binary", "runner", "signature"))
def test_authority_rejects_tampering_and_self_signed_replacement(mutation: str) -> None:
    authority_key = Ed25519PrivateKey.generate()
    runner_key = Ed25519PrivateKey.generate()
    public_key = authority_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    runner_public = runner_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    payload = _signed_authority(authority_key, runner_public)
    if mutation == "origin":
        payload["services"]["gateway"]["origin"] = "https://attacker.example"  # type: ignore[index]
    elif mutation == "binary":
        payload["executables"]["codex"]["sha256"] = "a" * 64  # type: ignore[index]
    elif mutation == "runner":
        payload["runner_attestation"]["public_key_base64"] = base64.b64encode(b"x" * 32).decode("ascii")  # type: ignore[index]
    else:
        attacker = Ed25519PrivateKey.generate()
        unsigned = {key: value for key, value in payload.items() if key != "signature"}
        payload["signature"]["value_base64"] = base64.b64encode(attacker.sign(_canonical(unsigned))).decode("ascii")  # type: ignore[index]

    with pytest.raises(FactoryV3LiveAuthorityError, match="authority binding"):
        parse_signed_factory_v3_live_authority(
            payload,
            verification_public_key=public_key,
        )


def test_product_loader_rejects_a_valid_document_signed_by_an_untrusted_key(
    tmp_path: Path,
) -> None:
    authority_key = Ed25519PrivateKey.generate()
    runner_key = Ed25519PrivateKey.generate()
    runner_public = runner_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    binding_path = (
        tmp_path
        / ".captain-cook"
        / "private"
        / "factory-v3-live"
        / "authority-binding.json"
    )
    binding_path.parent.mkdir(parents=True)
    binding_path.write_text(
        json.dumps(_signed_authority(authority_key, runner_public)),
        encoding="utf-8",
    )

    assert load_captain_factory_v3_live_authority(tmp_path) is None


def test_local_runtime_requires_signed_digests_and_exact_controlled_probe_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_key = Ed25519PrivateKey.generate()
    runner_key = Ed25519PrivateKey.generate()
    authority_public = authority_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    runner_public = runner_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    repository = tmp_path / "repo"
    scripts = repository / "scripts"
    hermes = repository / "hermes-agent"
    scripts.mkdir(parents=True)
    (hermes / "hermes_cli").mkdir(parents=True)
    codex = tmp_path / "codex.exe"
    pwsh = tmp_path / "pwsh.exe"
    hermes_python = hermes / "python.exe"
    codex_script = scripts / "codex-session.ps1"
    module_file = hermes / "hermes_cli" / "__init__.py"
    for path, content in (
        (codex, b"codex-binary"),
        (pwsh, b"pwsh-binary"),
        (hermes_python, b"python-binary"),
        (codex_script, (ROOT / "scripts" / "codex-session.ps1").read_bytes()),
        (module_file, b"__version__ = '0.9.0'\n"),
    ):
        path.write_bytes(content)
    (hermes / "runtime.txt").write_text("factory-v3-runtime\n", encoding="utf-8")
    skill_root = tmp_path / "skills"
    skill = skill_root / "captain-factory-brief-codex"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# released\n", encoding="utf-8")

    unsigned = _unsigned_authority(runner_public)
    unsigned["executables"] = {
        "codex": {"sha256": hashlib.sha256(codex.read_bytes()).hexdigest(), "version": "codex-cli 1.2.3"},
        "pwsh": {"sha256": hashlib.sha256(pwsh.read_bytes()).hexdigest(), "version": "7.5.2"},
        "hermes_python": {"sha256": hashlib.sha256(hermes_python.read_bytes()).hexdigest(), "version": "3.11.9"},
    }
    unsigned["codex_session"] = {
        "sha256": hashlib.sha256(codex_script.read_bytes()).hexdigest(),
        "version": "captain.codex-session.v1",
    }
    unsigned["hermes_runtime"]["runtime_sha256"] = "9" * 64  # type: ignore[index]
    unsigned["hermes_runtime"]["module_file_sha256"] = hashlib.sha256(module_file.read_bytes()).hexdigest()  # type: ignore[index]
    unsigned["released_skill"]["sha256"] = "a" * 64  # type: ignore[index]
    signature = authority_key.sign(_canonical(unsigned))
    signed = unsigned | {
        "signature": {
            "algorithm": "Ed25519",
            "key_id": "captain-factory-v3-authority-2026-01",
            "value_base64": base64.b64encode(signature).decode("ascii"),
        }
    }
    binding = parse_signed_factory_v3_live_authority(
        signed,
        verification_public_key=authority_public,
    )
    calls: list[tuple[str, ...]] = []
    probe_options: list[dict[str, object]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        probe_options.append(dict(kwargs))
        if argv[0] == str(codex.resolve()):
            return subprocess.CompletedProcess(argv, 0, "codex-cli 1.2.3\n", "")
        if argv[0] == str(pwsh.resolve()):
            return subprocess.CompletedProcess(argv, 0, "7.5.2\n", "")
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "schema": "captain.factory-v3-hermes-runtime-probe.v1",
                    "python_version": "3.11.9",
                    "distribution": "hermes-agent",
                    "distribution_version": "0.9.0",
                    "module_file_sha256": hashlib.sha256(module_file.read_bytes()).hexdigest(),
                    "capabilities": ["factory-v3-build", "released-skill-execution"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n",
            "",
        )

    monkeypatch.setattr("gateway.factory_v3_live_authority.subprocess.run", run)
    monkeypatch.setattr(
        "gateway.factory_v3_live_authority._factory_v3_runtime_digest",
        lambda _: "9" * 64,
    )
    monkeypatch.setattr(
        "gateway.factory_v3_live_authority._factory_v3_skill_digest",
        lambda _: "a" * 64,
    )
    monkeypatch.setattr(
        "gateway.factory_v3_live_authority._factory_v3_git_identity_ready",
        lambda *_: True,
    )

    assert verify_factory_v3_local_runtime(
        binding=binding,
        repository_root=repository,
        codex_path=codex,
        pwsh_path=pwsh,
        codex_home=tmp_path,
        hermes_python_path=hermes_python,
        hermes_runtime_root=hermes,
        released_skill_root=skill_root,
    )
    assert len(calls) == 3
    assert all(call[0] in {str(codex.resolve()), str(pwsh.resolve()), str(hermes_python.resolve())} for call in calls)
    assert all(options["timeout"] == 5 for options in probe_options)
    assert all(
        isinstance(options["env"], dict)
        and options["env"]["PATH"] == ""  # type: ignore[index]
        and options["env"]["PYTHONNOUSERSITE"] == "1"  # type: ignore[index]
        for options in probe_options
    )

    codex.write_bytes(b"arbitrary executable named codex")
    calls.clear()
    assert not verify_factory_v3_local_runtime(
        binding=binding,
        repository_root=repository,
        codex_path=codex,
        pwsh_path=pwsh,
        codex_home=tmp_path,
        hermes_python_path=hermes_python,
        hermes_runtime_root=hermes,
        released_skill_root=skill_root,
    )
    assert calls == []

    codex.write_bytes(b"codex-binary")
    pwsh.write_bytes(b"arbitrary executable named pwsh")
    assert not verify_factory_v3_local_runtime(
        binding=binding,
        repository_root=repository,
        codex_path=codex,
        pwsh_path=pwsh,
        codex_home=tmp_path,
        hermes_python_path=hermes_python,
        hermes_runtime_root=hermes,
        released_skill_root=skill_root,
    )
    assert calls == []

    pwsh.write_bytes(b"pwsh-binary")
    module_file.write_text("# fabricated Hermes runtime\n", encoding="utf-8")
    assert not verify_factory_v3_local_runtime(
        binding=binding,
        repository_root=repository,
        codex_path=codex,
        pwsh_path=pwsh,
        codex_home=tmp_path,
        hermes_python_path=hermes_python,
        hermes_runtime_root=hermes,
        released_skill_root=skill_root,
    )
    assert calls == []
