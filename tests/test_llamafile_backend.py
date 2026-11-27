"""claude-hooks, not the engine, picks the embedder's backend (ch-33).

An opencoti llamafile started without ``--gpu`` chooses a GPU itself, a
Vulkan driver can return wrong vectors without an error (RADV, Mesa
20.3.5: cosine 0.38 against the CPU), and under the daemon's sandbox the
engine loads a stale library unless its HOME is writable. These tests
pin each of those answers.
"""
from __future__ import annotations

import math
import random
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_hooks import embedding_manager as em
from claude_hooks import llamafile_backend as lb

REPO = Path(__file__).resolve().parent.parent

# Real `--gpu vulkan --list-devices` output on solidpc (private RADV).
LIST_ONE = """vulkan: loaded /x/ggml-vulkan.so (bundled, beside the executable, 71980952 bytes)
vulkan: abi ggml bac5050ddc8c matches this engine
Available devices:
  Vulkan0: AMD Radeon Graphics (RADV RENOIR) (64601 MiB, 64566 MiB free)
ggml_vulkan: shutdown: 0 device(s) destroyed, 0 kept, instance destroyed
"""
LIST_TWO = """Available devices:
  Vulkan0: NVIDIA GeForce RTX 3090 (24576 MiB, 24000 MiB free) pci=0000:2d:00.0
  Vulkan1: AMD Radeon Graphics (RADV RENOIR) (64601 MiB, 64566 MiB free) pci=0000:30:00.0
"""
# Two identical cards: only the PCI address tells them apart.
LIST_TWINS = """Available devices:
  CUDA0: NVIDIA GeForce RTX 3090 (24576 MiB, 23000 MiB free) pci=0000:2d:00.0
  CUDA1: NVIDIA GeForce RTX 3090 (24576 MiB, 24000 MiB free) pci=0000:41:00.0
"""
# Every device refused (Mesa 20 without the override): nothing usable.
LIST_REFUSED = """ggml_vulkan: AMD RADV RENOIR (ACO) (driver radv 20.3.5) is REFUSED: ...
Available devices:
"""


@pytest.fixture(autouse=True)
def _home(isolated_home):
    return isolated_home


class TestFlags:
    def test_cpu_and_vulkan_always_name_the_backend(self):
        assert lb.flags("cpu") == ["--gpu", "disable"]
        assert lb.flags("vulkan", "Vulkan1") == ["--gpu", "vulkan", "-dev", "Vulkan1",
                                                 "-ngl", "99"]

    def test_vulkan_needs_a_device(self):
        with pytest.raises(lb.BackendError):
            lb.flags("vulkan", "")


class TestChildEnv:
    def test_private_home_and_tmpdir(self, isolated_home):
        env = lb.child_env(mode="cpu", base={"HOME": "/root"}, platform="linux")
        rt = isolated_home / ".claude" / "claude-hooks-llamafile"
        assert env["HOME"] == str(rt / "home") and env["TMPDIR"] == str(rt / "tmp")
        assert (rt / "home").is_dir() and (rt / "tmp").is_dir()

    def test_windows_keeps_home(self):
        env = lb.child_env(mode="cpu", base={"USERPROFILE": "C:/u"}, platform="win32")
        assert "TMPDIR" not in env and env["USERPROFILE"] == "C:/u"

    def test_vulkan_knobs_only_for_vulkan(self):
        inherited = {"VK_ICD_FILENAMES": "/sys.json", "OPENCOTI_VK_ALLOW_OLD_DRIVER": "1"}
        cpu = lb.child_env(mode="cpu", base=dict(inherited), platform="linux")
        assert "VK_ICD_FILENAMES" not in cpu and "OPENCOTI_VK_ALLOW_OLD_DRIVER" not in cpu
        vk = lb.child_env(mode="vulkan", vulkan_icd="/mesa/radeon.json",
                          base=dict(inherited), platform="linux")
        assert vk["VK_ICD_FILENAMES"] == vk["VK_DRIVER_FILES"] == "/mesa/radeon.json"
        assert "OPENCOTI_VK_ALLOW_OLD_DRIVER" not in vk   # the daemon's env never decides
        old = lb.child_env(mode="vulkan", allow_old_driver=True, base={}, platform="linux")
        assert old["OPENCOTI_VK_ALLOW_OLD_DRIVER"] == "1"


