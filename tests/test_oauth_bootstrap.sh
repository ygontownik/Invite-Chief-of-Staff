#!/usr/bin/env bash
# test_oauth_bootstrap.sh — black-box checks for oauth_bootstrap.sh.
#
# These tests do NOT trigger any real OAuth consent. They only exercise
# arg parsing, idempotency detection, and error paths. Real consent flows
# require a browser and human attention; that's covered by Build #7
# (fresh-Mac dry run).

set -e

REPO="$HOME/cos-pipeline"
SCRIPT="$REPO/oauth_bootstrap.sh"

PASS=0; FAIL=0

assert_contains() {
  local haystack="$1"; local needle="$2"; local label="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    echo "  ✓ $label"; PASS=$((PASS+1))
  else
    echo "  ✗ $label"
    echo "    expected: $needle"
    echo "    got:      $haystack" | head -3
    FAIL=$((FAIL+1))
  fi
}

assert_exit_code() {
  local actual="$1"; local expected="$2"; local label="$3"
  if [ "$actual" -eq "$expected" ]; then
    echo "  ✓ $label (exit $actual)"; PASS=$((PASS+1))
  else
    echo "  ✗ $label (got exit $actual, expected $expected)"
    FAIL=$((FAIL+1))
  fi
}

echo "── Test 1: bash syntax check ──"
bash -n "$SCRIPT" && echo "  ✓ syntax OK" && PASS=$((PASS+1)) \
  || { echo "  ✗ syntax error"; FAIL=$((FAIL+1)); }

echo "── Test 2: --help works and lists scopes ──"
out=$("$SCRIPT" --help 2>&1)
ec=$?
assert_exit_code "$ec" 0 "--help exits 0"
assert_contains "$out" "full" "--help mentions 'full' scope"
assert_contains "$out" "drive" "--help mentions 'drive' scope"
assert_contains "$out" "gmail-read" "--help mentions 'gmail-read' scope"
assert_contains "$out" "gmail-compose" "--help mentions 'gmail-compose' scope"

echo "── Test 3: unknown scope is rejected ──"
out=$("$SCRIPT" --scope=bogus 2>&1) || ec=$?
assert_exit_code "${ec:-0}" 1 "unknown scope exits non-zero"
assert_contains "$out" "unknown scope" "error message mentions 'unknown scope'"

echo "── Test 4: unknown arg is rejected ──"
out=$("$SCRIPT" --gibberish 2>&1) || ec=$?
assert_exit_code "${ec:-0}" 1 "unknown arg exits non-zero"

echo "── Test 5: idempotency — existing token skips consent ──"
# This works only if at least one canonical token exists (which it should
# on Yoni's machine; in a fresh-Mac dry run the script would prompt).
if [ -f "$HOME/credentials/token.json" ]; then
  out=$("$SCRIPT" --scope=full 2>&1)
  ec=$?
  assert_exit_code "$ec" 0 "idempotent --scope=full exits 0"
  assert_contains "$out" "already bootstrapped" "skip-message for existing token"
else
  echo "  · token.json absent — skipping idempotency test"
fi

echo "── Test 6: --scope=all skips all four when tokens exist ──"
if [ -f "$HOME/credentials/token.json" ] \
   && [ -f "$HOME/credentials/gdrive_token.pickle" ] \
   && [ -f "$HOME/credentials/gmail_token.pickle" ] \
   && [ -f "$HOME/credentials/gmail_mini_token.pickle" ]; then
  out=$("$SCRIPT" --scope=all 2>&1)
  ec=$?
  assert_exit_code "$ec" 0 "idempotent --scope=all exits 0"
  count=$(echo "$out" | grep -c "already bootstrapped" || true)
  if [ "$count" -eq 4 ]; then
    echo "  ✓ all 4 scopes detected as bootstrapped"; PASS=$((PASS+1))
  else
    echo "  ✗ expected 4 'already bootstrapped' lines, got $count"
    FAIL=$((FAIL+1))
  fi
else
  echo "  · not all 4 token files present — skipping --scope=all check"
fi

echo ""
echo "── Summary: $PASS passed, $FAIL failed ──"
exit $FAIL
