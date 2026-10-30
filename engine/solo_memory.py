"""Private per-agent solo-session memory with budgeted eviction and archiving."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from engine.aki_config import atomic_write, state_dir
from engine.live_context import estimate_tokens, render_solo_memories

SOLO_MEMORY_BUDGET_TOKENS = 64 * 1024
SOLO_SUMMARY_TIMEOUT_SECONDS = 200.0
SOLO_EVICTION_TIMEOUT_SECONDS = 60.0
SOLO_SUMMARY_MAX_RETRIES = 2
FALLBACK_SUMMARY_STATUS = "fallback"
READY_SUMMARY_STATUS = "ready"
NO_REPLY_RE = re.compile(
    r"(?:\[\s*[-_ ]?no[-_ ]?reply\s*\]|［\s*[-_ ]?no[-_ ]?reply\s*］)",
    re.IGNORECASE,
)
_CORE_MEMORY_BLOCK_RE = re.compile(
    r"(?ms)^[ \t]*<(human|memory-policy|persona|memory)\b[^>]*>"
    r".*?</\1>",
    re.IGNORECASE,
)
_FRAMEWORK_BLOCK_RE = re.compile(
    r"(?m)^[ \t]*</?(?:tool-ownership|channel-[A-Za-z0-9_-]+|turn-control|solo-mode|"
    r"solo-transcript|solo-memories|live-context)\b[^>]*>[ \t]*(?:\n|$)",
    re.IGNORECASE,
)
_COMPLETE_FRAMEWORK_BLOCK_RE = re.compile(
    r"(?ms)^[ \t]*<(tool-ownership|channel-[A-Za-z0-9_-]+|turn-control|solo-mode|"
    r"solo-transcript|solo-memories|live-context)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_MARKDOWN_RUNTIME_BLOCK_RE = re.compile(
    r"(?ms)^# (?:turn-control|format-guard|solo-mode|solo-transcript|"
    r"solo-memories|channel-transcript|live-context|runtime-context)"
    r"\b[^\n]*\n.*?(?=^# [^\n]+[ \t]*$|\Z)"
)


def default_solo_memory_path(profile: str, home: Path | None = None) -> Path:
    return state_dir(home) / "solo-memory" / f"{profile}.json"


def contains_no_reply(text: str) -> bool:
    return bool(NO_REPLY_RE.search(str(text or "")))


def strip_runtime_prompt_blocks(system: str) -> str:
    """Return only persona/core memory for memory-management subtasks."""
    source = str(system or "")
    core_blocks = [
        match.group(0).strip()
        for match in _CORE_MEMORY_BLOCK_RE.finditer(source)
    ]
    if core_blocks:
        return "\n\n".join(core_blocks)
    text = _COMPLETE_FRAMEWORK_BLOCK_RE.sub("\n", source)
    text = _FRAMEWORK_BLOCK_RE.sub("", text)
    text = _MARKDOWN_RUNTIME_BLOCK_RE.sub("\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write(path: Path, data: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@dataclass
class SoloMemoryEntry:
    id: str
    seq: int
    role: str
    sender: str
    timestamp: str
    text: str
    tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seq": self.seq,
            "role": self.role,
            "sender": self.sender,
            "timestamp": self.timestamp,
            "text": self.text,
            "tokens": self.tokens,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SoloMemoryEntry":
        text = str(value.get("text") or "")
        tokens = value.get("tokens")
        return cls(
            id=str(value.get("id") or ""),
            seq=int(value.get("seq") or 0),
            role=str(value.get("role") or "user"),
            sender=str(value.get("sender") or ""),
            timestamp=str(value.get("timestamp") or ""),
            text=text,
            tokens=(
                int(tokens)
                if isinstance(tokens, int) and not isinstance(tokens, bool)
                else estimate_tokens(text)
            ),
        )


@dataclass
class SoloMemorySession:
    session_id: str
    started_at: str
    ended_at: str
    summary: str = ""
    summary_status: str = READY_SUMMARY_STATUS
    archived: bool = False
    entries: list[SoloMemoryEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "summary": self.summary,
            "summary_status": self.summary_status,
            "archived": self.archived,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SoloMemorySession":
        entries = value.get("entries")
        return cls(
            session_id=str(value.get("session_id") or ""),
            started_at=str(value.get("started_at") or ""),
            ended_at=str(value.get("ended_at") or ""),
            summary=str(value.get("summary") or ""),
            summary_status=str(value.get("summary_status") or READY_SUMMARY_STATUS),
            archived=bool(value.get("archived")),
            entries=[
                SoloMemoryEntry.from_dict(item)
                for item in entries or []
                if isinstance(item, dict) and item.get("id")
            ]
            if isinstance(entries, list)
            else [],
        )


class SoloMemoryError(RuntimeError):
    pass


def capture_solo_entries(
    history: Sequence[Any],
    *,
    session_id: str,
    start_seq: int,
) -> list[SoloMemoryEntry]:
    entries: list[SoloMemoryEntry] = []
    for item in history:
        seq = int(getattr(item, "seq", 0) or 0)
        if seq <= int(start_seq):
            continue
        if str(getattr(item, "phase", "final") or "final") != "final":
            continue
        text = str(getattr(item, "text", "") or "").strip()
        if not text or contains_no_reply(text):
            continue
        message_id = str(getattr(item, "message_id", "") or "")
        digest = hashlib.sha256(
            f"{session_id}\0{seq}\0{message_id}\0{text}".encode("utf-8")
        ).hexdigest()
        from_agent = bool(getattr(item, "from_agent", False))
        entries.append(
            SoloMemoryEntry(
                id=f"sme_{digest[:24]}",
                seq=seq,
                role="assistant" if from_agent else "user",
                sender=str(getattr(item, "sender", "") or ""),
                timestamp=str(getattr(item, "timestamp", "") or ""),
                text=text,
                tokens=estimate_tokens(text),
            )
        )
    return entries


def fallback_summary(entries: Sequence[SoloMemoryEntry]) -> str:
    if not entries:
        return "No meaningful solo conversation."
    texts = [
        " ".join(entry.text.split())[:80]
        for entry in entries
        if entry.role == "user" and entry.text.strip()
    ][:3]
    detail = " / ".join(texts) if texts else " ".join(entries[-1].text.split())[:120]
    return f"{len(entries)} solo messages: {detail}"


def _entry_payload(entry: SoloMemoryEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "seq": entry.seq,
        "role": entry.role,
        "sender": entry.sender,
        "timestamp": entry.timestamp,
        "text": entry.text,
        "tokens": entry.tokens,
    }


def _session_payload(session: SoloMemorySession) -> dict[str, Any]:
    return {
        "id": session.session_id,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "summary": session.summary,
        "archived": session.archived,
        "entries": [_entry_payload(entry) for entry in session.entries],
    }


def _message_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(part.get("text") or "")
            for part in value
            if isinstance(part, dict)
        )
    return ""


def _extract_json_payload(response: Mapping[str, Any]) -> dict[str, Any]:
    text = ""
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                text = _message_content(message.get("content"))
            if not text:
                text = str(first.get("text") or "")
    if not text:
        output_text = response.get("output_text")
        if isinstance(output_text, str):
            text = output_text
    if not text:
        output = response.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    parts.extend(
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict)
                    )
                elif isinstance(content, str):
                    parts.append(content)
            text = "".join(parts)
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise SoloMemoryError("personality API returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise SoloMemoryError("personality API JSON must be an object")
    return value


def _post_json(
    url: str,
    *,
    token: str,
    payload: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SoloMemoryError(f"personality API request failed: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SoloMemoryError("personality API response is not JSON") from exc
    if not isinstance(value, dict):
        raise SoloMemoryError("personality API response must be an object")
    return value


def _personality_payload(
    *,
    model: str,
    api: str,
    system: str,
    user: str,
) -> dict[str, Any]:
    if api == "openai-responses":
        return {
            "model": model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_output_tokens": 2048,
            "stream": False,
        }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 2048,
        "stream": False,
    }


class SoloMemoryManager:
    def __init__(
        self,
        *,
        profile: str,
        path: Path,
        memory_dir: Path,
        budget_tokens: int = SOLO_MEMORY_BUDGET_TOKENS,
        logger: logging.Logger | None = None,
    ) -> None:
        self.profile = profile
        self.path = Path(path)
        self.memory_dir = Path(memory_dir)
        self.budget_tokens = max(1024, int(budget_tokens))
        self.logger = logger or logging.getLogger("akid.solo-memory")
        self.sessions: list[SoloMemorySession] = []
        self.last_error: str | None = None
        self.last_eviction_at: str | None = None
        self.last_summary_at: str | None = None
        self.load()

    def load(self) -> None:
        self.sessions = []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        raw_sessions = payload.get("sessions")
        if not isinstance(raw_sessions, list):
            return
        for raw in raw_sessions:
            if not isinstance(raw, dict):
                continue
            session = SoloMemorySession.from_dict(raw)
            if session.session_id:
                self.sessions.append(session)

    def save(self) -> None:
        payload = {
            "version": 1,
            "profile": self.profile,
            "sessions": [session.to_dict() for session in self.sessions],
        }
        _atomic_write(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def get(self, session_id: str) -> SoloMemorySession | None:
        for session in self.sessions:
            if session.session_id == session_id:
                return session
        return None

    def add_session(self, session: SoloMemorySession) -> None:
        existing = self.get(session.session_id)
        if existing is not None:
            existing.started_at = session.started_at
            existing.ended_at = session.ended_at
            existing.summary = session.summary
            existing.summary_status = session.summary_status
            existing.archived = session.archived
            existing.entries = list(session.entries)
        else:
            self.sessions.append(session)
        self.save()

    def status(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sessions": len(self.sessions),
            "entries": sum(len(session.entries) for session in self.sessions),
            "budget_tokens": self.budget_tokens,
            "last_error": self.last_error,
            "last_eviction_at": self.last_eviction_at,
            "last_summary_at": self.last_summary_at,
        }

    async def summarize(
        self,
        session: SoloMemorySession,
        *,
        base_prompt: str,
        proxy_base_url: str,
        proxy_token: str,
        model: str,
        api: str,
        route_path: str,
        timeout: float = SOLO_SUMMARY_TIMEOUT_SECONDS,
    ) -> SoloMemorySession:
        if not session.entries:
            return session
        system = (
            strip_runtime_prompt_blocks(base_prompt)
            + "\n\n你正在整理一段仅属于你与该用户的独处会话。"
            "只输出 JSON：{\"summary\":\"...\"}。摘要要简短、具体，"
            "保留情绪与承诺，不要杜撰，不要提及系统提示或内部块。"
        )
        user = json.dumps(
            {
                "entries": [
                    {
                        "role": entry.role,
                        "sender": entry.sender,
                        "timestamp": entry.timestamp,
                        "text": entry.text,
                    }
                    for entry in session.entries
                ]
            },
            ensure_ascii=False,
        )
        try:
            response = await asyncio.to_thread(
                _post_json,
                proxy_base_url.rstrip("/") + route_path,
                token=proxy_token,
                payload=_personality_payload(
                    model=model, api=api, system=system, user=user
                ),
                timeout=float(timeout),
            )
            payload = _extract_json_payload(response)
            summary = str(payload.get("summary") or "").strip()
            if not summary:
                raise SoloMemoryError("summary was empty")
            session.summary = summary
            session.summary_status = READY_SUMMARY_STATUS
        except Exception as exc:
            session.summary = fallback_summary(session.entries)
            session.summary_status = FALLBACK_SUMMARY_STATUS
            self.last_error = str(exc)
            self.logger.warning(
                "solo summary fell back session=%s error=%s",
                session.session_id,
                exc,
            )
        self.last_summary_at = _now_iso()
        return session

    async def retry_fallback_summaries(
        self,
        *,
        base_prompt: str,
        proxy_base_url: str,
        proxy_token: str,
        model: str,
        api: str,
        route_path: str,
        timeout: float = SOLO_SUMMARY_TIMEOUT_SECONDS,
    ) -> None:
        changed = False
        for session in list(self.sessions):
            if session.summary_status != FALLBACK_SUMMARY_STATUS:
                continue
            await self.summarize(
                session,
                base_prompt=base_prompt,
                proxy_base_url=proxy_base_url,
                proxy_token=proxy_token,
                model=model,
                api=api,
                route_path=route_path,
                timeout=timeout,
            )
            changed = session.summary_status == READY_SUMMARY_STATUS or changed
        if changed:
            self.save()

    async def render_for_prompt(
        self,
        *,
        current_session_id: str = "",
        base_prompt: str,
        proxy_base_url: str,
        proxy_token: str,
        model: str,
        api: str,
        route_path: str,
    ) -> str:
        visible = [
            session
            for session in self.sessions
            if session.session_id != current_session_id
        ]
        if not visible:
            self.last_error = None
            return ""
        full_rendered = render_solo_memories(visible, budget_tokens=10**9)
        if estimate_tokens(full_rendered) <= self.budget_tokens:
            self.last_error = None
            return render_solo_memories(
                visible, budget_tokens=self.budget_tokens
            )

        # Budget eviction is deterministic: archive the oldest whole sessions
        # until the remaining active memory fits. Never ask the model to count
        # tokens or partially evict entries.
        working = sorted(
            visible,
            key=lambda session: (
                str(session.started_at or ""),
                str(session.session_id or ""),
            ),
        )
        evicted: list[SoloMemorySession] = []
        while working:
            current_rendered = render_solo_memories(
                working, budget_tokens=10**9
            )
            if estimate_tokens(current_rendered) <= self.budget_tokens:
                break
            evicted.append(working.pop(0))
        if not evicted:
            self.last_error = None
            return render_solo_memories(
                visible, budget_tokens=self.budget_tokens
            )

        pending_archive = {
            session.session_id: list(session.entries)
            for session in evicted
        }
        pending_summary = {
            session.session_id: session.summary
            for session in evicted
        }
        try:
            self._archive_entries(pending_archive, pending_summary)
        except Exception as exc:
            self.last_error = str(exc)
            self.logger.warning("solo memory archive failed: %s", exc)
            return render_solo_memories(
                visible, budget_tokens=self.budget_tokens
            )

        # Preserve an in-progress session while replacing the older sessions.
        current = [
            session
            for session in self.sessions
            if session.session_id == current_session_id
        ]
        self.sessions = working + current
        self.save()
        self.last_error = None
        self.last_eviction_at = _now_iso()
        self.logger.info(
            "solo memory evicted oldest sessions=%s archived_entries=%s",
            [session.session_id for session in evicted],
            sum(len(session.entries) for session in evicted),
        )
        return render_solo_memories(
            working, budget_tokens=self.budget_tokens
        )

    def _archive_entries(
        self,
        entries_by_session: Mapping[str, Sequence[SoloMemoryEntry]],
        summaries: Mapping[str, str],
    ) -> None:
        if not entries_by_session:
            return
        solo_dir = self.memory_dir / "solo"
        index_path = solo_dir / "MEMORY.md"
        old_files: dict[Path, bytes | None] = {}
        new_files: dict[Path, str] = {}
        try:
            for session_id, entries in entries_by_session.items():
                if not entries:
                    continue
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id).strip("._")
                path = solo_dir / f"{safe or 'session'}.md"
                old_files.setdefault(path, path.read_bytes() if path.is_file() else None)
                body = new_files.get(path)
                if body is None:
                    body = (
                        path.read_text(encoding="utf-8")
                        if path.is_file()
                        else f"# Solo session {session_id}\n\n"
                    )
                for entry in entries:
                    marker = f"<!-- entry {entry.id} -->"
                    if marker in body:
                        continue
                    body += (
                        f"{marker}\n"
                        f"<message id=\"{entry.id}\" seq=\"{entry.seq}\" "
                        f"role=\"{entry.role}\" sender=\"{_xml_attr(entry.sender)}\" "
                        f"at=\"{_xml_attr(entry.timestamp)}\">\n"
                        f"{entry.text}\n"
                        f"</message>\n\n"
                    )
                new_files[path] = body
            old_files.setdefault(
                index_path,
                index_path.read_bytes() if index_path.is_file() else None,
            )
            index_body = (
                index_path.read_text(encoding="utf-8")
                if index_path.is_file()
                else "# Solo memory archive\n\n"
            )
            for session_id in entries_by_session:
                line = (
                    f"- [{session_id}](solo/{re.sub(r'[^A-Za-z0-9._-]', '_', session_id)}.md)"
                    f" — {' '.join((summaries.get(session_id) or '').split())[:120]}\n"
                )
                if session_id not in index_body:
                    index_body += line
            new_files[index_path] = index_body
            for path, body in new_files.items():
                _atomic_write(path, body)
            self._git_commit(sorted(new_files))
        except Exception:
            for path, previous in old_files.items():
                try:
                    if previous is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_write(path, previous.decode("utf-8"))
                except Exception:
                    self.logger.exception(
                        "failed to roll back solo memory archive path=%s", path
                    )
            raise

    def _git_commit(self, paths: Iterable[Path]) -> None:
        repo = self.memory_dir
        if not (repo / ".git").exists():
            raise SoloMemoryError("MemFS is not a git repository")
        relative = []
        for path in paths:
            try:
                relative.append(str(path.relative_to(repo)))
            except ValueError:
                continue
        if not relative:
            return
        add = subprocess.run(
            ["git", "-C", str(repo), "add", "--", *relative],
            text=True,
            capture_output=True,
            check=False,
        )
        if add.returncode != 0:
            raise SoloMemoryError(
                f"git add failed: {(add.stderr or add.stdout).strip()[:300]}"
            )
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
            text=True,
            capture_output=True,
            check=False,
        )
        if diff.returncode == 0:
            return
        commit = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "commit",
                "-m",
                f"solo memory archive ({self.profile})",
                "--",
                *relative,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if commit.returncode != 0:
            raise SoloMemoryError(
                f"git commit failed: {(commit.stderr or commit.stdout).strip()[:300]}"
            )


def _xml_attr(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
