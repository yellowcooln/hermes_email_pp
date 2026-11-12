"""Secure IMAP/SMTP transport for the Email++ Hermes platform."""

from __future__ import annotations

import asyncio
import base64
import email
import imaplib
import json
import logging
import mimetypes
import os
import re
import secrets
import smtplib
import ssl
import tempfile
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from gateway.config import Platform  # type: ignore[import-not-found]
from gateway.platforms.base import (  # type: ignore[import-not-found]
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
    validate_inbound_media_size,
    validate_media_delivery_path,
)

from .config import (
    delete_processed,
    environment_settings,
    outbound_attachment_limit,
    process_history_window,
)
from .forwarding import (
    ForwardedMessage,
    hermes_prompt,
    html_to_text,
    is_suspected_forward,
    parse_forward,
)
from .rendering import quote_html, quote_plain, render_markdown
from .threading import (
    DEFAULT_MAX_THREADS,
    EmailThreadRouter,
    ThreadRoute,
    active_profile_home,
)

_AUTOMATED = ("noreply", "no-reply", "mailer-daemon", "postmaster", "bounce")
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MESSAGE_ID = re.compile(r"<[^\s<>@]+@[^\s<>@]+>")
_MESSAGE_IDS = re.compile(r"<[^\s<>@]+@[^\s<>@]+>(?:\s+<[^\s<>@]+@[^\s<>@]+>)*")
_INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
_POLL_BATCH_SIZE = 25
_SMTP_REPLY_POLICY = SMTP.clone(max_line_length=998)
_MAX_THREAD_INDEX_LEVELS = 100
_MAX_REFERENCES_LENGTH = 900
_DELIVERY_COMPLETION_TIMEOUT = 600
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_ATTACHMENT_ENVELOPE_RE = re.compile(
    r"\[\[email_pp_attachments:v1:([A-Za-z0-9_-]+)\]\]"
)
_UNRESOLVED_MEDIA_RE = re.compile(r"MEDIA:\s*[`\"']?(?:~/|/|[A-Za-z]:[/\\])")
_MAX_ATTACHMENT_ENVELOPE_BYTES = 16 * 1024
_MAX_ATTACHMENT_ENVELOPE_PATHS = 100
logger = logging.getLogger(__name__)


@dataclass
class _MailboxState:
    """State retained across automatic reconnects in one gateway process."""

    uidvalidity: bytes | None
    baseline_uid: int
    seen: set[bytes]


@dataclass(frozen=True)
class _InboundMail:
    """One fetched IMAP delivery bound to its UIDVALIDITY generation."""

    raw: bytes
    uid: bytes
    uidvalidity: bytes | None


@dataclass(frozen=True)
class _AttachmentEnvelope:
    """Validated attachment references carried through Hermes' text-only seam."""

    paths: tuple[Path, ...]
    invalid: bool = False


@dataclass(frozen=True)
class _PreparedAttachment:
    """One attachment snapshotted before opening the SMTP transaction."""

    data: bytes
    maintype: str
    subtype: str
    filename: str


def _encode_attachment_envelope(envelope: _AttachmentEnvelope) -> str:
    """Encode bounded private metadata that never reaches the recipient body."""
    payload = {
        "paths": [str(path) for path in envelope.paths],
        "invalid": envelope.invalid,
    }
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    marker = f"[[email_pp_attachments:v1:{encoded}]]"
    if len(marker.encode("utf-8")) > _MAX_ATTACHMENT_ENVELOPE_BYTES:
        return _encode_attachment_envelope(_AttachmentEnvelope((), invalid=True))
    return marker


def _append_attachment_envelope(content: str, envelope: _AttachmentEnvelope) -> str:
    marker = _encode_attachment_envelope(envelope)
    return f"{content.rstrip()}\n{marker}" if content.strip() else marker


def _decode_attachment_envelope(
    content: str,
) -> tuple[str, _AttachmentEnvelope | None]:
    """Remove one final envelope marker, treating malformed metadata as failure."""
    matches = list(_ATTACHMENT_ENVELOPE_RE.finditer(content))
    if not matches:
        return content, None
    marker = matches[-1]
    body = content[: marker.start()].rstrip()
    if len(matches) != 1 or content[marker.end() :].strip():
        return body, _AttachmentEnvelope((), invalid=True)
    encoded = marker.group(1)
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(raw) > _MAX_ATTACHMENT_ENVELOPE_BYTES:
            raise ValueError("attachment envelope is too large")
        payload = json.loads(raw)
        paths = payload["paths"]
        invalid = payload["invalid"]
        if (
            not isinstance(paths, list)
            or len(paths) > _MAX_ATTACHMENT_ENVELOPE_PATHS
            or not isinstance(invalid, bool)
            or not all(isinstance(path, str) and path for path in paths)
        ):
            raise ValueError("attachment envelope has invalid fields")
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return body, _AttachmentEnvelope((), invalid=True)
    return body, _AttachmentEnvelope(tuple(Path(path) for path in paths), invalid)


def _decode(value: str) -> str:
    """Decode an RFC 2047 header without letting a bad charset abort a poll."""
    decoded: list[str] = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _address(value: str) -> str:
    """Return a normalized RFC address or an empty value for unsafe input."""
    _, address = parseaddr(value)
    address = address.strip().lower()
    return address if "@" in address and " " not in address else ""


def _message_ids(value: object) -> list[str]:
    """Return valid Message-IDs without permitting header injection."""
    if not isinstance(value, str):
        return []
    value = re.sub(r"\r?\n[ \t]+", " ", value)
    if "\r" in value or "\n" in value:
        return []
    return _MESSAGE_ID.findall(value) if _MESSAGE_IDS.fullmatch(value.strip()) else []


def _header_text(value: object) -> str:
    """Return an unfolded display header while rejecting injected fields."""
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\r?\n[ \t]+", " ", value)
    if "\r" in value or "\n" in value:
        return ""
    return _decode(value).strip()


