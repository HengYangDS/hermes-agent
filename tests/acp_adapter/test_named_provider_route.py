"""A named custom provider keeps its identity through the ACP session lifecycle (#63681)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager, SessionState
from hermes_state import SessionDB


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Two named entries on different wire protocols; the profile default is the Responses one."""
    providers = {
        name: {
            "name": name,
            "base_url": f"https://{name}.example.invalid/v1",
            "transport": transport,
            "default_model": "fixture-model",
            "models": ["fixture-model"],
        }
        for name, transport in (("fixture-responses", "codex_responses"), ("fixture-chat", "chat_completions"))
    }
    config = {
        "model": {"default": "fixture-model", "provider": "custom:fixture-responses", "context_length": 131072},
        "providers": providers,
        "platform_toolsets": {"acp": ["file", "no_mcp"]},
        "mcp_servers": {},
        "memory": {"provider": "builtin", "memory_enabled": False, "user_profile_enabled": False},
        "compression": {"enabled": False},
    }
    (tmp_path / "config.yaml").write_text(json.dumps(config), encoding="utf-8")  # JSON is valid YAML
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setattr("hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build", lambda **kwargs: None)
    monkeypatch.setattr("acp_adapter.session._register_task_cwd", lambda *args: None)
    return tmp_path


def test_selected_named_provider_survives_save_restore_and_fork(home):
    """A session on the non-default entry restores and forks onto that entry, not onto whatever the
    bare runtime class ``custom`` resolves to, and ``/model`` / ``/context`` name it."""
    import run_agent

    db = SessionDB(home / "state.db")
    agents = []
    try:
        selected = SessionManager(db=db)._make_agent(
            session_id="selected", cwd=str(home), model="fixture-model", requested_provider="custom:fixture-chat")
        agents.append(selected)
        first = SessionManager(agent_factory=lambda: selected, db=db)
        state = first.create_session(cwd=str(home))
        state.history = [{"role": "user", "content": "Original source."}]
        first.save_session(state.session_id)

        manager = SessionManager(db=db)
        restored = manager.get_session(state.session_id)
        agents.append(restored.agent)
        forked = manager.fork_session(state.session_id, cwd=str(home))
        agents.append(forked.agent)

        for agent in (restored.agent, forked.agent):
            assert isinstance(agent, run_agent.AIAgent)
            assert (agent.provider, agent.requested_provider) == ("custom", "custom:fixture-chat")
            assert agent.api_mode == "chat_completions"
            assert agent.base_url.rstrip("/") == "https://fixture-chat.example.invalid/v1"
            assert agent._api_call_count == 0
        assert [row["content"] for row in forked.history] == ["Original source."]
        surface = HermesACPAgent(session_manager=manager)
        assert "Provider: custom:fixture-chat" in surface._cmd_model("", restored)
        assert "custom:fixture-chat" in surface._cmd_context("", restored)
    finally:
        for agent in agents:
            agent.close()
        db.close()


@pytest.mark.parametrize(
    ("identity", "keeps_endpoint"),
    [
        ({"provider": "custom", "requested_provider": "custom:fixture-responses"}, True),
        ({"provider": "custom"}, False),
        ({"provider": None}, False),
    ],
    ids=["same-named-provider", "legacy-bare-custom", "legacy-unknown"],
)
def test_model_switch_keeps_the_session_endpoint_only_for_the_same_named_provider(
        monkeypatch, identity, keeps_endpoint):
    """A switch within the selected entry keeps its endpoint and wire-mode override; an older
    session without that identity never pins its old wire mode onto a newly selected entry."""
    from hermes_cli.model_switch import ModelSwitchResult

    seen, made = {}, {}
    monkeypatch.setattr("hermes_cli.config.load_config", dict)
    monkeypatch.setattr("hermes_cli.config.get_compatible_custom_providers", lambda config: [])

    def switch(**kwargs):
        seen.update(kwargs)
        return ModelSwitchResult(success=True, target_provider="custom:fixture-responses", new_model="fixture-model")

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", switch)

    class Manager:
        def _make_agent(self, **kwargs):
            made.update(kwargs)
            return SimpleNamespace(**kwargs)

        def save_session(self, session_id):
            pass

    live = SimpleNamespace(base_url="https://override.example.invalid/v1", api_mode="chat_completions", **identity)
    state = SessionState(session_id="switch", agent=live, model="fixture-model")
    HermesACPAgent(session_manager=Manager())._switch_model(
        state, "custom:fixture-responses:fixture-model", keep_endpoint=True)

    assert seen["current_provider"] == (identity.get("requested_provider") or identity["provider"] or "openrouter")
    assert made["requested_provider"] == "custom:fixture-responses"
    assert ("base_url" in made, "api_mode" in made) == (keeps_endpoint, keeps_endpoint)
