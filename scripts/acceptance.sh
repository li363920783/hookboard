#!/usr/bin/env bash
# Hookboard M1 acceptance.
#
# It is the "5-minute demo" from docs/POSITIONING.md AND the seven assertions
# from docs/PLAN.md §10, in one run:
#   pip install -e .  ->  hookboard run  ->  task pending -> waiting -> approve -> done
# using config.example.json VERBATIM (no config editing; only the port is set
# through the environment because 8899 may already be taken on this machine).
#
# Usage: bash scripts/acceptance.sh      (ACCEPT_PORT=18899 to use another port)
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${ACCEPT_PORT:-8899}"
BASE="http://127.0.0.1:${PORT}"
TOKEN="change-me"   # the value shipped in config.example.json; not edited

if ! command -v jq >/dev/null 2>&1; then
  echo "acceptance: jq is required (curl + jq drive the assertions)" >&2
  exit 1
fi

if python3 - "$PORT" <<'PY'
import socket, sys
sock = socket.socket()
sock.settimeout(0.3)
sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
then
  echo "acceptance: port $PORT is already in use; set ACCEPT_PORT to a free port" >&2
  exit 1
fi

TMP="$(mktemp -d)"
CFG="$TMP/config.json"
LOG="$TMP/server.log"
PID=""

