"""Named routes sharing an endpoint keep their native Desktop identity."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
import yaml

from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tui_gateway import server


@pytest.fixture
def routes(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    url = "https://shared.example.invalid/v1"
    config = {
        "model": {
            "default": "fixture-model",
            "provider": "custom:fixture-responses",
            "context_length": 131072,
        },
        "providers": {
            f"fixture-{suffix}": {
                "base_url": url,
                "transport": transport,
                "api_key": "fixture-key",
                "default_model": "fixture-model",
                "models": ["fixture-model"],
            }
            for suffix, transport in (
                ("chat", "chat_completions"),
                ("responses", "codex_responses"),
            )
        },
        "platform_toolsets": {"desktop": ["no_mcp"]},
        "mcp_servers": {},
        "compression": {"enabled": False},
        "memory": {
            "provider": "builtin",
            "memory_enabled": False,
            "user_profile_enabled": False,
        },
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_probe_credentials", lambda agent: None)
    monkeypatch.setattr("hermes_cli.banner.get_update_result", lambda **kw: None)
    monkeypatch.setattr("hermes_cli.banner.get_available_skills", lambda: {})
    return home, url


def _agent(home, url, **extra):
    return SimpleNamespace(
        model="fixture-model",
        provider="custom",
        requested_provider="custom:fixture-responses",
        base_url=url,
        api_mode="codex_responses",
        reasoning_config=None,
        service_tier=None,
        session_id="native-identity",
        **extra,
    )


def test_explicit_native_identity_wins_over_ambiguous_endpoint(routes):
    home, url = routes
    agent = _agent(home, url)
    metadata = server._runtime_model_config(agent, {"_branched_from": "original"})
    assert metadata["provider"] == agent.requested_provider
    assert metadata["api_mode"] == agent.api_mode
    assert metadata["_branched_from"] == "original"
    assert "api_key" not in metadata


def test_live_display_and_resume_identity_agree(routes):
    home, url = routes
    agent = _agent(home, url)
    session = {
        "agent": agent,
        "cwd": str(home),
        "session_key": agent.session_id,
        "profile_home": str(home),
    }
    assert server._session_info(agent, session)["provider"] == agent.requested_provider
    assert server._live_session_identity(session) == (
        agent.model,
        agent.requested_provider,
    )
    session["_metadata_mirror"] = {"model": "remote-model", "provider": "custom:remote"}
    assert server._live_session_identity(session) == ("remote-model", "custom:remote")
    assert server._session_info(agent, session)["provider"] == "custom:remote"
    session["pending_model_switch"] = {
        "display_model": "pending-model",
        "display_provider": "custom:pending",
    }
    assert server._live_session_identity(session) == ("pending-model", "custom:pending")
    assert server._session_info(agent, session)["provider"] == "custom:pending"


@pytest.mark.parametrize("requested", [None, "", "custom", "auto"])
def test_legacy_bare_identity_keeps_existing_endpoint_recovery(routes, requested):
    home, url = routes
    agent = _agent(home, url)
    agent.requested_provider = requested
    assert server._runtime_model_config(agent)["provider"] == "custom:fixture-chat"


@pytest.mark.parametrize("requested", [None, "", "custom", "auto"])
def test_legacy_non_custom_provider_is_not_lost(routes, requested):
    home, url = routes
    native = _agent(home, url)
    native.provider = "anthropic"
    native.requested_provider = requested
    assert server._runtime_model_config(native)["provider"] == native.provider


@pytest.mark.parametrize("lookup_error", [False, True])
def test_legacy_unresolved_lookup_never_invents_a_named_route(
    routes, monkeypatch, lookup_error
):
    from hermes_cli import runtime_provider

    home, url = routes
    native = _agent(home, url)
    native.requested_provider = "custom"

    def lookup(**kwargs):
        if lookup_error:
            raise RuntimeError("Synthetic catalog unavailable")
        return None

    monkeypatch.setattr(runtime_provider, "canonical_custom_identity", lookup)
    assert server._runtime_model_config(native)["provider"] == "custom"


def test_metadata_reads_do_not_hydrate_external_secret_sources(routes, monkeypatch):
    home, url = routes
    native = _agent(home, url)
    session = {
        "agent": native,
        "cwd": str(home),
        "session_key": native.session_id,
        "profile_home": str(home),
    }
    hydrated = []
    monkeypatch.setattr(
        "hermes_cli.env_loader.hydrate_profile_secret_sources",
        lambda *args: hydrated.append(args),
    )
    assert (
        server._session_info(native, session)["provider"] == native.requested_provider
    )
    assert server._live_session_identity(session) == (
        native.model,
        native.requested_provider,
    )
    assert hydrated == []


def test_same_session_keeps_its_selected_route_after_profile_config_changes(routes):
    home, url = routes
    native = _agent(home, url)
    session = {
        "agent": native,
        "cwd": str(home),
        "session_key": native.session_id,
        "profile_home": str(home),
    }
    path = home / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["model"]["provider"] = "custom:fixture-chat"
    config["providers"] = dict(reversed(list(config["providers"].items())))
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    assert (
        server._session_info(native, session)["provider"] == native.requested_provider
    )
    assert server._live_session_identity(session) == (
        native.model,
        native.requested_provider,
    )


def test_live_model_switch_and_once_snapshot_keep_selected_route(routes):
    home, url = routes
    native = _agent(home, url)
    snapshot = server._snapshot_agent_model_runtime(native)
    assert snapshot["provider"] == native.requested_provider
    assert server._current_model_runtime(native, "")[0] == native.requested_provider
    with server._profile_build_scope(home):
        assert server._current_model_runtime(None, "")[0] == native.requested_provider


def test_native_once_restore_without_primary_runtime_keeps_named_identity(routes):
    home, url = routes
    native = None
    db = SessionDB(home / "state.db")
    try:
        with server._profile_build_scope(home):
            native = server._make_agent(
                "native-ui",
                "native-once",
                session_db=db,
                platform_override="desktop",
                cwd_override=str(home),
            )
            native._primary_runtime = None
            snapshot = server._snapshot_agent_model_runtime(native)
            original_provider = native.requested_provider
            native.switch_model(
                new_model=native.model,
                new_provider="custom:fixture-chat",
                base_url=url,
                api_mode="chat_completions",
                api_key="fixture-key",
            )
            server._restore_agent_model_runtime(native, snapshot)
            assert native.requested_provider == original_provider
            assert native.api_mode == snapshot["api_mode"] == "codex_responses"
            assert native._api_call_count == 0
    finally:
        if native is not None:
            native.close()
        db.close()


def test_native_runtime_round_trip_re_resolves_the_selected_route(routes):
    home, url = routes
    native = _agent(home, url)
    db = SessionDB(home / "state.db")
    try:
        db.create_session(native.session_id, source="desktop", model=native.model)
        native._session_db = db
        session = {
            "agent": native,
            "session_key": native.session_id,
            "profile_home": str(home),
        }
        with server._profile_build_scope(home):
            server._persist_live_session_runtime(session)
            row = db.get_session(native.session_id)
            overrides = server._stored_session_runtime_overrides(row)
            model, restored = server._resolve_agent_model_runtime(
                overrides["model_override"], overrides.get("provider_override")
            )
        assert model == native.model
        assert restored["requested_provider"] == native.requested_provider
        assert restored["api_mode"] == native.api_mode
        assert restored["base_url"] == native.base_url
    finally:
        db.close()


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        if self.target:
            self.target(*self.args, **self.kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


@pytest.mark.parametrize("one_turn", [False, True])
def test_ordinary_turn_persistence_preserves_cache_and_once_failure_restores(
    routes, monkeypatch, one_turn
):
    home, url = routes
    db = SessionDB(home / "state.db")
    native = _agent(
        home,
        url,
        _session_db=db,
        clear_interrupt=lambda: None,
        run_conversation=lambda *args, **kwargs: {"final_response": "done"},
    )
    original = server._snapshot_agent_model_runtime(native)
    original_provider = native.requested_provider
    native._primary_runtime = {
        "model": native.model,
        "provider": native.provider,
        "requested_provider": native.requested_provider,
        "base_url": native.base_url,
        "api_mode": native.api_mode,
    }

    def restore_primary():
        native.__dict__.update(native._primary_runtime)
        return True

    native._restore_primary_runtime = restore_primary
    original = server._snapshot_agent_model_runtime(native)
    native.system_prompt = "Byte-stable original system prefix."
    native.messages = [{"role": "user", "content": "Retained native context."}]
    before_context = json.dumps(native.messages)
    if one_turn:
        native.model = "one-turn-model"
        native.provider = native.requested_provider = "custom:fixture-chat"
        native.api_mode = "chat_completions"

        def fail_turn(*args, **kwargs):
            raise RuntimeError("Synthetic one-turn failure")

        native.run_conversation = fail_turn
    observations = []
    run_turn = native.run_conversation

    def observe_turn(*args, **kwargs):
        observations.append(
            json.loads(db.get_session(native.session_id)["model_config"])
        )
        return run_turn(*args, **kwargs)

    native.run_conversation = observe_turn
    session = {
        "agent": native,
        "session_key": native.session_id,
        "profile_home": str(home),
        "cwd": str(home),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": True,
        "attached_images": [],
        "cols": 80,
        "source": "desktop",
        **({"one_turn_model_restore": original} if one_turn else {}),
    }
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda *args: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *args: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *args: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None, raising=False)
    monkeypatch.setattr(
        server, "_sync_session_key_after_compress", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(server, "_get_usage", lambda *args: {})
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *args: None)
    monkeypatch.setattr(
        server, "_persist_live_session_system_prompt", lambda *args: None
    )
    anchor = {"prompt_tokens": 47, "base_count": 1, "base_last_role": "user"}
    try:
        db.create_session(
            native.session_id,
            source="desktop",
            model=original["model"],
            model_config={
                "reasoning_config": {"enabled": True},
                "_usage_anchor": anchor,
            },
        )
        db.append_message(
            native.session_id, role="user", content="Protected original source."
        )
        protected = db.get_messages(native.session_id)
        with server._profile_build_scope(home):
            server._run_prompt_submit(
                "rid", "native-ui", session, "Read-only synthetic turn."
            )
        row = db.get_session(native.session_id)
        metadata = json.loads(row["model_config"])
        assert metadata["provider"] == native.requested_provider
        assert metadata["api_mode"] == native.api_mode
        assert metadata["base_url"] == native.base_url
        assert metadata["_usage_anchor"] == anchor
        assert row["model"] == original["model"]
        assert native.requested_provider == original_provider
        assert db.get_messages(native.session_id)[0] == protected[0]
        assert native.system_prompt == "Byte-stable original system prefix."
        assert json.dumps(native.messages) == before_context
        assert observations
        if one_turn:
            assert "provider" not in observations[0]
            assert "api_mode" not in observations[0]
        else:
            assert observations[0]["provider"] == native.requested_provider
        assert session["running"] is False
    finally:
        db.close()


def test_real_native_agents_alternate_profiles_without_inference(routes, monkeypatch):
    home, url = routes
    other = home.parent / "other"
    other.mkdir()
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    config["model"]["provider"] = "custom:fixture-chat"
    (other / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    identities = []
    for root in (home, other, home):
        db = SessionDB(root / "state.db")
        native = None
        try:
            with server._profile_build_scope(root):
                native = server._make_agent(
                    "native-ui",
                    "native-profile",
                    session_db=db,
                    platform_override="desktop",
                    cwd_override=str(root),
                )
                metadata = server._runtime_model_config(native)
                identities.append((metadata["provider"], metadata["api_mode"]))
                assert metadata["base_url"] == url
                assert native._api_call_count == 0
        finally:
            if native is not None:
                native.close()
            db.close()
        assert get_hermes_home() == home
    assert identities[0] == identities[2]
    assert identities[0] == ("custom:fixture-responses", "codex_responses")
    assert identities[1] == ("custom:fixture-chat", "chat_completions")
