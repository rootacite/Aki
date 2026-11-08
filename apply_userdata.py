#!/usr/bin/env python3
"""Detect and sync persona data between project userdata/ and local MemFS deployment."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.aki_config import (
    ConfigError,
    discover_profiles,
    load_config,
    profile_config_path,
)

SYNC_PERSONA = ROOT / "scripts" / "sync-persona.py"
AKICTL = ROOT / "bin" / "akictl"
MANIFEST_NAME = "MANIFEST.json"

# Strict allowlist of persona/character files in userdata.
# Memory-related files (e.g. human.md, profile/**, chat history, transcripts)
# are NEVER touched or synced.
PERSONA_ALLOWLIST = (
    "persona.md",
    "character/voice.md",
    "character/relationship.md",
    "character/boundaries.md",
    "character/examples.md",
    "character/MEMORY.md",
    "memory-policy.md",
)

PROFILE_FLAGS = {
    "aki": "userdata",
    "fuyu": "userdata2",
    "haru": "userdata3",
    "natsu": "userdata4",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run(command: list[object], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in command],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _gateway_active(profile: str) -> bool:
    res = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", f"akid@{profile}.service"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10.0,
    )
    return res.returncode == 0


def _recompile(profile: str) -> dict:
    if not AKICTL.is_file():
        raise RuntimeError(f"akictl executable not found: {AKICTL}")
    res = _run([sys.executable, str(AKICTL), profile, "recompile"], timeout=60.0)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(f"akictl {profile} recompile failed: {detail[-2000:]}")
    try:
        data = json.loads(res.stdout)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _update_manifest(source: Path, username: str, commit: str) -> bool:
    manifest_path = source / MANIFEST_NAME
    if not manifest_path.is_file():
        return False
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read {manifest_path}: {exc}") from exc
    if not isinstance(data, dict):
        return False

    templates: dict[str, dict] = {}
    rendered: dict[str, dict] = {}
    for rel in PERSONA_ALLOWLIST:
        src_file = source / rel
        if not src_file.is_file():
            continue
        raw = src_file.read_bytes()
        rendered_bytes = raw.decode("utf-8").replace("{{USERNAME}}", username).encode("utf-8")
        templates[rel] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
        rendered[rel] = {
            "sha256": hashlib.sha256(rendered_bytes).hexdigest(),
            "bytes": len(rendered_bytes),
        }

    data.update({
        "memfs_commit": commit,
        "username": username,
        "synced_at": _now_iso(),
        "templates": templates,
        "rendered": rendered,
    })
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def inspect_profile(profile: str) -> dict:
    """Inspect and compare the persona files of a given profile."""
    discovered = discover_profiles()
    config_path = discovered.get(profile) or profile_config_path(profile)
    if not config_path.is_file():
        raise RuntimeError(f"Profile config not found: {config_path}")

    config = load_config(config_path, profile=profile)
    source_dir = ROOT / (config.userdata or PROFILE_FLAGS.get(profile, "userdata"))
    if not source_dir.is_dir():
        raise RuntimeError(f"Source userdata directory not found: {source_dir}")

    backend_dir = Path(os.environ.get("AKI_LETTA_BACKEND_DIR") or Path.home() / ".letta" / "lc-local-backend").expanduser()
    memory_dir = backend_dir / "memfs" / config.agent_id / "memory"
    if not memory_dir.is_dir():
        raise RuntimeError(f"MemFS memory directory not found: {memory_dir}")

    username = config.username or "Master"

    diffs: dict[str, dict] = {}
    for rel in PERSONA_ALLOWLIST:
        src_file = source_dir / rel
        if not src_file.is_file():
            continue

        raw = src_file.read_bytes()
        try:
            rendered_text = raw.decode("utf-8").replace("{{USERNAME}}", username)
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"{src_file}: invalid UTF-8") from exc

        target_file = memory_dir / rel
        if not target_file.is_file():
            target_text = ""
            is_different = True
        else:
            try:
                target_text = target_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                target_text = ""
            is_different = (rendered_text != target_text)

        if is_different:
            unified = list(difflib.unified_diff(
                target_text.splitlines(keepends=True),
                rendered_text.splitlines(keepends=True),
                fromfile=f"local:{rel}",
                tofile=f"project:{rel}",
            ))
            diffs[rel] = {
                "source_path": str(src_file),
                "target_path": str(target_file),
                "diff": "".join(unified),
                "rendered_bytes": rendered_text.encode("utf-8"),
            }

    return {
        "profile": profile,
        "config": config,
        "source_dir": source_dir,
        "memory_dir": memory_dir,
        "username": username,
        "diffs": diffs,
        "in_sync": len(diffs) == 0,
    }


def sync_and_deploy_profile(info: dict) -> dict:
    """Sync persona files into MemFS, commit git, update manifest, and recompile gateway."""
    profile = info["profile"]
    config = info["config"]
    source_dir = info["source_dir"]
    memory_dir = info["memory_dir"]
    username = info["username"]

    # 1. Run sync-persona.py to validate, backup, write, and commit into MemFS
    cmd = [
        sys.executable,
        str(SYNC_PERSONA),
        "--source",
        str(source_dir),
        "--agent-id",
        config.agent_id,
        "--username",
        username,
        "--json",
    ]
    res = _run(cmd)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(f"sync-persona failed: {detail[-2000:]}")

    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"sync-persona returned invalid JSON: {exc}") from exc

    commit = payload.get("commit")
    if not commit or commit in {"not committed", "no changes to commit"}:
        raise RuntimeError("sync-persona did not produce a git commit in MemFS")

    # 2. Get full git commit hash
    commit_res = _run(["git", "-C", str(memory_dir), "rev-parse", "HEAD"])
    full_commit = commit_res.stdout.strip() if commit_res.returncode == 0 else commit

    # 3. Update MANIFEST.json in project userdata
    manifest_updated = _update_manifest(source_dir, username, full_commit)

    # 4. Recompile live conversation prompt if gateway service is running
    recompiled = False
    recompile_payload = {}
    if _gateway_active(profile):
        recompile_payload = _recompile(profile)
        recompiled = bool(recompile_payload.get("recompiled", False))

    return {
        "profile": profile,
        "commit": full_commit,
        "backup": payload.get("backup"),
        "files": payload.get("files", []),
        "manifest_updated": manifest_updated,
        "gateway_active": _gateway_active(profile),
        "recompiled": recompiled,
        "recompile_payload": recompile_payload,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apply_userdata.py",
        description="检测项目目录 userdatax/ 中的人格数据并同步部署至本机 MemFS（仅同步人格，绝不同步记忆）。",
    )
    parser.add_argument("--aki", action="store_true", help="处理 Aki 人格数据 (userdata/)")
    parser.add_argument("--fuyu", action="store_true", help="处理 Fuyu 人格数据 (userdata2/)")
    parser.add_argument("--haru", action="store_true", help="处理 Haru 人格数据 (userdata3/)")
    parser.add_argument("--natsu", action="store_true", help="处理 Natsu 人格数据 (userdata4/)")
    parser.add_argument("--all", action="store_true", help="处理所有智能体的人格数据")
    parser.add_argument("-y", "--yes", action="store_true", help="检测到差异时不提示确认，直接同步并部署")
    parser.add_argument("--diff", action="store_true", help="显示详细的统一差异内容 (unified diff)")
    args = parser.parse_args(argv)

    selected: list[str] = []
    if args.aki:
        selected.append("aki")
    if args.fuyu:
        selected.append("fuyu")
    if args.haru:
        selected.append("haru")
    if args.natsu:
        selected.append("natsu")

    if args.all or not selected:
        selected = ["aki", "fuyu", "haru", "natsu"]

    print(f"=== Aki 人格同步部署检测 [目标: {', '.join(selected)}] ===")
    print("注意: 本工具只同步人格数据 (persona.md, character/*)，绝不同步或修改任何记忆档案 (human.md, profile/ 等)。\n")

    has_diffs = False
    pending_syncs: list[dict] = []

    for profile in selected:
        try:
            info = inspect_profile(profile)
        except Exception as exc:
            print(f"[{profile}] 检查失败: {exc}")
            continue

        source_name = info["source_dir"].name
        if info["in_sync"]:
            print(f"[{profile}] 本机人格与项目目录 ({source_name}/) 完全一致，无需同步。")
        else:
            has_diffs = True
            diff_files = list(info["diffs"].keys())
            print(f"[{profile}] ⚠️ 检测到人格数据不一致！涉及 {len(diff_files)} 个文件:")
            for f in diff_files:
                print(f"    - {f}")
            if args.diff:
                print("\n--- 差异详情 (Diff) ---")
                for f, d in info["diffs"].items():
                    print(d["diff"])
                print("-----------------------\n")
            pending_syncs.append(info)

    if not has_diffs:
        print("\n所有指定智能体的人格数据均已与本机部署一致。")
        return 0

    print("")
    # Prompt for confirmation if not --yes
    for info in pending_syncs:
        profile = info["profile"]
        source_name = info["source_dir"].name
        files_str = ", ".join(info["diffs"].keys())

        confirmed = args.yes
        if not confirmed:
            try:
                prompt_msg = f"是否将 [{profile}] 的项目人格 ({source_name}/: {files_str}) 同步并部署至本机？[y/N]: "
                choice = input(prompt_msg).strip().lower()
                confirmed = choice in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                print(f"\n[{profile}] 操作已取消。")
                continue

        if not confirmed:
            print(f"[{profile}] 用户取消同步。")
            continue

        print(f"[{profile}] 正在同步并部署最新 Persona 到本机...")
        try:
            result = sync_and_deploy_profile(info)
            print(f"[{profile}] ✅ 人格文件已同步至 MemFS，Git 提交: {result['commit'][:10]}")
            if result["manifest_updated"]:
                print(f"[{profile}] ✅ MANIFEST.json 已更新")
            if result["backup"]:
                print(f"[{profile}] 📁 旧版本已备份至: {result['backup']}")

            if result["gateway_active"]:
                if result["recompiled"]:
                    rev = result["recompile_payload"].get("memfs_revision", "")
                    print(f"[{profile}] 🚀 运行中的 akid 已经成功完成热重编译，当前已生效最新 Persona (revision: {rev})。")
                else:
                    print(f"[{profile}] ⚠️ 运行中的服务未能自动热重编译，请执行 `akictl {profile} recompile`。")
            else:
                print(f"[{profile}] ℹ️ 服务当前未运行，MemFS 已完成部署，下次启动时将直接加载最新 Persona。")
        except Exception as exc:
            print(f"[{profile}] ❌ 同步或部署失败: {exc}")
            return 1

    print("\n部署流程结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
