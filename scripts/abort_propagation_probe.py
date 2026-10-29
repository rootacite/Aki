#!/usr/bin/env python3
"""Probe provider-stream abort propagation in the vendored Letta runtime.

Baseline mode reverse-applies the provider-abort patch to a temporary copy of
the source tree. A failing probe means the wrapper stream can be cancelled
without aborting the provider request. Patched mode runs the same probe
against the tree as-is and expects it to pass.

This probes the provider wrapper boundary only. The Aki proxy and llama-server
are covered separately by a direct client-disconnect test.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TREE = ROOT / "build" / "letta-code"
DEFAULT_PATCH = ROOT / "patches" / "letta-code" / "0006-aki-provider-abort.patch"
PROBE_PATH = Path("src/backend/dev/__abort_propagation_probe.test.ts")

PROBE = r'''
import { expect, test } from "bun:test";
import { ProviderTurnExecutor } from "./provider-turn-executor";
import type {
  ProviderStreamAdapter,
  ProviderTurnInput,
} from "./provider-turn-executor";

test("wrapper abort reaches provider signal", async () => {
  let captured: AbortSignal | undefined;
  const adapter: ProviderStreamAdapter = {
    async stream(input: ProviderTurnInput) {
      captured = (input as { abortSignal?: AbortSignal }).abortSignal;
      return (async function* () {
        yield {
          type: "letta-chunk",
          chunk: { message_type: "assistant_message", content: [] },
        } as never;
        await new Promise<void>((resolve) => {
          if (captured?.aborted) return resolve();
          captured?.addEventListener("abort", () => resolve(), { once: true });
          setTimeout(resolve, 200);
        });
        yield {
          type: "letta-chunk",
          chunk: { message_type: "stop_reason", stop_reason: "end_turn" },
        } as never;
      })();
    },
  };
  const executor = new ProviderTurnExecutor(adapter);
  const stream = await executor.execute({
    conversationId: "default",
    agentId: "agent-probe",
    agent: {
      id: "agent-probe",
      name: "Probe",
      system: "system",
      tags: [],
      model: "probe/model",
      model_settings: {},
    },
    body: { messages: [] } as never,
    history: [],
    uiMessages: [],
  } as never);
  const iterator = stream[Symbol.asyncIterator]();
  await iterator.next();
  expect(captured).toBeDefined();
  stream.controller.abort();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(captured?.aborted).toBe(true);
  await iterator.return?.();
});
'''


def bun_path() -> Path:
    lock = json.loads((ROOT / "vendor" / "letta-code.lock.json").read_text(encoding="utf-8"))
    version = str((lock.get("build") or {}).get("bun_version") or "")
    candidate = Path.home() / ".cache" / "aki" / "toolchain" / "bun" / version / "bun"
    if candidate.is_file():
        return candidate
    found = shutil.which("bun")
    if found:
        return Path(found)
    raise SystemExit("bun not found; run scripts/build-letta.py once to populate the toolchain")


def stage_tree(tree: Path, patch: Path, reverse: bool) -> Path:
    if not tree.is_dir():
        raise SystemExit(f"source tree not found: {tree}")
    if not (tree / "node_modules").is_dir():
        raise SystemExit(f"node_modules not found under {tree}")
    temp = Path(tempfile.mkdtemp(prefix="letta-abort-probe-"))
    try:
        subprocess.run(
            [
                "rsync",
                "-a",
                "--exclude",
                "node_modules",
                "--exclude",
                ".git",
                "--exclude",
                "dist",
                "--exclude",
                "letta.js",
                f"{tree}/",
                f"{temp}/",
            ],
            check=True,
        )
        (temp / "node_modules").symlink_to(
            tree / "node_modules", target_is_directory=True
        )
        if reverse:
            if not patch.is_file():
                raise SystemExit(f"patch not found: {patch}")
            result = subprocess.run(
                ["patch", "-R", "-p1", "-d", str(temp), "-i", str(patch)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if result.returncode != 0:
                raise SystemExit(f"cannot reverse-apply {patch.name}:\n{result.stdout}")
        probe = temp / PROBE_PATH
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(PROBE, encoding="utf-8")
        return temp
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def run_probe(tree: Path, bun: Path) -> tuple[int, str]:
    result = subprocess.run(
        [str(bun), "test", str(PROBE_PATH)],
        cwd=str(tree),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.returncode, result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=DEFAULT_TREE)
    parser.add_argument("--patch", type=Path, default=DEFAULT_PATCH)
    parser.add_argument(
        "--mode",
        choices=("baseline", "patched", "both"),
        default="both",
    )
    args = parser.parse_args()
    modes = ("baseline", "patched") if args.mode == "both" else (args.mode,)
    bun = bun_path()
    ok = True
    for mode in modes:
        temp = stage_tree(args.tree, args.patch, reverse=mode == "baseline")
        try:
            code, output = run_probe(temp, bun)
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        print(f"== {mode} ==")
        print(output.rstrip())
        if mode == "baseline":
            if code == 0:
                print("UNEXPECTED: baseline passed; the abort bug was not reproduced")
                ok = False
            else:
                print("REPRODUCED: wrapper abort did not reach the provider signal")
        else:
            if code == 0:
                print("FIXED: wrapper abort reached the provider signal")
            else:
                print("STILL BROKEN: patched probe failed")
                ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
