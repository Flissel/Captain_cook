from pathlib import Path


def test_link_requires_client_certificate_and_forwards_only_portal_routes() -> None:
    config = Path("deploy/portal-link/captain-proxy.conf").read_text(encoding="utf-8")

    assert "ssl_verify_client on" in config
    assert "location /v1/portal/" in config
    assert "proxy_pass http://127.0.0.1:8090" in config
    assert "location / {" in config and "return 404" in config
