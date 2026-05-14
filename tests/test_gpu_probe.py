"""Tests for claude_hooks.gpu_probe (v1.4).

The probe chain is small but has many failure modes (missing binary,
non-zero exit, timeout, unparseable output) and we want it to fail
**closed** to ``vendor=none`` in every one of them — false positives
mean we'd ask the runtime to offload to a GPU that isn't there.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from claude_hooks import gpu_probe


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #

def _completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Build a ``subprocess.CompletedProcess``-shaped mock."""
    m = MagicMock()
    m.stdout = stdout
    m.stderr = ""
    m.returncode = returncode
    return m


# --------------------------------------------------------------------- #
# Lower-level _run / probe routing
# --------------------------------------------------------------------- #

class TestRun:
    def test_missing_binary_returns_none(self):
        with patch("shutil.which", return_value=None):
            assert gpu_probe._run(["nonexistent-tool"]) is None

    def test_timeout_returns_none(self):
        with patch("shutil.which", return_value="/usr/bin/x"):
            with patch("subprocess.run",
                       side_effect=subprocess.TimeoutExpired(cmd="x", timeout=2)):
                assert gpu_probe._run(["x"]) is None

    def test_oserror_returns_none(self):
        with patch("shutil.which", return_value="/usr/bin/x"):
            with patch("subprocess.run", side_effect=OSError("permission")):
                assert gpu_probe._run(["x"]) is None

    def test_nonzero_exit_returns_none(self):
        with patch("shutil.which", return_value="/usr/bin/x"):
            with patch("subprocess.run", return_value=_completed("output", 1)):
                assert gpu_probe._run(["x"]) is None

    def test_empty_stdout_returns_none(self):
        with patch("shutil.which", return_value="/usr/bin/x"):
            with patch("subprocess.run", return_value=_completed("", 0)):
                assert gpu_probe._run(["x"]) is None

    def test_happy_returns_stdout(self):
        with patch("shutil.which", return_value="/usr/bin/x"):
            with patch("subprocess.run", return_value=_completed("hello\n", 0)):
                assert gpu_probe._run(["x"]) == "hello"


# --------------------------------------------------------------------- #
# nvidia probe
# --------------------------------------------------------------------- #

class TestNvidiaProbe:
    def test_single_gpu(self):
        with patch.object(gpu_probe, "_run", return_value="24576, 22134"):
            out = gpu_probe._probe_nvidia()
        assert out == {
            "vendor": "nvidia",
            "total_mb": 24576,
            "free_mb": 22134,
            "raw": "24576, 22134",
        }

    def test_multi_gpu_sums(self):
        out_lines = "24576, 22134\n16384, 14000\n"
        with patch.object(gpu_probe, "_run", return_value=out_lines):
            out = gpu_probe._probe_nvidia()
        assert out["vendor"] == "nvidia"
        assert out["total_mb"] == 24576 + 16384
        assert out["free_mb"] == 22134 + 14000

    def test_unparseable_returns_none(self):
        with patch.object(gpu_probe, "_run", return_value="not csv"):
            assert gpu_probe._probe_nvidia() is None

    def test_non_numeric_returns_none(self):
        with patch.object(gpu_probe, "_run", return_value="lots, free"):
            assert gpu_probe._probe_nvidia() is None

    def test_zero_total_returns_none(self):
        with patch.object(gpu_probe, "_run", return_value="0, 0"):
            assert gpu_probe._probe_nvidia() is None

    def test_no_output_returns_none(self):
        with patch.object(gpu_probe, "_run", return_value=None):
            assert gpu_probe._probe_nvidia() is None


# --------------------------------------------------------------------- #
# AMD / rocm probe
# --------------------------------------------------------------------- #

class TestAmdProbe:
    def _rocm_json(self, total_b: int, used_b: int) -> str:
        return json.dumps({
            "card0": {
                "VRAM Total Memory (B)": str(total_b),
                "VRAM Total Used Memory (B)": str(used_b),
            }
        })

    def test_single_card(self):
        # 16 GiB total, 4 GiB used.
        total_b = 16 * 1024**3
        used_b = 4 * 1024**3
        with patch.object(gpu_probe, "_run", return_value=self._rocm_json(total_b, used_b)):
            out = gpu_probe._probe_amd()
        assert out["vendor"] == "amd"
        assert out["total_mb"] == 16 * 1024
        assert out["free_mb"] == 12 * 1024

    def test_multi_card_sums(self):
        per_card_total = 8 * 1024**3
        per_card_used = 2 * 1024**3
        blob = json.dumps({
            "card0": {
                "VRAM Total Memory (B)": str(per_card_total),
                "VRAM Total Used Memory (B)": str(per_card_used),
            },
            "card1": {
                "VRAM Total Memory (B)": str(per_card_total),
                "VRAM Total Used Memory (B)": str(per_card_used),
            },
            "system": {"random": "ignored"},  # non-card key, must be skipped
        })
        with patch.object(gpu_probe, "_run", return_value=blob):
            out = gpu_probe._probe_amd()
        assert out["total_mb"] == 16 * 1024
        assert out["free_mb"] == 12 * 1024

    def test_invalid_json_returns_none(self):
        with patch.object(gpu_probe, "_run", return_value="{not json"):
            assert gpu_probe._probe_amd() is None

    def test_non_dict_json_returns_none(self):
        with patch.object(gpu_probe, "_run", return_value=json.dumps([])):
            assert gpu_probe._probe_amd() is None

    def test_missing_keys_returns_none(self):
        # Has card entries but the expected memory keys aren't there.
        blob = json.dumps({"card0": {"some-other-field": "x"}})
        with patch.object(gpu_probe, "_run", return_value=blob):
            assert gpu_probe._probe_amd() is None

    def test_non_integer_values_returns_none(self):
        blob = json.dumps({
            "card0": {
                "VRAM Total Memory (B)": "lots",
                "VRAM Total Used Memory (B)": "some",
            }
        })
        with patch.object(gpu_probe, "_run", return_value=blob):
            assert gpu_probe._probe_amd() is None


