"""ALLOWED_ORIGINS → CORS allow_origins parsing (WOR-839 / A9)."""

from mcp_router.models.settings import Settings


def test_default_cors_is_wildcard() -> None:
    assert Settings().cors_allow_origins == ["*"]


def test_comma_separated_origins() -> None:
    settings = Settings(allowed_origins="https://app.example.com, http://localhost:3000")
    assert settings.cors_allow_origins == [
        "https://app.example.com",
        "http://localhost:3000",
    ]


def test_blank_origins_fall_back_to_wildcard() -> None:
    assert Settings(allowed_origins="  ,  ").cors_allow_origins == ["*"]
