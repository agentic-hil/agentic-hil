from __future__ import annotations

import json
import queue
import subprocess
import threading
from contextlib import suppress

from hardci.types import JsonObject

CHILD_REAP_TIMEOUT_S = 5.0


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
        threading.Thread(target=self._stdout_reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()

    def close(self) -> None:
        try:
            self.request("close", {}, 1)
        finally:
            self.closed = True
            self.child.terminate()
            try:
                self.child.wait(timeout=CHILD_REAP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.child.kill()
                with suppress(subprocess.TimeoutExpired):
                    self.child.wait(timeout=CHILD_REAP_TIMEOUT_S)

    def status(self) -> JsonObject:
        return {"active": not self.closed and self.child.poll() is None, "backend": self.adapter_name}

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
        return {"ok": False, "adapter": self.adapter_name, "error_type": f"{self.error_prefix}_{kind}", "summary": summary}

    def _stdout_reader(self) -> None:
        for line in self.child.stdout:
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            request_id = response.get("id")
            queue_ = self.pending.pop(request_id, None)
            if queue_ is not None:
                queue_.put(response)

    def _stderr_reader(self) -> None:
        for line in self.child.stderr:
            self.stderr += line


def public_backend_result(result: JsonObject, omit: list[str] | None = None) -> JsonObject:
    omit_set = {"session", *(omit or [])}
    return {key: value for key, value in result.items() if key not in omit_set}