class TestDevices:
    def test_parse(self):
        [d] = lb.parse_devices(LIST_ONE)
        assert (d.id, d.name, d.memory_mib, d.free_mib, d.pci) == (
            "Vulkan0", "AMD Radeon Graphics (RADV RENOIR)", 64601, 64566, "")
        assert [(d.id, d.pci) for d in lb.parse_devices(LIST_TWO)] == [
            ("Vulkan0", "0000:2d:00.0"), ("Vulkan1", "0000:30:00.0")]
        assert lb.parse_devices(LIST_REFUSED) == []

    def test_resolve_by_name_or_id(self):
        devs = lb.parse_devices(LIST_TWO)
        assert lb.resolve_device("renoir", devs).id == "Vulkan1"
        assert lb.resolve_device("vulkan0", devs).id == "Vulkan0"
        assert lb.resolve_device("", lb.parse_devices(LIST_ONE)).id == "Vulkan0"

    def test_pci_tells_twins_apart_and_wins(self):
        devs = lb.parse_devices(LIST_TWINS)
        assert lb.resolve_device("", devs, pci="41:00.0").id == "CUDA1"
        assert lb.resolve_device("CUDA0", devs, pci="0000:41:00.0").id == "CUDA1"
        with pytest.raises(lb.BackendError, match="matches several"):
            lb.resolve_device("3090", devs)
        with pytest.raises(lb.BackendError, match="no device at PCI 0000:99:00.0"):
            lb.resolve_device("", devs, pci="99:00.0")

    @pytest.mark.parametrize("spec,listing,match", [
        ("", LIST_TWO, "several"),
        ("RADEON RX", LIST_TWO, "no device matches"),
        ("a", LIST_TWO, "matches several"),
        ("RENOIR", LIST_REFUSED, "no usable"),
    ])
    def test_refuses_rather_than_guesses(self, spec, listing, match):
        with pytest.raises(lb.BackendError, match=match):
            lb.resolve_device(spec, lb.parse_devices(listing))

    def test_cuda_flags_and_listing(self):
        assert lb.flags("cuda", "CUDA1") == ["--gpu", "nvidia", "-dev", "CUDA1", "-ngl", "99"]
        with patch.object(lb.subprocess, "run") as run:
            run.return_value.stdout, run.return_value.stderr = LIST_TWINS, ""
            lb.list_devices("/x.llamafile", {}, backend="cuda", platform="linux")
        assert run.call_args[0][0] == ["/bin/sh", "/x.llamafile", "--gpu", "nvidia",
                                       "--list-devices"]

    def test_child_env_drops_inherited_device_masks(self):
        env = lb.child_env(mode="cuda", base={"CUDA_VISIBLE_DEVICES": "1",
                                              "GGML_VK_VISIBLE_DEVICES": "0"},
                           platform="linux")
        assert "CUDA_VISIBLE_DEVICES" not in env and "GGML_VK_VISIBLE_DEVICES" not in env


