"""Decide when a package boundary cannot have been crossed.

``describe_scope`` warns that a reference search stopped at a package
root and that sibling packages were therefore not searched. The warning
is correct and load-bearing — but on a symbol nothing outside the
package *can* see, it is noise attached to a complete answer, and a
warning that cries wolf on complete answers is one a caller learns to
skip on the incomplete ones.

The cheap way to remove it is the inversion: do not try to prove the
symbol IS used across the boundary, which needs the cross-package
resolution the narrow root exists to avoid. Prove it CANNOT be. If the
declaring file is not reachable from the package's published entry
points, no sibling can import it, so the narrow search saw every use
that exists and the boundary is not a boundary for this symbol.

Everything here is biased one way: **unsure means keep the warning.** A
false suppression hides a real incompleteness, which is the failure mode
the scope note was added for; a false warning only costs a paragraph.
So every hole below returns False (may escape):

* no ``package.json``, or one without an ``exports`` map — ``main`` plus
  an unrestricted path means any deep import is legal, so every file is
  public whatever the entry re-exports;
* a ``*`` anywhere in the ``exports`` map, or a ``typesVersions`` block
  — both map whole subtrees the entry graph does not describe;
* a ``tsconfig`` ``paths`` alias aiming into this package's sources from
  an ancestor — the compiler then resolves a sibling's import straight
  at ``src/``, bypassing ``exports`` entirely (cline does exactly this);
* an entry target that cannot be mapped back to a source file, an
  unresolvable relative re-export, or a graph bigger than we will walk;
* a symbol the declaring file *imports* — then the declaration is
  somewhere else and this file's reachability says nothing about it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Iterator, Optional

#: Extensions a re-export chain can be written in.
_SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs",
                    ".cjs")

#: A published package with more modules than this in its public graph
#: is not worth walking on a nav request. Bail rather than stall.
_MAX_FILES = 400

#: ``extends`` chains are short in practice; this only stops a cycle.
_MAX_EXTENDS = 6

#: Directories that hold build output. An entry target must never be
#: mapped onto one: bundled output has no relative re-exports left, so
#: walking it reaches nothing and *every* file in the package reads as
#: private. Measured on cline's ``apps/cli``, which declares no
#: ``outDir`` — the fallback found the real, built ``dist/index.js`` and
#: suppressed all 55 symbols sampled for exactly that wrong reason.
_BUILD_DIR_NAMES = frozenset({"dist", "build", "out", "lib", "es", "esm",
                              "cjs", "umd", "output", ".next"})

_RE_EXPORT_FROM = re.compile(
    r"""^[^\S\n]*export[^\S\n]+(?:type[^\S\n]+)?"""
    r"""(?:\*(?:[^\S\n]+as[^\S\n]+[\w$]+)?|\{[^}]*\})"""
    r"""[^\S\n]*from[^\S\n]*['"]([^'"]+)['"]""",
    re.M)

_IMPORT_FROM = re.compile(
    r"""^[^\S\n]*import[^\S\n]+(?:type[^\S\n]+)?([^'";]*?)"""
    r"""[^\S\n]*from[^\S\n]*['"]([^'"]+)['"]""",
    re.M)

#: ``export { A, B as C }`` with no ``from``: a re-export of bindings the
#: file imported, which reaches those modules without an export-from.
_LOCAL_EXPORT_LIST = re.compile(
    r"""^[^\S\n]*export[^\S\n]+(?:type[^\S\n]+)?\{([^}]*)\}[^\S\n]*;?"""
    r"""[^\S\n]*$""",
    re.M)

_LOCAL_EXPORT_DEFAULT = re.compile(
    r"""^[^\S\n]*export[^\S\n]+default[^\S\n]+([\w$]+)[^\S\n]*;?[^\S\n]*$""",
    re.M)

_BARE_EXPORT_STAR = re.compile(
    r"""^[^\S\n]*export[^\S\n]+(?:type[^\S\n]+)?\*""", re.M)


def _read_jsonc(path: Path) -> Optional[dict]:
    """Parse a ``package.json`` / ``tsconfig.json``, comments and all.

    ``tsconfig`` is JSONC and every real one in the wild uses it, so a
    strict parser would make the tsconfig hole below unobservable —
    it would look like "no paths" rather than "could not tell".
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    out: list[str] = []
    i, n = 0, len(raw)
    in_str = False
    while i < n:
        c = raw[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(raw[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and raw[i + 1] == "/":
            while i < n and raw[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and raw[i + 1] == "*":
            end = raw.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    text = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _export_targets(node: object) -> Iterator[str]:
    """Every string target in an ``exports`` map, conditions included."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield key          # keys carry the subpath wildcards
            yield from _export_targets(value)
    elif isinstance(node, list):
        for value in node:
            yield from _export_targets(value)


def _compiler_options(root: Path) -> dict:
    """``compilerOptions`` for ``root``, following ``extends`` shallowly.

    Only ``outDir`` / ``rootDir`` are wanted, and a package that sets
    them in a base config is ordinary — cline's do the opposite, which
    is why the local file is read first and the base only fills gaps.
    """
    merged: dict = {}
    seen: set[Path] = set()
    current: Optional[Path] = root / "tsconfig.json"
    for _ in range(_MAX_EXTENDS):
        if current is None or current in seen or not current.is_file():
            break
        seen.add(current)
        data = _read_jsonc(current)
        if data is None:
            break
        opts = data.get("compilerOptions")
        if isinstance(opts, dict):
            for key, value in opts.items():
                merged.setdefault(key, value)
        parent = data.get("extends")
        if not isinstance(parent, str) or parent.startswith("@"):
            break
        nxt = (current.parent / parent)
        current = nxt if nxt.suffix else nxt.with_suffix(".json")
    return merged


def _strip_code_suffix(rel: str) -> Optional[str]:
    """The path without its code extension, or None if it has none.

    None is how a target that cannot hold a symbol — ``./package.json``,
    a stylesheet, an asset — is told apart from one we failed to map.
    """
    for suffix in (".d.ts", ".d.mts", ".d.cts"):
        if rel.endswith(suffix):
            return rel[: -len(suffix)]
    for suffix in _SOURCE_SUFFIXES:
        if rel.endswith(suffix):
            return rel[: -len(suffix)]
    return None


def _existing(base: Path) -> Optional[Path]:
    for suffix in _SOURCE_SUFFIXES:
        candidate = base.with_name(base.name + suffix)
        if candidate.is_file():
            return candidate
    for suffix in _SOURCE_SUFFIXES:
        candidate = base / f"index{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _source_for_target(root: Path, target: str, out_dir: Optional[str],
                       root_dir: Optional[str]) -> Optional[Path]:
    """Map a published ``exports`` target back to its source file.

    ``exports`` names build output — ``./dist/index.js`` — and the
    question is about ``src/index.ts``. The compiler already states the
    relation as ``outDir`` / ``rootDir``, so use that rather than
    guessing; a package that publishes its sources maps to itself.
    """
    rel = target[2:] if target.startswith("./") else target
    rel = rel.lstrip("/")
    stem = _strip_code_suffix(rel)
    if stem is None:
        return None
    parts = Path(stem).parts
    if not parts:
        return None
    candidates: list[Path] = []
    out_parts = Path(out_dir.strip("./")).parts if out_dir else ()
    if out_parts and parts[:len(out_parts)] == out_parts:
        inner = parts[len(out_parts):]
        if not inner:
            return None
        src_base = (root.joinpath(*Path(root_dir.strip("./")).parts)
                    if root_dir else root)
        candidates.append(src_base.joinpath(*inner))
    elif parts[0] in _BUILD_DIR_NAMES:
        # Output directory the compiler did not declare. ``dist/x`` ->
        # ``src/x`` is the near-universal convention, and guessing it
        # gives a *bigger* entry graph than bailing would, so a wrong
        # guess that still resolves errs towards keeping the warning.
        # The one thing it may never do is fall through to the artifact.
        if len(parts) > 1:
            candidates.append(root.joinpath("src", *parts[1:]))
    else:
        # Published straight from source — cline's cline-hub exports
        # "./src/server.ts" — so the target is already the source path.
        candidates.append(root.joinpath(*parts))
    for base in candidates:
        found = _existing(base)
        if found is not None and not _is_build_output(root, found, out_dir):
            return found
    return None


