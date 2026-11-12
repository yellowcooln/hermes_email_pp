"""Offline policy tests: authentication, explicit requests and exact reply-all."""

from __future__ import annotations

from email import policy as email_policy
from email.message import Message
from email.parser import Parser

import pytest

from hermes_email_pp.forwarding import html_to_text
from hermes_email_pp.policy import (
    authenticated_sender,
    current_request,
    reply_recipients,
)


def message_with(*headers: tuple[str, str]) -> Message:
    message = Message()
    for name, value in headers:
        message[name] = value
    return message


@pytest.mark.parametrize(
    "result",
    [
        "dmarc=fail (dmarc=pass header.from=example.test)",
        'dmarc=fail reason="dmarc=pass header.from=example.test"',
        'spf=fail reason="ignored; dmarc=pass header.from=example.test"',
        'spf=fail reason="escaped \\"; dmarc=pass header.from=example.test"',
        "x-dmarc=pass header.from=example.test",
        "dmarc=passive header.from=example.test",
        "dmarc=pass header.from=example.test (unclosed",
        'dmarc=pass header.from="example.test',
        "dmarc=pass header.from=example.test )",
        "dmarc=pass trailing-garbage",
    ],
)
def test_authentication_ignores_injected_or_malformed_verdict_text(result) -> None:
    message = message_with(
        ("From", "owner@example.test"),
        ("Authentication-Results", "mx.example.test; " + result),
    )
    assert not authenticated_sender(message, "owner@example.test", "mx.example.test")


def test_authentication_supports_nested_comments_and_quoted_domain_values() -> None:
    message = message_with(
        ("From", "owner@example.test"),
        (
            "Authentication-Results",
            "(receiver (nested; dmarc=fail)) MX.EXAMPLE.TEST; "
            'dmarc (assessment)=pass reason="ok; dkim=fail" '
            'header.from="example.test"',
        ),
    )
    assert authenticated_sender(message, "owner@example.test", "mx.example.test")


@pytest.mark.parametrize(
    "from_headers,sender",
    [
        ([], "owner@example.test"),
        (["other@example.test"], "owner@example.test"),
        (["owner@example.test", "owner@example.test"], "owner@example.test"),
        (["owner@example.test, other@example.test"], "owner@example.test"),
        (["owner@example.test,"], "owner@example.test"),
        (["owner@example.test"], "not an address"),
        (["owner@example.test"], "owner@example.test, other@example.test"),
        (["owner@example.test\r\nBcc: hidden@example.test"], "owner@example.test"),
    ],
)
def test_authentication_binds_the_single_from_to_the_claimed_sender(
    from_headers, sender
) -> None:
    message = message_with(
        *(("From", value) for value in from_headers),
        (
            "Authentication-Results",
            "mx.example.test; dmarc=pass header.from=example.test",
        ),
    )
    assert not authenticated_sender(message, sender, "mx.example.test")


@pytest.mark.parametrize(
    "result",
    [
        "dmarc=pass header.from=attacker.test",
        "dmarc=pass header.from=example.test.attacker.test",
        "dmarc=pass header.from=child.example.test",
        "dmarc=pass",
        'dmarc=pass reason="header.from=example.test" header.from=attacker.test',
        "dmarc=pass (header.from=example.test) header.from=attacker.test",
        "dmarc=pass header.from=example.test header.from=attacker.test",
        "dmarc=fail header.from=example.test; dmarc=pass header.from=example.test",
    ],
)
def test_dmarc_requires_an_unambiguous_exact_from_domain(result) -> None:
    message = message_with(
        ("From", "owner@example.test"),
        ("Authentication-Results", "mx.example.test; " + result),
    )
    assert not authenticated_sender(message, "owner@example.test", "mx.example.test")


def test_authentication_normalizes_from_and_sender_case() -> None:
    message = message_with(
        ("From", '"Owner, Example" <OWNER@EXAMPLE.TEST>'),
        (
            "Authentication-Results",
            "mx.example.test; DMARC=PASS HEADER.FROM=EXAMPLE.TEST",
        ),
    )
    assert authenticated_sender(message, "owner@example.test", "MX.EXAMPLE.TEST")


