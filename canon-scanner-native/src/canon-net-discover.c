/*
 * canon-net-discover —— 网络上的 Canon 扫描仪主动发现
 *
 * 这类 IVEC 一体机的网络扫描不在 SANE 支持范围内（pixma 后端只认 BJNP:8612 /
 * MFNP:8610，而实测这些端口全关，mDNS 里也没有 _scanner._tcp），
 * scanimage -L 因此永远 "No scanners were identified"。原厂 scangearmp2 用
 * Canon 私有网络栈 CNNET2_CreateInstance → OptSetting → Search →
 * EnumSearchInfo 做发现，底层是 SNMP v1（community "canon_admin"，
 * Canon 私有 OID，IANA 企业号 1602）。本文件自带该 SNMP v1 实现，零库依赖。
 *
 * 单播优先、广播兜底：广播包（目的 255.255.255.255）的回包与 conntrack 元组
 * 不匹配，会被判成 NEW 而 reject（本机 firewalld 实测），所以主路径是单播
 * （ARP 表里的邻居 + 本机 /24），广播只在单播全空时试一次。
 *
 * 扫描服务通道地址取自原厂头文件 include/libcnnet3_url.h：
 *   #define CNNET3_URL_SCAN  "/canon/ij/command2/port3"
 * 探测方式是发一条设备不认识的 IVEC operation：设备回 NG/NotSupportOperation，
 * 但 HTTP 200 且响应体是 ivec XML —— 不改变设备状态，又能证明这台机器在
 * 网络上提供扫描服务。
 *
 * 许可：GPL-2.0-or-later
 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <ifaddrs.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>
#include <stdarg.h>

#define CNNET3_URL_SCAN "/canon/ij/command2/port3"

#define MAX_DEV   64
#define MAX_CANDS 1024

/* Canon 私有 MIB：IANA 企业号 1602。community 对齐原厂驱动。 */
#define SNMP_COMMUNITY "canon_admin"
#define OID_MODEL    "1.3.6.1.4.1.1602.1.1.1.1.0"     /* ASCII 机型名 */
#define OID_SERIAL   "1.3.6.1.4.1.1602.1.2.1.8.1.3.1.1" /* ASCII 序列号 */
#define OID_DEVTYPE  "1.3.6.1.4.1.1602.1.1.1.10.0"   /* INTEGER */
#define OID_CONNMODE "1.3.6.1.4.1.1602.1.3.1.12.0"   /* INTEGER */
#define OID_MAC      "1.3.6.1.4.1.1602.1.3.1.13.0"    /* 6 字节二进制 */

/* conn_mode 取值：原厂 libcnnet2_type.h 里 currentConnectMode_ 的注释 */
static const char *conn_mode_name(int m)
{
    switch (m) {
    case 3: return "有线 LAN";
    case 4: return "无线直连";
    case 5: return "无线基础架构";
    case 6: return "无线 AP 模式";
    case 7: return "Wi-Fi Direct";
    case 8: return "Wi-Fi Direct 服务";
    default: return "";
    }
}

typedef struct {
    char ip[16];
    char mac[13];        /* 12 位十六进制，无分隔符 */
    char serial[64];
    char model[64];
    int  device_type;
    int  conn_mode;
} cn_dev;

/* BER / SNMP v1：只做 GetRequest / GetResponse 里用得到的部分（定长整数、OID、
 * OctetString、Sequence）。没有可绕的分支，全部在固定缓冲上做边界检查。 */

static void trim(char *s);

static size_t ber_len(unsigned char *p, size_t n)
{
    if (n < 0x80) { p[0] = (unsigned char)n; return 1; }
    if (n <= 0xff) { p[0] = 0x81; p[1] = (unsigned char)n; return 2; }
    p[0] = 0x82; p[1] = (unsigned char)(n >> 8); p[2] = (unsigned char)n;
    return 3;
}

/* 写一个 TLV；返回写入字节数。cap 不足返回 0（调用方直接放弃） */
static size_t ber_tlv(unsigned char *p, size_t cap, unsigned char tag,
                      const unsigned char *val, size_t vlen)
{
    unsigned char l[3];
    size_t ln = ber_len(l, vlen), need = 1 + ln + vlen;
    if (cap < need) return 0;
    p[0] = tag;
    memcpy(p + 1, l, ln);
    memcpy(p + 1 + ln, val, vlen);
    return need;
}

