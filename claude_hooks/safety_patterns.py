"""
Default dangerous-pattern list for the command safety scanner.

Each pattern targets a substring that can appear ANYWHERE in a Bash
command — after a pipe, in ``find -exec``, in a subshell, or chained
with ``&&``/``;``. Prefix-based allow-lists in ``~/.claude/settings.json``
can't catch these without becoming unreasonably strict.

Ported from rtfpessoa/code-factory's hooks/command-safety-scanner.sh:
https://github.com/rtfpessoa/code-factory/blob/main/hooks/command-safety-scanner.sh

Each entry: (regex, short_name, reason).
A match returns ``permissionDecision: "ask"`` with the reason so the
user always makes the final call — the hook never auto-denies.
"""

from __future__ import annotations

# NOTE: these are compiled with re.IGNORECASE in pre_tool_use.py, but
# the patterns should still be written with \b for word boundaries so
# we don't match inside unrelated tokens (e.g. match ``sudo`` but not
# ``pseudo``).

DEFAULT_PATTERNS: list[tuple[str, str, str]] = [
    # --- Privilege escalation / system control ---
    (r"\bsudo\b", "sudo", "Contains 'sudo' — requires elevated privileges"),
    (
        r"\bdd\s+(if=|of=|bs=|count=|status=|conv=|seek=|skip=)",
        "dd-flags",
        "Contains 'dd' with disk I/O flags — can overwrite raw disks",
    ),
    (
        r"(^|[|;&])\s*dd\s",
        "dd-cmd",
        "Contains 'dd' at command position — can overwrite raw disks",
    ),
    (r"\bmkfs\b", "mkfs", "Contains 'mkfs' — creates filesystems (destroys existing data)"),
    (r"\bfdisk\b", "fdisk", "Contains 'fdisk' — modifies disk partition tables"),
    (r"\bparted\b", "parted", "Contains 'parted' — modifies disk partitions"),
    (r"\bshutdown\b", "shutdown", "Contains 'shutdown' — shuts down the system"),
    (r"\breboot\b", "reboot", "Contains 'reboot' — reboots the system"),
    (r"\bhalt\b", "halt", "Contains 'halt' — halts the system"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "systemctl-stop",
     "Contains 'systemctl stop/disable/mask' — disables system services"),
    (r"\blaunchctl\b", "launchctl", "Contains 'launchctl' — macOS service manager"),
    (r"\bnvram\b", "nvram", "Contains 'nvram' — modifies firmware settings"),
    (r"\bcsrutil\b", "csrutil", "Contains 'csrutil' — modifies macOS SIP"),
    # --- Destructive file / permission ops ---
    (
        r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)",
        "rm-rf",
        "Contains 'rm -rf' — recursive forced deletion",
    ),
    (r"\brm\s+-[a-zA-Z]*r", "rm-r", "Contains 'rm -r' — recursive deletion"),
    (r"\bchmod\s+-[a-zA-Z]*R", "chmod-R", "Contains 'chmod -R' — recursive permission change"),
    (r"\bchown\s+-[a-zA-Z]*R", "chown-R", "Contains 'chown -R' — recursive ownership change"),
    # --- Supply chain / remote code execution ---
    (
        r"\bcurl\b[^|]*\|\s*(sh|bash|zsh)\b",
        "curl-pipe-sh",
        "Contains 'curl | sh/bash/zsh' — executes remote code directly",
    ),
    (
        r"\bwget\b[^|]*\|\s*(sh|bash|zsh)\b",
        "wget-pipe-sh",
        "Contains 'wget | sh/bash/zsh' — executes remote code directly",
    ),
    # --- Destructive git operations ---
    (
        r"\bgit\s+push\s.*(--force|--mirror|--delete|\s-f\b)",
        "git-push-destructive",
        "Contains destructive git push flag (--force / --mirror / --delete / -f)",
    ),
    (
        r"\bgit\s+reset\s+--hard",
        "git-reset-hard",
        "Contains 'git reset --hard' — discards all uncommitted changes",
    ),
    (
        r"\bgit\s+clean\s+.*-[a-zA-Z]*f",
        "git-clean-f",
        "Contains 'git clean -f' — permanently deletes untracked files",
    ),
    (
        r"\bgit\s+branch\s+.*-D\b",
        "git-branch-D",
        "Contains 'git branch -D' — force-deletes a branch without merge check",
    ),
    (
        r"\bgit\s+checkout\s+--\s",
        "git-checkout-discard",
        "Contains 'git checkout -- ' — discards uncommitted changes",
    ),
    (
        r"\bgit\s+stash\s+(drop|clear)\b",
        "git-stash-destroy",
        "Contains 'git stash drop/clear' — permanently drops stashed changes",
    ),
    # --- Package managers (system-wide installs) ---
    (r"\bbrew\s+install\b", "brew-install", "Contains 'brew install' — installs system packages"),
    (
        r"\bnpm\s+(install|i)\s+(-g|--global)\b",
        "npm-install-g",
        "Contains 'npm install -g' — installs global npm package",
    ),
    (
        r"\bpnpm\s+add\s+(-g|--global)\b",
        "pnpm-install-g",
        "Contains 'pnpm add -g' — installs global pnpm package",
    ),
    (
        r"\bapt(-get)?\s+(install|remove|purge|autoremove)\b",
        "apt-install",
        "Contains 'apt install/remove' — modifies system packages",
    ),
    (
        r"\bpip(3)?\s+install\s+.*--break-system-packages\b",
        "pip-break-system",
        "Contains 'pip install --break-system-packages' — bypasses PEP 668 isolation",
    ),
    # --- Docker destructive ---
    (
        r"\bdocker\s+system\s+prune\b.*(-a|--all|--volumes)",
        "docker-prune-all",
        "Contains 'docker system prune -a/--volumes' — removes all images/volumes",
    ),
    (
        r"\bdocker\s+rm\s+.*-f\b",
        "docker-rm-f",
        "Contains 'docker rm -f' — force-removes running container",
    ),
    (
        r"\bdocker\s+volume\s+rm\b",
        "docker-volume-rm",
        "Contains 'docker volume rm' — deletes persistent data",
    ),
    (
        r"\bkubectl\s+delete\s+(ns|namespace|all|--all)",
        "kubectl-delete",
        "Contains 'kubectl delete' with broad scope — destroys cluster resources",
    ),
    # --- SQL / database destructive ---
    (
        r"\b(drop|truncate)\s+(database|table|schema)\b",
        "sql-drop",
        "Contains DROP/TRUNCATE SQL — destroys data",
    ),
    # --- Networking configuration ---
    (
        r"\biptables\b|\bufw\s+(delete|reset)\b",
        "firewall-mod",
        "Contains firewall-modifying command",
    ),
]
