# 授权与来源说明

本仓库以 **GPL-2.0-or-later** 授权分发。根目录 [`LICENSE`](LICENSE) 是 GPL-2.0
全文；"or later"（可选用更新版本）由各源文件的 SPDX 标识
`GPL-2.0-or-later` 表达。两者缺一不可：只有 README 里一句话不构成授权。

该授权只覆盖**本项目自己编写的代码与表达**。本仓库不含任何佳能源代码或
二进制文件，因此不存在"把厂商 GPLv2-only 代码单方面改成 or-later"的问题；
若将来从佳能源码包引入实际代码，必须单独标注为 `GPL-2.0-only`，见下文。

## 分层说明

| 层 | 文件 | 来源 | 授权 |
|---|---|---|---|
| 驱动层 | `canon-scanner-native/src/canon-scan-fix.c`<br>`canon-scanner-native/src/canon-net-discover.c` | 本项目独立编写 | GPL-2.0-or-later |
| 应用层 | `canon-scan-studio/src/*.py`<br>`canon-scan-studio/web/*` | 本项目独立编写 | GPL-2.0-or-later |
| 数据 | `canon-scanner-native/data/canon-mfp2-models.conf`<br>由它自动生成的 udev 规则 | 表格格式、字段定义与按 PID 升序的编排由本项目编写；其中的 USB 产品 ID / 能力位 / 机型名与佳能官方驱动 `canon_mfp2.conf` 的事实数据一致（逐条核对见下） | GPL-2.0-or-later（编排与表达）；事实数据本身不受著作权保护 |
| 构建 | `*/make-deb.sh`、`assets/` | 本项目独立编写 / 自有截图 | GPL-2.0-or-later |

机型名（PIXMA / MAXIFY / LiDE / TS / GX / XK 等）是 Canon Inc. 的商标，仅用于
指明兼容设备；Canon Inc. 未参与本软件开发，也不为其提供支持。

## 与佳能官方源码包的关系

以佳能官方 `scangearmp2-source-4.91-1`（2026-08）核对。该包按
`doc/LICENSE-scangearmp-4.91*.txt` 分为三类模块，**不是整体 GPL**：

- **Module I（EULA 专有）**：`80-canon_mfp2.rules`、`canon_mfp2.conf`、
  `libcncpmslld2` / `libcncpnet2` / `libcncpnet20` / `libcncpnet30` 的
  预编译 `.so`、`canon_mfp2_net.ini`。随包提供 aarch64 / i686 / mips64 /
  x86_64 四种二进制，**没有 loongarch64** —— 官方驱动在本机架构上无法运行，
  这也是本项目存在的原因。
- **Module II（GPL v2）**：`.mo`、`scangearmp2-pkgconfig.sh`、`scangearmp2.glade`
- **Module III（GPL v2 + 链接例外）**：`scangearmp2` 主程序源码，例外允许它
  与 Module I 的闭源库链接而不使整体受 GPL 覆盖。

EULA 与 `scangearmp2/LICENSE` 均把 GPL 明确定义为 **Version 2**（`GPL-2.0-only`，
没有 "or later"）。本仓库不分发、不链接、也不复制上述任何模块。

## 接口事实的来源

本仓库**不含任何佳能源代码或二进制**，全部实现为独立编写。网络扫描用到的
以下内容属于互操作所必需的**接口事实**（协议格式、地址、常量、硬件标识），
不是代码表达：

- 扫描服务 URL `/canon/ij/command2/port3`：取自佳能源码包的 GPLv2 头文件
  `include/libcnnet3_url.h` 中的 `CNNET3_URL_SCAN`（该头文件带 Canon 版权头与
  链接例外声明）。本项目只使用这一个字符串常量，未引入该文件的代码。
- SNMP v1 community `canon_admin` 与佳能私有 MIB OID（IANA 企业号 1602）
- CHMP 传输方式（`POST` 提交 + `GET` 取 chunked 响应）与 `X-CHMP-Version` 头
- IVEC 命令序列与各命令体长度（`CMD_START_JOB` 288 字节等）

后三项**不在佳能公开的 GPL 源码里**：官方 `src/` 与 `include/` 中完全没有
CHMP / SNMP / `canon_admin` 字样，协议栈在 Module I 的闭源
`libcncpnet*.so` 中。它们是通过观察设备网络报文得到的，此处如实说明来源，
不表述为"阅读官方 GPL 源码包得到"。

USB 扫描走系统 SANE 的 `pixma` 后端公开接口（`scanimage`），不链接其内部代码。

> 法律边界：佳能 EULA 禁止对 Module I 做反向工程。本项目只提取为互操作所
> 必需的接口事实，未反编译、未复制其代码；此种互操作所需的观察在中国
> 《计算机软件保护条例》第十七条、欧盟 2009/24/EC 第 5/6 条下一般被认可。
> 这不构成法律意见，也不意味着可以使用或再分发 Module I 的二进制。
> 若有人发现本仓库中存在与原厂代码实质性相似的表达，请开 issue —— 那属于
> 应当修正的问题。

## 与官方 4.91-1 的逐条核对（2026-11-10）

- 机型表：官方 `canon_mfp2.conf` 162 条，本项目 `canon-mfp2-models.conf` 162 条；
  **USB 产品 ID、能力码、机型名逐条一致，无缺失、无多余、无取值差异**。
  本项目只是改成按 PID 升序的自定义格式，并据此自动生成 udev 规则。
- `NEWS` 中 4.90 → 4.91 仅写 "Fixed various issues."，**没有新增机型**，
  4.90 新增的 15 个机型已全部在本表中。故本包无需因 4.91 更新数据或代码。
- 官方包内无任何 loongarch64 二进制，也没有可移植到本项目的协议源码。

## 分发时的义务

GPL-2.0 要求分发二进制（即 `packages/*.deb`）时提供对应源码。本仓库本身即是
源码；`make-deb.sh` 会把 `LICENSE` 全文随包装入 `/usr/share/doc/<包名>/`，
并生成 Debian 机器可读格式的 `copyright`（不依赖系统上是否存在
`/usr/share/common-licenses/GPL-2`）。
