"""Fail-closed, side-effect-free policies for inbound email and visible reply-all.

The caller owns owner-alias allowlisting and dispatch. Authentication here trusts
only the first Authentication-Results field from an exactly pinned receiving MTA;
that MTA must remove inbound fields claiming its identity (RFC 8601). A matching
header is not independent cryptographic proof of provenance.
"""

from __future__ import annotations

import re
from email import policy as email_policy
from email.errors import HeaderParseError
from email.message import Message
from email.utils import getaddresses

MAX_HEADER_BYTES = 64 * 1024
MAX_RECIPIENTS = 100


def _unfold_header(value: str) -> str:
    if len(value) > MAX_HEADER_BYTES or len(value.encode("utf-8")) > MAX_HEADER_BYTES:
        raise ValueError("Header exceeds the policy size limit")
    value = re.sub(r"\r?\n[ \t]+", " ", value)
    if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value):
        raise ValueError("Header contains an injected newline or control character")
    return value


def _addresses(value: str, *, allow_groups: bool = True) -> list[str]:
    """Use both strict stdlib parsers: neither may silently repair the input."""
    value = _unfold_header(value)
    try:
        pairs = getaddresses([value], strict=True)
        parsed = email_policy.default.header_factory("To", value)
    except (HeaderParseError, IndexError, RecursionError) as exc:
        raise ValueError("Malformed or overly complex email address list") from exc
    if (
        parsed.defects
        or (
            not allow_groups
            and any(group.display_name is not None for group in parsed.groups)
        )
        or not pairs
        or any(not address for _, address in pairs)
        or len(pairs) != len(parsed.addresses)
        or any(
            not address.username or not address.domain for address in parsed.addresses
        )
    ):
        raise ValueError("Malformed email address list")
    addresses = []
    for address in parsed.addresses:
        if (
            len(address.addr_spec.encode("ascii")) > 254
            or len(address.addr_spec.rsplit("@", 1)[0].encode("ascii")) > 64
            or any(
                not re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label
                )
                for label in address.domain.split(".")
            )
        ):
            raise ValueError("Unsupported SMTP mailbox")
        addresses.append(address.addr_spec.lower())
    return addresses


def _from_address(message: Message) -> str:
    values = [value for name, value in message.raw_items() if name.lower() == "from"]
    if (
        len(values) != 1
        or len(addresses := _addresses(values[0], allow_groups=False)) != 1
    ):
        raise ValueError("A single unambiguous From is required")
    return addresses[0]


def _auth_clauses(text: str) -> list[list[tuple[str, bool]]]:
    """Tokenize a limited RFC 8601 grammar, never treating comments as verdicts.

    Each token retains whether it was quoted. Semicolons inside nested comments
    or quoted strings cannot introduce another result or property.
    """
    clauses: list[list[tuple[str, bool]]] = [[]]
    i = 0
    while i < len(text):
        char = text[i]
        if char.isspace():
            i += 1
        elif char == "(":
            depth = 1
            i += 1
            while i < len(text) and depth:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                i += 1
            if depth:
                raise ValueError("Unterminated authentication comment")
        elif char == '"':
            value: list[str] = []
            i += 1
            while i < len(text) and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                    if i >= len(text):
                        raise ValueError("Unterminated authentication escape")
                value.append(text[i])
                i += 1
            if i >= len(text):
                raise ValueError("Unterminated authentication quoted string")
            clauses[-1].append(("".join(value), True))
            i += 1
            if i < len(text) and not (text[i].isspace() or text[i] in "(;"):
                raise ValueError("Missing delimiter after authentication quoted string")
        elif char == ";":
            clauses.append([])
            i += 1
        elif char == "=":
            clauses[-1].append((char, False))
            i += 1
        elif char in ")\\":
            raise ValueError("Unexpected authentication delimiter")
        else:
            end = i + 1
            while end < len(text) and not (
                text[end].isspace() or text[end] in '()";=\\'
            ):
                end += 1
            clauses[-1].append((text[i:end], False))
            i = end
    return clauses


def _auth_results(
    clauses: list[list[tuple[str, bool]]],
) -> list[tuple[str, str, list[tuple[str, str]]]]:
    results: list[tuple[str, str, list[tuple[str, str]]]] = []
    for clause in clauses:
        if clause == [("none", False)]:
            continue
        if (
            len(clause) < 3
            or clause[0][1]
            or clause[1] != ("=", False)
            or clause[2][1]
            or (len(clause) - 3) % 3
        ):
            raise ValueError("Malformed authentication result")
        props = []
        for i in range(3, len(clause), 3):
            if (
                clause[i][1]
                or clause[i + 1] != ("=", False)
                or clause[i + 2] == ("=", False)
            ):
                raise ValueError("Malformed authentication property")
            props.append((clause[i][0].lower(), clause[i + 2][0]))
        results.append((clause[0][0].lower(), clause[2][0].lower(), props))
    return results


