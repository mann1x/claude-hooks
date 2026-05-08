#!/usr/bin/env python3
"""Capture or verify Ollama model snapshots for benchmark runs.

The benchmark protocol (docs/benchmarks/EVALUATION.md §6) pins each
label to a specific model snapshot so r2/r3 runs can detect whether
the cloud upstream silently rotated the model behind the same tag.
If it did, every prior run is invalidated.

Modes
-----

``capture``
    Probe ``/api/show`` for each unique model used by the consultants
    config and write the responses to ``<dir>/models.json``. Run at
    the start of r1.

``verify``
    Re-probe the same models and compare against ``<dir>/models.json``
    field-by-field. Exits 0 on match, 2 on drift, 3 on probe failure.
    Run before r2/r3 — and re-run all prior rN if drift is reported.

Comparison fields (any difference = drift):
- ``modified_at`` — primary drift signal for cloud models (cloud
  rotations bump this even when the tag stays the same).
- ``capabilities`` — adding/removing ``thinking``, ``tools``, etc.
  is a behavioral change.
- ``details.parameter_size`` — quantisation or architecture change.
- ``details.quantization_level``
- ``model_info.<arch>.context_length``

Local-only models also have a ``digest`` field; cloud models don't.
We include digest in the capture when present and compare it when
present in both sides.

Output (capture)
----------------

::

    {
      "captured_at": "2026-05-07T12:34:56+02:00",
      "endpoint": "http://192.168.178.2:11433",
      "models": {
        "kimi-k2.6:cloud": {
          "modified_at": "2026-03-31T00:00:00Z",
          "capabilities": ["vision", "thinking", "completion", "tools"],
          "details": {...},
          "model_info": {...},
          "raw": <full /api/show response>
        },
        ...
      }
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


def _default_endpoint() -> str:
    """Pick the same Ollama endpoint the consultants engine uses
    (caliber proxy). Order: env CALIBER_GROUNDING_UPSTREAM, env
    OLLAMA_HOST (only when http(s)://), then 192.168.178.2:11433."""
    upstream = os.environ.get("CALIBER_GROUNDING_UPSTREAM")
    if upstream:
        return upstream
    ollama_host = os.environ.get("OLLAMA_HOST", "")
    if ollama_host.startswith(("http://", "https://")):
        return ollama_host
    return "http://192.168.178.2:11433"


def _api_show(endpoint: str, model: str, timeout_s: float = 10.0) -> dict:
    body = json.dumps({"model": model}).encode()
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/api/show",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read())


# Comparison subset extracted from the full /api/show response.
COMPARE_FIELDS = (
    "modified_at",
    "digest",
    "capabilities",
    "details",
    "model_info",
)


def _compact(raw: dict) -> dict:
    """Pick the fields that matter for drift comparison."""
    out: dict = {}
    for k in COMPARE_FIELDS:
        if k in raw:
            out[k] = raw[k]
    return out


def _consultants_models_in_use() -> list[str]:
    """Return the unique list of role->model tags from the
    current consultants config. Falls back to a bare ``kimi-k2.6:cloud``
    if the config can't be read.
    """
    repo = Path(__file__).resolve().parent.parent
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    try:
        from consultants import config as cc
    except ImportError:
        return ["kimi-k2.6:cloud"]
    cfg = cc.load_config(Path.cwd())
    seen = []
    for role, rc in cfg.roles.items():
        if rc.enabled and rc.model and rc.model not in seen:
            seen.append(rc.model)
    return seen or ["kimi-k2.6:cloud"]


def cmd_capture(args: argparse.Namespace) -> int:
    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "models.json"

    models = list(args.model) if args.model else _consultants_models_in_use()
    endpoint = args.endpoint or _default_endpoint()

    snapshot: dict[str, Any] = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "endpoint": endpoint,
        "models": {},
    }
    failed: list[str] = []
    for m in models:
        try:
            raw = _api_show(endpoint, m)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError) as e:
            failed.append(f"{m}: {e}")
            continue
        compact = _compact(raw)
        compact["raw"] = raw
        snapshot["models"][m] = compact
        print(f"::: captured {m}: modified_at={compact.get('modified_at','-')}")

    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"::: wrote {out_path}")
    if failed:
        print(f"!!! {len(failed)} probes failed:", file=sys.stderr)
        for line in failed:
            print(f"    {line}", file=sys.stderr)
        return 3
    return 0


def _diff_field(name: str, prev: Any, cur: Any) -> list[str]:
    if prev == cur:
        return []
    return [f"  - {name}: prev={prev!r}  cur={cur!r}"]


def cmd_verify(args: argparse.Namespace) -> int:
    in_dir = Path(args.dir)
    in_path = in_dir / "models.json"
    if not in_path.is_file():
        print(f"!!! no snapshot at {in_path}", file=sys.stderr)
        return 1
    snap = json.loads(in_path.read_text(encoding="utf-8"))
    endpoint = args.endpoint or snap.get("endpoint") or _default_endpoint()

    drift_lines: list[str] = []
    probe_fail: list[str] = []
    for m, prev in snap.get("models", {}).items():
        try:
            raw = _api_show(endpoint, m)
        except Exception as e:
            probe_fail.append(f"{m}: {e}")
            continue
        cur = _compact(raw)
        for f in COMPARE_FIELDS:
            drift_lines.extend(_diff_field(f"{m}.{f}",
                                           prev.get(f), cur.get(f)))

    if probe_fail:
        print("!!! probe failures (cannot verify):", file=sys.stderr)
        for line in probe_fail:
            print(f"    {line}", file=sys.stderr)
        return 3
    if drift_lines:
        print("!!! MODEL DRIFT — prior runs in this label are invalidated:",
              file=sys.stderr)
        for line in drift_lines:
            print(line, file=sys.stderr)
        print("\n    Per EVALUATION.md §6: re-execute all prior r1..rN runs",
              file=sys.stderr)
        print("    before adding the next run to this label.\n",
              file=sys.stderr)
        return 2
    print(f"::: verified — {len(snap.get('models', {}))} model(s) match snapshot")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture",
                         help="Capture /api/show snapshot for the label.")
    cap.add_argument("dir",
                     help="benchmark label directory (will be created)")
    cap.add_argument("--endpoint",
                     help="Ollama base URL (default: CALIBER_GROUNDING_UPSTREAM "
                          "or http://192.168.178.2:11433)")
    cap.add_argument("--model", action="append",
                     help="Probe this model (repeatable). Default: every "
                          "model in the consultants config.")
    cap.set_defaults(fn=cmd_capture)

    ver = sub.add_parser("verify",
                         help="Verify current /api/show matches stored snapshot.")
    ver.add_argument("dir", help="benchmark label directory")
    ver.add_argument("--endpoint",
                     help="Override the endpoint (otherwise read from snapshot)")
    ver.set_defaults(fn=cmd_verify)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
