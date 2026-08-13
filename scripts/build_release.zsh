#!/bin/zsh
# build_release.zsh — build a self-contained Rynmesh wheel.
#
#   1. run the test suite (a release never ships red)
#   2. build the webapp and fold it into the Python package (rynmesh/webui)
#   3. build the wheel
#   4. write installer metadata and checksums into dist/
#
# Usage:  ./scripts/build_release.zsh [--skip-tests]
set -euo pipefail

REPO="${0:A:h:h}"
SKIP_TESTS=0
[[ "${1:-}" == "--skip-tests" ]] && SKIP_TESTS=1

cd "$REPO"
VERSION="$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
say() { print -P "%F{green}▸%f $*"; }
die() { print -P "%F{red}✗%f $*" >&2; exit 1; }

say "rynmesh $VERSION — building release"

# ---- 1. tests ----------------------------------------------------------------
if (( SKIP_TESTS )); then
  print -P "%F{yellow}!%f skipping tests (--skip-tests)"
else
  say "running test suite"
  python3 -m pytest tests/ -q || die "tests failed — fix before releasing"
fi

# ---- 2. webapp -> rynmesh/webui ----------------------------------------------
say "building webapp"
( cd webapp && npm run build >/dev/null ) || die "webapp build failed"
[[ -f webapp/dist/index.html ]] || die "webapp/dist/index.html missing after build"

say "folding webapp into the Python package (rynmesh/webui)"
rm -rf rynmesh/webui
cp -R webapp/dist rynmesh/webui
# The packaged app is same-origin: the daemon serves both UI and API, so the
# webapp's relative "/api/local" calls resolve without a dev proxy.

# ---- 3. wheel ----------------------------------------------------------------
say "building wheel"
rm -rf dist build
python3 -m pip install --quiet --upgrade build >/dev/null 2>&1 || true
python3 -m build --wheel >/dev/null || die "wheel build failed"
WHEEL="$(ls dist/rynmesh-${VERSION}-*.whl 2>/dev/null | head -1)"
[[ -n "$WHEEL" ]] || die "no wheel produced for $VERSION"

# the wheel must actually contain the UI, or installs serve a blank page
python3 - "$WHEEL" <<'PY' || exit 1
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
if not any(n.startswith("rynmesh/webui/") and n.endswith("index.html") for n in names):
    sys.exit("wheel is missing rynmesh/webui/index.html — check pyproject package-data")
print(f"  wheel contains {sum(n.startswith('rynmesh/webui/') for n in names)} web UI files")
PY

# ---- 4. release metadata ------------------------------------------------------
say "writing GitHub release metadata"
cp "$REPO/scripts/install.sh" dist/install.sh
( cd dist && shasum -a 256 "$(basename "$WHEEL")" > SHA256SUMS )
SHA="$(awk '{print $1}' dist/SHA256SUMS)"
cat > dist/latest.json <<JSON
{
  "version": "$VERSION",
  "wheel": "https://github.com/yeogirlyun/rynmesh/releases/download/v$VERSION/$(basename "$WHEEL")",
  "sha256": "$SHA",
  "install": "https://github.com/yeogirlyun/rynmesh/releases/latest/download/install.sh"
}
JSON

print
say "built rynmesh $VERSION"
print "  wheel   : $WHEEL"
print "  sha256  : $SHA"
print "  metadata: dist/latest.json"
print
print "Tag v$VERSION on GitHub to publish these artifacts through the release workflow."
