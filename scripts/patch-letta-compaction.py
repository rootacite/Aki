#!/usr/bin/env python3
"""Set Letta's local context compaction threshold to a window ratio."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path


DEFAULT_RATIO = 0.5
FUNCTION_RE = re.compile(r"function contextCompactionThreshold\(contextWindow\) \{.*?\n\}", re.S)
PATCHED_RATIO_RE = re.compile(
    r"function contextCompactionThreshold\(contextWindow\) \{\n"
    r"  if \(typeof contextWindow !== \"number\" \|\| !Number\.isFinite\(contextWindow\) \|\| contextWindow <= 0\) \{\n"
    r"    return;\n"
    r"  \}\n"
    r"  return Math\.max\(0, Math\.floor\(contextWindow \* ([0-9.]+)\)\);\n"
    r"\}"
)


def default_bundle_path() -> Path:
    executable = shutil.which("letta")
    if not executable:
        raise SystemExit("letta executable not found in PATH")
    return Path(executable).resolve()


def render_function(ratio: float) -> str:
    return (
        "function contextCompactionThreshold(contextWindow) {\n"
        '  if (typeof contextWindow !== "number" || !Number.isFinite(contextWindow) || contextWindow <= 0) {\n'
        "    return;\n"
        "  }\n"
        f"  return Math.max(0, Math.floor(contextWindow * {ratio:g}));\n"
        "}"
    )


def current_ratio(text: str) -> float | None:
    match = PATCHED_RATIO_RE.search(text)
    return float(match.group(1)) if match else None


def patch_bundle(bundle: Path, ratio: float) -> str:
    if not 0 < ratio <= 1:
        raise ValueError("ratio must be greater than 0 and at most 1")
    text = bundle.read_text(encoding="utf-8")
    patched = current_ratio(text)
    if patched == ratio:
        return f"already patched: ratio={ratio:g} bundle={bundle}"
    match = FUNCTION_RE.search(text)
    if not match:
        raise RuntimeError(f"contextCompactionThreshold not found in {bundle}")
    backup = bundle.with_name(f"{bundle.name}.aki-original")
    if not backup.exists():
        shutil.copy2(bundle, backup)
    updated = text[: match.start()] + render_function(ratio) + text[match.end() :]
    mode = stat.S_IMODE(bundle.stat().st_mode)
    fd, temp_path = tempfile.mkstemp(prefix=f".{bundle.name}.", dir=bundle.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, bundle)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return f"patched: ratio={ratio:g} bundle={bundle} backup={backup}"


def restore_bundle(bundle: Path) -> str:
    backup = bundle.with_name(f"{bundle.name}.aki-original")
    if not backup.exists():
        raise RuntimeError(f"backup not found: {backup}")
    mode = stat.S_IMODE(bundle.stat().st_mode)
    shutil.copy2(backup, bundle)
    os.chmod(bundle, mode)
    return f"restored: bundle={bundle} backup={backup}"


def status(bundle: Path) -> str:
    text = bundle.read_text(encoding="utf-8")
    ratio = current_ratio(text)
    if ratio is not None:
        return f"patched: ratio={ratio:g} bundle={bundle}"
    if FUNCTION_RE.search(text):
        return f"unpatched: bundle={bundle}"
    return f"unsupported: contextCompactionThreshold not found in {bundle}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="Letta bundle path (defaults to resolved letta executable)")
    parser.add_argument("--ratio", type=float, default=DEFAULT_RATIO, help="compaction threshold ratio (default: 0.5)")
    parser.add_argument("--status", action="store_true", help="show patch status")
    parser.add_argument("--restore", action="store_true", help="restore the original bundle")
    args = parser.parse_args(argv)
    bundle = (args.bundle or default_bundle_path()).expanduser().resolve()
    if not bundle.is_file():
        print(f"bundle not found: {bundle}", file=sys.stderr)
        return 1
    try:
        if args.status:
            print(status(bundle))
        elif args.restore:
            print(restore_bundle(bundle))
        else:
            print(patch_bundle(bundle, args.ratio))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, PermissionError):
            print("hint: run this script with sudo", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
