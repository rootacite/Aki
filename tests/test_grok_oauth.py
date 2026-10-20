from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

from engine import grok_oauth
from engine.grok_oauth import GrokOAuthError, GrokOAuthProvider


CLIENT_ID = "test-client-id"


class _OidcHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/.well-known/openid-configuration":
            self.send_error(404)
            return
        self.server.requests.append(("GET", self.path, {}))
        payload = {
            "issuer": self.server.base_url,
            "token_endpoint": f"{self.server.base_url}/oauth2/token",
        }
        self._json(200, payload)

    def do_POST(self) -> None:
        if self.path != "/oauth2/token":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        self.server.requests.append(("POST", self.path, parse_qs(body)))
        self._json(
            200,
            {
                "access_token": "fresh-access-token",
                "refresh_token": "fresh-refresh-token",
                "expires_in": 3600,
            },
        )

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class GrokOAuthProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.auth_path = self.root / "auth.json"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OidcHandler)
        self.server.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def _write_auth(self, **overrides: object) -> None:
        record: dict[str, object] = {
            "key": "stale-access-token",
            "auth_mode": "oidc",
            "user_id": "user-123",
            "email": "aki@example.test",
            "oidc_issuer": self.server.base_url,
            "oidc_client_id": CLIENT_ID,
            "principal_type": "User",
            "principal_id": "user-123",
            "refresh_token": "old-refresh-token",
            "expires_at": "2020-01-01T00:00:00.123456789Z",
        }
        record.update(overrides)
        self.auth_path.write_text(
            json.dumps({f"{self.server.base_url}::{CLIENT_ID}": record}),
            encoding="utf-8",
        )

    def test_refreshes_expired_token_and_persists_rotation(self) -> None:
        self._write_auth()
        provider = GrokOAuthProvider(
            self.auth_path,
            timeout=3,
            client_version="1.0.62",
        )
        with mock.patch.object(grok_oauth, "XAI_ISSUER", self.server.base_url):
            credentials = provider.credentials()

        self.assertEqual(credentials.access_token, "fresh-access-token")
        self.assertEqual(credentials.user_id, "user-123")
        headers = credentials.extra_headers()
        self.assertEqual(headers["X-XAI-Token-Auth"], "xai-grok-cli")
        self.assertEqual(headers["x-authenticateresponse"], "authenticate-response")
        self.assertEqual(headers["x-grok-client-mode"], "headless")
        self.assertEqual(headers["x-userid"], "user-123")
        self.assertEqual(headers["x-email"], "aki@example.test")
        self.assertEqual(headers["x-grok-client-version"], "1.0.62")

        posts = [item for item in self.server.requests if item[0] == "POST"]
        self.assertEqual(len(posts), 1)
        form = posts[0][2]
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], ["old-refresh-token"])
        self.assertEqual(form["client_id"], [CLIENT_ID])
        self.assertEqual(form["principal_type"], ["User"])
        self.assertEqual(form["principal_id"], ["user-123"])

        stored = json.loads(self.auth_path.read_text(encoding="utf-8"))
        record = stored[f"{self.server.base_url}::{CLIENT_ID}"]
        self.assertEqual(record["key"], "fresh-access-token")
        self.assertEqual(record["refresh_token"], "fresh-refresh-token")
        self.assertEqual(record["user_id"], "user-123")
        self.assertTrue(record["expires_at"].endswith("Z"))
        self.assertEqual(os.stat(self.auth_path).st_mode & 0o777, 0o600)

    def test_valid_token_uses_cache_without_network(self) -> None:
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        self._write_auth(expires_at=expires.isoformat().replace("+00:00", "Z"))
        provider = GrokOAuthProvider(self.auth_path, timeout=1)
        with mock.patch.object(grok_oauth, "XAI_ISSUER", self.server.base_url):
            credentials = provider.credentials()
        self.assertEqual(credentials.access_token, "stale-access-token")
        self.assertEqual(self.server.requests, [])

    def test_expired_token_without_refresh_token_is_rejected(self) -> None:
        self._write_auth(refresh_token="")
        provider = GrokOAuthProvider(self.auth_path, timeout=1)
        with mock.patch.object(grok_oauth, "XAI_ISSUER", self.server.base_url):
            with self.assertRaises(GrokOAuthError):
                provider.credentials()

    def test_ignores_non_xai_credentials(self) -> None:
        self.auth_path.write_text(
            json.dumps(
                {
                    "https://example.test::client": {
                        "key": "other-token",
                        "oidc_issuer": "https://example.test",
                    }
                }
            ),
            encoding="utf-8",
        )
        provider = GrokOAuthProvider(self.auth_path, timeout=1)
        with self.assertRaises(GrokOAuthError):
            provider.credentials()


if __name__ == "__main__":
    unittest.main()