def _is_build_output(root: Path, path: Path, out_dir: Optional[str]) -> bool:
    """Is ``path`` compiler output rather than a source file?"""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts[:-1]
    if out_dir:
        out_parts = Path(out_dir.strip("./")).parts
        if out_parts and parts[:len(out_parts)] == out_parts:
            return True
    return any(part in _BUILD_DIR_NAMES for part in parts)


def _resolve_relative(from_file: Path, spec: str) -> Optional[Path]:
    """Resolve a relative specifier to a source file inside the package.

    ``./foo.js`` is included because NodeNext-style TypeScript writes
    the *output* extension in the source, so the file on disk is
    ``foo.ts`` and a literal lookup finds nothing.
    """
    base = (from_file.parent / spec)
    if base.is_file():
        return base
    stem = _strip_code_suffix(str(base))
    if stem is not None:
        found = _existing(Path(stem))
        if found is not None:
            return found
    return _existing(base)


def _local_names(bindings: str) -> set[str]:
    """Local names an import clause introduces."""
    names: set[str] = set()
    text = bindings.strip().rstrip(",")
    brace = re.search(r"\{([^}]*)\}", text)
    if brace:
        for piece in brace.group(1).split(","):
            piece = piece.strip()
            if not piece:
                continue
            parts = piece.split()
            names.add(parts[-1])        # `A as B` binds B; `A` binds A
        text = text[: brace.start()] + text[brace.end():]
    for piece in text.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if piece.startswith("*"):
            parts = piece.split()
            if len(parts) >= 3:
                names.add(parts[-1])
            continue
        if re.fullmatch(r"[\w$]+", piece):
            names.add(piece)            # default import
    return names