# --------------------------------------------------------------------- #
# Vulkan probe
# --------------------------------------------------------------------- #

class TestVulkanProbe:
    def test_discrete_gpu_present(self):
        summary = (
            "Devices:\n"
            "GPU0:\n"
            "        deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU\n"
        )
        with patch.object(gpu_probe, "_run", return_value=summary):
            out = gpu_probe._probe_vulkan()
        assert out is not None
        assert out["vendor"] == "vulkan"
        assert out["total_mb"] is None
        assert out["free_mb"] is None
        assert "DISCRETE_GPU" in out["raw"]

    def test_only_cpu_device_returns_none(self):
        summary = (
            "Devices:\n"
            "CPU0:\n"
            "        deviceType         = PHYSICAL_DEVICE_TYPE_CPU\n"
        )
        with patch.object(gpu_probe, "_run", return_value=summary):
            assert gpu_probe._probe_vulkan() is None

    def test_no_output_returns_none(self):
        with patch.object(gpu_probe, "_run", return_value=None):
            assert gpu_probe._probe_vulkan() is None


# --------------------------------------------------------------------- #
# probe() chain + convenience functions
# --------------------------------------------------------------------- #

class TestProbeChain:
    def test_nvidia_wins_first(self):
        nvi = {"vendor": "nvidia", "total_mb": 1, "free_mb": 1, "raw": "x"}
        with patch.object(gpu_probe, "_probe_nvidia", return_value=nvi):
            with patch.object(gpu_probe, "_probe_amd") as amd_spy:
                with patch.object(gpu_probe, "_probe_vulkan") as vk_spy:
                    out = gpu_probe.probe()
        assert out is nvi
        amd_spy.assert_not_called()
        vk_spy.assert_not_called()

    def test_falls_through_to_amd(self):
        amd = {"vendor": "amd", "total_mb": 8, "free_mb": 4, "raw": "rocm-smi"}
        with patch.object(gpu_probe, "_probe_nvidia", return_value=None):
            with patch.object(gpu_probe, "_probe_amd", return_value=amd):
                with patch.object(gpu_probe, "_probe_vulkan") as vk_spy:
                    out = gpu_probe.probe()
        assert out is amd
        vk_spy.assert_not_called()

    def test_falls_through_to_vulkan(self):
        vk = {"vendor": "vulkan", "total_mb": None, "free_mb": None, "raw": "x"}
        with patch.object(gpu_probe, "_probe_nvidia", return_value=None):
            with patch.object(gpu_probe, "_probe_amd", return_value=None):
                with patch.object(gpu_probe, "_probe_vulkan", return_value=vk):
                    out = gpu_probe.probe()
        assert out is vk

    def test_all_none_returns_none_vendor(self):
        with patch.object(gpu_probe, "_probe_nvidia", return_value=None):
            with patch.object(gpu_probe, "_probe_amd", return_value=None):
                with patch.object(gpu_probe, "_probe_vulkan", return_value=None):
                    out = gpu_probe.probe()
        assert out == {"vendor": "none", "total_mb": None,
                       "free_mb": None, "raw": None}

    def test_probe_function_raises_treated_as_none(self):
        """Defensive guard: if a probe layer raises despite its own
        internal try/except, the chain must continue rather than
        propagate."""
        def boom():
            raise RuntimeError("internal contract violated")

        with patch.object(gpu_probe, "_probe_nvidia", side_effect=boom):
            with patch.object(gpu_probe, "_probe_amd", return_value=None):
                with patch.object(gpu_probe, "_probe_vulkan", return_value=None):
                    out = gpu_probe.probe()
        assert out["vendor"] == "none"


class TestHasGpu:
    def test_true_when_nvidia(self):
        with patch.object(gpu_probe, "probe",
                          return_value={"vendor": "nvidia", "total_mb": 1,
                                        "free_mb": 1, "raw": "x"}):
            assert gpu_probe.has_gpu() is True

    def test_false_when_none(self):
        with patch.object(gpu_probe, "probe",
                          return_value={"vendor": "none", "total_mb": None,
                                        "free_mb": None, "raw": None}):
            assert gpu_probe.has_gpu() is False


class TestCanFitInVram:
    def test_fits_with_headroom(self):
        with patch.object(gpu_probe, "probe",
                          return_value={"vendor": "nvidia",
                                        "total_mb": 24000, "free_mb": 14000,
                                        "raw": "x"}):
            assert gpu_probe.can_fit_in_vram(12000, headroom_mb=512) is True

    def test_doesnt_fit(self):
        with patch.object(gpu_probe, "probe",
                          return_value={"vendor": "nvidia",
                                        "total_mb": 16000, "free_mb": 4000,
                                        "raw": "x"}):
            assert gpu_probe.can_fit_in_vram(5000) is False

    def test_unknown_when_vulkan(self):
        with patch.object(gpu_probe, "probe",
                          return_value={"vendor": "vulkan", "total_mb": None,
                                        "free_mb": None, "raw": "x"}):
            assert gpu_probe.can_fit_in_vram(1000) is None

    def test_unknown_when_no_gpu(self):
        with patch.object(gpu_probe, "probe",
                          return_value={"vendor": "none", "total_mb": None,
                                        "free_mb": None, "raw": None}):
            assert gpu_probe.can_fit_in_vram(1000) is None