class TestProbe:
    def test_reference_ships_for_the_model(self):
        ref = lb.load_reference()
        assert ref and ref["model"] == "qwen3-embedding:0.6b" and ref["pooling"] == "last"
        assert len(ref["texts"]) == len(ref["vectors"]) == 3
        assert {len(v) for v in ref["vectors"]} == {1024}

    def test_package_data_lists_it(self):
        assert "embedder_probe_reference.json" in (REPO / "pyproject.toml").read_text()

    def _fake(self, transform):
        ref = lb.load_reference()
        table = {t: transform(v) for t, v in zip(ref["texts"], ref["vectors"])}
        return ref, (lambda url, text, timeout: table[text])

    def test_matching_vectors_pass(self):
        ref, fake = self._fake(lambda v: [x * 1.001 for x in v])
        ok, detail = lb.check_vectors("u", ref, embed=fake)
        assert ok and "probe cosine" in detail

    def test_wrong_vectors_fail(self):
        rnd = random.Random(1)
        ref, fake = self._fake(lambda v: [x + rnd.gauss(0, 0.05) for x in v])
        ok, detail = lb.check_vectors("u", ref, embed=fake)
        assert not ok and "< 0.99" in detail

    def test_bad_shape_or_error_fails(self):
        ref, fake = self._fake(lambda v: v[:512])
        assert lb.check_vectors("u", ref, embed=fake)[0] is False
        ref, fake = self._fake(lambda v: [math.nan] * len(v))
        assert lb.check_vectors("u", ref, embed=fake)[0] is False

        def boom(url, text, timeout):
            raise OSError("refused")
        assert lb.check_vectors("u", ref, embed=boom) == (False, "probe request failed: refused")


# ------------------------------------------------------------------ manager

class FakeProc:
    def __init__(self, pid):
        self.pid, self.returncode, self.terminated = pid, None, 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _mgr(tmp_path, **kw):
    binary = tmp_path / "embedder.llamafile"
    binary.write_bytes(b"MZqFpD")
    cfg = em.EmbeddingConfig(llamafile_path=str(binary), port=38199,
                             spawn_timeout_seconds=1.0, **kw)
    m = em.EmbeddingManager(cfg)
    m._port_free = lambda: True
    return m


def _run(m, *, health, verified=None, listing=LIST_TWO):
    calls = []
    procs = iter([FakeProc(1), FakeProc(2)])

    def popen(cmd, **kw):
        calls.append((cmd, kw["env"]))
        return next(procs)
    checks = iter(verified or [])
    with patch.object(em.subprocess, "Popen", side_effect=popen), \
            patch.object(m, "_wait_for_health", side_effect=list(health)), \
            patch.object(lb, "list_devices", return_value=listing) as ld, \
            patch.object(lb, "check_vectors", side_effect=lambda url, ref: next(checks)):
        result = m.ensure_running()
    return result, calls, ld


class TestManager:
    def test_vulkan_spawn_names_backend_device_and_driver(self, tmp_path):
        m = _mgr(tmp_path, mode="vulkan", device="RENOIR", vulkan_icd="/mesa/radeon.json")
        result, calls, _ = _run(m, health=[True], verified=[(True, "probe cosine 0.9993")])
        cmd, env = calls[0]
        assert cmd[cmd.index("--gpu"):cmd.index("--gpu") + 6] == [
            "--gpu", "vulkan", "-dev", "Vulkan1", "-ngl", "99"]
        assert env["VK_ICD_FILENAMES"] == "/mesa/radeon.json"
        assert env["HOME"].endswith("claude-hooks-llamafile/home") or em.sys.platform.startswith("win")
        assert result["mode"] == "vulkan" and "gpu_fallback" not in result
        assert m.status()["device"]["id"] == "Vulkan1"
        assert m.status()["device"]["pci"] == "0000:30:00.0"

    def test_wrong_vectors_fall_back_to_cpu(self, tmp_path):
        m = _mgr(tmp_path, mode="vulkan", device="RENOIR")
        result, calls, _ = _run(m, health=[True, True], verified=[
            (False, "probe cosine 0.5347 < 0.99 against the CPU reference")])
        assert len(calls) == 2 and "--gpu" in calls[1][0]
        assert calls[1][0][calls[1][0].index("--gpu") + 1] == "disable"
        assert result["mode"] == "cpu" and result["gpu_fallback"] is True
        assert "0.5347" in result["fallback_reason"] and m.gpu_offload_failed

    def test_unknown_device_starts_on_cpu_and_says_why(self, tmp_path):
        m = _mgr(tmp_path, mode="vulkan", device="RX 9070")
        result, calls, _ = _run(m, health=[True])
        assert len(calls) == 1 and calls[0][0][calls[0][0].index("--gpu") + 1] == "disable"
        assert result["mode"] == "cpu" and "no device matches" in result["fallback_reason"]

    def test_no_health_on_vulkan_falls_back(self, tmp_path):
        m = _mgr(tmp_path, mode="vulkan", device="RENOIR")
        result, calls, _ = _run(m, health=[False, True])
        assert result["mode"] == "cpu" and "no health on vulkan" in result["fallback_reason"]

    def test_cpu_never_asks_for_devices(self, tmp_path):
        m = _mgr(tmp_path, mode="cpu")
        result, calls, ld = _run(m, health=[True])
        ld.assert_not_called()
        assert calls[0][0][calls[0][0].index("--gpu") + 1] == "disable"

    def test_custom_gguf_is_not_checked_against_the_shipped_reference(self, tmp_path):
        m = _mgr(tmp_path, mode="vulkan", device="RENOIR", model_gguf="/x.gguf")
        result, _, _ = _run(m, health=[True], verified=[])   # check_vectors would raise
        assert result["mode"] == "vulkan"

    def test_config_keys(self):
        cfg = em.config_from_dict({"embedding": {
            "mode": "vulkan", "device": "RENOIR", "vulkan_icd": "~/mesa/icd.json",
            "allow_old_driver": True, "verify_backend": False}})
        assert (cfg.mode, cfg.device, cfg.allow_old_driver, cfg.verify_backend) == (
            "vulkan", "RENOIR", True, False)
        assert not cfg.vulkan_icd.startswith("~")
        with pytest.raises(ValueError, match="invalid mode"):
            em.EmbeddingManager(em.EmbeddingConfig(mode="gpu"))


