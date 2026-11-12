"""Real host background final-delivery path and real loopback SMTP dialogue.

Only the unavailable model and TLS-enabled external service are substituted.
No provider credentials or real recipients are involved.
"""

import asyncio
import smtplib
from email import message_from_bytes
from types import SimpleNamespace

from gateway.platform_registry import PlatformEntry, platform_registry
from test_collaboration import request_mail

from hermes_email_pp.plugin import create_adapter


def test_real_host_background_turn_submits_one_threaded_smtp_message(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    platform_registry.register(
        PlatformEntry(
            name="email_pp",
            label="Email++",
            adapter_factory=lambda c: c,
            check_fn=lambda: True,
        )
    )
    adapter = create_adapter(
        SimpleNamespace(
            extra={
                "collaboration": "true",
                "address": "hermes@example.invalid",
                "login": "mailbox@example.invalid",
                "password": "fixture-only",
                "allowed_users": "owner@example.invalid",
                "authserv_id": "mx.example.invalid",
                "backchannel_target": "telegram:123",
                "backchannel_owner": "456",
            }
        )
    )
    transcripts, notices, events = [], [], []

    async def run():
        finished = asyncio.Event()

        async def smtp(reader, writer):
            writer.write(b"220 test ESMTP\r\n")
            await writer.drain()
            data = []
            while line := await reader.readline():
                command = line.decode().strip()
                if command.upper().startswith("EHLO"):
                    writer.write(b"250-test\r\n250 AUTH PLAIN\r\n")
                elif command.upper().startswith("AUTH"):
                    writer.write(b"235 authenticated\r\n")
                elif command.upper().startswith(("MAIL", "RCPT", "RSET")):
                    writer.write(b"250 OK\r\n")
                elif command.upper() == "DATA":
                    writer.write(b"354 continue\r\n")
                    await writer.drain()
                    while (line := await reader.readline()) != b".\r\n":
                        if not line:
                            break
                        data.append(line[1:] if line.startswith(b"..") else line)
                    transcripts.append(b"".join(data))
                    writer.write(b"250 accepted\r\n")
                elif command.upper() == "QUIT":
                    writer.write(b"221 bye\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"500 unsupported\r\n")
                await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(smtp, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(
                adapter, "_smtp", lambda: smtplib.SMTP("127.0.0.1", port, timeout=5)
            )

            async def notify(text):
                notices.append(text)
                return True

            adapter._notify = notify

            async def model_boundary(event):
                events.append(event)
                return "Fixture test evidence: GPU initialization succeeded."

            adapter.set_message_handler(model_boundary)
            original_complete = adapter.on_processing_complete

            async def complete(event, outcome):
                await original_complete(event, outcome)
                finished.set()

            monkeypatch.setattr(adapter, "on_processing_complete", complete)
            await adapter._dispatch(request_mail().as_bytes())
            await asyncio.wait_for(finished.wait(), 10)
            await asyncio.sleep(0)

    asyncio.run(run())
    assert len(events) == len(transcripts) == 1
    actual = message_from_bytes(transcripts[0])
    assert actual["In-Reply-To"] == "<request@example.invalid>"
    assert "john@example.invalid" in actual["To"]
    assert "person10@example.invalid" in actual["Cc"]
    assert actual["From"] == "Hermes Agent <hermes@example.invalid>"
    assert len(notices) == 2
    assert not adapter._running_requests
