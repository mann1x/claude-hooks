#!/bin/bash
# OpenWolf status — shows all daemons, dashboards, and port assignments
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$SCRIPT_DIR/openwolfstatus.py"
echo ""
echo "=== PM2 Process List ==="
pm2 list 2>/dev/null | grep openwolf
