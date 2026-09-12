"""Daemon port discovery via the port file.

``DEFAULT_PORT`` is a wish, not a guarantee. On pandorum it sat inside
a Windows reserved range (47013-47112, part of a near-continuous
Hyper-V block) and the daemon could never bind — `WinError 10013`, for
long enough that the Windows canary was exercising no daemon code at
all. Those ranges are re-reserved at boot, so relocating to another
fixed port only moves the failure; the daemon has to be able to take
whatever the OS will give it and say where it landed.

The properties that matter:

* a client with no port file behaves exactly as before (DEFAULT_PORT),
* a published port wins over the default,
* nonsense in the file degrades to the default rather than crashing a
  hook, and
* the port is resolved per call, because the daemon can move across a
  restart while a long-lived process keeps running.
"""
import pytest

from claude_hooks import daemon, daemon_client


@pytest.fixture
def port_file(tmp_path, monkeypatch):
    p = tmp_path / "claude-hooks-daemon.port"
    monkeypatch.setattr(daemon, "DEFAULT_PORT_FILE", p)
    return p


class TestReadWrite:
    def test_roundtrip(self, port_file):
        daemon.write_port_file(49152, port_file)
        assert daemon.read_port_file(port_file) == 49152

    def test_absent_file_reads_none(self, port_file):
        assert daemon.read_port_file(port_file) is None

    def test_garbage_reads_none(self, port_file):
        port_file.write_text("not a port\n")
        assert daemon.read_port_file(port_file) is None

    def test_empty_reads_none(self, port_file):
        port_file.write_text("")
        assert daemon.read_port_file(port_file) is None

    @pytest.mark.parametrize("bad", ["0", "-1", "65536", "99999"])
    def test_out_of_range_reads_none(self, port_file, bad):
        port_file.write_text(bad)
        assert daemon.read_port_file(port_file) is None

    def test_surrounding_whitespace_is_tolerated(self, port_file):
        port_file.write_text("  49152  \n")
        assert daemon.read_port_file(port_file) == 49152

    def test_write_is_atomic_and_leaves_no_litter(self, port_file):
        daemon.write_port_file(49152, port_file)
        daemon.write_port_file(49153, port_file)
        assert daemon.read_port_file(port_file) == 49153
        assert [f.name for f in port_file.parent.iterdir()] == [port_file.name]

    def test_write_never_raises(self, tmp_path, monkeypatch):
        """Publishing the port must not be able to stop the daemon
        serving — the file is an optimisation, the socket is the job."""
        bad = tmp_path / "not-a-dir" / "x.port"
        (tmp_path / "not-a-dir").write_text("i am a file")
        daemon.write_port_file(49152, bad)      # must not raise


class TestClear:
    def test_clear_removes_it(self, port_file):
        daemon.write_port_file(49152, port_file)
        daemon.clear_port_file(port_file)
        assert not port_file.exists()

    def test_clear_is_idempotent(self, port_file):
        daemon.clear_port_file(port_file)
        daemon.clear_port_file(port_file)


class TestResolve:
    def test_no_file_yields_the_default(self, port_file):
        """The overwhelmingly common case must be unchanged."""
        assert daemon.resolve_port(port_file) == daemon.DEFAULT_PORT

    def test_published_port_wins(self, port_file):
        daemon.write_port_file(49152, port_file)
        assert daemon.resolve_port(port_file) == 49152

    def test_corrupt_file_degrades_to_the_default(self, port_file):
        """A hook must not die because a cache file got truncated."""
        port_file.write_text("\x00\x00")
        assert daemon.resolve_port(port_file) == daemon.DEFAULT_PORT


