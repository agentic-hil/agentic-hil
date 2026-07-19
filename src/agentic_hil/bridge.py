from __future__ import annotations

import json
import math
import queue
import subprocess
import threading

from agentic_hil.process import terminate_process_tree
from agentic_hil.types import JsonObject

CHILD_REAP_TIMEOUT_S = 5.0
BRIDGE_PROTOCOL_VERSION = 2
STDERR_TAIL_CHARS = 65536
ERROR_STDERR_TAIL_CHARS = 2000


class ProcessBridgeSession:
    """JSON-per-line request/response session with a configured bridge child process.

    Requests are ``{"id": N, "method": str, "params": object}``; the child answers
    with ``{"id": N, "result": object}`` or ``{"id": N, "error": object}`` on stdout.
    """

    adapter_name = "process"
    error_prefix = "bridge"
    bridge_label = "Bridge"

    def __init__(self, child: subprocess.Popen[str]):
        self.child = child
        self.pending: dict[int, queue.Queue[JsonObject]] = {}
        self.next_request_id = 1
        self.lock = threading.Lock()
        self.closed = False
        self.cleanup_required = False
        self.safe_state_confirmed = False
        self.process_reaped = False
        self.close_result: JsonObject | None = None
        self.stderr = ""
        try:
            threading.Thread(target=self._stdout_reader, daemon=True).start()
            threading.Thread(target=self._stderr_reader, daemon=True).start()
        except BaseException as primary_error:
            try:
                terminate_process_tree(self.child, CHILD_REAP_TIMEOUT_S)
            except BaseException as cleanup_error:
                raise RuntimeError(f"Bridge reader setup failed and process cleanup remains unconfirmed: {cleanup_error}") from primary_error
            raise

    def close(self) -> JsonObject:
        if self.closed and self.close_result is not None:
            return dict(self.close_result)
        response: JsonObject
        request_error: BaseException | None = None
        if self.safe_state_confirmed:
            response = {"ok": True, "protocol_version": BRIDGE_PROTOCOL_VERSION, "safe_state_confirmed": True}
        else:
            try:
                response = self.request("close", {}, 1)
            except BaseException as error:
                request_error = error
                response = self._bridge_error("close_interrupted", "Bridge close request was interrupted.")
            self.safe_state_confirmed = (
                response.get("ok") is True
                and response.get("protocol_version") == BRIDGE_PROTOCOL_VERSION
                and response.get("safe_state_confirmed") is True
                and not set(response) - {"ok", "protocol_version", "safe_state_confirmed", "safe_state", "backend", "summary"}
                and ("safe_state" not in response or isinstance(response["safe_state"], dict))
                and all(field not in response or isinstance(response[field], str) for field in ("backend", "summary"))
            )
        reap_error: BaseException | None = None
        try:
            terminate_process_tree(self.child, CHILD_REAP_TIMEOUT_S)
            self.process_reaped = True
        except BaseException as error:
            reap_error = error
        result: JsonObject = {
            "ok": self.safe_state_confirmed and self.process_reaped,
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "safe_state_confirmed": self.safe_state_confirmed,
            "process_reaped": self.process_reaped,
        }
        if not self.safe_state_confirmed:
            result.update({"error_type": "bridge_safe_state_unconfirmed", "summary": "Bridge did not confirm physical safe state before process cleanup.", "close_response": public_backend_result(response)})
        if reap_error is not None:
            result.update({"error_type": "bridge_process_reap_failed", "summary": "Bridge process cleanup could not be confirmed.", "backend_error": str(reap_error)})
        self.close_result = result
        self.cleanup_required = not result["ok"]
        self.closed = bool(result["ok"])
        interrupt = request_error if isinstance(request_error, (KeyboardInterrupt, SystemExit)) else reap_error if isinstance(reap_error, (KeyboardInterrupt, SystemExit)) else None
        if interrupt is not None:
            interrupt.args = (*interrupt.args, str(result.get("summary", "Bridge cleanup remains unconfirmed.")))
            raise interrupt
        if not result["ok"]:
            raise BridgeCleanupError(result)
        return dict(result)

    def status(self) -> JsonObject:
        return {
            "active": not self.closed and self.child.poll() is None,
            "backend": self.adapter_name,
            "cleanup_required": self.cleanup_required,
            "safe_state_confirmed": self.safe_state_confirmed,
            "process_reaped": self.process_reaped,
        }

    def request(self, method: str, params: JsonObject, timeout_s: float) -> JsonObject:
        if self.closed or self.child.poll() is not None:
            return self._bridge_error("process_exited", f"{self.bridge_label} process is not running.")
        with self.lock:
            request_id = self.next_request_id
            self.next_request_id += 1
            response_queue: queue.Queue[JsonObject] = queue.Queue(maxsize=1)
            self.pending[request_id] = response_queue
            try:
                self.child.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}, allow_nan=False) + "\n")
                self.child.stdin.flush()
            except (OSError, TypeError, ValueError):
                self.pending.pop(request_id, None)
                return self._bridge_error("invalid_request", f"{self.bridge_label} request could not be serialized or sent.")
        try:
            response = response_queue.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            self.pending.pop(request_id, None)
            return self._bridge_error("timeout", f"{self.bridge_label} request timed out.")
        has_error = "error" in response
        has_result = "result" in response
        if has_error == has_result or set(response) not in ({"id", "result"}, {"id", "error"}):
            return self._bridge_error("invalid_response", f"{self.bridge_label} must return exactly one of result or error.")
        if has_error:
            error = response["error"]
            if isinstance(error, dict):
                return {**error, "ok": False}
            return self._bridge_error("invalid_response", f"{self.bridge_label} returned a non-object error.")
        result = response["result"]
        if isinstance(result, dict) and isinstance(result.get("ok"), bool):
            return result
        return self._bridge_error("invalid_response", f"{self.bridge_label} returned a result without boolean ok.")

    def _bridge_error(self, kind: str, summary: str) -> JsonObject:
        result: JsonObject = {"ok": False, "adapter": self.adapter_name, "error_type": f"{self.error_prefix}_{kind}", "summary": summary}
        if self.stderr:
            result["stderr_tail"] = self.stderr[-ERROR_STDERR_TAIL_CHARS:]
        return result

    def _stdout_reader(self) -> None:
        for line in self.child.stdout:
            try:
                response = json.loads(line, parse_constant=_reject_non_finite, parse_float=_finite_float)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(response, dict):
                continue
            request_id = response.get("id")
            if type(request_id) is not int:
                continue
            queue_ = self.pending.pop(request_id, None)
            if queue_ is not None:
                queue_.put(response)

    def _stderr_reader(self) -> None:
        for line in self.child.stderr:
            self.stderr = (self.stderr + line)[-STDERR_TAIL_CHARS:]


def public_backend_result(result: JsonObject, omit: list[str] | None = None) -> JsonObject:
    omit_set = {"session", *(omit or [])}
    return {key: value for key, value in result.items() if key not in omit_set}


def _reject_non_finite(value: str) -> float:
    raise ValueError(f"Non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Non-finite JSON number: {value}")
    return parsed


class BridgeCleanupError(RuntimeError):
    def __init__(self, result: JsonObject):
        super().__init__(str(result.get("summary", "Bridge cleanup failed.")))
        self.result = result
