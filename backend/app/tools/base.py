"""Tool definitions: named, schema-checked actions the brain may call.

Deliberately an allow-list of parameterized functions — never a field where
the model can put a free-form shell string (plan §2.3). Risk tiers and
confirmation gating arrive in Phase 3; everything registered today is
read-only.
"""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema for the arguments object
    handler: Callable[[dict], dict]
    risk: str = "read-only"
