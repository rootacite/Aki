#!/usr/bin/env python3
"""Build and install the vendored Letta Code runtime.

The submodule is treated as read-only upstream source. Aki patches are applied
to a disposable build tree, then the resulting Node runtime is installed under
~/.local/lib/aki/letta-code/<version> with an atomic `current` symlink.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = ROOT / "vendor" / "letta-code.lock.json"
SUBMODULE = ROOT / "vendor" / "letta-code"
PATCH_DIR = ROOT / "patches" / "letta-code"
BUILD_ROOT = ROOT / "build"
SOURCE_STAGE = BUILD_ROOT / "letta-code"
RUNTIME_STAGE = BUILD_ROOT / "letta-runtime"
TOOLCHAIN_ROOT = Path.home() / ".cache" / "aki" / "toolchain"
INSTALL_ROOT = Path.home() / ".local" / "lib" / "aki" / "letta-code"
CURRENT = INSTALL_ROOT / "current"
PREVIOUS = INSTALL_ROOT / "previous"

BRAND_ASSETS = {
    "assets/letta-code-demo.gif",
    "assets/tutor-profile.png",
}

COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    "node_modules",
    "dist",
    "skills",
    "letta.js",
    "image-resize-worker.js",
    "*.map",
)


class BuildError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_lock() -> dict:
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"cannot read {LOCK_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise BuildError(f"{LOCK_PATH} is not a JSON object")
    return data


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BuildError(
            f"command failed ({result.returncode}): {' '.join(command)}"
            + (f"\n{detail[-2000:]}" if detail else "")
        )
    return result


def verify_submodule(lock: dict) -> None:
    expected = str(lock.get("commit") or "")
    if not expected:
        raise BuildError("lock file is missing commit")
    if not (SUBMODULE / ".git").exists() and not SUBMODULE.is_dir():
        raise BuildError(
            "vendor/letta-code is missing; run `git submodule update --init`"
        )
    actual = run(
        ["git", "-C", str(SUBMODULE), "rev-parse", "HEAD"], capture=True
    ).stdout.strip()
    if actual != expected:
        raise BuildError(
            f"submodule commit mismatch: expected {expected}, got {actual}"
        )
    dirty = run(
        ["git", "-C", str(SUBMODULE), "status", "--porcelain"], capture=True
    ).stdout.strip()
    if dirty:
        raise BuildError("vendor/letta-code is dirty; keep patches in the parent repo")


def patch_hashes() -> dict[str, str]:
    if not PATCH_DIR.is_dir():
        return {}
    return {path.name: sha256_file(path) for path in sorted(PATCH_DIR.glob("*.patch"))}


def verify_patch_lock(lock: dict) -> None:
    expected = lock.get("patches") or {}
    actual = patch_hashes()
    if expected != actual:
        raise BuildError(
            "patch lock mismatch; run scripts/build-letta.py --update-lock after changing patches"
        )


def ensure_bun(lock: dict) -> Path:
    version = str((lock.get("build") or {}).get("bun_version") or "")
    expected = str((lock.get("build") or {}).get("bun_linux_x64_zip_sha256") or "")
    if not version or not expected:
        raise BuildError("lock file is missing Bun version/hash")
    target_dir = TOOLCHAIN_ROOT / "bun" / version
    bun = target_dir / "bun"
    if bun.is_file():
        result = run([str(bun), "--version"], capture=True, check=False)
        if result.returncode == 0 and result.stdout.strip() == version:
            return bun

    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = target_dir / "bun-linux-x64.zip"
    if not archive.is_file() or sha256_file(archive) != expected:
        url = (
            "https://github.com/oven-sh/bun/releases/download/"
            f"bun-v{version}/bun-linux-x64.zip"
        )
        print(f"downloading Bun {version}: {url}")
        with tempfile.NamedTemporaryFile(
            prefix=".bun-", suffix=".zip", dir=target_dir, delete=False
        ) as handle:
            temp_path = Path(handle.name)
            try:
                with urllib.request.urlopen(url, timeout=180) as response:
                    shutil.copyfileobj(response, handle)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
        actual = sha256_file(temp_path)
        if actual != expected:
            temp_path.unlink(missing_ok=True)
            raise BuildError(
                f"Bun archive checksum mismatch: expected {expected}, got {actual}"
            )
        os.replace(temp_path, archive)
        os.chmod(archive, 0o600)

    with zipfile.ZipFile(archive) as bundle:
        member = "bun-linux-x64/bun"
        try:
            payload = bundle.read(member)
        except KeyError as exc:
            raise BuildError(f"Bun archive does not contain {member}") from exc
    temp_bun = target_dir / ".bun.tmp"
    temp_bun.write_bytes(payload)
    os.chmod(temp_bun, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    os.replace(temp_bun, bun)
    result = run([str(bun), "--version"], capture=True)
    if result.stdout.strip() != version:
        raise BuildError(f"unexpected Bun version: {result.stdout.strip()}")
    bunx = target_dir / "bunx"
    bunx.write_text(
        f'#!/bin/sh\nexec "{bun}" x "$@"\n', encoding="utf-8"
    )
    os.chmod(bunx, 0o755)
    return bun


def stage_source() -> None:
    if SOURCE_STAGE.exists():
        shutil.rmtree(SOURCE_STAGE)
    SOURCE_STAGE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(SUBMODULE, SOURCE_STAGE, symlinks=True, ignore=COPY_IGNORE)
    for relative in BRAND_ASSETS:
        path = SOURCE_STAGE / relative
        if path.is_file():
            path.unlink()


def apply_patches() -> None:
    patches = sorted(PATCH_DIR.glob("*.patch")) if PATCH_DIR.is_dir() else []
    for patch in patches:
        print(f"applying {patch.name}")
        run(
            [
                "patch",
                "--batch",
                "--forward",
                "-p1",
                "-d",
                str(SOURCE_STAGE),
                "--dry-run",
                "-i",
                str(patch),
            ]
        )
        run(
            [
                "patch",
                "--batch",
                "--forward",
                "-p1",
                "-d",
                str(SOURCE_STAGE),
                "-i",
                str(patch),
            ]
        )


def build_source(bun: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise BuildError("node executable not found")
    env = dict(os.environ)
    env["PATH"] = f"{bun.parent}:{env.get('PATH', '')}"
    print("installing build dependencies")
    run([str(bun), "install", "--frozen-lockfile"], cwd=SOURCE_STAGE, env=env)
    print("building letta.js")
    run([str(bun), "run", "build"], cwd=SOURCE_STAGE, env=env)
    if not (SOURCE_STAGE / "letta.js").is_file():
        raise BuildError("build did not produce letta.js")
    print("pruning dev dependencies")
    run(
        [str(bun), "install", "--production", "--frozen-lockfile", "--ignore-scripts"],
        cwd=SOURCE_STAGE,
        env=env,
    )
    print("applying Letta node_modules patches")
    run([node, "scripts/postinstall-patches.js"], cwd=SOURCE_STAGE, env=env)
    os.chmod(SOURCE_STAGE / "letta.js", 0o755)


def copy_path(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target)


def assemble_runtime(lock: dict) -> None:
    if RUNTIME_STAGE.exists():
        shutil.rmtree(RUNTIME_STAGE)
    RUNTIME_STAGE.mkdir(parents=True, mode=0o755)
    for name in (
        "letta.js",
        "image-resize-worker.js",
        "dist",
        "skills",
        "vendor",
        "scripts",
        "package.json",
        "bun.lock",
        "npm-shrinkwrap.json",
        "LICENSE",
        "README.md",
        "node_modules",
    ):
        source = SOURCE_STAGE / name
        if source.exists():
            copy_path(source, RUNTIME_STAGE / name)
    manifest = {
        "schema": "aki.letta-runtime.v1",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "version": lock.get("version"),
        "commit": lock.get("commit"),
        "tag": lock.get("tag"),
        "upstream_license": lock.get("upstream_license"),
        "bun_version": (lock.get("build") or {}).get("bun_version"),
        "patches": patch_hashes(),
        "excluded_brand_assets": sorted(BRAND_ASSETS),
    }
    (RUNTIME_STAGE / "aki-runtime-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def install_runtime(lock: dict) -> Path:
    version = str(lock.get("version") or "")
    if not version:
        raise BuildError("lock file is missing version")
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
    target = INSTALL_ROOT / version
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if target.exists():
        backup = INSTALL_ROOT / f"{version}.previous-{stamp}"
        target.rename(backup)
        _set_symlink(PREVIOUS, backup)
    temp_target = INSTALL_ROOT / f".{version}.{os.getpid()}.tmp"
    if temp_target.exists():
        shutil.rmtree(temp_target)
    shutil.copytree(RUNTIME_STAGE, temp_target, symlinks=True)
    manifest_path = temp_target / "aki-runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_sha256"] = tree_hash(temp_target)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_target, target)
    _set_symlink(CURRENT, target)
    return target


def _set_symlink(path: Path, target: Path) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.unlink(missing_ok=True)
    temp.symlink_to(target, target_is_directory=True)
    os.replace(temp, path)


def check_runtime(lock: dict, *, run_binary: bool = True) -> None:
    verify_submodule(lock)
    verify_patch_lock(lock)
    version = str(lock.get("version") or "")
    target = INSTALL_ROOT / version
    bundle = target / "letta.js"
    if not bundle.is_file():
        raise BuildError(f"runtime bundle missing: {bundle}")
    if not CURRENT.is_symlink() or CURRENT.resolve() != target.resolve():
        raise BuildError(f"{CURRENT} does not point to {target}")
    manifest = json.loads((target / "aki-runtime-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("commit") != lock.get("commit"):
        raise BuildError("runtime manifest commit does not match lock")
    if manifest.get("version") != version:
        raise BuildError("runtime manifest version does not match lock")
    if run_binary:
        node = shutil.which("node")
        if node is None:
            raise BuildError("node executable not found")
        result = run([node, str(bundle), "--version"], capture=True, check=False)
        if result.returncode != 0 or version not in (result.stdout or ""):
            detail = (result.stderr or result.stdout or "").strip()
            raise BuildError(f"vendored letta --version failed: {detail[-1000:]}")


def update_lock(lock: dict) -> None:
    lock["patches"] = patch_hashes()
    version = str(lock.get("version") or "")
    target = INSTALL_ROOT / version
    manifest_path = target / "aki-runtime-manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        lock["runtime_sha256"] = manifest.get("runtime_sha256")
    LOCK_PATH.write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify lock, submodule and runtime")
    parser.add_argument("--build", action="store_true", help="stage, patch and build the runtime")
    parser.add_argument("--install", action="store_true", help="install the built runtime")
    parser.add_argument("--clean", action="store_true", help="remove build/")
    parser.add_argument("--update-lock", action="store_true", help="record patch/runtime hashes in the lock")
    args = parser.parse_args(argv)
    if not any((args.check, args.build, args.install, args.clean, args.update_lock)):
        args.build = True
        args.install = True
    if args.clean:
        if BUILD_ROOT.exists():
            shutil.rmtree(BUILD_ROOT)
        print(f"removed {BUILD_ROOT}")
        return 0
    lock = load_lock()
    try:
        verify_submodule(lock)
        if args.build:
            verify_patch_lock(lock)
            bun = ensure_bun(lock)
            stage_source()
            apply_patches()
            build_source(bun)
            assemble_runtime(lock)
            print(f"built runtime at {RUNTIME_STAGE}")
        if args.install:
            target = install_runtime(lock)
            print(f"installed runtime at {target}")
        if args.update_lock:
            update_lock(lock)
            print(f"updated {LOCK_PATH}")
        if args.check:
            check_runtime(lock)
            print("runtime check OK")
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
