"""Durable native route identity, distinct from the resolved billing class."""

import logging


logger = logging.getLogger(__name__)


def agent_provider_identity(agent) -> str:
    """Prefer the selected route; endpoint recovery is only for legacy bare identities."""
    if agent is None:
        return ""
    requested = str(getattr(agent, "requested_provider", "") or "").strip()
    provider = str(getattr(agent, "provider", "") or "").strip()
    if requested and requested.lower() not in {"custom", "auto"}:
        return requested
    if provider.lower() == "custom":
        try:
            from hermes_cli.runtime_provider import canonical_custom_identity

            return (
                canonical_custom_identity(
                    base_url=str(getattr(agent, "base_url", "") or "").strip(),
                    model=str(getattr(agent, "model", "") or "").strip() or None,
                )
                or provider
            )
        except Exception:
            logger.debug("custom provider identity lookup failed", exc_info=True)
    return provider
