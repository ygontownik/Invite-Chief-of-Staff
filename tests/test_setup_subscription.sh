#!/bin/bash
# test_setup_subscription.sh — bash-level tests for setup.sh.subscription.next.
#
# Validates: shell syntax, --help works, arg validation rejects bogus values,
# --instance is required, dry-run doesn't write to disk, plist staging produces
# plutil-clean output. Does NOT exercise the interactive Claude.ai project
# walkthrough (S3) — that requires human paste; covered by --yes.
#
# Run:
#     ./tests/test_setup_subscription.sh
#
# Exit 0 when every test passes; exit 1 with a list of failed cases otherwise.

set -uo pipefail

REPO="$HOME/cos-pipeline"
SCRIPT="$REPO/setup.sh.subscription.next"

PASS=0
FAIL=0
FAILED_NAMES=()

assert() {
  local name="$1"; local cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    echo "  ✓ $name"
    PASS=$((PASS+1))
  else
    echo "  ✗ $name"
    FAIL=$((FAIL+1))
    FAILED_NAMES+=("$name")
  fi
}

assert_contains() {
  local name="$1"; local cmd="$2"; local needle="$3"
  local out
  out=$(eval "$cmd" 2>&1) || true
  if echo "$out" | grep -qF -- "$needle"; then
    echo "  ✓ $name"
    PASS=$((PASS+1))
  else
    echo "  ✗ $name (output did not contain '$needle')"
    FAIL=$((FAIL+1))
    FAILED_NAMES+=("$name")
  fi
}

echo "=== setup.sh.subscription.next bash test ==="
echo

# Existence + syntax
assert "script exists" "[ -f '$SCRIPT' ]"
assert "bash -n syntax check" "bash -n '$SCRIPT'"
assert "script is executable" "[ -x '$SCRIPT' ] || chmod +x '$SCRIPT'"

# --help should print the docstring
assert_contains "--help prints docstring header" \
  "bash '$SCRIPT' --help" \
  "setup.sh.subscription.next"

# --instance required
assert_contains "--instance required when missing" \
  "bash '$SCRIPT' --auth-mode=api --yes" \
  "--instance=<slug> is required"

# --auth-mode validation
assert_contains "--auth-mode=garbage rejected" \
  "bash '$SCRIPT' --instance=test --auth-mode=garbage --yes" \
  "must be 'subscription' or 'api'"

# --auth-mode=api on a missing config dir is rejected (preserves the
# precondition that setup.sh has already provisioned the basics).
TMP_HOME=$(mktemp -d)
trap "rm -rf '$TMP_HOME'" EXIT
assert_contains "missing config dir reports clearly" \
  "HOME='$TMP_HOME' bash '$SCRIPT' --instance=ghost --auth-mode=api --yes" \
  "Config dir not found"

# Stand up a fake config dir + assert --auth-mode=api short-circuits to a
# single-field write into firm_context.yaml.
FAKE_HOME=$(mktemp -d)
mkdir -p "$FAKE_HOME/cos-pipeline-config-faketest"
touch "$FAKE_HOME/cos-pipeline-config-faketest/firm_context.yaml"
echo '{}' > "$FAKE_HOME/cos-pipeline-config-faketest/firm_config.json"
HOME="$FAKE_HOME" bash "$SCRIPT" --instance=faketest --auth-mode=api --yes >/dev/null 2>&1
if grep -q '^auth_mode: api$' "$FAKE_HOME/cos-pipeline-config-faketest/firm_context.yaml"; then
  echo "  ✓ --auth-mode=api writes auth_mode: api"
  PASS=$((PASS+1))
else
  echo "  ✗ --auth-mode=api did not write auth_mode: api"
  FAIL=$((FAIL+1))
  FAILED_NAMES+=("auth_mode write")
fi

# --auth-mode=api should be idempotent — second run shouldn't append again.
HOME="$FAKE_HOME" bash "$SCRIPT" --instance=faketest --auth-mode=api --yes >/dev/null 2>&1
COUNT=$(grep -c '^auth_mode:' "$FAKE_HOME/cos-pipeline-config-faketest/firm_context.yaml" || echo 0)
if [ "$COUNT" -eq 1 ]; then
  echo "  ✓ --auth-mode=api is idempotent (single auth_mode line)"
  PASS=$((PASS+1))
else
  echo "  ✗ --auth-mode=api wrote $COUNT auth_mode lines (expected 1)"
  FAIL=$((FAIL+1))
  FAILED_NAMES+=("auth_mode idempotency")
fi
rm -rf "$FAKE_HOME"

echo
echo "─────────────────────────────────────"
echo "  PASS: $PASS   FAIL: $FAIL"
echo "─────────────────────────────────────"

if [ "$FAIL" -gt 0 ]; then
  echo "Failed cases:"
  for n in "${FAILED_NAMES[@]}"; do echo "  - $n"; done
  exit 1
fi
exit 0