# ------------------------------------------------------------------ installer

class TestInstaller:
    def test_reinstall_keeps_the_backend(self):
        import install
        cfg = {"embedding": {"mode": "vulkan", "device": "RENOIR",
                             "vulkan_icd": "/opt/radv/icd.json", "allow_old_driver": True}}
        with patch.object(Path, "is_file", return_value=True):
            block = install._setup_llamafile_engine(cfg, non_interactive=True, dry_run=False)
        assert (block["mode"], block["device"], block["vulkan_icd"]) == (
            "vulkan", "RENOIR", "/opt/radv/icd.json")
        assert block["allow_old_driver"] is True and block["verify_backend"] is True

    def test_fresh_install_defaults_to_cpu(self):
        import install
        out = install._ask_embedding_backend({}, "/x", non_interactive=True)
        assert out["mode"] == "cpu"

    def test_vulkan_dialog_lists_devices_and_picks_one(self, tmp_path):
        import install
        binary = tmp_path / "e.llamafile"
        binary.write_bytes(b"x")
        answers = iter(["vulkan", "", ""])
        with patch("builtins.input", side_effect=lambda *_: next(answers)), \
                patch.object(lb, "list_devices", return_value=LIST_ONE):
            out = install._ask_embedding_backend({}, str(binary), non_interactive=False)
        assert out == {"mode": "vulkan", "device": "AMD Radeon Graphics (RADV RENOIR)",
                       "pci": "", "vulkan_icd": ""}

    def test_device_by_pci(self, tmp_path):
        import install
        binary = tmp_path / "e.llamafile"
        binary.write_bytes(b"x")
        answers = iter(["cuda", "pci:0000:41:00.0"])
        with patch("builtins.input", side_effect=lambda *_: next(answers)), \
                patch.object(lb, "list_devices", return_value=LIST_TWINS):
            out = install._ask_embedding_backend(
                {}, str(binary), non_interactive=False, backends=("cpu", "vulkan", "cuda"))
        assert out == {"mode": "cuda", "device": "", "pci": "0000:41:00.0", "vulkan_icd": ""}

    def test_cuda_only_offered_by_a_composite_that_has_it(self):
        import install
        out = install._ask_embedding_backend({"mode": "cuda"}, "/x", non_interactive=True,
                                             backends=("cpu", "vulkan"))
        assert out["mode"] == "cpu"

    def test_composite_menu_offers_this_platforms_cuda_build_only(self):
        import install
        with patch.object(install, "_host_platform", return_value="linux-x86_64"):
            assert install._composite_choices() == ["vulkan", "cuda-linux", "legacy"]
        with patch.object(install, "_host_platform", return_value="win-x86_64"):
            assert install._composite_choices() == ["vulkan", "cuda-win", "legacy"]
        with patch.object(install, "_host_platform", return_value="linux-aarch64"):
            assert install._composite_choices() == ["vulkan", "legacy"]

    def test_rerun_keeps_the_composite(self):
        import install
        path = f"/x/{install._EMBEDDER_COMPOSITES['legacy']['asset']}"
        assert install._ask_composite({"llamafile_path": path},
                                      non_interactive=True) == "legacy"
        assert install._ask_composite({}, non_interactive=True) == "vulkan"

    def test_every_composite_has_a_committed_sha(self):
        import install
        for key, spec in install._EMBEDDER_COMPOSITES.items():
            assert len(install._read_committed_composite_sha(spec["asset"])) == 64, key

    def test_dialog_default_is_the_existing_value(self):
        import install
        with patch("builtins.input", return_value=""):
            out = install._ask_embedding_backend({"mode": "cpu"}, "/x", non_interactive=False)
        assert out["mode"] == "cpu"


