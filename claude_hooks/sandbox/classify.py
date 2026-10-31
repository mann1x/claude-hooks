"""Put a shell command on the permission ladder by cost and reach.

The design's §5.6 table. With a jail in place this is **not** the safety
boundary — a wrong ``auto`` costs CPU time inside the sandbox, not the
host — so the table only sorts commands by what they spend and where
they reach: reading and testing run, building and interpreters are
logged through the assistant rung, anything that can spend money waits
for a person, and whatever the table does not know goes to
``ask_assistant`` (never ``auto``).

Commands are parsed with :mod:`claude_hooks.shell_ast` (the process
guard's parser), so ``a | b && c`` is three commands and the most
expensive one decides. A command it cannot parse is ``unmatched``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from claude_hooks.tool_registry.policy import (ASK_ASSISTANT, ASK_HUMAN,
                                               AUTO)

INVESTIGATE = frozenset({
    "ls", "cat", "head", "tail", "rg", "grep", "egrep", "fgrep", "find",
    "wc", "file", "stat", "du", "df", "tree", "echo", "printf", "pwd",
    "which", "type", "true", "false", "sort", "uniq", "cut", "tr", "awk",
    "sed", "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "nl", "jq",
    "column", "date", "env", "id", "uname", "basename", "dirname",
    "realpath", "readlink", "test", "[", "tee", "mkdir", "touch", "cp",
    "mv", "rm", "ln", "xxd", "od", "hexdump", "strings", "less", "more",
    "seq", "sleep", "command", "nproc", "free", "ps", "tar", "gzip",
    "gunzip", "zcat", "unzip", "zstd", "base64", "comm", "join", "paste",
    "fold", "fmt", "expand", "rev", "tac", "split", "csplit", "cd",
})
GIT_READ = frozenset({
    "log", "status", "diff", "show", "blame", "grep", "ls-files",
    "ls-tree", "rev-parse", "rev-list", "branch", "tag", "describe",
    "shortlog", "cat-file", "merge-base", "name-rev", "reflog", "config",
    "for-each-ref", "whatchanged", "show-ref", "count-objects",
})
TEST = frozenset({
    "pytest", "py.test", "tox", "nox", "ruff", "mypy", "pyright", "flake8",
    "pylint", "black", "isort", "shellcheck", "eslint", "tsc", "vitest",
    "jest", "ctest",
})
#: ``<tool> <sub>`` pairs that run a test or a check.
TEST_SUB = {"npm": {"test", "run"}, "pnpm": {"test"}, "yarn": {"test"},
            "bun": {"test"}, "cargo": {"test", "check", "clippy", "fmt"},
            "go": {"test", "vet"}, "make": {"check", "test"},
            "dotnet": {"test"}}
#: Interpreters and builds: they run, logged through the assistant rung.
RUN = frozenset({"python", "python3", "node", "bun", "deno", "bash", "sh",
                 "zsh", "perl", "ruby", "make", "cmake", "ninja", "cargo",
                 "go", "npm", "pnpm", "yarn", "npx", "pip", "pip3", "uv",
                 "conda", "gcc", "g++", "cc", "clang", "clang++", "rustc",
                 "javac", "java", "dotnet", "xargs", "timeout", "nice",
                 "time"})
#: Anything that can spend money or reach infrastructure.
SPEND = frozenset({"runpod", "runpodctl", "aws", "gcloud", "gsutil", "az",
                   "kubectl", "helm", "docker", "podman", "terraform",
                   "pulumi", "vercel", "fly", "flyctl", "heroku", "modal",
                   "vastai", "lambda", "ssh", "scp", "rsync", "sftp", "curl",
                   "wget", "gh", "ollama", "huggingface-cli", "hf"})
#: Calls asking more than this wall clock are "long" (§5.6).
LONG_S = 120

_RANK = {AUTO: 0, ASK_ASSISTANT: 1, ASK_HUMAN: 2}


@dataclass(frozen=True)
class Verdict:
    klass: str          # investigate / test / run / long / spend / unmatched
    level: str
    reason: str


def _name(argv: list[str]) -> str:
    return os.path.basename(argv[0]) if argv else ""


def _python_module(argv: list[str]) -> Optional[str]:
    if _name(argv).startswith("python") and len(argv) > 2 and argv[1] == "-m":
        return argv[2]
    return None


def _one(argv: list[str]) -> Verdict:
    name = _name(argv)
    rest = argv[1:]
    sub = next((a for a in rest if not a.startswith("-")), "")
    if name in SPEND:
        return Verdict("spend", ASK_HUMAN, f"`{name}` can spend money or "
                       "reach outside infrastructure")
    if name == "git":
        if sub in GIT_READ or not sub:
            return Verdict("investigate", AUTO, f"`git {sub}` reads history")
        return Verdict("unmatched", ASK_ASSISTANT, f"`git {sub}` is not a "
                       "known read")
    mod = _python_module(argv)
    if mod in TEST:
        return Verdict("test", AUTO, f"`python -m {mod}` runs checks")
    if name in TEST:
        return Verdict("test", AUTO, f"`{name}` runs checks")
    if name in TEST_SUB and sub in TEST_SUB[name]:
        return Verdict("test", AUTO, f"`{name} {sub}` runs checks")
    if name in INVESTIGATE:
        return Verdict("investigate", AUTO, f"`{name}` inspects files")
    if name in RUN:
        return Verdict("run", ASK_ASSISTANT, f"`{name}` runs arbitrary code")
    return Verdict("unmatched", ASK_ASSISTANT,
                   f"`{name or '?'}` is not in the shell classification table")


def _commands(source: str, depth: int = 0) -> Optional[list[list[str]]]:
    from claude_hooks import shell_ast
    tree = shell_ast.try_parse(source)
    if tree is None:
        return None
    out: list[list[str]] = []
    for simple in shell_ast.iter_simple(tree):
        argv = simple.argv
        for w in simple.words:
            for sub in w.subs:
                for s in shell_ast.iter_simple(sub):
                    out.append(s.argv)
        if (depth < 3 and len(argv) >= 3 and _name(argv) in ("sh", "bash")
                and argv[1] == "-c"):
            inner = _commands(argv[2], depth + 1)
            if inner is None:
                return None
            out.extend(inner)
            continue
        if argv:
            out.append(argv)
    return out


def classify(*, argv: Optional[list[str]] = None,
             command: Optional[str] = None, timeout_s: int = 0,
             spend_level: str = ASK_HUMAN) -> Verdict:
    """The ladder rung for one ``run_command`` call."""
    if argv:
        if _name(argv) in ("sh", "bash") and len(argv) >= 3 and argv[1] == "-c":
            cmds = _commands(argv[2])
        else:
            cmds = [list(argv)]
    elif command:
        cmds = _commands(command)
    else:
        return Verdict("unmatched", ASK_ASSISTANT, "empty command")
    if not cmds:
        return Verdict("unmatched", ASK_ASSISTANT,
                       "the command could not be parsed")
    worst = max((_one(c) for c in cmds), key=lambda v: _RANK[v.level])
    if worst.klass == "spend":
        worst = Verdict("spend", spend_level, worst.reason)
    if timeout_s > LONG_S and _RANK[worst.level] < _RANK[ASK_ASSISTANT]:
        return Verdict("long", ASK_ASSISTANT,
                       f"asks {timeout_s}s, over the {LONG_S}s of a short "
                       "command")
    return worst


__all__ = ["Verdict", "classify", "LONG_S"]
