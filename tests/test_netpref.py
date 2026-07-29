"""IPv4-first resolution: sorting, config off-switch, idempotence, restore."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime import netpref


@pytest.fixture(autouse=True)
def _restore():
    original = socket.getaddrinfo
    netpref._installed = False
    yield
    socket.getaddrinfo = original
    netpref._installed = False


def _fake_infos(*_a, **_k):
    return [(socket.AF_INET6, 1, 6, "", ("::1", 443)),
            (socket.AF_INET6, 1, 6, "", ("::2", 443)),
            (socket.AF_INET, 1, 6, "", ("1.2.3.4", 443))]


def test_sorts_ipv4_first_preserving_order(tmp_path: Path, monkeypatch) -> None:
    init_runtime(tmp_path, examples=True)
    monkeypatch.setattr(netpref, "_original_getaddrinfo", _fake_infos)
    assert netpref.install_ipv4_preference(tmp_path) is True
    infos = socket.getaddrinfo("x", 443)
    assert [i[0] for i in infos] == [socket.AF_INET, socket.AF_INET6, socket.AF_INET6]
    assert infos[1][4][0] == "::1"  # stable within family


def test_config_off_switch(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"network": {"prefer_ipv4": False}})
    original = socket.getaddrinfo
    assert netpref.install_ipv4_preference(tmp_path) is False
    assert socket.getaddrinfo is original


def test_idempotent(tmp_path: Path) -> None:
    init_runtime(tmp_path, examples=True)
    assert netpref.install_ipv4_preference(tmp_path) is True
    wrapped = socket.getaddrinfo
    assert netpref.install_ipv4_preference(tmp_path) is True
    assert socket.getaddrinfo is wrapped  # not double-wrapped
