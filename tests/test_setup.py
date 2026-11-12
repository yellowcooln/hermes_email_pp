"""Offline tests for the terminal-only credential wizard."""

from __future__ import annotations

import getpass
import importlib
import importlib.util
import inspect
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

ANSWERS = [
    "agent@example.org",
    "mailbox@example.org",
    "",
    "",
    "",
    "owner@example.org,other@example.org",
    "mx.receiver.example",
    "telegram:123",
    "123",
]
PASSWORD = " a'\\b\"#${UNCHANGED}$(never-run) "


def module():
    assert importlib.util.find_spec("hermes_email_pp.setup") is not None, (
        "The offline setup wizard is not implemented"
    )
    return importlib.import_module("hermes_email_pp.setup")


def wizard(home, *, answers=None, password=PASSWORD, runner=None):
    prompts, secrets, commands = [], [], []
    values = iter(ANSWERS if answers is None else answers)

    def input_fn(prompt):
        assert "password" not in prompt.lower()
        prompts.append(prompt)
        return next(values)

    def password_fn(prompt):
        secrets.append(prompt)
        return password

    def run_config(command, **kwargs):
        commands.append((command, kwargs))
        assert not secrets, "Disable the transport before requesting credentials"
        if runner:
            return runner(command, **kwargs)
        return subprocess.CompletedProcess(command, 0)

    result = module().setup_profile(
        home,
        input_fn=input_fn,
        password_fn=password_fn,
        run_config=run_config,
    )
    return result, prompts, secrets, commands


def test_wizard_saves_secure_profile_but_never_activates(tmp_path, capsys):
    original = b"# unrelated\r\nOTHER_TOKEN='do not touch'\r\nUNQUOTED=keep # note\r\n"
    (tmp_path / ".env").write_bytes(original)
    result, prompts, secrets, commands = wizard(tmp_path)
    assert result == 0
    assert len(prompts) == len(ANSWERS)
    assert len(secrets) == 1
    assert len(commands) == 1
    command, kwargs = commands[0]
    assert command == ["hermes", "config", "set", "platforms.email_pp.enabled", "false"]
    assert kwargs["env"]["HERMES_HOME"] == str(tmp_path)
    assert kwargs.get("shell", False) is False
    path = tmp_path / ".env"
    assert path.read_bytes().startswith(original)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    values = dotenv_values(path, interpolate=False)
    assert values["EMAIL_PP_ADDRESS"] == "agent@example.org"
    assert values["EMAIL_PP_LOGIN"] == "mailbox@example.org"
    assert values["EMAIL_PP_PASSWORD"] == PASSWORD
    assert values["EMAIL_PP_IMAP_HOST"] == "mail.privateemail.com"
    assert values["EMAIL_PP_SMTP_HOST"] == "mail.privateemail.com"
    assert values["EMAIL_PP_MAILBOX"] == "Hermes"
    assert values["EMAIL_PP_ALLOWED_USERS"] == "owner@example.org,other@example.org"
    assert values["EMAIL_PP_AUTHSERV_ID"] == "mx.receiver.example"
    assert values["EMAIL_PP_BACKCHANNEL_TARGET"] == "telegram:123"
    assert values["EMAIL_PP_BACKCHANNEL_OWNER"] == "123"
    for name, value in {
        "COLLABORATION": "true",
        "ALLOW_ALL_USERS": "false",
        "REQUIRE_AUTHENTICATED_SENDER": "true",
        "DELETE_PROCESSED": "false",
        "PROCESS_HISTORY_WINDOW": "-1",
        "QUOTE_MODE": "never",
    }.items():
        assert values[f"EMAIL_PP_{name}"] == value
    output = capsys.readouterr().out
    assert PASSWORD not in output
    assert "acceptance" in output.lower() and "activation" in output.lower()
    assert "disabled" in output.lower()


