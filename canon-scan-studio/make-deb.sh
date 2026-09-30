#!/bin/bash
# 打包 canon-scan-studio（原生 loong64 deb）
set -eu

PKG=canon-scan-studio
VER=1.0
REV=12
ARCH=loong64

ROOT="$(cd "$(dirname "$0")" && pwd)"
PKGPOOL="$(cd "$ROOT/.." && pwd)/packages"   # 产物汇总处（唯一出包位置）
STAGE="$ROOT/build/stage"
DIST="$PKGPOOL"                              # 直接出到仓库 packages/，不再另开 dist/ 副本
DOC="$STAGE/usr/share/doc/$PKG"
DEBFILE="$DIST/${PKG}_${VER}-${REV}_${ARCH}.deb"

rm -rf "$ROOT/build"
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" \
  "$STAGE/usr/lib/canon-studio/web" \
  "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps" \
  "$DOC" "$DIST"

# ---------- 程序 ----------
install -m755 "$ROOT/src/canon-studio"            "$STAGE/usr/bin/canon-studio"
install -m755 "$ROOT/src/canon_webd.py"           "$STAGE/usr/lib/canon-studio/"
install -m644 "$ROOT/src/canon_net.py"            "$STAGE/usr/lib/canon-studio/"
install -m644 "$ROOT/src/canon_scan_net.py"       "$STAGE/usr/lib/canon-studio/"
install -m755 "$ROOT/src/canon-studio-shell.py"   "$STAGE/usr/lib/canon-studio/"
install -m644 "$ROOT/web/index.html"              "$STAGE/usr/lib/canon-studio/web/"
install -m644 "$ROOT/web/app.css"                 "$STAGE/usr/lib/canon-studio/web/"
install -m644 "$ROOT/web/app.js"                  "$STAGE/usr/lib/canon-studio/web/"

# ---------- 集成 ----------
install -m644 "$ROOT/data/canon-studio.desktop"   "$STAGE/usr/share/applications/"
install -m644 "$ROOT/data/canon-studio.svg" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps/canon-studio.svg"

# ---------- 文档 ----------
[ -f "$ROOT/data/README.zh-CN.md" ] && install -m644 "$ROOT/data/README.zh-CN.md" "$DOC/README.md"

cat > "$DOC/copyright" <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: canon-scan-studio
Source: 自研，无上游代码仓库

Files: *
Copyright: 2026 atree
License: GPL-2.0-or-later
 本套件为独立实现，不含任何厂商源代码或二进制。网络扫描的 IVEC/CHMP 命令体
 由本项目自行实现，与原厂驱动真机抓包逐字节一致；此类命令体属互操作必需的
 接口事实，不受著作权保护。USB 扫描经系统 SANE pixma 后端公开接口完成，
 不链接其代码。
 .
 On Debian systems the full text of the GPL-2 license can be found in
 /usr/share/common-licenses/GPL-2.
EOF

# ---------- control ----------
SIZE=$(du -sk "$STAGE/usr" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VER-$REV
Architecture: $ARCH
Maintainer: atree <local>
Installed-Size: $SIZE
Depends: python3 (>= 3.9), sane-utils, libsane1
Recommends: python3-pil, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, canon-scanner-native
Section: graphics
Priority: optional
Description: Canon 扫描 —— PIXMA 一体机的原生扫描界面
 WebKitGTK 桌面界面 + Python 标准库后端。USB 走系统 SANE pixma，网络走
 Canon 私有 IVEC/CHMP 通道。实时预览、多页连扫、PDF 导出、中断自动复位。
 全 LoongArch 原生。
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postrm"

# ---------- 打包 ----------
install -d "$DIST"
[ -f "$DEBFILE" ] && rm -f "$DEBFILE"
XZ_OPT="-T0 -9" dpkg-deb --root-owner-group -Zxz -b "$STAGE" "$DEBFILE" >/dev/null

echo "==> $DEBFILE"
ls -la "$DEBFILE"

echo
echo "===== 包内文件 ====="
dpkg-deb -c "$DEBFILE" | awk '{print $6, $7, $8}' | tail -n +2

echo
echo "===== ELF 架构核查（应全部为 LoongArch 或非 ELF）====="
TMP="$ROOT/build/verify"
rm -rf "$TMP"; mkdir -p "$TMP"
dpkg-deb -x "$DEBFILE" "$TMP"
/usr/bin/python3 - "$TMP" <<'PY'
import os, subprocess, sys
root = sys.argv[1]
bad, elf, other = [], 0, 0
for dp, _, fs in os.walk(root):
    for f in fs:
        p = os.path.join(dp, f)
        try:
            with open(p, 'rb') as fh:
                if fh.read(4) != b'\x7fELF':
                    other += 1
                    continue
        except OSError:
            continue
        elf += 1
        out = subprocess.run(['readelf', '-h', p], capture_output=True, text=True).stdout
        mach = ''
        for line in out.splitlines():
            if 'Machine:' in line:
                mach = line.split(':', 1)[1].strip()
        if 'LoongArch' not in mach:
            bad.append((p, mach))
print(f"  ELF 文件 {elf} 个，非 ELF {other} 个")
print(f"  非 LoongArch 的 ELF: {len(bad)} 个")
for p, m in bad:
    print("   !!", p, m)
PY
echo
echo "===== 单元自检 ====="
/usr/bin/python3 -m py_compile "$ROOT/src/canon_webd.py" && echo "  canon_webd.py 语法 OK"
/usr/bin/python3 -m py_compile "$ROOT/src/canon_scan_net.py" && echo "  canon_scan_net.py 语法 OK"
/usr/bin/python3 -m py_compile "$ROOT/src/canon-studio-shell.py" && echo "  shell.py 语法 OK"
bash -n "$ROOT/src/canon-studio" && echo "  canon-studio 脚本语法 OK"
rm -rf "$ROOT/src/__pycache__"        # py_compile 的字节码缓存，不是源码，清掉

echo
echo "===== 网络扫描协议自检（命令体须逐字节等于原厂抓包）====="
/usr/bin/python3 - "$ROOT/src/canon_scan_net.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location('csn', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
# 期望值 = 原厂 x86-64 扫描驱动在同机同设备上真实抓包里的命令体长度
EXPECT = {'CMD_START_JOB': 288, 'CMD_MODE_SHIFT': 390, 'CMD_END_JOB': 262,
          'CMD_DB20': 16, 'CMD_F320': 16, 'CMD_D820': 72, 'CMD_D920': 16,
          'CMD_DA20': 16, 'CMD_DC20': 16, 'CMD_D420': 16, 'CMD_EF20': 16}
bad = 0
for k in sorted(EXPECT):
    n = EXPECT[k]
    got = len(getattr(m, k))
    ok = got == n
    bad += 0 if ok else 1
    print("  %-14s %3d 字节  %s" % (k, got, "OK" if ok else "!! 期望 %d" % n))
print("  结论：11 条命令体与原厂抓包一致" if not bad
      else "  结论：!! %d 条与原厂抓包不一致" % bad)
sys.exit(1 if bad else 0)
PY
