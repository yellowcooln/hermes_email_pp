"""Behavioral tests for durable collaboration requests and private notices."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from stat import S_IMODE
from threading import Barrier

import pytest


def request_store(path):
    assert find_spec("hermes_email_pp.requests") is not None, (
        "durable request store is not implemented"
    )
    return import_module("hermes_email_pp.requests").RequestStore(path)


def test_create_persists_the_original_request_snapshot(tmp_path):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    payload = {
        "owner": "owner@example.com",
        "thread_id": "thread-1",
        "recipients": {"to": ["team@example.com"], "cc": ["owner@example.com"]},
        "trigger_message": {"message_id": "<one@example.com>", "text": "Run tests"},
    }

    request, created = store.create("message-1", payload)

    assert created is True
    assert request["state"] == "accepted"
    assert request["message_key"] == "message-1"
    assert isinstance(request["id"], str) and len(request["id"]) >= 24
    assert request["id"] != "message-1"
    assert 0 < request["created_at"] <= request["updated_at"]
    assert {key: request[key] for key in payload} == payload
    assert request_store(path).get(request["id"]) == request
    assert store.get("missing") is None


def test_duplicate_message_returns_original_without_replacing_the_snapshot(tmp_path):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    original, _ = store.create("same-message", {"owner": "first@example.com"})

    duplicate, created = request_store(path).create(
        "same-message", {"owner": "replacement@example.com"}
    )
    unrelated, unrelated_created = store.create("different-message", {})

    assert created is False
    assert duplicate == original
    assert unrelated_created is True
    assert unrelated["id"] != original["id"]


@pytest.mark.parametrize("expected", ["accepted", ["ready", "accepted"]])
def test_transition_compares_state_and_durably_merges_fields(tmp_path, expected):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    original, _ = store.create("message", {"thread_id": "thread-1"})

    updated = store.transition(
        original["id"], expected=expected, state="working", result={"tests": 42}
    )

    assert updated == request_store(path).get(original["id"])
    assert updated["state"] == "working"
    assert updated["thread_id"] == "thread-1"
    assert updated["result"] == {"tests": 42}
    assert updated["created_at"] == original["created_at"]
    assert updated["updated_at"] >= original["updated_at"]
    with pytest.raises(ValueError, match="state"):
        store.transition(original["id"], expected="accepted", state="sent", result={})
    assert store.get(original["id"]) == updated
    with pytest.raises(ValueError, match="unknown"):
        store.transition("missing", expected="accepted", state="working")


@pytest.mark.parametrize("existing", [False, True])
def test_store_is_owner_only_including_new_parent_directories(tmp_path, existing):
    path = tmp_path / "private" / "nested" / "requests.sqlite3"
    if existing:
        path.parent.mkdir(parents=True)
        path.parent.chmod(0o755)
        path.touch(mode=0o644)

    store = request_store(str(path))
    request, _ = store.create("private-message", {"text": "private content"})

    assert store.get(request["id"])["text"] == "private content"
    assert S_IMODE(path.stat().st_mode) == 0o600
    assert S_IMODE(path.parent.stat().st_mode) == 0o700
    if not existing:
        assert S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert all(
        S_IMODE(item.stat().st_mode) == 0o600
        for item in Path(path.parent).iterdir()
        if item.is_file()
    )


def test_list_active_excludes_terminal_and_uncertain_delivery_states(tmp_path):
    store = request_store(tmp_path / "requests.sqlite3")
    assert store.list_active() == []
    active_states = ["accepted", "ready", "working", "waiting_for_owner", "sending"]
    terminal_states = [
        "sent",
        "completed",
        "smtp_accepted",
        "failed",
        "cancelled",
        "delivery_uncertain",
    ]
    expected = []
    for state in active_states + terminal_states:
        request, _ = store.create(state, {"owner": "actual-owner"})
        updated = store.transition(request["id"], "accepted", state)
        if state in active_states:
            expected.append(updated)

    assert store.list_active() == expected
    store.list_active()[0]["owner"] = "mutated"
    assert store.list_active() == expected


@pytest.mark.parametrize("ttl", [None, 120])
def test_ask_persists_an_unpredictable_expiring_question(tmp_path, monkeypatch, ttl):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    request, _ = store.create("message", {"owner": "owner-1", "thread_id": "thread"})
    monkeypatch.setattr("hermes_email_pp.requests.time.time", lambda: 1_000_000)

    if ttl is None:
        waiting = store.ask(request["id"], "Which test result?")
    else:
        waiting = store.ask(request["id"], "Which test result?", ttl_seconds=ttl)

    assert waiting == request_store(path).get(request["id"])
    assert waiting["state"] == "waiting_for_owner"
    assert waiting["question"] == "Which test result?"
    assert waiting["expires_at"] == 1_000_000 + (86400 if ttl is None else ttl)
    assert isinstance(waiting["question_id"], str) and len(waiting["question_id"]) >= 24
    assert waiting["question_id"] != request["id"]
    assert waiting["owner"] == "owner-1"
    assert waiting["thread_id"] == "thread"
    replacement = store.ask(request["id"], "Which environment?", ttl_seconds=60)
    assert replacement["question_id"] != waiting["question_id"]


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), True, "120"])
def test_ask_rejects_invalid_expiry_without_mutation(tmp_path, ttl):
    store = request_store(tmp_path / "requests.sqlite3")
    request, _ = store.create("message", {})

    with pytest.raises(ValueError, match="ttl"):
        store.ask(request["id"], "Question?", ttl_seconds=ttl)

    assert store.get(request["id"]) == request


@pytest.mark.parametrize(
    "case",
    ["valid", "wrong_question", "other_request", "expired", "at_expiry", "replaced"],
)
def test_answer_only_resumes_the_matching_unexpired_question(
    tmp_path, monkeypatch, case
):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    now = [1_000_000.0]
    monkeypatch.setattr("hermes_email_pp.requests.time.time", lambda: now[0])
    request, _ = store.create("message", {"owner": "owner-1"})
    waiting = store.ask(request["id"], "Which environment?", ttl_seconds=60)
    question_id = waiting["question_id"]
    if case == "wrong_question":
        question_id = "not-the-question"
    elif case == "other_request":
        other, _ = store.create("other-message", {"owner": "owner-2"})
        question_id = store.ask(other["id"], "Other question?")["question_id"]
    elif case == "expired":
        now[0] += 61
    elif case == "at_expiry":
        now[0] += 60
    elif case == "replaced":
        waiting = store.ask(request["id"], "Replacement question?")

    if case != "valid":
        with pytest.raises(ValueError):
            request_store(path).answer(request["id"], question_id, "production")
        assert store.get(request["id"]) == waiting
    else:
        ready = request_store(path).answer(request["id"], question_id, "production")
        assert ready["state"] == "ready"
        assert ready["answer"] == "production"
        assert ready["owner"] == "owner-1"
        assert store.get(request["id"]) == ready
        with pytest.raises(ValueError):
            store.answer(request["id"], question_id, "duplicate answer")
        assert store.get(request["id"]) == ready


@pytest.mark.parametrize(
    "state",
    [
        "accepted",
        "ready",
        "working",
        "waiting_for_owner",
        "queued",
        "sending",
        "sent",
        "completed",
        "smtp_accepted",
        "failed",
        "cancelled",
        "delivery_uncertain",
    ],
)
def test_cancel_only_changes_nonterminal_nonsending_requests(tmp_path, state):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    request, _ = store.create("message", {"owner": "owner"})
    before = store.transition(request["id"], "accepted", state)

    if state in {"accepted", "ready", "working", "waiting_for_owner", "queued"}:
        cancelled = store.cancel(request["id"])
        assert cancelled["state"] == "cancelled"
        assert cancelled["owner"] == "owner"
        assert request_store(path).get(request["id"]) == cancelled
        assert store.list_active() == []
        with pytest.raises(ValueError):
            store.cancel(request["id"])
    else:
        with pytest.raises(ValueError):
            store.cancel(request["id"])
        assert store.get(request["id"]) == before


def test_restart_recovery_requeues_work_but_quarantines_ambiguous_sends(tmp_path):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    snapshots = {}
    for state in [
        "accepted",
        "ready",
        "working",
        "sending",
        "sent",
        "failed",
        "cancelled",
    ]:
        request, _ = store.create(state, {"owner": state, "result": {"kept": True}})
        snapshots[state] = store.transition(request["id"], "accepted", state)
    request, _ = store.create("waiting", {"owner": "waiting"})
    snapshots["waiting_for_owner"] = store.ask(request["id"], "Which tests?")
    restarted = request_store(path)
    assert restarted.get(snapshots["working"]["id"])["state"] == "working"

    assert restarted.recover() is None

    for state, before in snapshots.items():
        after = restarted.get(before["id"])
        if state in {"working", "sending"}:
            assert (
                after["state"]
                == {"working": "ready", "sending": "delivery_uncertain"}[state]
            )
            assert after["updated_at"] >= before["updated_at"]
            assert {
                key: value
                for key, value in after.items()
                if key not in {"state", "updated_at"}
            } == {
                key: value
                for key, value in before.items()
                if key not in {"state", "updated_at"}
            }
        else:
            assert after == before
    recovered = [restarted.get(before["id"]) for before in snapshots.values()]
    restarted.recover()
    assert [restarted.get(before["id"]) for before in snapshots.values()] == recovered
    assert "delivery_uncertain" not in {
        item["state"] for item in restarted.list_active()
    }


def test_notification_outbox_persists_and_deduplicates_per_request_kind_text(tmp_path):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    first, _ = store.create("message-1", {})
    second, _ = store.create("message-2", {})
    assert store.pending_notifications() == []

    notice = store.notification(first["id"], "accepted", "Work accepted")
    duplicate = request_store(path).notification(
        first["id"], "accepted", "Work accepted"
    )
    other_kind = store.notification(first["id"], "completed", "Work accepted")
    other_text = store.notification(first["id"], "accepted", "Another question")
    other_request = store.notification(second["id"], "accepted", "Work accepted")

    assert duplicate == notice
    assert notice["request_id"] == first["id"]
    assert notice["kind"] == "accepted"
    assert notice["text"] == "Work accepted"
    assert isinstance(notice["id"], str) and len(notice["id"]) >= 24
    assert request_store(path).pending_notifications() == [
        notice,
        other_kind,
        other_text,
        other_request,
    ]
    with pytest.raises(ValueError, match="unknown"):
        store.notification("missing", "accepted", "Orphan notice")


def test_notification_ack_is_durable_idempotent_and_does_not_requeue_duplicates(
    tmp_path,
):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    request, _ = store.create("message", {})
    first = store.notification(request["id"], "accepted", "Accepted")
    second = store.notification(request["id"], "question", "Which test?")

    assert store.notification_sent(first["id"]) is None

    assert request_store(path).pending_notifications() == [second]
    sent = request_store(path).notification(request["id"], "accepted", "Accepted")
    assert sent["id"] == first["id"]
    assert sent["sent_at"] is not None
    store.notification_sent(first["id"])
    assert store.notification(request["id"], "accepted", "Accepted") == sent
    store.notification_sent(second["id"])
    assert store.pending_notifications() == []
    with pytest.raises(ValueError, match="unknown"):
        store.notification_sent("missing")


@pytest.mark.parametrize(
    "state",
    [
        "sending",
        "sent",
        "completed",
        "smtp_accepted",
        "failed",
        "cancelled",
        "delivery_uncertain",
    ],
)
def test_ask_cannot_reopen_a_terminal_or_sending_request(tmp_path, state):
    store = request_store(tmp_path / "requests.sqlite3")
    request, _ = store.create("message", {})
    before = store.transition(request["id"], "accepted", state)

    with pytest.raises(ValueError, match="state"):
        store.ask(request["id"], "Reopen work?")

    assert store.get(request["id"]) == before


def test_new_question_does_not_reuse_a_previous_answer(tmp_path):
    store = request_store(tmp_path / "requests.sqlite3")
    request, _ = store.create("message", {})
    first = store.ask(request["id"], "Which environment?")
    store.answer(request["id"], first["question_id"], "production")

    waiting = store.ask(request["id"], "Which version?")

    assert waiting["state"] == "waiting_for_owner"
    assert "answer" not in waiting
    assert waiting["question_id"] != first["question_id"]
    with pytest.raises(ValueError):
        store.answer(request["id"], first["question_id"], "late old answer")


def test_store_rejects_a_symlink_database_without_changing_its_target(tmp_path):
    target = tmp_path / "unrelated.txt"
    target.write_text("do not touch")
    target.chmod(0o644)
    path = tmp_path / "requests.sqlite3"
    path.symlink_to(target)

    with pytest.raises((ValueError, OSError)):
        request_store(path)

    assert target.read_text() == "do not touch"
    assert S_IMODE(target.stat().st_mode) == 0o644


def concurrent_results(operation, count=12):
    barrier = Barrier(count, timeout=15)

    def worker(index):
        barrier.wait()
        return operation(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(worker, index) for index in range(count)]
        return [future.result(timeout=30) for future in futures]


@pytest.mark.parametrize("shared_instance", [True, False])
def test_concurrent_duplicate_creation_has_one_winner(tmp_path, shared_instance):
    path = tmp_path / "private" / "nested" / "requests.sqlite3"
    shared = request_store(path) if shared_instance else None

    def create(index):
        store = shared if shared is not None else request_store(path)
        return store.create("same-trigger", {"worker": index})

    results = concurrent_results(create)

    assert sum(created for _, created in results) == 1
    assert len({request["id"] for request, _ in results}) == 1
    original = next(request for request, created in results if created)
    assert all(request == original for request, _ in results)
    assert request_store(path).list_active() == [original]


@pytest.mark.parametrize("shared_instance", [True, False])
def test_concurrent_state_cas_has_one_winner(tmp_path, shared_instance):
    path = tmp_path / "requests.sqlite3"
    shared = request_store(path)
    original, _ = shared.create("trigger", {})

    def claim(index):
        store = shared if shared_instance else request_store(path)
        try:
            return store.transition(original["id"], "accepted", "working", worker=index)
        except ValueError:
            return None

    results = concurrent_results(claim)

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert shared.get(original["id"]) == winners[0]


def test_concurrent_owner_answers_are_consumed_once(tmp_path):
    store = request_store(tmp_path / "requests.sqlite3")
    request, _ = store.create("trigger", {})
    waiting = store.ask(request["id"], "Which test?")

    def answer(index):
        try:
            return store.answer(request["id"], waiting["question_id"], str(index))
        except ValueError:
            return None

    results = concurrent_results(answer)

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert store.get(request["id"]) == winners[0]
    assert winners[0]["state"] == "ready"


def test_concurrent_notification_enqueue_is_deduplicated(tmp_path):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    request, _ = store.create("trigger", {})

    def notify(_index):
        return request_store(path).notification(request["id"], "accepted", "Accepted")

    results = concurrent_results(notify)

    assert len({notice["id"] for notice in results}) == 1
    assert store.pending_notifications() == [results[0]]


def test_cancellation_races_atomically_with_beginning_smtp(tmp_path):
    path = tmp_path / "requests.sqlite3"
    store = request_store(path)
    for index in range(6):
        request, _ = store.create(f"trigger-{index}", {})
        store.transition(request["id"], "accepted", "ready")

        def cancel_or_send(worker, request_id=request["id"]):
            try:
                if worker == 0:
                    return request_store(path).cancel(request_id)
                return store.transition(request_id, "ready", "sending")
            except ValueError:
                return None

        results = concurrent_results(cancel_or_send, count=2)
        winners = [result for result in results if result is not None]
        assert len(winners) == 1
        assert store.get(request["id"]) == winners[0]
        assert winners[0]["state"] in {"cancelled", "sending"}


def test_payload_cannot_shadow_lifecycle_columns(tmp_path):
    store = request_store(tmp_path / "requests.sqlite3")
    request, _ = store.create(
        "real-key",
        {
            "id": "forged-id",
            "state": "sent",
            "message_key": "forged-key",
            "created_at": -1,
            "updated_at": -1,
            "owner": "actual-owner",
            "recipients": {"to": ["team@example.com"]},
        },
    )
    assert request["id"] != "forged-id"
    assert request["state"] == "accepted"
    assert request["message_key"] == "real-key"
    assert request["created_at"] > 0
    assert request["updated_at"] > 0
    updated = store.transition(
        request["id"],
        "accepted",
        "working",
        message_key="replacement",
        created_at=-2,
        updated_at=-2,
    )
    assert updated["message_key"] == "real-key"
    assert updated["created_at"] == request["created_at"]
    assert updated["updated_at"] >= request["updated_at"]
    assert updated["owner"] == "actual-owner"
    request["recipients"]["to"].append("mutated@example.com")
    assert store.get(request["id"])["recipients"] == {"to": ["team@example.com"]}


def test_failed_json_serialization_rolls_back_and_releases_the_transaction(tmp_path):
    store = request_store(tmp_path / "requests.sqlite3")
    request, _ = store.create("trigger", {"kept": True})

    with pytest.raises(TypeError):
        store.transition(request["id"], "accepted", "working", result=object())

    assert store.get(request["id"]) == request
    updated = store.transition(request["id"], "accepted", "working", result="valid")
    assert updated["result"] == "valid"
    with pytest.raises(TypeError):
        store.create("invalid", {"bad": object()})
    _request, created = store.create("invalid", {"now": "valid"})
    assert created is True


@pytest.mark.parametrize("method", ["ask", "answer", "cancel"])
def test_owner_operations_reject_unknown_requests(tmp_path, method):
    store = request_store(tmp_path / "requests.sqlite3")
    arguments = {
        "ask": ("missing", "Question?"),
        "answer": ("missing", "question-id", "Answer"),
        "cancel": ("missing",),
    }
    with pytest.raises(ValueError, match="unknown"):
        getattr(store, method)(*arguments[method])


def test_relative_database_path_does_not_follow_later_working_directory_changes(
    tmp_path, monkeypatch
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    store = request_store("requests.sqlite3")
    request, _ = store.create("message", {})

    monkeypatch.chdir(second)

    assert store.get(request["id"]) == request
    assert list(second.iterdir()) == []


def test_request_and_outbox_survive_a_separate_process_exit(tmp_path):
    path = tmp_path / "private" / "requests.sqlite3"
    script = """
import json
import sys
from hermes_email_pp.requests import RequestStore
store = RequestStore(sys.argv[1])
request, _ = store.create('child-message', {'owner': 'child-owner'})
waiting = store.ask(request['id'], 'Which tests?')
notice = store.notification(request['id'], 'question', 'Which tests?')
print(json.dumps({'request': waiting, 'notice': notice}))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    persisted = json.loads(child.stdout)

    restarted = request_store(path)
    restarted.recover()

    assert restarted.get(persisted["request"]["id"]) == persisted["request"]
    assert restarted.pending_notifications() == [persisted["notice"]]
    ready = restarted.answer(
        persisted["request"]["id"], persisted["request"]["question_id"], "integration"
    )
    assert ready["state"] == "ready"
    restarted.notification_sent(persisted["notice"]["id"])
    assert request_store(path).pending_notifications() == []
