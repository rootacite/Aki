#!/usr/bin/env python3
"""Passively capture five operator-authored turns from the proxy debug socket."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.live_acceptance import analyze_events  # noqa: E402
from engine.proxy_debug import default_socket_path  # noqa: E402

DEBUG_STREAM_LIMIT = 16 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def request_progress(events: list[dict[str, Any]]) -> tuple[set[int], bool]:
    terminal = {
        str(event.get("request_id") or "")
        for event in events
        if str(event.get("event") or "") in ("response_end", "error")
        and str(event.get("request_id") or "")
    }
    request_ids = {
        str(event.get("request_id") or "")
        for event in events
        if str(event.get("event") or "") == "request"
        and str(event.get("request_id") or "")
    }
    return request_ids, request_ids.issubset(terminal)


async def capture(
    *,
    socket_path: Path,
    raw_path: Path,
    turns: int,
    timeout: float,
    settle_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    reader, writer = await asyncio.open_unix_connection(
        str(socket_path), limit=DEBUG_STREAM_LIMIT
    )
    events: list[dict[str, Any]] = []
    hello: dict[str, Any] = {}
    timed_out = False
    try:
        hello_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not hello_line:
            raise RuntimeError("debug socket closed before hello")
        hello = json.loads(hello_line)
        ring_end = int(hello.get("ring_end_seq") or 0)
        # Discard every replay row emitted after hello.
        for _ in range(int(hello.get("queued_events") or 0)):
            replay = await asyncio.wait_for(reader.readline(), timeout=10)
            if not replay:
                raise RuntimeError("debug socket closed during replay")
        with raw_path.open("wb") as raw:
            raw.write(
                (
                    json.dumps(
                        hello, ensure_ascii=False, separators=(",", ":")
                    )
                    + "\n"
                ).encode("utf-8")
            )
            raw.flush()
            started = time.monotonic()
            last_event = started
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    timed_out = True
                    break
                request_ids, requests_complete = request_progress(events)
                snapshot = analyze_events(events, expected_turns=turns)
                if (
                    snapshot["received_turns"] >= turns
                    and request_ids
                    and requests_complete
                    and time.monotonic() - last_event >= settle_seconds
                ):
                    break
                wait = min(timeout - elapsed, settle_seconds)
                if wait <= 0:
                    timed_out = True
                    break
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=wait)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
                raw.write(line)
                raw.flush()
                last_event = time.monotonic()
                try:
                    event = json.loads(line)
                except ValueError:
                    events.append({"event": "malformed", "raw": line.decode("utf-8", "replace")})
                    continue
                if isinstance(event, dict):
                    seq = event.get("seq")
                    if isinstance(seq, int) and seq <= ring_end:
                        continue
                    events.append(event)
    finally:
        writer.close()
        await writer.wait_closed()
    return events, hello, timed_out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="aki")
    parser.add_argument("--turns", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--settle-seconds", type=float, default=8.0)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    socket_path = args.socket or default_socket_path(args.profile)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.out or Path(f"/tmp/aki-live-acceptance-{stamp}")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw_path = output / "aki-proxy-debug.jsonl"
    manifest_path = output / "manifest.json"
    report_path = output / "report.json"
    started = now_iso()

    print(f"raw capture: {raw_path}", flush=True)
    print(
        f"Send {args.turns} separate, real user messages in the configured channel. "
        "This tool only reads the Unix debug socket and never sends a message.",
        flush=True,
    )
    events, hello, timed_out = asyncio.run(
        capture(
            socket_path=socket_path,
            raw_path=raw_path,
            turns=args.turns,
            timeout=args.timeout,
            settle_seconds=args.settle_seconds,
        )
    )
    report = analyze_events(events, expected_turns=args.turns)
    if timed_out and report["status"] != "FAIL":
        report["status"] = "INCOMPLETE"
        report.setdefault("errors", []).append("capture timed out")
    manifest = {
        "schema": "aki.live-api-acceptance.v1",
        "profile": args.profile,
        "socket": str(socket_path),
        "started_at": started,
        "finished_at": now_iso(),
        "timeout_seconds": args.timeout,
        "settle_seconds": args.settle_seconds,
        "requested_turns": args.turns,
        "received_turns": report["received_turns"],
        "timed_out": timed_out,
        "hello": hello,
        "ring_end_seq": int(hello.get("ring_end_seq") or 0),
        "raw_capture": str(raw_path),
        "report": str(report_path),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if report["status"] == "PASS":
        return 0
    if report["status"] == "INCOMPLETE":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
