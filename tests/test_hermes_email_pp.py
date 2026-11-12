"""Tests for the Email++ plugin registration contract."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

import hermes_email_pp
from hermes_email_pp import config as email_config
from hermes_email_pp import plugin as email_plugin
from hermes_email_pp import threading as email_threading
from hermes_email_pp.config import (
    CHANNEL_ENV,
    CHANNEL_REQUIRED_ENV,
    REQUIRED_ENV,
    delete_processed,
    environment_enablement,
    is_configured,
    outbound_attachment_limit,
    process_history_window,
)
from hermes_email_pp.plugin import check_requirements, create_adapter, register
from hermes_email_pp.threading import EmailThreadRouter, ThreadRoute

_LOCAL_BUILD_ARTIFACTS = (
    ".git",
    ".release-venv",
    ".venv",
    "venv",
    "ENV",
    "build",
    "dist",
    "__pycache__",
)


class RecordingContext:
    """Minimal Hermes plugin context used to inspect registration arguments."""

    def register_platform(self, **kwargs: object) -> None:
        self.registration = kwargs


def test_project_declares_email_pp_entry_point() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    lock = tomllib.loads(Path("uv.lock").read_text())
    locked_project = next(
        package for package in lock["package"] if package["name"] == "hermes-email-pp"
    )

    assert project["project"]["version"] == hermes_email_pp.__version__
    assert locked_project["version"] == hermes_email_pp.__version__
    assert project["project"]["entry-points"]["hermes_agent.plugins"] == {
        "email-pp": "hermes_email_pp.plugin"
    }
    assert project["project"]["requires-python"] == ">=3.13"
    assert project["project"]["dependencies"] == ["markdown>=3.10"]


def test_root_directory_plugin_manifest_and_loader() -> None:
    root = Path.cwd()
    manifest = yaml.safe_load((root / "plugin.yaml").read_text())
    assert manifest["name"] == "email-pp"
    assert manifest["kind"] == "platform"
    assert manifest["version"] == hermes_email_pp.__version__
    required_names = {
        item["name"] if isinstance(item, dict) else item
        for item in manifest["requires_env"]
    }
    assert "EMAIL_PP_PASSWORD" in required_names
    assert "EMAIL_PP_ALLOW_ALL_USERS" in required_names
    optional_names = {
        item["name"] if isinstance(item, dict) else item
        for item in manifest["optional_env"]
    }
    assert optional_names == set(email_config.OPTIONAL_ENV) - {
        "EMAIL_PP_ALLOW_ALL_USERS"
    }

    module_name = "test_directory_email_pp"
    spec = spec_from_file_location(
        module_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        assert callable(module.register)
    finally:
        for name in list(sys.modules):
            if name == module_name or name.startswith(f"{module_name}."):
                sys.modules.pop(name, None)


def test_repository_root_has_a_safe_hermes_plugin_scan(tmp_path) -> None:
    plugin_guard = pytest.importorskip("tools.plugin_guard")
    plugin = tmp_path / "plugin"
    shutil.copytree(
        Path.cwd(), plugin, ignore=shutil.ignore_patterns(*_LOCAL_BUILD_ARTIFACTS)
    )

    result = plugin_guard.scan_plugin(plugin, source="JNevrly/hermes_email_pp")

    assert result.verdict == "safe"
    assert not {finding.severity for finding in result.findings} & {"critical", "high"}


def test_git_installer_accepts_the_directory_plugin(monkeypatch, tmp_path) -> None:
    from hermes_cli.plugins_cmd import _install_plugin_core

    source = tmp_path / "source"
    shutil.copytree(
        Path.cwd(),
        source,
        ignore=shutil.ignore_patterns(*_LOCAL_BUILD_ARTIFACTS),
    )
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "--quiet",
            "-m",
            "test plugin",
        ],
        check=True,
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    target, manifest, name = _install_plugin_core(f"file://{source}", force=False)

    assert name == "email-pp"
    assert manifest["kind"] == "platform"
    assert (target / "__init__.py").is_file()
    assert (target / "hermes_email_pp" / "plugin.py").is_file()


def test_readme_documents_dashboard_installation_and_enablement() -> None:
    readme = Path("README.md").read_text()

    assert "Settings > Plugins > Install from Git" in readme
    assert "JNevrly/hermes_email_pp" in readme
    assert "enabled: [email-pp]" in readme
    assert "Hermes Agent 0.20.5" in readme
    assert "Channels > Email++ >" in readme
    assert "true` or `false" in readme
    assert "always`, `forwarded`, or `never" in readme


def test_registers_email_pp_with_isolated_access_control() -> None:
    context = RecordingContext()

    register(context)

    assert context.registration["name"] == "email_pp"
    assert context.registration["required_env"] == list(CHANNEL_REQUIRED_ENV)
    assert context.registration["allowed_users_env"] == "EMAIL_PP_ALLOWED_USERS"
    assert context.registration["allow_all_env"] == "EMAIL_PP_ALLOW_ALL_USERS"
    assert "MEDIA:/absolute/path/to/file" in context.registration["platform_hint"]
    assert "localhost" in context.registration["platform_hint"]


def test_registers_without_vanilla_channel_metadata(monkeypatch) -> None:
    def missing_hermes_config(*args: object, **kwargs: object) -> ModuleType:
        raise ImportError

    monkeypatch.setattr(email_plugin, "import_module", missing_hermes_config)
    context = RecordingContext()

    register(context)

    assert context.registration["name"] == "email_pp"


def test_registers_all_channel_metadata_with_vanilla_hermes(monkeypatch) -> None:
    from hermes_cli.config import OPTIONAL_ENV_VARS

    for field in CHANNEL_ENV:
        monkeypatch.delitem(OPTIONAL_ENV_VARS, field["name"], raising=False)
    context = RecordingContext()

    register(context)

    assert "fields" not in context.registration
    assert set(OPTIONAL_ENV_VARS).issuperset(field["name"] for field in CHANNEL_ENV)
    assert OPTIONAL_ENV_VARS["EMAIL_PP_PASSWORD"]["password"] is True
    assert OPTIONAL_ENV_VARS["EMAIL_PP_QUOTE_MODE"]["category"] == "messaging"


def test_vanilla_channels_discovers_all_email_pp_settings(monkeypatch) -> None:
    from gateway.platform_registry import PlatformEntry
    from hermes_cli.config import OPTIONAL_ENV_VARS

    try:
        from hermes_cli.web_server_messaging import _build_catalog_entry
    except ImportError:
        from hermes_cli.web_server import _build_catalog_entry

    for field in CHANNEL_ENV:
        monkeypatch.delitem(OPTIONAL_ENV_VARS, field["name"], raising=False)
    context = RecordingContext()
    register(context)
    entry = PlatformEntry(**context.registration)
    catalog = _build_catalog_entry("email_pp", entry)

    assert catalog["required_env"] == tuple(CHANNEL_REQUIRED_ENV)
    assert set(catalog["env_vars"]) == set(REQUIRED_ENV) | set(
        email_config.OPTIONAL_ENV
    )


def test_channel_environment_metadata_covers_every_email_pp_setting() -> None:
    fields = {field["name"]: field for field in CHANNEL_ENV}

    assert set(fields) == set(REQUIRED_ENV) | set(email_config.OPTIONAL_ENV)
    assert fields["EMAIL_PP_PASSWORD"]["password"] is True
    assert "Default: 993" in fields["EMAIL_PP_IMAP_PORT"]["description"]
    assert "WARNING" in fields["EMAIL_PP_ALLOW_ALL_USERS"]["description"]
    assert fields["EMAIL_PP_SENDER_NAME"]["prompt"] == "Sender name"
    assert "Default: Hermes Agent" in fields["EMAIL_PP_SENDER_NAME"]["description"]
    assert (
        "Default: true"
        in fields["EMAIL_PP_REQUIRE_AUTHENTICATED_SENDER"]["description"]
    )
    assert "always, forwarded, or never" in fields["EMAIL_PP_QUOTE_MODE"]["description"]
    assert fields["EMAIL_PP_MAX_OUTBOUND_ATTACHMENTS"]["advanced"] is True
    assert "15 MiB" in fields["EMAIL_PP_MAX_OUTBOUND_TOTAL_BYTES"]["description"]


def test_configuration_never_reads_builtin_email_settings(monkeypatch) -> None:
    monkeypatch.setenv("EMAIL_ADDRESS", "built-in@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "built-in-secret")
    monkeypatch.setenv("EMAIL_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("EMAIL_SMTP_HOST", "smtp.example.com")

    assert check_requirements()
    assert not is_configured(SimpleNamespace(extra={}))
    assert environment_enablement() is None


def test_configuration_accepts_email_pp_environment(monkeypatch) -> None:
    for name, value in zip(
        REQUIRED_ENV,
        ("agent@example.com", "secret", "imap.example.com", "smtp.example.com"),
        strict=True,
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("EMAIL_PP_IMAP_PORT", "993")
    monkeypatch.setenv("EMAIL_PP_SENDER_NAME", "Support Team")
    monkeypatch.setenv("EMAIL_PP_QUOTE_MODE", "forwarded")

    assert is_configured(SimpleNamespace(extra={}))
    assert environment_enablement() == {
        "address": "agent@example.com",
        "password": "secret",
        "imap_host": "imap.example.com",
        "smtp_host": "smtp.example.com",
        "imap_port": "993",
        "sender_name": "Support Team",
        "quote_mode": "forwarded",
    }


def test_configuration_accepts_platform_extra_without_environment() -> None:
    config = SimpleNamespace(
        extra={
            "address": "agent@example.com",
            "password": "secret",
            "imap_host": "imap.example.com",
            "smtp_host": "smtp.example.com",
        }
    )

    assert is_configured(config, environ={})


def test_process_history_window_validation_and_environment_mapping(monkeypatch) -> None:
    assert process_history_window("") == 0
    assert process_history_window("0") == 0
    assert process_history_window("-1") == -1
    assert process_history_window("60") == 60
    for value in ("no", "-2"):
        with pytest.raises(ValueError, match="PROCESS_HISTORY_WINDOW"):
            process_history_window(value)
    for name, value in zip(
        REQUIRED_ENV,
        ("agent@example.com", "secret", "imap.example.com", "smtp.example.com"),
        strict=True,
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("EMAIL_PP_PROCESS_HISTORY_WINDOW", "60")
    assert environment_enablement()["process_history_window"] == "60"
    monkeypatch.setenv("EMAIL_PP_PROCESS_HISTORY_WINDOW", "invalid")
    with pytest.raises(ValueError, match="PROCESS_HISTORY_WINDOW"):
        is_configured(SimpleNamespace(extra={}))


def test_processed_mail_deletion_validation_and_environment_mapping(
    monkeypatch,
) -> None:
    assert not delete_processed("")
    assert not delete_processed("false")
    assert delete_processed("true")
    assert delete_processed("1")
    with pytest.raises(ValueError, match="DELETE_PROCESSED"):
        delete_processed("sometimes")
    for name, value in zip(
        REQUIRED_ENV,
        ("agent@example.com", "secret", "imap.example.com", "smtp.example.com"),
        strict=True,
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("EMAIL_PP_DELETE_PROCESSED", "true")
    assert environment_enablement()["delete_processed"] == "true"
    monkeypatch.setenv("EMAIL_PP_DELETE_PROCESSED", "invalid")
    with pytest.raises(ValueError, match="DELETE_PROCESSED"):
        is_configured(SimpleNamespace(extra={}))


def test_outbound_attachment_limit_validation_and_environment_mapping(
    monkeypatch,
) -> None:
    assert outbound_attachment_limit("", setting="TEST", default=10) == 10
    assert outbound_attachment_limit("3", setting="TEST", default=10) == 3
    for value in ("0", "-1", "invalid"):
        with pytest.raises(ValueError, match="TEST"):
            outbound_attachment_limit(value, setting="TEST", default=10)
    for name, value in zip(
        REQUIRED_ENV,
        ("agent@example.com", "secret", "imap.example.com", "smtp.example.com"),
        strict=True,
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("EMAIL_PP_MAX_OUTBOUND_ATTACHMENTS", "3")
    monkeypatch.setenv("EMAIL_PP_MAX_OUTBOUND_TOTAL_BYTES", "1024")

    assert environment_enablement()["max_outbound_attachments"] == "3"
    assert environment_enablement()["max_outbound_total_bytes"] == "1024"
    monkeypatch.setenv("EMAIL_PP_MAX_OUTBOUND_ATTACHMENTS", "0")
    with pytest.raises(ValueError, match="MAX_OUTBOUND_ATTACHMENTS"):
        is_configured(SimpleNamespace(extra={}))


def test_scoped_configuration_never_falls_back_to_another_profile(monkeypatch) -> None:
    from agent.secret_scope import (
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )

    monkeypatch.setenv("EMAIL_PP_PASSWORD", "poisoned-default-profile-password")
    set_multiplex_active(True)
    token = set_secret_scope(
        {
            "EMAIL_PP_ADDRESS": "agent@example.com",
            "EMAIL_PP_PASSWORD": "scoped-password",
            "EMAIL_PP_IMAP_HOST": "imap.example.com",
            "EMAIL_PP_SMTP_HOST": "smtp.example.com",
        }
    )
    try:
        assert environment_enablement()["password"] == "scoped-password"
    finally:
        reset_secret_scope(token)

    token = set_secret_scope({})
    try:
        assert environment_enablement() is None
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)


def test_secret_fallbacks_remain_available_for_default_profile(monkeypatch) -> None:
    monkeypatch.setenv("EMAIL_PP_ADDRESS", "default@example.com")
    with monkeypatch.context() as isolated:
        isolated.setitem(sys.modules, "agent.secret_scope", None)
        assert email_config._secret("EMAIL_PP_ADDRESS") == "default@example.com"

    from agent.secret_scope import set_multiplex_active

    set_multiplex_active(True)
    try:
        assert email_config._secret("EMAIL_PP_ADDRESS") == "default@example.com"
    finally:
        set_multiplex_active(False)


def test_configuration_rejects_non_mapping_platform_extra() -> None:
    assert not is_configured(SimpleNamespace(extra=[]), environ={})


def test_adapter_factory_defers_transport_import(monkeypatch) -> None:
    config = SimpleNamespace()
    adapter_module = ModuleType("hermes_email_pp.adapter")

    class FakeAdapter:
        def __init__(self, received_config: object) -> None:
            self.config = received_config

    adapter_module.EmailPPAdapter = FakeAdapter
    monkeypatch.setitem(sys.modules, "hermes_email_pp.adapter", adapter_module)

    assert create_adapter(config).config is config


def test_unrelated_messages_from_one_sender_get_isolated_threads(tmp_path) -> None:
    router = EmailThreadRouter(tmp_path)

    first = router.resolve("person@example.com", message_id="<first@example.com>")
    second = router.resolve("person@example.com", message_id="<second@example.com>")

    assert first.chat_id == second.chat_id == "person@example.com"
    assert first.thread_id != second.thread_id
    assert "example.com" not in first.thread_id
    assert "first" not in first.thread_id


def test_references_and_generated_aliases_continue_a_thread_after_restart(
    tmp_path,
) -> None:
    first = EmailThreadRouter(tmp_path).resolve(
        "person@example.com", message_id="<root@example.com>"
    )
    router = EmailThreadRouter(tmp_path)
    router.record_outbound(first, "<hermes-1@agent.example>")
    router.update_context(
        first,
        quote_source={"message_id": "<root@example.com>"},
        draft_context={"subject": "Draft"},
    )

    restarted = EmailThreadRouter(tmp_path)
    via_references = restarted.resolve(
        "person@example.com",
        message_id="<reply@example.com>",
        references="<root@example.com> <hermes-1@agent.example>",
    )
    via_generated_alias = restarted.resolve(
        "person@example.com",
        message_id="<reply-two@example.com>",
        in_reply_to="<hermes-1@agent.example>",
    )

    assert via_references.thread_id == first.thread_id == via_generated_alias.thread_id
    assert restarted.context_for(first) == {
        "delivery_context": {"recipient": "person@example.com"},
        "quote_source": {"message_id": "<root@example.com>"},
        "draft_context": {"subject": "Draft"},
    }
    assert (tmp_path / "email_pp" / "threads.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "email_pp").stat().st_mode & 0o777 == 0o700


def test_sender_scope_and_malformed_headers_cannot_merge_threads(tmp_path) -> None:
    router = EmailThreadRouter(tmp_path)
    first = router.resolve("first@example.com", message_id="<root@example.com>")
    other_sender = router.resolve(
        "second@example.com",
        message_id="<reply@example.com>",
        references="<root@example.com>",
    )
    malformed = router.resolve(
        "first@example.com",
        message_id="<reply@example.com>",
        references="not-a-message-id",
        in_reply_to="<root@example.com>",
    )

    assert other_sender.thread_id != first.thread_id
    assert malformed.thread_id != first.thread_id
    assert (
        router.context_for(ThreadRoute("second@example.com", first.thread_id)) is None
    )


def test_context_and_outbound_alias_reject_unknown_or_cross_recipient_routes(
    tmp_path,
) -> None:
    router = EmailThreadRouter(tmp_path, max_threads=1)
    route = router.resolve("person@example.com", message_id="<root@example.com>")
    other = ThreadRoute("other@example.com", route.thread_id)

    with pytest.raises(ValueError, match="valid RFC"):
        router.record_outbound(route, "not-an-id")
    with pytest.raises(ValueError, match="not known"):
        router.record_outbound(other, "<hermes@agent.example>")
    with pytest.raises(ValueError, match="not known"):
        router.update_context(
            other, delivery_context={"recipient": "other@example.com"}
        )

    router.resolve("person@example.com", message_id="<second@example.com>")

    assert router.context_for(route) is None


def test_threading_header_validation_and_profile_home_resolution(
    monkeypatch, tmp_path
) -> None:
    assert email_threading._message_ids(1) is None
    assert email_threading._message_ids("<one@example.com>\n<two@example.com>") is None
    assert email_threading._message_ids("<one@example.com>\n <two@example.com>") == [
        "<one@example.com>",
        "<two@example.com>",
    ]
    assert email_threading._message_ids("<one@example.com> <two@example.com>") == [
        "<one@example.com>",
        "<two@example.com>",
    ]
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "hermes_constants", None)

    assert email_threading.active_profile_home() == tmp_path
    hermes_constants = ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: tmp_path / "active"
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)

    assert email_threading.active_profile_home() == tmp_path / "active"


def test_threading_rejects_blank_sender_and_discards_invalid_persisted_data(
    tmp_path,
) -> None:
    state_path = tmp_path / "email_pp" / "threads.json"
    state_path.parent.mkdir()
    state_path.write_text("[]")
    router = EmailThreadRouter(tmp_path)

    with pytest.raises(ValueError, match="non-blank"):
        router.resolve(" ")

    route = router.resolve("person@example.com")

    assert route.thread_id.startswith("email_pp:")
    assert router.context_for(route) == {
        "delivery_context": {"recipient": "person@example.com"},
        "quote_source": {},
        "draft_context": {},
    }


def test_conflicting_known_aliases_fail_toward_isolation_and_retention_is_bounded(
    tmp_path,
) -> None:
    router = EmailThreadRouter(tmp_path, retention_days=1, max_threads=3)
    first = router.resolve("person@example.com", message_id="<first@example.com>")
    second = router.resolve("person@example.com", message_id="<second@example.com>")

    conflicting = router.resolve(
        "person@example.com",
        message_id="<conflict@example.com>",
        references="<first@example.com> <second@example.com>",
    )
    router._max_threads = 1
    router._data["threads"][conflicting.thread_id]["updated_at"] = 0
    router._prune(2 * 24 * 60 * 60)

    assert conflicting.thread_id not in router._data["threads"]
    assert first.thread_id != second.thread_id != conflicting.thread_id


def test_failed_state_write_removes_its_temporary_file(tmp_path, monkeypatch) -> None:
    router = EmailThreadRouter(tmp_path)

    def fail_replace(*args) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(email_threading.os, "replace", fail_replace)
    monkeypatch.setattr(email_threading.os, "unlink", fail_replace)

    with pytest.raises(OSError, match="disk failure"):
        router.resolve("person@example.com", message_id="<root@example.com>")
