"""Hermes entry point for the Email++ platform."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .config import (
    CHANNEL_ENV,
    CHANNEL_REQUIRED_ENV,
    environment_enablement,
    is_configured,
)


def check_requirements() -> bool:
    """Email++ uses only Python's standard-library IMAP and SMTP clients."""
    return True


def create_adapter(config: Any) -> Any:
    """Load the transport only when the gateway creates the Email++ adapter."""
    from .config import environment_settings

    extra = getattr(config, "extra", {}) or {}
    collaboration = environment_settings().get(
        "EMAIL_PP_COLLABORATION", str(extra.get("collaboration", "false"))
    )
    if collaboration.lower() in {"1", "true", "yes", "on"}:
        module = import_module(".collaboration", package=__package__)
        return module.CollaborationAdapter(config)
    adapter_module = import_module(".adapter", package=__package__)
    return adapter_module.EmailPPAdapter(config)


def register(ctx: Any) -> None:
    """Register Email++ without touching Hermes' built-in email platform."""
    _register_channel_env_metadata()
    from .runtime import CollaborationRuntime

    runtime = CollaborationRuntime(ctx)
    runtime.register()
    registration: dict[str, Any] = {
        "name": "email_pp",
        "label": "Email++",
        "adapter_factory": runtime.factory,
        "check_fn": check_requirements,
        "validate_config": is_configured,
        "is_connected": is_configured,
        "required_env": list(CHANNEL_REQUIRED_ENV),
        "env_enablement_fn": environment_enablement,
        "allowed_users_env": "EMAIL_PP_ALLOWED_USERS",
        "allow_all_env": "EMAIL_PP_ALLOW_ALL_USERS",
        "emoji": "email",
        "platform_hint": (
            "You are responding by email. Keep replies clear and professional. "
            "When the user asks you to create, send, provide, return, or attach "
            "an asset, create it and include MEDIA:/absolute/path/to/file in your "
            "response so Email++ attaches it. A Markdown link, file URL, localhost "
            "URL, or plain local path does not substitute for an attachment when "
            "the asset itself was requested. Local paths and local HTTP URLs remain "
            "appropriate informational references when the user asks for a location "
            "or link, or when the recipient shares the environment. You may attach "
            "an asset with MEDIA: and include its location separately when both help."
        ),
    }
    ctx.register_platform(**registration)


def _register_channel_env_metadata() -> None:
    """Expose Email++ settings through vanilla Hermes' Channels metadata path."""
    try:
        optional_env_vars = import_module("hermes_cli.config").OPTIONAL_ENV_VARS
    except (AttributeError, ImportError):
        return
    for field in CHANNEL_ENV:
        name = field["name"]
        optional_env_vars.setdefault(
            name,
            {
                **field,
                "category": "messaging",
            },
        )
