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
