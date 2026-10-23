"""Bench power supplies: the static type table and the load-time secret registry (issue #731).

This module is a quality-gates stub. Its only job is to make the product gaps of
the `power_supplies` section visible to the acceptance tests:

- ``POWER_SUPPLY_TYPES`` holds no record yet, so no supply type is known.
- ``register_config_secrets`` is not implemented. The loader calls it first, and
  for a document that carries a ``power_supplies`` section it refuses to go on.

Documents without that section are unaffected, so the rest of the suite still
loads exactly as before.
"""

from __future__ import annotations

from typing import Any

# The record of each supply type is not written yet. An empty table gives no
# record for any type, so a lookup fails at this gap instead of against an
# invented value.
POWER_SUPPLY_TYPES: dict[str, dict[str, Any]] = {}


def register_config_secrets(raw_document: Any) -> None:
    """Register the serials of a raw document's supplies before anything can refuse it.

    Not implemented: a document with a ``power_supplies`` section stops here.
    """
    if isinstance(raw_document, dict) and "power_supplies" in raw_document:
        raise NotImplementedError("power supply secret registration is not implemented")
