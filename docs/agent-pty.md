# Ryn Agent terminal sessions

`ryn_shell_execute` keeps its existing PIPE mode by default. Explicit `tty: true`
starts a real Windows ConPTY. This is an execution transport option, not a new
permission grant or a sandbox. The existing execution classification, preview,
operation ledger, task ownership and permission watcher remain in force.

## Tool usage

Start an interactive command and yield its session:

```json
{"command":"$name=Read-Host 'Name'; Write-Output ('Hello '+$name)","shell":"powershell","tty":true,"rows":24,"cols":80,"yield_seconds":1,"timeout_seconds":120}
```

Use the returned `process_id` in subsequent calls:

| Tool | Arguments in addition to `process_id` | Meaning |
| --- | --- | --- |
| `ryn_process_read` | `offset: 0, wait_seconds: 10` | Read output/status; wait for new output or exit |
| `ryn_process_write` | `text: "Alice\r"` | Type a line and press Enter |
| `ryn_process_write` | `text: "", rows: 40, cols: 120` | Resize, with both dimensions in 1–500 |
| `ryn_process_write` | `text: "", control: "interrupt"` | Send Ctrl+C; the program can catch it and continue |
| `ryn_process_write` | `text: "", control: "eof"` | Send Windows console EOF keys, Ctrl+Z then Enter |
| `ryn_process_write` | `text: "", control: "close_terminal"` | Close the terminal and clean its owned process tree |
| `ryn_process_stop` | none | Force termination of the owned process tree |

Control and resize operations require empty `text`; they cannot be combined with
each other. Raw control characters can also be sent in `text`. `eof: true` retains
its old PIPE meaning: close stdin after writing. It is rejected for PTY sessions
because terminals do not offer an independent stdin half-close. Terminal EOF is
application dependent, does not close the transport, and may be ignored in raw
mode. No terminal action can target another task's session.

Read `next_offset` pages first; when exhausted, use `output_length` as the next
offset. Offsets count characters. Existing limits remain: 512 KiB captured output,
6,000-character pages, at most eight running processes and 128 retained sessions.
The transport keeps draining output after the capture cap, so verbose commands do
not block on an unread terminal. Output is a raw UTF-8 VT stream, including input
echo and screen updates; it is not a rendered screen or necessarily a lossless
record of every application stdout write. ConPTY can coalesce rapid screen writes.

PTY responses additionally identify `terminal_backend: "conpty"`, dimensions,
`terminal_closed`, `output_complete`, and `cleanup_complete`. Exit codes describe
the OS command/shell process. Inspect `stop_reason` and any `terminal_error` as well:
a terminated or failed terminal is not task success, even if Windows reports exit
code zero. A transport failure has `stop_reason: "terminal_transport_error"`.

PowerShell launches with `-NoLogo -NoProfile -Command`, omitting `-NonInteractive`
only for PTY mode. There is no implicit `-NoExit`: the shell exits when the command
finishes. PIPE retains `-NonInteractive`. CMD retains `/d /s /c`. A PowerShell
pipeline interrupted by Ctrl+C can return 1 even when a child catches the signal
and later exits 0; Ryn does not overwrite the shell's status with the child's.

## Ownership and dependency