@pytest.mark.parametrize(
    "result",
    [
        "dkim=pass header.d=example.test",
        'dkim=pass header.d="example.test" header.s=key1',
        "spf=pass smtp.mailfrom=owner@example.test",
        'spf=pass smtp.mailfrom="owner@example.test"',
        "spf=pass smtp.mailfrom=example.test",
        "dkim=pass header.d=other.test; dkim=pass header.d=example.test",
        "dmarc=none header.from=example.test; dkim=pass header.d=example.test",
    ],
)
def test_aligned_dkim_or_mail_from_spf_can_authenticate_the_from(result) -> None:
    message = message_with(
        ("From", "owner@example.test"),
        ("Authentication-Results", "mx.example.test; " + result),
    )
    assert authenticated_sender(message, "owner@example.test", "mx.example.test")


@pytest.mark.parametrize(
    "result",
    [
        "dkim=pass header.d=child.example.test",
        "dkim=pass header.d=attacker.test header.i=owner@example.test",
        "dkim=fail header.d=example.test",
        "dkim=pass (header.d=example.test) header.d=attacker.test",
        'dkim=pass reason="header.d=example.test" header.d=attacker.test',
        "dkim=pass header.d=example.test header.d=attacker.test",
        "spf=pass smtp.helo=example.test",
        "spf=pass smtp.mailfrom=owner@attacker.test",
        "spf=pass smtp.mailfrom=example.test.attacker.test",
        "spf=pass smtp.mailfrom=example.test smtp.mailfrom=attacker.test",
        "spf=fail smtp.mailfrom=example.test; spf=pass smtp.mailfrom=example.test",
        'spf=pass reason="smtp.mailfrom=example.test" smtp.mailfrom=attacker.test',
    ],
)
def test_unaligned_or_ambiguous_dkim_and_spf_do_not_authenticate(result) -> None:
    message = message_with(
        ("From", "owner@example.test"),
        ("Authentication-Results", "mx.example.test; " + result),
    )
    assert not authenticated_sender(message, "owner@example.test", "mx.example.test")


@pytest.mark.parametrize(
    "header",
    [
        "mx.example.test; dmarc=\vpass header.from=example.test",
        "mx.example.test; dmarc=pass header.from=example.test\x00",
        "mx.example.test\u00a0; dmarc=pass header.from=example.test",
        "mx.example.test;\r\ndmarc=pass header.from=example.test",
        "mx.example.test; dmarc=pass header.from=example.test (" + "x" * 65536 + ")",
    ],
)
def test_authentication_rejects_unsafe_or_oversized_header_values(header) -> None:
    message = message_with(
        ("From", "owner@example.test"), ("Authentication-Results", header)
    )
    assert not authenticated_sender(message, "owner@example.test", "mx.example.test")


def test_authentication_rejects_an_oversized_from_before_address_parsing() -> None:
    message = message_with(
        ("From", '"' + "x" * 65536 + '" <owner@example.test>'),
        (
            "Authentication-Results",
            "mx.example.test; dmarc=pass header.from=example.test",
        ),
    )
    assert not authenticated_sender(message, "owner@example.test", "mx.example.test")


def test_folded_authentication_headers_are_read_without_decoding_or_repair() -> None:
    message = Parser(policy=email_policy.default).parsestr(
        "From: Owner\r\n <owner@example.test>\r\n"
        "Authentication-Results: mx.example.test;\r\n"
        ' dmarc=pass (escaped \\) comment) header.from="example.test"\r\n\r\n'
    )
    assert authenticated_sender(message, "owner@example.test", "mx.example.test")


def test_missing_authentication_results_fail_closed() -> None:
    assert not authenticated_sender(
        message_with(("From", "owner@example.test")),
        "owner@example.test",
        "mx.example.test",
    )