cleanup() {
  status=$?
  if [ -n "$PID" ]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  if [ "$status" -ne 0 ] && [ -s "$LOG" ]; then
    echo "--- server log (tail) ---" >&2
    tail -n 40 "$LOG" >&2
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

fail() { echo "FAIL  $*" >&2; exit 1; }
pass() { echo "PASS  $*"; }

# --- the demo's first step: pip install -e . gives a real `hookboard` command.
# Installed into a throwaway venv so the acceptance never touches the global
# environment. If this machine cannot build the package, fall back to the
# module entry point and say so; the flow assertions below still run.
HUB_CMD="python3 -m hub"
VENV="$TMP/venv"
if python3 -m venv --system-site-packages "$VENV" >/dev/null 2>&1 \
   && "$VENV/bin/pip" install -e . --no-build-isolation -q >/dev/null 2>&1; then
  HUB_CMD="$VENV/bin/hookboard"
  echo "note: pip install -e . OK, demo uses the installed 'hookboard' command"
else
  echo "note: pip install -e . unavailable here; demo runs via 'python3 -m hub'"
fi

# --- out-of-box config: copy the example VERBATIM, edit nothing.
cp config.example.json "$CFG"

# OUTBOX mode: no network requests; channel payloads land in data/outbox/.
HUB_OUTBOX=1 HUB_PORT="$PORT" $HUB_CMD run --config "$CFG" >"$LOG" 2>&1 &
PID=$!
OUTBOX="$TMP/data/outbox"

# --- 1. health -------------------------------------------------------------
HEALTH=""
for _ in $(seq 1 50); do
  HEALTH=$(curl -sf "$BASE/api/health" 2>/dev/null || true)
  if [ -n "$HEALTH" ] && [ "$(jq -r '.ok' <<<"$HEALTH" 2>/dev/null || true)" = "true" ]; then
    break
  fi
  sleep 0.1
done
[ "$(jq -r '.ok' <<<"$HEALTH" 2>/dev/null || true)" = "true" ] || fail "1. GET /api/health did not return ok:true"
pass "1. GET /api/health ok:true (version=$(jq -r '.version' <<<"$HEALTH"), workers=$(jq -r '.workers' <<<"$HEALTH"))"

# --- 2. create task, visible as pending ------------------------------------
CREATE=$(curl -sf -X POST "$BASE/api/tasks" \
  -H "X-Hub-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"source":"generic","kind":"demo","external_id":"acc-1","title":"acceptance task"}') \
  || fail "2. POST /api/tasks failed"
TASK_ID=$(jq -r '.task_id' <<<"$CREATE")
[ "$TASK_ID" != "null" ] && [ -n "$TASK_ID" ] || fail "2. POST /api/tasks returned no task_id"
[ "$(jq -r '.created' <<<"$CREATE")" = "true" ] || fail "2. create did not report created:true"
[ "$(jq -r '.task.status' <<<"$CREATE")" = "pending" ] || fail "2. new task is not pending"
curl -sf "$BASE/api/tasks" | jq -e --argjson id "$TASK_ID" '.tasks | map(.id) | index($id) != null' >/dev/null \
  || fail "2. GET /api/tasks does not list task $TASK_ID"
pass "2. POST /api/tasks -> task $TASK_ID pending, visible in GET /api/tasks"

# --- 3. worker claims it within 3 seconds ----------------------------------
STATUS=""
CLAIMED=0
for _ in $(seq 1 15); do
  STATUS=$(curl -sf "$BASE/api/tasks/$TASK_ID" | jq -r '.task.status')
  case "$STATUS" in
    running|waiting) CLAIMED=1; break ;;
  esac
  sleep 0.2
done
[ "$CLAIMED" = 1 ] || fail "3. task not claimed within 3s (status=$STATUS)"
pass "3. worker claimed task within 3s (status=$STATUS)"

# --- 4. approval opened, page serves HTML ----------------------------------
APPROVAL_TOKEN=""
for _ in $(seq 1 25); do
  APPROVAL_TOKEN=$(curl -sf "$BASE/api/tasks/$TASK_ID" \
    | jq -r '[.approvals[] | select(.status=="open")][0].token // empty')
  [ -n "$APPROVAL_TOKEN" ] && break
  sleep 0.2
done
[ -n "$APPROVAL_TOKEN" ] || fail "4. no open approval appeared"
PAGE_CODE=$(curl -s -o "$TMP/approval.html" -w '%{http_code}' "$BASE/a/$APPROVAL_TOKEN")
[ "$PAGE_CODE" = "200" ] || fail "4. GET /a/{token} returned HTTP $PAGE_CODE"
grep -q '需要确认' "$TMP/approval.html" || fail "4. approval page does not show the question"
pass "4. open approval token=${APPROVAL_TOKEN:0:8}…, /a/{token} returns 200 HTML"

# --- 5. approve -> answered, pending, stage advanced (and idempotent) ------
STAGE_BEFORE=$(curl -sf "$BASE/api/tasks/$TASK_ID" | jq -r '.task.stage')
DECIDE=$(curl -sf -X POST "$BASE/a/$APPROVAL_TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"index":0}') || fail "5. POST /a/{token} failed"
[ "$(jq -r '.approval.status' <<<"$DECIDE")" = "answered" ] || fail "5. approval did not become answered"
[ "$(jq -r '.task.status' <<<"$DECIDE")" = "pending" ] || fail "5. task is not pending right after approval"
STAGE_AFTER=$(jq -r '.task.stage' <<<"$DECIDE")
[ "$STAGE_AFTER" != "$STAGE_BEFORE" ] || fail "5. stage did not advance past $STAGE_BEFORE"
REPLAY=$(curl -sf -X POST "$BASE/a/$APPROVAL_TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"index":0}') || fail "5. replay POST /a/{token} failed"
[ "$(jq -r '.changed' <<<"$REPLAY")" = "false" ] || fail "5. replaying the decision was not idempotent"
pass "5. approval answered, task pending, stage $STAGE_BEFORE -> $STAGE_AFTER, replay changed=false"

# --- 6. run to done; every stage left one event ----------------------------
DETAIL=""
STATUS=""
for _ in $(seq 1 50); do
  DETAIL=$(curl -sf "$BASE/api/tasks/$TASK_ID")
  STATUS=$(jq -r '.task.status' <<<"$DETAIL")
  [ "$STATUS" = "done" ] && break
  NEXT_TOKEN=$(jq -r '[.approvals[] | select(.status=="open")][0].token // empty' <<<"$DETAIL")
  if [ -n "$NEXT_TOKEN" ]; then
    curl -sf -X POST "$BASE/a/$NEXT_TOKEN" \
      -H 'Content-Type: application/json' -H 'Accept: application/json' \
      -d '{"index":0}' >/dev/null || fail "6. approving review gate failed"
  fi
  sleep 0.2
done
[ "$STATUS" = "done" ] || fail "6. task never reached done (status=$STATUS)"
for stage in intake plan dispatch work review finish; do
  COUNT_STAGE=$(jq --arg s "$stage" '[.events[] | select(.kind=="stage" and .detail==$s)] | length' <<<"$DETAIL")
  [ "$COUNT_STAGE" -ge 1 ] || fail "6. no stage event for $stage"
done
pass "6. task done; events carry every stage (intake plan dispatch work review finish)"

# --- 7. duplicate delivery keeps a single row ------------------------------
COUNT_BEFORE=$(curl -sf "$BASE/api/tasks" | jq '.tasks | length')
DUPLICATE=$(curl -sf -X POST "$BASE/hooks/generic" \
  -H "X-Hub-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"id":"acc-1","kind":"demo","title":"acceptance task"}') \
  || fail "7. duplicate hook delivery failed"
[ "$(jq -r '.created' <<<"$DUPLICATE")" = "false" ] || fail "7. duplicate hook created a second task"
[ "$(jq -r '.task_id' <<<"$DUPLICATE")" = "$TASK_ID" ] || fail "7. duplicate hook resolved to a different task"
COUNT_AFTER=$(curl -sf "$BASE/api/tasks" | jq '.tasks | length')
[ "$COUNT_BEFORE" = "$COUNT_AFTER" ] || fail "7. task count changed $COUNT_BEFORE -> $COUNT_AFTER"
pass "7. duplicate hook kept one task ($COUNT_AFTER rows) with id $TASK_ID"

# --- 8. the 5-minute demo, driven through the CLI (no curl to create) ------
# Same out-of-box config, same running server: create with `hookboard create`,
# watch it reach waiting, approve on the token page, watch it reach done.
DEMO_CREATE=$($HUB_CMD create --config "$CFG" --source cli --kind demo \
  --external-id demo-5min --title "5-minute demo") || fail "8. hookboard create failed"
DEMO_ID=$(awk '{print $2}' <<<"$DEMO_CREATE")
[ -n "$DEMO_ID" ] || fail "8. could not parse demo task id from: $DEMO_CREATE"

DEMO_TOKEN=""
for _ in $(seq 1 40); do
  DEMO_TOKEN=$(curl -sf "$BASE/api/tasks/$DEMO_ID" \
    | jq -r '[.approvals[] | select(.status=="open")][0].token // empty')
  [ -n "$DEMO_TOKEN" ] && break
  sleep 0.2
done
[ -n "$DEMO_TOKEN" ] || fail "8. demo task never reached waiting (no open approval)"
curl -sf -o /dev/null "$BASE/a/$DEMO_TOKEN" || fail "8. demo approval page not reachable"

DEMO_STATUS=""
for _ in $(seq 1 60); do
  DEMO_DETAIL=$(curl -sf "$BASE/api/tasks/$DEMO_ID")
  DEMO_STATUS=$(jq -r '.task.status' <<<"$DEMO_DETAIL")
  [ "$DEMO_STATUS" = "done" ] && break
  NEXT_TOKEN=$(jq -r '[.approvals[] | select(.status=="open")][0].token // empty' <<<"$DEMO_DETAIL")
  if [ -n "$NEXT_TOKEN" ]; then
    curl -sf -X POST "$BASE/a/$NEXT_TOKEN" \
      -H 'Content-Type: application/json' -H 'Accept: application/json' \
      -d '{"index":0}' >/dev/null || fail "8. demo approval failed"
  fi
  sleep 0.2
done
[ "$DEMO_STATUS" = "done" ] || fail "8. demo task never reached done (status=$DEMO_STATUS)"
pass "8. 5-minute demo: hookboard create -> waiting -> approve -> done (task $DEMO_ID, no config edits)"

# --- 9. OUTBOX channels: payloads written, link in HTML+text, no duplicates -
python3 - "$OUTBOX" "$TASK_ID" <<'PY' || fail "9. outbox channel assertions failed"
import json, pathlib, sys
outbox, task_id = pathlib.Path(sys.argv[1]), int(sys.argv[2])
msgs = [json.loads(p.read_text(encoding="utf-8")) for p in outbox.glob("*.json")]
mine = [m for m in msgs if m.get("task_id") == task_id]
assert mine, "no outbox messages for the task"
def channel(m):
    if m.get("msg_type") == "interactive": return "feishu"
    if m.get("multipart") == "alternative": return "email"
    return "webhook"
channels = {channel(m) for m in mine}
assert {"webhook", "email", "feishu"} <= channels, f"missing channels: {channels}"
seen = {}
for m in mine:
    key = (m.get("stage"), channel(m))
    seen[key] = seen.get(key, 0) + 1
assert all(n == 1 for n in seen.values()), f"duplicate sends: {seen}"
for m in mine:
    ch = channel(m)
    if ch == "email":
        assert "/a/" in m["text"] and "/a/" in m["html"], "approval link missing in an email part"
    elif ch == "webhook":
        assert any("/a/" in l["url"] for l in m["links"]), "webhook link missing approval url"
print(f"outbox OK: {len(mine)} messages for task {task_id}, channels={sorted(channels)}, dedup keys={len(seen)}")
PY
pass "9. OUTBOX mode: feishu+webhook+email payloads written, one send per (task,stage,channel)"

# --- 10. feishu card: interactive, content carries /a/ and /api/board links --
python3 - "$OUTBOX" "$TASK_ID" <<'PY' || fail "10. feishu card assertions failed"
import json, pathlib, sys
outbox, task_id = pathlib.Path(sys.argv[1]), int(sys.argv[2])
feishu = [json.loads(p.read_text(encoding="utf-8")) for p in outbox.glob("*-feishu-*.json")]
feishu = [m for m in feishu if m.get("task_id") == task_id]
assert feishu, "no feishu outbox message for the task"
for m in feishu:
    assert m["msg_type"] == "interactive", "feishu msg_type must be interactive"
    content = m["content"]
    assert "/a/" in content, "feishu content missing approval link (/a/)"
    assert "/api/board" in content, "feishu content missing board link (/api/board)"
    card = json.loads(content)
    assert card["schema"] == "2.0"
    actions = [e for e in card["body"]["elements"] if e["tag"] == "action"][0]["actions"]
    assert len(actions) == 2 and all(b["type"] == "default" for b in actions)
print(f"feishu OK: {len(feishu)} interactive card(s), content has /a/ and /api/board")
PY
pass "10. feishu outbox: msg_type=interactive, content greps /a/ and /api/board, schema 2.0, 2 buttons"

echo "ACCEPTANCE PASSED (7 criteria + 5-minute demo + OUTBOX channels + feishu card) on port $PORT"