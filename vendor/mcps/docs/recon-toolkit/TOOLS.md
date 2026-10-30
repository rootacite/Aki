# Recon MCP tools — descriptions

Source of truth for the text is `src/mcps/tools/recon/descriptions.py`. Phase 1 only.

All tools reject out-of-scope targets. Default IP scope includes `192.168.0.0/16`. Hostnames need `scope.allowed_domains`.

## `subfinder_enum`

Passive subdomain enumeration via public sources. Does not send traffic to the target's business ports.

When to use: first step of recon on an in-scope root domain.
When not to use: brute-force / graph-heavy mapping (use amass_passive or bbot_scan); active probing (use dnsx/httpx later).
Side effects: outbound HTTPS to third-party OSINT APIs; no packets to the domain's own ports.
Returns: JSON with a de-duplicated hostname list plus source metadata when present.
Out of scope targets are rejected.

Parameters:

- `domain` (str, required)
- `all_sources` (bool, default false) → `subfinder -all`
- `timeout_s` (int, default 120)
- `json_output` (bool, default true)

Argv: `subfinder -d DOMAIN -silent -oJ -duc -nc` (`-all` if requested).

## `dnsx_resolve`

Batch DNS resolution (A/AAAA/CNAME by default) with optional CDN hints. Low impact on the target.

When to use: after subdomain enum, before port scans, to collapse hosts that share an IP.
When not to use: zone transfers (AXFR is rejected); brute-forcing with a wordlist (not exposed).
Side effects: DNS queries only.
Returns: JSON records per host (answers, CDN when available).
Out of scope targets are rejected.

Parameters:

- `hosts` (list[str], required, capped by `max_hosts`)
- `records` (list[str], default `["A","AAAA","CNAME"]`)
- `timeout_s` (int, default 60)
- `json_output` (bool, default true)

Argv: `dnsx -l - -silent -j -cdn -nc -duc` plus record flags. Hosts on stdin.

## `amass_passive`

OWASP Amass v5 passive enumeration (`amass enum`, passive is the default). Slower and broader than subfinder.

When to use: you need more names than subfinder after a first pass.
When not to use: brute force / zone transfer / active cert grabs (`amass_active` is TODO). This tool never passes -active/-brute/-alts.
Side effects: OSINT API queries; enumeration artifacts go to the job dir via -dir/-log/-oA. Does not rewrite HOME; Amass keys come from the operator's real config. Long-running — returns a job_id.
Returns: `{job_id, status}` immediately; poll recon_job_status / recon_job_logs.
Out of scope targets are rejected.

Parameters:

- `domain` (str, required)
- `timeout_s` (int, default 300) → job

Argv: `amass enum -d DOMAIN -nocolor -rigid -timeout MINUTES -dir DIR -log DIR/amass.log -oA DIR/amass`. Isolation is the job dir flags, not a fake `$HOME`.

## `amass_intel`

Passive org/ASN/root-domain intel via Amass v5 `enum` (the v3 `intel` subcommand is gone). Used to refine scope, not to scan ports.

When to use: map an ASN or already in-scope domain before enumeration.
When not to use: port scanning; brute force. The `org` parameter is not supported on this Amass 5.1 CLI.
Side effects: OSINT queries; artifacts in the job dir via -dir/-log/-oA. Does not rewrite HOME.
Returns: `{job_id, status}` immediately.
Out of scope targets are rejected (domain and CIDR). ASN-only queries do not take a host target.

Parameters:

- `domain` (str | none)
- `org` (str | none) — rejected on this CLI
- `asn` (int | none)
- `timeout_s` (int, default 300) → job

At least domain or asn.

## `bbot_scan`

BBOT preset scan. Default is passive subdomain-enum (`-rf passive`). kitchen-sink is forbidden.

When to use: extra passive coverage after subfinder/amass, or cloud/email enum presets.
When not to use: kitchen-sink or unlisted presets; this environment may fail if bbot cannot create SemLocks.
Side effects: OSINT (passive_only) or more if passive_only is false; artifacts go to the job dir via -o. Does not rewrite HOME. Long-running job.
Returns: `{job_id, status}`; when complete, event-type counts plus up to N sample events (not the full log).
Out of scope targets are rejected.

Parameters:

- `target` (str, required)
- `preset`: `subdomain-enum` | `cloud-enum` | `email-enum` (default subdomain-enum)
- `passive_only` (bool, default true) → `-rf passive`
- `timeout_s` (int, default 600) → job

Argv: `bbot -t TARGET -p PRESET -y --no-deps --no-color -j -o DIR --force` plus `-rf passive` when requested. Isolation is `-o DIR`, not a fake `$HOME`.

## `recon_job_status` / `recon_job_logs` / `recon_job_cancel`

Job UX for long Phase 1 tools. See `descriptions.py`.

## `naabu_scan`

Fast port discovery. Does not do service identification (use nmap_scan) or HTTP (use httpx_probe).

When to use: after DNS resolution, to find open TCP ports on in-scope hosts.
When not to use: full-port or top-1000 scans without aggressive=true; service versioning.
Side effects: SYN (default, with sudo/cap when configured) or TCP connect probes. No nmap-cli passthrough.
Returns: JSON list of host:port plus scan_type_note (SYN may fall back to connect if unprivileged).
Out of scope targets are rejected.

Parameters:

- `targets` (list[str], required)
- `top_ports` (100 or 1000, default 100). `1000` requires `aggressive=true`
- `ports` (str | none) — mutually exclusive with top_ports; full-port requires `aggressive=true`
- `exclude_cdn` (bool, default true) → `-ec`
- `rate` (int, default 1000, capped by `naabu_max_rate`)
- `aggressive` (bool, default false)
- `scan_type`: `syn` | `connect` (default syn; falls back to connect without cap/sudo)
- `timeout_s` (int | none)

Argv: `naabu -l - -silent -json -nc -duc -Pn -tp 100 -ec -s s|c`. Never `-nmap-cli`.

## `nmap_scan`

Service identification on a small, already-narrowed target set. Not a network sweeper.

When to use: after naabu (or similar) reduced the set to a handful of hosts/ports.
When not to use: large CIDRs, -A, default vuln scripts, more than 16 targets.
Side effects: SYN or connect scan plus optional -sV/-sC; NSE scripts limited to http-/ssl-/tls-/ssh-/dns-/banner.
Returns: JSON hosts with open ports, service, product, version parsed from XML.
Out of scope targets are rejected.

Parameters:

- `targets` (list[str], max 16)
- `ports` (str | none; default `-F` top 100). Full-port specs rejected
- `service_version` (bool, default true) → `-sV`
- `default_scripts` (bool, default false) → `-sC`
- `scripts` (list[str] | none) — allowlist prefixes only
- `timing`: T2|T3|T4 (default T3)
- `skip_host_discovery` (bool, default true) → `-Pn`
- `scan_type`: syn|connect (default syn, same fallback as naabu)

Argv: `nmap -n -T3 --open -oX - -sS|-sT -Pn -sV -F …targets`. No `-A`.

## `httpx_probe`

HTTP liveness and basic fingerprinting (status, title, server, tech, CDN).

When to use: after naabu, as a filter before nuclei/ffuf/whatweb/jshunter. Accepts host, host:port, or URL.
When not to use: crawling (katana); dumping response bodies; screenshots.
Side effects: HTTP(S) requests; follows redirects by default (capped). This httpx-pd build has no -js flag.
Returns: JSON endpoints with url, status_code, title, webserver, tech, cdn, content_length, host, port, ip.
Out of scope targets are rejected.

Parameters:

- `targets` (list[str], required) — host, host:port, or URL
- `follow_redirects` (bool, default true)
- `tech_detect` (bool, default true)
- `timeout_s` (int, default 90)

Argv: `httpx` (resolved to `httpx-pd`) `-l - -silent -json -title -sc -cl -server -ip -cdn -td -fr -omit-body`.

## `whatweb_fingerprint`

Lightweight web fingerprint (WhatWeb aggression 1–3, default 1).

Max 20 URLs. Argv: `whatweb --aggression=N --color=never --log-json=- --quiet`.
Out of scope targets are rejected.

## `wafw00f_detect`

Identify a WAF in front of a single URL (wafw00f).

No custom header files. Argv: `wafw00f -f json -o - --no-colors URL`.
Out of scope targets are rejected.

## `katana_crawl`

In-scope crawl with JS URL harvest for jshunter.

Depth 1–3 (default 2). timeout_s>60 → job.
Argv: `katana -u URL -d N -jc -j -silent -fs rdn -mdp MAX -ct TIME`.
Out of scope targets are rejected.

## `jshunter_analyze`

Extract endpoints and (redacted) secrets from JavaScript (cc1a2b/jshunter).

Exactly one of `url` / `urls` / `file`. `file` must be under `js_workspace`. No `-H`/`-p`/webhooks.
Argv: `jshunter -q -j -o WORKSPACE/jshunter.json -u|-l|-f … -ep -s`.
Out of scope targets are rejected. Local files must be inside js_workspace.

## `restringer_deobfuscate`

Deobfuscate a workspace JS file with restringer (isolated-vm). Not a generic JS runtime.

Output path is forced under `js_workspace`. Vendor bundles (jquery/react/…) refused. No caller `-o`.
Out of scope targets are rejected.

## `nuclei_tech`

Nuclei templates tagged tech/waf/cdn at info/low severity. Template path is locked.

No user `-t`. `-pt http` and exclude code/javascript/headless/file. extra_tags are names only.
Out of scope targets are rejected.

## `nuclei_vuln`

Nuclei cve/misconfig/exposure at medium+ on live URLs. Always a job. Template path is locked.

Out of scope targets are rejected.

## `ffuf_fuzz`

Controlled ffuf (dir/vhost/param). Wordlist must sit under allowed prefixes.

URL host in scope. `dir`/`param` require `FUZZ` in the URL. Threads capped. Large lists → job.
Out of scope targets are rejected.

## `recon_domain_passive`

Passive subdomain enumeration then DNS resolution. Does not connect to business ports.

Out of scope targets are rejected.

## `recon_web_active`

Authorized active web pipeline: dnsx → naabu top100 → httpx → nuclei_tech (+ optional whatweb).

`authorize_active=true` required. Always a job. Out of scope targets are rejected.

## `recon_js_analyze`

Harvest JS (katana), jshunter endpoints/secrets, restringer only on obfuscated non-vendor files.

`authorize_active=true` required. Always a job. Out of scope targets are rejected.

## `recon_health`

Inventory recon binaries (including jshunter, restringer, node), scope, limits, js_workspace writability.