class TestComposite:
    def test_composer_bakes_no_backend_and_refuses_one(self):
        sh = (REPO / "vendor/llamafile/dist/oc_compose.sh").read_text()
        args_line = sh[sh.index('printf -- "%s\\n"'):sh.index("> .args")]
        for flag in ("--gpu", "-ngl", "--n-gpu-layers", " -dev ", "--device"):
            assert flag not in args_line
        assert "refusing: .args names a backend" in sh
        assert "verify \"$bare\"" in sh          # inputs checked against the pins

    def test_targets_pack_the_libraries_under_the_names_the_engine_loads(self):
        mk = (REPO / "vendor/llamafile/dist/Makefile").read_text()
        for name in ("ggml-vulkan.so", "ggml-vulkan.dll", "ggml-cuda.so", "ggml-cuda.dll",
                     "ggml-cuda-cu12.so", "ggml-cuda-cu12.dll"):
            assert f":{name}\"" in mk, name

    def test_inputs_are_pinned(self):
        sums = (REPO / "vendor/llamafile/opencoti-c14-r19/SHA256SUMS").read_text().split()
        assert "f7eea3ca475ddf32475a3041091ebd85dd0fe4f4b6c4be46a8c3b4ad7a145eb3" in sums
        assert len(sums) == 14          # engine, 2 vulkan, 2 cuda, 2 cuda12


# ------------------------------------------------------------------ PCI

SMI = "0, 00000000:01:00.0\n1, 00000000:2D:00.0\n"


class TestPci:
    """--list-devices prints no PCI address (c14 r19); the host fills it in."""

    def test_nvidia_smi_rows(self):
        assert lb.parse_nvidia_smi(SMI) == {0: "0000:01:00.0", 1: "0000:2d:00.0"}

    def test_cuda_index_maps_to_smi_index(self):
        devs = [lb.Device("CUDA0", "RTX 3090", 24124, 23753),
                lb.Device("CUDA1", "RTX 3090", 24124, 23753)]
        got = lb.with_pci(devs, "cuda", smi=lb.parse_nvidia_smi(SMI))
        assert [d.pci for d in got] == ["0000:01:00.0", "0000:2d:00.0"]

    def test_cuda_order_is_pci_order_in_the_child(self, tmp_path):
        env = lb.child_env(mode="cuda", base={"CUDA_DEVICE_ORDER": "FASTEST_FIRST"},
                           home=str(tmp_path), platform="linux")
        assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"

    def test_vulkan_by_the_only_display_device_of_its_vendor(self):
        devs = [lb.Device("Vulkan0", "AMD Radeon Graphics (RADV RENOIR)", 64601, 64566)]
        display = [("0000:01:00.0", "0x10de"), ("0000:18:00.0", "0x1002")]
        assert lb.with_pci(devs, "vulkan", display=display)[0].pci == "0000:18:00.0"

    def test_vulkan_twins_stay_unknown(self):
        devs = [lb.Device("Vulkan0", "AMD Radeon RX 7900", 1, 1),
                lb.Device("Vulkan1", "AMD Radeon RX 7900", 1, 1)]
        display = [("0000:03:00.0", "0x1002"), ("0000:04:00.0", "0x1002")]
        assert [d.pci for d in lb.with_pci(devs, "vulkan", display=display)] == ["", ""]

    def test_an_engine_pci_is_kept(self):
        devs = [lb.Device("Vulkan0", "AMD Radeon", 1, 1, pci="0000:0a:00.0")]
        assert lb.with_pci(devs, "vulkan", display=[("0000:18:00.0", "0x1002")]) == devs

    def test_props_confirms_the_running_device(self, tmp_path):
        m = _mgr(tmp_path, mode="cuda", device="CUDA0")
        m.active_device = lb.Device("CUDA0", "RTX 3090", 24124, 23753)
        body = b'{"opencoti": {"devices": [{"name": "CUDA0", "pci": "0000:01:00.0"}]}}'

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body

        with patch.object(em.urllib.request, "urlopen", return_value=R()):
            m._confirm_pci()
        assert m.active_device.pci == "0000:01:00.0"


