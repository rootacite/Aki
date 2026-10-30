#!/usr/bin/env bash
# Inventory recon CLIs on PATH. No scanning, no installs, no network probes.
set -euo pipefail
# Do not inherit interactive aliases (e.g. gau='git add --update').
unalias -a 2>/dev/null || true

pd_version_line() {
  # ProjectDiscovery tools print a banner then "[INF] Current Version: ..."
  awk '
    /Current Version:/ { print; found=1; exit }
    /Nuclei Engine Version:/ { print; found=1; exit }
    /version:/ { print; found=1; exit }
    /^v[0-9]/ { print; found=1; exit }
    /WhatWeb version/ { print; found=1; exit }
    /Nmap version/ { print; found=1; exit }
    /Masscan version/ { print; found=1; exit }
    /ffuf version/ { print; found=1; exit }
  ' <<<"$1"
  [[ -n "${found:-}" ]] || true
}

first_nonempty() {
  awk 'NF { print; exit }' <<<"$1" | cut -c1-240
}

try_version() {
  local bin="$1" path="$2"
  local flags=() flag out line
  case "$bin" in
    nmap) flags=(--version) ;;
    whatweb) flags=(--version) ;;
    ffuf) flags=(-V) ;;
    restringer) flags=(-V --version) ;;
    wafw00f) flags=(--version -V) ;;
    masscan) flags=(--version) ;;
    theHarvester|theharvester) flags=(--version -V) ;;
    waybackurls) flags=(--version -h) ;;
    bbot) flags=(--version) ;;
    amass) flags=(-version version --version) ;;
    httpx) flags=(-version --version) ;;
    subfinder|dnsx|naabu|nuclei|katana) flags=(-version) ;;
    jshunter) flags=(-h) ;;
    *) flags=(-version --version -V) ;;
  esac
  for flag in "${flags[@]}"; do
    out="$(timeout 3s "$path" "$flag" 2>&1 || true)"
    line="$(pd_version_line "$out")"
    if [[ -n "$line" ]]; then
      printf '%s' "$line"
      return 0
    fi
    if [[ -n "${out//[$'\t\r\n ']/}" ]]; then
      # keep going unless this is the last flag
      :
    fi
  done
  # last-resort: first non-empty line of last output
  if [[ -n "${out:-}" ]]; then
    first_nonempty "$out"
  else
    echo "(no version string)"
  fi
}

pacman_info() {
  local path="$1"
  if ! command -v pacman >/dev/null 2>&1; then
    echo "n/a (no pacman)"
    return
  fi
  if pacman -Qqo "$path" >/dev/null 2>&1; then
    local pkg
    pkg="$(pacman -Qqo "$path" 2>/dev/null | head -n1)"
    local ver
    ver="$(pacman -Qi "$pkg" 2>/dev/null | awk '/^Version/ { sub(/^Version[[:space:]]*:[[:space:]]*/, ""); print; exit }')"
    printf '%s %s' "$pkg" "$ver"
  else
    echo "not owned by pacman"
  fi
}

jshunter_fingerprint() {
  local path="$1"
  local help
  help="$(timeout 3s "$path" -h 2>&1 || true)"
  local identity="UNKNOWN"
  if grep -qE 'Created by cc1a2b' <<<"$help"; then
    identity="MATCH github.com/cc1a2b/jshunter (banner: Created by cc1a2b)"
  elif grep -qE -- '-ep' <<<"$help" && grep -qE -- '-u' <<<"$help" && grep -qE -- '-f' <<<"$help"; then
    identity="LIKELY github.com/cc1a2b/jshunter (help has -u/-f/-ep)"
  else
    identity="NOT cc1a2b/jshunter (help missing -u/-f/-s/-ep). Possible PyPI/other mass-scanner. Do not wrap this binary."
  fi
  echo "$identity"
  echo "flags: $(grep -oE -- '-u,|--url|-l,|--list|-f,|--file|-s,|--secrets|-ep,|--end-point' <<<"$help" | tr '\n' ' ')"
  echo "---- jshunter -h (first 24 lines) ----"
  printf '%s\n' "$help" | head -n 24
}

httpx_identity() {
  local path="$1"
  local help
  help="$(timeout 3s "$path" -h 2>&1 || true)"
  help+=$'\n'"$(timeout 3s "$path" --help 2>&1 || true)"
  if grep -qi 'projectdiscovery' <<<"$help"; then
    echo "MATCH projectdiscovery/httpx"
  elif grep -qiE 'httpx \[OPTIONS\] URL|A next generation HTTP client|Usage: httpx' <<<"$help"; then
    echo "WRONG BINARY: Python encode/httpx CLI, not projectdiscovery/httpx. Look for httpx-pd."
  else
    echo "UNKNOWN httpx; inspect help before wrapping."
  fi
}

