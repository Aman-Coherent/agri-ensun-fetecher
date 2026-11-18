"""Test isolation: every runtime folder (data/, output/, logs/) points at a
throwaway temp dir, so no test can ever read or modify real scrape databases.

Must run before any ``src`` import — config_loader resolves paths at import.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

import pytest

_RUNTIME = tempfile.mkdtemp(prefix="ensun_agri_tests_")
os.environ["ENSUN_AGRI_RUNTIME_ROOT"] = _RUNTIME
atexit.register(shutil.rmtree, _RUNTIME, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_real_vpn_service(monkeypatch):
    """Tests must never talk to the real Proton VPN automation tool
    (127.0.0.1:8765) — not even status reports, let alone rotations.
    Every VPN call behaves as if the tool is not running. Tests that need
    specific VPN answers patch src.vpn_client._request themselves."""
    import src.vpn_client as vpn_client

    monkeypatch.setattr(
        vpn_client,
        "_request",
        lambda *a, **k: (0, {"ok": False, "error": "VPN service disabled in tests"}),
    )


def pytest_sessionstart(session) -> None:  # noqa: ARG001
    from src import config_loader

    assert str(config_loader.DATA_DIR).startswith(_RUNTIME), (
        "Test isolation failed: DATA_DIR is not the temp runtime dir"
    )
