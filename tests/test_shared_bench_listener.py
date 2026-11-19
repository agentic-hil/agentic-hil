"""The bench server's listener on a LAN the operator trusts (#891).

Sockets bind 127.0.0.1 alone. A peer on another machine is the address the
listener is told a connection came from (`foreign_peer`), and a wildcard listen
is proven on the settings and the pure predicates, never by binding 0.0.0.0.
"""

from __future__ import annotations

import copy
import json
import socket
from dataclasses import replace
from http.client import HTTPException
from ipaddress import ip_network
from pathlib import Path
from typing import Any

import pytest
import yaml
from bench_job_fakes import job_bench
from httpserver_fakes import (
    JOB_ID,
    JOB_PATH,
    ScriptedJobService,
    foreign_peer,
    multipart,
    no_cors,
    request,
    running_http,
    short_settings,
)

from agentic_hil import httpapi, httpserver
from agentic_hil.config import ConfigError, load_authoritative_config, validate_config_document
from agentic_hil.httpapi import JobServiceError, JobStatus, ServerSettings
from agentic_hil.knowledge import remediation_fields

PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def test_the_listener_settings_come_from_the_server_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    # Without `server.listen` the listener is loopback and nothing else.
    loopback = httpapi.load_server_settings(bench.config)
    assert loopback is not None
    assert loopback.host == "127.0.0.1"
    server = httpserver.create_http_server(replace(loopback, port=0), ScriptedJobService())
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()

    configured = httpapi.load_server_settings(bench.with_server(
        listen="0.0.0.0:8765", hostnames=["bench.example"], limits={"max_upload_mb": 7, "max_artifact_wait_s": 30},
    ))
    assert configured is not None
    if (configured.host, configured.port, configured.hostnames) != ("0.0.0.0", 8765, ("bench.example",)):
        raise AssertionError("load_server_settings does not take the listen address from the server block")
    assert configured.allowed_networks == tuple(ip_network(network) for network in PRIVATE_RANGES)
    assert (configured.max_upload_mb, configured.max_artifact_wait_s) == (7, 30)

    narrowed = httpapi.load_server_settings(bench.with_server(allowed_networks=["192.168.0.0/24"]))
    assert narrowed is not None
    assert narrowed.allowed_networks == (ip_network("192.168.0.0/24"),)


def test_allowed_networks_default_to_the_private_ranges_and_refuse_other_forms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    server = load_authoritative_config(bench.workspace).server
    if getattr(server, "allowed_networks", None) != PRIVATE_RANGES:
        raise AssertionError("server.allowed_networks does not default to the three private ranges")

    document = bench.document()

    def with_networks(value: Any) -> dict[str, Any]:
        candidate = copy.deepcopy(document)
        candidate["server"]["allowed_networks"] = value
        return candidate

    # Validation is pure: it resolves no name and opens no socket.
    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("server validation attempted network access")

    with monkeypatch.context() as network:
        network.setattr(socket, "getaddrinfo", no_network)
        network.setattr(socket, "socket", no_network)
        for accepted in ([], ["192.168.0.0/24"], ["10.0.0.0/8", "203.0.113.0/24"]):
            validate_config_document(with_networks(accepted), str(bench.config_path))
        for refused in (["fd00::/8"], ["192.168.1.1/16"], ["not-a-network"], ["10.0.0.0/33"]):
            with pytest.raises(ConfigError) as refusal:
                validate_config_document(with_networks(refused), str(bench.config_path))
            assert refusal.value.error_type == "config_invalid", refused
            assert refusal.value.details.get("field") == "server.allowed_networks", (refused, refusal.value.details)
        with pytest.raises(ConfigError) as typed:
            validate_config_document(with_networks([5]), str(bench.config_path))
        assert typed.value.error_type == "config_invalid"

    bench.config_path.write_text(yaml.safe_dump(with_networks([]), sort_keys=False), encoding="utf-8")
    assert load_authoritative_config(bench.workspace).server.allowed_networks == ()


