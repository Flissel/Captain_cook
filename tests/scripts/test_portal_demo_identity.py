from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "provision_portal_demo_identity.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("provision_portal_demo_identity", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _segment(value: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def test_extract_secret_requires_one_literal_mapping() -> None:
    provision = _load_module()
    compose = """\
services:
  auth:
    environment:
      ANON_KEY: public-demo-key
      SUPABASE_SERVICE_KEY: private-admin-key
"""

    assert provision.extract_compose_value(compose, "ANON_KEY") == "public-demo-key"
    with pytest.raises(ValueError, match="expected one literal MISSING"):
        provision.extract_compose_value(compose, "MISSING")


def test_extract_secret_accepts_repeated_identical_compose_values_only() -> None:
    provision = _load_module()
    repeated = """\
services:
  auth:
    environment:
      ANON_KEY: public-demo-key
  studio:
    environment:
        ANON_KEY: public-demo-key
"""
    divergent = repeated.replace(
        "        ANON_KEY: public-demo-key",
        "        ANON_KEY: foreign-key",
    )

    assert provision.extract_compose_value(repeated, "ANON_KEY") == "public-demo-key"
    with pytest.raises(ValueError, match="expected one literal ANON_KEY"):
        provision.extract_compose_value(divergent, "ANON_KEY")


def test_validate_session_requires_es256_and_nested_organization() -> None:
    provision = _load_module()
    token = ".".join(
        (
            _segment({"alg": "ES256", "kid": "key-1"}),
            _segment(
                {
                    "sub": "10000000-0000-4000-8000-000000000001",
                    "app_metadata": {"organization_id": "org-demo"},
                }
            ),
            "signature",
        )
    )

    summary = provision.validate_session(token, organization_id="org-demo")

    assert summary == {"algorithm": "ES256", "kid_present": True}


def test_write_identity_environment_excludes_tokens_and_admin_key(tmp_path: Path) -> None:
    provision = _load_module()
    output = tmp_path / ".env.portal-demo"

    provision.write_identity_environment(
        output,
        email="captain.portal.demo@example.invalid",
        password="generated-password",
        organization_id="org-demo",
        subject_id="10000000-0000-4000-8000-000000000001",
        anon_key="public-demo-key",
    )

    content = output.read_text(encoding="utf-8")
    assert "CAPTAIN_PORTAL_DEMO_EMAIL=" in content
    assert "CAPTAIN_PORTAL_DEMO_PASSWORD=" in content
    assert "CAPTAIN_PORTAL_SUPABASE_ANON_KEY=" in content
    assert "ACCESS_TOKEN" not in content
    assert "SERVICE" not in content
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
