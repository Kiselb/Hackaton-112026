from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_caddy_ingress_is_loopback_authenticated_and_does_not_forward_credentials() -> None:
    config = (ROOT / "scripts" / "demo" / "Caddyfile").read_text(encoding="utf-8")

    assert "http://:{$DEMO_PROXY_PORT}" in config
    assert "bind 127.0.0.1" in config
    assert "reverse_proxy 127.0.0.1:{$DEMO_BACKEND_PORT}" in config
    assert "basic_auth bcrypt" in config
    assert "{env.DEMO_AUTH_PASSWORD_HASH}" in config
    assert "header_up -Authorization" in config
    assert "request>headers>Authorization delete" in config
    backend_matcher = next(
        line.strip() for line in config.splitlines() if line.strip().startswith("@backend path")
    )
    for route in ("/api/*", "/healthz", "/readyz", "/docs", "/docs/*", "/redoc", "/openapi.json"):
        assert route in backend_matcher
    assert config.index("basic_auth bcrypt") < config.index("request_body")
    assert config.index("basic_auth bcrypt") < config.index("handle @backend")


def test_example_configuration_contains_no_demo_secret() -> None:
    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value

    assert values["PUBLIC_DEMO_ENABLED"] == "false"
    assert values["DEMO_AUTH_USERNAME"] == "jury"
    assert values["DEMO_AUTH_PASSWORD_HASH"] == ""
    assert values["DEMO_TUNNEL_PROVIDER"] == "tailscale"
    assert values["TAILSCALE_FUNNEL_URL"] == ""


def test_proxy_check_covers_every_sensitive_route_family() -> None:
    script = (ROOT / "scripts" / "demo" / "Test-PublicDemo.ps1").read_text(
        encoding="utf-8"
    )

    for route in (
        "/api/v1/ask",
        "/api/v1/documents",
        "/deactivate",
        "/restore",
        "/api/v1/admin/traces",
        "/feedback",
        "/healthz",
        "/docs",
        "/redoc",
        "/openapi.json",
    ):
        assert route in script
    for name, route in (
        ("Swagger UI", "/docs"),
        ("ReDoc", "/redoc"),
        ("OpenAPI", "/openapi.json"),
    ):
        assert (
            f'Name = "{name} without credentials"; Method = "GET"; '
            f'Path = "{route}"; Headers = @{{}}; Expected = 401'
        ) in script
        assert (
            f'Name = "{name} with wrong credentials"; Method = "GET"; '
            f'Path = "{route}"; Headers = $wrong; Expected = 401'
        ) in script
        assert (
            f'Name = "{name} with valid credentials"; Method = "GET"; '
            f'Path = "{route}"; Headers = $valid; Expected = 200'
        ) in script
    assert "wrong credentials" in script
    assert "valid credentials" in script
    assert '<div id="swagger-ui">' in script
    assert "SwaggerUIBundle" in script
    assert '<redoc spec-url="/openapi.json"></redoc>' in script
    assert "ConvertFrom-Json" in script
    assert "PSObject.Properties['openapi']" in script
    assert "PSObject.Properties['paths']" in script
    assert not any(
        'Method = "POST"' in line and "Headers = $valid" in line
        for line in script.splitlines()
    )
