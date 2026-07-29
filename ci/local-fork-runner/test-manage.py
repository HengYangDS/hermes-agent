#!/usr/bin/env python3
"""Contract tests for safe local fork-runner interruption and cleanup."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("fork_runner_manage", HERE / "manage.py")
assert SPEC is not None and SPEC.loader is not None
manager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manager
SPEC.loader.exec_module(manager)


class RunnerCleanupTests(unittest.TestCase):
    def test_termination_is_recoverable_until_owned_cleanup_finishes(self) -> None:
        with self.assertRaises(manager.RunnerInterrupted) as caught:
            manager.raise_after_cleanup(signal.SIGTERM, None)
        self.assertEqual(caught.exception.signum, signal.SIGTERM)

    def test_cleanup_stops_each_owned_container_before_volume_and_remote_reconcile(self) -> None:
        events: list[tuple[str, ...]] = []
        active = [manager.CONTAINER, f"{manager.CONTAINER}-register", manager.HOLDER]

        def fake_containers() -> list[dict[str, str]]:
            return [{"name": name} for name in active]

        def fake_command(*argv: str, **_kwargs: object) -> object:
            events.append(("stopped", *argv))
            active.remove(argv[-1])
            return type("Result", (), {"returncode": 0, "stderr": ""})()

        with patch.object(manager, "containers", side_effect=fake_containers), \
             patch.object(manager, "assert_owned_container", side_effect=lambda name, volume: events.append(("verified", name, volume))), \
             patch.object(manager, "command", side_effect=fake_command), \
             patch.object(manager, "state_volumes", side_effect=[[], []]), \
             patch.object(manager, "remove_volume", side_effect=lambda name: events.append(("volume", name))), \
             patch.object(manager, "reconcile", side_effect=lambda **_kwargs: events.append(("reconcile",))):
            manager.cleanup_resources(manager.OwnedResources(volume="owned-volume"))

        stopped = [event[-1] for event in events if event[0] == "stopped"]
        self.assertEqual(stopped, [manager.CONTAINER, f"{manager.CONTAINER}-register", manager.HOLDER])
        verified = [event[1] for event in events if event[0] == "verified"]
        self.assertEqual(verified, [manager.CONTAINER, f"{manager.CONTAINER}-register", manager.HOLDER])
        self.assertEqual(events[-1], ("reconcile",))

    def test_cleanup_refuses_to_stop_a_same_named_container_without_owned_identity(self) -> None:
        with patch.object(manager, "containers", return_value=[{"name": manager.HOLDER}]), \
             patch.object(manager, "assert_owned_container", side_effect=RuntimeError("identity mismatch")), \
             patch.object(manager, "command") as command:
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                manager.stop_owned_containers("owned-volume")
        command.assert_not_called()

    def test_cleanup_removes_tracked_token_even_when_later_cleanup_fails(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            token = Path(handle.name)
        resources = manager.OwnedResources(volume="owned-volume", token=token)
        with patch.object(manager, "stop_owned_containers", side_effect=RuntimeError("stop failed")), \
             patch.object(manager, "state_volumes", return_value=[]), \
             patch.object(manager, "remove_volume"), \
             patch.object(manager, "reconcile"):
            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                manager.cleanup_resources(resources)
        self.assertFalse(token.exists())
        self.assertIsNone(resources.token)

    def test_real_sigterm_enters_once_cleanup_before_process_exit(self) -> None:
        cleanup_calls: list[manager.OwnedResources] = []

        def interrupted_checked(*argv: str, **_kwargs: object) -> str:
            if argv[:3] == ("docker", "volume", "create"):
                return "owned-volume\n"
            if argv[:3] == ("docker", "run", "--detach"):
                os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError(argv)

        with patch.object(manager, "containers", return_value=[]), \
             patch.object(manager, "reconcile"), \
             patch.object(manager, "checked", side_effect=interrupted_checked), \
             patch.object(manager, "validate_volume"), \
             patch.object(manager, "cleanup_resources", side_effect=lambda resources: cleanup_calls.append(resources)):
            with self.assertRaises(manager.RunnerInterrupted):
                with manager.interruption_guard():
                    manager.once("runner-image")

        self.assertEqual(len(cleanup_calls), 1)
        self.assertTrue(cleanup_calls[0].volume.startswith(manager.STATE_PREFIX + "-"))

    def test_cleanup_waits_for_remote_runner_to_go_offline_before_deregistration(self) -> None:
        online = {
            "name": manager.RUNNER_NAME,
            "id": 42,
            "status": "online",
            "busy": True,
            "labels": [{"name": label} for label in manager.EXPECTED_LABELS],
        }
        offline = {**online, "status": "offline", "busy": False}
        deletes: list[tuple[str, ...]] = []

        def fake_checked(*argv: str, **_kwargs: object) -> str:
            deletes.append(argv)
            return ""

        with patch.object(manager, "containers", return_value=[]), \
             patch.object(manager, "runner_rows", side_effect=[[online], [offline]]), \
             patch.object(manager, "checked", side_effect=fake_checked), \
             patch.object(manager, "state_volumes", return_value=[]), \
             patch.object(manager.time, "sleep") as sleep:
            manager.reconcile(wait_for_offline=True)

        sleep.assert_called_once_with(manager.REMOTE_DEREGISTRATION_POLL_SECONDS)
        self.assertEqual(deletes, [("gh", "api", "--method", "DELETE", f"repos/{manager.REPOSITORY}/actions/runners/42")])


class WorkflowContractTests(unittest.TestCase):
    def test_dispatch_checkout_is_asserted_against_the_requested_immutable_sha(self) -> None:
        workflow = (HERE.parents[1] / ".github" / "workflows" / "local-fork-validation.yml").read_text()
        self.assertIn("ref: ${{ inputs.ref }}", workflow)
        self.assertIn("- name: Assert exact checkout", workflow)
        self.assertIn('ACTUAL=$(git rev-parse HEAD)', workflow)
        self.assertIn('[ "$ACTUAL" = "$REF" ]', workflow)


if __name__ == "__main__":
    unittest.main()
