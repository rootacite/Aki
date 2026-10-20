"""Exec a command inside the akid writable-directory sandbox."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.writable import WritableRegistry, build_bwrap_command, sandbox_environment


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("sandbox_exec: missing -- <command>", file=sys.stderr)
        return 2
    split = argv.index("--")
    own_args, command = argv[:split], argv[split + 1 :]
    if not command:
        print("sandbox_exec: empty command", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state")
    parser.add_argument("--bwrap")
    parser.add_argument("--letta-home")
    parser.add_argument("--backend-dir")
    parser.add_argument("--disabled", action="store_true")
    args = parser.parse_args(own_args)

    enabled_value = os.environ.get("AKI_SANDBOX_ENABLED", "1").strip().lower()
    enabled = enabled_value not in ("0", "false", "no", "off")
    if args.disabled or not enabled:
        os.execvpe(command[0], command, os.environ)

    registry = WritableRegistry(
        state_path=Path(args.state).expanduser() if args.state else None,
        letta_home=Path(args.letta_home).expanduser() if args.letta_home else None,
        backend_dir=Path(args.backend_dir).expanduser() if args.backend_dir else None,
        bwrap_bin=args.bwrap or os.environ.get("AKI_BWRAP_BIN", "bwrap"),
    )
    registry.load()
    registry.ensure_runtime_dirs()
    available, bwrap_path, error = registry.probe(refresh=True)
    if not available or bwrap_path is None:
        print(f"sandbox_exec: sandbox unavailable: {error}", file=sys.stderr)
        return 2
    try:
        bwrap_args = build_bwrap_command(command, registry, bwrap_path=bwrap_path)
    except RuntimeError as exc:
        print(f"sandbox_exec: {exc}", file=sys.stderr)
        return 2
    env = sandbox_environment(os.environ, registry)
    try:
        os.execvpe(bwrap_path, bwrap_args, env)
    except OSError as exc:
        print(f"sandbox_exec: failed to exec bwrap: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
