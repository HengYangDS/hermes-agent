#!/bin/sh
set -eu

# Run the container entrypoints without their required environment.  A caller
# mistake must fail with the scripts' documented usage exit code, not with a
# shell-specific parameter-expansion error.

: "${1:?usage: $0 <runner-image>}"
image=$1

run_missing_env() {
  entrypoint=$1
  expected=$2
  output=$(mktemp)
  trap 'rm -f "$output"' EXIT HUP INT TERM

  set +e
  docker run --rm --network none --read-only --user 501:20 \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    --entrypoint "$entrypoint" "$image" >"$output" 2>&1
  status=$?
  set -e

  [ "$status" -eq 64 ] || {
    cat "$output" >&2
    echo "expected exit 64 from $entrypoint, got $status" >&2
    exit 1
  }
  grep -F "$expected" "$output" >/dev/null
  rm -f "$output"
  trap - EXIT HUP INT TERM
}

run_missing_env /usr/local/bin/hermes-fork-runner-register 'RUNNER_TOKEN_FILE is required'
run_missing_env /usr/local/bin/hermes-fork-runner-run 'RUNNER_STATE_DIR is required'
