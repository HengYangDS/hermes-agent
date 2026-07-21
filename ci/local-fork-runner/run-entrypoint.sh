#!/bin/sh
set -eu

if [ -z "${RUNNER_STATE_DIR:-}" ]; then
  echo 'RUNNER_STATE_DIR is required' >&2
  exit 64
fi
[ "$(id -u)" != 0 ] || { echo 'runner must not execute as root' >&2; exit 64; }
[ -z "${RUNNER_TOKEN_FILE:-}" ] || { echo 'registration token is forbidden during job execution' >&2; exit 64; }
[ "$RUNNER_STATE_DIR" = '/runner-state' ] || { echo 'unexpected runner state directory' >&2; exit 64; }
[ -f "$RUNNER_STATE_DIR/.runner" ] && [ -f "$RUNNER_STATE_DIR/.credentials" ] && [ -f "$RUNNER_STATE_DIR/run.sh" ] || { echo 'runner state is incomplete' >&2; exit 64; }

mkdir -p "$HOME"
cd "$RUNNER_STATE_DIR"
exec ./run.sh
