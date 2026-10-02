#!/bin/bash
# Regression test: headless serve mode must honor the CLI's spec-decode flags.
#
# `runHeadlessServe` builds its own ServerConfig literal. It shipped with all
# three PLD fields hardcoded (`false`/`5`/`3`), so nothing the user passed on
# the command line reached a headless request. #95 threaded `--pld`; the
# neighbouring `--pld-draft-len` / `--pld-key-len` stayed literals and were
# still silently dropped.
#
# This is the mode that matters: the Swift app ALWAYS launches headless
# (`--serve --model-dir`, no `--model` — ServerOptions.swift:455) and ALWAYS
# emits all three flags (:497-499), so every app-launched server ran on the
# hardcoded values regardless of Settings.
#
# Fully hermetic: an EMPTY --model-dir discovers zero models and never loads
# one, so the boot banner is reachable with no checkpoint on disk. The banner
# is the observable — server.zig's
# `[pld] <on|off> (<source>); draft_len=N, key_len=M...` line reads
# the exact ServerConfig fields a request would.
#
# Usage: ./tests/test_headless_spec_flags.sh [port]

set -u

PORT="${1:-11265}"
BINARY="${BINARY:-./zig-out/bin/sushi}"
PASS=0
FAIL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

check() {
    local desc="$1" ok="$2"
    if [ "$ok" = "1" ]; then
        PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $desc"
    else
        FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $desc"
    fi
}

if [ ! -x "$BINARY" ]; then
    echo "[fail] $BINARY not found — build first: zig build -Doptimize=ReleaseFast"
    exit 1
fi

EMPTY_DIR="$(mktemp -d)"
LOG="$(mktemp)"
SERVER_PID=""
cleanup() {
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
    rm -rf "$EMPTY_DIR" "$LOG"
}
trap cleanup EXIT

require_free_port() {
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | grep -q LISTEN; then
        echo "port $PORT is already in use; stop that server or pass another port" >&2
        exit 1
    fi
}

# Boot headless over the empty dir, capture the banner, stop. Prints nothing;
# the caller greps "$LOG".
boot() {
    require_free_port
    : > "$LOG"
    "$BINARY" --serve --model-dir "$EMPTY_DIR" --port "$PORT" --log-file off "$@" > "$LOG" 2>&1 &
    local pid=$!
    SERVER_PID=$pid
    for _ in $(seq 1 60); do
        curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
        sleep 0.5
        kill -0 "$pid" 2>/dev/null || break
    done
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || {
        echo "  (server did not come up; log follows)"; cat "$LOG"; kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; return 1
    }
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    return 0
}

echo "Headless spec-decode flag plumbing (port $PORT)"

echo "[1/6] --pld with non-default draft/key lengths"
if boot --pld --pld-draft-len 8 --pld-key-len 4; then
    grep -q "\[pld\] on (--pld)" "$LOG"
    check "--pld enables PLD in headless mode" "$([ $? -eq 0 ] && echo 1 || echo 0)"
    grep -q "draft_len=8" "$LOG"
    check "--pld-draft-len 8 reaches the request defaults" "$([ $? -eq 0 ] && echo 1 || echo 0)"
    grep -q "key_len=4" "$LOG"
    check "--pld-key-len 4 reaches the request defaults" "$([ $? -eq 0 ] && echo 1 || echo 0)"
else
    check "boot with --pld" 0
fi

echo "[2/6] --no-pld still disables"
if boot --no-pld --pld-draft-len 8 --pld-key-len 4; then
    grep -q "\[pld\] off (--no-pld)" "$LOG"
    check "--no-pld keeps PLD off even with lengths passed" "$([ $? -eq 0 ] && echo 1 || echo 0)"
else
    check "boot with --no-pld" 0
fi

echo "[3/6] bare default matches the documented 5/3"
if boot; then
    grep -q "\[pld\] on (default); draft_len=5, key_len=3" "$LOG"
    check "default headless boot is PLD on at 5/3" "$([ $? -eq 0 ] && echo 1 || echo 0)"