# ------------------------------------------------------------------ busy

# /proc/net/tcp: a listener on 38092 (0A), one request in flight (01), the
# client side of it (local port 50311), and a closing server socket (08).
PROC_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid
   0: 00000000:94CC 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0
   1: 0100007F:94CC 0100007F:C487 01 00000000:00000000 00:00000000 00000000 0
   2: 0100007F:C487 0100007F:94CC 01 00000000:00000000 00:00000000 00000000 0
   3: 0100007F:94CC 0100007F:C4B0 08 00000000:00000000 00:00000000 00000000 0
"""
NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State
  TCP    0.0.0.0:38092          0.0.0.0:0              LISTENING
  TCP    127.0.0.1:38092        127.0.0.1:50311        ESTABLISHED
  TCP    127.0.0.1:50311        127.0.0.1:38092        ESTABLISHED
  TCP    [::1]:38092            [::1]:50400            TIME_WAIT
"""


class TestBusy:
    """The first live switch cut a 14k-character request: ``/slots`` read
    idle for ~3 s after it connected, until a slot took it."""

    def test_only_established_server_side_sockets_count(self):
        assert lb.count_proc_net_tcp(PROC_TCP, 38092) == 1
        assert lb.count_proc_net_tcp(PROC_TCP, 50311) == 1
        assert lb.count_proc_net_tcp(PROC_TCP, 1) == 0

    def test_netstat_counts_the_same(self):
        assert lb.count_netstat(NETSTAT, 38092) == 1

    def test_an_open_connection_is_busy_before_any_slot_is(self, tmp_path):
        m = _mgr(tmp_path)
        with patch.object(lb, "open_connections", return_value=1), \
                patch.object(em.urllib.request, "urlopen") as up:
            assert m._busy()
        up.assert_not_called()

    def test_slots_that_time_out_are_busy(self, tmp_path):
        """The mozilla engine answers /slots from its inference loop."""
        m = _mgr(tmp_path)
        with patch.object(lb, "open_connections", return_value=0), \
                patch.object(em.urllib.request, "urlopen",
                             side_effect=em.urllib.error.URLError(TimeoutError("timed out"))):
            assert m._busy()
        with patch.object(lb, "open_connections", return_value=None), \
                patch.object(em.urllib.request, "urlopen", side_effect=TimeoutError()):
            assert m._busy()

    def test_a_refused_server_is_not_busy(self, tmp_path):
        m = _mgr(tmp_path)
        with patch.object(lb, "open_connections", return_value=0), \
                patch.object(em.urllib.request, "urlopen",
                             side_effect=em.urllib.error.URLError(ConnectionRefusedError())):
            assert not m._busy()


# ------------------------------------------------------------------ switch

