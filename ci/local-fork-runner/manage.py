#!/usr/bin/env python3
"""Operate one isolated, ephemeral runner for the owned Hermes fork only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = Path(__file__).resolve().parent
REPOSITORY = "HengYangDS/hermes-agent"
RUNNER_URL = f"https://github.com/{REPOSITORY}"
RUNNER_NAME = "hermes-fork-validation-linux-arm64"
RUNNER_LABEL = "hermes-fork-validation"
EXPECTED_LABELS = {"self-hosted", "Linux", "ARM64", RUNNER_LABEL}
CONTAINER = "hermes-fork-validation-actions-runner"
HOLDER = f"{CONTAINER}-state"
STATE_PREFIX = "hermes-fork-validation-actions-state"
STATE_LABEL = "ai.yheng.hermes.runner-state=fork-validation"
STATE_DESTINATION = "/runner-state"
TOKEN_DESTINATION = "/run/secrets/runner-registration-token"


def command(*argv: str, capture: bool = False, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=capture, check=False)


def checked(*argv: str, cwd: Path = ROOT) -> str:
    result = command(*argv, capture=True, cwd=cwd)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true", help="Build only; no remote registration.")
    group.add_argument("--once", action="store_true", help="Register one ephemeral runner and wait for one job.")
    group.add_argument("--status", action="store_true", help="Read only local and owned-fork runner state.")
    group.add_argument("--reconcile", action="store_true", help="Remove only a verified offline stale registration and unmounted tmpfs state.")
    return parser.parse_args()


def require_clean_lane() -> None:
    if checked("git", "branch", "--show-current").strip() != "work/hermes-fork-local-runner-harness-20260720":
        raise RuntimeError("manager must run from its owned fork-runner work lane")
    if checked("git", "status", "--porcelain").strip():
        raise RuntimeError("fork-runner work lane must be clean")


def image_name() -> str:
    digest = hashlib.sha256()
    for path in sorted(CONTEXT.glob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return f"hermes-fork-validation-actions-runner:sha-{digest.hexdigest()[:12]}"


def runner_rows() -> list[dict[str, object]]:
    payload = json.loads(checked("gh", "api", f"repos/{REPOSITORY}/actions/runners?per_page=100"))
    rows = payload.get("runners")
    if not isinstance(rows, list):
        raise RuntimeError("runner API response is malformed")
    return [row for row in rows if isinstance(row, dict) and row.get("name") == RUNNER_NAME]


def assert_owned_runner(row: dict[str, object]) -> int:
    labels = row.get("labels")
    names = {item.get("name") for item in labels if isinstance(item, dict)} if isinstance(labels, list) else set()
    if names != EXPECTED_LABELS or not isinstance(row.get("id"), int):
        raise RuntimeError("same-named runner does not have the expected owned identity")
    return int(row["id"])


def containers() -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for name in (CONTAINER, f"{CONTAINER}-register", HOLDER):
        raw = checked("docker", "ps", "--all", "--filter", f"name=^{name}$", "--format", "{{json .}}")
        for line in filter(None, raw.splitlines()):
            item = json.loads(line)
            if item.get("Names") != name:
                raise RuntimeError("container identity mismatch")
            result.append({"name": name, "status": str(item.get("Status", ""))})
    return result


def state_volumes() -> list[str]:
    raw = checked("docker", "volume", "ls", "--filter", f"label={STATE_LABEL}", "--format", "{{.Name}}")
    return [line for line in raw.splitlines() if line]


def validate_volume(name: str) -> None:
    metadata = json.loads(checked("docker", "volume", "inspect", name))[0]
    if not name.startswith(f"{STATE_PREFIX}-") or metadata.get("Labels") != {STATE_LABEL.split("=", 1)[0]: STATE_LABEL.split("=", 1)[1]}:
        raise RuntimeError("volume ownership markers do not match")
    options = metadata.get("Options", {})
    if options.get("type") != "tmpfs" or options.get("device") != "tmpfs" or "mode=0700" not in options.get("o", ""):
        raise RuntimeError("volume is not the expected private tmpfs state")


def remove_volume(name: str) -> None:
    validate_volume(name)
    if checked("docker", "ps", "--all", "--filter", f"volume={name}", "--format", "{{.ID}}").strip():
        raise RuntimeError("refusing to remove a mounted runner-state volume")
    checked("docker", "volume", "rm", name)


def reconcile() -> None:
    if containers():
        raise RuntimeError("owned runner containers exist; refusing reconciliation")
    rows = runner_rows()
    if len(rows) > 1:
        raise RuntimeError("more than one same-named owned runner exists")
    if rows:
        row = rows[0]
        runner_id = assert_owned_runner(row)
        if row.get("status") == "online" or row.get("busy") is True:
            raise RuntimeError("owned runner remains online or busy")
        checked("gh", "api", "--method", "DELETE", f"repos/{REPOSITORY}/actions/runners/{runner_id}")
    for volume in state_volumes():
        remove_volume(volume)


def ensure_image(image: str) -> None:
    uid, gid = os.getuid(), os.getgid()
    if uid == 0 or gid == 0:
        raise RuntimeError("runner manager must not execute as root")
    checked("docker", "image", "inspect", "node:22-bookworm")
    result = command(
        "docker", "build", "--pull=false", "--file", "Dockerfile",
        "--build-arg", f"RUNNER_UID={uid}", "--build-arg", f"RUNNER_GID={gid}",
        "--tag", image, ".", cwd=CONTEXT,
    )
    if result.returncode:
        raise RuntimeError("runner image build failed")


def token_file() -> Path:
    payload = json.loads(checked("gh", "api", "--method", "POST", f"repos/{REPOSITORY}/actions/runners/registration-token"))
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("registration token response is malformed")
    fd, raw = tempfile.mkstemp(prefix="hermes-fork-runner-token-")
    path = Path(raw)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    return path


def common(image: str, volume: str) -> list[str]:
    uid, gid = os.getuid(), os.getgid()
    return [
        "docker", "run", "--rm", "--pull", "never", "--network", "bridge", "--read-only",
        "--user", f"{uid}:{gid}", "--workdir", "/tmp", "--tmpfs", "/tmp:rw,nosuid,nodev,size=4g",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "1024",
        "--memory", "8g", "--cpus", "8", "--mount", f"type=volume,source={volume},target={STATE_DESTINATION},volume-nocopy",
        "--env", f"RUNNER_STATE_DIR={STATE_DESTINATION}",
    ]


def once(image: str) -> int:
    if containers():
        raise RuntimeError("owned runner container is already active")
    reconcile()
    volume = f"{STATE_PREFIX}-{secrets.token_hex(12)}"
    uid, gid = os.getuid(), os.getgid()
    checked("docker", "volume", "create", "--driver", "local", "--label", STATE_LABEL, "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", f"o=uid={uid},gid={gid},mode=0700,size=8g", volume)
    validate_volume(volume)
    token: Path | None = None
    holder = False
    try:
        checked(*[
            "docker", "run", "--detach", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--user", f"{uid}:{gid}", "--workdir", "/tmp", "--tmpfs", "/tmp:rw,nosuid,nodev,size=4g",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "64", "--memory", "128m", "--cpus", "1",
            "--mount", f"type=volume,source={volume},target={STATE_DESTINATION},volume-nocopy", "--name", HOLDER,
            "--entrypoint", "/bin/sh", image, "-c", "while :; do sleep 3600; done",
        ])
        holder = True
        token = token_file()
        register = command(*[
            *common(image, volume), "--name", f"{CONTAINER}-register",
            "--mount", f"type=bind,source={token},target={TOKEN_DESTINATION},readonly",
            "--env", f"RUNNER_TOKEN_FILE={TOKEN_DESTINATION}", "--env", f"RUNNER_URL={RUNNER_URL}",
            "--env", f"RUNNER_NAME={RUNNER_NAME}", "--env", f"RUNNER_LABELS={RUNNER_LABEL}",
            "--entrypoint", "/usr/local/bin/hermes-fork-runner-register", image,
        ])
        if register.returncode:
            return register.returncode
        token.unlink(); token = None
        return command(*[*common(image, volume), "--name", CONTAINER, image]).returncode
    finally:
        if token is not None:
            token.unlink(missing_ok=True)
        if holder:
            command("docker", "stop", "--time", "5", HOLDER)
        remove_volume(volume)
        reconcile()


def main() -> int:
    args = parse_args()
    for binary in ("docker", "gh"):
        if not shutil.which(binary):
            raise RuntimeError(f"missing required tool: {binary}")
    if args.status:
        print(json.dumps({"containers": containers(), "volumes": state_volumes(), "remote_runners": runner_rows()}, indent=2))
        return 0
    require_clean_lane()
    if args.reconcile:
        reconcile(); print("reconciled owned runner residue"); return 0
    image = image_name()
    ensure_image(image)
    if args.build:
        print(image); return 0
    return once(image)


if __name__ == "__main__":
    import shutil
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"hermes fork runner failed closed: {error}", file=sys.stderr)
        raise SystemExit(1)
