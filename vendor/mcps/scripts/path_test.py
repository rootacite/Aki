"""Test path-based (file) inputs for all 6 content tools against the live model."""

import asyncio
import json
import tempfile
import os

from mcps.server import mcp


def show(name: str, result) -> None:
    text = ""
    for item in result.content:
        text += getattr(item, "text", str(item))
    flag = " [isError]" if getattr(result, "isError", False) else ""
    print(f"\n>>> {name}{flag}\n{text[:900]}")
    if len(text) > 900:
        print(f"... [{len(text)} chars total]")


async def main() -> None:
    c_src = """\
#include <stdlib.h>
char *dup_and_free(char *src, int len) {
    char *dst = malloc(len);
    if (src == NULL) return NULL;
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
    items_txt = """\
GET /api/users 200 12ms
worker-3: failed to acquire lock: timeout after 5000ms
disk: /dev/sda1 at 91% capacity
segfault in worker-7 at addr 0x7f4a
cron: job nightly-backup completed ok
"""
    crash_log = """\
at 2026-08-26T10:02:33Z process 'api-server' (pid 4412) crashed with signal SIGSEGV
stack: parser.c:214 parse_token -> lexer.c:88 lex_next -> main.c:55
ERROR [EIO] unable to open config /etc/api/config.yaml
"""

    with tempfile.TemporaryDirectory() as td:
        c_file = os.path.join(td, "sample.c")
        items_file = os.path.join(td, "items.txt")
        crash_file = os.path.join(td, "crash.log")
        with open(c_file, "w") as f:
            f.write(c_src)
        with open(items_file, "w") as f:
            f.write(items_txt)
        with open(crash_file, "w") as f:
            f.write(crash_log)

        show("summarize(path=README.md)",
             await mcp.call_tool("summarize", {"path": "README.md", "objective": "what tools does this server expose", "max_words": 60}))

        show("code_review_precheck(path=sample.c)",
             await mcp.call_tool("code_review_precheck", {"path": c_file, "language": "c"}))

        show("classify(path=items.txt)",
             await mcp.call_tool("classify", {"path": items_file, "categories": ["normal", "warning", "failure"]}))

        show("batch_analyze(path=items.txt)",
             await mcp.call_tool("batch_analyze", {"path": items_file, "task": "check each log line for operational impact"}))

        show("extract(path=crash.log)",
             await mcp.call_tool("extract", {"path": crash_file, "fields": ["timestamp", "process", "pid", "signal"]}))

        show("compress_context(path=crash.log)",
             await mcp.call_tool("compress_context", {"path": crash_file, "objective": "summarize the crash"}))

        show("error: summarize(path=/nonexistent)",
             await mcp.call_tool("summarize", {"path": "/no/such/file.log"}))

        show("error: classify(path + items both)",
             await mcp.call_tool("classify", {"path": items_file, "items": ["x"], "categories": ["a"]}))


asyncio.run(main())