class TestClientResolution:
    def test_client_uses_the_published_port(self, port_file, monkeypatch):
        daemon.write_port_file(49152, port_file)
        monkeypatch.setattr(daemon_client, "resolve_port",
                            lambda: daemon.resolve_port(port_file))
        seen = {}

        def fake_conn(address, timeout=None, *a, **kw):
            seen["port"] = address[1]
            raise OSError("no daemon in a unit test")

        monkeypatch.setattr(daemon_client.socket, "create_connection", fake_conn)
        monkeypatch.setattr(daemon_client, "_read_secret", lambda p: "s" * 16)
        daemon_client.call("Ping", {})
        assert seen["port"] == 49152

    def test_explicit_port_beats_the_file(self, port_file, monkeypatch):
        """An operator passing --port means it."""
        daemon.write_port_file(49152, port_file)
        monkeypatch.setattr(daemon_client, "resolve_port",
                            lambda: daemon.resolve_port(port_file))
        seen = {}

        def fake_conn(address, timeout=None, *a, **kw):
            seen["port"] = address[1]
            raise OSError("no daemon in a unit test")

        monkeypatch.setattr(daemon_client.socket, "create_connection", fake_conn)
        monkeypatch.setattr(daemon_client, "_read_secret", lambda p: "s" * 16)
        daemon_client.call("Ping", {}, port=40404)
        assert seen["port"] == 40404

    def test_resolution_happens_per_call_not_at_import(self, port_file, monkeypatch):
        """A long-lived process must follow the daemon across a restart
        onto a different port."""
        monkeypatch.setattr(daemon_client, "resolve_port",
                            lambda: daemon.resolve_port(port_file))
        seen = []

        def fake_conn(address, timeout=None, *a, **kw):
            seen.append(address[1])
            raise OSError("no daemon in a unit test")

        monkeypatch.setattr(daemon_client.socket, "create_connection", fake_conn)
        monkeypatch.setattr(daemon_client, "_read_secret", lambda p: "s" * 16)

        daemon.write_port_file(49152, port_file)
        daemon_client.call("Ping", {})
        daemon.write_port_file(49153, port_file)      # daemon restarted
        daemon_client.call("Ping", {})
        assert seen == [49152, 49153]


class TestCtlFollowsTheDaemon:
    def test_ctl_port_default_is_resolved_not_pinned(self):
        """Pinning DEFAULT_PORT here reported NOT RESPONDING against a
        healthy daemon that had bound elsewhere."""
        from claude_hooks.daemon_ctl import _build_parser
        args = _build_parser().parse_args(["status"])
        assert args.port is None

    def test_start_waits_on_the_port_the_daemon_publishes(
        self, port_file, tmp_path, monkeypatch,
    ):
        """`start` reported "did not come up" for a daemon that had come
        up perfectly — it polled the port resolved *before* the spawn,
        and the port file is written by the process it is waiting for.
        """
        from claude_hooks import daemon_ctl
        monkeypatch.setattr(daemon_ctl, "resolve_port",
                            lambda: daemon.resolve_port(port_file))
        monkeypatch.setattr(daemon_ctl, "_detect_entry", lambda: "unit test")

        def spawn():
            daemon.write_port_file(49152, port_file)   # daemon picks its port

        monkeypatch.setattr(daemon_ctl, "_platform_start", spawn)
        monkeypatch.setattr(
            daemon_client, "ping",
            lambda **kw: kw["port"] == 49152,
        )
        rc = daemon_ctl.cmd_start(
            host="127.0.0.1", secret_path=tmp_path / "s", wait=2.0,
        )
        assert rc == 0

    def test_start_honours_an_explicit_port_while_waiting(
        self, port_file, tmp_path, monkeypatch,
    ):
        """--port is an instruction, not a hint: don't drift onto the
        port file mid-wait."""
        from claude_hooks import daemon_ctl
        monkeypatch.setattr(daemon_ctl, "resolve_port",
                            lambda: daemon.resolve_port(port_file))
        monkeypatch.setattr(daemon_ctl, "_detect_entry", lambda: "unit test")
        monkeypatch.setattr(daemon_ctl, "_platform_start",
                            lambda: daemon.write_port_file(49152, port_file))
        seen = set()

        def ping(**kw):
            seen.add(kw["port"])
            return False

        monkeypatch.setattr(daemon_client, "ping", ping)
        rc = daemon_ctl.cmd_start(
            host="127.0.0.1", port=40404, secret_path=tmp_path / "s", wait=0.6,
        )
        assert rc == 1
        assert seen == {40404}

    def test_wait_returns_the_port_it_found(self, port_file, tmp_path, monkeypatch):
        from claude_hooks import daemon_ctl
        monkeypatch.setattr(daemon_ctl, "resolve_port",
                            lambda: daemon.resolve_port(port_file))
        daemon.write_port_file(49152, port_file)
        monkeypatch.setattr(daemon_client, "ping",
                            lambda **kw: kw["port"] == 49152)
        assert daemon_ctl._wait_for_ping(
            host="127.0.0.1", port=None, secret_path=tmp_path / "s",
            timeout=1.0,
        ) == 49152

    def test_wait_returns_none_on_timeout(self, port_file, tmp_path, monkeypatch):
        from claude_hooks import daemon_ctl
        monkeypatch.setattr(daemon_ctl, "resolve_port",
                            lambda: daemon.resolve_port(port_file))
        monkeypatch.setattr(daemon_client, "ping", lambda **kw: False)
        assert daemon_ctl._wait_for_ping(
            host="127.0.0.1", port=None, secret_path=tmp_path / "s",
            timeout=0.4,
        ) is None
