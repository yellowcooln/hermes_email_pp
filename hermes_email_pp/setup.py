"""Terminal-only Email++ setup. Importing this module never reads a profile.

Run ``python -m hermes_email_pp.setup`` in the intended Hermes environment.
Credentials stay local; setup deliberately leaves the transport disabled.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import tempfile
import warnings
from pathlib import Path

DEFAULTS = {
    "EMAIL_PP_IMAP_HOST": "mail.privateemail.com",
    "EMAIL_PP_SMTP_HOST": "mail.privateemail.com",
    "EMAIL_PP_MAILBOX": "Hermes",
}
POLICY = {
    "EMAIL_PP_COLLABORATION": "true",
    "EMAIL_PP_ALLOW_ALL_USERS": "false",
    "EMAIL_PP_REQUIRE_AUTHENTICATED_SENDER": "true",
    "EMAIL_PP_DELETE_PROCESSED": "false",
    "EMAIL_PP_PROCESS_HISTORY_WINDOW": "-1",
    "EMAIL_PP_QUOTE_MODE": "never",
}
PROMPTS = {
    "EMAIL_PP_ADDRESS": "From alias address (not necessarily the mailbox login): ",
    "EMAIL_PP_LOGIN": "Underlying mailbox login address: ",
    "EMAIL_PP_IMAP_HOST": "IMAP host [mail.privateemail.com]: ",
    "EMAIL_PP_SMTP_HOST": "SMTP host [mail.privateemail.com]: ",
    "EMAIL_PP_MAILBOX": "Dedicated mail folder [Hermes]: ",
    "EMAIL_PP_ALLOWED_USERS": (
        "Allowed sender addresses (comma-separated, exact addresses only): "
    ),
    "EMAIL_PP_AUTHSERV_ID": (
        "Exact receiving authserv-id from a known trusted Authentication-Results "
        "header (required; do not guess): "
    ),
    "EMAIL_PP_BACKCHANNEL_TARGET": (
        "Private target (telegram:123 / discord:123 / fluxer:123 / signal:+number; "
        "optional :thread): "
    ),
    "EMAIL_PP_BACKCHANNEL_OWNER": (
        "Owner's actual authenticated backchannel user ID (not a display name): "
    ),
}


def _single_line(name: str, value: str) -> str:
    if any(ord(char) < 32 or char in "\x7f\x85\u2028\u2029" for char in value):
        raise ValueError(name)
    return value


def _hostname(value: str) -> bool:
    return bool(
        len(value) <= 253
        and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in value.split(".")
        )
    )


def _address(value: str) -> bool:
    if len(value) > 254 or value.count("@") != 1:
        return False
    local, domain = value.split("@")
    return bool(
        len(local) <= 64
        and re.fullmatch(r"[A-Za-z0-9!#$%&'+/=?^_`{|}~.-]+", local)
        and not local.startswith(".")
        and not local.endswith(".")
        and ".." not in local
        and _hostname(domain)
    )


def _validate(name: str, value: str) -> None:
    _single_line(name, value)
    valid = bool(value)
    if name in {"EMAIL_PP_ADDRESS", "EMAIL_PP_LOGIN"}:
        valid = _address(value)
    elif name in {"EMAIL_PP_IMAP_HOST", "EMAIL_PP_SMTP_HOST"}:
        valid = _hostname(value)
    elif name == "EMAIL_PP_ALLOWED_USERS":
        valid = all(_address(item.strip()) for item in value.split(","))
    elif name == "EMAIL_PP_AUTHSERV_ID":
        valid = bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value))
    elif name == "EMAIL_PP_BACKCHANNEL_TARGET":
        valid = bool(
            re.fullmatch(
                r"(?:telegram|discord|fluxer|signal):[A-Za-z0-9_+@.-]+(?::[A-Za-z0-9_-]+)?",
                value,
            )
        )
    elif name == "EMAIL_PP_BACKCHANNEL_OWNER":
        valid = bool(re.fullmatch(r"[A-Za-z0-9_+@.-]+", value))
    if not valid:
        raise ValueError(name)


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _save(home: Path, values: dict[str, str]) -> None:
    path = home / ".env"
    if path.is_symlink():
        raise ValueError(".env")
    original = path.read_bytes().decode("utf-8") if path.exists() else ""
    pieces, cursor = [], 0
    for key, _value, start, end in _bindings(original):
        if key in values:
            pieces.append(original[cursor:start])
            cursor = end
    pieces.append(original[cursor:])
    text = "".join(pieces)
    if text and not text.endswith(("\r", "\n")):
        text += "\n"
    text += "".join(f"{key}={_quote(value)}\n" for key, value in values.items())
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".email-pp-", dir=home)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _bindings(text: str):
    """Yield logical dotenv entries with byte-preserving character spans.

    Handle quoted multiline values so a key-shaped line inside another value
    cannot be mistaken for an assignment. Malformed syntax fails closed.
    """
    cursor = 0
    while cursor < len(text):
        end_match = re.search(r"\r\n|\r|\n|$", text[cursor:])
        assert end_match is not None  # The final alternative always matches.
        line_end = cursor + end_match.end()
        line = text[cursor:line_end]
        if not line.strip() or line.lstrip().startswith("#"):
            cursor = line_end
            continue
        match = re.match(
            r"[ \t]*(?:export[ \t]+)?(?:'([^'\r\n]+)'|([^\s=#'\"]+))[ \t]*(=)?[ \t]*",
            text[cursor:],
        )
        if not match:
            raise ValueError(".env")
        key = match[1] or match[2]
        start = cursor + match.end()
        value = ""
        if match[3] and start < len(text) and text[start] in "'\"":
            quote = text[start]
            quoted = re.match(
                re.escape(quote) + r"((?:\\.|[^\\" + quote + r"])*)" + re.escape(quote),
                text[start:],
                re.DOTALL,
            )
            if not quoted:
                raise ValueError(".env")
            value = quoted[1]
            escapes = (
                {"\\": "\\", "'": "'"}
                if quote == "'"
                else {
                    "\\": "\\",
                    "'": "'",
                    '"': '"',
                    "n": "\n",
                    "r": "\r",
                    "t": "\t",
                    "a": "\a",
                    "b": "\b",
                    "f": "\f",
                    "v": "\v",
                }
            )
            value = re.sub(
                r"\\(.)",
                lambda m, escapes=escapes: escapes.get(m[1], m[0]),
                value,
            )
            tail = re.match(
                r"[ \t]*(?:#[^\r\n]*)?(?:\r\n|\r|\n|$)", text[start + quoted.end() :]
            )
            if not tail:
                raise ValueError(".env")
            end = start + quoted.end() + tail.end()
        else:
            raw = text[start:line_end].rstrip("\r\n")
            if not match[3] and raw.strip() and not raw.lstrip().startswith("#"):
                raise ValueError(".env")
            value = (
                re.split(r"[ \t]+#", raw, maxsplit=1)[0].rstrip() if match[3] else ""
            )
            end = line_end
        yield key, value, cursor, end
        cursor = end


def _read_values(text: str) -> dict[str, str]:
    """Parse data only; no shell execution or environment interpolation."""
    return {key: value for key, value, _start, _end in _bindings(text)}


def check(home: Path) -> dict:
    """Offline stored-field readiness only; no network, expansion, or values."""
    names = sorted({*PROMPTS, "EMAIL_PP_PASSWORD", *POLICY})
    try:
        path = Path(home) / ".env"
        values = (
            _read_values(path.read_bytes().decode("utf-8")) if path.exists() else {}
        )
    except (OSError, UnicodeError, ValueError):
        return {
            "status": "credentials_required",
            "present": [],
            "missing": names,
            "invalid": [".env"],
        }
    present = [name for name in names if values.get(name)]
    missing = [name for name in names if not values.get(name)]
    invalid = []
    for name in present:
        try:
            _validate(name, values[name])
            if name in POLICY and values[name] != POLICY[name]:
                raise ValueError(name)
        except ValueError:
            invalid.append(name)
    return {
        "status": "credentials_required"
        if missing or invalid
        else "configured_activation_pending",
        "present": present,
        "missing": missing,
        "invalid": invalid,
    }


def setup_profile(
    home: Path,
    input_fn=input,
    password_fn=getpass.getpass,
    run_config=subprocess.run,
) -> int:
    """Save a disabled profile using injected terminal/config boundaries."""
    home = Path(home).expanduser().absolute()
    command = ["hermes", "config", "set", "platforms.email_pp.enabled", "false"]
    try:
        result = run_config(
            command,
            env={**os.environ, "HERMES_HOME": str(home)},
            shell=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                "Setup stopped: Hermes could not disable Email++. No credentials saved."
            )
            return 1
    except (OSError, subprocess.SubprocessError):
        print(
            "Setup stopped: Hermes config CLI unavailable or failed. "
            "No credentials saved."
        )
        return 1
    try:
        values = {}
        for key, prompt in PROMPTS.items():
            raw = _single_line(key, input_fn(prompt))
            value = raw.strip() or DEFAULTS.get(key, "")
            _validate(key, value)
            values[key] = value
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            values["EMAIL_PP_PASSWORD"] = password_fn("Mailbox password (hidden): ")
        _validate("EMAIL_PP_PASSWORD", values["EMAIL_PP_PASSWORD"])
        values.update(POLICY)
        _save(home, values)
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Setup cancelled or secure terminal unavailable. No credentials saved.")
        return 1
    except (ValueError, OSError):
        print(
            "Setup stopped: invalid input or profile could not be saved. "
            "No credentials saved."
        )
        return 1
    print(
        "Settings saved; Email++ remains disabled. "
        "Ask Hermes for acceptance testing and activation. "
        "No gateway was restarted and no mail was sent."
    )
    return 0


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path(
            os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
        ).expanduser()
    return Path(get_hermes_home())


def main(argv=None) -> int:
    """Run the wizard or print an offline, redacted readiness report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check stored fields offline; never authenticate or activate",
    )
    args = parser.parse_args(argv)
    home = _hermes_home()
    if args.check:
        report = check(home)
        print(json.dumps(report, sort_keys=True))
        return 0 if report["status"] == "configured_activation_pending" else 1
    return setup_profile(home)


if __name__ == "__main__":
    raise SystemExit(main())