static size_t ber_int(unsigned char *p, size_t cap, long v)
{
    unsigned char b[8];
    size_t n = 0;
    if (v == 0) { b[n++] = 0; return ber_tlv(p, cap, 0x02, b, n); }
    unsigned long u = (unsigned long)v;
    unsigned char tmp[8];
    while (u) { tmp[n++] = (unsigned char)(u & 0xff); u >>= 8; }
    size_t k = 0;
    for (size_t i = n; i > 0; i--)
        if (tmp[i - 1] & 0x80) { b[k++] = 0; break; }
    for (size_t i = n; i > 0; i--) b[k++] = tmp[i - 1];
    return ber_tlv(p, cap, 0x02, b, k);
}

static size_t ber_oid(unsigned char *p, size_t cap, const char *dots)
{
    unsigned char body[64];
    size_t n = 0;
    char tmp[96];
    snprintf(tmp, sizeof(tmp), "%s", dots);

    char *save = NULL, *tok = strtok_r(tmp, ".", &save);
    long parts[48];
    int np = 0;
    while (tok && np < 48) { parts[np++] = strtol(tok, NULL, 10); tok = strtok_r(NULL, ".", &save); }
    if (np < 2) return 0;

    body[n++] = (unsigned char)(parts[0] * 40 + parts[1]);
    for (int i = 2; i < np; i++) {
        long v = parts[i];
        unsigned char sub[10];
        int k = 0;
        sub[k++] = (unsigned char)(v & 0x7f);
        v >>= 7;
        while (v) { sub[k++] = (unsigned char)((v & 0x7f) | 0x80); v >>= 7; }
        if (n + (size_t)k > sizeof(body)) return 0;
        while (k) body[n++] = sub[--k];
    }
    return ber_tlv(p, cap, 0x06, body, n);
}

/* 读一个 TLV 头；成功返回 0 并给出 value 起点、value 长度、下一 TLV 位置；
 * 越界/长度非法返回 -1 */
static int ber_tlv_read(const unsigned char *buf, size_t len, size_t pos,
                        unsigned char *tag, const unsigned char **val,
                        size_t *vlen, size_t *next)
{
    if (pos + 2 > len) return -1;
    *tag = buf[pos++];
    size_t l = buf[pos++];
    if (l & 0x80) {
        int n = (int)(l & 0x7f);
        if (n == 0 || n > 4 || pos + (size_t)n > len) return -1;
        l = 0;
        for (int i = 0; i < n; i++) l = (l << 8) | buf[pos++];
    }
    if (pos + l > len) return -1;
    *val = buf + pos;
    *vlen = l;
    *next = pos + l;
    return 0;
}

/* 造一条 SNMP v1 GetRequest，一次查 5 个 Canon OID。返回报文长度 */
static size_t snmp_build(unsigned char *out, size_t cap, long reqid)
{
    static const char *oids[5];
    oids[0] = OID_MODEL; oids[1] = OID_SERIAL; oids[2] = OID_MAC;
    oids[3] = OID_DEVTYPE; oids[4] = OID_CONNMODE;

    unsigned char vbs[512];
    size_t vn = 0;
    for (int i = 0; i < 5; i++) {
        unsigned char vb[96];
        size_t bn = 0;
        bn += ber_oid(vb + bn, sizeof(vb) - bn, oids[i]);
        bn += ber_tlv(vb + bn, sizeof(vb) - bn, 0x05, (const unsigned char *)"", 0);
        if (!bn) return 0;
        vn += ber_tlv(vbs + vn, sizeof(vbs) - vn, 0x30, vb, bn);
        if (!vn) return 0;
    }

    unsigned char pdu[640], body[640];
    size_t pn = 0, bnn = 0;
    pn += ber_int(pdu + pn, sizeof(pdu) - pn, reqid);
    pn += ber_int(pdu + pn, sizeof(pdu) - pn, 0);      /* error-status */
    pn += ber_int(pdu + pn, sizeof(pdu) - pn, 0);      /* error-index */
    pn += ber_tlv(pdu + pn, sizeof(pdu) - pn, 0x30, vbs, vn);
    if (!pn) return 0;

    bnn += ber_tlv(body + bnn, sizeof(body) - bnn, 0x02,
                   (const unsigned char *)"\x00", 1);   /* version = 0 (v1) */
    bnn += ber_tlv(body + bnn, sizeof(body) - bnn, 0x04,
                   (const unsigned char *)SNMP_COMMUNITY,
                   sizeof(SNMP_COMMUNITY) - 1);
    bnn += ber_tlv(body + bnn, sizeof(body) - bnn, 0xA0, pdu, pn);
    if (!bnn) return 0;

    return ber_tlv(out, cap, 0x30, body, bnn);
}