class TestSwitch:
    def _alive(self, tmp_path, **kw):
        m = _mgr(tmp_path, override_path=str(tmp_path / "override.json"), **kw)
        m.proc = FakeProc(7)
        m.IDLE_GAP_S = 0
        return m

    def test_switch_waits_for_idle_then_restarts_on_the_new_backend(self, tmp_path):
        m = self._alive(tmp_path, mode="vulkan", device="RENOIR")
        busy = iter([True, True, False, False])
        started = []
        with patch.object(m, "_busy", side_effect=lambda: next(busy)), \
                patch.object(em.time, "sleep"), \
                patch.object(m, "ensure_running", side_effect=lambda: started.append(
                    m._effective_mode()) or {"ready": True, "mode": "cpu"}):
            res = m.switch("cpu", reason="opencoti gate on the iGPU")
        assert res["ok"] and res["restarted"] and not res["forced"]
        assert started == ["cpu"]
        assert m.backend()["source"] == "switched"
        assert m.backend()["reason"] == "opencoti gate on the iGPU"

    def test_busy_refuses_unless_forced(self, tmp_path):
        m = self._alive(tmp_path, mode="cpu")
        with patch.object(m, "_busy", return_value=True), patch.object(em.time, "sleep"), \
                patch.object(em.time, "monotonic", side_effect=[0, 0, 0, 999, 999, 999]):
            res = m.switch("cpu", idle_timeout=1)
        assert not res["ok"] and not res["changed"] and "busy" in res["error"]
        assert m.proc is not None and m.override is None

    def test_unknown_device_changes_nothing(self, tmp_path):
        m = self._alive(tmp_path, mode="cpu")
        with patch.object(lb, "list_devices", return_value=LIST_ONE):
            res = m.switch("vulkan", device="RX 9070")
        assert not res["ok"] and not res["changed"] and m.proc.terminated == 0

    def test_switch_persists_until_reset(self, tmp_path):
        m = self._alive(tmp_path, mode="vulkan", device="RENOIR")
        with patch.object(m, "_busy", return_value=False), \
                patch.object(m, "ensure_running", return_value={"ready": True}):
            m.switch("cpu", reason="gate")
        again = em.EmbeddingManager(m.cfg)            # a daemon restart
        assert again.backend()["mode"] == "cpu" and again.backend()["source"] == "switched"
        again.proc = FakeProc(8)
        with patch.object(again, "_busy", return_value=False), \
                patch.object(again, "ensure_running", return_value={"ready": True}):
            res = again.reset()
        assert res["ok"] and again.backend() == {
            "mode": "vulkan", "device": "RENOIR", "pci": "", "source": "config",
            "reason": "", "set_at": None}
        assert not (tmp_path / "override.json").exists()

    def test_a_stopped_server_is_not_started(self, tmp_path):
        m = _mgr(tmp_path, override_path=str(tmp_path / "o.json"), mode="cpu")
        with patch.object(lb, "list_devices", return_value=LIST_TWINS), \
                patch.object(m, "ensure_running") as ens:
            res = m.switch("cuda", pci="41:00.0", persist=False)
        ens.assert_not_called()
        assert res["ok"] and not res["restarted"]
        assert m.backend()["device"] == "CUDA1" and m.backend()["pci"] == "0000:41:00.0"
        assert not (tmp_path / "o.json").exists()

    def test_switch_resets_a_previous_fallback(self, tmp_path):
        m = self._alive(tmp_path, mode="vulkan", device="RENOIR")
        m.gpu_offload_failed, m.fallback_reason = True, "probe cosine 0.53"
        with patch.object(lb, "list_devices", return_value=LIST_ONE), \
                patch.object(m, "_busy", return_value=False), \
                patch.object(m, "ensure_running", return_value={"ready": True}):
            m.switch("vulkan", device="RENOIR")
        assert not m.gpu_offload_failed and m.fallback_reason == ""

    def test_backends_lists_every_backend_and_caches(self, tmp_path):
        m = _mgr(tmp_path, mode="cpu")
        calls = []

        def listing(path, env, backend="vulkan", **kw):
            calls.append(backend)
            return LIST_TWO if backend == "vulkan" else ""
        with patch.object(lb, "list_devices", side_effect=listing):
            out = m.backends()
            m.backends()
        assert calls == ["vulkan", "cuda"]
        b = out["backends"]
        assert b["cpu"]["available"] and b["vulkan"]["available"]
        assert b["vulkan"]["devices"][1]["pci"] == "0000:30:00.0"
        assert not b["cuda"]["available"] and "no usable device" in b["cuda"]["reason"]