def _reply_thread_index(value: str, now: datetime | None = None) -> str | None:
    """Append an Outlook-compatible response block to a conversation index."""
    try:
        raw = base64.b64decode("".join(value.split()), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    if (
        len(raw) < 22
        or (len(raw) - 22) % 5
        or raw[0] != 1
        or (len(raw) - 22) // 5 >= _MAX_THREAD_INDEX_LEVELS
    ):
        return None
    anchor = int.from_bytes(raw[:6], "big") << 16
    for offset in range(22, len(raw), 5):
        block = int.from_bytes(raw[offset : offset + 4], "big")
        anchor += (block & 0x7FFF_FFFF) << (23 if block >> 31 else 18)
    reply_time = now or datetime.now(timezone.utc)
    delta = reply_time.astimezone(timezone.utc) - _FILETIME_EPOCH
    filetime = (delta.days * 86_400 + delta.seconds) * 10**7 + delta.microseconds * 10
    difference = abs((filetime & ~0xFFFF) - anchor)
    delta_code, shift = (0, 18) if difference < 1 << 49 else (1, 23)
    response = (
        ((delta_code << 31 | (difference >> shift) & 0x7FFF_FFFF) << 8)
        | secrets.randbits(8)
    ).to_bytes(5, "big")
    return base64.b64encode(raw + response).decode("ascii")


def _reply_subject(subject: str) -> str:
    """Return one reply prefix while retaining a forwarding wrapper's topic."""
    subject = re.sub(r"^(?:\s*re\s*:\s*)+", "", subject, flags=re.I).strip()
    return f"Re: {subject}" if subject else "Re: Hermes Agent"


def _references_header(references: list[str], reply_to: str) -> str:
    """Retain the root and newest ancestors within one literal SMTP header line."""
    identifiers = list(dict.fromkeys([*references, reply_to]))
    selected = [identifiers[0]]
    for identifier in reversed(identifiers[1:]):
        if len(" ".join([identifier, *selected])) > _MAX_REFERENCES_LENGTH:
            continue
        selected.insert(1, identifier)
    return " ".join(selected)


def _display_name(value: str) -> str:
    """Extract a header-safe display name from a decoded mailbox header."""
    name, _ = parseaddr(_decode(value))
    return name.replace("\r", "").replace("\n", "").strip()


class EmailPPAdapter(BasePlatformAdapter):
    """Poll a TLS mailbox and deliver only explicitly routed email replies."""

    # SMTP cannot revise a message after acceptance. Hermes must wait for the
    # complete response so the body and explicit MEDIA files share one message.
    SUPPORTS_MESSAGE_EDITING = False
    _mailbox_states: dict[tuple[str, str, str, str], _MailboxState] = {}

    def __init__(self, config: Any) -> None:
        super().__init__(config, Platform("email_pp"))
        extra = getattr(config, "extra", {}) or {}
        settings = environment_settings()
        self._address = self._setting(settings, extra, "EMAIL_PP_ADDRESS", "address")
        self._login = (
            self._setting(settings, extra, "EMAIL_PP_LOGIN", "login") or self._address
        )
        self._password = self._setting(settings, extra, "EMAIL_PP_PASSWORD", "password")
        self._imap_host = self._setting(
            settings, extra, "EMAIL_PP_IMAP_HOST", "imap_host"
        )
        self._smtp_host = self._setting(
            settings, extra, "EMAIL_PP_SMTP_HOST", "smtp_host"
        )
        self._imap_port = self._integer(
            settings, extra, "EMAIL_PP_IMAP_PORT", "imap_port", 993
        )
        self._smtp_port = self._integer(
            settings, extra, "EMAIL_PP_SMTP_PORT", "smtp_port", 587
        )
        self._sender_name = (
            self._setting(settings, extra, "EMAIL_PP_SENDER_NAME", "sender_name")
            or "Hermes Agent"
        )
        self._poll_interval = self._integer(
            settings, extra, "EMAIL_PP_POLL_INTERVAL", "poll_interval", 15
        )
        self._mailbox = (
            self._setting(settings, extra, "EMAIL_PP_MAILBOX", "mailbox") or "INBOX"
        )
        allowed_users = self._setting(
            settings, extra, "EMAIL_PP_ALLOWED_USERS", "allowed_users"
        )
        self._allowed = {
            value.strip().lower()
            for value in allowed_users.split(",")
            if _address(value)
        }
        self._allow_all = self._setting(
            settings, extra, "EMAIL_PP_ALLOW_ALL_USERS", "allow_all_users"
        ).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._require_auth = settings.get(
            "EMAIL_PP_REQUIRE_AUTHENTICATED_SENDER",
            str(extra.get("require_authenticated_sender", True)),
        ).lower() not in {"0", "false", "no", "off"}
        self._authserv_id = self._setting(
            settings, extra, "EMAIL_PP_AUTHSERV_ID", "authserv_id"
        )
        self._quote_mode = self._parse_quote_mode(settings, extra)
        self._process_history_window = process_history_window(
            self._setting(
                settings,
                extra,
                "EMAIL_PP_PROCESS_HISTORY_WINDOW",
                "process_history_window",
            )
        )
        self._delete_processed = delete_processed(
            self._setting(
                settings, extra, "EMAIL_PP_DELETE_PROCESSED", "delete_processed"
            )
        )
        self._max_outbound_attachments = outbound_attachment_limit(
            self._setting(
                settings,
                extra,
                "EMAIL_PP_MAX_OUTBOUND_ATTACHMENTS",
                "max_outbound_attachments",
            ),
            setting="EMAIL_PP_MAX_OUTBOUND_ATTACHMENTS",
            default=10,
        )
        self._max_outbound_total_bytes = outbound_attachment_limit(
            self._setting(
                settings,
                extra,
                "EMAIL_PP_MAX_OUTBOUND_TOTAL_BYTES",
                "max_outbound_total_bytes",
            ),
            setting="EMAIL_PP_MAX_OUTBOUND_TOTAL_BYTES",
            default=15 * 1024 * 1024,
        )
        self._seen: set[bytes] = set()
        self._baseline_uid = 0
        self._uidvalidity: bytes | None = None
        self._history_cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=max(0, self._process_history_window)
        )
        self._imap: imaplib.IMAP4 | None = None
        self._imap_lock = threading.Lock()
        self._poll_task: asyncio.Task[None] | None = None
        self._route_cache_size = DEFAULT_MAX_THREADS
        self._routes: OrderedDict[tuple[str, str], ThreadRoute] = OrderedDict()
        self._outlook_reply_context: OrderedDict[tuple[str, str], tuple[str, str]] = (
            OrderedDict()
        )
        self._router = EmailThreadRouter()
        self._pending_deletions = self._load_pending_deletions()
        self._response_deliveries: set[tuple[str, str]] = set()
        self._delivery_queues: dict[str, deque[MessageEvent]] = {}
        self._active_delivery_threads: set[str] = set()
        self._active_deliveries: dict[str, MessageEvent] = {}
        self._delivery_watchdogs: dict[str, asyncio.Task[None]] = {}
        self._delivery_completion_timeout = _DELIVERY_COMPLETION_TIMEOUT

    @staticmethod
    def _setting(settings: dict[str, str], extra: Any, env: str, key: str) -> str:
        value = settings.get(env) or extra.get(key, "")
        if isinstance(value, (list, tuple, set)):
            value = ",".join(str(item) for item in value)
        return str(value).strip()

    @classmethod
    def _integer(
        cls, settings: dict[str, str], extra: Any, env: str, key: str, default: int
    ) -> int:
        try:
            return int(cls._setting(settings, extra, env, key))
        except ValueError:
            return default

    @classmethod
    def _parse_quote_mode(cls, settings: dict[str, str], extra: Any) -> str:
        mode = cls._setting(settings, extra, "EMAIL_PP_QUOTE_MODE", "quote_mode")
        mode = mode.lower() or "always"
        if mode not in {"always", "forwarded", "never"}:
            raise ValueError(
                "EMAIL_PP_QUOTE_MODE must be one of: always, forwarded, never"
            )
        return mode

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Validate both TLS transports and begin asynchronous mailbox polling."""
        if not all((self._address, self._password, self._imap_host, self._smtp_host)):
            logger.warning(
                "Email++ connection rejected because required settings are missing"
            )
            return False
        try:
            logger.info("Email++ IMAP connection starting")
            if not self._acquire_platform_lock(
                "email_pp", self._address, "Email++ mailbox"
            ):
                return False
            await asyncio.to_thread(self._baseline_mailbox, is_reconnect)
            await asyncio.to_thread(self._probe_smtp)
            await asyncio.to_thread(self._retry_pending_deletions)
        except (OSError, imaplib.IMAP4.error, smtplib.SMTPException):
            logger.warning("Email++ connection validation failed")
            await asyncio.to_thread(self._close_imap_session)
            self._release_platform_lock()
            return False
        self._mark_connected()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Email++ IMAP polling started")
        return True

    async def disconnect(self) -> None:
        """Stop polling without leaving a running task behind."""
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await asyncio.to_thread(self._close_imap_session)
        self._release_platform_lock()
        self._mark_disconnected()
        logger.info("Email++ IMAP polling stopped")

    def _baseline_mailbox(self, is_reconnect: bool) -> None:
        def baseline(imap: imaplib.IMAP4) -> None:
            uidvalidity = self._select_mailbox(imap)
            cached = self._mailbox_states.get(self._mailbox_key)
            if (
                is_reconnect
                and cached is not None
                and cached.uidvalidity == uidvalidity
            ):
                self._restore_mailbox_state(cached)
                logger.info("Email++ IMAP reconnect restored mailbox state")
                return
            if is_reconnect and cached is not None:
                logger.warning(
                    "Email++ IMAP UIDVALIDITY changed; applying cold-start recovery"
                )
            self._start_cold_mailbox(imap, uidvalidity)

        self._with_imap(baseline)

    @property
    def _mailbox_key(self) -> tuple[str, str, str, str]:
        return (
            self._address.lower(),
            self._imap_host.lower(),
            str(self._imap_port),
            self._mailbox,
        )

    @property
    def _deletion_store_key(self) -> str:
        """Keep acknowledgement records isolated across IMAP mailboxes."""
        return "\0".join(
            (
                self._address.lower(),
                self._imap_host.lower(),
                str(self._imap_port),
                self._mailbox,
            )
        )

    @property
    def _deletion_store_path(self) -> Path:
        return active_profile_home() / "email_pp" / "pending_deletions.json"

    def _load_pending_deletions(self) -> set[tuple[bytes, bytes]]:
        """Load only validated, mailbox-local pending acknowledgement records."""
        if not self._delete_processed:
            return set()
        try:
            with self._deletion_store_path.open(encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return set()
        records = (
            stored.get(self._deletion_store_key, []) if isinstance(stored, dict) else []
        )
        if not isinstance(records, list):
            return set()
        pending: set[tuple[bytes, bytes]] = set()
        for record in records:
            if not isinstance(record, list) or len(record) != 2:
                continue
            try:
                uidvalidity = record[0].encode("ascii")
                uid = record[1].encode("ascii")
            except (AttributeError, UnicodeEncodeError):
                continue
            if uidvalidity and uid.isdigit():
                pending.add((uidvalidity, uid))
        return pending

    def _save_pending_deletions(self) -> None:
        """Atomically persist acknowledgement retries without storing mail content."""
        if not self._delete_processed:
            return
        path = self._deletion_store_path
        try:
            with path.open(encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, json.JSONDecodeError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored[self._deletion_store_key] = [
            [uidvalidity.decode("ascii"), uid.decode("ascii")]
            for uidvalidity, uid in sorted(self._pending_deletions)
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=".pending-deletions-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(stored, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _restore_mailbox_state(self, state: _MailboxState) -> None:
        self._uidvalidity = state.uidvalidity
        self._baseline_uid = state.baseline_uid
        self._seen = set(state.seen)

    def _save_mailbox_state(self) -> None:
        self._mailbox_states[self._mailbox_key] = _MailboxState(
            self._uidvalidity, self._baseline_uid, set(self._seen)
        )

    def _start_cold_mailbox(
        self, imap: imaplib.IMAP4, uidvalidity: bytes | None
    ) -> None:
        all_uids = self._search_uids(imap, "ALL")
        valid_uids = [
            value for uid in all_uids if (value := self._uid_number(uid)) is not None
        ]
        self._uidvalidity = uidvalidity
        self._baseline_uid = max(valid_uids, default=0)
        self._seen = set()
        self._history_cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=max(0, self._process_history_window)
        )
        self._save_mailbox_state()
        logger.info(
            "Email++ IMAP cold-start state recorded (history_window=%d)",
            self._process_history_window,
        )

    @staticmethod
    def _uid_number(uid: bytes) -> int | None:
        try:
            value = int(uid)
        except ValueError:
            logger.warning("Email++ IMAP ignored malformed UID response")
            return None
        return value if value >= 0 else None

    def _select_mailbox(self, imap: imaplib.IMAP4) -> bytes | None:
        status, _ = imap.select(self._mailbox)
        if status != "OK":
            logger.warning("Email++ IMAP mailbox selection failed")
            raise imaplib.IMAP4.error("IMAP mailbox selection failed")
        response = imap.response("UIDVALIDITY")
        if not isinstance(response, tuple) or len(response) != 2:
            logger.warning("Email++ IMAP returned malformed UIDVALIDITY response")
            return None
        _, values = response
        if not values or not isinstance(values[0], bytes):
            return None
        return values[0]

    def _with_imap(self, operation: Callable[[imaplib.IMAP4], Any]) -> Any:
        """Run one IMAP operation, reconnecting once after a broken session."""
        with self._imap_lock:
            for attempt in range(2):
                try:
                    return operation(self._get_imap())
                except (OSError, imaplib.IMAP4.error):
                    self._discard_imap()
                    if attempt:
                        raise
        raise AssertionError("IMAP retry loop exited unexpectedly")  # pragma: no cover

    def _get_imap(self) -> imaplib.IMAP4:
        if self._imap is None:
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, timeout=30)
            try:
                imap.login(self._login, self._password)
            except BaseException:
                self._close_imap(imap)
                raise
            self._imap = imap
        return self._imap

    def _discard_imap(self) -> None:
        if self._imap is None:
            return
        imap, self._imap = self._imap, None
        self._close_imap(imap)

    def _close_imap_session(self) -> None:
        """Wait for active workers before closing the shared IMAP connection."""
        with self._imap_lock:
            self._discard_imap()

    @staticmethod
    def _has_uidplus(imap: imaplib.IMAP4) -> bool:
        """Return whether targeted UID EXPUNGE is available on this server."""
        try:
            status, data = imap.capability()
        except (AttributeError, imaplib.IMAP4.error):
            return False
        return (
            status == "OK"
            and isinstance(data, (list, tuple))
            and b"UIDPLUS"
            in b" ".join(
                value.upper() for value in data if isinstance(value, bytes)
            ).split()
        )

    def _retry_pending_deletions(self) -> None:
        """Retry recorded UID-targeted deletions without redispatching mail."""
        if not self._delete_processed or not self._pending_deletions:
            return

        def retry(imap: imaplib.IMAP4) -> None:
            changed = False
            uidvalidity = self._select_mailbox(imap)
            if uidvalidity is None:
                logger.warning("Email++ cannot delete mail without UIDVALIDITY")
                return
            if not self._has_uidplus(imap):
                logger.warning(
                    "Email++ cannot delete processed mail because IMAP UIDPLUS is "
                    "unavailable"
                )
                return
            for expected_uidvalidity, uid in tuple(self._pending_deletions):
                if expected_uidvalidity != uidvalidity:
                    # UIDs may refer to unrelated mail after a UIDVALIDITY change.
                    self._pending_deletions.remove((expected_uidvalidity, uid))
                    changed = True
                    self._save_pending_deletions()
                    logger.warning(
                        "Email++ discarded stale processed-mail deletion after "
                        "UIDVALIDITY changed"
                    )
                    continue
                status, _ = imap.uid(
                    "store", uid.decode("ascii"), "+FLAGS.SILENT", r"(\Deleted)"
                )
                if status != "OK":
                    logger.warning("Email++ could not mark processed mail for deletion")
                    continue
                status, _ = imap.uid("expunge", uid.decode("ascii"))
                if status != "OK":
                    logger.warning("Email++ could not expunge processed mail by UID")
                    continue
                self._pending_deletions.remove((expected_uidvalidity, uid))
                changed = True
                self._save_pending_deletions()
                logger.info("Email++ deleted successfully processed mail")
            if changed:
                self._save_pending_deletions()

        self._with_imap(retry)

    def _remember_processed_delivery(self, delivery: _InboundMail) -> None:
        """Persist a successful response before attempting destructive cleanup."""
        if delivery.uidvalidity is None:
            logger.warning("Email++ retained processed mail without UIDVALIDITY")
            return
        self._pending_deletions.add((delivery.uidvalidity, delivery.uid))
        self._save_pending_deletions()
        self._retry_pending_deletions()

    def _mark_seen(self, delivery: _InboundMail) -> None:
        """Retain terminal rejected mail without repeatedly evaluating it."""

        def mark_seen(imap: imaplib.IMAP4) -> None:
            if self._select_mailbox(imap) != delivery.uidvalidity:
                logger.warning("Email++ did not mark stale rejected mail as seen")
                return
            status, _ = imap.uid(
                "store", delivery.uid.decode("ascii"), "+FLAGS.SILENT", r"(\Seen)"
            )
            if status != "OK":
                logger.warning("Email++ could not mark rejected mail as seen")

        self._with_imap(mark_seen)

    def _release_for_retry(self, delivery: _InboundMail) -> None:
        """Make a failed non-destructive delivery eligible for a later poll."""
        self._seen.discard(delivery.uid)
        self._save_mailbox_state()

    @staticmethod
    def _search_uids(imap: imaplib.IMAP4, criteria: str) -> list[bytes]:
        status, data = imap.uid("search", None, criteria)  # type: ignore[arg-type]
        if status != "OK":
            logger.warning("Email++ IMAP UID search failed (criteria=%s)", criteria)
            raise imaplib.IMAP4.error("IMAP UID search failed")
        if not isinstance(data, (list, tuple)) or not data:
            logger.warning("Email++ IMAP returned malformed UID search response")
            raise imaplib.IMAP4.error("IMAP UID search returned malformed data")
        if not isinstance(data[0], bytes):
            logger.warning("Email++ IMAP returned malformed UID search data")
            raise imaplib.IMAP4.error("IMAP UID search returned malformed data")
        return data[0].split()

    @staticmethod
    def _close_imap(imap: imaplib.IMAP4) -> None:
        try:
            imap.logout()
        except Exception:
            try:
                imap.shutdown()
            except Exception:
                pass

    def _smtp(self) -> smtplib.SMTP:
        context = ssl.create_default_context()
        if self._smtp_port == 465:
            return smtplib.SMTP_SSL(
                self._smtp_host, self._smtp_port, timeout=30, context=context
            )
        smtp = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30)
        smtp.starttls(context=context)
        return smtp

    def _probe_smtp(self) -> None:
        smtp = self._smtp()
        try:
            smtp.login(self._login, self._password)
        finally:
            smtp.quit()

    # Gateway lifecycle drives this loop; its worker calls are covered below.
    async def _poll_loop(self) -> None:  # pragma: no cover
        while self._running:
            try:
                messages = await asyncio.to_thread(self._fetch_unseen)
                await asyncio.to_thread(self._retry_pending_deletions)
                logger.debug("Email++ IMAP poll fetched batch_size=%d", len(messages))
                for delivery in messages:
                    try:
                        await self._dispatch(delivery)
                    except Exception:
                        logger.warning("Email++ IMAP message dispatch failed")
                        if self._delete_processed:
                            await asyncio.to_thread(self._release_for_retry, delivery)
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Email++ IMAP polling failed")
                self._set_fatal_error(
                    "poll_failed", "IMAP polling failed", retryable=True
                )
                await self._notify_fatal_error()
                return
            await asyncio.sleep(max(1, self._poll_interval))

    def _fetch_unseen(self) -> list[_InboundMail]:
        return self._with_imap(self._fetch_unseen_from_imap)

    def _fetch_unseen_from_imap(self, imap: imaplib.IMAP4) -> list[_InboundMail]:
        messages: list[_InboundMail] = []
        uidvalidity = self._select_mailbox(imap)
        if self._delete_processed and uidvalidity is None:
            raise imaplib.IMAP4.error("IMAP UIDVALIDITY is required for deletion")
        if uidvalidity != self._uidvalidity:
            logger.warning("Email++ IMAP UIDVALIDITY changed during polling")
            self._start_cold_mailbox(imap, uidvalidity)
        seen = set(self._seen)
        attempts = 0
        for uid in sorted(
            self._search_uids(
                imap, "UNSEEN UNDELETED" if self._delete_processed else "UNSEEN"
            ),
            key=self._uid_sort_key,
        ):
            if uid in seen:
                continue
            uid_number = self._uid_number(uid)
            if uid_number is None:
                continue
            if uid_number <= self._baseline_uid and self._process_history_window == 0:
                continue
            if attempts >= _POLL_BATCH_SIZE:
                break
            attempts += 1
            fetched = self._fetch_message(imap, uid, peek=self._delete_processed)
            if fetched is None:
                continue
            raw, internaldate = fetched
            if (
                uid_number <= self._baseline_uid
                and self._process_history_window > 0
                and (internaldate is None or internaldate <= self._history_cutoff)
            ):
                if internaldate is None:
                    logger.warning(
                        "Email++ IMAP skipped unread history with malformed "
                        "INTERNALDATE"
                    )
                seen.add(uid)
                continue
            seen.add(uid)
            messages.append(_InboundMail(raw, uid, uidvalidity))
        self._seen = seen
        self._trim_seen()
        self._save_mailbox_state()
        if attempts:
            logger.info(
                "Email++ IMAP completed poll batch (fetched=%d, attempted=%d)",
                len(messages),
                attempts,
            )
        return messages

    @staticmethod
    def _uid_sort_key(uid: bytes) -> int:
        return int(uid) if uid.isdigit() else -1

    @staticmethod
    def _fetch_message(
        imap: imaplib.IMAP4, uid: bytes, *, peek: bool = False
    ) -> tuple[bytes, datetime | None] | None:
        item = "BODY.PEEK[]" if peek else "RFC822"
        status, data = imap.uid("fetch", uid.decode("ascii"), f"({item} INTERNALDATE)")
        if status != "OK" or not isinstance(data, (list, tuple)) or not data:
            logger.warning("Email++ IMAP message fetch failed")
            return None
        raw: bytes | None = None
        metadata: list[bytes] = []
        for index, item in enumerate(data):
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            descriptor, literal = item
            if (
                not isinstance(descriptor, bytes)
                or not isinstance(literal, bytes)
                or (b"RFC822" not in descriptor and b"BODY[" not in descriptor)
            ):
                continue
            raw = literal
            metadata.append(descriptor)
            # imaplib appends protocol attributes after an RFC822 literal.
            # Only inspect those attributes, never bytes from the message body.
            metadata.extend(
                item for item in data[index + 1 :] if isinstance(item, bytes)
            )
            break
        if raw is None:
            logger.warning("Email++ IMAP returned malformed message fetch data")
            return None
        match = _INTERNALDATE.search(b" ".join(metadata))
        if match is None:
            return raw, None
        try:
            internaldate = parsedate_to_datetime(match.group(1).decode("ascii"))
        except (TypeError, ValueError, UnicodeDecodeError):
            return raw, None
        if internaldate.tzinfo is None:
            return raw, None
        return raw, internaldate.astimezone(timezone.utc)

    def _trim_seen(self) -> None:
        if len(self._seen) > 2000:
            self._seen = set(sorted(self._seen, key=int)[-1000:])

    def _remember_route(
        self,
        route: ThreadRoute,
        message_id: str,
        outlook_reply_context: tuple[str, str] | None = None,
    ) -> None:
        """Retain recent reply metadata using the router's active-thread bound."""
        key = (route.chat_id, message_id)
        self._routes[key] = route
        self._routes.move_to_end(key)
        while len(self._routes) > self._route_cache_size:
            self._routes.popitem(last=False)
        if outlook_reply_context is None:
            return
        self._outlook_reply_context[key] = outlook_reply_context
        self._outlook_reply_context.move_to_end(key)
        while len(self._outlook_reply_context) > self._route_cache_size:
            self._outlook_reply_context.popitem(last=False)

    async def _dispatch(self, inbound: bytes | _InboundMail) -> None:
        """Build an event and retain its IMAP identity until terminal delivery."""
        delivery = inbound if isinstance(inbound, _InboundMail) else None
        if delivery is None:
            assert isinstance(inbound, bytes)
            raw = inbound
        else:
            raw = delivery.raw
        message = email.message_from_bytes(raw)
        sender = _address(message.get("From", ""))
        if not self._permitted(sender, message):
            if self._delete_processed and delivery is not None:
                await asyncio.to_thread(self._mark_seen, delivery)
            return
        route = self._router.resolve(
            sender,
            message_id=message.get("Message-ID"),
            references=message.get("References"),
            in_reply_to=message.get("In-Reply-To"),
        )
        message_id = message.get("Message-ID", "")
        if message_id:
            self._remember_route(
                route,
                message_id,
                (
                    _header_text(message.get("Thread-Topic", "")),
                    _header_text(message.get("Thread-Index", "")),
                ),
            )
        text, urls, types, message_type = self._content(message)
        alternatives = self._text_alternatives(message)
        subject = _decode(message.get("Subject", ""))
        message_ids = _message_ids(message_id)
        references = _message_ids(message.get("References", ""))
        if not references:
            references = _message_ids(message.get("In-Reply-To", ""))
        if message_ids:
            references.append(message_ids[0])
        forward = self._forward(alternatives)
        if forward is None and is_suspected_forward(subject, alternatives):
            self._router.update_context(
                route,
                delivery_context={
                    "display_name": _display_name(message.get("From", sender)),
                    "subject": subject,
                },
                quote_source={"body": "", "is_forwarded": "false"},
                draft_context={"is_forwarded": "false", "draft_sent": "true"},
            )
            sent_notice = await self._send_unsafe_forward_notice(route, message_id)
            if self._delete_processed and delivery is not None:
                if sent_notice:
                    await asyncio.to_thread(self._remember_processed_delivery, delivery)
                else:
                    await asyncio.to_thread(self._mark_seen, delivery)
            return
        self._router.update_context(
            route,
            delivery_context={
                "display_name": _display_name(message.get("From", sender)),
                "subject": subject,
            },
            quote_source={
                "body": (forward.quote if forward else text)[:100_000],
                "sender": forward.original_sender if forward else sender,
                "references": " ".join(dict.fromkeys(references)),
                "is_forwarded": str(bool(forward)).lower(),
            },
            draft_context=self._draft_context(forward),
        )
        source = self.build_source(
            chat_id=route.chat_id,
            chat_name=_decode(message.get("From", sender)),
            chat_type="dm",
            user_id=sender,
            user_name=sender,
            thread_id=route.thread_id,
            message_id=message_id or None,
        )
        event = MessageEvent(
            text=hermes_prompt(forward) if forward else text or "(empty email)",
            message_type=message_type,
            source=source,
            raw_message=delivery,
            message_id=message_id or None,
            reply_to_message_id=message.get("In-Reply-To") or None,
            media_urls=urls,
            media_types=types,
        )
        if self._delete_processed and delivery is not None:
            if event.message_id is None:
                await asyncio.to_thread(self._mark_seen, delivery)
                return
            await self._queue_delivery(event)
            return
        await self.handle_message(event)

    async def _queue_delivery(self, event: MessageEvent) -> None:
        """Serialize mail per Hermes thread so completion maps to one UID."""
        thread_id = event.source.thread_id
        if thread_id in self._active_deliveries:
            queue = self._delivery_queues.setdefault(thread_id, deque())
            queue.append(event)
            logger.info(
                "Email++ queued delivery behind active thread "
                "(thread_id=%s, queue_size=%d)",
                thread_id,
                len(queue),
            )
            return
        self._activate_delivery(event)
        await self.handle_message(event)

    def _activate_delivery(self, event: MessageEvent) -> None:
        """Track one delivery and arm its completion watchdog."""
        thread_id = event.source.thread_id
        self._active_delivery_threads.add(thread_id)
        self._active_deliveries[thread_id] = event
        watchdog = asyncio.create_task(self._watch_delivery(event))
        self._delivery_watchdogs[thread_id] = watchdog
        self._background_tasks.add(watchdog)
        watchdog.add_done_callback(self._background_tasks.discard)

    async def _watch_delivery(self, event: MessageEvent) -> None:
        """Release a thread if Hermes never reports this delivery complete."""
        try:
            await asyncio.sleep(self._delivery_completion_timeout)
        except asyncio.CancelledError:
            raise
        thread_id = event.source.thread_id
        if self._active_deliveries.get(thread_id) is event:
            logger.warning(
                "Email++ delivery completion stalled; abandoning serialization guard "
                "(thread_id=%s, timeout_seconds=%d)",
                thread_id,
                self._delivery_completion_timeout,
            )
            self._finish_delivery(event, abandoned=True)

    def _finish_delivery(self, event: MessageEvent, *, abandoned: bool = False) -> None:
        """Release only the active slot owned by this exact delivery."""
        thread_id = event.source.thread_id
        if self._active_deliveries.get(thread_id) is not event:
            if not abandoned:
                logger.warning(
                    "Email++ ignored late processing completion for inactive delivery "
                    "(thread_id=%s)",
                    thread_id,
                )
            return
        self._active_deliveries.pop(thread_id)
        self._active_delivery_threads.discard(thread_id)
        watchdog = self._delivery_watchdogs.pop(thread_id, None)
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        self._start_next_delivery(thread_id)

    def _start_next_delivery(self, thread_id: str) -> None:
        """Schedule the next event after Hermes releases the current session guard."""
        queue = self._delivery_queues.get(thread_id)
        if queue:
            event = queue.popleft()
            if not queue:
                self._delivery_queues.pop(thread_id, None)
            self._activate_delivery(event)
            logger.info(
                "Email++ advancing delivery queue (thread_id=%s, remaining=%d)",
                thread_id,
                len(queue),
            )
            task = asyncio.create_task(self.handle_message(event))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return
        logger.info("Email++ delivery queue drained (thread_id=%s)", thread_id)

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        """Acknowledge only SMTP-delivered email after Hermes reaches SUCCESS."""
        delivery = (
            event.raw_message if isinstance(event.raw_message, _InboundMail) else None
        )
        if delivery is None or not self._delete_processed:
            return
        key = (event.source.chat_id.lower(), event.message_id or "")
        delivered = key in self._response_deliveries
        self._response_deliveries.discard(key)
        try:
            if outcome is ProcessingOutcome.SUCCESS and delivered:
                if delivery.uidvalidity is None:
                    logger.warning(
                        "Email++ retained processed mail without UIDVALIDITY"
                    )
                else:
                    await asyncio.to_thread(self._remember_processed_delivery, delivery)
            else:
                await asyncio.to_thread(self._release_for_retry, delivery)
        finally:
            self._finish_delivery(event)

    @staticmethod
    def _text_alternatives(message: Any) -> list[str]:
        alternatives: list[str] = []
        for part in message.walk() if message.is_multipart() else [message]:
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True) or b""
            body = payload.decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
            alternatives.append(
                html_to_text(body) if content_type == "text/html" else body
            )
        return alternatives

    @staticmethod
    def _forward(alternatives: list[str]) -> ForwardedMessage | None:
        return next(
            (forward for body in alternatives if (forward := parse_forward(body))), None
        )

    @staticmethod
    def _draft_context(forward: ForwardedMessage | None) -> dict[str, str]:
        if forward is None:
            return {"is_forwarded": "false", "draft_sent": "true"}
        return {
            "is_forwarded": "true",
            "draft_sent": "false",
            "task_prompt": forward.task_prompt[:100_000],
            "original_sender": forward.original_sender,
            "original_date": forward.original_date,
            "original_to": forward.original_to,
            "original_cc": forward.original_cc,
            "original_subject": forward.original_subject,
            "original_body": forward.original_body[:100_000],
        }

    async def _send_unsafe_forward_notice(
        self, route: ThreadRoute, message_id: str
    ) -> bool:
        reply_ids = _message_ids(message_id)
        if len(reply_ids) != 1:
            return False
        content = (
            "I could not safely identify the boundary between your task and "
            "the forwarded message, so no review draft was created. Please "
            "resend it with a separate task prompt followed by a complete "
            "Gmail or Outlook inline forward."
        )
        sent = await asyncio.to_thread(
            self._send_email, route, content, reply_ids[0], None, None
        )
        self._remember_route(route, sent)
        self._router.record_outbound(route, sent)
        return True

    def _permitted(self, sender: str, message: Any) -> bool:
        local_part, _, _ = sender.partition("@")
        if not sender or sender == self._address.lower() or local_part in _AUTOMATED:
            logger.debug(
                "Email++ rejected inbound message: invalid or automated sender"
            )
            return False
        if not self._allow_all and sender not in self._allowed:
            logger.debug("Email++ rejected inbound message: sender is not allowlisted")
            return False
        if self._allow_all or not self._require_auth:
            return True
        headers = message.get_all("Authentication-Results") or []
        for header in headers:
            normalized = " ".join(header.split())
            if self._authserv_id and not normalized.lower().startswith(
                self._authserv_id.lower()
            ):
                continue
            if re.search(r"\bdmarc\s*=\s*pass\b", normalized, re.I):
                return True
        logger.debug("Email++ rejected inbound message: sender authentication failed")
        return False

    def _content(self, message: Any) -> tuple[str, list[str], list[str], Any]:
        text = ""
        html = ""
        urls: list[str] = []
        types: list[str] = []
        message_type = MessageType.TEXT
        for part in message.walk() if message.is_multipart() else [message]:
            payload = part.get_payload(decode=True) or b""
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                text = payload.decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
            elif content_type == "text/html" and "attachment" not in disposition:
                html = html_to_text(
                    payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                )
            elif "attachment" in disposition or part.get_filename():
                path, attachment_type = self._cache_attachment(
                    payload, content_type, part.get_filename()
                )
                urls.append(path)
                types.append(content_type)
                message_type = attachment_type
        return text or html, urls, types, message_type

    @staticmethod
    def _cache_attachment(
        payload: bytes, content_type: str, filename: str | None
    ) -> tuple[str, Any]:
        """Store inbound media in Hermes' bounded profile cache."""
        safe_name = filename or "attachment"
        suffix = Path(safe_name).suffix or ".bin"
        validate_inbound_media_size(len(payload), media_type="email attachment")
        if content_type in _IMAGE_TYPES:
            try:
                return cache_image_from_bytes(payload, suffix), MessageType.PHOTO
            except ValueError:
                # A false image claim remains available as a document.
                return (
                    cache_document_from_bytes(payload, safe_name),
                    MessageType.DOCUMENT,
                )
        if content_type.startswith("audio/"):
            return cache_audio_from_bytes(payload, suffix), MessageType.AUDIO
        if content_type.startswith("video/"):
            return cache_video_from_bytes(payload, suffix), MessageType.VIDEO
        return cache_document_from_bytes(payload, safe_name), MessageType.DOCUMENT

    def extract_media(self, content: str) -> tuple[list[tuple[str, bool]], str]:
        """Carry explicit MEDIA files to send() instead of Hermes' later media loop."""
        media, cleaned = BasePlatformAdapter.extract_media(content)
        protected = BasePlatformAdapter._mask_protected_spans(cleaned)
        protected = BasePlatformAdapter._mask_json_string_media(protected)
        unresolved = bool(_UNRESOLVED_MEDIA_RE.search(protected))
        if not media and not unresolved:
            return [], cleaned
        paths: list[Path] = []
        seen: set[str] = set()
        invalid = unresolved
        for raw_path, _is_voice in media:
            safe_path = validate_media_delivery_path(raw_path)
            if safe_path is None:
                invalid = True
                continue
            if safe_path not in seen:
                seen.add(safe_path)
                paths.append(Path(safe_path))
        if len(paths) > self._max_outbound_attachments:
            invalid = True
        envelope = _AttachmentEnvelope(
            tuple(paths[: self._max_outbound_attachments]), invalid=invalid
        )
        return [], _append_attachment_envelope(cleaned, envelope)

    @staticmethod
    def extract_local_files(content: str) -> tuple[list[str], str]:
        """Keep local paths as useful email references unless MEDIA is explicit."""
        return [], content

    @staticmethod
    def extract_images(content: str) -> tuple[list[tuple[str, str]], str]:
        """Leave remote and localhost image references in the email body."""
        return [], content

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: Any = None,
    ) -> Any:
        body, envelope = _decode_attachment_envelope(content)
        if envelope is None:
            return await self._send_parts(chat_id, body, reply_to, ())
        if envelope.invalid:
            return await self._send_attachment_failure(chat_id, reply_to)
        try:
            attachments = await asyncio.to_thread(
                self._prepare_attachments,
                tuple((path, None) for path in envelope.paths),
            )
        except (OSError, ValueError) as error:
            logger.warning("Email++ attachment preflight failed: %s", error)
            return await self._send_attachment_failure(chat_id, reply_to)
        return await self._send_prepared_parts(chat_id, body, reply_to, attachments)

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Never downgrade an attachment-bearing response to a body-only retry."""
        _body, envelope = _decode_attachment_envelope(content)
        if envelope is not None:
            return await self.send(chat_id, content, reply_to, metadata)
        return await super()._send_with_retry(
            chat_id, content, reply_to, metadata, **kwargs
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: Any = None,
    ) -> Any:
        return await self._send_parts(
            chat_id, caption or "", reply_to, ((Path(image_url), None),)
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: Any = None,
        **kwargs: Any,
    ) -> Any:
        return await self._send_parts(
            chat_id, caption or "", reply_to, ((Path(file_path), file_name),)
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: Any = None,
        **kwargs: Any,
    ) -> Any:
        return await self._send_parts(
            chat_id, caption or "", reply_to, ((Path(audio_path), None),)
        )

    async def get_chat_info(self, chat_id: str) -> dict[str, str]:
        """Return the address-based metadata required by Hermes adapters."""
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    async def _send_parts(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None,
        attachments: tuple[tuple[Path, str | None], ...],
    ) -> Any:
        try:
            prepared = await asyncio.to_thread(self._prepare_attachments, attachments)
        except (OSError, ValueError) as error:
            return SendResult(success=False, error=str(error))
        return await self._send_prepared_parts(chat_id, content, reply_to, prepared)

    async def _send_attachment_failure(self, chat_id: str, reply_to: str | None) -> Any:
        return await self._send_parts(
            chat_id,
            (
                "I could not deliver the requested attachment(s), so no files were "
                "attached. Please ask me to regenerate or resend them."
            ),
            reply_to,
            (),
        )

    async def _send_prepared_parts(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None,
        attachments: tuple[_PreparedAttachment, ...],
    ) -> Any:
        key = (chat_id.lower(), reply_to or "")
        route = self._routes.get(key)
        if route is not None:
            self._routes.move_to_end(key)
        if _address(chat_id) != chat_id.lower() or route is None:
            return SendResult(
                success=False, error="explicit known recipient and reply route required"
            )
        outlook_reply_context = self._outlook_reply_context.get(key)
        if outlook_reply_context is not None:
            self._outlook_reply_context.move_to_end(key)
        try:
            message_id = await asyncio.to_thread(
                self._send_email,
                route,
                content,
                reply_to or "",
                attachments,
                None,
                outlook_reply_context,
            )
            self._remember_route(route, message_id)
            self._router.record_outbound(route, message_id)
            if reply_to:
                self._response_deliveries.add((route.chat_id.lower(), reply_to))
            return SendResult(success=True, message_id=message_id)
        except (OSError, smtplib.SMTPException, ValueError) as error:
            return SendResult(success=False, error=str(error))

    def _response_bodies(self, route: ThreadRoute, content: str) -> tuple[str, str]:
        """Render a response and add a quote only when its mode permits it."""
        context = self._router.context_for(route) or {}
        quote_source = context.get("quote_source", {})
        quote = str(quote_source.get("body", ""))
        include_quote = self._quote_mode == "always" or (
            self._quote_mode == "forwarded"
            and quote_source.get("is_forwarded") == "true"
        )
        html_body = render_markdown(content)
        if not include_quote or not quote:
            return content, html_body
        sender = str(quote_source.get("sender", route.chat_id))
        return quote_plain(content, quote, sender), quote_html(html_body, quote, sender)

    def _prepare_attachments(
        self, attachments: tuple[tuple[Path, str | None], ...]
    ) -> tuple[_PreparedAttachment, ...]:
        """Revalidate and snapshot every requested file before SMTP begins."""
        if len(attachments) > self._max_outbound_attachments:
            raise ValueError(
                "requested attachment count exceeds EMAIL_PP_MAX_OUTBOUND_ATTACHMENTS"
            )
        total = 0
        prepared: list[_PreparedAttachment] = []
        for path, requested_name in attachments:
            safe_path = validate_media_delivery_path(str(path))
            if safe_path is None:
                raise ValueError("requested attachment is unavailable or not permitted")
            resolved = Path(safe_path)
            data = resolved.read_bytes()
            total += len(data)
            if total > self._max_outbound_total_bytes:
                raise ValueError(
                    "requested attachments exceed EMAIL_PP_MAX_OUTBOUND_TOTAL_BYTES"
                )
            filename = Path(requested_name or resolved.name).name
            filename = filename.replace("\r", "").replace("\n", "")
            if not filename or filename in {".", ".."}:
                filename = "attachment"
            content_type, _ = mimetypes.guess_type(filename)
            maintype, subtype = (content_type or "application/octet-stream").split(
                "/", 1
            )
            prepared.append(_PreparedAttachment(data, maintype, subtype, filename))
        return tuple(prepared)

    def _send_email(
        self,
        route: ThreadRoute,
        content: str,
        reply_to: str,
        attachments: tuple[_PreparedAttachment, ...] | Path | None,
        file_name: str | None = None,
        outlook_reply_context: tuple[str, str] | None = None,
    ) -> str:
        if isinstance(attachments, Path):
            attachments = self._prepare_attachments(((attachments, file_name),))
        elif attachments is None:
            attachments = ()
        reply_ids = _message_ids(reply_to)
        if len(reply_ids) != 1:
            raise ValueError("reply_to must be one valid RFC Message-ID")
        context = self._router.context_for(route) or {}
        delivery = context.get("delivery_context", {})
        quote_source = context.get("quote_source", {})
        draft = context.get("draft_context", {})
        plain_body, html_body = self._response_bodies(route, content)

        subject = str(delivery.get("subject", "")).strip()
        fresh_draft = (
            draft.get("is_forwarded") == "true" and draft.get("draft_sent") == "false"
        )
        subject = _reply_subject(subject)
        recipient_name = str(delivery.get("display_name", ""))
        message = EmailMessage(policy=_SMTP_REPLY_POLICY)
        message["From"] = Address(
            display_name=self._sender_name, addr_spec=self._address
        )
        message["To"] = Address(display_name=recipient_name, addr_spec=route.chat_id)
        message["Subject"] = subject
        message["In-Reply-To"] = reply_ids[0]
        references = _message_ids(str(quote_source.get("references", "")))
        message["References"] = _references_header(references, reply_ids[0])
        if outlook_reply_context is not None:
            topic, parent_index = outlook_reply_context
            reply_index = _reply_thread_index(parent_index)
            if topic and reply_index is not None:
                message["Thread-Topic"] = topic
                message["Thread-Index"] = reply_index
        message["Date"] = formatdate(localtime=True)
        message_id = f"<hermes-{uuid.uuid4().hex}@{self._address.rsplit('@', 1)[-1]}>"
        message["Message-ID"] = message_id
        message.set_content(plain_body, charset="utf-8")
        message.add_alternative(html_body, subtype="html", charset="utf-8")
        if attachments:
            message.make_mixed()
            for attachment in attachments:
                message.add_attachment(
                    attachment.data,
                    maintype=attachment.maintype,
                    subtype=attachment.subtype,
                    filename=attachment.filename,
                )
        # SMTP DATA may have succeeded even if its response is lost, so keep sends
        # isolated rather than retrying or reusing a session with ambiguous state.
        smtp = self._smtp()
        try:
            smtp.login(self._login, self._password)
            smtp.send_message(message)
        finally:
            try:
                smtp.quit()
            except (OSError, smtplib.SMTPException):
                logger.warning("Email++ SMTP QUIT failed after send")
        if fresh_draft:
            self._router.update_context(route, draft_context={"draft_sent": "true"})
        return message_id
