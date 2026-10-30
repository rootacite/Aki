"""FastMCP server exposing a local LLM as agent-facing preprocessing tools."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from fastmcp import FastMCP

from . import llm, prompts
from .tools.recon import register as register_recon
from .utils import chunk_text, parse_json, read_file_text, read_items, split_items

# Inputs larger than this are processed in overlapping chunks and merged.
CHUNK_CHARS = 12_000
CHUNK_OVERLAP = 300
# batch_analyze request grouping.
BATCH_MAX_ITEMS = 8
BATCH_MAX_CHARS = 6_000

mcp = FastMCP(
    "local-ai-mcp",
    instructions=(
        "Exposes a local lightweight LLM as low-cost preprocessing tools: "
        "summarize, classify, extract, batch_analyze, code_review_precheck, "
        "compress_context, and ask. Outputs are candidates and evidence for a "
        "primary reasoning agent, not final judgments. Also exposes scoped recon "
        "tools (passive enum / DNS first); out-of-scope targets are rejected."
    ),
)
register_recon(mcp)


async def _call_model(
    system: str, user: str, *, max_tokens: int | None = None, temperature: float = 0.2
) -> str:
    """Run the local model; return a JSON error object instead of raising."""
    try:
        return await llm.complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )
    except Exception as exc:  # noqa: BLE001 - surface any model failure to the caller
        return json.dumps(
            {"error": f"local model call failed: {type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


def _resolve_input(text: str, path: str, *, text_label: str) -> str:
    """Return inline text or file contents; exactly one must be provided."""
    if path:
        if text.strip():
            raise ValueError(
                f"provide either inline {text_label} or a path, not both"
            )
        return read_file_text(path)
    if not text.strip():
        raise ValueError(f"either inline {text_label} or a path is required")
    return text


@mcp.tool()
async def summarize(
    text: str = "",
    objective: str = "",
    max_words: int = 300,
    language: str = "en",
    path: str = "",
) -> str:
    """Summarize a document, log, command output, conversation, or source file into a compact, information-dense representation.

    Use for context reduction when the primary agent needs an overview without consuming the full input. Preserves important facts, identifiers, anomalies, and quantitative information relevant to the objective. Large inputs are processed in chunks and merged. Pass inline text or a local file `path` (the server reads the file itself); exactly one is required.
    """
    text = _resolve_input(text, path, text_label="text")
    user = (
        f"Objective: {objective or 'general overview'}\n"
        f"Approximate summary length: {max_words} words.\n"
        f"Output language: {language}\n\n"
        f"Input:\n{text}"
    )
    if len(text) <= CHUNK_CHARS:
        return await _call_model(
            prompts.SUMMARIZE_SYSTEM,
            user,
            max_tokens=min(4096, max(256, max_words * 2)),
            temperature=0.4,
        )
    chunks = chunk_text(text, CHUNK_CHARS, CHUNK_OVERLAP)
    sem = asyncio.Semaphore(llm.CONCURRENCY)

    async def summarize_chunk(index: int, chunk: str) -> str:
        async with sem:
            chunk_user = (
                f"Objective: {objective or 'general overview'}\n"
                f"Output language: {language}\n\n"
                f"Input (chunk {index + 1}/{len(chunks)}):\n{chunk}"
            )
            return await _call_model(
                prompts.SUMMARIZE_SYSTEM, chunk_user, max_tokens=512, temperature=0.4
            )

    summaries = await asyncio.gather(
        *(summarize_chunk(i, c) for i, c in enumerate(chunks))
    )
    merge_user = (
        f"Output language: {language}\n"
        f"Approximate final length: {max_words} words.\n\n"
        f"Chunk summaries:\n" + "\n---\n".join(summaries)
    )
    return await _call_model(
        prompts.SUMMARIZE_MERGE_SYSTEM,
        merge_user,
        max_tokens=min(4096, max(256, max_words * 2)),
        temperature=0.4,
    )


@mcp.tool()
async def classify(
    items: list[str] | None = None,
    categories: list[str] | None = None,
    criteria: str = "",
    allow_unknown: bool = True,
    path: str = "",
) -> str:
    """Classify one or more items into caller-supplied categories for fast routing, filtering, triage, and labeling.

    Returns per-item category, rationale, confidence, and needs_review flag plus a category distribution. Uses exactly the supplied categories; "unknown" is only used when allow_unknown is true and no category fits. Pass `items` inline or a `path` to a file (JSON array of strings, or one item per line); exactly one is required.
    """
    if path:
        if items:
            raise ValueError("provide either items or a path, not both")
        items = read_items(path)
    items = items or []
    if not items:
        raise ValueError("either items or a path to an items file is required")
    if not categories:
        raise ValueError("categories are required")
    user = (
        f"Categories: {json.dumps(categories, ensure_ascii=False)}\n"
        f"Allow 'unknown' fallback: {allow_unknown}\n"
        f"Criteria: {criteria or 'use the most reasonable match'}\n\n"
        f"Items to classify:\n{json.dumps(items, ensure_ascii=False, indent=1)}"
    )
    raw = await _call_model(
        prompts.CLASSIFY_SYSTEM,
        user,
        max_tokens=min(4096, max(768, len(items) * 64)),
        temperature=0.2,
    )
    data, ok = parse_json(raw)
    if ok and isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(
        {"results": [], "distribution": {}, "parse_error": raw}, ensure_ascii=False
    )


@mcp.tool()
async def extract(
    text: str = "",
    fields: list[str] | None = None,
    field_descriptions: dict[str, str] | None = None,
    path: str = "",
) -> str:
    """Extract explicitly requested structured fields, entities, or patterns from unstructured or semi-structured input.

    Use when the caller knows exactly which fields are needed (identifiers, timestamps, error codes, paths, versions...). Preserves exact values and provenance, distinguishes missing from empty values, and reports malformed records. Not for open-ended reasoning. Pass inline text or a local file `path` (the server reads the file itself); exactly one is required.
    """
    text = _resolve_input(text, path, text_label="text")
    fields = fields or []
    if not fields:
        raise ValueError("fields are required")
    desc = ""
    if field_descriptions:
        desc = (
            "Field descriptions:\n"
            + json.dumps(field_descriptions, ensure_ascii=False, indent=1)
            + "\n"
        )
    user = (
        f"Fields to extract: {json.dumps(fields, ensure_ascii=False)}\n"
        f"{desc}Input:\n{text}"
    )
    raw = await _call_model(
        prompts.EXTRACT_SYSTEM,
        user,
        max_tokens=min(4096, max(768, 512 + len(text) // 8)),
        temperature=0.2,
    )
    data, ok = parse_json(raw)
    if ok and isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(
        {"records": [], "malformed": [], "parse_error": raw}, ensure_ascii=False
    )


@mcp.tool()
async def batch_analyze(
    items: list[str] | None = None,
    task: str = "",
    path: str = "",
    item_ids: list[str] | None = None,
) -> str:
    """Run the same lightweight analysis task independently across a collection of inputs (high-volume parallel preprocessing).

    Designed for screening dozens to hundreds of similar items. Items are analyzed independently in chunks; returns compact per-item results plus aggregate statistics (processed, with_findings, failed, distribution). Pass `items` inline or a `path` to a file (JSON array of strings, or one item per line); exactly one is required.
    """
    if path:
        if items:
            raise ValueError("provide either items or a path, not both")
        items = read_items(path)
    items = items or []
    if not items:
        raise ValueError("either items or a path to an items file is required")
    if not task.strip():
        raise ValueError("task is required")
    return await _batch_analyze_items(items, task, item_ids)


async def _batch_analyze_items(
    items: list[str], task: str, item_ids: list[str] | None = None
) -> str:
    if not items:
        return json.dumps(
            {
                "results": [],
                "aggregates": {
                    "processed": 0,
                    "with_findings": 0,
                    "failed": 0,
                    "distribution": {},
                },
                "escalation": None,
            },
            ensure_ascii=False,
        )
    ids = list(item_ids) if item_ids else [str(i) for i in range(len(items))]
    if len(ids) < len(items):
        ids += [str(i) for i in range(len(ids), len(items))]
    chunks = split_items(items, BATCH_MAX_ITEMS, BATCH_MAX_CHARS)
    sem = asyncio.Semaphore(llm.CONCURRENCY)

    async def process_chunk(chunk: list[tuple[int, str]]) -> dict[str, Any]:
        payload = [{"id": ids[idx], "content": item} for idx, item in chunk]
        user = (
            f"Analysis task: {task}\n\n"
            f"Items:\n{json.dumps(payload, ensure_ascii=False, indent=1)}"
        )
        raw = await _call_model(
            prompts.BATCH_ANALYZE_SYSTEM,
            user,
            max_tokens=min(4096, max(1024, len(chunk) * 200)),
            temperature=0.3,
        )
        data, ok = parse_json(raw)
        if ok and isinstance(data, dict) and isinstance(data.get("results"), list):
            return data
        return {"results": [], "parse_error": raw}

    async def guarded(chunk: list[tuple[int, str]]) -> dict[str, Any]:
        async with sem:
            return await process_chunk(chunk)

    chunk_results = await asyncio.gather(*(guarded(c) for c in chunks))
    results_by_id: dict[str, dict[str, Any]] = {}
    parse_errors: list[str] = []
    for cr in chunk_results:
        if "parse_error" in cr:
            parse_errors.append(cr["parse_error"])
            continue
        for result in cr.get("results", []):
            rid = result.get("item_id")
            if rid is not None:
                results_by_id[str(rid)] = result
    ordered: list[dict[str, Any]] = []
    for i in range(len(items)):
        result = results_by_id.get(str(ids[i]))
        if result is None:
            ordered.append(
                {
                    "item_id": ids[i],
                    "status": "failed",
                    "findings": [],
                    "confidence": 0.0,
                    "rationale": "no result returned by the model",
                    "locations": [],
                }
            )
        else:
            ordered.append(result)
    with_findings = sum(1 for r in ordered if r.get("status") == "finding")
    failed = sum(1 for r in ordered if r.get("status") == "failed")
    distribution: dict[str, int] = {}
    for r in ordered:
        status = str(r.get("status") or "unknown")
        distribution[status] = distribution.get(status, 0) + 1
    output: dict[str, Any] = {
        "results": ordered,
        "aggregates": {
            "processed": len(ordered),
            "with_findings": with_findings,
            "failed": failed,
            "distribution": distribution,
        },
    }
    if parse_errors:
        output["warnings"] = [
            f"chunk output could not be parsed: {pe[:300]}" for pe in parse_errors
        ]
    return json.dumps(output, ensure_ascii=False)


@mcp.tool()
async def code_review_precheck(
    code: str = "",
    file_path: str = "untitled",
    language: str = "",
    path: str = "",
) -> str:
    """Lightweight first-pass source code review: candidate issues, suspicious patterns, resource-lifetime hazards, and areas deserving deeper review.

    Triage only; never claims a proven vulnerability. Returns findings with severity, confidence, category, file/line, and suggested verification. High-recall by design; absence of findings does not mean the code is safe. Pass inline `code` or a local `path` to a source file (the server reads the file itself); exactly one is required.
    """
    if path:
        if code.strip():
            raise ValueError("provide either inline code or a path, not both")
        code = read_file_text(path)
        if file_path == "untitled":
            file_path = path
    elif not code.strip():
        raise ValueError("either inline code or a path is required")
    user = (
        f"File: {file_path}\n"
        f"Language: {language or 'infer from content'}\n\n"
        f"Code:\n{code}"
    )
    raw = await _call_model(
        prompts.CODE_REVIEW_SYSTEM,
        user,
        max_tokens=min(4096, max(1024, 512 + len(code) // 10)),
        temperature=0.3,
    )
    data, ok = parse_json(raw)
    if ok and isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(
        {
            "findings": [],
            "summary": {
                "files_reviewed": 1,
                "findings_count": 0,
                "high_severity_count": 0,
            },
            "parse_error": raw,
        },
        ensure_ascii=False,
    )


def _naive_merge_packs(packs: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge chunk evidence packs without a final model pass (fallback)."""
    merged: dict[str, Any] = {
        "facts": [],
        "identifiers": [],
        "values": [],
        "constraints": [],
        "contradictions": [],
        "hypotheses": [],
        "unresolved_questions": [],
        "omitted": (
            "chunk packs were merged without a final model pass; duplicates may remain"
        ),
        "escalation": None,
    }
    seen: set[str] = set()
    omitted_notes: list[str] = []
    for pack in packs:
        for key in (
            "facts",
            "identifiers",
            "values",
            "constraints",
            "contradictions",
            "hypotheses",
            "unresolved_questions",
        ):
            items = pack.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                sig = (
                    json.dumps(item, ensure_ascii=False, sort_keys=True)
                    if isinstance(item, dict)
                    else str(item)
                )
                if sig not in seen:
                    seen.add(sig)
                    merged[key].append(item)
        note = pack.get("omitted")
        if isinstance(note, str) and note:
            omitted_notes.append(note)
    if omitted_notes:
        merged["omitted"] += " | " + " | ".join(omitted_notes)
    return merged


