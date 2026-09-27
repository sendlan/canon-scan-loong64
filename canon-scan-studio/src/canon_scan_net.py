#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""canon_scan_net.py —— PIXMA/MAXIFY 一体机的网络扫描实现（纯标准库）。
命令体逐字节对齐原厂 x86-64 驱动真机抓包。

通道：HTTP/CHMP，POST /canon/ij/command2/port3（port3=扫描服务），
每条命令 = POST（命令体，回 200 空 ack）+ GET（回 chunked 正文）。

一次 A4/300dpi/彩色 扫描序列：StartJob → ModeShift(1) → db20 → f320 →
d820(扫描参数) → d920 → da20×25 → dc20 → d420 轮询取图 → ef20 → EndJob。

取图响应每轮 body = 固定 16 字节包头（06 06 开头，末 2 字节=载荷长度，
无数据=0000）+ ≤10KiB JPEG 流；末包使 img 以 ffd9 结尾即整幅收完。

⚠️ chunked 分帧：读满一块后必须吃掉线上收尾 CRLF，否则结尾 0\r\n\r\n
残留到下一次 _response()，所有响应整体错位一位。
"""

import os
import re
import socket
import time

CHMP_URL = "/canon/ij/command2/port3"
CHMP_PORT = 80
CHMP_VERSION = "1.0.0"
CHMP_TIMEOUT = 20

# ---- 命令体（全部取自真机抓包，逐字节一致）--------------------------------
_IVEC_HEAD = ('<?xml version="1.0" encoding="utf-8" ?><cmd '
              'xmlns:ivec="http://www.canon.com/ns/cmd/2008/07/common/">')
_IVEC_HEAD_VCN = ('<?xml version="1.0" encoding="utf-8" ?><cmd '
                  'xmlns:ivec="http://www.canon.com/ns/cmd/2008/07/common/" '
                  'xmlns:vcn="http://www.canon.com/ns/cmd/2008/07/canon/">')
_PARAM_SET = ('<ivec:param_set servicetype="scan"><ivec:jobID>00000001</ivec:jobID>'
              '%s</ivec:param_set></ivec:contents></cmd>')

CMD_START_JOB = (_IVEC_HEAD + '<ivec:contents><ivec:operation>StartJob</ivec:operation>'
                 + (_PARAM_SET % '<ivec:bidi>1</ivec:bidi>')).encode()
CMD_MODE_SHIFT = (_IVEC_HEAD_VCN + '<ivec:contents><ivec:operation>VendorCmd</ivec:operation>'
                  + (_PARAM_SET % '<vcn:ijoperation>ModeShift</vcn:ijoperation>'
                                  '<vcn:ijmode>1</vcn:ijmode>')).encode()
CMD_END_JOB = (_IVEC_HEAD + '<ivec:contents><ivec:operation>EndJob</ivec:operation>'
               + (_PARAM_SET % '')).encode()

CMD_DB20 = bytes.fromhex('db200001000000000000000000000000')
CMD_F320 = bytes.fromhex('f3200000000000000000000000000010')
# ★扫描参数包（A4 / 300dpi / 彩色 / 平板）—— 72 字节，含宽高 0x09b0=2480、0x0db3=3507
CMD_D820 = bytes.fromhex(
    'd8200000000000000000000000000038'
    '0101010000000000812c812c0000000000000000000009b000000db308180001'
    'ff8400020100000000000000000100000100000000000081')
CMD_D920 = bytes.fromhex('d9200001000000000000000000000000')
CMD_DA20 = bytes.fromhex('da200000000000000000000000000008')
CMD_DC20 = bytes.fromhex('dc200000000000000000000000000008')
CMD_D420 = bytes.fromhex('d4200000000000000000000000004000')   # 末 2 字节 = 请求 16384 字节
CMD_EF20 = bytes.fromhex('ef200000000000000000000000000000')

CHUNK_SIZE = 0x4000      # 16384
DA_WAIT_ROUNDS = 25      # 与原厂一致
MAX_POLL = 600           # 取图轮询上限（原厂实际 ~139 次）

# 唤醒窗口：睡眠/断电后把扫描引擎带起来需要时间，首个 CHMP round 即唤醒设备
# （原厂起 CWakeup 线程轮询等待）。G3010 冷启动实测 ~73s 才就绪，其间会依次回
# DeviceBusy → 断连（TCP 栈重启）→ PCScanning；软拒绝（NG）与断连都按
# WAKE_BACKOFF 轮询，超过 WAKE_WINDOW 才算真失败。
WAKE_WINDOW = 90.0       # 唤醒总窗口（秒）：覆盖 PowerOff 冷启动 ~73s
WAKE_BACKOFF = 2.0       # 每次失败后等待秒数


class NetScanError(Exception):
    pass


class NetScanCancelled(NetScanError):
    """用户中止（与真失败区分开，便于上层回 cancelled 事件）。"""


def dechunk(body):
    """解析 HTTP chunked body，返回各分块数据（不含长度行）"""
    out = []
    i = 0
    n = len(body)
    while i < n:
        j = body.find(b'\r\n', i)
        if j < 0:
            break
        try:
            size = int(body[i:j].split(b';')[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        out.append(body[j + 2:j + 2 + size])
        i = j + 2 + size + 2
    return out


class ChmpSession(object):
    """一个到设备 port3 的 HTTP/CHMP 长连接"""

    def __init__(self, ip, timeout=CHMP_TIMEOUT, port=CHMP_PORT):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.sock = socket.create_connection((ip, port), timeout=15)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(timeout)
        self._buf = b''

    # ---------- 底层读写 ----------
    def _recv_until(self, marker):
        while marker not in self._buf:
            d = self.sock.recv(65536)
            if not d:
                raise NetScanError('设备断开连接')
            self._buf += d
        i = self._buf.index(marker) + len(marker)
        head, self._buf = self._buf[:i], self._buf[i:]
        return head

    def _read_exact(self, n):
        while len(self._buf) < n:
            d = self.sock.recv(max(65536, n - len(self._buf)))
            if not d:
                raise NetScanError('设备断开连接')
            self._buf += d
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_line(self):
        return self._recv_until(b'\r\n')

    def _eat_trailer_crlf(self):
        """吃掉 chunked 结尾 0\\r\\n 之后的最后一个 CRLF（可能还没到，短暂等一下）。"""
        if not self._buf.startswith(b'\r\n'):
            old = self.sock.gettimeout()
            try:
                self.sock.settimeout(1.0)
                d = self.sock.recv(65536)
                if d:
                    self._buf += d
            except Exception:
                pass
            finally:
                try:
                    self.sock.settimeout(old)
                except Exception:
                    pass
        if self._buf.startswith(b'\r\n'):
            self._buf = self._buf[2:]

    def _response(self):
        """读一个完整 HTTP 响应，返回 (status, headers_dict, body)

        body 保持【原始字节】——chunked 的分块帧不在这里剥掉，
        由调用方按需 dechunk()，这样既能拿 XML 也能拿带 16 字节包头的图像块。
        """
        head = self._recv_until(b'\r\n\r\n')
        lines = head[:-4].split(b'\r\n')
        status = lines[0].decode('latin1')
        hdrs = {}
        for ln in lines[1:]:
            if b':' in ln:
                k, v = ln.split(b':', 1)
                hdrs[k.strip().lower()] = v.strip()
        if b'transfer-encoding' in hdrs and b'chunked' in hdrs[b'transfer-encoding'].lower():
            # 设备的分帧是标准 HTTP/1.1：  <SIZE>\r\n<DATA>\r\n ... 0\r\n\r\n
            # ⚠️ 关键：读满一块数据后必须把【线上的那个 CRLF】吃掉，否则它会被下一轮
            #    _read_line() 当成"空长度行"而 break，把结尾的 0\r\n\r\n 留给下一次
            #    _response() —— 表现为 status='0'、body 空、之后所有响应整体错位一位。
            body = bytearray()
            while True:
                size_line = self._read_line()
                try:
                    size = int(size_line.strip().split(b';')[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self._eat_trailer_crlf()
                    break
                body += size_line                  # 保留长度行，dechunk() 才能还原分块
                body += self._read_exact(size)      # 保留数据
                body += b'\r\n'
                self._read_exact(2)                 # 吃掉该块在线上的结尾 CRLF
            return status, hdrs, bytes(body)
        clen = int(hdrs.get(b'content-length', b'0') or 0)
        body = self._read_exact(clen) if clen else b''
        return status, hdrs, body

    # ---------- 协议动作 ----------
    def post(self, body):
        req = (b'POST ' + CHMP_URL.encode() + b' HTTP/1.1\r\n'
               b'Content-Length: ' + str(len(body)).encode() + b'\r\n'
               b'X-CHMP-Timeout: ' + str(self.timeout).encode() + b'\r\n'
               b'Connection: Keep-Alive\r\n'
               b'Content-Type: application/octet-stream\r\n'
               b'Host: ' + self.ip.encode() + b'\r\n'
               b'X-CHMP-Version: ' + CHMP_VERSION.encode() + b'\r\n\r\n' + body)
        self.sock.sendall(req)
        return self._response()          # 200 空 ack

    def get(self):
        req = (b'GET ' + CHMP_URL.encode() + b' HTTP/1.1\r\n'
               b'Connection: Keep-Alive\r\n'
               b'Content-Type: application/octet-stream\r\n'
               b'Host: ' + self.ip.encode() + b'\r\n'
               b'X-CHMP-Version: ' + CHMP_VERSION.encode() + b'\r\n\r\n')
        self.sock.sendall(req)
        return self._response()

    def command(self, body):
        """一轮 = POST(命令) + GET(取响应体)，返回响应体"""
        self.post(body)
        _st, _h, _ack = (b'', {}, b'')
        st, hdrs, body_out = self.get()
        return body_out

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def _xml_ok(body):
    return b'<ivec:response>OK</ivec:response>' in body


def _xml_text(body):
    """把响应里的分块帧去掉，只留可读 XML（用于错误信息）。"""
    if not body:
        return '（响应为空 —— 设备未回内容）'
    parts = dechunk(body)
    txt = b''.join(parts) if parts else body
    t = txt.decode('utf8', 'replace')
    # 压掉换行，只留响应要点
    m = re.search(r'<ivec:operation>(.*?)</ivec:operation>.*?'
                  r'<ivec:response>(.*?)</ivec:response>.*?'
                  r'<ivec:response_detail>(.*?)</ivec:response_detail>', t, re.S)
    if m:
        return '%s = %s %s' % (m.group(1), m.group(2), m.group(3).strip() or '')
    return ' '.join(t.split())[:300]


def _packet_payload(body):
    """从一轮 d420 响应中取出图像载荷。

    每轮响应的**第一块固定是 16 字节包头**（`06 06` 开头，末 2 字节 = 本次载荷长度，
    如 `…3ff8` = 16376；无数据时为 `…0000`），其后才是 JPEG 流（设备按 10 KiB 切块：
    10240 + 6136）。所以剥掉首块即可。
    """
    chunks = dechunk(body)
    if chunks and len(chunks[0]) == 16 and chunks[0][:2] == b'\x06\x06':
        chunks = chunks[1:]
    return b''.join(chunks)


def scan(ip, out_path=None, progress=None, on_bytes=None, cancel_check=None,
         da_rounds=DA_WAIT_ROUNDS, poll_max=MAX_POLL):
    """对网络扫描仪执行一次扫描。

    progress(msg)  —— 阶段文字（给日志/CLI 用）
    on_bytes(n)    —— 每收到一包图像数据回调一次，n = 累计字节数（给进度条用）
    cancel_check() —— 返回 True 则中止（抛 NetScanCancelled）
    返回 dict(image=<路径>, bytes=<长度>, seconds=<耗时>, polls=<轮询轮数>)
    """
    t0 = time.time()

    def note(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    def got(n):
        if on_bytes:
            try:
                on_bytes(n)
            except Exception:
                pass

    def aborted():
        if cancel_check:
            try:
                return bool(cancel_check())
            except Exception:
                return False
        return False

    img = bytearray()
    polls = 0
    started = False
    retried = 0

    def _wake_loop():
        """唤醒轮询：连接 + EndJob 清场 + StartJob 直到扫描服务就绪；
        即原厂 CWakeup 线程 —— 冷启动前 StartJob 依次回 DeviceBusy/断连/
        PCScanning 再转 OK，超 WAKE_WINDOW 未就绪才算真失败。"""
        nonlocal retried, s, started
        dead = time.time() + WAKE_WINDOW
        while not started:
            if aborted():
                raise NetScanCancelled('用户取消')
            if s is None:
                try:
                    s = ChmpSession(ip)
                except (ConnectionError, OSError, socket.timeout) as e:
                    if time.time() >= dead:
                        raise NetScanError('设备唤醒窗口（%.0fs）内未能连接：%s'
                                           % (WAKE_WINDOW, e))
                    retried += 1
                    note('设备唤醒中（连接 %s），%.0fs 后重连'
                         % (e, WAKE_BACKOFF))
                    time.sleep(WAKE_BACKOFF)
                    continue
                # 清场：上一轮若被中断，设备可能仍挂着一个作业，先发 EndJob
                # 释放（失败无所谓，引导期可能还没就绪）
                try:
                    s.command(CMD_END_JOB)
                except Exception:
                    pass
            try:
                r = s.command(CMD_START_JOB)
            except (ConnectionError, OSError, socket.timeout, NetScanError) as e:
                # 引导期 TCP 栈会整体重启：现有的套接字作废，丢会话重连
                try:
                    s.close()
                except Exception:
                    pass
                s = None
                if time.time() >= dead:
                    raise NetScanError('设备唤醒窗口（%.0fs）内未就绪：%s'
                                       % (WAKE_WINDOW, e))
                retried += 1
                note('设备唤醒中（连接中断 %s），%.0fs 后重连'
                     % (e, WAKE_BACKOFF))
                time.sleep(WAKE_BACKOFF)
                continue
            if not _xml_ok(r):
                # 软拒绝：引擎还没转起来（NotSupportService / DeviceBusy /
                # PCScanning…），HTTP 层在但服务不可用 —— 继续等
                if time.time() >= dead:
                    raise NetScanError('StartJob 被拒（唤醒窗口内未就绪）：%s'
                                       % _xml_text(r))
                retried += 1
                note('设备唤醒中（StartJob 未就绪：%s），%.0fs 后重试'
                     % (_xml_text(r), WAKE_BACKOFF))
                time.sleep(WAKE_BACKOFF)
                continue
            started = True

    try:
        s = None
        _wake_loop()

        note('开始作业（StartJob 就绪）')
        note('切换到扫描模式（ModeShift）')
        r = s.command(CMD_MODE_SHIFT)
        if not _xml_ok(r):
            raise NetScanError('ModeShift 被拒：%s' % _xml_text(r))

        s.command(CMD_DB20)
        s.command(CMD_F320)
        note('下发扫描参数（A4／300dpi／彩色）')
        s.command(CMD_D820)
        s.command(CMD_D920)

        note('等待扫描仪就绪')
        for _ in range(da_rounds):
            s.command(CMD_DA20)
        s.command(CMD_DC20)

        note('读取图像数据')
        empty_run = 0
        while polls < poll_max:
            if aborted():
                raise NetScanCancelled('用户取消')
            polls += 1
            data = _packet_payload(s.command(CMD_D420))
            if data:
                img += data
                empty_run = 0
                note('已接收 %.1f KB' % (len(img) / 1024.0))
                got(len(img))
                if img.endswith(b'\xff\xd9'):
                    break                    # JPEG 收尾标记 = 整幅已收完
            else:
                empty_run += 1
                # 正常节奏：每 ~19 轮来一包数据；连续 30 轮空且已有数据 → 认为扫描结束
                if img and empty_run >= 30:
                    break

        note('结束扫描（EndScan）')
        try:
            s.command(CMD_EF20)
        except Exception:
            pass
    finally:
        if started:
            note('结束作业（EndJob）')
            try:
                s.command(CMD_END_JOB)
            except Exception:
                pass
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    if not img:
        raise NetScanError('未收到图像数据（设备可能忙或未就绪）')
    if not img.startswith(b'\xff\xd8'):
        raise NetScanError('图像数据不是 JPEG（首字节 %s）' % img[:4].hex())

    result = {'bytes': len(img), 'seconds': round(time.time() - t0, 1),
          'polls': polls, 'retried': retried}
    if out_path:
        d = os.path.dirname(out_path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(out_path, 'wb') as f:
            f.write(img)
        result['image'] = out_path
    return result


if __name__ == '__main__':
    import sys
    ip = sys.argv[1] if len(sys.argv) > 1 else '192.168.1.84'
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser('~/tmp-work/canon-net/native-net.jpg')
    r = scan(ip, out, progress=lambda m: sys.stdout.write('[%5.1fs] %s\n' % (time.time() % 1000, m)) or sys.stdout.flush())
    print(r)
