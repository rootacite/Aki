#!/usr/bin/env bash
# One-command deployment for the Aki gateway.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
TIMEOUT="${AKI_DEPLOY_TIMEOUT:-60}"
DEPLOY_STARTED_EPOCH="$(date +%s)"

DRY_RUN=0
PULL=0
START_STOPPED=0
SYNC_PERSONAS=1
RESTART=1
INSTALL_UNITS=1
PROFILES=()

log() {
    printf '[deploy] %s\n' "$*"
}

warn() {
    printf '[deploy] warning: %s\n' "$*" >&2
}

die() {
    printf '[deploy] error: %s\n' "$*" >&2
    exit 1
}

trap 'status=$?; if ((status != 0)); then printf "[deploy] failed with exit code %s\n" "$status" >&2; fi' EXIT

active_akid_units() {
    if ((${#PROFILES[@]})); then
        local profile unit
        for profile in "${PROFILES[@]}"; do
            for unit in "akid@${profile}.service" "akid-proxy@${profile}.service"; do
                if systemctl --user is-active --quiet "$unit" 2>/dev/null; then
                    printf '%s\n' "$unit"
                fi
            done
        done
        return 0
    fi
    systemctl --user list-units \
        --type=service \
        --state=running \
        --no-legend \
        --plain \
        'akid@*.service' \
        'akid-proxy@*.service' \
        2>/dev/null | awk 'NF { print $1 }'
}

unit_enter_monotonic() {
    systemctl --user show "$1" -p ActiveEnterTimestampMonotonic --value 2>/dev/null \
        | tr -d '[:space:]'
}

verify_restarted_units() {
    if ((${#units_before[@]} == 0)); then
        warn "no running akid units found before restart; start-time verification skipped"
        return 0
    fi
    local unit state before after
    for unit in "${!units_before[@]}"; do
        state="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
        [[ "$state" == "active" ]] || die "$unit is not active after restart (state: ${state:-unknown})"
        before="${units_before[$unit]}"
        after="$(unit_enter_monotonic "$unit")"
        [[ "$before" =~ ^[0-9]+$ ]] || die "cannot read pre-restart timestamp for $unit"
        [[ "$after" =~ ^[0-9]+$ ]] || die "cannot read post-restart timestamp for $unit"
        ((after > before)) || die "$unit did not restart (ActiveEnterTimestampMonotonic did not advance)"
        log "$unit restarted; start timestamp advanced"
    done
}

report_recent_journal() {
    if ! command -v journalctl >/dev/null 2>&1; then
        warn "journalctl not available; recent-log check skipped"
        return 0
    fi
    local unit output
    local journal_args=()
    for unit in "${!units_before[@]}"; do
        journal_args+=(-u "$unit")
    done
    ((${#journal_args[@]})) || return 0
    output="$(journalctl --user --since "@$DEPLOY_STARTED_EPOCH" -p warning \
        --no-pager -q -o cat "${journal_args[@]}" 2>/dev/null | tail -n 40 || true)"
    if [[ -n "$output" ]]; then
        warn "recent journal warnings/errors since deployment start:"
        printf '%s\n' "$output" >&2
    else
        log "no journal warning/error entries since deployment start"
    fi
}

usage() {
    cat <<'EOF'
Usage: ./deploy.sh [options] [profile ...]

Deploy Aki from this checkout:
  1. optionally pull the current branch with --ff-only
  2. run install-daemon.py to prepare the vendored runtime and systemd units
  3. sync persona templates with scripts/sync-personas.py
  4. restart running profiles with aki-safectl.py
  5. verify service start times and recent journal warnings
  6. run the aki-safectl.py health/status check

Options:
  --profile NAME       deploy only this profile; may be repeated
  --timeout SECONDS    readiness timeout for aki-safectl (default: 60)
  --dry-run            show pending sync/restart work without changing anything
  --pull               run `git pull --ff-only` first; requires a clean worktree
  --start              also start profiles that are currently stopped
  --install            run install-daemon.py (default; kept for explicitness)
  --no-install         skip install-daemon.py and only check the runtime
  --no-sync            skip persona synchronization
  --no-restart         skip service restarts and starts
  -h, --help           show this help

Environment:
  PYTHON               Python interpreter (default: python3)
  AKI_DEPLOY_TIMEOUT   default readiness timeout in seconds

Examples:
  ./deploy.sh --dry-run
  ./deploy.sh
  ./deploy.sh --profile aki --profile fuyu
  ./deploy.sh --pull --start
EOF
}

while (($#)); do
    case "$1" in
        --profile)
            (($# >= 2)) || die "--profile requires a value"
            PROFILES+=("$2")
            shift 2
            ;;
        --profile=*)
            value="${1#*=}"
            [[ -n "$value" ]] || die "--profile requires a value"
            PROFILES+=("$value")
            shift
            ;;
        --timeout)
            (($# >= 2)) || die "--timeout requires a value"
            TIMEOUT="$2"
            shift 2
            ;;
        --timeout=*)
            TIMEOUT="${1#*=}"
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --pull)
            PULL=1
            shift
            ;;
        --start|--start-stopped)
            START_STOPPED=1
            shift
            ;;
        --install)
            INSTALL_UNITS=1
            shift
            ;;
        --no-install)
            INSTALL_UNITS=0
            shift
            ;;
        --no-sync)
            SYNC_PERSONAS=0
            shift
            ;;
        --no-restart)
            RESTART=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --*)
            die "unknown option: $1"
            ;;
        *)
            PROFILES+=("$1")
            shift
            ;;
    esac
done

[[ "$TIMEOUT" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "--timeout must be a positive number"
[[ ! "$TIMEOUT" =~ ^0+([.]0+)?$ ]] || die "--timeout must be greater than zero"

((EUID != 0)) || die "run as the service user, not root"
command -v "$PYTHON" >/dev/null 2>&1 || die "python interpreter not found: $PYTHON"

for required in \
    scripts/build-letta.py \
    scripts/sync-personas.py \
    install-daemon.py \
    aki-safectl.py; do
    [[ -f "$ROOT/$required" ]] || die "missing required file: $required"
done

profile_args=()
for profile in "${PROFILES[@]}"; do
    [[ -n "$profile" ]] || die "profile name must not be empty"
    profile_args+=(--profile "$profile")
done

if ((PULL)); then
    if ((DRY_RUN)); then
        log "would pull current branch with --ff-only"
    else
        log "pulling current branch"
        if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]]; then
            die "--pull requires a clean tracked worktree"
        fi
        git -C "$ROOT" pull --ff-only
    fi
fi

if ((INSTALL_UNITS)); then
    if ((DRY_RUN)); then
        log "would run install-daemon.py to refresh the runtime and systemd units"
    else
        log "installing daemon runtime and systemd units"
        "$PYTHON" install-daemon.py
    fi
else
    log "checking vendored Letta runtime (install-daemon.py skipped)"
    if ! "$PYTHON" scripts/build-letta.py --check; then
        if ((DRY_RUN)); then
            warn "runtime check failed; a real deployment would rebuild it"
        else
            warn "vendored Letta runtime is missing or stale; rebuilding"
            "$PYTHON" scripts/build-letta.py --build --install
            "$PYTHON" scripts/build-letta.py --check
        fi
    fi
fi

if ((SYNC_PERSONAS)); then
    sync_args=(scripts/sync-personas.py)
    sync_args+=("${profile_args[@]}")
    if ((START_STOPPED)); then
        sync_args+=(--start)
    fi
    if ((DRY_RUN)); then
        sync_args+=(--dry-run)
    fi
    log "syncing persona templates"
    "$PYTHON" "${sync_args[@]}"
fi

if ((RESTART)); then
    declare -A units_before=()
    if ((!DRY_RUN)); then
        while IFS= read -r unit; do
            [[ -n "$unit" ]] || continue
            units_before["$unit"]="$(unit_enter_monotonic "$unit")"
        done < <(active_akid_units)
    fi

    restart_args=(aki-safectl.py restart)
    restart_args+=("${profile_args[@]}")
    restart_args+=(--timeout "$TIMEOUT")
    if ((DRY_RUN)); then
        restart_args+=(--dry-run)
    fi
    log "restarting running profiles"
    "$PYTHON" "${restart_args[@]}"

    if ((START_STOPPED)); then
        start_args=(aki-safectl.py start)
        start_args+=("${profile_args[@]}")
        start_args+=(--timeout "$TIMEOUT")
        if ((DRY_RUN)); then
            start_args+=(--dry-run)
        fi
        log "starting any stopped profiles"
        "$PYTHON" "${start_args[@]}"
    fi

    if ((!DRY_RUN)); then
        verify_restarted_units
        report_recent_journal
    fi
fi

status_args=(aki-safectl.py status)
status_args+=("${profile_args[@]}")
log "checking profile health and status"
"$PYTHON" "${status_args[@]}"

if ((DRY_RUN)); then
    log "dry run complete; no changes made"
else
    log "deployment complete"
fi