def _outgoing(text: str) -> list[str]:
    """Specifiers a reached module makes public.

    Two edges count. ``export ... from './x'`` is the obvious one. The
    other is ``import { A } from './x'`` followed by a bare
    ``export { A }``, which publishes ``./x``'s symbol without ever
    naming ``./x`` in an export — miss it and a barrel written that way
    looks like it exports nothing.
    """
    specs = [m.group(1) for m in _RE_EXPORT_FROM.finditer(text)]
    published: set[str] = set()
    for match in _LOCAL_EXPORT_LIST.finditer(text):
        for piece in match.group(1).split(","):
            piece = piece.strip()
            if piece:
                published.add(piece.split()[0])   # `A as B` publishes A
    for match in _LOCAL_EXPORT_DEFAULT.finditer(text):
        published.add(match.group(1))
    if published:
        for match in _IMPORT_FROM.finditer(text):
            if _local_names(match.group(1)) & published:
                specs.append(match.group(2))
    return specs


def _tsconfig_paths_reach(root: Path, boundary: Path) -> bool:
    """Does an ancestor ``tsconfig`` alias a specifier into ``root``?

    A ``paths`` entry like ``"@scope/pkg/*": ["./packages/pkg/src/*"]``
    lets a sibling import any source file under the package by name, and
    the compiler — which is what the LSP answers from — honours it over
    ``exports``. One such alias makes the whole package public no matter
    what its entry re-exports, so it is checked before anything else.
    """
    # Both sides must be resolved before they are compared: this host
    # reaches its checkouts through a symlink, so an unresolved root and
    # a tsconfig-relative target spell the same directory differently
    # and every alias silently misses.
    root = root.resolve(strict=False)
    boundary = boundary.resolve(strict=False)
    for directory in (root, *root.parents):
        for name in ("tsconfig.json", "tsconfig.base.json"):
            config = directory / name
            if not config.is_file():
                continue
            data = _read_jsonc(config)
            if data is None:
                return True            # cannot tell: assume it reaches
            opts = data.get("compilerOptions")
            paths = opts.get("paths") if isinstance(opts, dict) else None
            if not isinstance(paths, dict):
                continue
            base = opts.get("baseUrl")
            anchor = (directory / base) if isinstance(base, str) else directory
            for targets in paths.values():
                for target in (targets if isinstance(targets, list)
                               else [targets]):
                    if not isinstance(target, str):
                        continue
                    resolved = (anchor / target.replace("*", "")).resolve(
                        strict=False)
                    if resolved == root or root in resolved.parents:
                        return True
        if directory == boundary:
            break
    return False