REQUIRED=(
  subfinder dnsx naabu httpx nuclei nmap whatweb ffuf jshunter restringer
)
OPTIONAL=(
  amass bbot katana wafw00f masscan theHarvester gau waybackurls
)

echo "=== recon bin inventory ==="
echo "host: $(uname -srm)"
echo "date: $(date -Iseconds)"
echo "user: $(id)"
echo "PATH: $PATH"
if command -v pacman >/dev/null 2>&1; then
  echo "pacman: present"
else
  echo "pacman: not found"
fi
if command -v node >/dev/null 2>&1; then
  echo "node: $(node -v 2>/dev/null || true)  ($(command -v node))"
else
  echo "node: not on PATH (restringer needs Node 20+)"
fi
echo

check_one() {
  local kind="$1" bin="$2"
  local path=""
  if path="$(command -v "$bin" 2>/dev/null)"; then
    # command -v can still return a function; reject non-executables
    if [[ ! -x "$path" ]]; then
      printf '[%s] %-14s NOT-EXEC  command -v => %s\n' "$kind" "$bin" "$path"
      return 1
    fi
    local ver pkg
    ver="$(try_version "$bin" "$path")"
    pkg="$(pacman_info "$path")"
    printf '[%s] %-14s FOUND  path=%s\n' "$kind" "$bin" "$path"
    printf '         version: %s\n' "$ver"
    printf '         pacman:  %s\n' "$pkg"
    case "$bin" in
      jshunter)
        echo "         identity:"
        jshunter_fingerprint "$path" | sed 's/^/           /'
        ;;
      httpx)
        echo "         identity: $(httpx_identity "$path")"
        if command -v httpx-pd >/dev/null 2>&1; then
          local pdpath pdver pdpkg
          pdpath="$(command -v httpx-pd)"
          pdver="$(try_version httpx "$pdpath")"
          pdpkg="$(pacman_info "$pdpath")"
          printf '         httpx-pd: path=%s\n' "$pdpath"
          printf '                   version: %s\n' "$pdver"
          printf '                   pacman:  %s\n' "$pdpkg"
          echo "                   identity: $(httpx_identity "$pdpath")"
        else
          echo "         httpx-pd: not on PATH"
        fi
        ;;
    esac
    return 0
  else
    printf '[%s] %-14s MISSING\n' "$kind" "$bin"
    if [[ "$bin" == "httpx" ]] && command -v httpx-pd >/dev/null 2>&1; then
      echo "         note: httpx-pd exists at $(command -v httpx-pd)"
    fi
    if [[ "$bin" == "katana" ]] && command -v katana-pd >/dev/null 2>&1; then
      echo "         note: katana-pd exists at $(command -v katana-pd)"
    fi
    return 1
  fi
}

req_missing=0
opt_missing=0

echo "--- required (missing => tool registers unavailable; do not crash server) ---"
for b in "${REQUIRED[@]}"; do
  if ! check_one "REQ" "$b"; then
    req_missing=$((req_missing + 1))
  fi
done

echo
echo "--- optional (missing => tool returns a clear error) ---"
for b in "${OPTIONAL[@]}"; do
  if ! check_one "OPT" "$b"; then
    opt_missing=$((opt_missing + 1))
  fi
done

echo
echo "--- name collisions / extra binaries ---"
if command -v httpx-pd >/dev/null 2>&1; then
  echo "httpx-pd: $(command -v httpx-pd)"
else
  echo "httpx-pd: MISSING"
fi
if command -v katana-pd >/dev/null 2>&1; then
  echo "katana-pd: $(command -v katana-pd)"
else
  echo "katana-pd: MISSING"
fi
echo "gau alias in this shell: $(alias gau 2>/dev/null || echo none)"
echo

echo "--- nmap/naabu/masscan privileges (no scan) ---"
for p in nmap naabu masscan; do
  if command -v "$p" >/dev/null 2>&1; then
    bp="$(command -v "$p")"
    echo "$bp  mode=$(stat -c '%a' "$bp" 2>/dev/null || echo ?)  caps=$(getcap "$bp" 2>/dev/null || echo none)"
  fi
done
echo "sudo -n: $(sudo -n true 2>&1 | head -n1 || true)"
echo

echo "=== summary ==="
echo "required missing: $req_missing / ${#REQUIRED[@]}"
echo "optional missing: $opt_missing / ${#OPTIONAL[@]}"
echo "no targets scanned; no packages installed."
exit 0
