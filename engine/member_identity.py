"""Client-local immutable member identities and label ownership.

Identities are deliberately not issued or verified by a central service.  Each
client persists the IDs it owns (``identity.json`` / Tokifactor settings) and
keeps a local label registry so renames can preserve old aliases.  The shared
``~/.aki/identities.json`` file is only a local cache/claim table for the Aki and
Fuyu processes running for the same user.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from engine.member_profile import (
    MEMBER_KIND_AGENT,
    canonical_name,
    normalize_member_kind,
)

LEGACY_ID_NAMESPACE = b"tokifactor-member-v1\x00"
MEMBER_ID_PATTERN = re.compile(r"^(?:mem_[0-9a-f]{32}|legacy_[0-9a-f]{32})$")
MANIFEST_FILENAME = "identity.json"
REGISTRY_FILENAME = "identities.json"
REGISTRY_VERSION = 1
MAX_NAME_CHARS = 80
MAX_ALIASES = 32
MAX_CONFLICTS = 100


class IdentityError(ValueError):
    """Invalid identity data or an unresolvable identity conflict."""


class IdentityConflict(IdentityError):
    """A label is already permanently owned by another member id."""


def legacy_member_id(name: object) -> str:
    """Return the deterministic id shared by all clients migrating ``name``."""
    digest = hashlib.sha256(
        LEGACY_ID_NAMESPACE + canonical_name(name).encode("utf-8")
    ).hexdigest()
    return f"legacy_{digest[:32]}"


def new_member_id() -> str:
    return f"mem_{uuid.uuid4().hex}"


def is_member_id(value: object) -> bool:
    return bool(MEMBER_ID_PATTERN.fullmatch(str(value or "").strip()))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_name(value: object, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        text = fallback.strip()
    if not text:
        raise IdentityError("member name must not be empty")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise IdentityError("member name contains control characters")
    if len(text) > MAX_NAME_CHARS:
        raise IdentityError(f"member name must be at most {MAX_NAME_CHARS} characters")
    return text


def clean_aliases(values: Iterable[object], *, name: str = "") -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in (name, *values):
        label = str(value or "").strip()
        key = canonical_name(label)
        if not label or not key or key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= MAX_ALIASES:
            break
    return tuple(out)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


@dataclass
class MemberIdentity:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    kind: str = MEMBER_KIND_AGENT
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        self.id = str(self.id or "").strip()
        if not is_member_id(self.id):
            raise IdentityError(f"invalid member id: {self.id!r}")
        self.name = clean_name(self.name)
        self.aliases = clean_aliases(self.aliases, name=self.name)
        self.kind = normalize_member_kind(self.kind)
        if not self.created_at:
            self.created_at = now_iso()

    @property
    def labels(self) -> tuple[str, ...]:
        return clean_aliases(self.aliases, name=self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "aliases": list(self.aliases),
            "kind": self.kind,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MemberIdentity":
        aliases = value.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [item.strip() for item in aliases.split(",")]
        return cls(
            id=str(value.get("id") or ""),
            name=str(value.get("name") or ""),
            aliases=tuple(str(item) for item in aliases if str(item).strip()),
            kind=str(value.get("kind") or MEMBER_KIND_AGENT),
            created_at=str(value.get("created_at") or now_iso()),
        )

    def renamed(self, name: object) -> "MemberIdentity":
        target = clean_name(name)
        aliases = list(self.aliases)
        if self.name and canonical_name(self.name) != canonical_name(target):
            aliases.append(self.name)
        aliases.append(target)
        return MemberIdentity(
            id=self.id,
            name=target,
            aliases=clean_aliases(aliases),
            kind=self.kind,
            created_at=self.created_at,
        )


class IdentityRegistry:
    """A client-local JSON label table guarded by an advisory file lock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "version": REGISTRY_VERSION,
                "members": {},
                "labels": {},
                "sources": {},
                "conflicts": [],
            }
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {
                "version": REGISTRY_VERSION,
                "members": {},
                "labels": {},
                "sources": {},
                "conflicts": [],
            }
        if not isinstance(data, dict):
            return {
                "version": REGISTRY_VERSION,
                "members": {},
                "labels": {},
                "sources": {},
                "conflicts": [],
            }
        members = data.get("members")
        labels = data.get("labels")
        sources = data.get("sources")
        conflicts = data.get("conflicts")
        if not isinstance(members, dict):
            members = {}
        if not isinstance(labels, dict):
            labels = {}
        if not isinstance(sources, dict):
            sources = {}
        if not isinstance(conflicts, list):
            conflicts = []
        return {
            "version": REGISTRY_VERSION,
            "members": members,
            "labels": labels,
            "sources": sources,
            "conflicts": conflicts,
        }

    @contextlib.contextmanager
    def _locked(self, *, write: bool = False):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        mode = "a+" if write else "a+"
        with self.lock_path.open(mode, encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write_unlocked(self, data: Mapping[str, Any]) -> None:
        _atomic_write(
            self.path,
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    @staticmethod
    def _rebuild_labels(members: Mapping[str, Any]) -> dict[str, str]:
        labels: dict[str, str] = {}
        for member_id, raw in members.items():
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            aliases = raw.get("aliases") or []
            if isinstance(aliases, str):
                aliases = aliases.split(",")
            for label in (name, *aliases):
                key = canonical_name(label)
                if key and key not in labels:
                    labels[key] = str(member_id)
        return labels

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            return self._read_unlocked()

    def identities(self) -> dict[str, MemberIdentity]:
        data = self.snapshot()
        out: dict[str, MemberIdentity] = {}
        for member_id, raw in (data.get("members") or {}).items():
            if not isinstance(raw, dict):
                continue
            try:
                identity = MemberIdentity.from_dict(raw)
            except IdentityError:
                continue
            out[member_id] = identity
        return out

    def get(self, member_id: str) -> MemberIdentity | None:
        return self.identities().get(member_id)

    def resolve(self, label: object) -> str:
        key = canonical_name(label)
        if not key:
            return ""
        data = self.snapshot()
        owner = (data.get("labels") or {}).get(key)
        if isinstance(owner, str) and owner in (data.get("members") or {}):
            return owner
        # Rebuild if an older or hand-edited registry lacks the index.
        rebuilt = self._rebuild_labels(data.get("members") or {})
        return rebuilt.get(key, "")

    def owner_name(self, label: object) -> str:
        owner = self.resolve(label)
        identity = self.get(owner) if owner else None
        return identity.name if identity is not None else ""

    def _register_data(
        self,
        data: dict[str, Any],
        identity: MemberIdentity,
        *,
        strict: bool,
        source: str,
        claim: bool,
    ) -> tuple[bool, list[dict[str, str]]]:
        members = data["members"]
        current_raw = members.get(identity.id)
        if isinstance(current_raw, dict):
            try:
                current = MemberIdentity.from_dict(current_raw)
            except IdentityError:
                current = None
        else:
            current = None
        if current is not None:
            merged_aliases = clean_aliases(
                (*current.aliases, *identity.aliases), name=identity.name
            )
            identity = MemberIdentity(
                id=identity.id,
                name=identity.name,
                aliases=merged_aliases,
                kind=identity.kind,
                created_at=current.created_at,
            )
        labels = dict(data.get("labels") or {})
        rebuilt = self._rebuild_labels(members)
        labels.update({key: value for key, value in rebuilt.items() if key not in labels})
        conflicts: list[dict[str, str]] = []
        claimed = True
        for label in identity.labels:
            key = canonical_name(label)
            if not key:
                continue
            owner = labels.get(key)
            if owner and owner != identity.id:
                claimed = False
                conflict = {
                    "label": label,
                    "owner": owner,
                    "member_id": identity.id,
                    "source": source,
                    "at": now_iso(),
                }
                conflicts.append(conflict)
                if strict:
                    raise IdentityConflict(
                        f"label {label!r} already belongs to {owner}"
                    )
                continue
            if claim:
                labels[key] = identity.id
        members[identity.id] = identity.to_dict()
        if source:
            source_map = data.setdefault("sources", {})
            if isinstance(source_map, dict):
                source_map[identity.id] = source
        data["labels"] = labels
        if conflicts:
            history = list(data.get("conflicts") or [])
            history.extend(conflicts)
            data["conflicts"] = history[-MAX_CONFLICTS:]
        return claimed, conflicts

    def register(
        self,
        identity: MemberIdentity,
        *,
        source: str = "",
        strict: bool = False,
        claim: bool = True,
    ) -> bool:
        with self._locked(write=True):
            data = self._read_unlocked()
            claimed, _conflicts = self._register_data(
                data,
                identity,
                strict=strict,
                source=source,
                claim=claim,
            )
            self._write_unlocked(data)
            return claimed

    def observe(
        self,
        identity: MemberIdentity,
        *,
        source: str = "",
    ) -> tuple[bool, list[dict[str, str]]]:
        """Merge a remote identity without granting it a conflicting label."""
        with self._locked(write=True):
            data = self._read_unlocked()
            claimed, conflicts = self._register_data(
                data,
                identity,
                strict=False,
                source=source,
                claim=True,
            )
            self._write_unlocked(data)
            return claimed, conflicts

    def rename(self, member_id: str, name: object) -> MemberIdentity:
        with self._locked(write=True):
            data = self._read_unlocked()
            raw = (data.get("members") or {}).get(member_id)
            if not isinstance(raw, dict):
                raise IdentityError(f"unknown member id: {member_id}")
            current = MemberIdentity.from_dict(raw)
            renamed = current.renamed(name)
            labels = dict(data.get("labels") or {})
            rebuilt = self._rebuild_labels(data.get("members") or {})
            for key, owner in rebuilt.items():
                labels.setdefault(key, owner)
            target_key = canonical_name(renamed.name)
            owner = labels.get(target_key)
            if owner and owner != member_id:
                raise IdentityConflict(
                    f"label {renamed.name!r} already belongs to {owner}"
                )
            for label in renamed.labels:
                key = canonical_name(label)
                owner = labels.get(key)
                if owner and owner != member_id:
                    raise IdentityConflict(
                        f"label {label!r} already belongs to {owner}"
                    )
            for label in renamed.labels:
                labels[canonical_name(label)] = member_id
            data["members"][member_id] = renamed.to_dict()
            data["labels"] = labels
            self._write_unlocked(data)
            return renamed


def manifest_path(userdata_dir: Path) -> Path:
    return Path(userdata_dir).expanduser() / MANIFEST_FILENAME


def read_manifest(path: Path) -> dict[str, Any] | None:
    path = Path(path).expanduser()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IdentityError(f"cannot read identity manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise IdentityError(f"identity manifest {path} must contain a JSON object")
    return data


def identity_from_manifest(data: Mapping[str, Any]) -> MemberIdentity:
    aliases = data.get("aliases") or []
    if isinstance(aliases, str):
        aliases = aliases.split(",")
    return MemberIdentity(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or ""),
        aliases=tuple(str(item) for item in aliases if str(item).strip()),
        kind=str(data.get("kind") or MEMBER_KIND_AGENT),
        created_at=str(data.get("created_at") or now_iso()),
    )


def write_manifest(
    path: Path,
    identity: MemberIdentity,
    *,
    profile: str,
    agent_id: str,
    userdata: str,
) -> None:
    payload = identity.to_dict()
    payload.update(
        {
            "version": 1,
            "profile": profile,
            "agent_id": agent_id,
            "userdata": userdata,
        }
    )
    _atomic_write(
        Path(path),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def ensure_manifest(
    userdata_dir: Path,
    *,
    profile: str,
    agent_id: str,
    fallback_name: str,
    fallback_aliases: Iterable[str] = (),
    userdata_label: str = "",
) -> tuple[MemberIdentity, Path]:
    """Load or create the per-profile authoritative identity manifest."""
    path = manifest_path(userdata_dir)
    data = read_manifest(path)
    if data is not None:
        identity = identity_from_manifest(data)
        existing_profile = str(data.get("profile") or "").strip()
        if existing_profile and existing_profile != profile:
            raise IdentityError(
                f"identity manifest {path} belongs to profile {existing_profile!r}, "
                f"not {profile!r}"
            )
        existing_userdata = str(data.get("userdata") or "").strip()
        if existing_userdata and userdata_label and existing_userdata != userdata_label:
            # A moved directory is fine only when the caller explicitly updates
            # the userdata label; keep the old path for human inspection.
            pass
        if identity.name != fallback_name or tuple(identity.aliases) != clean_aliases(
            fallback_aliases, name=fallback_name
        ):
            payload = identity.to_dict()
            payload.update(
                {
                    "version": 1,
                    "profile": profile,
                    "agent_id": agent_id,
                    "userdata": existing_userdata or userdata_label,
                }
            )
            # The manifest is authoritative.  Only fill missing metadata; never
            # rewrite its name/aliases from a stale config mirror.
            if not existing_userdata:
                write_manifest(
                    path,
                    identity,
                    profile=profile,
                    agent_id=str(data.get("agent_id") or agent_id),
                    userdata=userdata_label,
                )
        return identity, path
    name = clean_name(fallback_name)
    identity = MemberIdentity(
        id=legacy_member_id(name),
        name=name,
        aliases=clean_aliases(fallback_aliases, name=name),
        kind=MEMBER_KIND_AGENT,
    )
    write_manifest(
        path,
        identity,
        profile=profile,
        agent_id=agent_id,
        userdata=userdata_label or str(userdata_dir),
    )
    return identity, path


def backup_migration_state(
    state_dir: Path,
    *,
    paths: Iterable[Path] = (),
) -> Path | None:
    """Create one best-effort backup before the first identity migration."""
    state_dir = Path(state_dir).expanduser()
    marker = state_dir / ".identity-migration-v1"
    if marker.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    root = state_dir / "backups" / f"identity-migration-{stamp}"
    try:
        root.mkdir(parents=True, mode=0o700)
        for source in paths:
            source = Path(source).expanduser()
            if not source.exists():
                continue
            target = root / source.name
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        marker.write_text(now_iso() + "\n", encoding="utf-8")
        return root
    except OSError:
        return None
