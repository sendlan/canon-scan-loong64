# 佳能一体机扫描 —— loong64 原生方案

让 PIXMA / MAXIFY 一体机在 loong64 上纯原生扫图。包内**不含任何非 LoongArch 的
ELF**，不依赖 box64 / LATX / qemu 指令翻译层。

## 组件

| 组件 | 作用 |
|---|---|
| `canon-scan-fix` | 原生 C：恢复被卡住的 USB 扫描通道 |
| `canon-net-discover` | 网络扫描仪主动发现（自带 Canon 私有 SNMP v1，只依赖 libc，不 dlopen 任何厂商库） |
| `canon-scan` | 命令行扫描：自动选设备、失败自动修复重试 |
| `canon-scan-gui` | 转启图形前端（优先 canon-studio，其次 xsane / simple-scan） |
| udev 规则 | 普通用户免 root 访问 USB 扫描仪 |

## 依赖

- 必需：`libsane1`（≥1.4，pixma 后端）、`sane-utils`（scanimage）、`libusb-1.0-0`
- 图形界面（任选）：`canon-scan-studio`（推荐）／`xsane`／`simple-scan`

## canon-scan-fix：扫描中断后设备硬挂起

现象：扫描中途被打断（Ctrl-C、关窗口、程序崩溃、USB 抖动）后，之后每次扫描都是
`sane_read: Error during device I/O` —— 写下去成功、设备完全不回话。根因是设备
内部仍认为有 PC 扫描作业在场：**光 clear_halt 不够**，必须走一遍完整
StartJob / EndJob 往返、拿到 `<ivec:response>OK</ivec:response>` 才算就绪。

```bash
canon-scan-fix            # 清端点 + 就绪握手循环，直到 OK
canon-scan-fix -R         # 整机 USB 复位（最后手段）
canon-scan-fix -n         # 只看设备状态，不写任何东西
canon-scan-fix --no-release   # 只清端点，不动作业状态
canon-scan-fix -a N       # 就绪握手最大次数（默认 6）
```

## 网络扫描

佳能 IVEC 一体机的网络扫描不在 SANE 支持范围内（pixma 只认 BJNP:8612 /
MFNP:8610，IVEC 机型默认关闭），需要自带发现手段：

```bash
canon-net-discover        # 单播扫本网段，列出发现的扫描仪与扫描通道
```

发现用 SNMP v1 单播（community `canon_admin`；广播会被带 conntrack 的防火墙拒掉）；
数据通道为 CHMP over HTTP `/canon/ij/command2/port3`。图形化网络扫描请用
`canon-scan-studio`。

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

注：勿用 lz-scan（靓仔扫描）—— 其 1.9.x 会把 SANE 设备名含 pixma 的一律误判为
网络设备，必然失败。

## 支持机型

- USB：SANE pixma 后端认得的佳能 IVEC 机型（G / MG / TS / MX / E 系列等）皆可用，
  支持范围随 pixma 扩展而扩大。
- 网络：需设备自身启用 CHMP 网络扫描（带网络功能的 IVEC 机型）。可先跑一次
  `canon-net-discover` 确认。
- 维护 / 打印通路：由配套 `cnijfilter2`（打印机侧）提供。

## 卸载

```bash
sudo apt remove -y canon-scanner-native
```

许可：GPL-2.0-or-later，本包全部为独立实现。
