from pathlib import Path


def test_edge_exposes_only_https_supabase_and_gitea_origins() -> None:
    config = Path("deploy/mini-pc-edge/nginx.conf").read_text(encoding="utf-8")

    assert "listen 5443 ssl;" in config
    assert "proxy_pass http://127.0.0.1:54321;" in config
    assert "listen 3443 ssl;" in config
    assert "proxy_pass http://127.0.0.1:3002;" in config
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in config
    assert "proxy_set_header Authorization $http_authorization;" in config
    assert "proxy_set_header X-Forwarded-Proto https;" in config


def test_edge_is_digest_pinned_read_only_and_mounts_ignored_tls() -> None:
    compose = Path("deploy/mini-pc-edge/compose.mini-pc-edge.yml").read_text(
        encoding="utf-8"
    )
    gitignore = Path("deploy/mini-pc-edge/.gitignore").read_text(encoding="utf-8")

    assert ".secrets/" in gitignore
    assert (
        "nginxinc/nginx-unprivileged:1.27.4-alpine@"
        "sha256:62a904036bfc0e4a4f2b556e34cbf17bc136b47fde8cdb4628762725f48c5782"
    ) in compose
    assert "network_mode: host" in compose
    assert "read_only: true" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "source: ./.secrets/mini-pc-edge.crt" in compose
    assert "source: ./.secrets/mini-pc-edge.key" in compose
    assert compose.count("read_only: true") >= 3
    assert "ports:" not in compose


def test_edge_tls_provisioner_is_dry_run_and_generates_ip_san_without_key_output() -> None:
    script = Path("scripts/provision-mini-pc-edge-tls.ps1").read_text(encoding="utf-8")

    assert "[switch]$Apply" in script
    assert "if (-not $Apply)" in script
    assert "IP.1 = $MiniPcAddress" in script
    assert "DNS.1 = $DnsName" in script
    assert "icacls.exe" in script
    assert "private_key" not in script.lower()
    assert "server_ca_sha256" in script


def test_edge_deployer_assigns_runtime_key_only_to_unprivileged_uid() -> None:
    script = Path("scripts/deploy-mini-pc-edge.ps1").read_text(encoding="utf-8")

    assert "[switch]$Apply" in script
    assert "if (-not $Apply)" in script
    assert ".incoming" in script
    assert "install -o 101 -g 101 -m 400" in script
    assert "docker compose" in script
    assert "mini-pc-edge" in script
    assert " down" not in script.lower()
    assert "down -v" not in script.lower()
