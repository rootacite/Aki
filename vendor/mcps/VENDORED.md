# Vendored local recon MCP

This directory contains the local recon/EDR MCP server used by Aki, copied from the
operator's `~/Deeppin/mcps` working tree (upstream commit `ee24906` plus the local
`httpx`/proxy and scope fixes).

The runtime virtual environment is generated, not committed:

```bash
uv sync --project vendor/mcps --frozen
python3 scripts/install-local-mcp.py --profile natsu
```

The MCP server command is `vendor/mcps/.venv/bin/mcps`; recon scope and limits live in
`config/recon.yaml`. Natsu enables the `local` server by default; Aki, Fuyu, and Haru
keep it excluded unless their `[mcp] excluded` setting is changed.
