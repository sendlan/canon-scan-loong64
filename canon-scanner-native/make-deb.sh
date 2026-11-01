#!/bin/bash
# 打包 canon-scanner-native（loong64 原生扫描驱动入口）
set -euo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "$0")" && pwd)"
PKGPOOL="$(cd "$(dirname "$HERE")" && pwd)/packages"   # 产物汇总处（唯一出包位置）
PKG=canon-scanner-native
VER=1.0-8
# 架构名：loong64（Debian/Loongnix）与 loongarch64（AOSC）指向同一套
# LoongArch 二进制，差别只在控制字段。默认出 Architecture: all 的单包，
# 两个系统都能装；要架构专包就 ARCH=loong64 覆盖。
ARCH="${ARCH:-all}"
# 依赖包名随发行版不同时可覆盖，例如 AOSC：
#   DEPS="glibc, libusb, sane-backends" bash make-deb.sh
# 默认值一律写成 OR 依赖，让一份 deb 同时满足 Debian/Loongnix 与 AOSC：
#   - AOSC 没有 sane-utils / libsane1，只有未拆分的 sane-backends
#     （libsane.so.1 + pixma 后端 + scanimage 都在里面）；
#   - AOSC 的 C 库包名为 glibc，libusb 包名为 libusb。
DEPS="${DEPS:-libc6 (>= 2.34) | glibc, libusb-1.0-0 | libusb, libsane1 | sane-backends, sane-utils | sane-backends}"

# 维护者署名：自动取当前 git 仓库身份（user.name / user.email），即你的
# GitHub 身份，保证打包元数据与提交作者一致；取不到用默认值，可覆盖。
GIT_NAME="$(git -C "$HERE" config user.name 2>/dev/null || true)"
GIT_MAIL="$(git -C "$HERE" config user.email 2>/dev/null || true)"
MAINT_NAME="${MAINT_NAME:-${GIT_NAME:-sendlan}}"
MAINT_MAIL="${MAINT_MAIL:-${GIT_MAIL:-sendlan@outlook.com}}"
MAINTAINER="${MAINTAINER:-$MAINT_NAME <$MAINT_MAIL>}"

STAGE="$HERE/stage"
OUT="$PKGPOOL"        # 直接出到仓库 packages/，不再另开 dist/ 副本
MODELS="$HERE/data/canon-mfp2-models.conf"

rm -rf "$STAGE"
mkdir -p "$STAGE"/DEBIAN \
         "$STAGE"/usr/bin \
         "$STAGE"/usr/lib/udev/rules.d \
         "$STAGE"/usr/share/canon-scanner-native \
         "$STAGE"/usr/share/doc/$PKG

echo "== 1/6 编译原生工具（loong64）=="
mkdir -p "$HERE/build"
gcc -O2 -Wall -Wextra -std=c11 \
    -o "$HERE/build/canon-scan-fix" \
    "$HERE/src/canon-scan-fix.c" \
    $(pkg-config --cflags --libs libusb-1.0)
file -b "$HERE/build/canon-scan-fix" | cut -c1-60

echo "   - canon-net-discover（网络扫描仪主动发现，零外部库依赖）"
gcc -O2 -Wall -Wextra -std=c11 \
    -o "$HERE/build/canon-net-discover" \
    "$HERE/src/canon-net-discover.c"
file -b "$HERE/build/canon-net-discover" | cut -c1-60

echo "== 2/6 由机型表生成 udev 规则 =="
# 机型表是唯一事实来源：udev 规则、canon-scan-fix 的机型识别都从它派生，
# 换新机型只需要改 canon-mfp2-models.conf 一处，不必手改规则文件。
[ -f "$MODELS" ] || { echo "缺机型表：$MODELS" >&2; exit 1; }

RULES="$HERE/build/80-canon-scanner.rules"
{
  cat <<'HDR'
# Canon PIXMA / MAXIFY 一体机 —— 普通用户访问权限
#
# ⚠ 本文件由 canon-mfp2-models.conf 在打包时自动生成，请勿手改；
#   要增删机型请改机型表后重新运行 make-deb.sh。
#
# libsane 的 pixma 后端通过 libusb 直接访问设备，普通用户需要对设备节点有
# 读写权限。这里同时给出两种机制：
#   - ENV{libsane_matched}="yes" 让 sane-utils 的 udev 辅助规则接手
#   - MODE="0666" + TAG+="uaccess" 作为独立兜底（不依赖 sane-utils 是否装全）
#
# 只匹配佳能一体机的那个 USB 设备节点，不放宽整类设备，也不碰别的厂商。

HDR
  awk '/^0x/ { pid = toupper(substr($1, 3)); name = $0; sub(/^[^ \t]*[ \t]+[^ \t]*[ \t]*/, "", name);
         printf "# %s\nATTRS{idVendor}==\"04a9\", ATTRS{idProduct}==\"%s\", MODE=\"0666\", ENV{libsane_matched}=\"yes\", TAG+=\"uaccess\"\n\n", name, pid }' \
      "$MODELS"
} > "$RULES"

NMODEL=$(grep -cE '^0x' "$MODELS")
NRULE=$(grep -c 'ATTRS{idProduct}' "$RULES")
[ "$NMODEL" = "$NRULE" ] || { echo "机型表 $NMODEL 项但生成了 $NRULE 条规则" >&2; exit 1; }
echo "  机型 $NMODEL 个 → udev 规则 $NRULE 条"
# 机型表里若有重复 PID 会让 udev 规则重复，这里提前挡住
DUP=$(awk '/^0x/{print $1}' "$MODELS" | sort | uniq -d)
[ -z "$DUP" ] || { echo "机型表有重复 PID：$DUP" >&2; exit 1; }