/* 安全追加：只在还放得下时写，并始终推进 k */
static size_t app(char *buf, size_t cap, size_t k, const char *fmt, ...)
{
    if (k >= cap) return k;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf + k, cap - k, fmt, ap);
    va_end(ap);
    if (n < 0) return k;
    return (size_t)n < cap - k ? k + (size_t)n : cap - 1;
}

/* OID 字节串 → 规范化点分串，用来在 varbind 里定位我们要的字段。
 * Canon 的 OID 约 30 字符，96 字节绰绰有余。 */
static void oid_to_dots(const unsigned char *o, size_t n, char *out, size_t cap)
{
    if (n == 0 || cap == 0) { if (cap) out[0] = '\0'; return; }
    size_t k = app(out, cap, 0, "%u.%u", o[0] / 40, o[0] % 40);
    unsigned long v = 0;
    for (size_t i = 1; i < n; i++) {
        v = (v << 7) | (o[i] & 0x7f);
        if (!(o[i] & 0x80)) {
            k = app(out, cap, k, ".%lu", v);
            v = 0;
        }
    }
    out[k] = '\0';
}

/* 整数型 varbind 的值（Canon 的 INTEGER 都小端序，见真机响应） */
static long varint(const unsigned char *v, size_t n)
{
    long r = 0;
    for (size_t i = 0; i < n && i < 4; i++) r |= (long)v[i] << (8 * i);
    return r;
}

/* 解析 GetResponse，把 5 个字段填进 d。返回 0 表示这条回包确实是给我们的 */
static int snmp_parse(const unsigned char *buf, size_t len, cn_dev *d)
{
    unsigned char tag;
    /* 每层容器必须用各自独立的指针/长度变量：ber_tlv_read 的出参指向当前层
     * 的 value，复用同一组变量会把「上一层的 value」当容器继续读。 */
    const unsigned char *seq, *sk, *pdu, *vbl, *one, *ob, *vb;
    size_t seql, skl, pdul, vbll, onl, obl, vbl2, nx;

    if (ber_tlv_read(buf, len, 0, &tag, &seq, &seql, &nx) || tag != 0x30)
        return -1;
    size_t p = 0;
    if (ber_tlv_read(seq, seql, p, &tag, &sk, &skl, &p)) return -1;   /* version */
    if (tag != 0x02) return -1;
    if (ber_tlv_read(seq, seql, p, &tag, &sk, &skl, &nx)) return -1;  /* community */
    if (tag != 0x04) return -1;
    p = nx;
    if (ber_tlv_read(seq, seql, p, &tag, &pdu, &pdul, &nx) || tag != 0xA2)
        return -1;                                                        /* GetResponse */

    size_t q = 0;
    for (int i = 0; i < 3; i++) {                    /* request-id / err / err-idx */
        if (ber_tlv_read(pdu, pdul, q, &tag, &sk, &skl, &nx)) return -1;
        q = nx;
    }
    if (ber_tlv_read(pdu, pdul, q, &tag, &vbl, &vbll, &nx) || tag != 0x30)
        return -1;                                                        /* varbind list */

    int hit = 0;
    char od[96];
    q = 0;
    while (q < vbll) {
        if (ber_tlv_read(vbl, vbll, q, &tag, &one, &onl, &nx) || tag != 0x30)
            break;
        q = nx;
        size_t r = 0;
        if (ber_tlv_read(one, onl, 0, &tag, &ob, &obl, &r) || tag != 0x06) continue;
        if (ber_tlv_read(one, onl, r, &tag, &vb, &vbl2, &r)) continue;
        oid_to_dots(ob, obl, od, sizeof(od));
        if (!strcmp(od, OID_MODEL)) {
            size_t k = vbl2 < sizeof(d->model) - 1 ? vbl2 : sizeof(d->model) - 1;
            memcpy(d->model, vb, k); d->model[k] = '\0';
            trim(d->model); hit = 1;
        } else if (!strcmp(od, OID_SERIAL)) {
            size_t k = vbl2 < sizeof(d->serial) - 1 ? vbl2 : sizeof(d->serial) - 1;
            memcpy(d->serial, vb, k); d->serial[k] = '\0';
            trim(d->serial); hit = 1;
        } else if (!strcmp(od, OID_MAC) && vbl2 == 6) {
            snprintf(d->mac, sizeof(d->mac), "%02X%02X%02X%02X%02X%02X",
                     vb[0], vb[1], vb[2], vb[3], vb[4], vb[5]);
            hit = 1;
        } else if (!strcmp(od, OID_DEVTYPE)) {
            d->device_type = (int)varint(vb, vbl2); hit = 1;
        } else if (!strcmp(od, OID_CONNMODE)) {
            d->conn_mode = (int)varint(vb, vbl2); hit = 1;
        }
    }
    return hit ? 0 : -1;
}

