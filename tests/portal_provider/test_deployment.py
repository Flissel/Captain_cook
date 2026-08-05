from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "portal-provider" / "compose.portal-provider.yml"
DOCKERFILE = ROOT / "deploy" / "portal-provider" / "Dockerfile"
DEPLOY = ROOT / "scripts" / "deploy-controlled-provider.ps1"
EDGE = ROOT / "deploy" / "mini-pc-edge" / "nginx.conf"


def test_provider_container_is_pinned_non_root_and_loopback_only() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "127.0.0.1:9080:8080" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "captain-provider-data:/data" in compose
    assert "user: \"10001:10001\"" in compose
    assert "python:3.11.13-slim-bookworm@sha256:" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "CAPTAIN_PROVIDER_BEARER_TOKEN" not in dockerfile


def test_provider_deployer_keeps_secrets_remote_and_edge_terminates_tls() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    edge = EDGE.read_text(encoding="utf-8")

    assert "/home/debian/.captain-secrets/controlled-provider.env" in deploy
    assert "openssl rand -hex 32" in deploy
    assert "secrets_emitted = $false" in deploy
    edge_deployer = (ROOT / "scripts" / "deploy-mini-pc-edge.ps1").read_text(
        encoding="utf-8"
    )
    assert "up -d --no-deps --force-recreate mini-pc-edge" in edge_deployer
    assert 'docker compose --project-name captain-mini-pc-edge -f compose.mini-pc-edge.yml exec -T mini-pc-edge nginx -t' in edge_deployer
    assert "mini PC edge failed its post-deploy nginx validation" in edge_deployer
    assert "listen 9443 ssl;" in edge
    assert "proxy_pass http://127.0.0.1:9080;" in edge
    assert "proxy_set_header Authorization $http_authorization;" in edge
    assert (
        'location ~ "^/captain-factory/integration-verification-templates/'
        'raw/commit/[0-9a-f]{40}/verification/(bearer|oauth2)\\.json$" {'
        in edge
    )
    assert "include /run/mini-pc-edge/gitea-template-auth.conf;" in edge
    edge_compose = (
        ROOT / "deploy" / "mini-pc-edge" / "compose.mini-pc-edge.yml"
    ).read_text(encoding="utf-8")
    assert "gitea-template-auth.conf" in edge_compose
    assert "/home/debian/.captain-secrets/gitea-factory.env" in edge_deployer
    assert "CAPTAIN_GITEA_TEMPLATE_READ_TOKEN" in edge_deployer
