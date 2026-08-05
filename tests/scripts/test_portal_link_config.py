from pathlib import Path


def test_link_requires_client_certificate_and_forwards_only_portal_routes() -> None:
    config = Path("deploy/portal-link/captain-proxy.conf").read_text(encoding="utf-8")

    assert "ssl_verify_client on" in config
    assert "location /v1/portal/" in config
    assert "proxy_pass http://127.0.0.1:8090" in config
    assert "location / {" in config and "return 404" in config


def test_captain_proxy_replaces_untrusted_authorization_with_fixed_identity() -> None:
    config = Path("deploy/portal-link/captain-proxy.conf").read_text(encoding="utf-8")

    assert 'proxy_set_header Authorization "";' in config
    assert 'proxy_set_header X-Captain-Portal-Link-Identity "mini-pc-portal";' in config


def test_mini_pc_proxy_verifies_captain_tls_and_denies_non_portal_routes() -> None:
    config = Path("deploy/portal-link/mini-pc-proxy.conf").read_text(encoding="utf-8")

    assert "proxy_ssl_server_name on;" in config
    assert "proxy_ssl_name captain-portal-link.internal;" in config
    assert "proxy_ssl_verify on;" in config
    assert "proxy_ssl_verify off;" not in config
    assert "location / {" in config and "return 404;" in config


def test_compose_uses_ignored_read_only_secret_mounts_and_separate_roles() -> None:
    compose = Path("deploy/portal-link/compose.portal-link.yml").read_text(encoding="utf-8")
    gitignore = Path("deploy/portal-link/.gitignore").read_text(encoding="utf-8")

    assert ".secrets/" in gitignore
    assert "source: ./.secrets/captain/captain-server.crt" in compose
    assert "source: ./.secrets/captain/captain-server.key" in compose
    assert "source: ./.secrets/captain/mini-pc-client-ca.crt" in compose
    assert "source: ./.secrets/mini-pc/mini-pc-client.crt" in compose
    assert "source: ./.secrets/mini-pc/mini-pc-client.key" in compose
    assert "source: ./.secrets/mini-pc/captain-server-ca.crt" in compose
    assert compose.count("read_only: true") == 10
    assert 'profiles: ["captain"]' in compose
    assert 'profiles: ["mini-pc"]' in compose


def test_captain_proxy_binds_only_the_private_loopback_endpoint() -> None:
    config = Path("deploy/portal-link/captain-proxy.conf").read_text(encoding="utf-8")

    assert "listen 127.0.0.1:443 ssl;" in config
    assert "listen 443 ssl;" not in config