/* 去掉首尾空白（SNMP 字符串值常见尾随空格/换行） */
static void trim(char *s)
{
    size_t n = strlen(s);
    while (n && (s[n-1] == ' ' || s[n-1] == '\t' || s[n-1] == '\r' ||
                 s[n-1] == '\n' || s[n-1] == '\0')) s[--n] = '\0';
    char *p = s;
    while (*p == ' ' || *p == '\t') p++;
    if (p != s) memmove(s, p, strlen(p) + 1);
}

/* 候选清单 = ARP 表里已知的邻居 + 本机各 /24 网段。
 *
 * ARP 邻居排前面：打印机通常已在邻居表里，先问它比盲扫 254 个地址快得多，也不
 * 依赖网段掩码猜得对；/24 全量扫是兜底（打印机刚上电时不在表里）。前缀大于
 * /24（网段更小）也一并扫，超过 254 个地址的网段（/16 之类，比如 docker0）
 * 直接跳过，否则白等几秒。 */
static int build_cands(char (*ips)[16], int max)
{
    int n = 0;

    FILE *f = fopen("/proc/net/arp", "r");
    if (f) {
        char line[256];
        if (fgets(line, sizeof(line), f)) {          /* 跳过表头 */
            while (fgets(line, sizeof(line), f) && n < max) {
                char ip[64] = "", hwtype[32] = "", flags[32] = "";
                if (sscanf(line, "%63s %31s %31s", ip, hwtype, flags) != 3)
                    continue;
                if (!strcmp(flags, "0x0"))            /* 条目不完整 */
                    continue;
                if (!strncmp(ip, "127.", 4) || !strncmp(ip, "0.", 2))
                    continue;
                snprintf(ips[n++], 16, "%s", ip);
            }
        }
        fclose(f);
    }

    struct ifaddrs *ifa = NULL, *it;
    if (getifaddrs(&ifa) == 0) {
        for (it = ifa; it && n < max; it = it->ifa_next) {
            if (!it->ifa_addr || it->ifa_addr->sa_family != AF_INET)
                continue;
            if (!it->ifa_netmask) continue;
            struct sockaddr_in *nm = (struct sockaddr_in *)it->ifa_netmask;
            uint32_t mask = ntohl(nm->sin_addr.s_addr);
            int plen = 0;
            for (uint32_t t = mask; t & 0x80000000u; t <<= 1) plen++;
            if (plen < 24)                             /* 网段太大，盲扫不划算 */
                continue;
            struct sockaddr_in *sa = (struct sockaddr_in *)it->ifa_addr;
            uint32_t net = ntohl(sa->sin_addr.s_addr) & mask;
            unsigned hosts = 1u << (32 - plen);       /* /24 → 256 */
            unsigned last = (plen == 24) ? 254u : hosts - 1u;  /* /24 留广播位 */
            for (unsigned h = 1; h <= last && n < max; h++) {
                struct in_addr ia;
                ia.s_addr = htonl(net | h);
                snprintf(ips[n++], 16, "%s", inet_ntoa(ia));
            }
        }
        freeifaddrs(ifa);
    }

    /* 去重（ARP 邻居通常也在 /24 里） */
    int w = 0;
    for (int i = 0; i < n; i++) {
        int dup = 0;
        for (int j = 0; j < w; j++)
            if (!strcmp(ips[i], ips[j])) { dup = 1; break; }
        if (!dup) memcpy(ips[w++], ips[i], 16);
    }
    return w;
}

