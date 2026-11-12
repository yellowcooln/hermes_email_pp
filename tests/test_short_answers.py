import asyncio
from types import SimpleNamespace

import pytest
from test_collaboration import collaboration as collaboration

from hermes_email_pp.requests import RequestStore

OWNER = SimpleNamespace(
    platform="telegram", chat_id="123", thread_id=None, user_id="456"
)


def pending(adapter, key="one"):
    request, _ = adapter.store.create(key, {"subject": key})
    return adapter.queue_question(request["id"], "Which word?")


def test_single_question_accepts_answer_without_ids(collaboration):
    request = pending(collaboration)
    result = asyncio.run(
        collaboration.owner_command("answer", "a three word answer", OWNER)
    )
    assert "saved" in result
    assert collaboration.store.get(request["id"])["answer"] == "a three word answer"
    notice = collaboration.store.pending_notifications()[0]["text"]
    assert request["id"] not in notice and request["question_id"] not in notice
    assert "/email-answer your answer" in notice


def test_multiple_questions_require_stable_short_reference(collaboration):
    first = pending(collaboration, "first")
    second = pending(collaboration, "second")
    result = asyncio.run(collaboration.owner_command("answer", "yes", OWNER))
    assert "more than one" in result.lower()
    assert collaboration.store.get(first["id"])["state"] == "waiting_for_owner"
    ref = collaboration.store.question_ref(second["id"], second["question_id"])
    assert f"#{ref}" in result
    result = asyncio.run(
        collaboration.owner_command("answer", f"#{ref} test 69", OWNER)
    )
    assert "saved" in result
    assert collaboration.store.get(second["id"])["answer"] == "test 69"
    assert collaboration.store.get(first["id"])["state"] == "waiting_for_owner"


def test_short_reference_survives_restart_and_cannot_answer_new_question(collaboration):
    first = pending(collaboration)
    ref = collaboration.store.question_ref(first["id"], first["question_id"])
    collaboration.store = RequestStore(collaboration.store.path)
    assert collaboration.store.question_ref(first["id"], first["question_id"]) == ref
    collaboration.store.answer(first["id"], first["question_id"], "old answer")
    fresh = collaboration.store.ask(first["id"], "New question?")
    assert collaboration.store.question_ref(first["id"], fresh["question_id"]) != ref
    result = asyncio.run(
        collaboration.owner_command("answer", f"#{ref} stale answer", OWNER)
    )
    assert "saved" not in result
    assert collaboration.store.get(first["id"])["question_id"] == fresh["question_id"]
    assert collaboration.store.get(first["id"])["state"] == "waiting_for_owner"


@pytest.mark.parametrize(
    "text",
    ["#0 yes", "#999999 yes", "#no yes", "#7", "#99999999999999999999999999 yes"],
)
def test_bad_reference_does_not_consume_pending_question(collaboration, text):
    request = pending(collaboration)
    result = asyncio.run(collaboration.owner_command("answer", text, OWNER))
    assert "saved" not in result
    assert collaboration.store.get(request["id"])["state"] == "waiting_for_owner"


def test_short_and_implicit_answers_still_enforce_expiry(collaboration):
    request = pending(collaboration)
    ref = collaboration.store.question_ref(request["id"], request["question_id"])
    collaboration.store.transition(
        request["id"], "waiting_for_owner", "waiting_for_owner", expires_at=0
    )
    for text in ["yes", f"#{ref} yes"]:
        result = asyncio.run(collaboration.owner_command("answer", text, OWNER))
        assert "saved" not in result
        assert collaboration.store.get(request["id"])["state"] == "waiting_for_owner"


def test_wrong_owner_cannot_use_short_or_implicit_answer(collaboration):
    request = pending(collaboration)
    stranger = SimpleNamespace(
        platform="telegram", chat_id="123", thread_id=None, user_id="999"
    )
    result = asyncio.run(collaboration.owner_command("answer", "yes", stranger))
    assert "Only" in result
    assert collaboration.store.get(request["id"])["state"] == "waiting_for_owner"
