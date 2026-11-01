# Canon 扫描（canon-scan-studio）

佳能 PIXMA / MAXIFY 一体机的扫描界面。可选安装 `python3-pil` 获得实时预览与 PDF 合成。

## 安装

```bash
sudo apt install ./canon-scan-studio_*.deb
```

依赖自动带上 sane-utils、libsane1、canon-scanner-native；推荐 python3-pil、
gir1.2-gtk-3.0、gir1.2-webkit2-4.1。

## 启动

应用菜单「Canon 扫描」，或 `canon-studio`。首次启动枚举 SANE 设备约 1~2 秒。

## 用法

原件放玻璃稿台，左侧选色彩/分辨率，点「开始扫描」。输出默认存 `~/扫描/`。

| 操作 | 方式 |
|---|---|
| 身份证双面合一页 | 扫描模式选「身份证」：先扫正面，翻面后自动扫反面，两卡合成一页 A4（上下各半） |
| 两页 A4 合成一张 | 扫描模式选「两页合一」：先扫第一页，换上第二页再扫，自动排到一张 A4（竖放）。「横向」（默认）＝两页各自转横、A 上 B 下，铺满整张；「纵向」＝两页保持竖向、A 左 B 右 |
| 多页连扫 | 点「扫描下一页」，每页进页签栏 |
| 导出多页 PDF | 格式选「多页 PDF」，点「保存」 |
| 保存图片 | PNG / JPEG / TIFF |
| 取消扫描 | `Esc` 或再点「开始扫描」 |
| 复位卡住的设备 | 右上角 ↻ |
| 缩放查看 | 底部工具条：适应 / 原尺寸 / ± |
| 删除页面 | 工具条最右侧垃圾桶 |
| 快捷键 | `Ctrl+Enter` 开始；`0`/`+`/`-` 缩放；`F11` 全屏 |

## 设备卡住

扫描被打断后设备停在「上一作业未结束」态，程序自动做就绪握手恢复（状态栏
提示「自动恢复中」）。仍失败点 ↻，再不行拔插 USB 或断电重启。命令行手动
复位：`canon-scan-fix`（轻）／`canon-scan-fix -R`（整机 USB 复位）。

## 网络扫描

设备列表出现带网络通道的佳能一体机时可直接选中扫描，无需接 USB。网络参数
锁定为 彩色 / 300dpi / A4 / 平板（设备侧固定）。重新发现：点右上角 ↻。
注：`scanimage -L` 找不到网络设备属正常（SANE pixma 不认 IVEC 网络通道）。

## 环境变量

| 变量 | 作用 |
|---|---|
| `CANON_SCAN_DIR` | 输出目录，默认 `~/扫描` |
| `CANON_PORT` | 固定后端端口，默认自动分配 |
| `CANON_NO_SHELL=1` | 不开原生窗口，改用默认浏览器打开 |
| `CANON_DEVTOOLS=1` | 窗口内启用 Web 检查器 |
| `CANON_WEBKIT_NO_COMPOSITING=1` | 画面花屏/黑屏时降级渲染 |
| `CANON_PYTHON` | 指定 Python 解释器 |

## 卸载

```bash
sudo apt remove canon-scan-studio
```

许可：GPL-2.0-or-later。
