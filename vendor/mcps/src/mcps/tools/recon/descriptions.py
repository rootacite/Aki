"""Tool descriptions. Keep in sync with docs/recon-toolkit/TOOLS.md."""

SUBFINDER_ENUM = """\
Passive subdomain enumeration via public sources. Does not send traffic to the target's business ports.

When to use: first step of recon on an in-scope root domain.
When not to use: brute-force / graph-heavy mapping (use amass_passive or bbot_scan); active probing (use dnsx/httpx later).
Side effects: outbound HTTPS to third-party OSINT APIs; no packets to the domain's own ports.
Returns: JSON with a de-duplicated hostname list plus source metadata when present.
Out of scope targets are rejected.
"""

DNSX_RESOLVE = """\
Batch DNS resolution (A/AAAA/CNAME by default) with optional CDN hints. Low impact on the target.

When to use: after subdomain enum, before port scans, to collapse hosts that share an IP.
When not to use: zone transfers (AXFR is rejected); brute-forcing with a wordlist (not exposed).
Side effects: DNS queries only.
Returns: JSON records per host (answers, CDN when available).
Out of scope targets are rejected.
"""

AMASS_PASSIVE = """\
OWASP Amass v5 passive enumeration (`amass enum`, passive is the default). Slower and broader than subfinder.

When to use: you need more names than subfinder after a first pass.
When not to use: brute force / zone transfer / active cert grabs (`amass_active` is TODO). This tool never passes -active/-brute/-alts.
Side effects: OSINT API queries; enumeration artifacts go to the job dir via -dir/-log/-oA. Does not rewrite HOME; Amass keys come from the operator's real config. Long-running — returns a job_id.
Returns: {job_id, status} immediately; poll recon_job_status / recon_job_logs.
Out of scope targets are rejected.
"""

AMASS_INTEL = """\
Passive org/ASN/root-domain intel via Amass v5 `enum` (the v3 `intel` subcommand is gone). Used to refine scope, not to scan ports.

When to use: map an ASN or already in-scope domain before enumeration.
When not to use: port scanning; brute force. The `org` parameter is not supported on this Amass 5.1 CLI.
Side effects: OSINT queries; artifacts in the job dir via -dir/-log/-oA. Does not rewrite HOME.
Returns: {job_id, status} immediately.
Out of scope targets are rejected (domain and CIDR). ASN-only queries do not take a host target.
"""

BBOT_SCAN = """\
BBOT preset scan. Default is passive subdomain-enum (`-rf passive`). kitchen-sink is forbidden.

When to use: extra passive coverage after subfinder/amass, or cloud/email enum presets.
When not to use: kitchen-sink or unlisted presets; this environment may fail if bbot cannot create SemLocks.
Side effects: OSINT (passive_only) or more if passive_only is false; artifacts go to the job dir via -o. Does not rewrite HOME. Long-running job.
Returns: {job_id, status}; when complete, event-type counts plus up to N sample events (not the full log).
Out of scope targets are rejected.
"""

NAABU_SCAN = """\
Fast port discovery. Does not do service identification (use nmap_scan) or HTTP (use httpx_probe).

When to use: after DNS resolution, to find open TCP ports on in-scope hosts.
When not to use: full-port or top-1000 scans without aggressive=true; service versioning.
Side effects: SYN (default, with sudo/cap when configured) or TCP connect probes. No nmap-cli passthrough.
Returns: JSON list of host:port plus scan_type_note (SYN may fall back to connect if unprivileged).
Out of scope targets are rejected.
"""

NMAP_SCAN = """\
Service identification on a small, already-narrowed target set. Not a network sweeper.

When to use: after naabu (or similar) reduced the set to a handful of hosts/ports.
When not to use: large CIDRs, -A, default vuln scripts, more than 16 targets.
Side effects: SYN or connect scan plus optional -sV/-sC; NSE scripts limited to http-/ssl-/tls-/ssh-/dns-/banner.
Returns: JSON hosts with open ports, service, product, version parsed from XML.
Out of scope targets are rejected.
"""

HTTPX_PROBE = """\
HTTP liveness and basic fingerprinting (status, title, server, tech, CDN).

When to use: after naabu, as a filter before nuclei/ffuf/whatweb/jshunter. Accepts host, host:port, or URL.
When not to use: crawling (katana); dumping response bodies; screenshots.
Side effects: HTTP(S) requests; follows redirects by default (capped). This httpx-pd build has no -js flag.
Returns: JSON endpoints with url, status_code, title, webserver, tech, cdn, content_length, host, port, ip.
Out of scope targets are rejected.
"""

WHATWEB_FINGERPRINT = """\
Lightweight web fingerprint (WhatWeb aggression 1–3, default 1).

When to use: after httpx, to name CMS/libs on a few live URLs (max 20).
When not to use: aggression 4 / heavy plugin crawls; large URL lists.
Side effects: HTTP requests (level 1 is one request plus redirects).
Returns: JSON plugin matches per URL.
Out of scope targets are rejected.
"""

WAFW00F_DETECT = """\
Identify a WAF in front of a single URL (wafw00f).

When to use: before noisy scans, to know if a WAF will distort results.
When not to use: as a substitute for nuclei/httpx; do not pass custom header files.
Side effects: a handful of HTTP probes that may look like attacks to a WAF.
Returns: JSON with detected WAF name(s) when possible.
Out of scope targets are rejected.
"""

