"""Read-only overlap after completed model items; authority stays in the host.

Callbacks: normalize(raw)->call, admit(index,call)->prepared, run(prepared)->result.
Admission must durably record intent and recheck current authority before it
returns. ``None`` declines early admission. The host owns all result receipts.
"""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor


class StreamToolMismatch(RuntimeError):
    """Completed stream items differ from the authoritative final response."""

    def __init__(self):
        super().__init__("stream tool item changed in final response")
        self.stream_results = []


def _identity(raw):
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)


class StreamToolCoordinator:
    def __init__(self, normalize, admit, run, *, parallel_reads, internal_names=(),
                 cancelled=lambda: False, max_workers=8):
        if type(max_workers) is not int or not 1 <= max_workers <= 8:
            raise ValueError("invalid stream read worker limit")
        self.normalize, self.admit, self.run = normalize, admit, run
        self.parallel_reads, self.internal_names = frozenset(parallel_reads), frozenset(internal_names)
        self.cancelled, self.max_workers = cancelled, max_workers
        self.items, self.started = {}, {}
        self.next_index = 0
        self.blocked = self.closed = False
        self.executor = None
        self.results = None

    def on_event(self, event):
        if not isinstance(event, dict) or event.get("type") != "tool_call_complete":
            return
        if self.closed:
            return
        try:
            if self.cancelled():
                self.close()
                return
            index = event.get("index")
            if type(index) is not int or not 0 <= index < 8:
                raise ValueError("invalid stream tool index")
            raw = copy.deepcopy(event.get("call"))
            identity = _identity(raw)
            if index in self.items:
                if self.items[index]["identity"] != identity:
                    raise StreamToolMismatch()
                return
            self.items[index] = {"raw_call": raw, "identity": identity}
            self._advance()
        except BaseException as exc:
            records = self.close()
            try:
                exc.stream_results = records
            except AttributeError:
                pass
            raise

    def _advance(self):
        while not self.blocked and self.next_index in self.items:
            if self.cancelled():
                self.close()
                return
            index = self.next_index
            item = self.items[index]
            call = self.normalize(copy.deepcopy(item["raw_call"]))
            if not isinstance(call, dict):
                raise ValueError("invalid normalized stream tool")
            name = call.get("name")
            if (name not in self.parallel_reads or name in self.internal_names
                    or call.get("internal") or call.get("effect") != "read"
                    or call.get("error_result") is not None):
                self.blocked = True
                return
            prepared = self.admit(index, copy.deepcopy(call))
            if prepared is None:
                self.blocked = True
                return
            record = {"index": index, "raw_call": copy.deepcopy(item["raw_call"]),
                      "call": call, "admission": prepared}
            self.started[index] = {**record, "future": None}
            try:
                # Preserve the admitted intent even if worker creation fails.
                if self.executor is None:
                    self.executor = ThreadPoolExecutor(max_workers=self.max_workers,
                                                       thread_name_prefix="ryn-stream-read")
                self.started[index]["future"] = self.executor.submit(self.run, prepared)
            except BaseException as exc:
                self.started[index]["submission_error"] = exc
                raise
            self.next_index += 1

    def reconcile(self, final_calls):
        try:
            matches = isinstance(final_calls, list) and all(
                index < len(final_calls) and _identity(final_calls[index]) == item["identity"]
                for index, item in self.items.items())
        except (TypeError, ValueError, RecursionError):
            matches = False
        if not matches:
            error = StreamToolMismatch()
            error.stream_results = self.close()
            raise error

    def drain(self, final_calls=None):
        if final_calls is not None:
            self.reconcile(final_calls)
        self.closed = True
        if self.results is None:
            if self.executor is not None:
                # Admitted reads finish under the host cancellation token. Do
                # not abandon futures or discard receipts on cancel/error.
                self.executor.shutdown(wait=True)
            self.results = []
            for index in sorted(self.started):
                entry = self.started[index]
                record = {key: value for key, value in entry.items() if key not in {"future", "submission_error"}}
                try:
                    if entry["future"] is None:
                        raise entry["submission_error"]
                    record.update(result=entry["future"].result(), error=None)
                except BaseException as exc:
                    record.update(result=None, error=exc)
                self.results.append(record)
        return list(self.results)

    def close(self):
        return self.drain()

    def cancel(self):
        return self.close()