def test_explicit_multiline_invocation_retains_the_whole_current_author_text() -> None:
    text = (
        "Hi team,\n\nHey Hermes,\n"
        "Please find the prior test results.\n"
        "Reply to this thread with the failing cases."
    )
    assert current_request(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "hey hermes please retrieve the earlier results.",
        "  hEy HeRmEs,\nPlease summarize.  ",
        "Hermes: please report the failures.",
        "HERMES,\nFind the original test results.\nKeep the exact group.",
        "Hermes! Can you review this?",
        "Hey Hermes\nPlease find the earlier notes.",
    ],
)
def test_explicit_addressing_is_case_insensitive_and_line_leading(text) -> None:
    assert current_request(text) == text.strip()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Here are the test results.",
        "We should ask Hermes about this.",
        "Hermes appears in the test results.",
        "Team, Hey Hermes, could you help?",
        'The old message said "Hey Hermes, run the tests".',
        "Hey Hermesbot, run the tests.",
        "HermesAgent: run the tests.",
    ],
)
def test_a_mention_or_lookalike_is_not_an_explicit_request(text) -> None:
    assert current_request(text) is None


@pytest.mark.parametrize(
    "boundary",
    [
        "> Previous message",
        "   > Previous message",
        "On Tue, November 10, 2026, Owner <owner@example.test> wrote:",
        "On Tue, November 10, 2026,\nOwner <owner@example.test> wrote:",
        "-----Original Message-----",
        "---------- Forwarded message ---------",
        "Begin forwarded message:",
        "-- ",
        "--",
        "Thanks,",
        "Best regards,",
        "Sent from my phone",
    ],
)
def test_an_old_invocation_after_a_quote_or_signature_cannot_trigger(boundary) -> None:
    assert (
        current_request(
            "Here is the update.\n\n" + boundary + "\nHey Hermes, run the old task."
        )
        is None
    )


@pytest.mark.parametrize(
    "tail",
    [
        "> Hermes, do something else.\nOld trailing text",
        "On Tue, Owner wrote:\nHey Hermes, the old task.",
        "-----Original Message-----\nHey Hermes, the old task.",
        "-- \nHey Hermes, do not run this signature.",
    ],
)
def test_only_the_current_author_body_is_returned_before_history_or_signature(tail):
    request = "Hello team,\n\nHey Hermes,\nPlease find the earlier test evidence."
    assert current_request(request + "\n\n" + tail) == request


def test_outlook_style_original_header_block_is_a_quote_boundary() -> None:
    assert (
        current_request(
            "Updated notes.\n\n"
            "From: Owner <owner@example.test>\n"
            "Sent: Tuesday, November 10, 2026\n"
            "To: Group <group@example.test>\nSubject: Old test request\n\n"
            "Hey Hermes, run this old task."
        )
        is None
    )


def test_reply_all_keeps_owner_and_twelve_visible_people_in_original_roles() -> None:
    people = [f"person{i}@example.test" for i in range(12)]
    message = message_with(
        ("From", "Owner <owner@example.test>"),
        (
            "To",
            "Agent <agent@example.test>, OWNER@example.test, " + ", ".join(people[:6]),
        ),
        ("Cc", ", ".join(people[6:]) + ", Agent Alias <alias@example.test>"),
        ("Bcc", "hidden@example.test"),
    )
    assert reply_recipients(message, {"AGENT@example.test", "alias@example.test"}) == {
        "to": ["owner@example.test", *people[:6]],
        "cc": people[6:],
    }


@pytest.mark.parametrize(
    "headers,expected",
    [
        ([], {"to": ["owner@example.test"], "cc": []}),
        ([("To", "agent@example.test")], {"to": ["owner@example.test"], "cc": []}),
        (
            [("Cc", "peer@example.test")],
            {"to": ["owner@example.test"], "cc": ["peer@example.test"]},
        ),
    ],
)
def test_missing_optional_visible_recipient_headers_do_not_drop_the_sender(
    headers, expected
) -> None:
    message = message_with(("From", "owner@example.test"), *headers)
    assert reply_recipients(message, {"agent@example.test"}) == expected


