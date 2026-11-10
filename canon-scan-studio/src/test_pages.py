#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""多页扫描 + PDF 导出自检：连扫两页 → 合成 PDF → 校验文件。"""

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("CANON_BASE", "http://127.0.0.1:8899")
OUTDIR = os.path.expanduser("~/扫描")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.read()


def post(path, obj):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def scan_once(dev, opts, label):
    r = post("/api/scan", {"device": dev, "opts": opts})
    task = r.get("task")
    if not task:
        print(label, "启动失败:", r)
        return None
    last = {}
    req = urllib.request.Request(BASE + "/api/events?t=" + task)
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            if ev["type"] == "retry":
                print("  ", label, "重试:", ev.get("msg"))
            if ev["type"] in ("done", "error", "cancelled"):
                last = ev
                break
    if last.get("type") != "done":
        print(label, "失败:", last)
        return None
    print("  %s 完成 %dx%d %d 字节 用时 %ss"
          % (label, last["W"], last["H"], last["bytes"], last["elapsed"]))
    add = post("/api/add-page", {"task": task})
    print("  ", label, "入列表:", add)
    return task


def main():
    devs = json.loads(get("/api/devices")).get("devices") or []
    if not devs:
        print("无设备")
        return 1
    dev = devs[0]["id"]
    post("/api/clear", {})
    print("清空页列表，预复位:", post("/api/fix", {}))

    # 低分辨率两页，省时间（预热期固定 ~7.5s，与 dpi 无关）
    opts = {"resolution": "75", "mode": "Color", "source": "Flatbed",
            "width": 210, "height": 297, "left": 0, "top": 0}
    for i in (1, 2):
        if not scan_once(dev, opts, "第%d页" % i):
            return 1

    pages = json.loads(get("/api/pages"))
    print("页列表:", pages)

    pdf = post("/api/save", {"format": "pdf"})
    print("导出 PDF:", pdf)
    if pdf.get("error"):
        return 1
    p = pdf["path"]
    with open(p, "rb") as f:
        head = f.read(8)
    size = os.path.getsize(p)
    print("PDF 文件: %s  %d 字节  头=%r  %s"
          % (p, size, head, "有效" if head.startswith(b"%PDF") else "无效!"))
    # 数一下 PDF 里的页对象
    with open(p, "rb") as f:
        blob = f.read()
    n = blob.count(b"/Type /Page") + blob.count(b"/Type/Page")
    print("PDF 内 /Page 出现次数:", n)

    # 再测单页图片导出
    one = post("/api/save", {"format": "png", "page": 0})
    print("导出单页 PNG:", one)
    return 0


if __name__ == "__main__":
    sys.exit(main())
