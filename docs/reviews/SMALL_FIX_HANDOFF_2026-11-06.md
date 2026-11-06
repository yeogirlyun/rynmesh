# Reviewed repairs on current develop — 2026-11-06

Owner scope: repair the smaller issues and merge ready integration work; leave
larger infrastructure and employee workflows to Ziling. This updates PR #118
against public develop `7613504`, preserving the new storage, login, automatic
mode, discovery and page-recovery work.

## Repairs

- Retain the previously reviewed post-commit settings projection, Darwin resource
  descriptor handling, missing math import, shared Rust discovery, Undici 7.30.1,
  and frontend-before-sidecar build order.
- On POSIX, allow a closed OAuth callback address to be reused after TIME_WAIT;
  Windows retains its exclusive bind. Real loopback tests verify reuse of a DCR
  registration and re-registration when its saved port is actually occupied.
- Use the native invocation boundary in the desktop URL fixture, and check the
  selected workspace mode's actual label in the composer test.
- Keep encrypted device-cache records in write order when timestamps tie or the
  clock moves backwards. Derive the next order from stored records within the
  existing read/write transaction, so it also survives reloads and multiple tabs.
  Add a deterministic same-millisecond/clock-correction regression.

## Local verification

All execution used actual local worktrees, synthetic data and owned fixtures.

- 155 targeted Python tests passed across settings, long-task continuation,
  encrypted task layout, storage relief, automatic permissions, OAuth/reimport,
  managed MCP, MCP conversation restart/context, desktop scripts and JS resources.
- Full web suite: 1404 tests passed across 175 files with four workers. The first
  unrestricted run exposed load-sensitive Sending/Sidebar tests (all 25 passed
  on their isolated rerun). A bounded-concurrency full run exposed the timestamp
  tie defect; after its deterministic fix the complete suite passed.
- Changed Python files pass Ruff; shell syntax and diff whitespace checks pass.
- Production web build/typecheck pass. No installed Windows/native application,
  real employee account, live message or production DB was exercised here.

## Merge boundaries and next work

This is a scoped develop repair batch, not promotion of the full workspace to
main. Two existing automatic-compaction cases still hit agent_tool_schema_limit
before compaction; whole-tree fixture lint and the third-party browser runtime's
public redistribution boundary remain integration gates. No tests were skipped
or relaxed to hide those failures. Main protection and reviewer requirements
remain in force.

Ziling owns: generic declared organization services replacing Gshopper constants
in the public host; ordinary-employee service scopes; searchable linked work
history; indexed task/archive storage and menu/compaction compatibility; a second
employee's clean-client report -> file -> reviewed sharing -> confirmed outcome
-> restart -> follow-up journey; paired-source release acceptance and main CI.
The company repository contains the detailed Chinese assignment at
docs/assignments/NEXT_BATCH_2026-11-06_ZH.md.