def test_all_visible_header_instances_keep_order_and_deduplicate_without_promotion():
    message = message_with(
        ("From", '"Owner, Example" <OWNER@example.test>'),
        ("To", "Agent <agent@example.test>, Alpha <ALPHA@example.test>"),
        ("To", "Beta <beta@example.test>, owner@example.test"),
        ("Cc", "Beta Again <BETA@example.test>, Gamma <gamma@example.test>"),
        ("Cc", "Alpha Again <alpha@example.test>, Delta <DELTA@example.test>"),
        ("Bcc", "gamma@example.test, hidden@example.test"),
    )
    assert reply_recipients(message, {"AGENT@example.test"}) == {
        "to": ["owner@example.test", "alpha@example.test", "beta@example.test"],
        "cc": ["gamma@example.test", "delta@example.test"],
    }


def test_folded_visible_addresses_and_explicit_group_members_are_preserved() -> None:
    message = Parser(policy=email_policy.default).parsestr(
        "From: Owner <owner@example.test>\r\n"
        "To: Agent <agent@example.test>,\r\n"
        ' Team: "Alpha, Example" <alpha@example.test>, beta@example.test;\r\n'
        "Cc: Gamma <gamma@example.test>\r\n\r\n"
    )
    assert reply_recipients(message, {"agent@example.test"}) == {
        "to": ["owner@example.test", "alpha@example.test", "beta@example.test"],
        "cc": ["gamma@example.test"],
    }


@pytest.mark.parametrize(
    "reply_to",
    [
        "outsider@attacker.test",
        '"Owner" <outsider@attacker.test>',
        "owner@example.test, outsider@attacker.test",
        "owner@example.test\r\nBcc: outsider@attacker.test",
        "not an address",
        "owner@example.test,",
    ],
)
def test_reply_to_cannot_redirect_or_silently_replace_the_visible_group(reply_to):
    message = message_with(
        ("From", "owner@example.test"),
        ("To", "agent@example.test, peer@example.test"),
        ("Cc", "colleague@example.test"),
        ("Reply-To", reply_to),
    )
    with pytest.raises(ValueError):
        reply_recipients(message, {"agent@example.test"})


def test_reply_to_an_existing_participant_neither_adds_nor_promotes_addresses():
    message = message_with(
        ("From", "owner@example.test"),
        ("To", "agent@example.test, peer@example.test"),
        ("Cc", "colleague@example.test"),
        ("Reply-To", "Colleague <COLLEAGUE@example.test>"),
    )
    assert reply_recipients(message, {"agent@example.test"}) == {
        "to": ["owner@example.test", "peer@example.test"],
        "cc": ["colleague@example.test"],
    }


def test_multiple_reply_to_fields_are_ambiguous_even_if_one_is_safe() -> None:
    message = message_with(
        ("From", "owner@example.test"),
        ("To", "agent@example.test, peer@example.test"),
        ("Reply-To", "owner@example.test"),
        ("Reply-To", "peer@example.test"),
    )
    with pytest.raises(ValueError):
        reply_recipients(message, {"agent@example.test"})


@pytest.mark.parametrize(
    "value",
    [
        "not an address",
        "peer@example.test,",
        "peer@example.test,,second@example.test",
        "peer@example.test; second@example.test",
        "peer@example.test\r\nBcc: outside@attacker.test",
        "peer@example.test\nBcc: outside@attacker.test",
        '"injected\x00name" <peer@example.test>',
        "peer@-example.test",
        "peer@example-.test",
        "peer@under_score.test",
        "peer@" + "a" * 64 + ".test",
        "a" * 65 + "@example.test",
        "peer@example.test (" + "(" * 1500 + "note" + ")" * 1501,
    ],
)
def test_malformed_visible_recipients_raise_instead_of_falling_back_to_sender(value):
    message = message_with(
        ("From", "owner@example.test"),
        ("To", "agent@example.test"),
        ("Cc", value),
    )
    with pytest.raises(ValueError):
        reply_recipients(message, {"agent@example.test"})


@pytest.mark.parametrize(
    "self_addresses",
    [
        {"agent@example.test, outside@attacker.test"},
        {"not an address"},
        {"agent@example.test\r\nBcc: outside@attacker.test"},
    ],
)
def test_self_addresses_are_validated_as_single_mailboxes(self_addresses) -> None:
    message = message_with(("From", "owner@example.test"))
    with pytest.raises(ValueError):
        reply_recipients(message, self_addresses)


