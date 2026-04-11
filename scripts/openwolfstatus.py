#!/usr/bin/env python3
"""OpenWolf status — shows all daemons, dashboards, and port assignments."""
import json, subprocess, os, sys, shutil

def main():
    registry_path = os.path.join(os.path.expanduser("~"), ".openwolf", "registry.json")
    if not os.path.exists(registry_path):
        print(f"No OpenWolf registry found at {registry_path}")
        sys.exit(1)

    reg = json.load(open(registry_path))

    # Get PM2 status
    pm2_bin = shutil.which("pm2") or "pm2"
    try:
        result = subprocess.run([pm2_bin, "jlist"], capture_output=True, text=True, timeout=10)
        pm2_procs = json.loads(result.stdout) if result.returncode == 0 else []
    except Exception:
        pm2_procs = []

    pm2_status = {p["name"].lower(): p.get("pm2_env", {}).get("status", "unknown") for p in pm2_procs}

    print("=== OpenWolf Project Status ===\n")
    print(f"{'PROJECT':<25} {'DASHBOARD':<12} {'DAEMON':<12} {'STATUS':<10}")
    print(f"{'-------':<25} {'---------':<12} {'------':<12} {'------':<10}")

    for proj in sorted(reg["projects"], key=lambda p: p["name"]):
        name = proj["name"]
        root = proj["root"]
        cfg_path = os.path.join(root, ".wolf", "config.json")
        try:
            cfg = json.load(open(cfg_path))
            dash = cfg["openwolf"]["dashboard"]["port"]
            daemon = cfg["openwolf"]["daemon"]["port"]
        except Exception:
            dash = "?"
            daemon = "?"

        status = pm2_status.get(f"openwolf-{name}".lower(), "not started")
        print(f"{name:<25} :{dash:<11} :{daemon:<11} {status}")

    # PM2 save check
    print()
    try:
        cur_names = sorted(p["name"] for p in pm2_procs if "openwolf" in p["name"])
        dump_path = os.path.join(os.path.expanduser("~"), ".pm2", "dump.pm2")
        if os.path.exists(dump_path):
            saved = json.load(open(dump_path))
            saved_names = sorted(p["name"] for p in saved if "openwolf" in p["name"])
            if cur_names != saved_names:
                print("WARNING: PM2 state has changed since last save!")
                print("  Run: pm2 save")
                print()
    except Exception:
        pass

if __name__ == "__main__":
    main()
