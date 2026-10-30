# mcps — Local-AI MCP Server

Exposes a local lightweight LLM as agent-facing preprocessing tools over MCP, so a primary reasoning agent can offload cheap, parallelizable, or information-compressing subtasks (see `local_mcp_tools.md` for the delegation policy).

**Recon tools are for authorized testing only.** The operator is responsible for scope, legality, and impact. Out-of-scope targets are rejected. Do not point these tools at systems you do not have permission to test.

## Tools

| Tool | Purpose |
|------|---------|
| `summarize` | Compact, objective-aware summary of a document, log, or source file. Large inputs are chunked and merged. |
| `classify` | Route/filter/triage items into caller-supplied categories with rationale, confidence, and `needs_review` flags. |
| `extract` | Pull explicitly requested fields/entities into structured records; preserves exact values and provenance. |
| `batch_analyze` | Run one lightweight task independently across many items; returns per-item results plus aggregates. |
| `code_review_precheck` | First-pass source review: candidate issues with severity, confidence, category, file/line, and suggested verification. Triage only. |
| `compress_context` | Loss-aware evidence pack (facts with provenance, identifiers, contradictions, unresolved questions) for downstream reasoning. |
| `ask` | General-purpose escape hatch for tasks that fit no specialized tool. |

Recon tools (scope-locked): see `docs/recon-toolkit/TOOLS.md` and `docs/recon-toolkit/AGENT_PLAYBOOK.md`. Configure `config/recon.yaml` (`MCP_RECON_CONFIG` override).

All structured outputs are JSON. Results are candidates/evidence, never final judgments; outputs flag uncertainty and escalate when the local model lacks the context or capability.

## File paths instead of inline content

Every content-accepting tool (`summarize`, `extract`, `compress_context`, `code_review_precheck`, `classify`, `batch_analyze`) also accepts a local file `path`. The server reads the file itself and injects its contents into the local model's context, so the calling agent never has to read the file or pass its contents inline — useful for large or many files.

- `summarize`, `extract`, `compress_context`: pass either inline text or `path` (exactly one).
- `code_review_precheck`: pass either inline `code` or `path` (exactly one); `file_path` defaults to the path for provenance.
- `classify`, `batch_analyze`: pass either inline `items` or `path` to a file containing the items — a JSON array of strings, or one item per non-empty line (exactly one).

Paths are resolved relative to the server process working directory; `~` is expanded; absolute paths are recommended. A missing/unreadable file, or providing both inline content and a path, returns a clear tool error.

Examples:

```json
{"path": "/var/log/app.log", "objective": "what failed and where"}
{"path": "/src/main.c", "language": "c"}
{"path": "/tmp/log_lines.txt", "categories": ["normal", "warning", "failure"]}
```

## Requirements

- A local OpenAI-compatible model server, e.g. llama.cpp at `http://127.0.0.1:10001/v1` (default, configurable).
- Python 3.14, dependencies managed with uv.

## Configuration

Environment variables (all optional, with defaults):

| Variable | Default |
|----------|---------|
| `LOCAL_AI_BASE_URL` | `http://127.0.0.1:10001/v1` |
| `LOCAL_AI_MODEL` | `/opt/models/language/Qwen3.8-9B-Q8_0.gguf` |
| `LOCAL_AI_API_KEY` | `sk-local` |
| `LOCAL_AI_MAX_TOKENS` | `1024` |
| `LOCAL_AI_TIMEOUT` | `300` (seconds) |
| `LOCAL_AI_CONCURRENCY` | `4` (parallel chunk requests) |
| `LOCAL_AI_ENABLE_THINKING` | `0` |

The bundled Qwen3 model spends its token budget on reasoning by default and returns empty `content`, so thinking is disabled via `chat_template_kwargs.enable_thinking=false` unless `LOCAL_AI_ENABLE_THINKING=1` is set.

## Usage

```bash
# stdio (default; what MCP clients spawn)
uv run mcps

# or directly with the venv python (no uv involved)
.venv/bin/mcps

# network transports
uv run mcps --transport sse --host 127.0.0.1 --port 8000
uv run mcps --transport streamable-http --host 127.0.0.1 --port 8000
```

### Registering in an MCP client

Example client config (Claude Code / compatible):

```json
{
  "mcpServers": {
    "local-ai": {
      "command": "/home/acite/Deeppin/mcps/.venv/bin/mcps",
      "args": []
    }
  }
}
```

If you prefer `uv run mcps`, note that uv's cache directory must be writable in the client's environment (see Troubleshooting).

## Development

```bash
uv run python scripts/smoke_test.py   # end-to-end tool tests against the live model
```

## Troubleshooting

- **Empty model responses**: the local Qwen3 build needs thinking disabled (handled by default). If you changed `LOCAL_AI_ENABLE_THINKING=1` or swapped models, responses may be empty.
- **`uv: Failed to initialize cache ... Permission denied`**: uv needs a writable cache directory. Set `UV_CACHE_DIR` to a writable path (e.g. `export UV_CACHE_DIR=/home/acite/Deeppin/mcps/.uv-cache`) or run the server via `.venv/bin/mcps` to bypass uv.
