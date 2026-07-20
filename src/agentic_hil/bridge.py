from __future__ import annotations

import json
import queue
import subprocess
import threading
from dataclasses import dataclass

from agentic_hil.types import JsonObject

CHILD_REAP_TIMEOUT_S = 5.0
STDERR_TAIL_CHARS = 65536
ERROR_STDERR_TAIL_CHARS = 2000


def reap_unmanaged_child(child: subprocess.Popen[str]) -> bool:
    if child.poll() is not None:
        return True
    try:
        child.terminate()
        child.wait(timeout=CHILD_REAP_TIMEOUT_S)
    except BaseException:
        try:
            child.kill()
            child.wait(timeout=CHILD_REAP_TIMEOUT_S)
        except BaseException:
            pass
    return child.poll() is not None


@dataclass(frozen=True)
class BridgeCloseResult:
    process_reaped: bool
    safe_state_confirmed: bool
    close_response: JsonObject | None
    errors: list[str]

    @property
    def cleanup_confirmed(self) -> bool:
        return self.process_reaped and self.safe_state_confirmed


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
        self.stderr = ""
        self.last_close_result: BridgeCloseResult | None = None
        threading.Thread(target=self._stdout_reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()

    def close(self) -> BridgeCloseResult:
        close_response: JsonObject | None = None
        pending_base_exception: BaseException | None = None
        try:
            close_response = self.request("close", {}, 1)
        except BaseException as error:
            pending_base_exception = error
        process_reaped, errors, reap_base_exception = self._reap_child()
        if pending_base_exception is None:
            pending_base_exception = reap_base_exception
        if close_response is None:
            close_response = self._bridge_error("close_interrupted", f"{self.bridge_label} close request was interrupted.")
        safe_state_confirmed = close_response.get("ok") is True and close_response.get("safe_state_confirmed") is True
        self.closed = process_reaped
        if not safe_state_confirmed:
            errors.append("bridge close did not confirm safe_state_confirmed=true")
        result = BridgeCloseResult(process_reaped=process_reaped, safe_state_confirmed=safe_state_confirmed, close_response=close_response, errors=errors)
        self.last_close_result = result
        if pending_base_exception is not None:
            raise pending_base_exception
        return result

    def status(self) -> JsonObject:
        return {"active": self.child.poll() is None, "backend": self.adapter_name}

    def request(self, method: str, params: JsonObject, timeout_s: float) -> JsonObject:
        if self.closed or self.child.poll() is not None:
            return self._bridge_error("process_exited", f"{self.bridge_label} process is not running.")
        with self.lock:
            request_id = self.next_request_id
            self.next_request_id += 1
            response_queue: queue.Queue[JsonObject] = queue.Queue(maxsize=1)
            self.pending[request_id] = response_queue
            try:
                self.child.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
                self.child.stdin.flush()
            except (OSError, ValueError):
                self.pending.pop(request_id, None)
                return self._bridge_error("process_exited", f"{self.bridge_label} process closed its input.")
        try:
            response = response_queue.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            self.pending.pop(request_id, None)
            return self._bridge_error("timeout", f"{self.bridge_label} request timed out.")
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                return {"ok": False, **error}
            return self._bridge_error("error", str(error))
        result = response.get("result", {})
        if isinstance(result, dict):
            return result
        return self._bridge_error("invalid_response", f"{self.bridge_label} returned a non-object result.")

    def _bridge_error(self, kind: str, summary: str) -> JsonObject:
        result: JsonObject = {"ok": False, "adapter": self.adapter_name, "error_type": f"{self.error_prefix}_{kind}", "summary": summary}
        if self.stderr:
            result["stderr_tail"] = self.stderr[-ERROR_STDERR_TAIL_CHARS:]
        return result

    def _reap_child(self) -> tuple[bool, list[str], BaseException | None]:
        errors: list[str] = []
        pending_base_exception: BaseException | None = None
        if self.child.poll() is None:
            try:
                self.child.terminate()
                self.child.wait(timeout=CHILD_REAP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                errors.append("bridge process did not terminate before kill")
                try:
                    self.child.kill()
                    self.child.wait(timeout=CHILD_REAP_TIMEOUT_S)
                except BaseException as error:
                    errors.append(f"{type(error).__name__}: {error}")
                    if not isinstance(error, Exception):
                        pending_base_exception = error
            except BaseException as error:
                pending_base_exception = error
                errors.append(f"{type(error).__name__}: {error}")
                try:
                    self.child.kill()
                    self.child.wait(timeout=CHILD_REAP_TIMEOUT_S)
                except BaseException as reap_error:
                    errors.append(f"{type(reap_error).__name__}: {reap_error}")
        return self.child.poll() is not None, errors, pending_base_exception

    def _stdout_reader(self) -> None:
        for line in self.child.stdout:
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(response, dict):
                continue
            request_id = response.get("id")
            queue_ = self.pending.pop(request_id, None)
            if queue_ is not None:
                queue_.put(response)

    def _stderr_reader(self) -> None:
        for line in self.child.stderr:
            self.stderr = (self.stderr + line)[-STDERR_TAIL_CHARS:]


def public_backend_result(result: JsonObject, omit: list[str] | None = None) -> JsonObject:
    omit_set = {"session", *(omit or [])}
    return {key: value for key, value in result.items() if key not in omit_set}
