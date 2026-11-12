"""Gateway/tool entry points must bind the authenticated actor to the request."""

import json
from types import SimpleNamespace

from gateway.session_context import clear_session_vars, set_session_vars

from hermes_email_pp.plugin import register


class Context:
    def __init__(self):
        self.tools, self.commands, self.cli = {}, {}, {}

    def register_platform(self, **kwargs):
        self.platform = kwargs

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_cli_command(self, name, **kwargs):
        self.cli[name] = kwargs


def test_plugin_registers_private_question_and_correlated_commands():
    context = Context()
    register(context)
    assert "email_ask_owner" in context.tools
    assert {"email-answer", "email-status", "email-cancel"} <= context.commands.keys()
    assert "email-collaboration" in context.cli


def test_private_question_tool_cannot_modify_another_session():
    from hermes_email_pp.runtime import CollaborationRuntime

    context = Context()
    runtime = CollaborationRuntime(context)
    requests = []
    runtime.adapter = SimpleNamespace(
        store=SimpleNamespace(
            get=lambda rid: {
                "id": rid,
                "chat_id": "owner@example.invalid",
                "thread_id": "thread-one",
                "sender": "owner@example.invalid",
                "state": "working",
            }
        ),
        queue_question=lambda *args: requests.append(args),
    )
    tokens = set_session_vars(
        platform="email_pp",
        chat_id="owner@example.invalid",
        thread_id="thread-other",
        user_id="owner@example.invalid",
        actor_id="owner@example.invalid",
    )
    try:
        result = json.loads(
            runtime.ask_tool({"request_id": "request-one", "question": "Which test?"})
        )
        assert result["success"] is False
        assert not requests
    finally:
        clear_session_vars(tokens)
