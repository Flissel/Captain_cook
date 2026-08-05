from pathlib import Path


def test_link_requires_client_certificate_and_forwards_only_portal_routes() -> None:
    config = Path("deploy/portal-link/captain-proxy.conf").read_text(encoding="utf-8")

    assert "ssl_verify_client on" in config
    assert "location /v1/portal/" in config
    assert "proxy_pass http://host.docker.internal:8090" in config
    assert "location / {" in config and "return 404" in config


def test_captain_proxy_preserves_supabase_bearer_with_fixed_link_identity() -> None:
    config = Path("deploy/portal-link/captain-proxy.conf").read_text(encoding="utf-8")

    portal_location = config.split("location /v1/portal/ {", 1)[1].split("}", 1)[0]
    assert "proxy_set_header Authorization $http_authorization;" in portal_location
    assert 'proxy_set_header Authorization "";' not in portal_location
    assert 'proxy_set_header X-Captain-Portal-Link-Identity "mini-pc-portal";' in config


def test_authorization_is_forwarded_only_inside_the_portal_location() -> None:
    config = Path("deploy/portal-link/captain-proxy.conf").read_text(encoding="utf-8")

    assert config.count("proxy_set_header Authorization $http_authorization;") == 1
    default_location = config.split("location / {", 1)[1]
    assert "proxy_set_header Authorization" not in default_location
    assert "return 404;" in default_location


def test_supabase_bearer_survives_every_canonical_proxy_hop() -> None:
    configs = (
        Path("portal/nginx.conf").read_text(encoding="utf-8"),
        Path("deploy/portal-link/mini-pc-proxy.conf").read_text(encoding="utf-8"),
        Path("deploy/portal-link/captain-proxy.conf").read_text(encoding="utf-8"),
    )

    for config in configs:
        portal_location = config.split("location /v1/portal/ {", 1)[1].split("}", 1)[0]
        assert "proxy_set_header Authorization $http_authorization;" in portal_location
        assert config.count("proxy_set_header Authorization $http_authorization;") == 1


def test_mini_pc_proxy_verifies_captain_tls_and_denies_non_portal_routes() -> None:
    config = Path("deploy/portal-link/mini-pc-proxy.conf").read_text(encoding="utf-8")

    assert "proxy_pass https://10.77.0.1;" in config
    assert "proxy_pass https://captain-portal-link.internal;" not in config
    assert "proxy_ssl_server_name on;" in config
    assert "proxy_ssl_name captain-portal-link.internal;" in config
    assert "proxy_ssl_verify on;" in config
    assert "proxy_ssl_verify off;" not in config
    assert "listen 8443;" in config
    assert "listen 127.0.0.1:8443;" not in config
    assert "location / {" in config and "return 404;" in config


def test_compose_uses_ignored_read_only_secret_mounts_and_separate_roles() -> None:
    compose = Path("deploy/portal-link/compose.portal-link.yml").read_text(encoding="utf-8")
    gitignore = Path("deploy/portal-link/.gitignore").read_text(encoding="utf-8")

    assert ".secrets/" in gitignore
    assert "wireguard/*.conf" in gitignore
    for source in (
        "./.secrets/captain/captain-server.crt",
        "./.secrets/captain/captain-server.key",
        "./.secrets/captain/mini-pc-client-ca.crt",
        "./.secrets/captain/wireguard/captain.conf",
        "./.secrets/mini-pc/captain-server-ca.crt",
        "./.secrets/mini-pc/mini-pc-client.crt",
        "./.secrets/mini-pc/mini-pc-client.key",
        "./.secrets/mini-pc/wireguard/mini-pc.conf",
    ):
        assert f"source: {source}\n        target:" in compose
        start = compose.index(f"source: {source}")
        end = compose.find("      - type: bind", start + 1)
        mount = compose[start:] if end == -1 else compose[start:end]
        assert "read_only: true" in mount
    assert 'profiles: ["captain"]' in compose
    assert 'profiles: ["mini-pc"]' in compose


def test_mini_pc_mtls_proxy_is_ordered_after_wireguard() -> None:
    compose = Path("deploy/portal-link/compose.portal-link.yml").read_text(encoding="utf-8")

    mini_pc_link = compose.split("  mini-pc-portal-link:", 1)[1]
    assert "depends_on:" in mini_pc_link
    assert "mini-pc-wireguard:" in mini_pc_link
    assert "condition: service_healthy" in mini_pc_link
    assert "10.77.0.2/30" in compose


