#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
canon_net —— 佳能 IJ 设备「网络主动发现」（对齐原厂 CNNET2_* 发现层：
CreateInstance → OptSetting 超时 → Search → EnumSearchInfo）

两级路径，逐级退化：驱动包原生 CLI `canon-net-discover`（canon-scanner-native 提供，
自带 Canon SNMP v1 实现、无外部库依赖）优先；工具没装时退回内置纯 Python SNMP v1
单播发现 —— 同 community、同 OID、同语义。

扫描通道探测：原厂把「扫描」服务绑在 /canon/ij/command2/port3（libcnnet3_url.h），
本模块用一条**不改变设备状态**的 IVEC 命令探测该通道是否存在。

只用 Python 标准库（socket / http.client / json / subprocess）。
"""

import errno
import http.client
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time

# Canon 私有 OID（IANA 企业号 1602），取自原厂驱动真机抓包
OID_MAC = "1.3.6.1.4.1.1602.1.3.1.13.0"     # 6 字节二进制
OID_SERIAL = "1.3.6.1.4.1.1602.1.2.1.8.1.3.1.1"  # ASCII
OID_MODEL = "1.3.6.1.4.1.1602.1.1.1.1.0"    # ASCII
OID_DEVTYPE = "1.3.6.1.4.1.1602.1.1.1.10.0"  # INTEGER
OID_CONNMODE = "1.3.6.1.4.1.1602.1.3.1.12.0"  # INTEGER
COMMUNITY = b"canon_admin"
OIDS = (OID_MODEL, OID_SERIAL, OID_MAC, OID_DEVTYPE, OID_CONNMODE)

# 原厂 CNNET2_SETTING_FLAGS
FLAG_DISCOVER_PRINTER_TIMEOUT_MILLIS = 4

# 原厂 libcnnet3_url.h
CNNET3_URL_SCAN = "/canon/ij/command2/port3"

# 连接模式（原厂 libcnnet2_type.h 注释）
CONN_MODE = {3: "有线 LAN", 4: "无线直连", 5: "无线基础架构",
             6: "无线 AP 模式", 7: "Wi-Fi Direct", 8: "Wi-Fi Direct 服务"}


def log(*a):
    print("[canon-net]", *a, file=sys.stderr, flush=True)


# 首选：驱动包的原生 CLI

CLI_NAMES = ("canon-net-discover",)


def discover_cli(timeout_ms=3000, probe=True):
    """与 canon-scanner-native 同一套原厂调用链（CNNET2_*），只是进程调用。
    返回 None 表示工具不存在或输出不可用，此时退回内置实现。
    """
    exe = None
    for n in CLI_NAMES:
        exe = shutil.which(n)
        if exe:
            break
    if not exe:
        for c in ("/usr/bin/canon-net-discover", "/usr/local/bin/canon-net-discover"):
            if os.path.exists(c):
                exe = c
                break
    if not exe:
        return None
    cmd = [exe, "-t", str(int(timeout_ms)), "--json"]
    if not probe:
        cmd.append("--no-probe")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_ms / 1000.0 + 8)
        if p.returncode not in (0, 1):
            log("canon-net-discover 退出码 %s：%s" % (p.returncode,
                                                     (p.stderr or "").strip()))
            return None
        d = json.loads(p.stdout)
    except Exception as e:                           # noqa: BLE001
        log("调用 canon-net-discover 失败：%r" % e)
        return None
    devs = []
    for x in d.get("devices", []):
        sc = x.get("scan_channel") or {}
        devs.append({
            "ip": x.get("ip", ""),
            "mac": x.get("mac", ""),
            "serial": x.get("serial", ""),
            "model": x.get("model", ""),
            "device_type": int(x.get("device_type") or 0),
            "conn_mode": int(x.get("conn_mode") or 0),
            "device_id": x.get("device_id", ""),
            "scan_channel": sc,
            "is_scanner": bool(sc.get("ok")),
            "source": "canon-net-discover(native)",
        })
    return {"devices": devs, "method": "canon-net-discover",
            "stats": {"found": len(devs), "exe": exe}}


# 兜底：内置 SNMP v1（与原生实现同 community / 同 OID / 同语义，可互为交叉验证）

def _ber_len(n):
    if n < 0x80:
        return bytes([n])
    if n <= 0xff:
        return bytes([0x81, n])
    return bytes([0x82, n >> 8, n & 0xff])


def _ber_tlv(tag, val):
    return bytes([tag]) + _ber_len(len(val)) + val


def _ber_int(v):
    if v == 0:
        return _ber_tlv(0x02, b"\x00")
    b = []
    while v:
        b.insert(0, v & 0xff)
        v >>= 8
    if b[0] & 0x80:
        b.insert(0, 0)
    return _ber_tlv(0x02, bytes(b))


def _ber_oid(dots):
    parts = [int(x) for x in dots.strip(".").split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for v in parts[2:]:
        tmp = [v & 0x7f]
        v >>= 7
        while v:
            tmp.insert(0, (v & 0x7f) | 0x80)
            v >>= 7
        out.extend(tmp)
    return _ber_tlv(0x06, bytes(out))


def snmp_get_packet(oids, community=COMMUNITY, reqid=1):
    vbs = b"".join(_ber_tlv(0x30, _ber_oid(o) + _ber_tlv(0x05, b""))
                   for o in oids)
    pdu = _ber_tlv(0xA0, _ber_int(reqid) + _ber_int(0) + _ber_int(0)
                   + _ber_tlv(0x30, vbs))
    return _ber_tlv(0x30, _ber_int(0) + _ber_tlv(0x04, community) + pdu)


def _ber_parse(buf, pos=0):
    """返回 (tag, value_bytes, next_pos)；value_bytes 对 constructed 类型为原文。"""
    tag = buf[pos]
    pos += 1
    l = buf[pos]
    pos += 1
    if l & 0x80:
        n = l & 0x7f
        l = int.from_bytes(buf[pos:pos + n], "big")
        pos += n
    return tag, buf[pos:pos + l], pos + l


def snmp_parse_response(buf):
    """解析 GetResponse，返回 {oid点分串: 原始值字节}。"""
    try:
        _, body, _ = _ber_parse(buf, 0)              # SEQUENCE
        pos = 0
        _, _, pos = _ber_parse(body, pos)            # version
        _, _, pos = _ber_parse(body, pos)            # community
        tag, pdu, _ = _ber_parse(body, pos)          # GetResponse
        if tag != 0xA2:
            return {}
        p = 0
        for _ in range(3):                           # req-id / err / err-idx
            _, _, p = _ber_parse(pdu, p)
        _, vbl, _ = _ber_parse(pdu, p)               # varbind list
    except Exception:                                # noqa: BLE001
        return {}
    out, q = {}, 0
    while q + 2 <= len(vbl):                         # 逐个 varbind，越界即停
        try:
            _, one, q = _ber_parse(vbl, q)
            r = 0
            _, ob, r = _ber_parse(one, r)            # OID
            _, vb, _ = _ber_parse(one, r)            # value
            out[_oid_to_dots(ob.hex())] = vb
        except Exception:                            # noqa: BLE001
            break
    return out


def _oid_to_dots(hexstr):
    if not hexstr:
        return ""
    b = bytes.fromhex(hexstr)
    parts = [b[0] // 40, b[0] % 40]
    i, v = 1, None
    for x in b[1:]:
        if v is None:
            v = 0
        v = (v << 7) | (x & 0x7f)
        if not x & 0x80:
            parts.append(v)
            v = None
    return ".".join(str(p) for p in parts)


def _cands():
    """候选地址：ARP 表已知主机 + 本机 /24 全部地址。"""
    ips = []
    try:
        with open("/proc/net/arp") as f:
            next(f)
            for line in f:
                p = line.split()
                if len(p) >= 4 and p[2] != "0x0":
                    ips.append(p[0])
    except Exception:                                # noqa: BLE001
        pass
    try:
        for line in subprocess.run(["ip", "-4", "-o", "addr", "show"],
                                   capture_output=True, text=True).stdout.splitlines():
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
            if not m:
                continue
            ip, plen = m.group(1), int(m.group(2))
            if plen == 24:
                base = ip.rsplit(".", 1)[0]
                ips.extend("%s.%d" % (base, i) for i in range(1, 255))
    except Exception:                                # noqa: BLE001
        pass
    seen, out = set(), []
    for ip in ips:
        if ip not in seen and not ip.startswith(("127.", "0.")):
            seen.add(ip)
            out.append(ip)
    return out


def snmp_unicast_sweep(timeout_ms=1200, extra_ips=()):
    """SNMP v1 单播扫描。返回 (设备列表, 统计)。

    为什么不用广播：广播包（目的 255.255.255.255）回包时源地址与 conntrack 元组
    不匹配，会被判成 NEW 而 reject（本机 firewalld 实测如此）；单播元组精确，回包正常。
    """
    ips = list(extra_ips) + _cands()
    pkt = snmp_get_packet(OIDS)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setblocking(False)
    sent = 0
    for ip in ips:
        try:
            s.sendto(pkt, (ip, 161))
            sent += 1
        except OSError:
            pass
    deadline = time.time() + timeout_ms / 1000.0
    found, seen = [], set()
    while time.time() < deadline:
        try:
            data, addr = s.recvfrom(4096)
        except BlockingIOError:
            time.sleep(0.02)
            continue
        except OSError:
            break
        ip = addr[0]
        if ip in seen:
            continue
        seen.add(ip)
        r = snmp_parse_response(data)
        if not r:
            continue
        by = r
        model = by.get(OID_MODEL, b"").decode("utf-8", "replace").strip()
        serial = by.get(OID_SERIAL, b"").decode("utf-8", "replace").strip()
        macb = by.get(OID_MAC, b"")
        mac = "-".join("%02X" % c for c in macb) if len(macb) == 6 else ""
        cm = by.get(OID_CONNMODE, b"")
        conn = int.from_bytes(cm, "big") if cm else 0
        dt = by.get(OID_DEVTYPE, b"")
        dtype = int.from_bytes(dt, "big") if dt else 0
        found.append({"ip": ip, "mac": mac.replace("-", ""),
                      "serial": serial, "model": model,
                      "device_type": dtype, "conn_mode": conn,
                      "device_id": "", "source": "snmp(builtin)"})
    s.close()
    return found, {"sent": sent, "replied": len(found), "candidates": len(ips)}


def discover_snmp(timeout_ms=1200):
    d, st = snmp_unicast_sweep(timeout_ms)
    return d, st


# 扫描通道探测

SCAN_PROBE = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<cmd xmlns:ivec="http://www.canon.com/ns/cmd/2008/07/common/">'
    '<ivec:contents><ivec:operation>CanonStudioProbe</ivec:operation>'
    '<ivec:param_set servicetype="scan">'
    '<ivec:jobID>00000001</ivec:jobID></ivec:param_set>'
    '</ivec:contents></cmd>'
).encode("utf-8")


def probe_scan_channel(ip, timeout=2.5, url=CNNET3_URL_SCAN):
    """探测「网络扫描通道」是否存在 —— 不改变设备任何状态。

    设备对未知 operation 会回 NG/NotSupportOperation，但 HTTP 200 且响应体是一段
    ivec XML —— 那就说明这台机器在网络上提供扫描服务。
    """
    res = {"ip": ip, "url": url, "ok": False, "detail": "", "chmp": ""}
    try:
        c = http.client.HTTPConnection(ip, 80, timeout=timeout)
        try:
            h = {"X-CHMP-Version": "1.0.0", "Content-Type": "text/xml",
                 "Content-Length": str(len(SCAN_PROBE))}
            c.request("POST", url, body=SCAN_PROBE, headers=h)
            r = c.getresponse()
            res["chmp"] = r.getheader("X-CHMP-Version", "") or ""
            r.read()
            if r.status != 200:
                res["detail"] = "POST HTTP %s" % r.status
                return res
            c.request("GET", url, headers={"X-CHMP-Version": "1.0.0"})
            r2 = c.getresponse()
            body = r2.read()
        finally:
            c.close()
    except Exception as e:                           # noqa: BLE001
        res["detail"] = "%s: %s" % (e.__class__.__name__, e)
        return res
    txt = body.decode("utf-8", "replace")
    if "<ivec:" in txt and "ivec:response_detail" in txt:
        m = re.search(r"<ivec:response>([^<]*)</ivec:response>", txt)
        d = re.search(r"<ivec:response_detail>([^<]*)"
                      r"</ivec:response_detail>", txt)
        res["ok"] = True
        res["detail"] = "%s / %s" % (m.group(1) if m else "?",
                                     d.group(1) if d else "?")
    else:
        res["detail"] = "非 IVEC 响应（%d 字节）" % len(body)
    return res


def discover(timeout_ms=3000, probe=True, probe_timeout=2.5):
    """主动发现网络上的 Canon 扫描仪。

    两级路径逐级退化：1) 驱动包原生 CLI `canon-net-discover`；2) 内置纯 Python
    SNMP v1 单播发现。同一套发现语义（同 community、同 OID），可互为交叉验证。
    返回 {"devices":[...], "method":..., "stats":{...}, "elapsed":...}
    """
    t0 = time.time()
    res = discover_cli(timeout_ms, probe)
    if res is None or not res["devices"]:
        s, st = discover_snmp(max(1200, timeout_ms // 2))
        if s or res is None:
            res = {"devices": s, "method": "snmp", "stats": st}
    devs, method, stats = res["devices"], res["method"], res["stats"]

    # 去重（同一台机器可能既在 ARP 表又被扫到）
    uniq, seen = [], set()
    for d in devs:
        key = d.get("ip") or d.get("mac") or d.get("model")
        if key and key not in seen:
            seen.add(key)
            uniq.append(d)
    if probe:
        for d in uniq:
            if d.get("scan_channel") or not d.get("ip"):
                continue
            p = probe_scan_channel(d["ip"], timeout=probe_timeout)
            d["scan_channel"] = p
            d["is_scanner"] = bool(p.get("ok"))
    return {"devices": uniq, "method": method, "stats": stats,
            "elapsed": round(time.time() - t0, 2)}


def net_device_entries(res=None, timeout_ms=3000):
    res = res or discover(timeout_ms)
    out = []
    for d in res["devices"]:
        ip = d.get("ip", "")
        model = d.get("model") or "Canon IJ 设备"
        out.append({
            "id": "net:" + ip,
            "vendor": "CANON",
            "model": model,
            "type": "网络扫描仪（IVEC/CHMP）" if d.get("is_scanner")
                    else "网络设备",
            "bus": "网络",
            "net": True,
            "ip": ip,
            "mac": d.get("mac", ""),
            "serial": d.get("serial", ""),
            "conn": CONN_MODE.get(int(d.get("conn_mode") or 0), ""),
            "scan_ready": bool(d.get("is_scanner")),
            "channel": CNNET3_URL_SCAN,
            "scan_channel": d.get("scan_channel"),
            "source": d.get("source", ""),
        })
    return out, res


def _main():
    import argparse
    ap = argparse.ArgumentParser(description="Canon 网络扫描仪主动发现")
    ap.add_argument("-t", "--timeout", type=int, default=3000,
                    help="发现超时（毫秒，默认 3000）")
    ap.add_argument("--no-probe", action="store_true",
                    help="不做扫描通道探测（更快）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()

    res = discover(a.timeout, probe=not a.no_probe)
    if a.json:
        import json
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print("发现方式: %s   耗时 %.2fs" % (res["method"], res["elapsed"]))
    if not res["devices"]:
        print("没找到网络上的 Canon 设备。")
        return 1
    for i, d in enumerate(res["devices"]):
        sc = d.get("scan_channel") or {}
        print("  [%d] %-16s %-22s %s" % (i, d.get("ip", "?"),
                                          d.get("model", "?"),
                                          d.get("mac", "")))
        print("      序列号 %s  连接 %s" % (d.get("serial", "-"),
                                            CONN_MODE.get(d.get("conn_mode"), "-")))
        print("      扫描通道 %s -> %s" % (CNNET3_URL_SCAN,
                                          "可用（%s）" % sc.get("detail")
                                          if sc.get("ok") else
                                          "不可用（%s）" % sc.get("detail")))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
