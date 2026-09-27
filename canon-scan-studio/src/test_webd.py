#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""canon_webd 的端到端自检：起扫描 → 消费 SSE → 取预览 → 保存 → 校验。"""

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("CANON_BASE", "http://127.0.0.1:8899")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.read()


def post(path, obj):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    dev = "pixma:04A9183B_8EFA5C"
    devs = json.loads(get("/api/devices")).get("devices") or []
    if devs:
        dev = devs[0]["id"]
    print("设备:", dev)

    caps = json.loads(get("/api/caps?d=" + dev))
    o = caps.get("options", {})
    print("mode:", o.get("--mode", {}).get("values"),
          "| res:", o.get("--resolution", {}).get("values"),
          "| source:", o.get("--source", {}).get("values"),
          "| -x range:", o.get("-x", {}).get("range"))

    # 先复位，确保干净起点
    print("预复位:", post("/api/fix", {}))

    opts = {"resolution": "150", "mode": "Color", "source": "Flatbed",
            "width": 210, "height": 297, "left": 0, "top": 0}
    t0 = time.time()
    r = post("/api/scan", {"device": dev, "opts": opts})
    task = r.get("task")
    print("任务:", task, "| 启动耗时 %.2fs" % (time.time() - t0))
    if not task:
        print("启动失败:", r)
        return 1

    # 消费 SSE
    events = []
    previews = 0
    first_prev_at = None
    req = urllib.request.Request(
        BASE + "/api/events?t=" + task,
        headers={"Accept": "text/event-stream"})
    t_start = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            events.append(ev)
            if ev["type"] == "progress":
                if ev.get("seq", 0) > 0 and first_prev_at is None:
                    first_prev_at = time.time() - t_start
                    # 拉一帧预览验证是有效 JPEG
                    try:
                        pj = get("/api/live?t=%s&n=%d" % (task, ev["seq"]))
                        previews += 1
                        ok_jpeg = pj[:2] == b"\xff\xd8"
                        print("首个预览帧: seq=%d %d 字节 JPEG=%s (t=%.2fs)"
                              % (ev["seq"], len(pj), ok_jpeg, first_prev_at))
                    except Exception as e:              # noqa: BLE001
                        print("预览获取失败:", e)
                if len(events) % 12 == 0:
                    print("  t=%5.2fs pct=%6.2f%% bytes=%9d/%9d rows=%d/%d"
                          % (time.time() - t_start, ev["pct"], ev["bytes"],
                             ev["total"], ev["rows"], ev["H"]))
            if ev["type"] in ("done", "error", "cancelled"):
                print("终止事件:", ev)
                break

    print("事件总数:", len(events), "| 预览帧:", previews)
    kinds = {}
    for e in events:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    print("事件分布:", kinds)

    done = [e for e in events if e["type"] == "done"]
    if not done:
        err = [e for e in events if e["type"] == "error"]
        print("扫描未完成", err[:1])
        return 1
    d = done[0]
    print("完成: %dx%d, %d 字节, 用时 %ss" % (d["W"], d["H"], d["bytes"],
                                             d["elapsed"]))

    # 取最终图并保存
    png = get("/api/result?t=%s&w=1600" % task)
    print("结果 PNG: %d 字节 (%s)" % (len(png), "有效" if png[:4] ==
                                     b"\x89PNG" else "无效!"))

    add = post("/api/add-page", {"task": task})
    print("入页列表:", add)
    sv = post("/api/save", {"format": "png", "page": add.get("index")})
    print("保存单页:", sv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