static long now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

/* 收一轮回包，解析后并入 devs（同一个 IP 只留第一条）。返回新增台数 */
static int drain(int fd, cn_dev *devs, int max, int *seen_n, char seen[][16],
                 int idle_ms)
{
    int added = 0;
    long deadline = now_ms() + idle_ms;
    for (;;) {
        long left = deadline - now_ms();
        if (left <= 0) break;
        struct pollfd pf = { fd, POLLIN, 0 };
        int pr = poll(&pf, 1, (int)left);
        if (pr <= 0) break;
        unsigned char buf[2048];
        struct sockaddr_in from;
        socklen_t fl = sizeof(from);
        ssize_t r = recvfrom(fd, buf, sizeof(buf), 0,
                             (struct sockaddr *)&from, &fl);
        if (r <= 0) break;
        char ip[16];
        if (!inet_ntop(AF_INET, &from.sin_addr, ip, sizeof(ip))) continue;
        int dup = 0;
        for (int i = 0; i < *seen_n; i++)
            if (!strcmp(seen[i], ip)) { dup = 1; break; }
        if (dup) continue;
        cn_dev d;
        memset(&d, 0, sizeof(d));
        if (snmp_parse(buf, (size_t)r, &d) != 0) continue;
        if (*seen_n < max) snprintf(seen[(*seen_n)++], 16, "%s", ip);
        if (added < max) { devs[added++] = d; snprintf(devs[added - 1].ip, 16, "%s", ip); }
    }
    return added;
}

/* SNMP v1 单播扫描。发现台数。 */
static int cn_discover(cn_dev *devs, int max, int tmo_ms, int *sent,
                       int *cands, int verbose)
{
    static char ips[MAX_CANDS][16];
    static char seen[MAX_DEV][16];
    int nc = build_cands(ips, MAX_CANDS);
    int ns = 0, n = 0;

    *cands = nc;
    if (nc == 0) {
        if (verbose) fprintf(stderr, "[canon-net-discover] 找不到本机 /24 网段\n");
        return 0;
    }

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) return 0;
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_BROADCAST, &one, sizeof(one));

    unsigned char pkt[1024];
    size_t plen = snmp_build(pkt, sizeof(pkt), 1);
    if (!plen) { close(fd); return 0; }

    /* ---- 单播优先 ---- */
    for (int i = 0; i < nc; i++) {
        struct sockaddr_in sa;
        memset(&sa, 0, sizeof(sa));
        sa.sin_family = AF_INET;
        sa.sin_port = htons(161);
        if (inet_pton(AF_INET, ips[i], &sa.sin_addr) != 1) continue;
        if (sendto(fd, pkt, plen, 0, (struct sockaddr *)&sa, sizeof(sa)) >= 0)
            (*sent)++;
    }
    n = drain(fd, devs, max, &ns, seen, tmo_ms);
    if (verbose)
        fprintf(stderr, "[canon-net-discover] 单播 %d/%d 个候选，发现 %d 台\n",
                *sent, nc, n);

    /* ---- 广播兜底：单播全空才试 ----
     * 本机带 conntrack 的防火墙会把广播回包判成 NEW 而拒掉（实测 firewalld
     * 如此），所以这里只是「万一猜错了网段」的最后机会。 */
    if (n == 0) {
        struct sockaddr_in sa;
        memset(&sa, 0, sizeof(sa));
        sa.sin_family = AF_INET;
        sa.sin_port = htons(161);
        sa.sin_addr.s_addr = htonl(INADDR_BROADCAST);
        if (sendto(fd, pkt, plen, 0, (struct sockaddr *)&sa, sizeof(sa)) >= 0) {
            int got = drain(fd, devs, max, &ns, seen, 600);
            if (verbose && got)
                fprintf(stderr, "[canon-net-discover] 广播兜底补到 %d 台\n", got);
            n += got;
        }
    }

    close(fd);
    return n;
}

