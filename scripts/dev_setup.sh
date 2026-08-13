#!/bin/sh
# dev_setup.sh — get a contributor from `git clone` to a running dev node.
#
#   git clone https://github.com/yeogirlyun/rynmesh.git
#   cd rynmesh && ./scripts/dev_setup.sh
#
# Creates .venv, installs the package editable with dev extras, installs webapp
# deps, and runs the test suite so you know the checkout is healthy before you
# change anything.
set -eu

cd "$(dirname "$0")/.."
REPO="$PWD"

info() { printf '\033[32m▸\033[0m %s\n' "$1"; }
warn() { printf '\033[33m!\033[0m %s\n' "$1"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

printf '\n\033[32mrynmesh — developer setup\033[0m\n\n'

# ---- python ------------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
[ -n "$PY" ] || die "Python 3.10+ required (https://python.org/downloads)"

if [ ! -x .venv/bin/python ]; then
    info "creating .venv"
    "$PY" -m venv .venv
fi
info "installing rynmesh (editable) + dev tools"
./.venv/bin/python -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
./.venv/bin/python -m pip install --quiet -e ".[dev]" || die "pip install failed"

# ---- webapp ------------------------------------------------------------------
if command -v npm >/dev/null 2>&1; then
    if [ ! -d webapp/node_modules ]; then
        info "installing webapp dependencies"
        ( cd webapp && npm install --silent ) || warn "npm install failed — backend dev still works"
    else
        info "webapp dependencies present"
    fi
else
    warn "npm not found — install Node 20+ to work on the webapp (https://nodejs.org)"
fi

# ---- optional: local model ---------------------------------------------------
if command -v ollama >/dev/null 2>&1; then
    info "Ollama found — AI features will work locally"
else
    warn "Ollama not installed; AI features stay off (https://ollama.com/download)"
fi

# ---- prove the checkout is healthy -------------------------------------------
info "running the test suite"
./.venv/bin/python -m pytest tests/ -q || die "tests failed on a fresh checkout — please report this"

cat <<'EOF'

  Ready. Two ways to run it:

    Backend + hot-reloading UI (day-to-day work)
      ./.venv/bin/rynmesh-peer                 # terminal 1 — node API on :8791
      cd webapp && npm run dev                 # terminal 2 — UI on :5173

    Exactly what users get (verify before you ship)
      ./scripts/build_release.zsh --skip-tests # folds the built UI into the package
      ./.venv/bin/rynmesh-peer                 # everything on :8791

  Before opening a PR:
      ./.venv/bin/python -m pytest tests/ -q
      ./.venv/bin/python -m ruff check rynmesh/ tests/
      cd webapp && npx tsc -b

  See CONTRIBUTING.md for the full loop.

EOF
