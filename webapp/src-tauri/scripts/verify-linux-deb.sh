#!/usr/bin/env bash
# Inspect the final Debian package without installing it.
set -euo pipefail

DEB="${1:-}"
EXPECTED_VERSION="${2:-}"
[ -f "$DEB" ] || { echo "Debian package not found: $DEB" >&2; exit 1; }

ARCH="$(dpkg-deb --field "$DEB" Architecture)"
[ "$ARCH" = "amd64" ] || { echo "expected amd64 package, got $ARCH" >&2; exit 1; }
DEPENDS="$(dpkg-deb --field "$DEB" Depends)"
printf '%s\n' "$DEPENDS" | grep -q 'libwebkit2gtk-4.1-0' || {
  echo "package does not declare the WebKitGTK 4.1 runtime" >&2
  exit 1
}
printf '%s\n' "$DEPENDS" | grep -q 'libgtk-3-0' || {
  echo "package does not declare the GTK 3 runtime" >&2
  exit 1
}
printf '%s\n' "$DEPENDS" | grep -Eq 'lib(ayatana-)?appindicator3-1' || {
  echo "package does not declare an app-indicator runtime" >&2
  exit 1
}
if [ -n "$EXPECTED_VERSION" ]; then
  VERSION="$(dpkg-deb --field "$DEB" Version)"
  [ "$VERSION" = "$EXPECTED_VERSION" ] || {
    echo "expected package version $EXPECTED_VERSION, got $VERSION" >&2
    exit 1
  }
fi

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
dpkg-deb --extract "$DEB" "$ROOT"

mapfile -t SIDECARS < <(find "$ROOT" -type f -name rynmesh-peer -print)
[ "${#SIDECARS[@]}" -eq 1 ] || {
  echo "expected exactly one packaged rynmesh-peer, found ${#SIDECARS[@]}" >&2
  exit 1
}
SIDECAR="${SIDECARS[0]}"
[ -x "$SIDECAR" ] || { echo "packaged sidecar is not executable: $SIDECAR" >&2; exit 1; }
file "$SIDECAR" | grep -Eq 'ELF 64-bit.*x86-64' || {
  echo "packaged sidecar is not an x86-64 ELF executable" >&2
  file "$SIDECAR" >&2
  exit 1
}
if ldd "$SIDECAR" 2>/dev/null | grep -Eiq '(^|[/ ])(python|node)([0-9.]|/| )'; then
  echo "packaged sidecar unexpectedly depends on a system Python or Node runtime" >&2
  ldd "$SIDECAR" >&2
  exit 1
fi

mapfile -t DESKTOP_FILES < <(find "$ROOT/usr/share/applications" -type f -name '*.desktop' -print 2>/dev/null)
[ "${#DESKTOP_FILES[@]}" -ge 1 ] || { echo "desktop entry is missing" >&2; exit 1; }
find "$ROOT/usr/share/icons" -type f \( -name '*.png' -o -name '*.svg' \) -print -quit 2>/dev/null | grep -q . || {
  echo "application icon is missing" >&2
  exit 1
}

EXEC_NAME="$(sed -n 's/^Exec=\([^ %]*\).*/\1/p' "${DESKTOP_FILES[0]}" | head -n 1)"
[ -n "$EXEC_NAME" ] || { echo "desktop entry has no Exec command" >&2; exit 1; }
case "$EXEC_NAME" in
  /*) DESKTOP_BIN="$ROOT$EXEC_NAME" ;;
  *) DESKTOP_BIN="$(find "$ROOT/usr/bin" -maxdepth 1 -type f -name "$EXEC_NAME" -print -quit)" ;;
esac
[ -n "$DESKTOP_BIN" ] && [ -x "$DESKTOP_BIN" ] || {
  echo "desktop executable from Exec=$EXEC_NAME is missing" >&2
  exit 1
}
file "$DESKTOP_BIN" | grep -Eq 'ELF 64-bit.*x86-64' || {
  echo "desktop executable is not an x86-64 ELF executable" >&2
  file "$DESKTOP_BIN" >&2
  exit 1
}

dpkg-deb --info "$DEB"
dpkg-deb --contents "$DEB"
echo "LINUX_DEB_VERIFIED architecture=$ARCH sidecar=${SIDECAR#"$ROOT"} desktop=${DESKTOP_BIN#"$ROOT"} depends=$DEPENDS"