/* 等 fd 可读；返回 0 可读，-1 超时/出错 */
static int wait_readable(int fd, int tmo_ms)
{
    fd_set rf;
    struct timeval tv;
    FD_ZERO(&rf);
    FD_SET(fd, &rf);
    tv.tv_sec = tmo_ms / 1000;
    tv.tv_usec = (tmo_ms % 1000) * 1000;
    return select(fd + 1, &rf, NULL, NULL, &tv) > 0 ? 0 : -1;
}

/* 读满一行（到 \n） */
static int read_line(int fd, char *buf, size_t cap, int tmo_ms)
{
    size_t n = 0;
    while (n + 1 < cap) {
        char c;
        if (wait_readable(fd, tmo_ms) != 0) return -1;
        ssize_t r = recv(fd, &c, 1, 0);
        if (r <= 0) return -1;
        buf[n++] = c;
        if (c == '\n') break;
    }
    buf[n] = '\0';
    return (int)n;
}

/* 把剩下的响应体尽量读进来（chunked 或定长都适用）。
 *
 * CHMP 的响应体在 GET 上、可能是 chunked。这里不做分块重组，而是把原始字节流
 * 收下来，再在流里直接找 XML —— 分块头（"15e\r\n"）只出现在段与段之间，不会
 * 插进 XML 内部，所以直接搜字符串既简单又稳。
 * 收束条件：连续 idle_ms 毫秒没有新数据，或缓冲满，或收到 0 长度块。 */
static int read_body_raw(int fd, char *out, size_t cap, int idle_ms)
{
    size_t total = 0;
    for (;;) {
        if (total + 1 >= cap) break;
        if (wait_readable(fd, idle_ms) != 0) break;      /* 静默 → 读完 */
        ssize_t r = recv(fd, out + total, cap - 1 - total, 0);
        if (r <= 0) break;
        total += (size_t)r;
        out[total] = '\0';
        if (strstr(out, "<ivec:response_detail>")) break;
    }
    out[total] = '\0';
    return (int)total;
}

/* 从响应头里取某个字段（大小写不敏感），找不到返回 NULL */
static const char *hdr_get(char *hdrs, const char *name)
{
    static char val[128];
    size_t nl = strlen(name);
    char *p = hdrs;
    while (p && *p) {
        char *e = strchr(p, '\n');
        if (e) *e = '\0';
        if (strncasecmp(p, name, nl) == 0 && p[nl] == ':') {
            const char *v = p + nl + 1;
            while (*v == ' ' || *v == '\t') v++;
            snprintf(val, sizeof(val), "%s", v);
            size_t l = strlen(val);
            while (l && (val[l-1] == '\r' || val[l-1] == '\n' ||
                         val[l-1] == ' ')) val[--l] = '\0';
            if (e) *e = '\n';
            return val;
        }
        if (e) *e = '\n';
        p = e ? e + 1 : NULL;
    }
    return NULL;
}

typedef struct {
    int  ok;
    int  http_status;
    char detail[192];
    char chmp[32];
} probe_t;

/* 读响应头（含末尾空行）。返回头字节数；-1 表示什么都没读到 */
static int read_headers(int fd, char *buf, size_t cap, int tmo_ms)
{
    size_t hl = 0;
    while (hl + 2 < cap) {
        if (read_line(fd, buf + hl, cap - hl, tmo_ms) < 0)
            return hl ? (int)hl : -1;
        size_t l = strlen(buf + hl);
        hl += l;
        if (l == 2 && buf[hl - 2] == '\r') break;    /* 空行 \r\n → 头结束 */
        if (l <= 1) break;                           /* 只有 \n，或 EOF */
    }
    buf[hl] = '\0';
    return (int)hl;
}

/* 在 /canon/ij/command2/port3 上发一条设备不认识的 IVEC 命令。
 *
 * CHMP 语义：POST 带命令拿 200 空 ack，再 GET 同一 URL 取响应体（chunked）。
 * 命令用设备不认识的 operation，设备只会回 NG/NotSupportOperation —— 不是为了
 * 执行，而是为了证明「这台机器在网络上提供扫描服务」，因此不改变设备任何状态。 */
