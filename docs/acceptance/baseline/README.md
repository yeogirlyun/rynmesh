# User requirements suite baseline

Date: 2026-09-04  
Branch: `codex/user-requirements-suite`  
Baseline commit: `85eeb6792ac099f05dcb362e83278de2cebdfad7`  
Worktree: `D:\code\rynmesh-user-requirements`

## Isolation

- The suite is developed in one local Git branch and one dedicated worktree.
- The original `D:\code\rynmesh` worktree remains on `codex/p2p-peer-transit`.
- Existing modified and untracked files in the original worktree were not staged or committed.
- No remote push or deployment is part of this work.

## Environment

- Python: 3.12.10, isolated `.venv`
- Node.js: 22.22.1
- npm: 10.9.4
- Webapp dependencies installed with `npm ci`; audit reported zero vulnerabilities.

## Backend baseline

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check rynmesh tests qa
```

Result:

- `593 passed, 1 skipped, 6 warnings in 88.42s`
- Ruff: `All checks passed!`
- Warnings are existing dependency/FastAPI deprecation warnings; no test failed.

## Webapp baseline

Commands:

```powershell
npm test
npm run lint
npm run build
```

Result:

- 9 test files passed
- 38 tests passed
- TypeScript build check passed
- Production build passed
- Output JS: 430.65 kB raw / 129.09 kB gzip

## Baseline conclusion

The isolated branch starts from a green backend, lint, Webapp test, TypeScript, and production
build baseline. New failures after this point are treated as suite regressions unless separately
documented with evidence.

