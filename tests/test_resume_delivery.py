"""Real native-admission ledger plus adapter background continuation regression."""

import asyncio
from types import SimpleNamespace

from gateway.session_envelope import prepare_native
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch
from test_collaboration import collaboration as collaboration
from test_collaboration import request_mail


def test_concurrent_question_calls_keep_one_pending_token(collaboration):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    async def notify(text):
        return True

    collaboration._notify = notify

    async def agent(event):
        request = collaboration.request_for_reply(
            event.source.chat_id, event.message_id
        )
        start = threading.Barrier(4)

        def question(index):
            start.wait(timeout=5)
            return collaboration.queue_question(request["id"], f"Which word? {index}")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(question, range(4)))
        assert len({result["question_id"] for result in results}) == 1
        pending = collaboration.store.pending_notifications()
        assert len([n for n in pending if n["kind"].startswith("question:")]) == 1

    collaboration.handle_message = agent
    asyncio.run(collaboration._dispatch(request_mail().as_bytes()))


def test_private_question_stops_only_the_bound_email_turn(collaboration):
    import contextvars
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from agent.interrupt_control import InterruptControlMixin
    from agent.subagent_lifecycle import bind_subagent_parent
    from gateway.session_context import clear_session_vars, set_session_vars

    from hermes_email_pp.runtime import CollaborationRuntime

    class HostAgent(InterruptControlMixin):
        def __init__(self):
            self._interrupt_requested = False
            self._execution_thread_id = None
            self._active_children_lock = threading.RLock()
            self._active_children = []
            self.quiet_mode = True

    parent, unrelated = HostAgent(), HostAgent()
    runtime = CollaborationRuntime(None)
    runtime.adapter = collaboration
    results = []

    async def notify(text):
        return True

    collaboration._notify = notify

    async def agent(event):
        request = collaboration.request_for_reply(
            event.source.chat_id, event.message_id
        )
        tokens = set_session_vars(
            platform="email_pp",
            chat_id=event.source.chat_id,
            thread_id=event.source.thread_id,
            user_id=request["sender"],
            actor_id=request["sender"],
        )
        try:
            with (
                bind_subagent_parent(parent),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                result = pool.submit(
                    contextvars.copy_context().run,
                    runtime.ask_tool,
                    {"request_id": request["id"], "question": "Which word?"},
                ).result()
                results.append(result)
            assert parent._interrupt_requested, (
                "A prompt instruction does not stop the host agent loop"
            )
            assert not unrelated._interrupt_requested
            assert (
                collaboration.store.get(request["id"])["state"] == "waiting_for_owner"
            )
        finally:
            clear_session_vars(tokens)

    collaboration.handle_message = agent
    asyncio.run(collaboration._dispatch(request_mail().as_bytes()))
    assert results


def test_answered_request_is_new_native_input_but_replies_to_original_mail(
    collaboration, tmp_path, monkeypatch
):
    adapter = collaboration
    db = SessionDB(tmp_path / "native-host.sqlite3")
    db.create_session("native-fixture", source="email_pp")
    epoch = begin_runtime_epoch(db, instance_id="isolated-fixture")
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False),
        _adapter_flag=lambda *args: False,
        _adapter_profile_for_source=lambda source: None,
        _is_user_authorized_for_source=lambda source, **kw: (
            source.user_id == "owner@example.invalid"
        ),
        _adapter_for_source=lambda source: adapter,
        session_store=SimpleNamespace(
            _generate_session_key=lambda source: "fixture-route"
        ),
    )
    sent, events, notices = [], [], []
    monkeypatch.setattr(
        adapter, "_submit_reply", lambda message: sent.append(message) or {}
    )

    async def notify(text):
        notices.append(text)
        return True

    adapter._notify = notify

    async def run():
        completed = asyncio.Queue()
        original_complete = adapter.on_processing_complete

        async def complete(event, outcome):
            await original_complete(event, outcome)
            completed.put_nowait(event.message_id)

        monkeypatch.setattr(adapter, "on_processing_complete", complete)

        async def model_boundary(event):
            payload = await prepare_native(runner, event)
            # The real host keys native inputs by event.message_id. A changed payload
            # under that same identity must conflict rather than silently bypassing it.
            admit_session_input(
                db,
                epoch=epoch,
                principal_id="native-email-owner",
                session_id="native-fixture",
                request_id=event.message_id,
                payload=payload,
            )
            events.append(event)
            request = adapter.request_for_reply(event.source.chat_id, event.message_id)
            if len(events) == 1:
                await adapter.ask_owner(request["id"], "Which test word?")
                return "Waiting for private clarification."
            assert request["answer"] == "fixture-word"
            assert "fixture-word" in event.text
            assert "Which test word?" in event.text
            return "CC test passed: fixture-word"

        adapter.set_message_handler(model_boundary)
        await adapter._dispatch(request_mail().as_bytes())
        await asyncio.wait_for(completed.get(), 5)
        request = adapter.store.list_active()[0]
        assert request["state"] == "waiting_for_owner"
        assert not sent
        source = SimpleNamespace(
            platform="telegram", chat_id="123", thread_id=None, user_id="456"
        )
        answer = await adapter.owner_command("answer", "fixture-word", source)
        assert "saved" in answer
        await adapter.process_ready()
        await asyncio.wait_for(completed.get(), 5)
        current = adapter.store.get(request["id"])
        assert current["state"] == "sent", current.get("error")
        assert len(events) == 2
        assert events[0].message_id != events[1].message_id
        assert events[0].source.thread_id == events[1].source.thread_id
        assert events[0].source.chat_id == events[1].source.chat_id
        assert events[0].source.user_id == events[1].source.user_id
        assert not events[1].metadata and not events[1].allow_gateway_control
        assert len(sent) == 1
        assert str(sent[0]["In-Reply-To"]) == "<request@example.invalid>"
        assert "<request@example.invalid>" in str(sent[0]["References"])
        assert (
            sent[0].get_body(preferencelist=("plain",)).get_content().strip()
            == "CC test passed: fixture-word"
        )
        assert not adapter._running_requests
        await adapter.process_ready()
        assert len(sent) == 1

    try:
        asyncio.run(run())
    finally:
        db.close()
