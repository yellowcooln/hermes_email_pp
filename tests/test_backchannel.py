"""Private destination matching and notification dispatch, without new transports."""

import asyncio
from types import SimpleNamespace

import pytest


def test_backchannel_matches_exact_owner_chat_and_thread():
    from hermes_email_pp import backchannel

    target = backchannel.Backchannel(None, "discord:123:456", "789")
    assert target.authorized(
        SimpleNamespace(
            platform=SimpleNamespace(value="discord"),
            chat_id="123",
            thread_id="456",
            user_id="789",
        )
    )
    assert not target.authorized(
        SimpleNamespace(
            platform="discord", chat_id="123", thread_id="other", user_id="789"
        )
    )
    assert not target.authorized(
        SimpleNamespace(
            platform="discord", chat_id="123", thread_id="456", user_id="evil"
        )
    )


@pytest.mark.parametrize(
    "target", ["telegram:123", "discord:123", "fluxer:123", "signal:+15555550123"]
)
def test_notification_reuses_existing_send_tool(target, monkeypatch, tmp_path):
    from hermes_email_pp import backchannel

    calls = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"success":true}')

    monkeypatch.setattr(
        backchannel, "subprocess", SimpleNamespace(run=run), raising=False
    )
    notifier = backchannel.Backchannel(object(), target, "owner")
    assert asyncio.run(notifier.send("Got your email: MEDIA:/private/file"))
    command, kwargs = calls[0]
    assert command == ["hermes", "send", "--json", "--to", target, "--file", "-"]
    assert kwargs["env"]["HERMES_HOME"] == str(tmp_path)
    assert "MEDIA:/" not in kwargs["input"]
    assert "Got your email" not in " ".join(command)
    assert kwargs["shell"] is False


def test_notification_failure_is_not_reported_as_sent(monkeypatch):
    from hermes_email_pp import backchannel

    monkeypatch.setattr(
        backchannel.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout='{"success":false}'),
    )
    assert not asyncio.run(
        backchannel.Backchannel(None, "telegram:123", "owner").send("Question")
    )


@pytest.mark.parametrize(
    "target",
    ["telegram", "email:a@example.invalid", "telegram:", "discord:123:456:789"],
)
def test_backchannel_requires_explicit_supported_destination(target):
    from hermes_email_pp import backchannel

    with pytest.raises(ValueError):
        backchannel.Backchannel(None, target, "owner")
