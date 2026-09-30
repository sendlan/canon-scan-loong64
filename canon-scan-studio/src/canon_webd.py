#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
canon_webd —— 佳能 PIXMA 一体机 scanimage 的本地 Web 后端

纯标准库。图像解码与 PDF 合成可选依赖 Pillow；缺失时降级为"只有进度、没有
实时预览"，扫描本身不受影响。

* scanimage 先吐 PNM 头部，之后像素数据流式到达 —— 据此算真实百分比，
  并把已扫到的部分即时解码成 JPEG 预览帧。
* 扫描被中断（用户取消 / 进程被杀）会让佳能 IVEC 固件进入"作业占用"态，
  之后所有扫描都超时；因此任何异常路径都会自动调用 canon-scan-fix 恢复。
* 前端用 SSE 拿进度数字，预览图走独立端点，避免大 payload 堵在事件流里。
"""

import base64
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import queue
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))


def _first_dir(*cands):
    # 都不存在时返回首个候选而非报错，便于定位安装路径
    for c in cands:
        if os.path.isdir(c):
            return os.path.normpath(c)
    return os.path.normpath(cands[0])


# 静态资源根：安装布局 /usr/lib/canon-studio/web；开发布局 <repo>/web/web
WEBROOT = _first_dir(os.path.join(HERE, "web"),
                     os.path.join(HERE, "..", "web"))

# 设备复位工具查找顺序：PATH（canon-scanner-native 装到 /usr/bin）→ 后端同目录
# → 开发树 pkg/build
FIX_BIN = shutil.which("canon-scan-fix")
if not FIX_BIN:
    for _c in (os.path.join(HERE, "canon-scan-fix"),
               os.path.join(os.path.dirname(HERE), "..", "..", "pkg", "build",
                            "canon-scan-fix")):
        if os.path.exists(_c):
            FIX_BIN = os.path.normpath(_c)
            break
    else:
        FIX_BIN = os.path.join(HERE, "canon-scan-fix")

OUTDIR = os.path.expanduser(os.environ.get("CANON_SCAN_DIR", "~/扫描"))
CACHEDIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "canon-studio")

PREVIEW_W = 620
PREVIEW_QUALITY = 72
# 预览节流：距上次 ≥0.28s 或新增 ≥24 行才刷一帧
PREVIEW_MIN_DT = 0.28
PREVIEW_MIN_ROWS = 24

# 纸张预设（mm）。None 表示"设备最大幅面"
PAPERS = [
    {"id": "max",    "name": "整幅玻璃",  "w": None,    "h": None},
    {"id": "a4",     "name": "A4 210×297", "w": 210.0,   "h": 297.0},
    {"id": "a4p",    "name": "A4+ 216×297", "w": 216.069, "h": 297.011},
    {"id": "a5",     "name": "A5 148×210", "w": 148.0,   "h": 210.0},
    {"id": "a6",     "name": "A6 105×148", "w": 105.0,   "h": 148.0},
    {"id": "stmt",   "name": "Statement 140×216", "w": 139.7, "h": 215.9},
    {"id": "b5",     "name": "B5 176×250", "w": 176.0,   "h": 250.0},
    {"id": "letter", "name": "Letter 216×279", "w": 215.9, "h": 279.4},
    {"id": "photo4x6", "name": "照片 4×6\"", "w": 101.6,  "h": 152.4},
    {"id": "photo5x7", "name": "照片 5×7\"", "w": 127.0,  "h": 177.8},
]

HAS_PIL = False
try:
    from PIL import Image
    from PIL import ImageFilter
    HAS_PIL = True
except Exception:
    Image = None


def log(*a):
    print("[canon-webd]", *a, file=sys.stderr, flush=True)


# 网络发现：同目录 canon_net.py（CNNET2_*），库不在时退回内置 SNMP v1 单播发现
HAVE_NET = False
canon_net = None
try:
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import canon_net as _cn
    canon_net = _cn
    HAVE_NET = True
except Exception as _e:                              # noqa: BLE001
    log("网络发现模块不可用（将只显示 USB 设备）:", _e)

# 网络扫描：canon_scan_net.py 经 CHMP/HTTP（/canon/ij/command2/port3）取图
HAVE_NETSCAN = False
canon_scan_net = None
try:
    import canon_scan_net as _csn
    canon_scan_net = _csn
    HAVE_NETSCAN = True
except Exception as _e2:                             # noqa: BLE001
    log("网络扫描模块不可用（网络设备只能发现不能扫）:", _e2)


class PnmStream:
    """流式解析 PNM：先吃头部，之后按行累计像素数据。"""

    MAGIC = {b"P1": ("1", 1), b"P2": ("L", 1), b"P3": ("RGB", 3),
             b"P4": ("1", 1), b"P5": ("L", 1), b"P6": ("RGB", 3)}

    def __init__(self):
        self.header_done = False
        self.pil_mode = None
        self.w = self.h = self.maxval = 0
        self.rowbytes = 0
        self.total = 0
        self.data = bytearray()
        self._tok = []
        self._raw = bytearray()
        self.error = None

    def feed_head(self, chunk):
        """消耗 chunk 的头部部分，返回已消费的字节数。

        调用方须把 chunk[consumed:] 交给 append()，否则像素数据会丢。
        """
        if self.header_done:
            return 0
        consumed = 0
        for byte in chunk:
            consumed += 1
            self._raw.append(byte)
            if byte == 0x0A:
                line = bytes(self._raw).rstrip(b"\r\n")
                self._raw.clear()
                if not line or line.startswith(b"#"):
                    continue
                self._tok.extend(line.split())
                if len(self._tok) >= 4:
                    break
        if len(self._tok) < 4:
            return consumed
        try:
            magic = self._tok[0]
            if magic not in self.MAGIC:
                raise ValueError("不是 PNM 数据：magic=%r" % magic)
            self.pil_mode, channels = self.MAGIC[magic]
            self.w = int(self._tok[1])
            self.h = int(self._tok[2])
            self.maxval = int(self._tok[3])
        except Exception as e:                     # noqa: BLE001
            self.error = str(e)
            return consumed
        if self.w <= 0 or self.h <= 0:
            self.error = "PNM 尺寸非法 %dx%d" % (self.w, self.h)
            return consumed
        # 非标准情形：maxval 与像素数据同行 —— 把多余 token 还原回数据区
        extra = self._tok[4:]
        self._tok = self._tok[:4]
        if magic == b"P4":
            self.rowbytes = (self.w + 7) // 8
        else:
            self.rowbytes = self.w * channels * (2 if self.maxval > 255 else 1)
        self.total = self.rowbytes * self.h
        self.header_done = True
        if extra:
            self.data.extend(b" ".join(extra) + b"\n")
        return consumed

    @property
    def bytes_ready(self):
        return len(self.data)

    @property
    def rows_ready(self):
        if not self.rowbytes:
            return 0
        return min(len(self.data) // self.rowbytes, self.h)

    @property
    def complete(self):
        return self.header_done and len(self.data) >= self.total

    def append(self, chunk):
        if not self.header_done:
            return
        room = self.total - len(self.data)
        if room > 0:
            self.data.extend(chunk[:room])


def pnm_to_image(stream, rows=None):
    """把前 rows 行（默认全部）解码成 PIL Image。"""
    if not HAS_PIL or stream.error or not stream.header_done:
        return None
    rows = min(rows or stream.h, stream.h, stream.rows_ready)
    if rows <= 0:
        return None
    need = stream.rowbytes * rows
    if len(stream.data) < need:
        return None
    # memoryview 零拷贝：600dpi 时数据区可达 26MB，避免每次预览都复制
    raw = memoryview(stream.data)[:need]
    try:
        return Image.frombytes(stream.pil_mode, (stream.w, rows), raw)
    except Exception as e:                          # noqa: BLE001
        log("解码预览失败:", e)
        return None


OPT_RE = re.compile(r"^\s{2,}(-{1,2}[A-Za-z0-9][A-Za-z0-9-]*)\s*(.*)$")
UNITS = ("dpi", "mm", "dpcm", "percent", "%", "in", "cm")


def _strip_unit(tok):
    t = tok.strip()
    for u in ("dpi", "dpcm", "percent", "%", "mm", "cm", "in"):
        if t.lower().endswith(u):
            t = t[: -len(u)].strip()
            break
    return t


def parse_options(text):
    """解析 scanimage -A 的输出为选项字典。"""
    opts, cur = {}, None
    for line in text.splitlines():
        if not line.strip():
            continue
        m = OPT_RE.match(line)
        if m and (m.group(1).startswith("-")):
            name, rest = m.group(1), m.group(2)
            brackets = re.findall(r"\[([^\]]*)\]", rest)
            head = rest[: rest.find("[")] if "[" in rest else rest
            default = brackets[-1] if brackets else ""
            flags = []
            for b in brackets[:-1] if brackets else []:
                flags.append(b)
            if default in ("read-only", "advanced", "inactive"):
                flags.append(default)
                if "read-only" not in flags:
                    # [inactive] 出现在 default 位，本身不作为 flag 再追加
                    pass
            if "read-only" in flags:
                continue                       # 只读选项对界面无意义
            if default == "advanced":
                default = ""
            o = {
                "name": name,
                "default": default,
                "inactive": "inactive" in flags or default == "inactive",
                "raw": (head or "").strip(),
                "values": [],
                "range": None,
            }
            raw = o["raw"]
            if raw:
                # 约束形如 "auto|75|150|300|600dpi" / "auto|0..216.069mm" /
                # "Flatbed" / "auto|0.299988..5" / "<int>"
                parts = [p.strip() for p in raw.split("|")] if "|" in raw \
                    else [raw.strip()]
                rng, vals = None, []
                for p in parts:
                    if ".." in p:
                        lo, hi = p.split("..", 1)
                        try:
                            rng = [float(_strip_unit(lo)),
                                   float(_strip_unit(hi))]
                        except ValueError:
                            rng = None          # 如 "0..65535,..." 之类，放弃
                    elif p:
                        vals.append(_strip_unit(p))
                if rng:
                    o["range"] = rng
                    o["values"] = []
                else:
                    # 纯枚举去掉 auto（界面用默认值表示"自动"），但单值枚举必须
                    # 保留，否则 --source Flatbed 会丢
                    clean = [v for v in vals if v != "auto"]
                    o["values"] = clean if clean else vals
            opts[name] = o
            cur = name
        elif cur:
            pass
    return opts


class DeviceManager:
    def __init__(self):
        self._cache = None
        self._t = 0
        self.lock = threading.Lock()

    def list(self, force=False):
        with self.lock:
            # 注意用 is not None：无设备时结果是 []（falsy），误判会让缓存永不
            # 命中，每次请求都白等 scanimage 的 BJNP 发现超时（实测 ~7s）
            if (self._cache is not None and not force
                    and time.time() - self._t < 6):
                return self._cache
            devs = []
            try:
                p = subprocess.run(["scanimage", "--formatted-device-list",
                                    "%d\t%v\t%m\t%t"],
                                   capture_output=True, text=True, timeout=40)
                for line in p.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = (line.split("\t") + ["", "", ""])[:4]
                    dev_id, vendor, model, dtype = parts
                    bus = "USB" if dtype.lower().startswith("flat") or \
                        "usb" in dev_id.lower() or dev_id.count(":") == 1 and \
                        not dev_id.startswith("net") else "网络"
                    if "escl" in dev_id or dev_id.startswith("net"):
                        bus = "网络"
                    elif dev_id.startswith("pixma:") and not dev_id.startswith("pixma:net"):
                        bus = "USB"
                    devs.append({
                        "id": dev_id,
                        "vendor": vendor or "Canon",
                        "model": model or dev_id,
                        "type": dtype or "平板扫描仪",
                        "bus": bus,
                    })
            except Exception as e:                  # noqa: BLE001
                log("枚举设备失败:", e)
            self._cache, self._t = devs, time.time()
            return devs

    def caps(self, dev):
        try:
            p = subprocess.run(["scanimage", "-d", dev, "-A"],
                               capture_output=True, text=True, timeout=40)
            if p.returncode != 0 and not p.stdout:
                return {"error": (p.stderr or "scanimage -A 失败").strip()}
            opts = parse_options(p.stdout)
            return {"device": dev, "options": opts, "papers": PAPERS}
        except Exception as e:                      # noqa: BLE001
            return {"error": str(e)}


DEVMGR = DeviceManager()


class NetworkManager:
    """Canon IJ 网络扫描仪主动发现（不走 SANE：sane-pixma 只认 BJNP:8612 /
    MFNP:8610，佳能 IVEC 一体机不开这两个端口）。同时探明扫描服务地址
    CNNET3_URL_SCAN = /canon/ij/command2/port3。
    """

    CACHE_TTL = 25.0

    def __init__(self):
        self._cache = None
        self._meta = {}
        self._t = 0.0
        self.lock = threading.Lock()

    def list(self, force=False, timeout_ms=3000):
        with self.lock:
            if (self._cache is not None and not force
                    and time.time() - self._t < self.CACHE_TTL):
                return self._cache, self._meta
            if not HAVE_NET:
                self._cache = []
                self._meta = {"method": "unavailable",
                              "error": "缺少网络发现模块"}
                return self._cache, self._meta
            try:
                entries, res = canon_net.net_device_entries(timeout_ms=timeout_ms)
                self._meta = {"method": res.get("method", ""),
                              "elapsed": res.get("elapsed"),
                              "found": len(entries)}
                log("网络发现：%s，%d 台，%.2fs" %
                    (self._meta["method"], len(entries),
                     res.get("elapsed", 0)))
            except Exception as e:                  # noqa: BLE001
                log("网络发现失败:", e)
                entries = []
                self._meta = {"method": "error", "error": str(e)}
            self._cache, self._t = entries, time.time()
            return self._cache, self._meta


NETMGR = NetworkManager()


def all_devices(force=False):
    """SANE（USB/其它后端）+ 网络发现，合并成一份设备清单。"""
    devs = list(DEVMGR.list(force))
    net, meta = NETMGR.list(force)
    for d in devs:
        d["net"] = False
    return devs + net, meta


def fix_device(full_reset=False, quiet=True):
    """调用 canon-scan-fix 复位扫描通道。返回 (ok, 输出)。"""
    if not os.path.exists(FIX_BIN):
        return False, "找不到 canon-scan-fix"
    cmd = [FIX_BIN]
    if quiet:
        cmd.append("-q")
    if full_reset:
        cmd.append("-R")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except Exception as e:                          # noqa: BLE001
        return False, str(e)


class ScanTask:
    """一次扫描 = 一个 scanimage 子进程 + 一个读取线程。"""

    def __init__(self, dev, opts, kind="scan"):
        self.id = uuid.uuid4().hex[:12]
        self.dev = dev
        self.opts = opts
        self.kind = kind
        self.q = queue.Queue()
        self.proc = None
        self.stream = PnmStream()
        self.preview_jpeg = None
        self.preview_seq = 0
        self.state = "pending"        # pending running done error cancelled
        self.error = None
        self.msg = ""
        self.started = time.time()
        self.t0 = 0.0
        self.t_scan = 0.0
        self.done = threading.Event()
        self._stop = False
        self._last_prev = 0.0
        self._last_rows = 0
        self.image = None
        self.saved_path = None
        self.thumb = None
        self.lock = threading.Lock()
        self.retried = 0

    def build_cmd(self):
        o = self.opts
        cmd = ["scanimage", "-d", self.dev, "--format=pnm",
               "--resolution", str(o.get("resolution", "300")),
               "--mode", str(o.get("mode", "Color"))]
        if o.get("source"):
            cmd += ["--source", str(o["source"])]
        for k, flag in (("left", "-l"), ("top", "-t"),
                        ("width", "-x"), ("height", "-y")):
            v = o.get(k)
            if v not in (None, "", "auto"):
                cmd += [flag, str(v)]
        # 光密度 / 阈值类（如有）
        for k in ("brightness", "contrast", "gamma", "threshold"):
            v = o.get(k)
            if v not in (None, ""):
                cmd += ["--" + k, str(v)]
        if o.get("calibrate"):
            cmd += ["--calibrate", str(o["calibrate"])]
        return cmd

    def emit(self, ev):
        self.q.put(ev)

    def _progress_payload(self, extra=None):
        st = self.stream
        pct = 0.0
        if st.total:
            pct = min(100.0, len(st.data) * 100.0 / st.total)
        elif st.header_done:
            pct = 0.0
        d = {
            "type": "progress",
            "pct": round(pct, 2),
            "bytes": len(st.data),
            "total": st.total or 0,
            "rows": st.rows_ready,
            "H": st.h,
            "W": st.w,
            "seq": self.preview_seq,
            "elapsed": round(time.time() - self.t0, 2) if self.t0 else 0,
        }
        if extra:
            d.update(extra)
        return d

    def _maybe_preview(self, force=False):
        st = self.stream
        if not HAS_PIL or not st.header_done or st.error:
            return
        now = time.time()
        rows = st.rows_ready
        if not force:
            if now - self._last_prev < PREVIEW_MIN_DT and \
                    rows - self._last_rows < PREVIEW_MIN_ROWS:
                return
            if rows <= self._last_rows:
                return
        img = pnm_to_image(st, rows)
        if img is None:
            return
        try:
            scale = PREVIEW_W / float(img.width)
            if scale < 1.0:
                prev = img.resize((PREVIEW_W, max(1, int(img.height * scale))),
                                  Image.BILINEAR)
            else:
                prev = img
            buf = io.BytesIO()
            prev.convert("RGB" if prev.mode not in ("L", "1") else "L").save(
                buf, "JPEG", quality=PREVIEW_QUALITY, optimize=False)
            with self.lock:
                self.preview_jpeg = buf.getvalue()
                self.preview_seq += 1
            self._last_prev, self._last_rows = now, rows
        except Exception as e:                      # noqa: BLE001
            log("预览编码失败:", e)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _run(self):
        self.state = "running"
        cmd = self.build_cmd()
        log("扫描开始:", " ".join(cmd))
        self.emit({"type": "start", "cmd": " ".join(cmd),
                   "device": self.dev, "opts": self.opts})

        for attempt in range(2):
            if self._stop:
                break
            ok, why = self._once()
            if ok:
                # ⚠️ 顺序不能反：必须先 _finalize() 出图像+缩略图，再 emit done。
                #    前端收到 done 立刻 POST /api/add-page，此时若尚未出图，画廊会
                #    永久存入 thumb=None，缩略图再也不显示。
                self._finalize()
                self.state = "done"
                self.emit({"type": "done",
                           "pct": 100.0,
                           "W": self.stream.w, "H": self.stream.h,
                           "bytes": len(self.stream.data),
                           "elapsed": round(self.t_scan, 2),
                           "retried": self.retried})
                self.done.set()
                return
            if self._stop:
                break
            self.error = why
            if attempt == 0:
                self.msg = "第 1 次失败（%s），正在自动复位扫描通道…" % why
                self.retried = 1
                self.emit({"type": "retry", "msg": self.msg, "why": why})
                log(self.msg)
                fix_device(False)
                time.sleep(1.2)
            else:
                self.msg = "第 2 次仍失败（%s），尝试整机 USB 复位…" % why
                self.emit({"type": "retry", "msg": self.msg, "why": why})
                log(self.msg)
                fix_device(True)
                time.sleep(2.5)
                if not self._stop:
                    ok, why = self._once()
                    if ok:
                        self._finalize()
                        self.state = "done"
                        self.emit({"type": "done", "pct": 100.0,
                                   "W": self.stream.w, "H": self.stream.h,
                                   "bytes": len(self.stream.data),
                                   "elapsed": round(self.t_scan, 2),
                                   "retried": 2})
                        self.done.set()
                        return
                    self.error = why

        if self._stop:
            self.state = "cancelled"
            self.emit({"type": "cancelled"})
        else:
            self.state = "error"
            self.emit({"type": "error",
                       "msg": self.error or "扫描失败",
                       "hint": "请确认扫描仪已通电、稿台盖好；仍失败可拔插 USB 线。"})
        self.done.set()

    def _once(self):
        self.stream = PnmStream()
        self._last_prev, self._last_rows = 0.0, 0
        try:
            self.proc = subprocess.Popen(
                self.build_cmd(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
        except Exception as e:                      # noqa: BLE001
            return False, "无法启动 scanimage: %s" % e

        self.t0 = time.time()
        try:
            while True:
                chunk = self.proc.stdout.read(65536)
                if not chunk:
                    break
                if not self.stream.header_done:
                    used = self.stream.feed_head(chunk)
                    if self.stream.error:
                        break
                    if used < len(chunk):
                        self.stream.append(chunk[used:])
                else:
                    self.stream.append(chunk)
                if not self.stream.header_done:
                    continue
                self._maybe_preview()
                if self._stop:
                    break
                if self.stream.rows_ready != self._last_rows:
                    self.emit(self._progress_payload())
        except Exception as e:                      # noqa: BLE001
            log("读取异常:", e)

        self.t_scan = time.time() - self.t0
        try:
            rc = self.proc.wait(timeout=90)
        except Exception:                           # noqa: BLE001
            self.proc.kill()
            rc = -9
        err = b""
        try:
            err = self.proc.stderr.read() or b""
        except Exception:                           # noqa: BLE001
            pass
        errs = err.decode("utf-8", "replace").strip()

        if self._stop:
            return False, "用户取消"
        if rc != 0:
            tail = errs.splitlines()[-3:]
            return False, ("scanimage 退出码 %s%s" %
                           (rc, ("：" + " / ".join(tail)) if tail else ""))
        if self.stream.error:
            return False, self.stream.error
        if not self.stream.complete:
            return False, ("数据不完整（%d/%d 字节）"
                           % (len(self.stream.data), self.stream.total))
        self._maybe_preview(force=True)
        return True, ""

    def _finalize(self):
        if not HAS_PIL:
            return
        img = pnm_to_image(self.stream)
        if img is None:
            return
        if img.mode == "1":
            img = img.convert("L")
        self.image = img
        try:
            t = img.copy()
            t.thumbnail((190, 260), Image.BILINEAR)
            if t.mode not in ("RGB", "L"):
                t = t.convert("RGB")
            buf = io.BytesIO()
            t.save(buf, "JPEG", quality=80)
            self.thumb = buf.getvalue()
        except Exception as e:                      # noqa: BLE001
            log("缩略图失败:", e)

    def cancel(self):
        self._stop = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=4)
                except Exception:                   # noqa: BLE001
                    self.proc.kill()
            except Exception:                       # noqa: BLE001
                pass
        # 中断后设备会进入"作业占用"态 —— 必须复位
        fix_device(False)

    def save(self, fmt="png", path=None):
        """保存图像到磁盘。返回 (路径, 错误)。"""
        if self.image is None:
            return None, "没有可保存的图像（本机缺少 Pillow）"
        path, err = save_image(self.image, fmt, path)
        if not err:
            self.saved_path = path
        return path, err


def save_image(img, fmt="png", path=None):
    """把 PIL 图像存到 ~/扫描（或指定路径）。"""
    fmt = (fmt or "png").lower()
    ext = {"jpg": "jpg", "jpeg": "jpg", "png": "png",
           "tif": "tif", "tiff": "tif", "bmp": "bmp"}.get(fmt, "png")
    os.makedirs(OUTDIR, exist_ok=True)
    if not path:
        base = time.strftime("扫描-%Y%m%d-%H%M%S")
        path = os.path.join(OUTDIR, base + "." + ext)
        n = 1
        while os.path.exists(path):
            path = os.path.join(OUTDIR, "%s(%d).%s" % (base, n, ext))
            n += 1
    try:
        if fmt in ("jpg", "jpeg") and img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(path)
    except Exception as e:                          # noqa: BLE001
        return None, "写入失败：%s" % e
    return path, ""


TASKS = {}
TASKS_LOCK = threading.Lock()


def new_task(dev, opts, kind="scan"):
    t = ScanTask(dev, opts, kind)
    with TASKS_LOCK:
        TASKS[t.id] = t
    return t


class NetScanTask:
    """一次【网络】扫描 = 一条到设备 port3 的 CHMP/HTTP 会话（见 canon_scan_net）。

    与 ScanTask 的差别：没有 scanimage 子进程、没有 PNM 流 —— 设备直接吐 JPEG
    分块且不预告总长，故只有「累计字节」型进度、没有逐行实时预览。出图按设备
    固定参数（CMD_D820）。对上层暴露与 ScanTask 相同的接口（q/done/image/thumb）。
    """

    # 进度条估算基准：A4 / 300dpi / 彩色 真机实测约 147 KB（设备不预告总长）
    NET_EST_BYTES = 150000

    def __init__(self, dev, opts, kind="scan"):
        self.id = uuid.uuid4().hex[:12]
        self.dev = dev
        self.ip = dev.split(":", 1)[1] if ":" in dev else dev
        self.opts = opts or {}
        self.kind = kind
        self.q = queue.Queue()
        self.preview_jpeg = None
        self.preview_seq = 0
        self.state = "pending"        # pending running done error cancelled
        self.error = None
        self.msg = ""
        self.started = time.time()
        self.t0 = 0.0
        self.t_scan = 0.0
        self.done = threading.Event()
        self._stop = False
        self.image = None
        self.saved_path = None
        self.thumb = None
        self.lock = threading.Lock()
        self.retried = 0
        self.bytes = 0
        self.jpeg_path = None
        self.command = "CHMP http://%s%s" % (
            self.ip, canon_scan_net.CHMP_URL if HAVE_NETSCAN
            else "/canon/ij/command2/port3")

    def emit(self, ev):
        self.q.put(ev)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _on_bytes(self, n):
        self.bytes = n
        pct = min(99.0, n * 100.0 / float(self.NET_EST_BYTES))
        self.emit({"type": "progress", "pct": round(pct, 2), "bytes": n,
                   "total": self.NET_EST_BYTES, "rows": 0, "H": 0, "W": 0,
                   "seq": self.preview_seq,
                   "elapsed": round(time.time() - self.t0, 2)})

    def _run(self):
        self.state = "running"
        self.t0 = time.time()
        log("网络扫描开始:", self.command, self.opts)
        self.emit({"type": "start", "cmd": self.command, "device": self.dev,
                   "opts": self.opts, "net": True})

        if not HAVE_NETSCAN:
            self.state = "error"
            self.error = "本机缺少网络扫描模块（canon_scan_net.py）"
            self.emit({"type": "error", "msg": self.error})
            self.done.set()
            return

        try:
            os.makedirs(CACHEDIR, exist_ok=True)
        except Exception:                           # noqa: BLE001
            pass
        self.jpeg_path = os.path.join(CACHEDIR, "net-%s.jpg" % self.id)

        try:
            r = canon_scan_net.scan(
                self.ip, self.jpeg_path,
                progress=lambda m: self.emit({"type": "stage", "msg": m}),
                on_bytes=self._on_bytes,
                cancel_check=lambda: self._stop)
        except canon_scan_net.NetScanCancelled:
            self.state = "cancelled"
            self.emit({"type": "cancelled"})
            self.done.set()
            return
        except Exception as e:                      # noqa: BLE001
            if self._stop:
                self.state = "cancelled"
                self.emit({"type": "cancelled"})
            else:
                self.state = "error"
                self.error = str(e) or e.__class__.__name__
                self.emit({"type": "error", "msg": self.error,
                           "net": True,
                           "hint": "请确认设备已开机、与电脑在同一网段；"
                                   "网络扫描不需要接 USB 线。"})
            self.done.set()
            return

        self.bytes = r.get("bytes", 0)
        self.t_scan = r.get("seconds", round(time.time() - self.t0, 2))
        if not self._finalize():
            self.state = "error"
            self.error = ("收到 %d 字节但图像解码失败" % self.bytes)
            self.emit({"type": "error", "msg": self.error, "net": True,
                       "hint": "可再扫一次；若反复失败请把打印机断电重启。"})
            self.done.set()
            return

        self.state = "done"
        self.emit({"type": "done", "pct": 100.0,
                   "W": self.image.width, "H": self.image.height,
                   "bytes": self.bytes, "elapsed": round(self.t_scan, 2),
                   "retried": r.get("retried", 0), "net": True})
        self.done.set()

    def _finalize(self):
        if not HAS_PIL:
            log("没有 Pillow，网络扫描结果无法进入画廊")
        if not self.jpeg_path or not os.path.exists(self.jpeg_path):
            return False
        try:
            with open(self.jpeg_path, "rb") as f:
                raw = f.read()
            if not raw.startswith(b"\xff\xd8"):
                log("网络扫描结果不是 JPEG:", raw[:8].hex())
                return False
            self.bytes = len(raw)
            if not HAS_PIL:
                return True
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
                self.image = im.copy()
            t = self.image.copy()
            t.thumbnail((190, 260), Image.BILINEAR)
            if t.mode not in ("RGB", "L"):
                t = t.convert("RGB")
            buf = io.BytesIO()
            t.save(buf, "JPEG", quality=80)
            self.thumb = buf.getvalue()
            return True
        except Exception as e:                      # noqa: BLE001
            log("网络扫描结果解码失败:", e)
            return False

    def cancel(self):
        self._stop = True
        # 会话线程会在下一轮轮询时退出；canon_scan_net 的 finally 会补发 EndJob
        # 释放作业

    def save(self, fmt="png", path=None):
        if self.image is None:
            return None, "没有可保存的图像"
        path, err = save_image(self.image, fmt, path)
        if not err:
            self.saved_path = path
        return path, err


def new_net_task(dev, opts, kind="scan"):
    t = NetScanTask(dev, opts, kind)
    with TASKS_LOCK:
        TASKS[t.id] = t
    return t


class Gallery:
    """已扫页面（内存 + 缓存目录），支持导出多页 PDF / 分批保存。"""

    def __init__(self):
        self.pages = []
        self.session = os.path.join(CACHEDIR, time.strftime("%Y%m%d-%H%M%S"))
        self.lock = threading.Lock()

    def add(self, task, dpi=300):
        if task.image is None:
            return None
        return self.add_image(task.image, task.thumb, task.saved_path, dpi)

    def add_image(self, image, thumb=None, path=None, dpi=300):
        if image is None:
            return None
        if thumb is None:
            try:
                t = image.copy()
                t.thumbnail((190, 260), Image.BILINEAR)
                if t.mode not in ("RGB", "L"):
                    t = t.convert("RGB")
                buf = io.BytesIO()
                t.save(buf, "JPEG", quality=80)
                thumb = buf.getvalue()
            except Exception as e:                      # noqa: BLE001
                log("补缩略图失败:", e)
        with self.lock:
            self.pages.append({"img": image, "thumb": thumb,
                               "path": path, "dpi": dpi,
                               "w": image.width, "h": image.height,
                               "mode": image.mode})
            return len(self.pages) - 1

    def clear(self):
        with self.lock:
            self.pages = []

    def save_pdf(self, path=None):
        if not HAS_PIL or not self.pages:
            return None, "没有页面可导出"
        os.makedirs(OUTDIR, exist_ok=True)
        if not path:
            base = time.strftime("扫描-%Y%m%d-%H%M%S")
            path = os.path.join(OUTDIR, base + ".pdf")
            n = 1
            while os.path.exists(path):
                path = os.path.join(OUTDIR, "%s(%d).pdf" % (base, n))
                n += 1
        imgs = []
        for p in self.pages:
            im = p["img"]
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            imgs.append(im)
        imgs[0].save(path, "PDF", save_all=True, append_images=imgs[1:],
                     resolution=float(self.pages[0].get("dpi", 300) or 300))
        return path, ""


def _proj_seg(proj, lo, hi, min_len, thr):
    """把一维投影拆成「连续超阈值」段，返回 [(start,end), ...] 按能量降序。"""
    segs = []
    s = None
    for i in range(lo, hi):
        if proj[i] > thr:
            if s is None:
                s = i
        elif s is not None:
            if i - s >= min_len:
                segs.append((s, i))
            s = None
    if s is not None and hi - s >= min_len:
        segs.append((s, hi))
    segs.sort(key=lambda b: sum(proj[b[0]:b[1]]), reverse=True)
    return segs


def detect_card(im):
    """在整幅玻璃扫描图中自动识别并裁出身份证。

    不假设盖板颜色/明暗/摆放角度：把原图降采样后，每个像素取「边缘能量 + 到
    背景参考色的色差」作为"内容量"，做行/列投影；枚举所有合格的行段×列段组合，
    按「面积占比合理 + 宽高比最接近身份证(≈1.59)」打分，取最优矩形。
    失败返回 None（调用方降级为整幅）。

    返回原图坐标 (left, top, right, bottom)。
    """
    orig_w, orig_h = im.size
    sw = 480
    sh = max(2, int(round(orig_h * sw / float(orig_w))))
    small = im.convert("RGB").resize((sw, sh), Image.BILINEAR)
    edges = list(small.convert("L").filter(ImageFilter.FIND_EDGES).getdata())

    # 背景参考色 = 各通道中位亮度（不假设白色/黑色盖板）
    px = list(small.getdata())
    chans = list(zip(*px))
    bg = tuple(sorted(c)[len(c) // 2] for c in chans)

    rows = [0] * sh
    cols = [0] * sw
    # 排除最外圈：FIND_EDGES 对越界像素的填充会在边界行/列产生伪高边缘，
    # 而扫描内容（身份证）不会贴在玻璃边缘
    for y in range(1, sh - 1):
        base = y * sw
        s = 0
        for x in range(1, sw - 1):
            r, g, b = px[base + x]
            dist = abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2])
            if edges[base + x] + dist * 0.45 > 34:
                s += 1
                cols[x] += 1
        rows[y] = s

    def segs(proj, n, bias=0.16):
        m = max(proj) or 1
        thr = max(20, int(m * bias))
        return _proj_seg(proj, 0, n, max(10, int(n * 0.03)), thr)

    rs = segs(rows, sh)
    cs = segs(cols, sw)
    if not rs or not cs:
        return None

    total = sw * sh
    AR0 = 85.6 / 54.0                        # 身份证参考宽高比 ≈1.586
    best = None
    best_score = None
    for (ry0, ry1), (cx0, cx1) in ((r, c) for r in rs for c in cs):
        bw, bh = cx1 - cx0, ry1 - ry0
        if bw < 20 or bh < 20:
            continue
        area = bw * bh
        if area < total * 0.006 or area > total * 0.55:
            continue
        ar = bw / float(bh)
        if not (0.3 <= ar <= 3.2):
            continue
        energy = sum(rows[ry0:ry1]) + sum(cols[cx0:cx1])
        score = energy / float(total) - 6.0 * abs(ar - AR0)
        if best_score is None or score > best_score:
            best_score = score
            best = (ry0, ry1, cx0, cx1)
    if best is None:
        return None
    ry0, ry1, cx0, cx1 = best

    bw, bh = cx1 - cx0, ry1 - ry0
    padx = max(2, int(bw * 0.04))
    pady = max(2, int(bh * 0.04))
    sx, sy = orig_w / float(sw), orig_h / float(sh)
    left = max(0, int(round((cx0 - padx) * sx)))
    top = max(0, int(round((ry0 - pady) * sy)))
    right = min(orig_w, int(round((cx1 + padx) * sx)))
    bottom = min(orig_h, int(round((ry1 + pady) * sy)))
    if right - left < 8 or bottom - top < 8:
        return None
    return (left, top, right, bottom)


def compose_id_page(front, back, layout="tb", dpi=300,
                    box_front=None, box_back=None):
    """身份证正反面合成一页：A4 白底，上下（或左右）各放一张抠出的卡片。
    每面优先用调用方传入的检测框（box_front/box_back），未传则自动检测；
    检测失败时该面降级为整幅缩放（contain + 居中）。"""
    w = int(round(210.0 / 25.4 * dpi))
    h = int(round(297.0 / 25.4 * dpi))
    canvas = Image.new("RGB", (w, h), "white")
    if layout not in ("lr",):
        layout = "tb"
    for i, src in enumerate((front, back)):
        box = (box_front, box_back)[i]
        if box is None:
            box = detect_card(src)
        im = src if box is None else src.crop(box)
        if im.mode != "RGB":
            im = im.convert("RGB")
        if layout == "tb":
            bh = h // 2
            im.thumbnail((w, bh), Image.BILINEAR)
            canvas.paste(im, ((w - im.width) // 2, bh * i + (bh - im.height) // 2))
        else:
            bw = w // 2
            im.thumbnail((bw, h), Image.BILINEAR)
            canvas.paste(im, (bw * i + (bw - im.width) // 2, (h - im.height) // 2))
    return canvas


GALLERY = Gallery()


MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".woff2": "font/woff2",
        ".json": "application/json; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "canon-webd/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("CANON_WEB_VERBOSE"):
            log("%s - %s" % (self.address_string(), fmt % args))

    def _send(self, code, body=b"", ctype="application/octet-stream",
              extra=None, head_only=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:                           # noqa: BLE001
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        if p.startswith("/api/"):
            return self.api_get(p, q)

        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[7:]
        full = os.path.normpath(os.path.join(WEBROOT, rel))
        if not full.startswith(WEBROOT) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        return self._send(200, data, MIME.get(ext, "application/octet-stream"))

    def api_get(self, p, q):
        if p == "/api/info":
            return self._json({
                "ok": True, "pil": HAS_PIL,
                "outdir": OUTDIR,
                "version": "1.0",
                "net": HAVE_NET,
                "netscan": HAVE_NETSCAN,
                "fix": os.path.exists(FIX_BIN),
            })
        if p == "/api/devices":
            force = q.get("force", ["0"])[0] == "1"
            devs, meta = all_devices(force)
            return self._json({"devices": devs, "net": meta})
        if p == "/api/discover":
            # 主动发现：不等缓存，立刻扫一遍网络
            devs, meta = all_devices(force=True)
            return self._json({"devices": devs, "net": meta})
        if p == "/api/caps":
            dev = (q.get("d") or [""])[0]
            if not dev:
                return self._json({"error": "缺少设备参数"}, 400)
            if dev.startswith("net:"):
                # 网络设备没有 SANE 参数表；把设备信息交给前端显示即可
                net, _meta = NETMGR.list()
                info = next((x for x in net if x["id"] == dev), None)
                return self._json({"device": dev, "options": {},
                                   "papers": PAPERS, "net": True,
                                   "info": info})
            return self._json(DEVMGR.caps(dev))
        if p == "/api/live":
            tid = (q.get("t") or [""])[0]
            t = TASKS.get(tid)
            if not t or not t.preview_jpeg:
                return self._send(404, b"", "image/jpeg")
            return self._send(200, t.preview_jpeg, "image/jpeg",
                              {"Cache-Control": "no-store"})
        if p == "/api/thumb":
            tid = (q.get("t") or [""])[0]
            t = TASKS.get(tid)
            if t and t.thumb:
                return self._send(200, t.thumb, "image/jpeg")
            idx = (q.get("i") or [""])[0]
            if idx.isdigit() and int(idx) < len(GALLERY.pages):
                th = GALLERY.pages[int(idx)]["thumb"]
                if th:
                    return self._send(200, th, "image/jpeg")
            return self._send(404, b"", "image/jpeg")
        if p == "/api/card-check":
            tid = (q.get("t") or [""])[0]
            t = TASKS.get(tid)
            if not t or t.image is None:
                return self._json({"error": "没有可分析的图像"}, 400)
            if not HAS_PIL:
                return self._json({"error": "本机缺少 Pillow"}, 400)
            try:
                box = detect_card(t.image)
            except Exception as e:                      # noqa: BLE001
                return self._json({"error": "分析失败：%s" % e}, 500)
            return self._json({"ok": bool(box),
                               "W": t.image.width, "H": t.image.height})
        if p == "/api/result":
            tid = (q.get("t") or [""])[0]
            idx = (q.get("i") or [""])[0]
            maxw = (q.get("w") or ["0"])[0]
            img = None
            if tid and TASKS.get(tid):
                img = TASKS[tid].image
            elif idx.isdigit() and int(idx) < len(GALLERY.pages):
                img = GALLERY.pages[int(idx)]["img"]
            if img is None:
                return self._send(404, b"", "image/png")
            # 600dpi 彩色可到 26MB，按显示需要缩一档，省内存与带宽
            try:
                mw = int(maxw)
            except Exception:                       # noqa: BLE001
                mw = 0
            if HAS_PIL and 0 < mw < img.width:
                r = mw / float(img.width)
                img = img.resize((mw, max(1, int(img.height * r))),
                                 Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return self._send(200, buf.getvalue(), "image/png")
        if p == "/api/pages":
            with GALLERY.lock:
                return self._json({
                    "pages": [{"i": i, "w": pg["w"], "h": pg["h"],
                               "path": pg["path"]}
                              for i, pg in enumerate(GALLERY.pages)],
                    "outdir": OUTDIR,
                })
        if p == "/api/events":
            tid = (q.get("t") or [""])[0]
            t = TASKS.get(tid)
            if not t:
                return self._json({"error": "未知任务"}, 404)
            # 已终态的任务：事件流在彼时已断，浏览器会自动重连；这里回 204 让
            # 浏览器按 SSE 规范停止重连 —— 否则每次重连都触发一次
            # EventSource.onerror，被 app.js 误判成「与后端连接中断」。
            if t.done.is_set():
                return self._send(204, b"", "text/plain")
            return self.sse(t)
        return self._json({"error": "未知接口"}, 404)

    def sse(self, task):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": canon-studio event stream\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = task.q.get(timeout=0.7)
                except queue.Empty:
                    if task.done.is_set() and task.q.empty():
                        break
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(ev, ensure_ascii=False)
                self.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
                self.wfile.flush()
                if ev.get("type") in ("done", "error", "cancelled"):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        return None

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        b = self._body()

        if p == "/api/scan":
            dev = b.get("device")
            if not dev:
                devs, _m = all_devices()
                dev = devs[0]["id"] if devs else None
            if not dev:
                return self._json({"error": "未找到扫描仪"}, 400)
            if dev.startswith("net:"):
                # 网络设备：走 CHMP/HTTP 通道（/canon/ij/command2/port3）取图
                if not HAVE_NETSCAN:
                    return self._json({
                        "error": "这是网络扫描仪：本机缺少网络扫描模块"
                                 "（canon_scan_net.py），暂不能扫描。"
                                 "当前请接 USB 线扫描。",
                        "net": True,
                    }, 409)
                t = new_net_task(dev, b.get("opts") or {},
                                 b.get("kind") or "scan")
                t.start()
                return self._json({"task": t.id, "device": dev, "net": True})
            t = new_task(dev, b.get("opts") or {}, b.get("kind") or "scan")
            t.start()
            return self._json({"task": t.id, "device": dev})

        if p == "/api/cancel":
            t = TASKS.get(b.get("task") or "")
            if not t:
                return self._json({"error": "未知任务"}, 404)
            t.cancel()
            return self._json({"ok": True})

        if p == "/api/fix":
            ok, out = fix_device(bool(b.get("reset")))
            return self._json({"ok": ok, "out": out})

        if p == "/api/add-page":
            t = TASKS.get(b.get("task") or "")
            if not t or t.image is None:
                return self._json({"error": "该任务没有可用图像"}, 400)
            # 兜底：done 已先 _finalize，这里防将来其它路径再引入同类竞态
            #（图像已就绪但 thumb 尚未编出）
            if not t.thumb and HAS_PIL:
                try:
                    th = t.image.copy()
                    th.thumbnail((190, 260), Image.BILINEAR)
                    if th.mode not in ("RGB", "L"):
                        th = th.convert("RGB")
                    buf = io.BytesIO()
                    th.save(buf, "JPEG", quality=80)
                    t.thumb = buf.getvalue()
                except Exception as e:              # noqa: BLE001
                    log("补缩略图失败:", e)
            try:
                dpi = int(float(t.opts.get("resolution") or 300))
            except Exception:                       # noqa: BLE001
                dpi = 300
            i = GALLERY.add(t, dpi)
            return self._json({"ok": True, "index": i,
                               "count": len(GALLERY.pages)})

        if p == "/api/merge-id":
            # 身份证模式：正面 task + 反面 task 合成一页 A4 入库
            front = TASKS.get(b.get("front") or "")
            back = TASKS.get(b.get("back") or "")
            if front is None or back is None or front.image is None \
                    or back.image is None:
                return self._json({"error": "缺正面或反面图像"}, 400)
            if not HAS_PIL:
                return self._json({"error": "本机缺少 Pillow，无法合成"}, 400)
            box_f = detect_card(front.image)
            box_b = detect_card(back.image)
            if box_f is None or box_b is None:
                return self._json({
                    "error": "稿台上未检测到身份证或卡片，"
                             "请放置证件后重扫（本次数据已丢弃）"}, 422)
            try:
                img = compose_id_page(front.image, back.image,
                                      b.get("layout") or "tb",
                                      box_front=box_f, box_back=box_b)
            except Exception as e:                      # noqa: BLE001
                log("身份证合成失败:", e)
                return self._json({"error": "合成失败：%s" % e}, 500)
            i = GALLERY.add_image(img, dpi=300)
            return self._json({"ok": True, "index": i,
                               "W": img.width, "H": img.height,
                               "count": len(GALLERY.pages)})

        if p == "/api/save":
            fmt = (b.get("format") or "png").lower()
            if fmt == "pdf":
                path, err = GALLERY.save_pdf(b.get("path"))
            else:
                t = TASKS.get(b.get("task") or "")
                img = None
                if t is not None and t.image is not None:
                    img = t.image
                else:
                    idx = b.get("page")
                    if isinstance(idx, int) and 0 <= idx < len(GALLERY.pages):
                        img = GALLERY.pages[idx]["img"]
                if img is None:
                    return self._json({"error": "没有可保存的图像"}, 400)
                path, err = save_image(img, fmt, b.get("path"))
            if err:
                return self._json({"error": err}, 400)
            return self._json({"ok": True, "path": path})

        if p == "/api/drop-task":
            # 丢弃一次扫描数据（身份证模式检测不到证件时）：释放图像内存
            tid = b.get("task") or ""
            with TASKS_LOCK:
                t = TASKS.pop(tid, None)
            if t is not None:
                try:
                    t.cancel()
                except Exception:                       # noqa: BLE001
                    pass
            return self._json({"ok": True})

        if p == "/api/clear":
            GALLERY.clear()
            return self._json({"ok": True})

        if p == "/api/del-page":
            i = b.get("index")
            with GALLERY.lock:
                if not isinstance(i, int) or i < 0 or i >= len(GALLERY.pages):
                    return self._json({"error": "页索引越界"}, 400)
                GALLERY.pages.pop(i)
                return self._json({"ok": True, "count": len(GALLERY.pages)})

        if p == "/api/reveal":
            path = b.get("path") or OUTDIR
            target = os.path.dirname(path) if os.path.isfile(path) else path
            for cmd in (["xdg-open", target],
                        ["dbus-send", "--session",
                         "--dest=org.freedesktop.FileManager1",
                         "--type=method_call", "/org/freedesktop/FileManager1",
                         "org.freedesktop.FileManager1.ShowItems",
                         "array:string:file://" + path, "string:"]):
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    return self._json({"ok": True})
                except Exception:                   # noqa: BLE001
                    continue
            return self._json({"error": "无法打开文件管理器"}, 500)

        if p == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None

        return self._json({"error": "未知接口"}, 404)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Canon 扫描 Web 后端")
    ap.add_argument("-p", "--port", type=int, default=0,
                    help="监听端口（0=自动分配）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--print-url", action="store_true",
                    help="启动后在 stdout 打印 URL 并保持前台")
    args = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    url = "http://%s:%d/" % (args.host, port)
    log("就绪 %s  (Pillow=%s, 输出目录=%s)" % (url, HAS_PIL, OUTDIR))
    print(url, flush=True)

    # 预热：开机就把「设备枚举 + 网络发现」跑一遍丢进缓存，免得界面首屏干等
    # scanimage 的 BJNP 发现超时（实测 ~7s）
    def _warm():
        try:
            all_devices(False)
            _d, m = NETMGR.list()
            log("预热完成（发现方式 %s）" % m.get("method", "?"))
        except Exception as e:                      # noqa: BLE001
            log("预热失败（不影响使用）:", e)

    threading.Thread(target=_warm, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