def test_a_peer_outside_allowed_networks_is_closed_before_a_byte_is_read() -> None:
    lan = short_settings(allowed_networks=(ip_network("10.0.0.0/8"),))
    assert httpserver.peer_admitted("203.0.113.5", lan) is False
    assert httpserver.peer_admitted("192.168.0.10", lan) is False
    assert httpserver.peer_admitted("10.1.2.3", lan) is True
    # Loopback is admitted whatever the list says, so the bench machine keeps its own server.
    loopback_only = short_settings(allowed_networks=())
    assert httpserver.peer_admitted("127.0.0.1", loopback_only) is True
    assert httpserver.peer_admitted("127.0.0.2", loopback_only) is True
    assert httpserver.peer_admitted("10.1.2.3", loopback_only) is False
    # The default is the three private ranges.
    assert httpserver.peer_admitted("192.168.0.10", short_settings()) is True
    assert httpserver.peer_admitted("192.0.2.10", short_settings()) is False

    service = ScriptedJobService()
    public = {"job_id": JOB_ID, "state": "queued"}
    service.script("status", JobStatus(1, public))
    with running_http(service) as server:
        with foreign_peer(server, "203.0.113.5"), pytest.raises((OSError, HTTPException)):
            request(server, "GET", JOB_PATH)
        assert service.calls == []
        with foreign_peer(server, "10.1.2.3"):
            admitted = request(server, "GET", JOB_PATH)
    assert admitted.status == 200
    assert admitted.json() == public


def test_a_browser_page_on_the_lan_cannot_submit_or_read_an_answer() -> None:
    """A page in a colleague's browser sends its Origin with a form POST and with a script's request.

    Either is refused before a byte of the body is read, and no answer carries a
    header that would let the page read it."""
    service = ScriptedJobService()
    form, form_type = multipart()
    settings = short_settings(hostnames=("bench.example",))
    with running_http(service, settings) as server, foreign_peer(server):
        host = f"bench.example:{server.server_port}"
        for origin in ("http://page.example", "null"):
            posted = request(
                server, "POST", "/v1/jobs", body=form, withhold_body=True,
                headers={"Host": host, "Origin": origin, "Content-Type": form_type},
            )
            assert posted.status == 403, posted
            assert posted.json()["error_type"] == "origin_refused"
            no_cors(posted)
            read = request(server, "GET", JOB_PATH, headers={"Host": host, "Origin": origin})
            assert read.status == 403, read
            no_cors(read)
    assert service.calls == []


def test_the_host_rule_for_a_specific_and_a_wildcard_listen() -> None:
    specific = short_settings(host="192.168.0.10", port=8765, hostnames=("bench.example",))
    assert httpserver.host_allowed("192.168.0.10:8765", specific, 8765, "192.168.0.10") is True
    assert httpserver.host_allowed("192.168.0.10", specific, 8765, "192.168.0.10") is True
    assert httpserver.host_allowed("BENCH.EXAMPLE:8765", specific, 8765, "192.168.0.10") is True
    assert httpserver.host_allowed("evil.example:8765", specific, 8765, "192.168.0.10") is False
    assert httpserver.host_allowed("192.168.0.10:9999", specific, 8765, "192.168.0.10") is False
    assert httpserver.host_allowed("127.0.0.1:8765", specific, 8765, "192.168.0.10") is False

    wildcard = short_settings(host="0.0.0.0", port=8765, hostnames=("bench.example",))
    # The address the connection arrived on, so no client has to send the wildcard.
    assert httpserver.host_allowed("192.168.0.10:8765", wildcard, 8765, "192.168.0.10") is True
    assert httpserver.host_allowed("127.0.0.1:8765", wildcard, 8765, "127.0.0.1") is True
    assert httpserver.host_allowed("127.0.0.1:8765", wildcard, 8765, "192.168.0.10") is False
    assert httpserver.host_allowed("bench.example", wildcard, 8765, "192.168.0.10") is True
    assert httpserver.host_allowed("0.0.0.0:8765", wildcard, 8765, "192.168.0.10") is False
    # A rebinding page sends its own name.
    assert httpserver.host_allowed("evil.example:8765", wildcard, 8765, "192.168.0.10") is False
    assert httpserver.host_allowed("192.168.0.10:9999", wildcard, 8765, "192.168.0.10") is False


def test_listen_takes_an_ipv4_literal_and_refuses_names() -> None:
    httpserver.validate_listen_address(ServerSettings(host="0.0.0.0"))
    httpserver.validate_listen_address(ServerSettings(host="192.168.0.10"))
    httpserver.validate_listen_address(ServerSettings(host="127.0.0.1"))
    for host in ("bench.example", "::1", "[::1]", "::"):
        with pytest.raises(JobServiceError) as refused:
            httpserver.validate_listen_address(ServerSettings(host=host))
        assert refused.value.error_type == "server_listen_refused", host
        assert "#891" not in json.dumps({**refused.value.to_dict(), **remediation_fields("server_listen_refused")})
