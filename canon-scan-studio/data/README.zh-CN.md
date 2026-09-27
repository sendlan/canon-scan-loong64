# Canon 扫描（canon-scan-studio）

佳能 PIXMA / MAXIFY 一体机的扫描界面。界面为 HTML/CSS/JS，由 WebKitGTK 承载为
桌面窗口；USB 走系统 SANE pixma 后端，网络走 Canon 私有 CHMP 通道（见下）。可选安装 python3-pil 获得
实时预览与 PDF 合成。

## 安装

```bash
sudo apt install ./canon-scan-studio_*.deb
```

依赖自动带上 sane-utils、libsane1、canon-scanner-native；推荐 python3-pil、
gir1.2-gtk-3.0、gir1.2-webkit2-4.1。

## 启动

应用菜单「Canon 扫描」，或 `canon-studio`。首次启动枚举 SANE 设备约 1~2 秒。

## 用法

原件放玻璃稿台，左侧选色彩/分辨率，点「开始扫描」。扫描进度为真实字节比，
青色光带即扫描头当前位置。输出默认存 `~/扫描/`。

| 操作 | 方式 |
|---|---|
| 多页连扫 | 点「扫描下一页」，每页进页签栏 |
| 导出多页 PDF | 格式选「多页 PDF」，点「保存」 |
| 保存图片 | PNG / JPEG / TIFF |
| 取消扫描 | `Esc` 或再点「开始扫描」 |
| 复位卡住的设备 | 右上角 ↻ |
| 缩放查看 | 底部工具条：适应 / 原尺寸 / ± |
| 删除页面 | 工具条最右侧垃圾桶 |
| 快捷键 | `Ctrl+Enter` 开始；`0`/`+`/`-` 缩放；`F11` 全屏 |

## 输出目录

默认 `~/扫描/`。改目录设环境变量：

```bash
CANON_SCAN_DIR=~/文档/扫描件 canon-studio
```

## 设备卡住

扫描中途被打断（取消/进程被杀/USB 抖动）后，佳能固件停在「上一作业未结束」态：
写成功、读超时。程序会调用 `canon-scan-fix` 做 StartJob/EndJob 就绪握手自动恢复
（状态栏提示「自动恢复中」）。仍失败则点 ↻，再不行就拔插 USB 或断电重启。

命令行手动复位：`canon-scan-fix`（轻）／`canon-scan-fix -R`（整机 USB 复位）／
`canon-scan-fix -n`（只看状态）。

## 网络扫描

设备列表出现带网络通道的佳能一体机时可直接选中扫描，无需接 USB。网络走 Canon
私有 CHMP over HTTP（`/canon/ij/command2/port3`），由后端纯标准库实现
（canon_scan_net.py）；发现用 SNMP v1 单播（community `canon_admin`）。选中网络
设备后参数锁定为 彩色 / 300dpi / A4 / 平板（设备侧固定）。让程序重新发现网络
设备：点右上角 ↻。

注意：SANE pixma 后端不认网络通道（只认 BJNP/MFNP，IVEC 机型默认关闭），
`scanimage -L` 网络方式下必然找不到设备，属正常。

## 环境变量

| 变量 | 作用 |
|---|---|
| `CANON_SCAN_DIR` | 输出目录，默认 `~/扫描` |
| `CANON_PORT` | 固定后端端口，默认自动分配 |
| `CANON_NO_SHELL=1` | 不开原生窗口，改用默认浏览器打开 |
| `CANON_DEVTOOLS=1` | 窗口内启用 Web 检查器 |
| `CANON_WEBKIT_NO_COMPOSITING=1` | 画面花屏/黑屏时降级渲染 |
| `CANON_PYTHON` | 指定 Python 解释器 |

## 内部结构

```
canon-studio               启动器：起后端 → 等就绪 → 开原生窗口
  └─ canon_webd.py         本地 HTTP 服务（127.0.0.1，随机端口）
       ├─ GET  /api/devices /api/caps
       ├─ POST /api/scan   （scanimage / NetScanTask）
       ├─ GET  /api/events   进度事件流（SSE）
       ├─ GET  /api/live     实时预览帧
       ├─ GET  /api/result   最终图像
       ├─ POST /api/add-page /api/save /api/fix
       └─ GET  /             前端静态文件
  └─ canon-studio-shell.py  GTK3 + WebKit2 窗口外壳
```

## 卸载

```bash
sudo apt remove canon-scan-studio
```

## 许可

GPL-2.0-or-later。套件为独立实现，不含任何厂商源代码/二进制。IVEC/CHMP
命令体按与原厂驱动真机抓包一致的方式自行生成，属互操作所必需的接口事实；
USB 经系统 SANE pixma 后端公开接口完成，不链接其代码。
