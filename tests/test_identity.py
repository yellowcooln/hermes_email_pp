"""Mailbox credentials and alias From are independent identities."""

from types import SimpleNamespace

from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_email_pp import adapter as module


def test_mailbox_login_does_not_change_alias_sender(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform_registry.register(
        PlatformEntry(
            name="email_pp",
            label="Email++",
            adapter_factory=lambda c: c,
            check_fn=lambda: True,
        )
    )
    adapter = module.EmailPPAdapter(
        SimpleNamespace(
            extra={
                "address": "hermes@example.invalid",
                "login": "no-reply@example.invalid",
                "password": "test-only",
                "imap_host": "imap.example.invalid",
                "smtp_host": "smtp.example.invalid",
                "sender_name": "Hermes Agent",
            }
        )
    )
    logins, messages = [], []

    class Transport:
        def login(self, username, password):
            logins.append(username)

        def quit(self):
            pass

        def send_message(self, message):
            messages.append(message)

    monkeypatch.setattr(module.imaplib, "IMAP4_SSL", lambda *a, **kw: Transport())
    monkeypatch.setattr(adapter, "_smtp", lambda: Transport())
    adapter._get_imap()
    adapter._probe_smtp()
    route = adapter._router.resolve(
        "owner@example.invalid", message_id="<one@example.invalid>"
    )
    adapter._send_email(route, "Test summary", "<one@example.invalid>", None)
    assert logins == ["no-reply@example.invalid"] * 3
    assert str(messages[0]["From"]) == "Hermes Agent <hermes@example.invalid>"