static void probe_scan_channel(const char *ip, int tmo_ms, probe_t *P)
{
    static const char *BODY =
        "<?xml version=\"1.0\" encoding=\"utf-8\" ?>"
        "<cmd xmlns:ivec=\"http://www.canon.com/ns/cmd/2008/07/common/\">"
        "<ivec:contents><ivec:operation>CanonNetDiscoverProbe</ivec:operation>"
        "<ivec:param_set servicetype=\"scan\">"
        "<ivec:jobID>00000001</ivec:jobID></ivec:param_set>"
        "</ivec:contents></cmd>";
    char req[1024], hdrs[1024], body[4096];
    int fd;

    memset(P, 0, sizeof(*P));

    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        snprintf(P->detail, sizeof(P->detail), "socket: %s", strerror(errno));
        return;
    }
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons(80);
    if (inet_pton(AF_INET, ip, &sa.sin_addr) != 1) {
        snprintf(P->detail, sizeof(P->detail), "非法 IP");
        close(fd);
        return;
    }
    struct timeval tv = { tmo_ms / 1000, (tmo_ms % 1000) * 1000 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) != 0) {
        snprintf(P->detail, sizeof(P->detail), "连接失败: %s", strerror(errno));
        close(fd);
        return;
    }

    /* POST：带命令换 200 空 ack */
    int n = snprintf(req, sizeof(req),
        "POST %s HTTP/1.1\r\nHost: %s\r\nX-CHMP-Version: 1.0.0\r\n"
        "Content-Type: text/xml\r\nContent-Length: %zu\r\n\r\n%s",
        CNNET3_URL_SCAN, ip, strlen(BODY), BODY);
    if (send(fd, req, (size_t)n, 0) != n) {
        snprintf(P->detail, sizeof(P->detail), "写入失败");
        close(fd);
        return;
    }
    if (read_headers(fd, hdrs, sizeof(hdrs), tmo_ms) < 0) {
        snprintf(P->detail, sizeof(P->detail), "POST 无响应");
        close(fd);
        return;
    }
    const char *st = strstr(hdrs, "HTTP/1.");
    P->http_status = st ? atoi(st + 8) : 0;
    if (P->http_status != 200) {
        snprintf(P->detail, sizeof(P->detail), "POST HTTP %d", P->http_status);
        close(fd);
        return;
    }

    /* GET：同一连接取响应体（chunked） */
    n = snprintf(req, sizeof(req),
                 "GET %s HTTP/1.1\r\nHost: %s\r\nX-CHMP-Version: 1.0.0\r\n\r\n",
                 CNNET3_URL_SCAN, ip);
    if (send(fd, req, (size_t)n, 0) != n) {
        snprintf(P->detail, sizeof(P->detail), "GET 写入失败");
        close(fd);
        return;
    }
    if (read_headers(fd, hdrs, sizeof(hdrs), tmo_ms) < 0) {
        snprintf(P->detail, sizeof(P->detail), "GET 无响应");
        close(fd);
        return;
    }
    const char *cv = hdr_get(hdrs, "X-CHMP-Version");
    if (cv) snprintf(P->chmp, sizeof(P->chmp), "%s", cv);

    int bl = read_body_raw(fd, body, sizeof(body), 400);
    close(fd);

    if (bl > 0 && strstr(body, "<ivec:") &&
        strstr(body, "ivec:response_detail")) {
        const char *p2, *q;
        char r1[64] = "?", r2[64] = "?";
        p2 = strstr(body, "<ivec:response>");
        if (p2) {
            p2 += strlen("<ivec:response>");
            q = strstr(p2, "</ivec:response>");
            if (q && (size_t)(q - p2) < sizeof(r1))
                snprintf(r1, sizeof(r1), "%.*s", (int)(q - p2), p2);
        }
        p2 = strstr(body, "<ivec:response_detail>");
        if (p2) {
            p2 += strlen("<ivec:response_detail>");
            q = strstr(p2, "</ivec:response_detail>");
            if (q && (size_t)(q - p2) < sizeof(r2))
                snprintf(r2, sizeof(r2), "%.*s", (int)(q - p2), p2);
        }
        P->ok = 1;
        snprintf(P->detail, sizeof(P->detail), "%s / %s", r1, r2);
    } else {
        snprintf(P->detail, sizeof(P->detail), "非 IVEC 响应（%d 字节）", bl);
    }
}

static void json_str(const char *s)
{
    putchar('"');
    for (; s && *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') { putchar('\\'); putchar(c); }
        else if (c == '\n') fputs("\\n", stdout);
        else if (c == '\r') fputs("\\r", stdout);
        else if (c == '\t') fputs("\\t", stdout);
        else if (c < 0x20) printf("\\u%04x", c);
        else putchar(c);
    }
    putchar('"');
}