# ------------------------------------------------------------------ http + cli

class TestHttp:
    @pytest.fixture
    def server(self):
        from claude_hooks import embedder_http

        class FakeMgr:
            def __init__(self):
                self.calls = []

            def status(self):
                return {"alive": True, "mode": "vulkan"}

            def backends(self, refresh=False):
                self.calls.append(("backends", refresh))
                return {"backends": {"cpu": {"available": True}}}

            def switch(self, mode, **kw):
                self.calls.append(("switch", mode, kw))
                return {"ok": mode != "bad", "changed": mode != "bad"}

            def reset(self, **kw):
                self.calls.append(("reset", kw))
                return {"ok": True, "changed": True}
        mgr = FakeMgr()
        srv = embedder_http.start(mgr, "127.0.0.1", 0) if False else \
            embedder_http.EmbedderHttp(mgr, "127.0.0.1", 0)
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield srv, mgr
        srv.shutdown()
        srv.server_close()

    def _req(self, srv, method, path, body=None):
        import json as _j
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
        data = _j.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, _j.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _j.loads(e.read())

    def test_routes(self, server):
        srv, mgr = server
        assert self._req(srv, "GET", "/embedder/status")[1]["mode"] == "vulkan"
        assert self._req(srv, "GET", "/embedder/backends?refresh=1")[0] == 200
        code, res = self._req(srv, "POST", "/embedder/backend",
                              {"mode": "cuda", "pci": "41:00.0", "reason": "x"})
        assert code == 200 and res["ok"]
        assert self._req(srv, "POST", "/embedder/reset", {})[0] == 200
        assert self._req(srv, "POST", "/embedder/backend", {"mode": "bad"})[0] == 409
        assert self._req(srv, "GET", "/nope")[0] == 404
        assert mgr.calls[0] == ("backends", True)
        assert mgr.calls[1][2]["pci"] == "41:00.0"

    def test_switching_is_loopback_only(self):
        from claude_hooks import embedder_http
        assert embedder_http._loopback("127.0.0.1") and embedder_http._loopback("::1")
        assert not embedder_http._loopback("192.168.178.25")


class TestCli:
    def test_embedder_verbs(self):
        from claude_hooks import daemon_ctl
        p = daemon_ctl._build_parser()
        a = p.parse_args(["embedder", "switch", "cpu", "--reason", "gate on the iGPU"])
        assert (a.cmd, a.emb_cmd, a.mode, a.reason) == ("embedder", "switch", "cpu",
                                                        "gate on the iGPU")
        a = p.parse_args(["embedder", "switch", "cuda", "--pci", "0000:41:00.0", "--force"])
        assert a.pci == "0000:41:00.0" and a.force
        assert p.parse_args(["embedder", "backends", "--refresh"]).refresh
        assert p.parse_args(["embedder", "reset"]).emb_cmd == "reset"

    def test_switch_goes_through_the_daemon(self, capsys):
        from claude_hooks import daemon_ctl
        with patch("claude_hooks.daemon_client.embedding_switch", return_value={
                "ok": True, "restarted": True, "waited_s": 0.3, "forced": False,
                "status": {"alive": True, "mode": "cpu", "backend": {
                    "mode": "cpu", "source": "switched", "reason": "gate"}}}) as sw:
            rc = daemon_ctl.main(["embedder", "switch", "cpu", "--reason", "gate"])
        assert rc == 0 and sw.call_args.args == ("cpu",)
        assert "switched, restarted after 0.3 s" in capsys.readouterr().out
