"""Capability-based model routing: graph nodes ask for a role, never for a specific server.

Each configured model declares a ``tier`` (``large`` or ``small``) and may restrict
its ``roles``. A role resolves to a preferred tier; the routing profile and the
observed health of each model choose the concrete model. Every fallback is an
explicit, logged decision that the configuration permits. An unhealthy model is
never silently replaced by a weaker one for complex engineering roles.
"""
from __future__ import annotations

import json

from .models import ModelError

LARGE, SMALL = "large", "small"
TIERS = (LARGE, SMALL)

# Preferred tier per worker role. Complex roles require the large tier unless the
# operator lists them in ``small_fallback_roles``; bounded roles may escalate.
ROLE_TIERS = {
    "classification": SMALL, "investigation": SMALL, "question_answering": SMALL,
    "regression_review": SMALL, "review": SMALL,
    "root_investigation": LARGE, "planning": LARGE, "implementation": LARGE,
    "regression_test": LARGE, "deep_review": LARGE,
}
COMPLEX_ROLES = frozenset(role for role, tier in ROLE_TIERS.items() if tier == LARGE)
# Roles that always prefer the small tier, whatever the profile (a bounded, cheap worker by design).
# A measured assignment ("assigned") still decides their order.
SMALL_FIRST_ROLES = frozenset({"question_answering"})
PROFILES = ("baseline", "multimodel", "assigned")
# "assigned" follows config["role_routes"]: per-role ordered model lists produced by
# measured model assignment (model_assignment.py). Each list already contains only
# the models that met that role's requirements, so it is also the fallback order.


class RoutingError(ModelError):
    """No configured, healthy model may perform the role under current policy."""


def tier_of(models: dict, key: str) -> str:
    model = models.get(key, {})
    if model.get("tier") in TIERS:
        return model["tier"]
    # Legacy configurations predate tiers: infer from the advertised context window.
    return SMALL if (model.get("context") or 0) and model["context"] <= 16384 else LARGE


def preferred_tier(config: dict, role: str, requested: str | None = None) -> str:
    """``baseline`` keeps the measured large-model-only default; ``multimodel`` uses role tiers."""
    assessed = config.get("routing") == "assigned" and role in config.get("role_routes", {})
    if role in SMALL_FIRST_ROLES and not assessed:
        return SMALL
    if config.get("routing", "baseline") == "baseline":
        return LARGE
    if assessed:
        route = config["role_routes"][role] if isinstance(config["role_routes"][role], list) else []
        if route and route[0] in config.get("models", {}):
            return tier_of(config["models"], route[0])
    return requested if requested in TIERS else ROLE_TIERS.get(role, LARGE)


def candidates(config: dict, role: str, requested: str | None = None) -> list[str]:
    """Ordered model keys permitted for this role: preferred tier first, then permitted fallbacks."""
    models = config.get("models", {})
    # roles=None (absent) permits every role; an explicit list, even empty, is an allow-list.
    usable = [k for k, m in models.items() if m.get("model_id") and (m.get("roles") is None or role in m["roles"])]
    routes = config.get("role_routes", {})
    if config.get("routing") == "assigned" and role in routes:
        # Measured assignment: only the listed models, in the listed order. An assessed role
        # with an empty route has no eligible model and must fail closed, not guess.
        # A malformed (non-list) route counts as empty: fail closed, never crash selection.
        ordered = [k for k in routes[role] if k in usable] if isinstance(routes[role], list) else []
    else:
        # Roles the assignment never assessed (e.g. an older routing.json) use tier routing.
        tier = preferred_tier(config, role, requested)
        ordered = [k for k in usable if tier_of(models, k) == tier]
        # A bounded role may escalate to a larger model; a complex role may use a small
        # model only when the operator explicitly lists that role.
        if tier == SMALL or role in config.get("small_fallback_roles", []):
            ordered += [k for k in usable if k not in ordered]
    override = config.get("model_overrides", {}).get(role)
    if override:
        if override not in usable:
            raise RoutingError(f"Override model '{override}' is not configured with a discovered identifier for role {role}")
        ordered = [override] + [k for k in ordered if k != override]
    return ordered


def observed_health(db) -> dict[str, bool]:
    """Latest scheduler health observations; missing table or rows mean 'unknown', not 'down'."""
    try:
        rows = db.execute("SELECT key,data FROM model_observations").fetchall()
    except Exception:
        return {}
    return {row[0]: bool(json.loads(row[1]).get("available")) for row in rows}


def select(config: dict, role: str, requested: str | None = None, health: dict[str, bool] | None = None) -> tuple[str, dict]:
    """Return (model_key, decision). Raises RoutingError when policy leaves no healthy model."""
    ordered = candidates(config, role, requested)
    if not ordered:
        raise RoutingError(f"No configured model with a discovered identifier may perform role {role}")
    health = health or {}
    healthy = [k for k in ordered if health.get(k, True)]
    if not healthy:
        raise RoutingError(f"Every model permitted for role {role} is observed unavailable: {', '.join(ordered)}",
                           transient=True, phase="health")
    chosen = healthy[0]
    decision = {"role": role, "preferred_tier": preferred_tier(config, role, requested), "candidates": ordered,
                "selected": chosen, "selected_tier": tier_of(config.get("models", {}), chosen),
                "degraded": chosen != ordered[0], "skipped_unavailable": [k for k in ordered[:ordered.index(chosen)]]}
    return chosen, decision


def role_label(config: dict, role: str, fallback: str = "the model") -> str:
    """Display name of the model a role would use first (configuration only, health ignored)."""
    try:
        ordered = candidates(config, role)
    except RoutingError:
        ordered = []
    return config.get("models", {}).get(ordered[0], {}).get("label") or ordered[0] if ordered else fallback


def larger_candidate(config: dict, role: str, current: str, exclude=frozenset()) -> str | None:
    """A large-tier model for escalation when a small model cannot hold the task (never an excluded one)."""
    models = config.get("models", {})
    return next((k for k in candidates(config, role, LARGE)
                 if k != current and k not in exclude and tier_of(models, k) == LARGE), None)