def test_unicode_display_names_and_quoted_local_parts_are_not_lost() -> None:
    message = message_with(
        ("From", "Owner <owner@example.test>"),
        ("To", 'Zoë <plus+tag@example.test>, "spaced local"@example.test'),
    )
    assert reply_recipients(message, {"agent@example.test"}) == {
        "to": [
            "owner@example.test",
            "plus+tag@example.test",
            '"spaced local"@example.test',
        ],
        "cc": [],
    }


def test_default_email_policy_cannot_hide_a_malformed_raw_address_header() -> None:
    message = Parser(policy=email_policy.default).parsestr(
        "From: owner@example.test\r\nTo: peer@example.test,\r\n\r\n"
    )
    assert str(message["To"]) == "peer@example.test"
    with pytest.raises(ValueError):
        reply_recipients(message, {"agent@example.test"})


def test_recipient_limit_is_applied_to_the_combined_reply_without_truncation() -> None:
    people = [f"person{i}@example.test" for i in range(100)]
    message = message_with(
        ("From", "owner@example.test"),
        ("To", ", ".join(people[:50])),
        ("Cc", ", ".join(people[50:])),
    )
    with pytest.raises(ValueError):
        reply_recipients(message, {"agent@example.test"})


def test_exactly_one_hundred_unique_recipients_are_allowed_after_self_removal():
    people = [f"person{i}@example.test" for i in range(99)]
    message = message_with(
        ("From", "owner@example.test"),
        ("To", "agent@example.test, " + ", ".join(people[:50])),
        ("Cc", ", ".join(people[50:]) + ", OWNER@example.test, alias@example.test"),
    )
    recipients = reply_recipients(message, {"agent@example.test", "alias@example.test"})
    assert recipients == {"to": ["owner@example.test", *people[:50]], "cc": people[50:]}
    assert sum(map(len, recipients.values())) == 100


def test_combined_address_header_size_is_bounded_before_parsing_duplicates() -> None:
    message = message_with(
        ("From", "owner@example.test"),
        *(("To", '"' + "x" * 5000 + '" <peer@example.test>') for _ in range(14)),
    )
    with pytest.raises(ValueError):
        reply_recipients(message, {"agent@example.test"})


def test_oversized_single_recipient_mailbox_is_not_sent() -> None:
    address = "a" * 64 + "@" + ".".join(["b" * 63] * 4)
    message = message_with(("From", "owner@example.test"), ("To", address))
    with pytest.raises(ValueError):
        reply_recipients(message, {"agent@example.test"})


def test_self_only_reply_is_rejected_instead_of_sending_to_an_empty_envelope():
    message = message_with(("From", "agent@example.test"), ("To", "alias@example.test"))
    with pytest.raises(ValueError):
        reply_recipients(message, {"agent@example.test", "alias@example.test"})


def test_even_a_malformed_bcc_header_is_ignored_and_never_disclosed() -> None:
    message = message_with(
        ("From", "owner@example.test"),
        ("Bcc", "hidden@example.test\r\nBcc: outside@attacker.test"),
    )
    assert reply_recipients(message, {"agent@example.test"}) == {
        "to": ["owner@example.test"],
        "cc": [],
    }


@pytest.mark.parametrize(
    "boundary",
    [
        "________________________________",
        "[Outlook O365 forwarded message]",
        "Forwarded message:",
        "Original Message:",
    ],
)
def test_other_stripped_forward_markers_do_not_expose_an_old_request(boundary):
    assert (
        current_request("Current update.\n" + boundary + "\nHey Hermes, old task.")
        is None
    )


def test_html_stripped_current_text_keeps_a_multiline_task_but_not_its_signature():
    text = html_to_text(
        "<p>Hey Hermes,</p><p>Please find the earlier results.</p>"
        "<p>-- </p><p>Hey Hermes, not part of the task.</p>"
    )
    assert current_request(text) == "Hey Hermes,\nPlease find the earlier results."