else
    check "bare default boot" 0
fi

# `--max-tokens` used to feed only the offline `--prompt` mode; headless hand-rolls its own
# ServerConfig literal, which is how the --pld* trio was eaten.
echo "[4/6] --max-tokens reaches the serve-mode omitted-field default"
if boot --max-tokens 4096; then
    grep -q "default max_tokens for omitted requests: 4096" "$LOG"
    check "--max-tokens 4096 reaches the headless request defaults" "$([ $? -eq 0 ] && echo 1 || echo 0)"
else
    check "boot with --max-tokens" 0
fi
if boot; then
    grep -q "default max_tokens for omitted requests" "$LOG"
    check "no flag announces no default (offline's 100 never leaks into serve)" "$([ $? -ne 0 ] && echo 1 || echo 0)"
else
    check "bare boot for the --max-tokens default" 0
fi

# Model resolution runs BEFORE dispatch, so with no default model an unknown
# path never reached the chain's 404 and came back 503 "No default model
# configured". A path's existence has nothing to do with model state, and a
# server that answers every unknown path with a non-404 looks like a catch-all
# to anything that maps endpoints by probing them — llmprobe scored every
# surface absent against a headless boot for exactly this reason (2026-07-25).
# Headless with an EMPTY model dir is the only place this is observable.
echo "[5/6] endpoint existence does not depend on a model being loaded"
require_free_port
: > "$LOG"
"$BINARY" --serve --model-dir "$EMPTY_DIR" --port "$PORT" --log-file off > "$LOG" 2>&1 &
HPID=$!
SERVER_PID=$HPID
for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 0.5
    kill -0 "$HPID" 2>/dev/null || break
done
post_code() {
    curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT$1" \
        -H 'Content-Type: application/json' -d '{}'
}
unknown_code=$(post_code /v1/__no_such_endpoint__)
known_code=$(post_code /v1/chat/completions)
media_code=$(post_code /v1/images/generations)
kill "$HPID" 2>/dev/null
wait "$HPID" 2>/dev/null
check "unknown endpoint is 404, not no-model (got $unknown_code)" \
    "$([ "$unknown_code" = "404" ] && echo 1 || echo 0)"
check "a served endpoint still reports 503 no-model (got $known_code)" \
    "$([ "$known_code" = "503" ] && echo 1 || echo 0)"
check "a removed media-generation endpoint is 404 (got $media_code)" \
    "$([ "$media_code" = "404" ] && echo 1 || echo 0)"

# `--fast` is a flag profile: each setting it turns on yields to its own flag given beside it.
# The boot line echoes the launch layer, before any model's own settings.
echo "[6/6] --fast turns on its preset; an explicit flag beside it wins"
if boot --fast; then
    grep -q "\[args\] fast: mtp on (--fast); acceptance typical (--fast); greedy tail on (--fast); kv8 (--fast)" "$LOG"
    check "--fast alone names --fast for MTP, acceptance, greedy tail and KV" "$([ $? -eq 0 ] && echo 1 || echo 0)"
else
    check "boot with --fast" 0
fi
if boot --no-mtp --fast --mtp-tokenv3 0.9 --kv-quant 4; then
    grep -q "\[args\] fast: mtp off (--no-mtp); acceptance tokenv3 (--mtp-tokenv3); greedy tail on (--fast); kv4 (--kv-quant)" "$LOG"
    check "explicit --no-mtp, --mtp-tokenv3 and --kv-quant outrank --fast, in any order" "$([ $? -eq 0 ] && echo 1 || echo 0)"
else
    check "boot with --fast and explicit flags" 0
fi
if boot; then
    grep -q "\[args\] fast:" "$LOG"
    check "no --fast, no fast line" "$([ $? -ne 0 ] && echo 1 || echo 0)"
else
    check "bare boot for --fast" 0
fi

echo
echo "  passed: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ]