@mcp.tool()
async def compress_context(
    context: str = "",
    objective: str = "",
    preserve: list[str] | None = None,
    path: str = "",
) -> str:
    """Compress a large body of context into a compact, loss-aware evidence pack optimized for downstream primary-model reasoning.

    Preserves facts with provenance, identifiers, values, constraints, contradictions, hypotheses, and unresolved questions; explicitly notes what was omitted. Very large inputs are processed in chunks and merged. Use when context size is the constraint, not reasoning ability. Pass inline context or a local file `path` (the server reads the file itself); exactly one is required.
    """
    context = _resolve_input(context, path, text_label="context")
    extra = ""
    if preserve:
        extra = "Additionally preserve, when present: " + ", ".join(preserve) + ".\n"
    user = (
        f"Objective: {objective or 'preserve decision-relevant information'}\n"
        f"{extra}Context:\n{context}"
    )
    if len(context) <= CHUNK_CHARS:
        raw = await _call_model(
            prompts.COMPRESS_SYSTEM, user, max_tokens=2048, temperature=0.3
        )
        data, ok = parse_json(raw)
        if ok and isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False)
        return json.dumps({"parse_error": raw}, ensure_ascii=False)

    chunks = chunk_text(context, CHUNK_CHARS, CHUNK_OVERLAP)
    sem = asyncio.Semaphore(llm.CONCURRENCY)

    async def pack_chunk(index: int, chunk: str) -> dict[str, Any]:
        async with sem:
            chunk_user = (
                f"Objective: {objective or 'preserve decision-relevant information'}\n"
                f"Context (chunk {index + 1}/{len(chunks)}):\n{chunk}"
            )
            raw = await _call_model(
                prompts.COMPRESS_SYSTEM, chunk_user, max_tokens=2048, temperature=0.3
            )
            data, ok = parse_json(raw)
            if ok and isinstance(data, dict):
                return data
            return {"parse_error": raw, "chunk": index}

    packs = await asyncio.gather(*(pack_chunk(i, c) for i, c in enumerate(chunks)))
    valid = [p for p in packs if "parse_error" not in p]
    if not valid:
        return json.dumps(
            {"parse_error": packs[0]["parse_error"]}, ensure_ascii=False
        )
    merge_user = (
        f"Objective: {objective or 'preserve decision-relevant information'}\n\n"
        f"Evidence packs (in order):\n"
        + "\n---\n".join(json.dumps(p, ensure_ascii=False) for p in valid)
    )
    raw = await _call_model(
        prompts.COMPRESS_MERGE_SYSTEM, merge_user, max_tokens=2048, temperature=0.3
    )
    data, ok = parse_json(raw)
    if ok and isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(_naive_merge_packs(valid), ensure_ascii=False)


@mcp.tool()
async def ask(task: str, context: str = "", language: str = "en") -> str:
    """Send a direct natural-language task to the local model. General-purpose escape hatch for tasks that do not fit the specialized tools.

    Use only when no specialized tool matches; treat the response as a cheap secondary opinion (candidates, reasoning, assumptions, confidence, ambiguities), not an authoritative answer.
    """
    user = f"Task: {task}\nOutput language: {language}\n"
    if context:
        user += f"\nContext:\n{context}\n"
    return await _call_model(prompts.ASK_SYSTEM, user, temperature=0.5)


def main(argv: list[str] | None = None) -> None:
    """Run the FastMCP server (stdio transport by default)."""
    parser = argparse.ArgumentParser(
        prog="mcps",
        description="Local-AI MCP server: exposes a local LLM as agent-facing preprocessing tools.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="host for sse/streamable-http transports",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="port for sse/streamable-http transports",
    )
    args = parser.parse_args(argv)
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)