def reply_recipients(
    message: Message, self_addresses: set[str]
) -> dict[str, list[str]]:
    """Return lowercase address-only To/Cc arrays, or raise ValueError to hold mail.

    The caller must authenticate the single From first. Prepend it to visible To,
    preserve Cc order/roles, deduplicate across both, and exclude all self aliases.
    Bcc is never inspected; Reply-To must stay within the original participants
    and never changes their roles. No sender-only fallback or truncation is used.

    Allow at most 100 unique non-self recipients and 64 KiB combined raw values
    for From/To/Cc/Reply-To. Supported mailboxes have ASCII local parts and DNS
    domains (no SMTPUTF8 or address literals); Unicode display names are allowed.
    """
    header_bytes = 0
    for name, value in message.raw_items():
        if name.lower() in {"from", "to", "cc", "reply-to"}:
            if len(value) > MAX_HEADER_BYTES:
                raise ValueError("Address headers exceed the policy size limit")
            header_bytes += len(value.encode("utf-8"))
            if header_bytes > MAX_HEADER_BYTES:
                raise ValueError("Address headers exceed the policy size limit")
    seen: set[str] = set()
    for value in self_addresses:
        addresses = _addresses(value, allow_groups=False)
        if len(addresses) != 1:
            raise ValueError("Each self address must be a single mailbox")
        seen.add(addresses[0])
    result: dict[str, list[str]] = {"to": [], "cc": []}

    def header_addresses(header_name: str) -> list[str]:
        return [
            address
            for name, value in message.raw_items()
            if name.lower() == header_name
            for address in _addresses(value)
        ]

    original_to = [_from_address(message), *header_addresses("to")]
    original_cc = header_addresses("cc")
    participants = set(original_to + original_cc)
    reply_to = [
        value for name, value in message.raw_items() if name.lower() == "reply-to"
    ]
    if len(reply_to) > 1:
        raise ValueError("Multiple Reply-To fields are ambiguous")
    if reply_to and not set(_addresses(reply_to[0])).issubset(participants):
        raise ValueError("Reply-To redirects outside the visible participants")
    for role, addresses in (("to", original_to), ("cc", original_cc)):
        for address in addresses:
            if address not in seen:
                seen.add(address)
                result[role].append(address)
    recipient_count = sum(map(len, result.values()))
    if not 1 <= recipient_count <= MAX_RECIPIENTS:
        raise ValueError("Reply requires between 1 and 100 visible non-self recipients")
    return result


def current_request(text: str) -> str | None:
    """Return the whole current author text for explicit line-leading addressing.

    Keep internal whitespace, trimming only edges and known quote/forward/signature
    boundaries. Input must be plain, quote-preserving text, not raw HTML: once an
    HTML converter discards blockquote boundaries they cannot be recovered here.
    An invocation inside history or a signature never authorizes the old task.
    """
    boundary = re.compile(
        r"^[ \t]*(?:>"
        r"|on[ \t]+(?:[^\n]*\n){0,3}?[^\n]*\bwrote:[ \t]*\r?$"
        r"|-{2,}[ \t]*(?:original|forwarded)[ \t]+message[ \t]*-{2,}[ \t]*\r?$"
        r"|(?:begin forwarded|forwarded|original) message:[ \t]*\r?$"
        r"|_{5,}[ \t]*\r?$"
        r"|\[Outlook O365 forwarded message\][ \t]*\r?$"
        r"|--[ \t]*\r?$"
        r"|(?:thanks|thank you|best(?: regards)?|kind regards|regards|cheers|sincerely)"
        r"[,!]?[ \t]*\r?$"
        r"|sent from my[ \t]+"
        r"|from:[^\n]+\n[ \t]*(?:sent|date):)",
        re.IGNORECASE | re.MULTILINE,
    )
    match = boundary.search(text)
    current = text[: match.start()].strip() if match else text.strip()
    invocation = re.compile(
        r"^[ \t]*(?:hey[ \t]+hermes(?=$|[ \t,:!?.\r\n])|hermes[ \t]*[:,!])",
        re.IGNORECASE | re.MULTILINE,
    )
    return current if invocation.search(current) else None


def authenticated_sender(message: Message, sender: str, authserv_id: str) -> bool:
    """Verify a single actual From against the topmost pinned receiver result.

    Pin comparison is exact and case-insensitive, never a hostname prefix or a
    search through lower fields. Require strict domain equality, not relaxed
    public-suffix alignment: DMARC needs one header.from; SPF one smtp.mailfrom;
    DKIM one header.d per passing signature. Missing/ambiguous/unsupported signals
    fail closed. This does not replace owner-alias allowlisting by the caller.
    """
    if not authserv_id:
        return False
    try:
        from_address = _from_address(message)
        if _addresses(sender, allow_groups=False) != [from_address]:
            return False
        from_domain = from_address.rsplit("@", 1)[1]
        headers = [
            value
            for name, value in message.raw_items()
            if name.lower() == "authentication-results"
        ]
        if not headers:
            return False
        header = _unfold_header(headers[0])
        if not header.isascii():
            return False
        clauses = _auth_clauses(header)
        if len(clauses[0]) != 1 or clauses[0][0][1]:
            return False
        if clauses[0][0][0].lower() != authserv_id.lower():
            return False
        results = _auth_results(clauses[1:])
        dmarc = [
            (result, props) for method, result, props in results if method == "dmarc"
        ]
        spf = [(result, props) for method, result, props in results if method == "spf"]
        if len(dmarc) > 1:
            return False

        def aligned(props: list[tuple[str, str]], property_name: str) -> bool:
            values = [value.lower() for name, value in props if name == property_name]
            if len(values) != 1:
                return False
            value = values[0]
            if property_name == "smtp.mailfrom" and "@" in value:
                addresses = _addresses(value)
                return addresses == [value] and value.rsplit("@", 1)[1] == from_domain
            return value == from_domain

        if dmarc and dmarc[0][0] == "pass" and aligned(dmarc[0][1], "header.from"):
            return True
        if (
            len(spf) == 1
            and spf[0][0] == "pass"
            and aligned(spf[0][1], "smtp.mailfrom")
        ):
            return True
        return any(
            method == "dkim" and result == "pass" and aligned(props, "header.d")
            for method, result, props in results
        )
    except ValueError:
        return False
