"""Claude-compatible tool schemas and their server-side validation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import write_config

from agentic_hil.config import load_config
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

FORBIDDEN_ROOT_COMBINATORS = {"oneOf", "allOf", "anyOf"}
PUBLICATION_FAILURE = "Published tool input schemas contain forbidden top-level combinators"


@pytest.fixture
def tool_service(tmp_path: Path) -> Iterator[AgenticHILToolService]:
    """Create an isolated service with a configured, unused COM port."""
    config_path = write_config(
        tmp_path,
        com_ports_yaml='''com_ports:
  schema_uart:
    device: "/dev/ttySCHEMA0"
''',
    )
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        yield service
    finally:
        service.close()


def test_tools_list_omits_top_level_combinators(tool_service: AgenticHILToolService) -> None:
    response = handle_mcp_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        tool_service,
    )
    assert isinstance(response, dict)
    listed = response["result"]["tools"]
    assert isinstance(listed, list) and listed

    tools_by_name = {tool["name"]: tool for tool in listed}
    affected = {"artifact_upload", "flash_firmware", "debug_start_session", "com_write"}
    assert affected <= tools_by_name.keys()

    offenders: list[tuple[str, str]] = []
    for name, tool in tools_by_name.items():
        schema = tool["inputSchema"]
        assert schema["type"] == "object", name
        offenders.extend((name, key) for key in FORBIDDEN_ROOT_COMBINATORS if key in schema)

    if offenders:
        raise AssertionError(PUBLICATION_FAILURE)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        pytest.param("artifact_upload", {}, id="upload-neither"),
        pytest.param(
            "artifact_upload",
            {
                "image_path": "build/app.elf",
                "filename": "app.elf",
                "data_base64": "f0VMRgAAAAAAAAAAAAAAAA==",
            },
            id="upload-both",
        ),
        pytest.param("com_write", {"port_id": "schema_uart"}, id="com-neither"),
        pytest.param(
            "com_write",
            {"port_id": "schema_uart", "text": "PING", "hex": "50"},
            id="com-both",
        ),
    ],
)
def test_invalid_upload_and_com_inputs_do_not_dispatch(
    tool_service: AgenticHILToolService,
    name: str,
    arguments: dict[str, str],
) -> None:
    upload = Mock(side_effect=AssertionError("artifact upload dispatched"))
    write = Mock(side_effect=AssertionError("COM write dispatched"))
    tool_service.artifacts.upload = upload
    tool_service.com_ports.write = write

    result = tool_service.call(name, arguments)

    assert result["ok"] is False, result
    assert result["error_type"] == "invalid_argument", result
    upload.assert_not_called()
    write.assert_not_called()
