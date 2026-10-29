"""Read and change the name used for the human in the channel."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from engine.aki_config import set_key
from engine.protocol import (
    ErrorCode,
    RpcError,
    require_string,
)
from .constants import LOGGER

class UsernameMixin:

    # ---------------------------------------------------------------- username
    def _userdata_dir(self) -> Path:
        return self.config.userdata_dir or (self.config.cwd / "userdata")


    def _profile_config_path(self) -> Path:
        if self.config.profile_config_path is not None:
            return self.config.profile_config_path
        if self.config.aki is not None:
            return self.config.aki.path
        return Path.home() / ".aki" / f"{self.config.profile}.conf"


    def _read_username(self) -> str:
        return self.config.username.strip() or "Master"


    def _username_get(self) -> dict[str, Any]:
        config_path = self._profile_config_path()
        source = self.config.username_source or "default"
        source_path = (
            str(self.config.username_path)
            if self.config.username_path is not None
            and source.startswith("legacy-")
            else str(config_path)
        )
        return {
            "username": self._read_username(),
            "path": str(config_path),
            "key": "gateway.username",
            "source": source,
            "source_path": source_path,
            "agent_id": self.config.agent_id,
            "profile": self.config.profile,
        }


    async def _username_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        username = require_string(params, "username").strip()
        if not username:
            raise RpcError(ErrorCode.INVALID_PARAMS, "username must not be empty")
        if len(username) > 64:
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "username must be at most 64 characters"
            )
        if any(ord(char) < 32 or ord(char) == 127 for char in username):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "username contains control characters"
            )

        async with self._username_lock:
            if (
                self._recompiling
                or self._compacting
                or self._resetting
                or self._policy_restarting
                or self.active is not None
                or self.queue
            ):
                raise RpcError(
                    ErrorCode.RECOMPILE_FAILED,
                    "cannot change username while a turn or maintenance operation is active",
                )
            userdata = self._userdata_dir()
            script = self.config.cwd / "scripts" / "sync-persona.py"
            if not userdata.is_dir():
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "userdata directory not found",
                    {"path": str(userdata)},
                )
            if not script.is_file():
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "persona sync script not found",
                    {"path": str(script)},
                )
            backup_dir = self.config.backup_dir or (Path.home() / ".letta" / "backups")
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script),
                "--json",
                "--source",
                str(userdata),
                "--memory-dir",
                str(self.memory_dir),
                "--backup-dir",
                str(backup_dir),
                "--username",
                username,
                "--message",
                f"username: set to {username}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                detail = stderr.decode("utf-8", "replace").strip()[-500:]
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "username sync failed",
                    {"detail": detail} if detail else None,
                )
            text = stdout.decode("utf-8", "replace").strip()
            try:
                result = json.loads(text)
            except ValueError as exc:
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "username sync returned invalid JSON",
                    {"stdout": text[-500:]},
                ) from exc
            if not isinstance(result, dict):
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "username sync returned an unexpected result",
                )
            config_path = self._profile_config_path()
            try:
                set_key(config_path, "gateway", "username", username)
            except OSError as exc:
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "persona rendered but profile config could not be updated",
                    {"path": str(config_path), "detail": str(exc)},
                ) from exc
            self.config.username = username
            self.config.username_source = "profile-config"
            self.config.username_path = None
            if self.config.aki is not None:
                self.config.aki.username = username
            result["path"] = str(config_path)
            result["key"] = "gateway.username"
            result["source"] = "profile-config"
            result["agent_id"] = self.config.agent_id
            result["profile"] = self.config.profile
            LOGGER.info(
                "username updated value=%s changed=%s commit=%s path=%s key=gateway.username",
                result.get("username"),
                result.get("changed"),
                result.get("commit"),
                config_path,
            )
            try:
                recompiled = await self._recompile_conversation()
            except RpcError as exc:
                raise RpcError(
                    ErrorCode.RECOMPILE_FAILED,
                    "username saved but prompt recompile failed; run 'akictl recompile' to retry",
                    {
                        "username": username,
                        "detail": exc.message,
                        **(exc.data or {}),
                    },
                ) from exc
            result.update(recompiled)
            result["applies"] = "now"
            return result
