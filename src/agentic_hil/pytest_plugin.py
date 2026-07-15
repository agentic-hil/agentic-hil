"""pytest plugin exposing Agentic HIL fixtures for hardware-in-the-loop test suites.

Usage in a configured firmware project:

    def test_target_is_available(agentic_hil):
        result = agentic_hil.call("probe_target")
        assert result["ok"] is True

Tests using the fixtures are skipped when neither the canonical external config nor
an ``AGENTIC_HIL_CONFIG`` override exists. An invalid configuration fails loudly
instead, so a typo cannot silently disable the hardware suite in CI.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentic_hil.tools import AgenticHILToolService
    from agentic_hil.types import AgenticHILConfig

CONFIG_ENV = "AGENTIC_HIL_CONFIG"


@pytest.fixture(scope="session")
def agentic_hil_config(request: pytest.FixtureRequest) -> AgenticHILConfig:
    """The validated Agentic HIL project configuration.

    Skips when no config exists; fails when an available config is invalid.
    """
    from agentic_hil.config import ConfigError, load_authoritative_config, project_config_path

    if not os.environ.get(CONFIG_ENV) and not project_config_path(request.config.rootpath).is_file():
        pytest.skip("Agentic HIL configuration unavailable: canonical external config does not exist")

    try:
        return load_authoritative_config(request.config.rootpath)
    except ConfigError as error:
        pytest.fail(
            f"Agentic HIL configuration invalid ({error.error_type}): {error.summary}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def _agentic_hil_service(agentic_hil_config: AgenticHILConfig) -> Iterator[AgenticHILToolService]:
    from agentic_hil.tools import AgenticHILToolService

    service = AgenticHILToolService(agentic_hil_config)
    try:
        yield service
    finally:
        service.close()


@pytest.fixture()
def agentic_hil(_agentic_hil_service: AgenticHILToolService) -> Iterator[AgenticHILToolService]:
    """A ready AgenticHILToolService; call tools by name exactly like an MCP agent would.

    The service (config, debugger backend) is shared across the session, but
    Debug, COM, and CAN sessions opened during a test are stopped afterwards so
    state cannot leak between tests.
    """
    try:
        yield _agentic_hil_service
    finally:
        errors: list[str] = []
        if _agentic_hil_service._debug_artifact is not None:
            stopped = _agentic_hil_service.debug_stop_session()
            if not stopped.get("ok"):
                errors.append(f"debug: {stopped.get('summary', stopped.get('error_type'))}")
        for name, resource in [("COM", _agentic_hil_service.com_ports), ("CAN", _agentic_hil_service.can_buses)]:
            try:
                resource.close()
            except Exception as error:
                errors.append(f"{name}: {type(error).__name__}: {error}")
        if errors:
            pytest.fail("Agentic HIL fixture cleanup failed: " + "; ".join(errors), pytrace=False)
