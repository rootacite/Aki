# Agent playbook — recon MCP

Authorized testing only. Fill `config/recon.yaml` scope before scanning. Out-of-scope targets are rejected.

Suggested order:

1. `recon_health` — binaries, scope, js_workspace.
2. **Passive** `recon_domain_passive` (or `subfinder_enum` → `dnsx_resolve`). No business-port traffic.
3. **Active web** only with `authorize_active=true`: `recon_web_active` (dnsx → naabu top100 → httpx → nuclei_tech). Do not put nuclei_vuln/ffuf on this path.
4. Fingerprint live URLs: `whatweb_fingerprint`, `wafw00f_detect`.
5. Collect JS: `katana_crawl` then `jshunter_analyze` (`timeout_s` ≤60 if you want a sync result).
6. Obfuscated business bundles only: `restringer_deobfuscate` (isolated-vm). Skip jquery/react vendor files.
7. Long jobs: `recon_job_status` / `recon_job_logs` / `recon_job_cancel`. Pipeline tools stash output in `pipeline_result`.

Lab targets currently in default scope: `192.168.0.0/16`, `127.0.0.1/32`, `localhost`, `git.acitelight.top`.
