from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = ROOT / "deploy" / "portal-provider" / "templates" / "verification"
RELEASES = TEMPLATE_ROOT.parent / "verification-releases.json"


@pytest.mark.parametrize(
    ("filename", "credential_type", "provider_path", "kind"),
    (
        ("bearer.json", "httpBearerAuth", "/v1/bearer/probes", "bearer"),
        ("oauth2.json", "oAuth2Api", "/v1/oauth2/probes", "oauth2"),
    ),
)
def test_verification_template_is_secret_free_and_provider_evidence_bound(
    filename: str,
    credential_type: str,
    provider_path: str,
    kind: str,
) -> None:
    raw = (TEMPLATE_ROOT / filename).read_bytes()
    workflow = json.loads(raw)

    assert set(workflow) == {"nodes", "connections", "settings"}
    assert raw.count(b"{{CAPTAIN_CREDENTIAL_ID}}") == 1
    assert raw.count(b"{{CAPTAIN_CREDENTIAL_NAME}}") == 1
    assert raw.count(b"{{CAPTAIN_WEBHOOK_PATH}}") == 1
    serialized = raw.decode("utf-8")
    for forbidden in (
        "Authorization",
        "client_secret",
        "access_token",
        "allowUnauthorizedCerts",
    ):
        assert forbidden not in serialized

    request = next(node for node in workflow["nodes"] if node["name"].startswith("Call "))
    assert request["parameters"]["url"] == "https://192.168.178.65:9443" + provider_path
    assert request["parameters"]["genericAuthType"] == credential_type
    assert request["credentials"][credential_type] == {
        "id": "{{CAPTAIN_CREDENTIAL_ID}}",
        "name": "{{CAPTAIN_CREDENTIAL_NAME}}",
    }
    assert (
        request["parameters"]["options"]["redirect"]["redirect"]["followRedirects"]
        is False
    )

    evidence = next(node for node in workflow["nodes"] if node["name"] == "Return provider evidence")
    assignments = {
        item["name"]: item["value"]
        for item in evidence["parameters"]["assignments"]["assignments"]
    }
    assert assignments["provider_trace_id"] == "={{ $json.trace_id }}"
    assert assignments["provider_proof_sha256"] == "={{ $json.proof_sha256 }}"
    assert assignments["provider_kind"] == "={{ $json.kind }}"
    assert kind in request["name"].lower()


def test_verification_release_manifest_pins_exact_template_bytes() -> None:
    releases = [
        GiteaTemplateReleaseV1.model_validate(item)
        for item in json.loads(RELEASES.read_text(encoding="utf-8"))
    ]

    assert {release.path for release in releases} == {
        "verification/bearer.json",
        "verification/oauth2.json",
    }
    assert len({release.revision for release in releases}) == 1
    for release in releases:
        template = TEMPLATE_ROOT / Path(release.path).name
        assert sha256(template.read_bytes()).hexdigest() == release.sha256
        assert release.contents_url.endswith(
            f"/raw/commit/{release.revision}/{release.path}"
        )
