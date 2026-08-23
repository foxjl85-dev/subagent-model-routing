#!/usr/bin/env bash
# codex-shim.sh - standalone transport shim: one-shot `codex exec` dispatch.
#
# Usage:
#   codex-shim.sh <prompt-source> [extra codex-exec args]
#     <prompt-source>  filepath, or "-" to read the prompt from stdin
#
# Contract:
#   - stdout = codex output; exit code = codex's exit code
#   - the LAST stdout line is always "SHIM-DONE exit=<n>" on its own line (a preceding blank line may appear)
#   - appends started/finished JSONL records to the routing ledger
#   - dispatch-level failures before a run begins (usage errors, missing timeout binary) emit only the sentinel, no ledger records
#
# Env:
#   SHIM_TIMEOUT_SECS           child wall ceiling (default 1140, about 19 min)
#   SUBAGENT_MODEL_ROUTING_UNRESTRICTED  1 (default) = bypass codex sandbox/approvals
#                               (see README security note); 0 = --sandbox workspace-write
#   SUBAGENT_MODEL_ROUTING_LEDGER        default ~/.claude/subagent-model-routing/ledger/observations.jsonl
#   OTEL_RESOURCE_ATTRIBUTES    optional; the shim appends gen_ai.request.model=<model> for span attribution (see README Observability)
set -u

fail_preflight() {
  local rc="$1"
  shift
  printf '%s\n' "codex-shim: $*" >&2
  printf 'SHIM-DONE exit=%s\n' "$rc"
  exit "$rc"
}

if [ "$#" -lt 1 ]; then
  fail_preflight 64 "usage: codex-shim.sh <prompt-source> [extra codex-exec args]"
fi

SOURCE="$1"
shift

if [ -z "${HOME:-}" ]; then
  fail_preflight 78 "HOME must be set"
fi

TIMEOUT_SECS="${SHIM_TIMEOUT_SECS:-1140}"
UNRESTRICTED="${SUBAGENT_MODEL_ROUTING_UNRESTRICTED:-1}"
LEDGER="${SUBAGENT_MODEL_ROUTING_LEDGER:-$HOME/.claude/subagent-model-routing/ledger/observations.jsonl}"

if [[ ! "$TIMEOUT_SECS" =~ ^[1-9][0-9]*$ ]]; then
  fail_preflight 64 "SHIM_TIMEOUT_SECS must be a positive integer"
fi

case "$UNRESTRICTED" in
  0|1) ;;
  *) fail_preflight 64 "SUBAGENT_MODEL_ROUTING_UNRESTRICTED must be 0 or 1" ;;
esac

if TIMEOUT_BIN="$(command -v timeout 2>/dev/null)"; then
  :
elif TIMEOUT_BIN="$(command -v gtimeout 2>/dev/null)"; then
  :
else
  fail_preflight 127 "GNU timeout not found (brew install coreutils provides gtimeout)"
fi

if CODEX_BIN="$(command -v codex 2>/dev/null)"; then
  :
else
  fail_preflight 127 "codex CLI not found"
fi

# Model label for the ledger: the user's config default unless an override is forwarded.
MODEL="$(sed -n 's/^model *= *"\(.*\)".*/\1/p' "$HOME/.codex/config.toml" 2>/dev/null | head -1)"
MODEL="${MODEL:-codex-default}"
_prev=""
for _a in "$@"; do
  case "$_prev" in -m|--model) MODEL="$_a" ;; esac
  case "$_a" in
    -m=*|--model=*) MODEL="${_a#*=}" ;;
    model=*) MODEL="${_a#model=}" ;;
  esac
  _prev="$_a"
done
MODEL="${MODEL:-codex-default}"

# Span attribution: codex emits usage but no model attribute; its OTel SDK honors
# OTEL_RESOURCE_ATTRIBUTES. Inert unless the user runs an OTel collector.
export OTEL_RESOURCE_ATTRIBUTES="${OTEL_RESOURCE_ATTRIBUTES:+${OTEL_RESOURCE_ATTRIBUTES},}gen_ai.request.model=${MODEL}"

json_escape() {
  local value
  value=${1//$'\r'/ }
  value=${value//$'\n'/ }
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '%s' "$value"
}

MODEL_JSON="$(json_escape "$MODEL")"

ARGS=(exec --skip-git-repo-check)
if [ "$UNRESTRICTED" = "1" ]; then
  ARGS+=(--dangerously-bypass-approvals-and-sandbox)
else
  ARGS+=(--sandbox workspace-write)
fi

now() { date -u +%Y-%m-%dT%H:%M:%S; }
ledger_append() {
  mkdir -p "$(dirname "$LEDGER")" 2>/dev/null || return 0
  printf '%s\n' "$1" >> "$LEDGER" 2>/dev/null || true
}

t0=$(date +%s)
ledger_append "{\"ts\":\"$(now)\",\"shim\":\"codex\",\"model\":\"$MODEL_JSON\",\"event\":\"started\",\"source\":\"shim\"}"

if [ "$SOURCE" = "-" ]; then
  "$TIMEOUT_BIN" "$TIMEOUT_SECS" "$CODEX_BIN" "${ARGS[@]}" "$@"
  rc=$?
else
  if [ ! -r "$SOURCE" ]; then
    echo "codex-shim: cannot read $SOURCE" >&2
    ledger_append "{\"ts\":\"$(now)\",\"shim\":\"codex\",\"model\":\"$MODEL_JSON\",\"event\":\"finished\",\"exit\":66,\"wall_s\":0,\"outcome\":\"error\",\"source\":\"shim\"}"
    echo "SHIM-DONE exit=66"
    exit 66
  fi
  "$TIMEOUT_BIN" "$TIMEOUT_SECS" "$CODEX_BIN" "${ARGS[@]}" "$@" < "$SOURCE"
  rc=$?
fi

wall=$(( $(date +%s) - t0 ))
outcome="ok"
[ "$rc" -eq 124 ] && outcome="timeout"
[ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && outcome="error"
ledger_append "{\"ts\":\"$(now)\",\"shim\":\"codex\",\"model\":\"$MODEL_JSON\",\"event\":\"finished\",\"exit\":$rc,\"wall_s\":$wall,\"outcome\":\"$outcome\",\"source\":\"shim\"}"
printf '\nSHIM-DONE exit=%s\n' "$rc"
exit "$rc"
