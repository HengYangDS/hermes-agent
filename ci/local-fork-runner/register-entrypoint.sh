#!/bin/sh
set -eu

: "${RUNNER_TOKEN_FILE:?RUNNER_TOKEN_FILE is required}"
: "${RUNNER_STATE_DIR:?RUNNER_STATE_DIR is required}"
: "${RUNNER_URL:?RUNNER_URL is required}"
: "${RUNNER_NAME:?RUNNER_NAME is required}"
: "${RUNNER_LABELS:?RUNNER_LABELS is required}"

[ "$(id -u)" != 0 ] || { echo 'runner must not execute as root' >&2; exit 64; }
[ "$RUNNER_URL" = 'https://github.com/HengYangDS/hermes-agent' ] || { echo 'runner URL is not the owned fork' >&2; exit 64; }
[ "$RUNNER_NAME" = 'hermes-fork-validation-linux-arm64' ] || { echo 'unexpected runner name' >&2; exit 64; }
[ "$RUNNER_LABELS" = 'hermes-fork-validation' ] || { echo 'unexpected runner label' >&2; exit 64; }
[ "$RUNNER_STATE_DIR" = '/runner-state' ] || { echo 'unexpected runner state directory' >&2; exit 64; }
[ -r "$RUNNER_TOKEN_FILE" ] || { echo 'registration token file is unreadable' >&2; exit 64; }
[ ! -e "$RUNNER_STATE_DIR/.runner" ] && [ ! -e "$RUNNER_STATE_DIR/.credentials" ] || { echo 'runner state is not empty' >&2; exit 64; }

token=$(cat "$RUNNER_TOKEN_FILE")
[ -n "$token" ] || { echo 'registration token is empty' >&2; exit 64; }
mkdir -p "$HOME"
cp -a /opt/actions-runner-dist/. "$RUNNER_STATE_DIR"
cd "$RUNNER_STATE_DIR"
./config.sh --unattended --url "$RUNNER_URL" --token "$token" --name "$RUNNER_NAME" --labels "$RUNNER_LABELS" --work _work --ephemeral --disableupdate
unset token
