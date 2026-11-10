# 授权与来源说明

本仓库以 **GPL-2.0-or-later** 授权分发。根目录 [`LICENSE`](LICENSE) 是 GPL-2.0
全文；"or later"（可选用更新版本）由各源文件的 SPDX 标识
`GPL-2.0-or-later` 表达。两者缺一不可：只有 README 里一句话不构成授权。

## 分层说明

| 层 | 文件 | 来源 | 授权 |
|---|---|---|---|
| 驱动层 | `canon-scanner-native/src/canon-scan-fix.c`<br>`canon-scanner-native/src/canon-net-discover.c` | 本项目独立编写 | GPL-2.0-or-later |
| 应用层 | `canon-scan-studio/src/*.py`<br>`canon-scan-studio/web/*` | 本项目独立编写 | GPL-2.0-or-later |
| 数据 | `canon-scanner-native/data/canon-mfp2-models.conf`<br>`canon-scanner-native/data/80-canon-scanner.rules` | 本项目独立编写；内容为 USB 产品 ID / 能力位等客观接口事实 | GPL-2.0-or-later |
| 构建 | `*/make-deb.sh`、`assets/` | 本项目独立编写 / 自有截图 | GPL-2.0-or-later |

## 与原厂软件的关系

本仓库**不含任何佳能（Canon）的源代码或二进制文件**，也不链接、不分发佳能的
任何库。全部实现为独立编写。

网络扫描用到的以下内容属于**互操作所必需的接口事实**，本项目据此自行实现，
未复制他方代码：

- SNMP v1 的 community 名 `canon_admin` 与佳能私有 MIB 的 OID（IANA 企业号 1602）
- CHMP 传输方式（`POST` 提交 + `GET` 取 chunked 响应）与 `X-CHMP-Version` 头
- 扫描服务的 URL `/canon/ij/command2/port3`，取自佳能公开源码包中的头文件
  `include/libcnnet3_url.h` 里的 `CNNET3_URL_SCAN`
- IVEC 命令序列与各命令体长度（`CMD_START_JOB` 288 字节等）

USB 扫描走系统 SANE 的 `pixma` 后端公开接口（`scanimage`），不链接其内部代码。

> 说明：上述事实通过观察设备行为、分析报文与阅读佳能公开的 GPL 源码包得到。
> 若后续有人从中发现实质性相似的原厂代码，请开 issue —— 那属于应当修正的问题。

## 分发时的义务

GPL-2.0 要求分发二进制（即 `packages/*.deb`）时提供对应源码。本仓库本身即是
源码，`make-deb.sh` 会生成符合 Debian 机器可读格式的
`/usr/share/doc/<包名>/copyright`，其中已写明授权与上述来源说明。
