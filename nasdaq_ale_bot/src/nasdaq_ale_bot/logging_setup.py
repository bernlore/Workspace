"""Structlog configuration with sensitive-key redaction."""

from typing import Any

import structlog

SENSITIVE_KEYS = {
    "api_key",
    "secret_key",
    "authorization",
    "bearer",
    "alpaca_api_key",
    "alpaca_secret_key",
}


def drop_sensitive(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    for k in list(event_dict.keys()):
        if k.lower() in SENSITIVE_KEYS:
            event_dict[k] = "***"
    return event_dict


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            drop_sensitive,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        cache_logger_on_first_use=True,
    )
