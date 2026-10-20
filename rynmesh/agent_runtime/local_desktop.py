"""Windows desktop adapter delegating exclusively to `NativeComputerFacade`.

Direct Win32/PowerShell/ImageGrab fallback paths are removed; `Desktop` requires
an owner-bound `NativeComputerFacade` and fails closed with
`agent_computer_host_required` when constructed without one.
"""
from __future__ import annotations

from typing import Any

from .native_computer import NativeComputerFacade
from .store import AgentToolRejected


class Desktop:
    """Thin compatibility wrapper around `NativeComputerFacade` for `LocalTools`."""

    def __init__(self, native_computer: NativeComputerFacade | None = None) -> None:
        if not isinstance(native_computer, NativeComputerFacade):
            raise AgentToolRejected("agent_computer_host_required")
        self.native_computer: NativeComputerFacade = native_computer
        self.observed: dict[Any, Any] = {}
        self.user = None

    def windows(self) -> list[dict[str, Any]]:
        raise AgentToolRejected("agent_computer_host_required")

    @staticmethod
    def accessibility(request: Any) -> dict[str, Any]:
        del request
        raise AgentToolRejected("agent_computer_host_required")

    def window(self, ident: Any, task_id: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        del ident, task_id
        raise AgentToolRejected("agent_computer_host_required")

    @staticmethod
    def key_codes(keys: Any) -> list[int]:
        del keys
        raise AgentToolRejected("agent_computer_host_required")

    def keyboard(self, code: Any, flags: int = 0) -> None:
        del code, flags
        raise AgentToolRejected("agent_computer_host_required")

    def preview(
        self,
        args: dict[str, Any],
        task_id: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.native_computer.preview_local_action(args, task_id, context=context)

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        task_id: str,
        resolve: Any,
        cancelled: Any,
        *,
        operation_id: str | None = None,
        context: dict[str, Any] | None = None,
        approved_preview: dict[str, Any] | None = None,
        on_ready_host_gate: Any = None,
    ) -> dict[str, Any]:
        del resolve
        if name == "ryn_computer_read":
            return self.native_computer.execute_local_read(
                args,
                task_id,
                operation_id=operation_id,
                context=context,
                cancelled=cancelled,
            )
        if name == "ryn_computer_action":
            return self.native_computer.execute_local_action(
                args,
                task_id,
                operation_id=str(
                    operation_id
                    or (context or {}).get("operation_id")
                    or "op_local_action"
                ),
                approved_preview=approved_preview,
                context=context,
                cancelled=cancelled,
                on_ready_host_gate=on_ready_host_gate,
            )
        raise AgentToolRejected("agent_unknown_tool")
