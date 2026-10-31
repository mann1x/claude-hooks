#!/usr/bin/env python3
"""Build the council-sandbox launcher, check it is static, (re)pin it.

    python3 scripts/build_council_sandbox.py              # build + check vs the pin
    python3 scripts/build_council_sandbox.py --pin        # build + write the pin
    python3 scripts/build_council_sandbox.py --out DIR    # + release assets + SHA256SUMS

Both architectures (x86_64, aarch64) are built from
``native/council-sandbox`` with the toolchain ``rust-toolchain.toml``
pins; aarch64 links with rust-lld, so no cross toolchain is needed.
The build is reproducible, which is what makes the pin meaningful: a
host's own build of the pinned source must hash to the pinned asset.

Release (``docs/RELEASING.md``): after a launcher source change, run
``--pin``, commit the pin with the source, and attach ``--out``'s files
to a GitHub release tagged ``council-sandbox-v<version>``.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from claude_hooks.sandbox import launcher  # noqa: E402

ARCHES = ("x86_64", "aarch64")


def crate_version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"',
                  (launcher.SOURCE / "Cargo.toml").read_text(), re.M)
    if not m:
        raise SystemExit("no version in Cargo.toml")
    return m.group(1)


def toolchain() -> str:
    m = re.search(r'^channel\s*=\s*"([^"]+)"',
                  (launcher.SOURCE / "rust-toolchain.toml").read_text(), re.M)
    return m.group(1) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pin", action="store_true",
                    help="write claude_hooks/sandbox/launcher_pin.json")
    ap.add_argument("--out", type=Path,
                    help="copy release assets + SHA256SUMS here")
    ap.add_argument("--arch", action="append", choices=ARCHES,
                    help="limit to these (default: both)")
    a = ap.parse_args()

    shas: dict[str, str] = {}
    for arch in a.arch or ARCHES:
        out = launcher.build(arch)          # raises unless static
        shas[arch] = launcher.sha256_file(out)
        print(f"{arch:8} {shas[arch]}  static  {out}")
        if a.out:
            a.out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out, a.out / f"{launcher.NAME}-{arch}")

    pin = {"version": crate_version(), "toolchain": toolchain(),
           "source_sha256": launcher.source_digest(), "assets": shas}
    if a.out:
        (a.out / "SHA256SUMS").write_text("".join(
            f"{s}  {launcher.NAME}-{k}\n" for k, s in sorted(shas.items())))
        print(f"release assets in {a.out}")
    if a.pin:
        launcher.PIN.write_text(json.dumps(pin, indent=2) + "\n")
        print(f"pinned v{pin['version']} in {launcher.PIN.relative_to(REPO)}")
        return 0
    try:
        old = launcher.load_pin()
    except (OSError, ValueError):
        print("no pin yet: run with --pin")
        return 1
    bad = [k for k, s in shas.items() if old.get("assets", {}).get(k) != s]
    if old.get("source_sha256") != pin["source_sha256"]:
        print("the source changed since it was pinned: run with --pin")
        return 1
    if bad:
        print(f"build differs from the pin for {', '.join(bad)} — is the "
              f"toolchain {old.get('toolchain')}?")
        return 1
    print("matches the pin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