@pytest.mark.parametrize("failure", [7, FileNotFoundError("not installed")])
def test_disable_failure_aborts_before_prompting_or_writing(tmp_path, failure, capsys):
    original = b"OTHER_TOKEN=unchanged\n"
    path = tmp_path / ".env"
    path.write_bytes(original)

    def run_config(command, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return subprocess.CompletedProcess(
            command, failure, stdout=PASSWORD, stderr=PASSWORD
        )

    def no_prompt(prompt):
        pytest.fail("Credentials must not be requested when disabling failed")

    result = module().setup_profile(
        tmp_path,
        input_fn=no_prompt,
        password_fn=no_prompt,
        run_config=run_config,
    )
    assert result != 0
    assert path.read_bytes() == original
    assert PASSWORD not in capsys.readouterr().out


@pytest.mark.parametrize(
    "index,value",
    [
        (0, "bad"),
        (0, "*@example.org"),
        (0, "Name <agent@example.org>"),
        (1, "mailbox"),
        (1, "alias\nINJECTED=yes"),
        (2, "https://mail.example.org"),
        (3, "host with space"),
        (4, "Hermes\rINJECTED=yes"),
        (5, "*"),
        (5, "*@example.org"),
        (5, "owner@example.org,*@other.org"),
        (5, "owner@example.org,"),
        (5, ""),
        (6, ""),
        (6, "mx.example; dmarc=pass"),
        (7, "slack:123"),
        (7, "Telegram:123"),
        (7, "telegram:"),
        (8, ""),
        (8, "Display Name"),
    ],
)
def test_bad_nonsecret_inputs_never_write(tmp_path, index, value, capsys):
    answers = list(ANSWERS)
    answers[index] = value
    original = b"OTHER=exactly preserved\n"
    path = tmp_path / ".env"
    path.write_bytes(original)
    result, *_ = wizard(tmp_path, answers=answers)
    assert result != 0
    assert path.read_bytes() == original
    assert value not in capsys.readouterr().out if value else True


@pytest.mark.parametrize("secret", ["", "line1\nline2", "line1\rline2", "bad\x00value"])
def test_invalid_password_never_writes(tmp_path, secret, capsys):
    result, *_ = wizard(tmp_path, password=secret)
    assert result != 0
    assert not (tmp_path / ".env").exists()
    output = capsys.readouterr().out
    if secret:
        assert secret not in output


def test_check_missing_names_only_and_ignores_process_environment(
    tmp_path, monkeypatch
):
    setup = module()
    assert hasattr(setup, "check"), "Offline readiness check is missing"
    monkeypatch.setenv("EMAIL_PP_PASSWORD", PASSWORD)
    result = setup.check(tmp_path)
    assert result["status"] == "credentials_required"
    assert "EMAIL_PP_PASSWORD" in result["missing"]
    assert result["present"] == []
    assert PASSWORD not in str(result)
    assert not (tmp_path / ".env").exists()


def test_check_reports_configured_without_disclosing_values_or_expanding(tmp_path):
    wizard(tmp_path)
    setup = module()
    assert hasattr(setup, "check"), "Offline readiness check is missing"
    original = (tmp_path / ".env").read_bytes()
    result = setup.check(tmp_path)
    assert result["status"] == "configured_activation_pending"
    assert result["missing"] == []
    assert "EMAIL_PP_PASSWORD" in result["present"]
    assert PASSWORD not in str(result)
    assert "agent@example.org" not in str(result)
    assert "telegram:123" not in str(result)
    assert (tmp_path / ".env").read_bytes() == original


def test_check_rejects_unsafe_saved_policy_and_allowlist(tmp_path):
    wizard(tmp_path)
    path = tmp_path / ".env"
    text = path.read_text().replace(
        "EMAIL_PP_ALLOW_ALL_USERS='false'", "EMAIL_PP_ALLOW_ALL_USERS=true"
    )
    text = text.replace("owner@example.org,other@example.org", "*@example.org")
    path.write_text(text)
    setup = module()
    assert hasattr(setup, "check"), "Offline readiness check is missing"
    result = setup.check(tmp_path)
    assert result["status"] == "credentials_required"
    assert set(result["invalid"]) == {
        "EMAIL_PP_ALLOW_ALL_USERS",
        "EMAIL_PP_ALLOWED_USERS",
    }
    assert "*@example.org" not in str(result)


@pytest.mark.parametrize("configured", [False, True])
def test_module_check_is_usable_offline_and_redacted(tmp_path, configured):
    if configured:
        wizard(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "hermes_email_pp.setup", "--check"],
        env={**os.environ, "HERMES_HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip(), "The module must expose an executable --check CLI"
    report = json.loads(result.stdout)
    assert result.returncode == (0 if configured else 1)
    assert report["status"] == (
        "configured_activation_pending" if configured else "credentials_required"
    )
    assert PASSWORD not in result.stdout + result.stderr
    assert "agent@example.org" not in result.stdout + result.stderr


def test_import_does_not_read_dotenv_or_change_environment(monkeypatch):
    def forbid(*args, **kwargs):
        pytest.fail("Module import must not read profile files")

    setup = module()
    monkeypatch.setattr(Path, "read_bytes", forbid)
    before = dict(os.environ)
    importlib.reload(setup)
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    "failure", [EOFError(), KeyboardInterrupt(), getpass.GetPassWarning("no tty")]
)
def test_terminal_failure_never_saves_secrets(tmp_path, failure, capsys):
    setup = module()
    assert (
        inspect.signature(setup.setup_profile).parameters["password_fn"].default
        is getpass.getpass
    )
    answers = iter(ANSWERS)

    def password_fn(prompt):
        if isinstance(failure, getpass.GetPassWarning):
            import warnings

            warnings.warn(failure, stacklevel=2)
            return PASSWORD
        raise failure

    result = setup.setup_profile(
        tmp_path,
        input_fn=lambda prompt: next(answers),
        password_fn=password_fn,
        run_config=lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )
    assert result != 0
    assert not (tmp_path / ".env").exists()
    assert PASSWORD not in capsys.readouterr().out


def test_atomic_replace_failure_preserves_original_and_cleans_temporary(
    tmp_path, monkeypatch
):
    path = tmp_path / ".env"
    original = b"UNRELATED=keep\n"
    path.write_bytes(original)

    def fail_replace(*args):
        raise OSError("replace denied")

    monkeypatch.setattr(os, "replace", fail_replace)
    result, *_ = wizard(tmp_path)
    assert result != 0
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_env_symlink_is_refused(tmp_path):
    target = tmp_path / "outside"
    target.write_text("UNRELATED=secret\n")
    (tmp_path / ".env").symlink_to(target)
    result, *_ = wizard(tmp_path)
    assert result != 0
    assert target.read_text() == "UNRELATED=secret\n"
    assert (tmp_path / ".env").is_symlink()


def test_setup_preserves_logical_multiline_entries_and_is_idempotent(tmp_path):
    unrelated = (
        "# exact text\r\nOTHER='first\n"
        "EMAIL_PP_PASSWORD=literal-do-not-change\nlast'\r\n"
    )
    path = tmp_path / ".env"
    path.write_bytes(
        (
            unrelated + "EMAIL_PP_PASSWORD='old\ncontinued'\n"
            "export EMAIL_PP_ADDRESS=old@example.org\n"
            "EMAIL_PP_ADDRESS=duplicate@example.org\n"
        ).encode()
    )
    assert wizard(tmp_path)[0] == 0
    first = path.read_bytes()
    assert first.startswith(unrelated.encode())
    assert b"continued" not in first
    values = dotenv_values(path, interpolate=False)
    assert values["OTHER"] == "first\nEMAIL_PP_PASSWORD=literal-do-not-change\nlast"
    assert values["EMAIL_PP_PASSWORD"] == PASSWORD
    assert wizard(tmp_path)[0] == 0
    assert path.read_bytes() == first


def test_malformed_existing_dotenv_is_not_modified(tmp_path):
    original = b"UNRELATED='unterminated\n"
    path = tmp_path / ".env"
    path.write_bytes(original)
    assert wizard(tmp_path)[0] != 0
    assert path.read_bytes() == original


def test_check_rejects_existing_multiline_secret(tmp_path):
    wizard(tmp_path)
    path = tmp_path / ".env"
    text = path.read_text()
    text = text.replace(module()._quote(PASSWORD), "'unsafe\nsecret'")
    path.write_text(text)
    report = module().check(tmp_path)
    assert report["status"] == "credentials_required"
    assert "EMAIL_PP_PASSWORD" in report["invalid"]
