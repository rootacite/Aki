"""Functional smoke test for all 7 MCP tools against the live local model."""

import asyncio
import json
import sys

from mcps.server import mcp


def show(name: str, result) -> None:
    text = ""
    for item in result.content:
        text += getattr(item, "text", str(item))
    print(f"\n{'=' * 70}\n>>> {name}\n{'=' * 70}")
    print(text[:1200])
    if len(text) > 1200:
        print(f"... [{len(text)} chars total]")


async def main() -> None:
    # 1. summarize (small input)
    build_log = """\
[2026-08-26T09:12:01Z] starting build of target release-x64 in /src/proj
[2026-08-26T09:12:05Z] compiling src/main.rs (cached)
[2026-08-26T09:12:11Z] compiling src/network/conn.rs
[2026-08-26T09:12:14Z] warning: unused variable `retries` at src/network/conn.rs:142
[2026-08-26T09:12:16Z] compiling src/io/reader.rs
[2026-08-26T09:12:20Z] error[E0507]: cannot move out of `buf`, a captured variable in an `FnOnce` closure at src/io/reader.rs:88
[2026-08-26T09:12:21Z] error[E0507]: cannot move out of `buf` at src/io/reader.rs:91
[2026-08-26T09:12:25Z] the full error chain is in the incremental compilation cache
[2026-08-26T09:12:26Z] BUILD FAILED: 2 errors, 1 warning, elapsed 25s
"""
    show("summarize (small)", await mcp.call_tool(
        "summarize",
        {"text": build_log, "objective": "understand what failed and where", "max_words": 80},
    ))

    # 2. classify
    logs = [
        "GET /api/users 200 12ms",
        "worker-3: failed to acquire lock: timeout after 5000ms",
        "disk: /dev/sda1 at 91% capacity",
        "GET /api/health 200 3ms",
        "segfault in worker-7 at addr 0x7f4a",
        "cron: job nightly-backup completed ok",
    ]
    show("classify", await mcp.call_tool(
        "classify",
        {"items": logs, "categories": ["normal", "warning", "failure"], "criteria": "classify by operational impact"},
    ))

    # 3. extract
    crash_text = """\
== Crash report ==
at 2026-08-26T10:02:33Z process 'api-server' (pid 4412) crashed with signal SIGSEGV
stack: parser.c:214 parse_token -> lexer.c:88 lex_next -> main.c:55
last log line: ERROR [EIO] unable to open config /etc/api/config.yaml
thread: 'worker-pool-3'
"""
    show("extract", await mcp.call_tool(
        "extract",
        {
            "text": crash_text,
            "fields": ["timestamp", "process", "pid", "signal", "function"],
            "field_descriptions": {
                "timestamp": "ISO timestamp of the crash",
                "process": "name of the crashing process",
                "pid": "process id as integer",
                "signal": "crash signal name",
                "function": "function names from the stack",
            },
        },
    ))

    # 4. batch_analyze
    snippets = [
        "char *p = malloc(16); if (!p) return; free(p); use(p);",
        "int x = read_fd(); if (x > 0) { return x; } return -1;",
        "void f() { int *a = malloc(8); if (!a) abort(); /* no free */ }",
        "size_t n = strlen(s); char buf[n]; strcpy(buf, s);",
        "lock(m); if (cond) { unlock(m); return; } unlock(m);",
    ]
    show("batch_analyze", await mcp.call_tool(
        "batch_analyze",
        {
            "items": snippets,
            "task": "check each snippet for memory-safety problems: leaks, use-after-free, double-free, buffer overflow",
            "item_ids": ["s1", "s2", "s3", "s4", "s5"],
        },
    ))

    # 5. code_review_precheck
    c_code = """\
#include <stdlib.h>
#include <string.h>

char *dup_and_free(char *src, int len) {
    char *dst = malloc(len);
    if (src == NULL) return NULL;
    memcpy(dst, src, len);
    free(src);
    return dst;
}

int main(void) {
    char *buf = malloc(64);
    if (!buf) return 1;
    char *out = dup_and_free(buf, 128);
    free(out);
    return 0;
}
"""
    show("code_review_precheck", await mcp.call_tool(
        "code_review_precheck",
        {"code": c_code, "file_path": "sample.c", "language": "c"},
    ))

    # 6. compress_context (small)
    debug_notes = """\
2026-08-26 11:00 user reported checkout hangs on repo 'acme/core' after upgrade to v2.3.1
11:05 dev: suspect the new lockfile parser; git log shows parser rewrite landed in v2.3.0
11:10 reproduced locally: hang only with symlinked .git/lockfile
11:15 strace shows flock(2) waiting forever on fd 7 -> .git/lockfile (symlink to /mnt/nfs/...)
11:20 nfs server on same mount has high latency; other repos on the mount also slow
11:25 candidate fix: use flock on realpath() of lockfile (issue #1421 open)
"""
    show("compress_context (small)", await mcp.call_tool(
        "compress_context",
        {"context": debug_notes, "objective": "diagnose why checkout hangs"},
    ))

    # 7. ask
    show("ask", await mcp.call_tool(
        "ask",
        {"task": "Give three candidate shell commands to check disk usage on Linux, with one-line rationale each."},
    ))

    # 8. summarize with chunked (large) input
    big = "\n".join(
        f"record-{i}: op=write key=k{i % 50} size={100 + i} status={'ok' if i % 7 else 'error:EIO'} path=/data/shard-{i % 4}/seg-{i % 9}.log"
        for i in range(400)
    )
    show("summarize (chunked, large)", await mcp.call_tool(
        "summarize",
        {"text": big, "objective": "summarize the distribution of errors across shards", "max_words": 120},
    ))

    # 9. compress_context with chunked (large) input
    big_ctx = "\n".join(
        f"[{i:03d}] event: probe {i} latency={i * 3}ms node=worker-{i % 6} region={('us' if i % 2 else 'eu')}-{i % 3} status={'ok' if i % 5 else 'timeout'}"
        for i in range(300)
    )
    show("compress_context (chunked, large)", await mcp.call_tool(
        "compress_context",
        {"context": big_ctx, "objective": "summarize latency and timeout patterns by region"},
    ))


if __name__ == "__main__":
    asyncio.run(main())
