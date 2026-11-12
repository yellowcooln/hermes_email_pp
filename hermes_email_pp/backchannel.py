"""Private backchannel over the host's existing send-message tool."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from typing import Any

from .threading import active_profile_home


class Backchannel:
    def __init__(self, context: Any, target: str, owner: str) -> None:
        match = re.fullmatch(
            r"(fluxer|discord|telegram|signal):([^:\s]+)(?::([^:\s]+))?", target
        )
        if match is None or not owner.strip():
            raise ValueError(
                "An explicit private platform:chat[:thread] and owner ID are required"
            )
        self.context = context
        self.target = target
        self.platform, self.chat_id, self.thread_id = match.groups()
        self.owner = owner
        self.home = str(active_profile_home())

    def authorized(self, source: Any) -> bool:
        platform = getattr(source, "platform", "")
        return (
            str(getattr(platform, "value", platform)) == self.platform
            and str(getattr(source, "chat_id", "")) == self.chat_id
            and str(getattr(source, "thread_id", "") or "") == (self.thread_id or "")
            and str(getattr(source, "user_id", "")) == self.owner
        )

    async def send(self, text: str) -> bool:
        # Use the supported CLI: send_message is not registry-dispatchable on this host.
        # Body is stdin, not argv. Pin the owning profile, not the caller's home.
        # Subjects/questions are text, never directives to attach a host file.
        text = re.sub(r"(?i)MEDIA:", "MEDIA\u200b:", text)
        text = (
            text.replace("@everyone", "@\u200beveryone")
            .replace("@here", "@\u200bhere")
            .replace("<@", "<\u200b@")
        )
        try:
            process = await asyncio.to_thread(
                subprocess.run,
                ["hermes", "send", "--json", "--to", self.target, "--file", "-"],
                input=text,
                capture_output=True,
                text=True,
                shell=False,
                timeout=45,
                env={**os.environ, "HERMES_HOME": self.home},
            )
            result = json.loads(process.stdout)
            return (
                process.returncode == 0
                and isinstance(result, dict)
                and result.get("success") is True
            )
        except Exception:
            # The durable outbox retains the notice; never claim it was delivered.
            return False