def symbol_is_package_private(root: Path, declared_in: Path,
                              symbol: Optional[str], *,
                              boundary: Optional[Path] = None) -> bool:
    """True only when nothing outside ``root`` can reference ``symbol``.

    False is the safe answer and the default for every case this cannot
    decide — see the module docstring for the list. A True here suppresses
    a warning about an incomplete search, so it has to mean *proved*.
    """
    try:
        return _symbol_is_package_private(root, declared_in, symbol,
                                         boundary=boundary)
    except Exception:  # pragma: no cover - never break a nav response
        return False


def _symbol_is_package_private(root: Path, declared_in: Path,
                               symbol: Optional[str], *,
                               boundary: Optional[Path]) -> bool:
    if not symbol or not str(symbol).strip():
        # Position-addressed call: no name, so the "is it imported here"
        # guard below cannot run and the declaration site is unknown.
        return False
    symbol = str(symbol).strip()
    root = Path(root).resolve(strict=False)
    declared_in = Path(declared_in).resolve(strict=False)
    if not declared_in.is_file():
        return False
    if root != declared_in.parent and root not in declared_in.parents:
        return False
    if declared_in.suffix not in _SOURCE_SUFFIXES:
        return False

    manifest = _read_jsonc(root / "package.json")
    if manifest is None:
        return False
    exports = manifest.get("exports")
    if not isinstance(exports, (dict, str, list)) or not exports:
        # No `exports` means no restriction: `require('pkg/src/x')` is a
        # legal deep import, so every file in the package is public.
        return False
    if manifest.get("typesVersions"):
        return False
    targets = [t for t in _export_targets(exports)]
    if any("*" in t for t in targets):
        return False

    if _tsconfig_paths_reach(root, boundary or root.parent):
        return False

    opts = _compiler_options(root)
    out_dir = opts.get("outDir") if isinstance(opts.get("outDir"), str) else None
    root_dir = (opts.get("rootDir")
                if isinstance(opts.get("rootDir"), str) else None)

    entries: list[Path] = []
    for target in targets:
        if not target.startswith(".") or target in (".", "./"):
            continue                    # a subpath key, not a file
        if _strip_code_suffix(target) is None:
            continue                    # ./package.json, assets, styles
        source = _source_for_target(root, target, out_dir, root_dir)
        if source is None:
            return False                # an entry we cannot account for
        entries.append(source)
    if not entries:
        return False

    # The declaring file must own the symbol. If it imports it, or
    # re-exports it from elsewhere, the declaration is in another file
    # and this file's reachability says nothing about that one.
    text = _read_source(declared_in)
    if text is None:
        return False
    if _BARE_EXPORT_STAR.search(text):
        return False
    for match in _IMPORT_FROM.finditer(text):
        if symbol in _local_names(match.group(1)):
            return False
    for match in _RE_EXPORT_FROM.finditer(text):
        if symbol in match.group(0):
            return False

    return not _reaches(entries, declared_in, root)


def _read_source(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _reaches(entries: Iterable[Path], target: Path, root: Path) -> bool:
    """Walk the public re-export graph; True if it reaches ``target``.

    True is also the answer for anything it could not follow, so a
    caller reading ``not _reaches(...)`` gets the conservative default
    without a second code path.
    """
    queue = [p.resolve(strict=False) for p in entries]
    seen: set[Path] = set()
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        if len(seen) > _MAX_FILES:
            return True                 # too big to walk: assume public
        if current == target:
            return True
        text = _read_source(current)
        if text is None:
            return True
        for spec in _outgoing(text):
            if spec.startswith("."):
                resolved = _resolve_relative(current, spec)
                if resolved is None:
                    return True         # a re-export we cannot resolve
                resolved = resolved.resolve(strict=False)
                if root != resolved.parent and root not in resolved.parents:
                    return True         # leaves the package: unknown
                queue.append(resolved)
            elif spec.startswith("#"):
                return True             # imports-map subpath, unresolved
            else:
                # A bare specifier is another package, except when it is
                # this one re-entering itself by name.
                name = _package_name(root)
                if name and (spec == name or spec.startswith(name + "/")):
                    return True
    return False


def _package_name(root: Path) -> Optional[str]:
    data = _read_jsonc(root / "package.json")
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) and name else None
