"""ACP route identities survive construction, native save, restore and fork."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager, SessionState
from hermes_state import SessionDB


class CapturingAgent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.requested_provider = kwargs.get("requested_provider") or kwargs.get(
            "provider"
        )


@pytest.fixture
def configured_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {
        "model": {"default": "fixture-model", "provider": "custom:fixture-responses"},
        "providers": {
            "fixture-responses": {
                "name": "Fixture responses",
                "base_url": "https://responses.example.invalid/v1",
                "transport": "codex_responses",
                "default_model": "fixture-model",
                "models": ["fixture-model"],
            },
            "fixture-chat": {
                "name": "Fixture chat",
                "base_url": "https://chat.example.invalid/v1",
                "transport": "chat_completions",
                "default_model": "fixture-model",
                "models": ["fixture-model"],
            },
        },
        "platform_toolsets": {"acp": ["file", "no_mcp"]},
        "mcp_servers": {},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr("run_agent.AIAgent", CapturingAgent)
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
        lambda **kwargs: None,
    )
    monkeypatch.setattr("acp_adapter.session._register_task_cwd", lambda *args: None)
    return tmp_path


def test_real_named_config_resolution_reaches_the_agent_without_losing_identity(
    configured_home,
):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    route = resolve_runtime_provider(
        requested="custom:fixture-responses", target_model="fixture-model"
    )
    db = SessionDB(configured_home / "state.db")
    try:
        agent = SessionManager(db=db)._make_agent(
            session_id="fresh", cwd=str(configured_home)
        )
        assert route["provider"] == "custom"
        assert (
            agent.requested_provider
            == route["requested_provider"]
            == "custom:fixture-responses"
        )
        assert agent.api_mode == route["api_mode"] == "codex_responses"
        assert agent.base_url == route["base_url"]
    finally:
        db.close()


def test_native_persist_restore_and_fork_keep_the_named_route(configured_home):
    db = SessionDB(configured_home / "state.db")
    try:
        native = CapturingAgent(
            model="fixture-model",
            provider="custom",
            requested_provider="custom:fixture-chat",
            base_url="https://chat.example.invalid/v1",
            api_mode="chat_completions",
        )
        first = SessionManager(agent_factory=lambda: native, db=db)
        state = first.create_session(cwd=str(configured_home))
        state.history = [{"role": "user", "content": "Original source."}]
        first.save_session(state.session_id)
        metadata = json.loads(db.get_session(state.session_id)["model_config"])
        assert metadata["provider"] == "custom"
        assert metadata["requested_provider"] == "custom:fixture-chat"
        restored_manager = SessionManager(db=db)
        restored = restored_manager.get_session(state.session_id)
        assert restored.agent.requested_provider == "custom:fixture-chat"
        assert restored.agent.base_url == native.base_url
        assert restored.agent.api_mode == native.api_mode
        forked = restored_manager.fork_session(
            state.session_id, cwd=str(configured_home)
        )
        assert forked.agent.requested_provider == restored.agent.requested_provider
        assert forked.agent.base_url == restored.agent.base_url
        assert forked.agent.api_mode == restored.agent.api_mode
        assert [row["content"] for row in forked.history] == ["Original source."]
    finally:
        db.close()


@pytest.mark.parametrize("legacy_provider", [None, "custom"])
def test_unknown_legacy_identity_does_not_pin_old_wire_mode(
    monkeypatch, legacy_provider
):
    from hermes_cli.model_switch import ModelSwitchResult

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.config.get_compatible_custom_providers", lambda config: []
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kwargs: ModelSwitchResult(
            success=True,
            target_provider="custom:fixture-responses",
            new_model="fixture-model",
        ),
    )
    made = {}

    class Manager:
        def _make_agent(self, **kwargs):
            made.update(kwargs)
            return SimpleNamespace(**kwargs)

        def save_session(self, session_id):
            pass

    state = SessionState(
        session_id="legacy",
        cwd=".",
        model="fixture-model",
        agent=SimpleNamespace(
            provider=legacy_provider,
            base_url="https://responses.example.invalid/v1",
            api_mode="chat_completions",
        ),
    )
    HermesACPAgent(session_manager=Manager())._switch_model(
        state, "custom:fixture-responses:fixture-model", keep_endpoint=True
    )
    assert made["requested_provider"] == "custom:fixture-responses"
    assert "api_mode" not in made
    assert "base_url" not in made


def test_known_named_identity_keeps_its_session_endpoint_override(monkeypatch):
    from hermes_cli.model_switch import ModelSwitchResult

    seen, made = {}, {}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.config.get_compatible_custom_providers", lambda config: []
    )

    def switch(**kwargs):
        seen.update(kwargs)
        return ModelSwitchResult(
            success=True,
            target_provider="custom:fixture-responses",
            new_model="fixture-model",
        )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", switch)

    class Manager:
        def _make_agent(self, **kwargs):
            made.update(kwargs)
            return SimpleNamespace(**kwargs)

        def save_session(self, session_id):
            pass

    native = SimpleNamespace(
        provider="custom",
        requested_provider="custom:fixture-responses",
        base_url="https://override.example.invalid/v1",
        api_mode="codex_responses",
    )
    state = SessionState(session_id="named", agent=native, model="fixture-model")
    HermesACPAgent(session_manager=Manager())._switch_model(
        state, "fixture-model", keep_endpoint=True
    )
    assert seen["current_provider"] == native.requested_provider
    assert made["base_url"] == native.base_url
    assert made["api_mode"] == native.api_mode


def test_native_model_and_context_commands_show_the_selected_named_provider():
    native = SimpleNamespace(
        provider="custom",
        requested_provider="custom:fixture-responses",
        model="fixture-model",
        context_compressor=None,
    )
    state = SessionState(session_id="named", agent=native, model=native.model)
    surface = HermesACPAgent(session_manager=SimpleNamespace())
    assert "Provider: custom:fixture-responses" in surface._cmd_model("", state)
    assert "custom:fixture-responses" in surface._cmd_context("", state)


def test_native_named_resolution_is_profile_scoped_a_b_a(configured_home, monkeypatch):
    from agent.secret_scope import (
        is_multiplex_active,
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    roots = [configured_home / name for name in ("profile-a", "profile-b")]
    for index, root in enumerate(roots):
        root.mkdir()
        config = {
            "model": {
                "default": "fixture-model",
                "provider": f"custom:fixture-{index}",
            },
            "providers": {
                f"fixture-{index}": {
                    "name": f"Fixture {index}",
                    "base_url": f"https://profile-{index}.example.invalid/v1",
                    "transport": "codex_responses"
                    if index == 0
                    else "chat_completions",
                    "default_model": "fixture-model",
                    "models": ["fixture-model"],
                }
            },
            "platform_toolsets": {"acp": ["file", "no_mcp"]},
            "mcp_servers": {},
        }
        (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    previous = is_multiplex_active()
    set_multiplex_active(True)
    try:
        results = []
        for root in [roots[0], roots[1], roots[0]]:
            home_token = set_hermes_home_override(root)
            secret_token = set_secret_scope({}, profile_home=str(root))
            db = SessionDB(root / "state.db")
            try:
                native = SessionManager(db=db)._make_agent(
                    session_id="scoped", cwd=str(root)
                )
                results.append((
                    native.requested_provider,
                    native.base_url,
                    native.api_mode,
                ))
            finally:
                db.close()
                reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)
        assert results[0] == results[2]
        assert results[0][0] == "custom:fixture-0"
        assert results[1][0] == "custom:fixture-1"
        assert results[0][1] != results[1][1]
        assert results[0][2] != results[1][2]
    finally:
        set_multiplex_active(previous)


def test_real_agent_uses_the_named_transport_without_inference(
    configured_home, monkeypatch
):
    import run_agent

    monkeypatch.undo()
    monkeypatch.setenv("HERMES_HOME", str(configured_home))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    path = configured_home / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["model"]["context_length"] = 131072
    config["memory"] = {
        "provider": "builtin",
        "memory_enabled": False,
        "user_profile_enabled": False,
    }
    config["compression"] = {"enabled": False}
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    agent = None
    db = SessionDB(configured_home / "state.db")
    try:
        agent = SessionManager(db=db)._make_agent(
            session_id="real-agent",
            cwd=str(configured_home),
            enabled_toolsets=[],
        )
        assert isinstance(agent, run_agent.AIAgent)
        assert agent.provider == "custom"
        assert agent.requested_provider == "custom:fixture-responses"
        assert agent.api_mode == "codex_responses"
        assert agent.base_url.rstrip("/") == "https://responses.example.invalid/v1"
        assert agent._api_call_count == 0
    finally:
        if agent is not None:
            agent.close()
        db.close()
