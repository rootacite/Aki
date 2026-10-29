"""Memory status and MemFS read RPCs."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from engine.protocol import (
    ErrorCode,
    RpcError,
    optional_string,
    require_int,
    require_string,
)
from .constants import MAX_MEMORY_FILE
from .paths import resolve_memory_path

class MemoryMixin:

    # ----------------------------------------------------------------- memory
    async def _run_letta_json_value(
        self,
        args: list[str],
        timeout: float = 20.0,
        *,
        label: str = "letta command",
    ) -> Any:
        command = [self.config.letta_bin, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.config.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, f"{label} failed") from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[:300]
            raise RpcError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"{label} failed",
                {"detail": detail} if detail else None,
            )
        try:
            return json.loads(stdout.decode("utf-8"))
        except ValueError as exc:
            raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, "letta returned invalid JSON") from exc


    async def _run_letta_json(self, args: list[str], timeout: float = 20.0) -> dict[str, Any]:
        data = await self._run_letta_json_value(
            args, timeout=timeout, label="letta memory command"
        )
        if not isinstance(data, dict):
            raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, "letta returned unexpected JSON")
        return data


    async def _memory_status(self) -> dict[str, Any]:
        if self._aki_bridge_available():
            return await self.backend.memory_status(agent_id=self.config.agent_id)
        return await self._run_letta_json(
            ["memory", "status", "--agent", self.config.agent_id]
        )


    async def _memory_tokens(self) -> dict[str, Any]:
        data, error = await self._memory_tokens_cached(allow_error=False)
        if data is None:
            raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, error or "memory tokens unavailable")
        return data


    async def _memory_tokens_cached(
        self, *, allow_error: bool = False
    ) -> tuple[dict[str, Any] | None, str | None]:
        async with self._token_lock:
            created, cached, error = self._token_cache
            if cached is not None and time.monotonic() - created < 30:
                return cached, error
            try:
                if self._aki_bridge_available():
                    data = await self.backend.memory_tokens(
                        agent_id=self.config.agent_id
                    )
                else:
                    data = await self._run_letta_json(
                        [
                            "memory",
                            "tokens",
                            "--agent",
                            self.config.agent_id,
                            "--format",
                            "json",
                        ]
                    )
            except RpcError as exc:
                self._token_cache = (time.monotonic(), None, exc.message)
                if allow_error:
                    return None, exc.message
                raise
            self._token_cache = (time.monotonic(), data, None)
            return data, None


    def _memory_tree(self, params: Mapping[str, Any]) -> dict[str, Any]:
        depth = require_int(params, "depth", default=4, minimum=1, maximum=10)
        root = self._memory_root()
        entries: list[dict[str, Any]] = []

        def walk(directory: Path, prefix: str, remaining: int) -> None:
            if remaining <= 0:
                return
            try:
                children = sorted(directory.iterdir(), key=lambda p: p.name)
            except OSError:
                return
            for child in children:
                if child.name.startswith("."):
                    continue
                try:
                    real = child.resolve(strict=False)
                except OSError:
                    continue
                if real != root and root not in real.parents:
                    continue
                rel = f"{prefix}/{child.name}" if prefix else child.name
                try:
                    st = child.stat()
                except OSError:
                    continue
                is_dir = child.is_dir()
                entries.append(
                    {
                        "path": rel,
                        "type": "dir" if is_dir else "file",
                        "size": None if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
                if is_dir:
                    walk(child, rel, remaining - 1)

        walk(root, "", depth)
        return {"root": ".", "entries": entries}


    def _memory_read(self, params: Mapping[str, Any]) -> dict[str, Any]:
        relative = require_string(params, "path")
        path = resolve_memory_path(self.memory_dir, relative)
        if not path.exists() or not path.is_file():
            raise RpcError(ErrorCode.INVALID_PATH, "file does not exist")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RpcError(ErrorCode.INVALID_PATH, "cannot stat file") from exc
        if size > MAX_MEMORY_FILE:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "file exceeds 1 MiB",
                {"size": size, "max": MAX_MEMORY_FILE},
            )
        try:
            content = path.read_text("utf-8")
        except UnicodeDecodeError as exc:
            raise RpcError(ErrorCode.INVALID_PATH, "file is not valid UTF-8") from exc
        except OSError as exc:
            raise RpcError(ErrorCode.INVALID_PATH, "cannot read file") from exc
        return {"path": relative, "content": content, "size": size}


    def _memory_search(self, params: Mapping[str, Any]) -> dict[str, Any]:
        query = require_string(params, "query")
        relative = optional_string(params, "path") or ""
        limit = require_int(params, "limit", default=50, minimum=1, maximum=200)
        root = self._memory_root()
        start = root if not relative else resolve_memory_path(root, relative)
        if start.is_file():
            files = [start]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
                dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
                for name in sorted(filenames):
                    if name.startswith("."):
                        continue
                    path = Path(dirpath) / name
                    try:
                        real = path.resolve(strict=False)
                    except OSError:
                        continue
                    if real != root and root not in real.parents:
                        continue
                    try:
                        if path.stat().st_size > MAX_MEMORY_FILE:
                            continue
                    except OSError:
                        continue
                    files.append(path)
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        for path in sorted(files):
            try:
                text = path.read_text("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line.casefold():
                    matches.append(
                        {
                            "path": path.relative_to(root).as_posix(),
                            "line": number,
                            "text": line,
                        }
                    )
                    if len(matches) >= limit:
                        return {"query": query, "matches": matches, "truncated": True}
        return {"query": query, "matches": matches, "truncated": False}


    def _memory_root(self) -> Path:
        try:
            return self.memory_dir.resolve(strict=True)
        except OSError as exc:
            raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, "memory root is unavailable") from exc
