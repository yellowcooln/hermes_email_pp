"""Installed Hermes factory and native-admission codec contracts."""

import asyncio
from types import SimpleNamespace

import pytest
from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.session_envelope import prepare_native, restore_native
from hermes_state_runtime import RuntimeStoreError
from test_collaboration import request_mail

from hermes_email_pp.plugin import register


def test_host_factory_injected_settings_and_native_replay(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class Context:
        def register_platform(self, **kwargs):
            self.entry = PlatformEntry(**kwargs)

    ctx = Context()
    register(ctx)
    platform_registry.register(ctx.entry)

    class Runner(GatewayAdapterLifecycleMixin):
        config = SimpleNamespace(
            group_sessions_per_user=True,
            thread_sessions_per_user=False,
            multiplex_profiles=False,
        )

    runner = Runner()
    config = PlatformConfig(
        enabled=False,
        extra={
            "collaboration": "true",
            "address": "hermes@example.invalid",
            "login": "mailbox@example.invalid",
            "password": "fixture-only",
            "imap_host": "imap.example.invalid",
            "smtp_host": "smtp.example.invalid",
            "allowed_users": "owner@example.invalid",
            "authserv_id": "mx.example.invalid",
        },
    )
    adapter = runner._create_adapter(Platform("email_pp"), config)
    assert adapter is not None
    assert adapter.gateway_runner is runner
    assert config.extra["group_sessions_per_user"] is True
    # Authorization is a fixture; sender/auth policies have separate tests.
    runner._adapter_flag = lambda *args: False
    runner._adapter_profile_for_source = lambda source: None
    runner._is_user_authorized_for_source = lambda source, **kw: (
        source.user_id == "owner@example.invalid"
    )
    runner._adapter_for_source = lambda source: (
        adapter if source.platform.value == "email_pp" else None
    )
    runner.session_store = SimpleNamespace(
        _generate_session_key=lambda source: "fixture-route"
    )
    events = []

    async def intake(event):
        events.append(event)

    async def notice(text):
        return True

    adapter.handle_message, adapter._notify = intake, notice

    async def run():
        await adapter._dispatch(request_mail().as_bytes())
        event = events[0]
        payload = await prepare_native(runner, event)
        restored = restore_native(payload, runner)
        assert restored.source.thread_id == event.source.thread_id
        assert restored.message_id == "<request@example.invalid>"
        assert restored.source.chat_id == event.source.chat_id
        assert not restored.metadata
        assert not restored.allow_gateway_control
        event.metadata = {"request_id": "not-allowed"}
        with pytest.raises(RuntimeStoreError):
            await prepare_native(runner, event)

    asyncio.run(run())
