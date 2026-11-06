#!/usr/bin/env bash
# =============================================================================
# APMLogViewer —— Ubuntu/Debian .deb 构建脚本（在 Ubuntu 上运行）
#
#   packaging/build_deb.sh              # 先 PyInstaller 再打 deb
#   packaging/build_deb.sh --skip-build # 复用已有 dist-linux/APMLogViewer
#
# 产物：release/APMLogViewer-<版本>-amd64.deb
# 安装：sudo apt install ./release/APMLogViewer-<版本>-amd64.deb
# 卸载：sudo apt remove apmlogviewer
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
NAME=apmlogviewer
ARCH=amd64
PY="${PY:-$ROOT/venv/bin/python}"
[ -x "$PY" ] || PY=python3

VER="$("$PY" - <<'PYEOF'
import pathlib, re
src = pathlib.Path("apmlog/__init__.py").read_text(encoding="utf-8")
print(re.search(r'__version__\s*=\s*"([^"]+)"', src).group(1))
PYEOF
)"
echo "[deb] 版本 $VER"

if [ "${1:-}" != "--skip-build" ]; then
    echo "[deb] PyInstaller 构建（onedir）…"
    "$PY" -m PyInstaller --noconfirm --clean \
        --distpath "$ROOT/dist-linux" \
        --workpath "$ROOT/build/pyi-linux" \
        "$ROOT/packaging/apmlogviewer-linux.spec"
fi

APP="$ROOT/dist-linux/APMLogViewer"
[ -x "$APP/APMLogViewer" ] || { echo "[deb] 缺少构建产物: $APP" >&2; exit 1; }

mkdir -p "$ROOT/release"
STAGE="$(mktemp -d /tmp/apmlog-deb.XXXXXX)"
trap 'chmod -R u+w "$STAGE" 2>/dev/null || true' EXIT

mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/opt/$NAME" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/256x256/apps" \
         "$STAGE/usr/share/doc/$NAME"

cp -a "$APP/." "$STAGE/opt/$NAME/"
ln -s "/opt/$NAME/APMLogViewer" "$STAGE/usr/bin/$NAME"
cp "$ROOT/packaging/apmlogviewer.png" "$STAGE/usr/share/icons/hicolor/256x256/apps/$NAME.png"

cat > "$STAGE/usr/share/applications/$NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=APMLogViewer
Name[zh_CN]=APMLogViewer 日志分析
GenericName=ArduCopter Log Analyzer
GenericName[zh_CN]=ArduCopter 飞行日志分析
Comment=Analyze ArduCopter DataFlash (.BIN) flight logs
Comment[zh_CN]=分析 ArduCopter DataFlash（.BIN）飞行日志
Exec=/opt/$NAME/APMLogViewer %f
TryExec=/opt/$NAME/APMLogViewer
Icon=$NAME
Terminal=false
Categories=Utility;Science;Engineering;
Keywords=ardupilot;arducopter;log;bin;uav;
EOF

cat > "$STAGE/usr/share/doc/$NAME/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: APMLogViewer
Source: https://github.com/KonuriMaki2004/APMLogViewer

Files: *
Copyright: 2026 APMLogViewer contributors
License: MIT
 见随包 LICENSE（上游仓库根目录）。
EOF

SIZE_KB="$(du -sk "$STAGE/opt/$NAME" | cut -f1)"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $NAME
Version: $VER
Section: utils
Priority: optional
Architecture: $ARCH
Installed-Size: $SIZE_KB
Maintainer: APMLogViewer <noreply@example.com>
Homepage: https://github.com/KonuriMaki2004/APMLogViewer
Depends: libc6 (>= 2.35), libgl1, libegl1, libxkbcommon0, libxkbcommon-x11-0, libdbus-1-3, libfontconfig1, libfreetype6, libx11-6, libxcb1, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, libxcb-render-util0, libxcb-shape0, libxcb-xinerama0, libxcb-xkb1, libxi6, libglib2.0-0t64 | libglib2.0-0
Recommends: fonts-noto-cjk
Description: ArduCopter DataFlash (.BIN) flight log analyzer
 APMLogViewer opens ArduCopter DataFlash logs and shows GPS/EKF/VIO tracks on a
 real map, multi-lane charts with 12 presets (vibration, attitude, battery,
 position, compass, IMU, PID, power, EKF, RC, GNSS), alerts, events and
 parameters. It also compares position tracks (per-pair and overall deviation
 percentages) and can download logs from a flight controller over MAVLink.
 .
 Bundled with its own Python + Qt runtime; no system Python packages required.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
fi
exit 0
EOF

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q || true
    fi
fi
exit 0
EOF

chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"
chmod 0644 "$STAGE/DEBIAN/control" "$STAGE/usr/share/applications/$NAME.desktop"

DEB="$ROOT/release/APMLogViewer-$VER-$ARCH.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" >/dev/null
echo "[deb] 产物: $DEB ($(stat -c %s "$DEB") B)"
sha256sum "$DEB" | tee "$DEB.sha256"
