"""HardCI - safe local hardware-in-the-loop bridge for AI agents."""

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

if TYPE_CHECKING:
    from hardci.artifacts import ArtifactManager
    from hardci.config import ConfigError, load_config
    from hardci.tools import HardCIToolService

__all__ = ["ArtifactManager", "ConfigError", "HardCIToolService", "load_config"]

# Lazy re-exports (PEP 562): the pytest11 entry point imports this package on
# every pytest startup, so the package root must not pull in yaml/jsonschema.
_LAZY_EXPORTS = {
    "ArtifactManager": ("hardci.artifacts", "ArtifactManager"),
    "ConfigError": ("hardci.config", "ConfigError"),
    "HardCIToolService": ("hardci.tools", "HardCIToolService"),
    "load_config": ("hardci.config", "load_config"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        from importlib import import_module

        module_name, attribute = _LAZY_EXPORTS[name]
        return getattr(import_module(module_name), attribute)
    raise AttributeError(f"module 'hardci' has no attribute {name!r}")
