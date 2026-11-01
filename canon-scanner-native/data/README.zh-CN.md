# 佳能一体机扫描 —— loong64 原生方案

让 PIXMA / MAXIFY 一体机在 loong64 上原生扫图。

## 组件

| 组件 | 作用 |
|---|---|
| `canon-scan-fix` | 恢复被卡住的 USB 扫描通道 |
| `canon-net-discover` | 网络扫描仪主动发现（Canon 私有 SNMP v1，只依赖 libc） |
| `canon-scan` | 命令行扫描：自动选设备、失败自动修复重试 |
| `canon-scan-gui` | 转启图形前端（优先 canon-studio，其次 xsane / simple-scan） |
| udev 规则 | 普通用户免 root 访问 USB 扫描仪 |

## 依赖

- 必需：`libsane1`（≥1.4，pixma 后端）、`sane-utils`（scanimage）、`libusb-1.0-0`
- 图形界面（任选）：`canon-scan-studio`（推荐）／`xsane`／`simple-scan`

## canon-scan-fix：扫描中断后设备硬挂起

扫描中断（Ctrl-C / 关窗口 / USB 抖动）后每次扫描都报
`sane_read: Error during device I/O`：需走完整 StartJob / EndJob 往返才算就绪。

```bash
canon-scan-fix            # 清端点 + 就绪握手循环，直到 OK
canon-scan-fix -R         # 整机 USB 复位（最后手段）
canon-scan-fix -n         # 只看设备状态，不写任何东西
canon-scan-fix --no-release   # 只清端点，不动作业状态
canon-scan-fix -a N       # 就绪握手最大次数（默认 6）
```

## 网络扫描

IVEC 机型的网络扫描 SANE 不支持（pixma 只认 BJNP/MFNP），用：

```bash
canon-net-discover        # 单播扫本网段，列出发现的扫描仪与扫描通道
```

发现 = SNMP v1 单播（community `canon_admin`），数据通道 = CHMP over HTTP
`/canon/ij/command2/port3`。图形化网络扫描用 `canon-scan-studio`。

## 用法（canon-scan）

```bash
canon-scan                                # 300dpi 彩色 → ~/扫描/
canon-scan --resolution 600 --mode Gray
canon-scan --format=jpeg -o out.jpg
canon-scan -A / -L                        # 列出可调参数 / 设备
```

参数原样透传给 scanimage。环境变量：`CANON_SCAN_DIR`（输出目录）、
`CANON_SCAN_DEVICE`（强制指定设备）、`CANON_SCAN_FIX`（修复工具路径）、
`CANON_SCAN_RECOVER=0`（关掉自动修复重试）。

注：勿用 lz-scan（1.9.x 会把 pixma 设备误判为网络设备）。

## 支持机型

- USB：SANE pixma 认得的佳能 IVEC 机型（G / MG / TS / MX / E 系列等）
- 网络：带 CHMP 网络扫描的 IVEC 机型，可用 `canon-net-discover` 确认
- 维护 / 打印通路：由配套 `cnijfilter2` 提供

## 卸载

```bash
sudo apt remove -y canon-scanner-native
```

许可：GPL-2.0-or-later。
