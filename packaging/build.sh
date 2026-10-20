#!/usr/bin/env bash
#
# Build the Debian package for Sekashot.
#
#   ./packaging/build.sh              # build dist/sekashot_<version>_all.deb
#   ./packaging/build.sh --no-icons   # skip re-rendering the PNG icons
#   ./packaging/build.sh --keep       # keep the staging tree in build/
#
# Requirements: dpkg-deb (dpkg), python3 (for version + optional icon rendering).
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_ICONS=0
KEEP=0
for arg in "$@"; do
    case "$arg" in
        --no-icons) SKIP_ICONS=1 ;;
        --keep)     KEEP=1 ;;
        -h|--help)  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

command -v dpkg-deb >/dev/null 2>&1 || {
    echo "error: dpkg-deb not found (install the 'dpkg' package)" >&2
    exit 1
}

VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); import sekashot; print(sekashot.__version__)")"
ARCH="all"                      # pure Python, architecture independent
PKG="$ROOT/build/sekashot"
OUT="$ROOT/dist"

echo "==> Sekashot $VERSION ($ARCH)"

rm -rf "$ROOT/build"
mkdir -p "$PKG/DEBIAN" \
         "$PKG/usr/lib/sekashot" \
         "$PKG/usr/bin" \
         "$PKG/usr/share/applications" \
         "$PKG/usr/share/icons" \
         "$PKG/usr/share/doc/sekashot" \
         "$PKG/etc/xdg/autostart"

# --- 1. the Python package -------------------------------------------------
cp -a "$ROOT/sekashot" "$PKG/usr/lib/sekashot/"
find "$PKG/usr/lib/sekashot" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$PKG/usr/lib/sekashot" -type f -name '*.pyc' -delete
# keep the tree honest: no compiled caches, only source
echo "    python package : $(find "$PKG/usr/lib/sekashot" -name '*.py' | wc -l) modules"

# --- 2. launcher -----------------------------------------------------------
install -m 0755 "$ROOT/packaging/sekashot" "$PKG/usr/bin/sekashot"

# --- 3. desktop integration ------------------------------------------------
install -m 0644 "$ROOT/packaging/sekashot.desktop" \
        "$PKG/usr/share/applications/sekashot.desktop"
install -m 0644 "$ROOT/packaging/sekashot-autostart.desktop" \
        "$PKG/etc/xdg/autostart/sekashot.desktop"

# --- 4. icons --------------------------------------------------------------
if [ "$SKIP_ICONS" -eq 0 ] && python3 -c 'import PyQt6' >/dev/null 2>&1; then
    echo "    rendering icons with PyQt6"
    QT_QPA_PLATFORM=offscreen python3 "$ROOT/tools/make_icons.py" "$ROOT/packaging/icons" >/dev/null
fi
cp -a "$ROOT/packaging/icons/hicolor/." "$PKG/usr/share/icons/hicolor/"
echo "    icon sizes     : $(find "$PKG/usr/share/icons/hicolor" -name '*.png' | wc -l)"

# --- 5. documentation ------------------------------------------------------
install -m 0644 "$ROOT/README.md" "$PKG/usr/share/doc/sekashot/README.md"
if [ -f "$ROOT/LICENSE" ]; then
    install -m 0644 "$ROOT/LICENSE" "$PKG/usr/share/doc/sekashot/copyright"
fi
if [ -f "$ROOT/CHANGELOG.md" ]; then
    gzip -9n -c "$ROOT/CHANGELOG.md" > "$PKG/usr/share/doc/sekashot/changelog.gz"
fi

# --- 6. maintainer scripts -------------------------------------------------
install -m 0755 "$ROOT/packaging/postinst" "$PKG/DEBIAN/postinst"
install -m 0755 "$ROOT/packaging/postrm" "$PKG/DEBIAN/postrm"

# --- 7. control ------------------------------------------------------------
# keep the staged tree to sane, non-writable-by-others permissions
chmod -R go-w "$PKG"
SIZE="$(du -sk --exclude=DEBIAN "$PKG" | cut -f1)"
sed -e "s|@VERSION@|$VERSION|g" \
    -e "s|@ARCH@|$ARCH|g" \
    -e "s|@SIZE@|$SIZE|g" \
    "$ROOT/packaging/control.in" > "$PKG/DEBIAN/control"

# --- 8. build the archive --------------------------------------------------
mkdir -p "$OUT"
DEB="$OUT/sekashot_${VERSION}_${ARCH}.deb"
rm -f "$DEB"

if dpkg-deb --root-owner-group --build "$PKG" "$DEB" 2>/dev/null; then
    :
elif command -v fakeroot >/dev/null 2>&1; then
    echo "    (falling back to fakeroot)"
    fakeroot dpkg-deb --build "$PKG" "$DEB"
else
    dpkg-deb --build "$PKG" "$DEB"
fi

cp -f "$DEB" "$OUT/sekashot.deb"
rm -f "$ROOT/sekashot.deb"
cp -f "$DEB" "$ROOT/sekashot.deb"

# --- 9. report -------------------------------------------------------------
echo
dpkg-deb --info "$DEB"
echo
echo "==> packages"
ls -la "$DEB" "$OUT/sekashot.deb" "$ROOT/sekashot.deb"
echo
echo "==> contents"
dpkg-deb --contents "$DEB" | sed 's/^/   /'
echo
echo "==> install with:  sudo apt install ./$(basename "$DEB")"
echo "                   # or:  sudo dpkg -i $(basename "$DEB") && sudo apt -f install"

if [ "$KEEP" -eq 0 ]; then
    rm -rf "$ROOT/build"
else
    echo
    echo "==> staging tree kept at $PKG"
fi