def test_html_stripped_reply_attribution_does_not_turn_an_old_request_into_a_new_one():
    text = html_to_text(
        "<p>Current update.</p><p>On Tuesday, Owner wrote:</p>"
        "<blockquote><p>Hey Hermes, this is an old task.</p></blockquote>"
    )
    assert current_request(text) is None


def test_an_html_stripped_o365_sentinel_stops_history_without_complete_headers():
    text = html_to_text(
        "<p>Current update.</p><div id='divRplyFwdMsg'>"
        "<p>Hey Hermes, this is an old task.</p></div>"
    )
    assert current_request(text) is None


def test_current_request_preserves_internal_crlf_and_indentation() -> None:
    text = "Hey Hermes,\r\nPlease review:\r\n    keep this indentation."
    assert current_request(text + "\r\n\r\n-- \r\nSignature") == text


def test_a_group_syntax_from_is_not_a_single_authenticated_author() -> None:
    message = message_with(
        ("From", "Owners: owner@example.test;"),
        (
            "Authentication-Results",
            "mx.example.test; dmarc=pass header.from=example.test",
        ),
    )
    assert not authenticated_sender(message, "owner@example.test", "mx.example.test")
    with pytest.raises(ValueError):
        reply_recipients(message, {"agent@example.test"})


def test_a_group_syntax_claimed_sender_is_not_an_owner_mailbox() -> None:
    message = message_with(
        ("From", "owner@example.test"),
        (
            "Authentication-Results",
            "mx.example.test; dmarc=pass header.from=example.test",
        ),
    )
    assert not authenticated_sender(
        message, "Owners: owner@example.test;", "mx.example.test"
    )


def test_a_self_identity_cannot_be_an_address_group() -> None:
    message = message_with(("From", "owner@example.test"))
    with pytest.raises(ValueError):
        reply_recipients(message, {"Agents: agent@example.test;"})


@pytest.mark.parametrize(
    "result",
    [
        'dmarc=pass header.from="example.test"reason=missing-space',
        'dmarc=pass reason="ok"header.from=example.test',
    ],
)
def test_adjacent_quoted_tokens_cannot_manufacture_authentication_properties(result):
    message = message_with(
        ("From", "owner@example.test"),
        ("Authentication-Results", "mx.example.test; " + result),
    )
    assert not authenticated_sender(message, "owner@example.test", "mx.example.test")


def test_an_authentication_comment_can_separate_a_quoted_property() -> None:
    message = message_with(
        ("From", "owner@example.test"),
        (
            "Authentication-Results",
            'mx.example.test; dmarc=pass reason="ok"'
            "(separator)header.from=example.test",
        ),
    )
    assert authenticated_sender(message, "owner@example.test", "mx.example.test")


def test_receiving_server_dmarc_pass_authenticates_the_actual_from() -> None:
    message = message_with(
        ("From", "Owner <owner@example.test>"),
        (
            "Authentication-Results",
            "mx.example.test; dmarc=pass header.from=example.test",
        ),
    )
    assert authenticated_sender(message, "owner@example.test", "mx.example.test")


@pytest.mark.parametrize(
    "pin", ["", "other.example.test", "mx.example.test.attacker.test"]
)
def test_authentication_requires_an_exact_nonempty_receiving_server_pin(pin) -> None:
    message = message_with(
        ("From", "owner@example.test"),
        (
            "Authentication-Results",
            "mx.example.test; dmarc=pass header.from=example.test",
        ),
    )
    assert not authenticated_sender(message, "owner@example.test", pin)


@pytest.mark.parametrize(
    "top",
    [
        "mx.example.test; dmarc=fail header.from=example.test",
        "untrusted.example.test; dmarc=pass header.from=example.test",
        "mx.example.test.attacker.test; dmarc=pass header.from=example.test",
    ],
)
def test_a_lower_authentication_header_never_overrides_the_topmost(top) -> None:
    message = message_with(
        ("From", "owner@example.test"),
        ("Authentication-Results", top),
        (
            "Authentication-Results",
            "mx.example.test; dmarc=pass header.from=example.test",
        ),
    )
    assert not authenticated_sender(message, "owner@example.test", "mx.example.test")
