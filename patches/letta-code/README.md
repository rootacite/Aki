# Letta Code patches

Upstream source lives in `vendor/letta-code` and is pinned by
`vendor/letta-code.lock.json`. Patches are applied to a disposable copy under
`build/letta-code/`; the submodule itself must stay clean.

Application order is lexical. `scripts/build-letta.py --build` applies every
`*.patch` with `patch -p1`, then builds the runtime. After changing a patch,
run:

```bash
python3 scripts/build-letta.py --update-lock
python3 scripts/build-letta.py --build --install --check
```

Patch rules:

- Keep patches small and anchored to code that is stable in the pinned tag.
- Add or update the corresponding test in the patch series.
- Never patch `node_modules` directly; use the upstream build/postinstall flow.
- When bumping Letta, apply patches to the new tag, resolve conflicts, update
  the lock file, rebuild, and run both the Letta tests and the Python suite.