def test_captain_mtls_proxy_waits_for_the_wireguard_address() -> None:
    compose = Path("deploy/portal-link/compose.portal-link.yml").read_text(encoding="utf-8")
    captain_link = compose.split("  captain-portal-link:", 1)[1].split(
        "  mini-pc-wireguard:", 1
    )[0]

    assert "condition: service_healthy" in captain_link
    assert "10.77.0.1/30" in compose


def test_captain_proxy_binds_only_the_wireguard_endpoint() -> None:
    config = Path("deploy/portal-link/captain-proxy.conf").read_text(encoding="utf-8")

    assert "listen 10.77.0.1:443 ssl;" in config
    assert "listen 127.0.0.1:443 ssl;" not in config
    assert "listen 443 ssl;" not in config


def test_proxies_share_only_their_wireguard_network_namespace() -> None:
    compose = Path("deploy/portal-link/compose.portal-link.yml").read_text(encoding="utf-8")

    assert "network_mode: service:captain-wireguard" in compose
    assert "network_mode: service:mini-pc-wireguard" in compose
    assert "network_mode: host" not in compose
    assert (
        '"${CAPTAIN_PORTAL_MINI_PC_ENDPOINT:-192.168.178.65}:'
        '51820:51820/udp"'
    ) in compose
    captain = compose.split("  captain-wireguard:", 1)[1].split(
        "  captain-portal-link:", 1
    )[0]
    assert "51820:51820/udp" not in captain
    assert '"127.0.0.1:8443:8443/tcp"' in compose


def test_wireguard_examples_define_the_fixed_private_peers_without_keys() -> None:
    captain = Path("deploy/portal-link/wireguard/captain.conf.example").read_text(
        encoding="utf-8"
    )
    mini_pc = Path("deploy/portal-link/wireguard/mini-pc.conf.example").read_text(
        encoding="utf-8"
    )

    assert "Address = 10.77.0.1/30" in captain
    assert "Endpoint = <MINI_PC_PRIVATE_UDP_ENDPOINT>:51820" in captain
    assert "PersistentKeepalive = 25" in captain
    assert "AllowedIPs = 10.77.0.2/32" in captain
    assert "Address = 10.77.0.2/30" in mini_pc
    assert "ListenPort = 51820" in mini_pc
    assert "AllowedIPs = 10.77.0.1/32" in mini_pc
    assert "CAPTAIN_WIREGUARD_PRIVATE_KEY_FROM_LOCAL_SECRET" in captain
    assert "MINI_PC_WIREGUARD_PRIVATE_KEY_FROM_LOCAL_SECRET" in mini_pc


def test_compose_pins_the_available_wireguard_image_without_latest() -> None:
    compose = Path("deploy/portal-link/compose.portal-link.yml").read_text(encoding="utf-8")
    image = (
        "lscr.io/linuxserver/wireguard@"
        "sha256:ac43e1226878d2611315172d6ea357a95cb326ee73124b91108118efc8666889"
    )

    assert compose.count(f"image: {image}") == 2
    assert "lscr.io/linuxserver/wireguard:latest" not in compose


def test_mtls_proxies_use_digest_pinned_drop_all_containers() -> None:
    compose = Path("deploy/portal-link/compose.portal-link.yml").read_text(encoding="utf-8")
    digest = (
        "nginx:1.27-alpine@"
        "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
    )

    assert compose.count(f"image: {digest}") == 2
    assert compose.count("cap_drop:\n      - ALL") == 2
    assert compose.count('user: "101:101"') == 1
    assert compose.count("no-new-privileges:true") == 2
    assert compose.count("/var/cache/nginx:uid=101,gid=101,mode=0700") == 1
    assert compose.count("/var/run:uid=101,gid=101,mode=0755") == 1
    assert "/var/cache/nginx:uid=0,gid=0,mode=0755" in compose
    assert "/var/run:uid=0,gid=0,mode=0755" in compose
    captain_proxy = compose.split("  captain-portal-link:", 1)[1].split(
        "  mini-pc-wireguard:", 1
    )[0]
    mini_proxy = compose.split("  mini-pc-portal-link:", 1)[1]
    for capability in ("CHOWN", "SETGID", "SETUID", "NET_BIND_SERVICE"):
        assert f"      - {capability}" in captain_proxy
    assert "NET_BIND_SERVICE" not in mini_proxy
    assert "image: nginx:1.27-alpine\n" not in compose
