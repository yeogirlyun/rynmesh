# Direct use of supplied workspace file tools

Date: 2026-11-15 (America/New_York). Product change `ca4364a5` (stacked on compact-export product `a61beefe`).

A retained personal-task trace spent an extra model turn looking for an unavailable patch tool, then listed files before using the supplied writer. The host's file-output policy prescribed `ryn_apply_patch` for all text creation even when the active menu exposed only `ryn_file_write`. The policy now directs models to use supplied workspace tools directly, use the supplied writer for new text, read existing files before edits, and discover capabilities only when absent from the current menu.

This is a guidance correction, not automatic dispatch or permission expansion. Workspace scope, desktop gates, file-change detection, approvals, readback, pinned/custom base instructions and app/plugin discovery rules remain intact. There is no extra model request to choose a tool and no new planner/service.

## Validation

- Focused request-construction/tool-menu/local-file/strict-schema tests: **79 passed, 4 existing optional/platform skips**. Ruff and diff checks passed.
- Regression exercises the actual instruction layers with a pinned owner base and read-only task, confirming the file policy offers the supplied writer while preserving workspace, desktop and permission authority.
- Same personal task and synthetic notes, existing local CLI configuration and owner budgets: read notes, save a three-step checklist, read back. No company connection or purchase. Independent artifact review confirmed the 300-unit budget, reuse of two existing containers, dependencies, acceptance methods and explicit unknowns.

| Source | Model calls | Tools | Cumulative reported tokens | Seconds |
|---|---:|---:|---:|---:|
| develop `fd30de9e` | 4 | 3 | 68,770 | 42.98 |
| direct-tool guidance `ca4364a5` | 4 | 3 | 68,108 | 40.69 |

Both fresh runs used the efficient read/write/read path without discovery. The older retained run needed six model calls, but this fresh baseline already needed four, so **no additional call-count saving is established by this pair**. Time/token differences can reflect output variation and do not establish a general speedup. The verified improvement is removing contradictory tool-selection guidance; the separate compact-export change has its own measured payload/call reduction. Original records and generated files remain private outside Git. No deployment or installed employee workflow acceptance.
