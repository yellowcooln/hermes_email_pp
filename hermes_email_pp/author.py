"""Separate current-author text from untrusted body/history reference data."""

from email.message import Message
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_REFERENCE_BYTES = 16 * 1024

_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _AuthorHTML(HTMLParser):
    def __init__(self, *, include_quotes=False):
        super().__init__(convert_charrefs=True)
        self.include_quotes = include_quotes
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []
        self.links: list[tuple[int, str]] = []

    def _hidden(self):
        return bool(self.stack and self.stack[-1][1])

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").lower().split())
        hidden = (
            self._hidden()
            or tag in {"script", "style"}
            or (
                not self.include_quotes
                and (
                    tag == "blockquote"
                    or bool(
                        classes & {"gmail_quote", "yahoo_quoted", "moz-cite-prefix"}
                    )
                    or (attrs.get("id") or "").lower() == "divrplyfwdmsg"
                )
            )
        )
        if tag not in _VOID:
            self.stack.append((tag, hidden))
        if not hidden and tag == "a":
            self.links.append((len(self.stack), attrs.get("href") or ""))
        if not hidden and tag in {"p", "div", "br", "hr", "li"}:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        # A void/mismatched closing tag must never pop a surrounding quote.
        if tag in _VOID or not self.stack or self.stack[-1][0] != tag:
            return
        if tag == "a" and self.links and self.links[-1][0] == len(self.stack):
            _, target = self.links.pop()
            if _safe_http_target(target):
                self.parts.append(f" ({target})")
        self.stack.pop()
        if not self._hidden() and tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._hidden():
            self.parts.append(data)


def _safe_http_target(target: str) -> bool:
    # Reject credentials and literal whitespace/control characters rather than
    # letting urlsplit silently strip them and synthesize instruction lines.
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in target):
        return False
    try:
        url = urlsplit(target)
        return (
            url.scheme.lower() in {"http", "https"}
            and bool(url.hostname)
            and url.username is None
            and url.password is None
        )
    except ValueError:
        return False


def _body(message: Message, *, prefer_html: bool = False) -> Message | None:
    if (
        message.get_content_disposition() == "attachment"
        or message.get_content_maintype() == "message"
        or message.get_filename()
    ):
        return None  # Prune whole attached-message subtrees, not just containers.
    if message.is_multipart():
        children = message.get_payload()
        if not isinstance(children, list):
            return None
        candidates = [
            part
            for child in children
            if isinstance(child, Message)
            and (part := _body(child, prefer_html=prefer_html)) is not None
        ]
        if message.get_content_subtype() == "alternative":
            return next(
                (
                    part
                    for part in candidates
                    if part.get_content_type()
                    == ("text/html" if prefer_html else "text/plain")
                ),
                candidates[0] if candidates else None,
            )
        # Mixed/related: first body branch wins, not a later attachment.
        return candidates[0] if candidates else None
    return (
        message if message.get_content_type() in {"text/plain", "text/html"} else None
    )


def author_text(message: Message, fallback: str = "") -> str:
    part = _body(message)
    if part is None:
        return ""  # An absent body cannot borrow text from a flattened MIME walk.
    payload = part.get_payload(decode=True) or b""
    if not isinstance(payload, bytes):
        return ""
    try:
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        text = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/plain":
        return text
    parser = _AuthorHTML()
    parser.feed(text)
    return "".join(parser.parts)


def reference_text(message: Message) -> str:
    """Return at most 16 KiB UTF-8 of untrusted body/history, never invocation input.

    Retain both plain and HTML alternatives when present to expose HTML hrefs
    without losing plain-only text. Attached mail/files and later mixed branches
    are excluded. This function performs no fetching or authorization.
    """
    plain = _body(message)
    html = _body(message, prefer_html=True)
    parts = [plain] if html is plain else [plain, html]
    texts = []
    for part in parts:
        if part is None:
            continue
        payload = part.get_payload(decode=True) or b""
        if not isinstance(payload, bytes):
            continue
        try:
            text = payload.decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            parser = _AuthorHTML(include_quotes=True)
            parser.feed(text)
            text = "".join(parser.parts)
        texts.append(text)
    return (
        "\n".join(texts)
        .encode("utf-8")[:MAX_REFERENCE_BYTES]
        .decode("utf-8", errors="ignore")
    )
