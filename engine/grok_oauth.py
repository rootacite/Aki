"""Reuse Grok Build's xAI OAuth credentials for akid upstream requests."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib import error, parse, request

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None


LOGGER = logging.getLogger("akid.grok_oauth")

XAI_ISSUER = "https://auth.x.ai"
XAI_LOCAL_ISSUER = "http://localhost:22255"
LEGACY_XAI_SCOPE = "https://accounts.x.ai/sign-in"
GROK_OAUTH_AUTH = "grok-oauth"
REFRESH_MARGIN = timedelta(minutes=5)
DISCOVERY_TTL = 3600.0
MAX_RESPONSE_BYTES = 1024 * 1024


class GrokOAuthError(RuntimeError):
    """Grok Build credentials are missing, expired, or could not be refreshed."""


@dataclass(frozen=True)
class GrokCredentials:
    access_token: str
    user_id: str
    email: str
    client_version: str

    def extra_headers(self) -> dict[str, str]:
        headers = {
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-authenticateresponse": "authenticate-response",
            "x-grok-client-mode": "headless",
        }
        if self.user_id:
            headers["x-userid"] = self.user_id
        if self.email:
            headers["x-email"] = self.email
        if self.client_version:
            headers["x-grok-client-version"] = self.client_version
        return headers


def default_auth_path(home: Path | None = None) -> Path:
    configured = os.environ.get("GROK_AUTH_PATH")
    if configured:
        return Path(configured).expanduser()
    grok_home = os.environ.get("GROK_HOME")
    if grok_home:
        return Path(grok_home).expanduser() / "auth.json"
    return (home or Path.home()) / ".grok" / "auth.json"


class GrokOAuthProvider:
    """Resolve and refresh the xAI OAuth bearer stored by Grok Build."""

    def __init__(
        self,
        auth_path: Path | str | None = None,
        *,
        upstream_proxy: str = "",
        timeout: float = 15.0,
        refresh_margin: timedelta = REFRESH_MARGIN,
        client_version: str = "",
    ) -> None:
        self.auth_path = Path(auth_path) if auth_path is not None else default_auth_path()
        self.upstream_proxy = upstream_proxy.strip()
        self.timeout = timeout
        self.refresh_margin = refresh_margin
        self._client_version = client_version
        self._discovery: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def credentials(self) -> GrokCredentials:
        with self._lock:
            now = datetime.now(timezone.utc)
            scope, record = _select_xai_record(self._read_auth())
            if not _needs_refresh(record, now, self.refresh_margin):
                return self._credentials(record)
            return self._refresh_locked()

    def _refresh_locked(self) -> GrokCredentials:
        with _auth_lock(self.auth_path):
            now = datetime.now(timezone.utc)
            scope, record = _select_xai_record(self._read_auth())
            if not _needs_refresh(record, now, self.refresh_margin):
                return self._credentials(record)
            refresh_token = _text(record.get("refresh_token"))
            client_id = _text(record.get("oidc_client_id"))
            if not refresh_token or not client_id:
                raise GrokOAuthError(
                    "Grok Build access token is expired and auth.json has no refresh credentials"
                )
            issuer = _text(record.get("oidc_issuer")) or XAI_ISSUER
            token_endpoint = self._token_endpoint(issuer)
            form = [
                ("grant_type", "refresh_token"),
                ("refresh_token", refresh_token),
                ("client_id", client_id),
            ]
            principal_type = _text(record.get("principal_type"))
            principal_id = _text(record.get("principal_id"))
            if principal_type:
                form.append(("principal_type", principal_type))
            if principal_id:
                form.append(("principal_id", principal_id))
            payload = self._post_form_json(token_endpoint, form)
            access_token = _text(payload.get("access_token"))
            if not access_token:
                raise GrokOAuthError("Grok OAuth refresh returned no access_token")

            updated = dict(record)
            updated["key"] = access_token
            rotated = _text(payload.get("refresh_token"))
            if rotated:
                updated["refresh_token"] = rotated
            expires_in = payload.get("expires_in")
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
                expires_at = now + timedelta(seconds=int(expires_in))
                updated["expires_at"] = _isoformat(expires_at)
            updated["create_time"] = _isoformat(now)
            self._write_record(scope, updated)
            return self._credentials(updated)

    def _credentials(self, record: Mapping[str, Any]) -> GrokCredentials:
        access_token = _text(record.get("key"))
        if not access_token:
            raise GrokOAuthError("Grok Build auth.json has no access token")
        return GrokCredentials(
            access_token=access_token,
            user_id=_text(record.get("user_id")),
            email=_text(record.get("email")),
            client_version=self._version(),
        )

    def _read_auth(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise GrokOAuthError(f"Grok Build is not logged in: {self.auth_path}") from exc
        except (OSError, ValueError) as exc:
            raise GrokOAuthError(f"cannot read Grok Build auth.json: {exc}") from exc
        if not isinstance(payload, dict):
            raise GrokOAuthError("Grok Build auth.json is not a JSON object")
        return payload

    def _write_record(self, scope: str, record: dict[str, Any]) -> None:
        payload = self._read_auth()
        payload[scope] = record
        directory = self.auth_path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp_path = tempfile.mkstemp(prefix=f".{self.auth_path.name}.", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.auth_path)
            os.chmod(self.auth_path, 0o600)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
            raise

    def _version(self) -> str:
        if not self._client_version:
            self._client_version = _detect_grok_version()
        return self._client_version

    def _request_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "grok-build-oauth/1"}
        version = self._version()
        if version:
            headers["x-grok-client-version"] = version
        return headers

    def _opener(self):
        if self.upstream_proxy:
            proxies = {"http": self.upstream_proxy, "https": self.upstream_proxy}
        else:
            proxies = {}
        return request.build_opener(request.ProxyHandler(proxies))

    def _get_json(self, url: str) -> dict[str, Any]:
        req = request.Request(url, headers=self._request_headers(), method="GET")
        return self._open_json(req, url)

    def _post_form_json(self, url: str, form: list[tuple[str, str]]) -> dict[str, Any]:
        headers = self._request_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = parse.urlencode(form).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method="POST")
        return self._open_json(req, url)

    def _open_json(self, req: request.Request, url: str) -> dict[str, Any]:
        try:
            with self._opener().open(req, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            body = exc.read(4096)
            code = ""
            with contextlib.suppress(ValueError):
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    code = _text(parsed.get("error"))
            suffix = f" ({code})" if code else ""
            raise GrokOAuthError(f"Grok OAuth HTTP {exc.code}{suffix}") from exc
        except (OSError, ValueError) as exc:
            raise GrokOAuthError(f"Grok OAuth request failed: {exc}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GrokOAuthError("Grok OAuth response was too large")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise GrokOAuthError(f"Grok OAuth returned invalid JSON from {url}") from exc
        if not isinstance(payload, dict):
            raise GrokOAuthError(f"Grok OAuth returned a non-object from {url}")
        return payload

    def _token_endpoint(self, issuer: str) -> str:
        issuer = issuer.rstrip("/")
        cached = self._discovery.get(issuer)
        if cached is not None and time.monotonic() - cached[0] < DISCOVERY_TTL:
            return cached[1]
        discovery_url = f"{issuer}/.well-known/openid-configuration"
        try:
            payload = self._get_json(discovery_url)
            endpoint = _text(payload.get("token_endpoint"))
        except GrokOAuthError as exc:
            if issuer not in (XAI_ISSUER, XAI_LOCAL_ISSUER):
                raise
            LOGGER.warning("Grok OAuth discovery failed, using xAI token endpoint: %s", exc)
            endpoint = f"{issuer}/oauth2/token"
        if not endpoint:
            raise GrokOAuthError("Grok OAuth discovery has no token_endpoint")
        parsed = parse.urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise GrokOAuthError("Grok OAuth token_endpoint is invalid")
        self._discovery[issuer] = (time.monotonic(), endpoint)
        return endpoint


def _select_xai_record(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for scope, record in payload.items():
        if not isinstance(record, dict) or not _is_xai_record(scope, record):
            continue
        if not _text(record.get("key")):
            continue
        expiry = _expiry_timestamp(record)
        rank = float("inf") if expiry is None else expiry
        candidates.append((rank, scope, record))
    if not candidates:
        raise GrokOAuthError(
            "Grok Build auth.json has no xAI OAuth credential; run `grok login` first"
        )
    _rank, scope, record = max(candidates, key=lambda item: item[0])
    return scope, record


def _is_xai_record(scope: str, record: Mapping[str, Any]) -> bool:
    issuer = _text(record.get("oidc_issuer")).rstrip("/")
    if issuer in (XAI_ISSUER, XAI_LOCAL_ISSUER):
        return True
    if issuer:
        return False
    return scope.startswith(f"{XAI_ISSUER}::") or scope == LEGACY_XAI_SCOPE


def _needs_refresh(record: Mapping[str, Any], now: datetime, margin: timedelta) -> bool:
    expires = _expiry(record)
    return expires is not None and expires <= now + margin


def _expiry_timestamp(record: Mapping[str, Any]) -> float | None:
    expires = _expiry(record)
    return expires.timestamp() if expires is not None else None


def _expiry(record: Mapping[str, Any]) -> datetime | None:
    raw = _text(record.get("expires_at"))
    if raw:
        text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        text = re.sub(r"(\.\d{6})\d+", r"\1", text)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    exp = _jwt_exp(_text(record.get("key")))
    return datetime.fromtimestamp(exp, timezone.utc) if exp is not None else None


def _jwt_exp(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
    except (ValueError, TypeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return exp if isinstance(exp, int) and not isinstance(exp, bool) else None


def _detect_grok_version() -> str:
    configured = _text(os.environ.get("GROK_VERSION"))
    if configured:
        return configured
    try:
        completed = subprocess.run(
            ["grok", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", completed.stdout or completed.stderr)
    return match.group(1) if match else ""


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@contextlib.contextmanager
def _auth_lock(auth_path: Path):
    if fcntl is None:
        yield
        return
    lock_path = auth_path.with_name("auth.json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}:{int(time.time())}")
            handle.flush()
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
