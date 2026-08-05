from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "configure_supabase_es256.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("configure_supabase_es256", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMPOSE = """\
services:
  supabase-auth:
    image: public.ecr.aws/supabase/gotrue:v2.188.1
    environment:
      GOTRUE_JWT_SECRET: legacy-secret
  supabase-rest:
    image: public.ecr.aws/supabase/postgrest:v14.8
    environment:
      PGRST_JWT_SECRET: legacy-secret
  supabase-realtime:
    image: public.ecr.aws/supabase/realtime:v2.82.0
    environment:
      API_JWT_SECRET: legacy-secret
  supabase-storage:
    image: public.ecr.aws/supabase/storage-api:v1.48.28
    environment:
      AUTH_JWT_SECRET: legacy-secret
"""


def test_transform_scopes_private_and_verification_environment_files() -> None:
    migration = _load_module()

    transformed = migration.transform_compose(COMPOSE)

    assert transformed.count(".env.captain-supabase-auth") == 1
    assert transformed.count(".env.captain-supabase-verify") == 3
    assert "PGRST_JWT_SECRET: legacy-secret" not in transformed
    assert "GOTRUE_JWT_SECRET: legacy-secret" in transformed
    assert "API_JWT_SECRET: legacy-secret" in transformed
    assert "AUTH_JWT_SECRET: legacy-secret" in transformed
    assert migration.transform_compose(transformed) == transformed


def test_write_scoped_env_files_never_places_private_key_in_verifier_file(
    tmp_path: Path,
) -> None:
    migration = _load_module()
    private = [
        {
            "kty": "EC",
            "kid": "test-kid",
            "alg": "ES256",
            "crv": "P-256",
            "x": "x",
            "y": "y",
            "d": "private-value",
        }
    ]
    public = {
        "keys": [
            {
                "kty": "EC",
                "kid": "test-kid",
                "alg": "ES256",
                "crv": "P-256",
                "x": "x",
                "y": "y",
                "key_ops": ["verify"],
            }
        ]
    }

    auth_path, verify_path = migration.write_scoped_env_files(
        tmp_path,
        jwt_keys=json.dumps(private, separators=(",", ":")),
        jwt_jwks=json.dumps(public, separators=(",", ":")),
    )

    assert "private-value" in auth_path.read_text(encoding="utf-8")
    assert "private-value" not in verify_path.read_text(encoding="utf-8")
    assert "PGRST_JWT_SECRET=" in verify_path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(verify_path.stat().st_mode) == 0o600


def test_validate_generated_keys_rejects_public_private_material() -> None:
    migration = _load_module()
    private = json.dumps(
        [
            {
                "kty": "EC",
                "kid": "test-kid",
                "alg": "ES256",
                "crv": "P-256",
                "x": "x",
                "y": "y",
                "d": "private-value",
            }
        ]
    )
    unsafe_public = json.dumps(
        {
            "keys": [
                {
                    "kty": "EC",
                    "kid": "test-kid",
                    "alg": "ES256",
                    "crv": "P-256",
                    "x": "x",
                    "y": "y",
                    "d": "private-value",
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="public JWKS contains private material"):
        migration.validate_generated_keys(private, unsafe_public)
