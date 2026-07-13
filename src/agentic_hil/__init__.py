"""Agentic HIL - safe local hardware-in-the-loop bridge for AI agents."""

from typing import TYPE_CHECKING, Any

__version__ = "0.2.2"

if TYPE_CHECKING:
    from agentic_hil.artifacts import ArtifactManager
    from agentic_hil.config import ConfigError, load_config
    from agentic_hil.tools import AgenticHILToolService

__all__ = ["ArtifactManager", "ConfigError", "AgenticHILToolService", "load_config"]

# Lazy re-exports (PEP 562): the pytest11 entry point imports this package on
# every pytest startup, so the package root must not pull in yaml/jsonschema.
_LAZY_EXPORTS = {
    "ArtifactManager": ("agentic_hil.artifacts", "ArtifactManager"),
    "ConfigError": ("agentic_hil.config", "ConfigError"),
    "AgenticHILToolService": ("agentic_hil.tools", "AgenticHILToolService"),
    "load_config": ("agentic_hil.config", "load_config"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        from importlib import import_module

        module_name, attribute = _LAZY_EXPORTS[name]
        return getattr(import_module(module_name), attribute)
    raise AttributeError(f"module 'agentic_hil' has no attribute {name!r}")
