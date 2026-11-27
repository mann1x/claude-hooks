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
  Vulkan0: NVIDIA GeForce RTX 3090 (24576 MiB, 24000 MiB free)
  Vulkan1: AMD Radeon Graphics (RADV RENOIR) (64601 MiB, 64566 MiB free)
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
        assert lb.parse_devices(LIST_ONE) == [("Vulkan0", "AMD Radeon Graphics (RADV RENOIR)")]
        assert [d[0] for d in lb.parse_devices(LIST_TWO)] == ["Vulkan0", "Vulkan1"]
        assert lb.parse_devices(LIST_REFUSED) == []

    def test_resolve_by_name_or_id(self):
        devs = lb.parse_devices(LIST_TWO)
        assert lb.resolve_device("renoir", devs) == "Vulkan1"
        assert lb.resolve_device("vulkan0", devs) == "Vulkan0"
        assert lb.resolve_device("", lb.parse_devices(LIST_ONE)) == "Vulkan0"

    @pytest.mark.parametrize("spec,listing,match", [
        ("", LIST_TWO, "several"),
        ("RADEON RX", LIST_TWO, "no Vulkan device matches"),
        ("a", LIST_TWO, "matches several"),
        ("RENOIR", LIST_REFUSED, "no usable"),
    ])
    def test_refuses_rather_than_guesses(self, spec, listing, match):
        with pytest.raises(lb.BackendError, match=match):
            lb.resolve_device(spec, lb.parse_devices(listing))


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
        assert m.status()["device"] == "Vulkan1"

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
        assert result["mode"] == "cpu" and "no Vulkan device matches" in result["fallback_reason"]

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
                       "vulkan_icd": ""}

    def test_dialog_default_is_the_existing_value(self):
        import install
        with patch("builtins.input", return_value=""):
            out = install._ask_embedding_backend({"mode": "cpu"}, "/x", non_interactive=False)
        assert out["mode"] == "cpu"


class TestComposite:
    def test_makefile_bakes_no_backend(self):
        mk = (REPO / "vendor/llamafile/dist/Makefile").read_text()
        block = mk[mk.index("$(OC_OUT):"):mk.index("# ----- verification")]
        args = block[block.index('@printf'):block.index('> "$(OC_STAGE)/.args"')]
        for flag in ("--gpu", "-ngl", "--n-gpu-layers", '"-dev"', "--device"):
            assert flag not in args
        assert "ggml-vulkan.so" in block and "ggml-vulkan.dll" in block

    def test_inputs_are_pinned(self):
        sums = (REPO / "vendor/llamafile/opencoti-c14-r19/SHA256SUMS").read_text().split()
        assert "f7eea3ca475ddf32475a3041091ebd85dd0fe4f4b6c4be46a8c3b4ad7a145eb3" in sums
        assert len(sums) == 6