Windows installs `pywinpty>=3.1.2,<4`. The implementation uses its native
[`PTY` API](https://github.com/andfoy/pywinpty/blob/main/src/lib.rs), explicitly
selecting `Backend.ConPTY`; it never silently falls back to WinPTY or PIPE.
Missing dependencies and unsupported platforms return explicit errors before
dispatch. A native startup failure cleans the host and is reported as an error,
never as a successful terminal session or a safely replayable command.

pywinpty cannot launch suspended. A small multiprocessing host waits at a gate
until the existing kill-on-close Windows Job Object owns it, then starts ConPTY
and the requested command. All descendants inherit that job. This avoids the race
of assigning an already-running user command to a job. The host receives the
existing sanitized child environment. It serializes terminal I/O and reports
acknowledgements, output and status through a private local pipe.

Normal exit, timeout, task cancellation, permission withdrawal, recovery, explicit
stop, terminal close and backend death all clean the job. Native process handles,
IPC, helper threads, queues and synchronization handles are released; retained
session history contains output/status, not a live terminal. Unacknowledged input
closes the session and reports an uncertain result, rather than leaving input to
be delivered later or replaying it. The terminal startup handshake has a separate
15-second bound; the existing command watchdog starts after successful startup.
Cancelling just a read call retains its existing meaning: interrupt the wait;
task cancellation/stop controls the command lifetime.

No changes are needed in `loop.py`, `runtime.py`, prompts, budgets or approval
policy. Only local tool argument validation is extended. Calls containing the new
terminal options conservatively remain write-classified, including otherwise
read-only shell commands; they do not acquire read-only execution authorization.

## Verification on 2026-10-30

Actual test host: Windows 11 x64, build 28120, CPython 3.12.10, pywinpty 3.1.2.
Tests use local programs, synthetic text and temporary workspaces, without real
credentials or production servers.

Final focused regression result: **103 passed, 1 skipped** in 192.57 seconds.
Ruff on every changed Python file and `git diff --check` passed. Existing
FastAPI/Starlette and browser WebSocket deprecation warnings remain.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_agent_local_processes.py tests/test_agent_local_tools.py tests/test_agent_local_execution_reliability.py tests/test_agent_process_recovery.py tests/test_agent_local_pty.py -q
```

`test_agent_local_pty.py` exercises real ConPTY: stdin/stdout terminal detection,
Chinese/emoji prompt input, resize observed by the child, incremental reads,
Ctrl+C caught by a still-interactive program (directly and through PowerShell),
EOF followed by more input, `Read-Host`, nonzero status, final Unicode output,
bounded capture, all cleanup paths, abrupt terminal-host and owner-process death,
and repeated-session OS handle counts. It also checks task isolation, dependency
errors, invalid controls and read-authorization rejection. Mocks are used for
missing-platform/dependency conditions, not terminal acceptance.

The same local `tests/fixtures/pty_probe.py` commands were run through Codex
`exec_command(tty=true)` and `write_stdin`:

```powershell
.venv\Scripts\python.exe -u tests/fixtures/pty_probe.py interaction
.venv\Scripts\python.exe -u tests/fixtures/pty_probe.py eof
.venv\Scripts\python.exe -u tests/fixtures/pty_probe.py orphan
```

| Behavior | Observed comparison |
| --- | --- |
| Real terminal | Both report `stdin=true, stdout=true` |
| Prompt then input | Both accept `测试🙂` followed by carriage return |
| Continued output | Both deliver tick output while retaining a live session |
| Ctrl+C | Both reach `interrupted`, then accept input at `Continue:` |
| EOF | Ctrl+Z + Enter reaches `eof`; both still accept the next input |
| Parent exits with descendant | Both finish; the reported descendant PID is no longer alive |
| PowerShell status after Ctrl+C | Codex's shell returned 1 despite the child's normal finish; Ryn preserves the actual shell status too |
| Resize | Ryn verified 120×40 in the child; the exposed Codex tools have no resize argument, so no claim of direct comparison |

Intentional interface differences: Ryn uses task-scoped `process_id` strings and
offset-based reads, not numeric `session_id` plus automatically consumed output.
`ryn_process_write` acknowledges input; a separate read obtains output. An empty
write is not a polling request. Yield/read waits use the existing Ryn seconds-based
limits and the command has a fixed lifetime timeout. This is behavioral alignment
for interactive execution, not complete Codex API parity or a terminal emulator.

This change supports PTY on Windows only. Linux/macOS retain PIPE and explicitly
reject `tty=true`; they were not tested here. The packaged/frozen desktop build
was not rebuilt or tested. Its existing `multiprocessing.freeze_support()` entry
is reused, so no shared entrypoint modification is included. Alternate Windows
versions, ARM, and full-screen TUI rendering remain unverified.

## Integration review correction

The initial implementation retained the launch dispatch's `cancelled()` callback
in the process watchdog. In the actual Agent runtime that callback becomes true
when a normal tool step hands control back to the scheduler (`running` to
`queued`). This killed background and yielded sessions, including PIPE sessions,
even though the task still authorized them. The original direct-tool tests used
an always-false callback and did not cover this handoff.

The lifetime watchdog now uses the durable task/permission callback, as PIPE did
before the PTY change. Dispatch cancellation still guards startup and foreground
waiting; task cancellation and permission withdrawal still terminate the session.
`test_agent_pty_runtime_handoff.py` reproduces the original failure through the
actual Agent runtime with a deterministic model, then verifies continued input
and explicit task cancellation for both PIPE/ConPTY and background/yield starts.

Independent review after this correction: **107 passed, 1 skipped** in 219.41
seconds on Windows, running the five suites above plus
`tests/test_agent_pty_runtime_handoff.py`. Ruff and `git diff --check` passed.
An explicit isolated pytest base directory was used because this host's shared
pytest temporary-directory cleanup encounters an unrelated Windows access error.
