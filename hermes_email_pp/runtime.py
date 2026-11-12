"""Profile-scoped plugin bindings, not a second gateway or inference loop."""

from __future__ import annotations

import json
from typing import Any


class CollaborationRuntime:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.adapter = None

    def factory(self, config):
        from .plugin import create_adapter

        adapter = create_adapter(config)
        if hasattr(adapter, "backchannel"):
            if adapter.backchannel is not None:
                adapter.backchannel.context = self.context
            self.adapter = adapter
        return adapter

    def ask_tool(self, args, **kwargs):
        from gateway.session_context import get_session_env

        adapter = self.adapter
        if adapter is None:
            return json.dumps(
                {"success": False, "error": "Email collaboration is not active"}
            )
        request_id, question = (
            str(args.get("request_id", "")),
            str(args.get("question", "")).strip(),
        )
        request = adapter.store.get(request_id)
        if not request or not question or len(question) > 4000:
            return json.dumps(
                {"success": False, "error": "Invalid active request or question"}
            )
        actor = get_session_env("HERMES_SESSION_ACTOR_ID")
        if not (
            get_session_env("HERMES_SESSION_PLATFORM") == "email_pp"
            and get_session_env("HERMES_SESSION_CHAT_ID") == request["chat_id"]
            and get_session_env("HERMES_SESSION_THREAD_ID") == request["thread_id"]
            and actor == request["sender"]
        ):
            return json.dumps(
                {
                    "success": False,
                    "error": "Question belongs to another actor or email session",
                }
            )
        try:
            result = adapter.queue_question(request_id, question)
            # This host retries empty finals. Stop the bound turn instead of
            # relying on a prompt to make the model end silently.
            from agent.subagent_lifecycle import get_active_subagent_parent

            parent = get_active_subagent_parent()
            interrupt = getattr(parent, "interrupt", None)
            if callable(interrupt) and not getattr(parent, "is_interrupted", False):
                interrupt(tool_reason="email clarification pending")
            return json.dumps(
                {
                    "success": True,
                    "state": result["state"],
                    "email_sent": False,
                    "instruction": (
                        "Question queued privately. End this turn; "
                        "do not email a question or draft."
                    ),
                }
            )
        except ValueError:
            return json.dumps(
                {"success": False, "error": "Request is not awaiting work"}
            )

    async def command(self, operation, raw_args, source=None):
        if self.adapter is None or source is None:
            return (
                "Email collaboration is not active or no authenticated "
                "chat source is available."
            )
        return await self.adapter.owner_command(operation, raw_args, source)

    def register(self):
        ctx = self.context
        if hasattr(ctx, "register_tool"):
            ctx.register_tool(
                name="email_ask_owner",
                toolset="email_collaboration",
                schema={
                    "name": "email_ask_owner",
                    "description": (
                        "Privately ask the owner to clarify an active email "
                        "request; holds group email unsent. End the turn after success."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "request_id": {"type": "string"},
                            "question": {"type": "string"},
                        },
                        "required": ["request_id", "question"],
                        "additionalProperties": False,
                    },
                },
                handler=self.ask_tool,
                check_fn=lambda: self.adapter is not None,
            )
        if hasattr(ctx, "register_command"):
            for name, operation in [
                ("email-answer", "answer"),
                ("email-status", "status"),
                ("email-cancel", "cancel"),
            ]:

                def bind(op):
                    async def handler(raw_args, *, source=None, **kwargs):
                        return await self.command(op, raw_args, source)

                    return handler

                ctx.register_command(
                    name,
                    bind(operation),
                    description=f"Email collaboration {operation}",
                    args_hint="your answer / #number your answer"
                    if operation == "answer"
                    else "<request ID>"
                    if operation == "cancel"
                    else "",
                )
        if hasattr(ctx, "register_cli_command"):

            def setup_parser(parser):
                parser.add_argument(
                    "--check",
                    action="store_true",
                    help="Offline redacted credential readiness check",
                )

            def handler(args):
                from .setup import main

                return main(["--check"] if args.check else [])

            ctx.register_cli_command(
                "email-collaboration",
                help="Secure mailbox setup; leaves email disabled",
                setup_fn=setup_parser,
                handler_fn=handler,
            )