int main(int argc, char **argv)
{
    int tmo_ms = 3000, do_probe = 1, as_json = 0, i;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-t") || !strcmp(argv[i], "--timeout")) {
            if (++i >= argc) { fprintf(stderr, "-t 缺少参数\n"); return 2; }
            tmo_ms = atoi(argv[i]);
            if (tmo_ms < 200) tmo_ms = 200;
            if (tmo_ms > 60000) tmo_ms = 60000;
        } else if (!strcmp(argv[i], "--no-probe")) {
            do_probe = 0;
        } else if (!strcmp(argv[i], "--json")) {
            as_json = 1;
        } else if (!strcmp(argv[i], "-q") || !strcmp(argv[i], "--quiet")) {
            as_json = 1;
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            printf(
"用法: canon-net-discover [选项]\n"
"  在局域网上主动发现 Canon 一体机（扫描仪），并探明扫描服务通道。\n"
"\n"
"  自带实现 Canon 私有 SNMP v1 发现（community canon_admin，企业号 1602），\n"
"  单播扫描本网段、单播全空时再用广播兜底；无任何外部库依赖。\n"
"  扫描服务地址取自原厂头文件 libcnnet3_url.h 的 CNNET3_URL_SCAN。\n"
"\n"
"  -t, --timeout MS   发现超时，毫秒（默认 3000）\n"
"      --no-probe     跳过扫描通道探测（更快）\n"
"      --json         输出 JSON（供上层程序消费）\n"
"  -q, --quiet        等同 --json\n"
"  -h, --help         本帮助\n"
"\n"
"退出码: 0 至少发现一台   1 没发现   2 用法错误\n");
            return 0;
        } else {
            fprintf(stderr, "未知参数: %s（用 -h 看帮助）\n", argv[i]);
            return 2;
        }
    }

    static cn_dev devs[MAX_DEV];
    int sent = 0, cands = 0;
    int got = cn_discover(devs, MAX_DEV, tmo_ms, &sent, &cands, !as_json);

    if (as_json) {
        printf("{\"method\":\"snmp(canon)\",\"url_scan\":");
        json_str(CNNET3_URL_SCAN);
        printf(",\"count\":%d,\"devices\":[", got);
    } else {
        printf("发现方式: 内置 Canon SNMP v1（单播 %d/%d 个候选）\n", sent, cands);
        printf("扫描服务地址: %s（原厂 libcnnet3_url.h 的 CNNET3_URL_SCAN）\n",
               CNNET3_URL_SCAN);
        printf("找到 %d 台设备。\n", got);
    }

    for (i = 0; i < got; i++) {
        cn_dev *e = &devs[i];
        probe_t P;
        if (do_probe) probe_scan_channel(e->ip, 2500, &P);
        else memset(&P, 0, sizeof(P));

        if (as_json) {
            if (i) putchar(',');
            printf("{\"ip\":");          json_str(e->ip);
            printf(",\"mac\":");         json_str(e->mac);
            printf(",\"serial\":");      json_str(e->serial);
            printf(",\"model\":");       json_str(e->model);
            printf(",\"device_type\":%d", e->device_type);
            printf(",\"conn_mode\":%d,\"conn\":", e->conn_mode);
            json_str(conn_mode_name(e->conn_mode));
            printf(",\"device_id\":\"\"");
            printf(",\"scan_channel\":{\"url\":");
            json_str(CNNET3_URL_SCAN);
            printf(",\"ok\":%s,\"detail\":", P.ok ? "true" : "false");
            json_str(P.detail);
            printf(",\"chmp\":");        json_str(P.chmp);
            printf("}}");
        } else {
            printf("\n  [%d] %-16s %-24s %s\n", i, e->ip, e->model, e->mac);
            printf("      序列号 %s   连接 %s (mode %d)\n", e->serial,
                   conn_mode_name(e->conn_mode), e->conn_mode);
            if (do_probe)
                printf("      扫描通道 %s -> %s（%s）\n", CNNET3_URL_SCAN,
                       P.ok ? "可用" : "不可用", P.detail);
        }
    }
    if (as_json) printf("]}\n");
    return got > 0 ? 0 : 1;
}
