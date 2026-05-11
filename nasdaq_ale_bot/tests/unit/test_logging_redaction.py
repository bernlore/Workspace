"""Structlog redaction of sensitive keys."""

from nasdaq_ale_bot.logging_setup import SENSITIVE_KEYS, drop_sensitive


def test_api_key_redacted():
    ev = {"event": "auth", "api_key": "super-secret-123"}
    out = drop_sensitive(None, "info", dict(ev))
    assert out["api_key"] == "***"
    assert "super-secret-123" not in str(out)


def test_secret_key_redacted_case_insensitive():
    ev = {"event": "auth", "Secret_Key": "xyz"}
    out = drop_sensitive(None, "info", dict(ev))
    assert out["Secret_Key"] == "***"


def test_non_sensitive_untouched():
    ev = {"event": "order_placed", "symbol": "QQQ", "qty": 10}
    out = drop_sensitive(None, "info", dict(ev))
    assert out["symbol"] == "QQQ"
    assert out["qty"] == 10


def test_all_sensitive_keys_registered():
    expected = {"api_key", "secret_key", "authorization", "bearer"}
    assert expected.issubset(SENSITIVE_KEYS)
