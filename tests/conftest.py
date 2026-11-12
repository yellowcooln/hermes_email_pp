"""Hermetic host compatibility tests; establish isolation before host imports."""

import os
import socket
import tempfile
from pathlib import Path

import pytest

_TEST_HOME = tempfile.mkdtemp(prefix="email-pp-tests-", dir=os.environ.get("TMPDIR"))
os.environ["HERMES_HOME"] = _TEST_HOME
for _key in tuple(os.environ):
    if _key.startswith(("EMAIL_", "EMAIL_PP_")):
        os.environ.pop(_key)


@pytest.fixture(autouse=True)
def no_external_sockets(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise AssertionError("External network forbidden in email tests")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return Path(tmp_path)
