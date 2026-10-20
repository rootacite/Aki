"""Persistent writable-directory registry and bwrap command construction."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from engine.protocol import ErrorCode, RpcError

STATE_VERSION = 1
FORBIDDEN_ROOTS = {Path("/"), Path("/proc"), Path("/sys"), Path("/dev")}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_state_path(home: Path | None = None) -> Path:
    configured = os.environ.get("AKI_WRITABLE_STATE")
    if configured:
        return Path(configured).expanduser()
    return (home or Path.home()) / ".local" / "share" / "aki" / "writable-dirs.json"


def default_letta_home(home: Path | None = None) -> Path:
    configured = os.environ.get("LETTA_HOME")
    if configured:
        return Path(configured).expanduser()
    return (home or Path.home()) / ".letta"


def default_backend_dir(home: Path | None = None) -> Path:
    configured = os.environ.get("LETTA_LOCAL_BACKEND_DIR")
    if configured:
        return Path(configured).expanduser()
    return (home or Path.home()) / ".letta" / "lc-local-backend"


def probe_bwrap(bwrap_bin: str = "bwrap", timeout: float = 5.0) -> tuple[bool, str | None, str | None]:
    path = shutil.which(bwrap_bin) if not os.path.isabs(bwrap_bin) else bwrap_bin
    if not path or not os.path.exists(path):
        return False, None, "bubblewrap (bwrap) not found"
    checks = [
        [path, "--version"],
        [path, "--ro-bind", "/", "/", "--unshare-user", "/bin/true"],
    ]
    for command in checks:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, path, f"bwrap probe failed: {exc}"
        if result.returncode != 0:
            detail = (result.stderr or "").strip()[:300]
            return False, path, detail or "bwrap user namespace probe failed"
    return True, path, None


def _canonical(path: Path) -> Path:
    return path.resolve(strict=True)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


@dataclass(frozen=True)
class RegisteredRoot:
    path: str
    added_at: str


class WritableRegistry:
    def __init__(
        self,
        *,
        state_path: Path | None = None,
        home: Path | None = None,
        letta_home: Path | None = None,
        backend_dir: Path | None = None,
        bwrap_bin: str = "bwrap",
    ) -> None:
        self.home = (home or Path.home()).expanduser()
        self.state_path = (state_path or default_state_path(self.home)).expanduser()
        self.letta_home = (letta_home or default_letta_home(self.home)).expanduser()
        self.backend_dir = (backend_dir or default_backend_dir(self.home)).expanduser()
        self.bwrap_bin = bwrap_bin
        self.defaults = tuple(
            dict.fromkeys(
                [
                    Path("/tmp").resolve(strict=False),
                    (self.home / ".local" / "share" / "tmp").resolve(strict=False),
                ]
            )
        )
        internal = [self.letta_home.resolve(strict=False), self.backend_dir.resolve(strict=False)]
        self.internal = tuple(dict.fromkeys(internal))
        self.revision = 0
        self._roots: list[RegisteredRoot] = []
        self._invalid: list[dict[str, str]] = []
        self._valid_paths: set[Path] = set()
        self.last_error: str | None = None
        self._probe_result: tuple[bool, str | None, str | None] | None = None

    @property
    def runtime_root(self) -> Path:
        return self.home / ".local" / "share" / "tmp" / "akid"

    def load(self) -> None:
        self._roots = []
        self._invalid = []
        self.revision = 0
        if not self.state_path.exists():
            self._refresh_invalid()
            return
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            self.last_error = f"cannot read writable registry: {exc}"
            return
        if not isinstance(data, dict):
            self.last_error = "writable registry is not a JSON object"
            return
        revision = data.get("revision", 0)
        self.revision = revision if isinstance(revision, int) and revision >= 0 else 0
        roots = data.get("roots", [])
        if isinstance(roots, list):
            for item in roots:
                if isinstance(item, str):
                    self._roots.append(RegisteredRoot(path=item, added_at=""))
                elif isinstance(item, dict) and isinstance(item.get("path"), str):
                    self._roots.append(
                        RegisteredRoot(
                            path=item["path"],
                            added_at=str(item.get("added_at") or ""),
                        )
                    )
        self._refresh_invalid()

    def ensure_runtime_dirs(self) -> None:
        (self.home / ".local" / "share" / "tmp").mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_path.parent, 0o700)
        for child in ("cache/xdg", "cache/npm", "cache/pip", "cache/uv", "runtime", "tmp"):
            path = self.runtime_root / child
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)

    def _refresh_invalid(self) -> None:
        self._invalid = []
        self._valid_paths = set()
        for entry in self._roots:
            try:
                canonical = self._validate(entry.path)
            except RpcError as exc:
                self._invalid.append({"path": entry.path, "error": exc.message})
                continue
            self._valid_paths.add(canonical)

    def _validate(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise RpcError(ErrorCode.INVALID_PATH, "path must be a non-empty string")
        if "\x00" in raw_path:
            raise RpcError(ErrorCode.INVALID_PATH, "path contains a NUL byte")
        expanded = Path(raw_path).expanduser()
        if not expanded.is_absolute():
            raise RpcError(ErrorCode.INVALID_PATH, "path must be absolute")
        try:
            path = _canonical(expanded)
        except OSError as exc:
            raise RpcError(ErrorCode.INVALID_PATH, "path does not exist") from exc
        if not path.is_dir():
            raise RpcError(ErrorCode.INVALID_PATH, "path must be a directory")
        if path in FORBIDDEN_ROOTS:
            raise RpcError(ErrorCode.INVALID_PATH, f"refusing to register {path}")
        return path

    def _match_key(self, raw_path: str) -> Path:
        expanded = Path(raw_path).expanduser()
        if not expanded.is_absolute():
            raise RpcError(ErrorCode.INVALID_PATH, "path must be absolute")
        return expanded.resolve(strict=False)

    def _is_default_or_internal(self, path: Path) -> Path | None:
        for root in (*self.defaults, *self.internal):
            if _is_within(path, root):
                return root
        return None

    def add(self, raw_path: str) -> dict[str, Any]:
        self._refresh_invalid()
        path = self._validate(raw_path)
        existing = {Path(entry.path) for entry in self._roots}
        if path in existing:
            raise RpcError(
                ErrorCode.PATH_ALREADY_REGISTERED,
                "directory is already registered",
                {"path": str(path)},
            )
        covered_by = self._is_default_or_internal(path)
        if covered_by is not None:
            return {
                "path": str(path),
                "canonical_path": str(path),
                "added": False,
                "already_writable": True,
                "covered_by": str(covered_by),
                "revision": self.revision,
            }
        self.ensure_runtime_dirs()
        self._roots.append(RegisteredRoot(path=str(path), added_at=_iso_now()))
        self._roots.sort(key=lambda item: item.path)
        self.revision += 1
        self.save()
        return {
            "path": str(path),
            "canonical_path": str(path),
            "added": True,
            "already_writable": False,
            "covered_by": None,
            "revision": self.revision,
        }

    def remove(self, raw_path: str) -> dict[str, Any]:
        self._refresh_invalid()
        key = self._match_key(raw_path)
        for root in (*self.defaults, *self.internal):
            if key == root.resolve(strict=False):
                raise RpcError(
                    ErrorCode.PATH_IS_DEFAULT,
                    "default or internal directories cannot be removed",
                    {"path": str(key), "root": str(root)},
                )
        for index, entry in enumerate(self._roots):
            entry_path = Path(entry.path)
            if key == entry_path or key == entry_path.resolve(strict=False):
                removed = self._roots.pop(index)
                self.revision += 1
                self.save()
                return {
                    "path": removed.path,
                    "canonical_path": removed.path,
                    "removed": True,
                    "revision": self.revision,
                }
        for root in (*self.defaults, *self.internal):
            if _is_within(key, root):
                raise RpcError(
                    ErrorCode.PATH_IS_DEFAULT,
                    "path is writable because it is covered by a default/internal directory",
                    {"path": str(key), "root": str(root)},
                )
        covering = [
            entry.path
            for entry in self._roots
            if _is_within(key, Path(entry.path).resolve(strict=False))
        ]
        raise RpcError(
            ErrorCode.PATH_NOT_REGISTERED,
            "directory is not explicitly registered",
            {"path": str(key), "covered_by": covering},
        )

    def clear(self) -> dict[str, Any]:
        removed = len(self._roots)
        self._roots = []
        if removed:
            self.revision += 1
            self.save()
        return {"removed_count": removed, "revision": self.revision}

    def save(self) -> None:
        self.ensure_runtime_dirs()
        payload = {
            "version": STATE_VERSION,
            "revision": self.revision,
            "roots": [
                {"path": entry.path, "added_at": entry.added_at} for entry in self._roots
            ],
        }
        fd, temp_name = tempfile.mkstemp(
            prefix=".writable-dirs.", dir=str(self.state_path.parent), text=True
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.state_path)
            os.chmod(self.state_path, 0o600)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def effective_roots(self) -> list[Path]:
        roots = [*self.defaults, *self.internal, *sorted(self._valid_paths)]
        return list(dict.fromkeys(roots))

    def external_source_roots(self) -> list[Path]:
        self._refresh_invalid()
        return list(dict.fromkeys([*self.defaults, *sorted(self._valid_paths)]))

    def invalid_entries(self) -> list[dict[str, str]]:
        return list(self._invalid)

    def minimal_roots(self) -> list[Path]:
        self._refresh_invalid()
        roots = sorted(self.effective_roots(), key=lambda path: len(path.parts))
        minimal: list[Path] = []
        for root in roots:
            try:
                resolved = root.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError(f"sandbox root is unavailable: {root}") from exc
            if not resolved.is_dir():
                raise RuntimeError(f"sandbox root is not a directory: {root}")
            if any(_is_within(resolved, existing) for existing in minimal):
                continue
            minimal.append(resolved)
        return minimal

    def status(self) -> dict[str, Any]:
        self._refresh_invalid()
        available, bwrap_path, error = self.probe()
        return {
            "enabled": True,
            "available": available,
            "backend": "bwrap",
            "bwrap_path": bwrap_path,
            "last_error": self.last_error or error,
            "revision": self.revision,
            "defaults": [str(path) for path in self.defaults],
            "internal": [str(path) for path in self.internal],
            "registered": [
                {"path": entry.path, "added_at": entry.added_at} for entry in self._roots
            ],
            "effective": [str(path) for path in self.effective_roots()],
            "invalid": list(self._invalid),
            "state_path": str(self.state_path),
        }

    def probe(self, *, refresh: bool = False) -> tuple[bool, str | None, str | None]:
        if self._probe_result is None or refresh:
            self._probe_result = probe_bwrap(self.bwrap_bin)
        return self._probe_result


def sandbox_environment(
    base_env: Mapping[str, str], registry: WritableRegistry
) -> dict[str, str]:
    registry.ensure_runtime_dirs()
    env = dict(base_env)
    cache = registry.runtime_root / "cache"
    env.update(
        {
            "XDG_CACHE_HOME": str(cache / "xdg"),
            "npm_config_cache": str(cache / "npm"),
            "NPM_CONFIG_CACHE": str(cache / "npm"),
            "PIP_CACHE_DIR": str(cache / "pip"),
            "UV_CACHE_DIR": str(cache / "uv"),
            "XDG_RUNTIME_DIR": str(registry.runtime_root / "runtime"),
            "TMPDIR": str(registry.runtime_root / "tmp"),
            "AKI_SANDBOXED": "1",
        }
    )
    return env


def build_bwrap_command(
    command: Sequence[str],
    registry: WritableRegistry,
    *,
    bwrap_path: str,
) -> list[str]:
    invalid = registry.invalid_entries()
    if invalid:
        detail = ", ".join(item["path"] for item in invalid)
        raise RuntimeError(f"registered sandbox root is unavailable: {detail}")
    args = [bwrap_path, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for root in registry.minimal_roots():
        args.extend(["--bind", str(root), str(root)])
    args.extend(["--die-with-parent", "--", *command])
    return args