echo "== 3/6 铺装文件 =="
install -m 0755 "$HERE/build/canon-scan-fix"     "$STAGE/usr/bin/canon-scan-fix"
install -m 0755 "$HERE/build/canon-net-discover" "$STAGE/usr/bin/canon-net-discover"
install -m 0755 "$HERE/scripts/canon-scan"     "$STAGE/usr/bin/canon-scan"
install -m 0755 "$HERE/scripts/canon-scan-gui" "$STAGE/usr/bin/canon-scan-gui"
install -m 0644 "$RULES"     "$STAGE/usr/lib/udev/rules.d/80-canon-scanner.rules"
install -m 0644 "$MODELS"    "$STAGE/usr/share/canon-scanner-native/canon-mfp2-models.conf"
# 注：本包不再提供桌面项。应用菜单里唯一的入口「Canon 扫描」由
#     canon-scan-studio 包提供（Exec=canon-studio）。canon-scan（CLI）与
#     canon-scan-gui（会转启 canon-studio）保留为命令行入口。
install -m 0644 "$HERE/data/README.zh-CN.md"        "$STAGE/usr/share/doc/$PKG/README.md"

cat > "$STAGE/usr/share/doc/$PKG/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: canon-scanner-native
Source: 自研，无上游代码仓库

Files: /usr/bin/canon-scan-fix /usr/bin/canon-scan /usr/bin/canon-scan-gui
       /usr/bin/canon-net-discover
Copyright: 2026 $MAINT_NAME
License: GPL-2.0-or-later
 本包全部为独立实现，不含任何厂商源代码或二进制。网络扫描的 IVEC/CHMP
 报文格式、命令码、OID 与 URL 属互操作所必需的接口事实，本包仅据此自行
 实现，未复制他方代码；扫描服务地址取自佳能开源前端头文件
 include/libcnnet3_url.h 公布的 CNNET3_URL_SCAN。
 .
 On Debian systems the full text of the GPL-2 license can be found in
 /usr/share/common-licenses/GPL-2.

Files: /usr/share/canon-scanner-native/canon-mfp2-models.conf
       /usr/lib/udev/rules.d/80-canon-scanner.rules
Copyright: 2026 $MAINT_NAME
License: GPL-2.0-or-later
 机型表由本项目独立编写（按 USB 产品 ID 升序重排），内容为 USB 产品 ID、
 能力位与机型名这三类接口事实；机型名本身是佳能注册商标，仅用于标识
 兼容的设备型号。
EOF

echo "== 4/6 生成 control =="
INSTALLED_SIZE=$(du -sk "$STAGE/usr" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VER
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_SIZE
Section: graphics
Priority: optional
Depends: $DEPS
Recommends: xsane | simple-scan
Conflicts: scangearmp2
Replaces: scangearmp2
Description: Canon PIXMA/MAXIFY 一体机的 loong64 原生扫描驱动入口
 用 SANE pixma 后端（loong64 原生）驱动佳能一体机，附原生 C 的扫描通道
 恢复工具（canon-scan-fix：中断后设备硬挂起时的 StartJob/EndJob 就绪握手）。
 另附 canon-net-discover：自带 Canon 私有 SNMP v1 的局域网络扫描仪发现，
 并探明 CHMP 扫描通道 /canon/ij/command2/port3，无外部库依赖。
 包内不含任何非 LoongArch 的 ELF 文件。本机实测机型：G3010 (04a9:183b)。
EOF

# 架构自检：Architecture: all 让一份 deb 同时兼容 loong64（Debian/Loongnix）
# 与 loongarch64（AOSC）两个名字，但任何机器都可解包 —— 这里在解包前校验目标架构。
cat > "$STAGE/DEBIAN/preinst" <<'PI'
#!/bin/sh
set -e
MACH="$(uname -m 2>/dev/null || echo unknown)"
# 不同发行版/内核的 uname -m 可能返回 loongarch64，也可能返回 loong64，
# 二者是同一套 LoongArch 64 位（lp64d）ABI，都必须放行。
case "$MACH" in
    loongarch64|loongarch64-*|loong64|loong64-*) ;;
    *)
        echo "canon-scanner-native: 本包只适用于 LoongArch 64 位（loong64 / loongarch64）。" >&2
        echo "canon-scanner-native: 当前机器架构为 '$MACH'，已拒绝安装，未做任何改动。" >&2
        exit 1
        ;;
esac
exit 0
PI
chmod 755 "$STAGE/DEBIAN/preinst"

cat > "$STAGE/DEBIAN/conffiles" <<'EOF'
EOF
rm -f "$STAGE/DEBIAN/conffiles"

( cd "$STAGE" && find usr -type f -exec md5sum {} \; > DEBIAN/md5sums )

echo "== 5/6 校验：包内是否含非 LoongArch ELF =="
bad=0
while IFS= read -r f; do
    if head -c4 "$f" | grep -q $'\x7fELF'; then
        m=$(readelf -h "$f" | awk '/Machine:/{print $2}')
        if [ "$m" != "LoongArch" ]; then
            echo "  !! 非 LoongArch: $f ($m)"; bad=1
        fi
    fi
done < <(find "$STAGE" -type f)
[ $bad -eq 0 ] && echo "  OK：所有 ELF 都是 LoongArch"

echo "== 6/6 打包 =="
mkdir -p "$OUT"
deb="$OUT/${PKG}_${VER}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz -b "$STAGE" "$deb"
ls -la "$deb"
echo
echo "包内容："
dpkg-deb -c "$deb"
