"""Tool definitions: named, schema-checked actions the brain may call.

Deliberately an allow-list of parameterized functions — never a field where
the model can put a free-form shell string (plan §2.3).

Phase 3 adds the safety scaffolding: every tool carries a *risk tier*, and the
tier decides what has to happen before the action runs (plan §2.5). The policy
lives here so it is one table you can read top-to-bottom, rather than
permission checks scattered through the agent loop.

    read-only         silent                    e.g. system_stats
    reversible-write  one-tap confirmation      e.g. volume, open an app
    destructive       typed confirmation        e.g. delete a file
    external-facing   typed + disabled by default  e.g. send an email

An unrecognized tier falls back to the *strictest* policy, so forgetting to
tag a new tool makes it prompt rather than run unguarded.
"""

from dataclasses import dataclass
from typing import Callable

# --- risk tiers -----------------------------------------------------------

READ_ONLY = "read-only"
REVERSIBLE_WRITE = "reversible-write"
DESTRUCTIVE = "destructive"
EXTERNAL_FACING = "external-facing"

# --- confirmation modes ---------------------------------------------------

#: No confirmation — the tool runs as soon as the model asks for it.
CONFIRM_NONE = "none"
#: A single approve/deny (one tap in the UI, y/N in the CLI).
CONFIRM_TAP = "tap"
#: The human must retype a challenge phrase, so a "yes" in the voice reply or
#: an injected instruction cannot satisfy it.
CONFIRM_TYPED = "typed"


@dataclass(frozen=True)
class RiskPolicy:
    """What a risk tier demands before its tools are allowed to run."""

    tier: str
    confirmation: str = CONFIRM_NONE
    #: Disabled tiers never run, even with a valid confirmation.
    enabled: bool = True
    #: Shown to the human (and the model) when a tier is disabled.
    reason: str = ""


RISK_POLICIES: dict[str, RiskPolicy] = {
    READ_ONLY: RiskPolicy(READ_ONLY),
    REVERSIBLE_WRITE: RiskPolicy(REVERSIBLE_WRITE, CONFIRM_TAP),
    DESTRUCTIVE: RiskPolicy(DESTRUCTIVE, CONFIRM_TYPED),
    EXTERNAL_FACING: RiskPolicy(
        EXTERNAL_FACING,
        CONFIRM_TYPED,
        enabled=False,
        reason=(
            "external-facing actions (anything that sends data, money, or "
            "messages off this machine) are disabled by default"
        ),
    ),
}


def policy_for(risk: str) -> RiskPolicy:
    """Policy for a tier; unknown tiers get the strictest one we have."""
    return RISK_POLICIES.get(risk) or RISK_POLICIES[DESTRUCTIVE]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema for the arguments object
    handler: Callable[[dict], dict]
    risk: str = READ_ONLY