KATANA_CRAWL = """\
In-scope crawl with JS URL harvest for jshunter.

When to use: after httpx, to collect same-site links and .js URLs.
When not to use: headless/hybrid chrome; unrestricted depth; off-site crawling.
Side effects: HTTP GET crawl; default depth 2 (max 3); may run as a job when timeout_s>60.
Returns: urls plus js_urls (deduped).
Out of scope targets are rejected.
"""

JSHUNTER_ANALYZE = """\
Extract endpoints and (redacted) secrets from JavaScript (cc1a2b/jshunter).

When to use: after katana/httpx produced JS URLs or workspace files.
When not to use: as a crawler; default deobfuscation (use restringer); custom headers/proxies/webhooks.
Side effects: HTTP GET of the JS if url/urls; writes JSON under js_workspace. urls>10 or timeout_s>60 → job.
Returns: endpoints, redacted secrets, flags, json path.
Out of scope targets are rejected. Local files must be inside js_workspace.
"""

RESTRINGER_DEOBFUSCATE = """\
Deobfuscate a workspace JS file with restringer (isolated-vm). Not a generic JS runtime.

When to use: a clearly obfuscated business bundle already on disk (or source_url in scope).
When not to use: jquery/react/vue vendor bundles; arbitrary -o paths; huge files.
Side effects: isolated-vm execution with size, iteration, and timeout caps; output only under js_workspace.
Returns: output path, whether the script changed, truncated preview, sizes.
Out of scope targets are rejected.
"""

NUCLEI_TECH = """\
Nuclei templates tagged tech/waf/cdn at info/low severity. Template path is locked.

When to use: after httpx, for technology/WAF/CDN hints.
When not to use: vulnerability hunting (nuclei_vuln); custom -t paths; code/js/file protocols.
Side effects: HTTP template requests. extra_tags are name-only (no paths).
Returns: JSONL findings (template-id, url, info).
Out of scope targets are rejected.
"""

NUCLEI_VULN = """\
Nuclei cve/misconfig/exposure at medium+ on live URLs. Always a job. Template path is locked.

When to use: after tech fingerprint, on a short list of in-scope URLs.
When not to use: nuclei -code, custom templates, file:// targets, huge URL lists.
Side effects: active vulnerability templates. Concurrency capped.
Returns: {job_id, status}.
Out of scope targets are rejected.
"""

FFUF_FUZZ = """\
Controlled ffuf (dir/vhost/param). Wordlist must sit under allowed prefixes.

When to use: after httpx, for a single in-scope FUZZ URL with a known-good wordlist.
When not to use: wordlists in /tmp or home; URL without FUZZ (except vhost Host:FUZZ); high threads.
Side effects: many HTTP requests. Large wordlists run as a job.
Returns: JSON matches (url, status, length) or a job_id.
Out of scope targets are rejected.
"""

RECON_DOMAIN_PASSIVE = """\
Passive subdomain enumeration then DNS resolution. Does not connect to business ports.

When to use: first recon on an in-scope domain.
When not to use: port/HTTP scans (recon_web_active). Amass is not waited on inline.
Side effects: OSINT queries and DNS lookups.
Returns: hostnames, dnsx records, failed_steps.
Out of scope targets are rejected.
"""

RECON_WEB_ACTIVE = """\
Authorized active web pipeline: dnsx → naabu top100 → httpx → nuclei_tech (+ optional whatweb).

When to use: after passive recon, with authorize_active=true.
When not to use: without authorization; nuclei_vuln/ffuf (not in this pipeline).
Side effects: port scan and HTTP. Always a job.
Returns: {job_id, status}; poll recon_job_status for pipeline_result.
Out of scope targets are rejected.
"""

RECON_JS_ANALYZE = """\
Harvest JS (katana), jshunter endpoints/secrets, restringer only on obfuscated non-vendor files.

When to use: after httpx found a live site. authorize_active=true required.
When not to use: as a generic crawler of vendor CDNs.
Side effects: HTTP crawl and JS downloads into js_workspace. Always a job.
Returns: {job_id}; pipeline_result has js_urls, endpoints, redacted secrets, deob paths.
Out of scope targets are rejected.
"""

RECON_HEALTH = """\
Inventory recon binaries (including jshunter, restringer, node), scope, limits, js_workspace writability.

When to use: before a recon session.
When not to use: as a scan. Does not probe third-party API keys.
Side effects: none beyond a tiny workspace probe file.
Returns: JSON inventory.
"""

RECON_JOB_STATUS = """\
Poll a recon background job started by a long-running tool (amass, bbot, later nuclei/ffuf).

When to use: after a tool returned {job_id, status: running}.
When not to use: one-shot tools that already returned results.
Side effects: none (reads job metadata only).
Returns: status, redacted argv, optional RunResult.
"""

RECON_JOB_LOGS = """\
Tail stdout/stderr captured for a recon job.

When to use: diagnose a running or failed job.
When not to use: as a substitute for structured tool output.
Side effects: none.
Returns: last `tail` log lines (secrets in argv already redacted).
"""

RECON_JOB_CANCEL = """\
Cancel a running recon job and kill its process group.

When to use: the job is stuck or no longer needed.
When not to use: finished jobs (no-op besides reporting current status).
Side effects: SIGTERM then SIGKILL on the job process group.
Returns: updated job status.
"""
