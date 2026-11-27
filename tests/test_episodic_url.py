"""One rule for where this host reaches the episodic server
(``claude_hooks/episodic_url.py``), shared by verify_deploy and SessionEnd."""
from __future__ import annotations

import pytest

from claude_hooks.episodic_url import server_base_url


@pytest.mark.parametrize("cfg, url", [
    ({"mode": "server", "server_host": "10.0.0.2", "server_port": 11435},
     "http://10.0.0.2:11435"),
    ({"mode": "server"}, "http://127.0.0.1:11435"),
    ({"mode": "server", "server_host": "0.0.0.0", "server_port": 9}, "http://127.0.0.1:9"),
    ({"mode": "server", "server_host": "::"}, "http://127.0.0.1:11435"),
    ({"mode": "server", "server_host": "fd00::2"}, "http://[fd00::2]:11435"),
    ({"mode": "Server", "server_host": "h"}, "http://h:11435"),
    ({"mode": "client", "server_url": "http://srv:11435/"}, "http://srv:11435"),
    ({"mode": "client", "server_url": ""}, None),
    ({"mode": "off", "server_url": "http://srv"}, None),
    ({}, None),
])
def test_server_base_url(cfg, url):
    assert server_base_url(cfg) == url


# ------------------------------------------------------------------ #
# verify_deploy reads the server's /health: a dead search and a sync that
# skipped indexing both count (each hid behind "ok" for weeks).
# ------------------------------------------------------------------ #
import io  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_deploy as vd  # noqa: E402


def _check(body: dict) -> list[tuple[str, str, str]]:
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(json.dumps(body).encode())
    r = vd.Results(quiet=True)
    with mock.patch("claude_hooks.config.load_config",
                    return_value={"episodic": {"mode": "client",
                                               "server_url": "http://srv:1"}}), \
            mock.patch("urllib.request.urlopen", return_value=resp):
        vd.check_episodic(r)
    return r.rows


def test_verify_passes_a_working_search():
    rows = _check({"cli_ok": True, "index_age_hours": 0.5, "last_sync": {"indexed": 3}})
    assert rows[-1][0] == vd.PASS and "search ok" in rows[-1][2]


def test_verify_flags_a_sync_that_skipped_indexing():
    rows = _check({"cli_ok": True, "last_sync": {
        "indexed": 0, "indexing_skipped": True,
        "error": "(embeddings): Semantic indexing skipped — ENOENT"}})
    # client mode: WARN (the fix is on another host); server mode: FAIL
    assert rows[-1][0] == vd.WARN
    assert "skipped semantic indexing" in rows[-1][2]


def test_verify_shows_the_last_stderr_line():
    rows = _check({"cli_ok": False, "error": "episodic-memory exited non-zero",
                   "stderr": "Loading...\nError searching: ENOENT mkdir .cache"})
    assert rows[-1][0] == vd.WARN
    assert rows[-1][2].endswith("Error searching: ENOENT mkdir .cache")
