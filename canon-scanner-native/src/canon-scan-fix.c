/*
 * canon-scan-fix —— Canon IVEC 扫描通道挂起恢复工具
 *
 * 扫描被中途打断后（Ctrl-C / kill / 崩溃 / 传输中断），扫描通道的
 * EP 0x07(OUT)/0x88(IN) 停在 halt 不再回数据（打印机通道 interface 1 与
 * 中断端点 EP 0x89 仍正常），此后每次扫描都超时。恢复分三步：clear_halt
 * 三个扫描端点 → StartJob/EndJob 握手释放未结束作业 → 整机 USB 复位（-R）。
 *
 * 机型按 canon-mfp2-models.conf 的 USB 产品 ID 识别（不以单台机器硬编码）；
 * 表读不到时退回「任意 04a9 设备」，--strict 下改为拒绝。
 * 退出码：0 已恢复/无需恢复，1 设备无响应，2 用法错误或找不到设备，
 *         3 设备被其它进程占用。
 *
 * 许可：GPL-2.0-or-later
 */

/* 用到 readlink() 等 POSIX 接口。必须在任何头文件之前定义：否则用 -std=c11
 * 这类严格 ISO 模式编译时，glibc 会把 POSIX 声明藏起来（readlink 隐式声明）。 */
#define _GNU_SOURCE

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <libusb-1.0/libusb.h>

#define CANON_VID      0x04A9
#define EP_BULK_OUT    0x07
#define EP_BULK_IN     0x88
#define EP_INT_IN      0x89
#define IFACE_SCAN     0
#define TIMEOUT_MS     5000

/* 机型能力表：安装到 /usr/share/canon-scanner-native/，开发时在包目录 data/ 下。
 * 可用 CANON_MODEL_TABLE 覆盖。 */
#define TABLE_DEFAULT_1 "/usr/share/canon-scanner-native/canon-mfp2-models.conf"
#define TABLE_DEFAULT_2 "/usr/lib/canon-scanner-native/canon-mfp2-models.conf"

static int opt_quiet    = 0;
static int opt_release  = 1;
static int opt_reset    = 0;
static int opt_index    = 0;
static int opt_dry      = 0;
static int opt_attempts = 6;
static int opt_any      = 0;
static int opt_strict   = 0;

#define INFO(...)  do { if (!opt_quiet) fprintf(stderr, __VA_ARGS__); } while (0)
#define ERR(...)   do { fprintf(stderr, __VA_ARGS__); } while (0)

/* 机型能力表：每行「USB 产品 ID  能力码  机型名」。回答两个问题：这台 04a9
 * 是不是受支持机型（决定要不要做扫描通道复位），以及它叫什么。 */

#define MAX_MODELS 512

struct model {
    uint16_t    pid;
    unsigned    cap;
    char        name[64];
};

static struct model g_models[MAX_MODELS];
static int          g_nmodels = 0;
static int          g_have_table = 0;
static const char  *g_table_path = NULL;

static const char *find_table(void)
{
    static const char *cands[] = {
        NULL,                       /* CANON_MODEL_TABLE */
        TABLE_DEFAULT_1,
        TABLE_DEFAULT_2,
        "/usr/share/canon-scanner-native/canon-mfp2-models.conf",
        NULL                        /* 开发树：包目录 data/ */
    };
    const char *env = getenv("CANON_MODEL_TABLE");
    static char dev[512];            /* static：返回值指向它 */
    ssize_t n;
    char *slash;
    cands[0] = env;

    for (int i = 0; i < (int)(sizeof(cands) / sizeof(cands[0])); i++) {
        if (cands[i] && access(cands[i], R_OK) == 0)
            return cands[i];
    }
    /* 开发树兜底：<二进制所在目录>/data/ 与 <其上级>/data/（build/ 里的二进制）*/
    n = readlink("/proc/self/exe", dev, sizeof(dev) - 1);
    if (n > 0) {
        static const char *suffix[] = {
            "/data/canon-mfp2-models.conf",
            "/../data/canon-mfp2-models.conf",
        };
        size_t dirlen;
        dev[n] = '\0';
        slash = strrchr(dev, '/');
        if (slash) {
            *slash = '\0';
            dirlen = strlen(dev);
            for (int i = 0; i < (int)(sizeof(suffix) / sizeof(suffix[0])); i++) {
                strncpy(dev + dirlen, suffix[i], sizeof(dev) - dirlen - 1);
                dev[sizeof(dev) - 1] = '\0';
                if (access(dev, R_OK) == 0)
                    return dev;
            }
        }
    }
    return NULL;
}

/* 读表。返回条目数；-1 表示读不到。 */
static int load_models(void)
{
    const char *path = find_table();
    FILE *f;
    char line[256];

    if (!path)
        return -1;
    f = fopen(path, "r");
    if (!f)
        return -1;

    g_nmodels = 0;
    while (fgets(line, sizeof(line), f)) {
        char *p = line, *pid_s, *cap_s, *name;
        while (*p == ' ' || *p == '\t') p++;
        if (*p == '#' || *p == '\n' || *p == '\0')  continue;
        pid_s = p;
        while (*p && *p != ' ' && *p != '\t' && *p != '\n') p++;
        if (*p) *p++ = '\0';
        while (*p == ' ' || *p == '\t') p++;
        cap_s = p;
        while (*p && *p != ' ' && *p != '\t' && *p != '\n') p++;
        if (*p) *p++ = '\0';
        while (*p == ' ' || *p == '\t') p++;
        name = p;
        p = strpbrk(name, "\r\n");
        if (p) *p = '\0';
        if (!*pid_s || !*cap_s || g_nmodels >= MAX_MODELS)
            continue;
        g_models[g_nmodels].pid = (uint16_t)strtoul(pid_s, NULL, 16);
        g_models[g_nmodels].cap = (unsigned)strtoul(cap_s, NULL, 16);
        snprintf(g_models[g_nmodels].name, sizeof(g_models[g_nmodels].name),
                 "%s", name);
        g_nmodels++;
    }
    fclose(f);
    g_have_table = 1;
    g_table_path = path;
    return g_nmodels;
}

/* 查机型名；不在表里返回 NULL */
static const char *model_name(uint16_t pid)
{
    for (int i = 0; i < g_nmodels; i++)
        if (g_models[i].pid == pid)
            return g_models[i].name;
    return NULL;
}

/* 该机型是否支持平板扫描（能力码 bit0）—— 决定能不能做扫描通道复位 */
static int model_is_flatbed(uint16_t pid)
{
    for (int i = 0; i < g_nmodels; i++)
        if (g_models[i].pid == pid)
            return (g_models[i].cap & 0x0001u) != 0;
    return -1;                      /* 未知 */
}

static int do_bulk_in(libusb_device_handle *h, unsigned char *buf, int len,
                      int *got, unsigned int to)
{
    return libusb_bulk_transfer(h, EP_BULK_IN, buf, len, got, to);
}

static int do_bulk_out(libusb_device_handle *h, unsigned char *buf, int len,
                       unsigned int to)
{
    int got = 0;
    return libusb_bulk_transfer(h, EP_BULK_OUT, buf, len, &got, to);
}

/* 发一条 IVEC XML 命令，读回响应；返回 0 成功。resp 内写入响应正文 */
static int ivec_cmd(libusb_device_handle *h, const char *xml, size_t len,
                    char *resp, size_t respsz)
{
    int rc;

    rc = do_bulk_out(h, (unsigned char *)xml, (int)len, TIMEOUT_MS);
    if (rc != 0) {
        ERR("  写入命令失败: %s\n", libusb_strerror(rc));
        return -1;
    }

    unsigned char buf[8192];
    int got = 0;
    rc = do_bulk_in(h, buf, sizeof(buf) - 1, &got, TIMEOUT_MS);
    if (rc != 0) {
        ERR("  读取响应失败: %s\n", libusb_strerror(rc));
        return -1;
    }
    if (got <= 0)
        return -1;

    buf[got] = '\0';
    if (resp && respsz) {
        size_t n = (size_t)got < respsz - 1 ? (size_t)got : respsz - 1;
        memcpy(resp, buf, n);
        resp[n] = '\0';
    }
    return 0;
}

static int xml_job(const char *op, char *out, size_t outsz)
{
    return snprintf(out, outsz,
        "<?xml version=\"1.0\" encoding=\"utf-8\" ?>"
        "<cmd xmlns:ivec=\"http://www.canon.com/ns/cmd/2008/07/common/\">"
        "<ivec:contents><ivec:operation>%s</ivec:operation>"
        "<ivec:param_set servicetype=\"scan\">"
        "<ivec:jobID>00000001</ivec:jobID>%s</ivec:param_set>"
        "</ivec:contents></cmd>",
        op, strcmp(op, "StartJob") == 0 ? "<ivec:bidi>1</ivec:bidi>" : "");
}

/* 从响应里抽 <ivec:xxx>值</ivec:xxx> */
static int extract(const char *resp, const char *tag, char *out, size_t outsz)
{
    char open_[64], close_[64];
    snprintf(open_, sizeof(open_), "<ivec:%s>", tag);
    snprintf(close_, sizeof(close_), "</ivec:%s>", tag);

    const char *p = strstr(resp, open_);
    if (!p) { if (outsz) out[0] = '\0'; return -1; }
    p += strlen(open_);
    const char *q = strstr(p, close_);
    if (!q) { if (outsz) out[0] = '\0'; return -1; }
    size_t n = (size_t)(q - p);
    if (n >= outsz) n = outsz - 1;
    memcpy(out, p, n);
    out[n] = '\0';
    return 0;
}

int main(int argc, char **argv)
{
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-q") || !strcmp(argv[i], "--quiet"))
            opt_quiet = 1;
        else if (!strcmp(argv[i], "-r") || !strcmp(argv[i], "--release-job"))
            opt_release = 1;
        else if (!strcmp(argv[i], "--no-release"))
            opt_release = 0;
        else if (!strcmp(argv[i], "-R") || !strcmp(argv[i], "--reset"))
            opt_reset = 1;
        else if (!strcmp(argv[i], "-n") || !strcmp(argv[i], "--dry-run"))
            opt_dry = 1;
        else if (!strcmp(argv[i], "-i") || !strcmp(argv[i], "--index")) {
            if (++i >= argc) { ERR("--index 缺少参数\n"); return 2; }
            opt_index = atoi(argv[i]);
        } else if (!strcmp(argv[i], "-a") || !strcmp(argv[i], "--attempts")) {
            if (++i >= argc) { ERR("--attempts 缺少参数\n"); return 2; }
            opt_attempts = atoi(argv[i]);
            if (opt_attempts < 1) opt_attempts = 1;
            if (opt_attempts > 50) opt_attempts = 50;
        } else if (!strcmp(argv[i], "-A") || !strcmp(argv[i], "--any")) {
            opt_any = 1;
        } else if (!strcmp(argv[i], "-S") || !strcmp(argv[i], "--strict")) {
            opt_strict = 1;
        } else if (!strcmp(argv[i], "--list-models")) {
            int cnt = load_models();
            if (cnt < 0) {
                ERR("读不到机型表（可用 CANON_MODEL_TABLE 指定路径）\n");
                return 2;
            }
            printf("机型表 %s —— %d 项\n", g_table_path, cnt);
            for (int i = 0; i < cnt; i++)
                printf("  04a9:%04x  %04x  %s\n",
                       g_models[i].pid, g_models[i].cap, g_models[i].name);
            return 0;
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            printf(
"用法: canon-scan-fix [选项]\n"
"  Canon IVEC 扫描仪「扫描通道挂起」恢复工具\n\n"
"  -r, --release-job   额外发 EndJob 释放未结束的作业（默认开）\n"
"      --no-release    只清端点，不动作业状态\n"
"  -a, --attempts N    就绪握手最大次数（默认 6）\n"
"  -R, --reset         整机 USB 复位（比清端点重，会重新枚举设备）\n"
"  -i, --index N       选中第 N 个匹配设备（从 0 起，默认 0）\n"
"  -A, --any           也接受机型表里没有的 04a9 设备（新机型兜底）\n"
"  -S, --strict        机型表读不到就退出，不退回「任意 04a9」\n"
"      --list-models   打印内置机型表后退出\n"
"  -n, --dry-run       只看设备状态，不做任何写操作\n"
"  -q, --quiet         安静模式\n"
"  -h, --help          本帮助\n\n"
"机型识别走 canon-mfp2-models.conf（162 个已知机型），可用\n"
"CANON_MODEL_TABLE 指定其它路径。\n\n"
"退出码: 0 已恢复/无需恢复  1 设备无响应  2 参数或找不到设备  3 设备被占用\n");
            return 0;
        } else {
            ERR("未知参数: %s（用 -h 看帮助）\n", argv[i]);
            return 2;
        }
    }

    if (opt_dry) { opt_release = 0; opt_reset = 0; }

    int ntab = load_models();
    if (ntab < 0) {
        if (opt_strict) {
            ERR("读不到机型表，且指定了 --strict。\n"
                "请安装 canon-scanner-native 的数据文件，或用 CANON_MODEL_TABLE 指定。\n");
            return 2;
        }
        ERR("⚠ 读不到机型表，退回「任意 04a9 设备」模式"
            "（用 -S 可改为拒绝）\n");
    } else {
        INFO("[canon-scan-fix] 机型表 %s（%d 项）\n", g_table_path, ntab);
    }

    libusb_context *ctx = NULL;
    if (libusb_init(&ctx) != 0) {
        ERR("libusb 初始化失败\n");
        return 2;
    }

    /* 找设备：表内机型（含未登记的新机型，若开了 -A）优先，
       开了 -A 时再退到表外的 04a9 设备 */
    libusb_device **list = NULL;
    ssize_t n = libusb_get_device_list(ctx, &list);
    if (n < 0) { ERR("枚举 USB 设备失败\n"); libusb_exit(ctx); return 2; }

    libusb_device *found = NULL;
    uint16_t found_pid = 0;
    const char *found_name = NULL;
    int seen = 0, seen_all = 0, pass;
    for (pass = 0; pass < 2 && !found; pass++) {
        seen = 0;
        for (ssize_t i = 0; i < n; i++) {
            struct libusb_device_descriptor d;
            const char *nm;
            if (libusb_get_device_descriptor(list[i], &d) != 0)
                continue;
            if (d.idVendor != CANON_VID)
                continue;
            seen_all++;
            nm = model_name(d.idProduct);
            if (pass == 0 && !nm) continue;
            if (pass == 1 && nm) continue;
            if (seen++ == opt_index) {
                found = list[i];
                found_pid = d.idProduct;
                found_name = nm;
                break;
            }
        }
    }
    if (!found) {
        if (seen_all && opt_index)
            ERR("没找到 Canon 扫描仪（--index %d 超出范围，当前插着 %d 台 04a9 设备）\n",
                opt_index, seen_all);
        else if (seen_all)
            ERR("插着 %d 台 04a9 设备，但都不在机型表里 —— 用 -A 强制复位\n",
                seen_all);
        else
            ERR("没找到 Canon 扫描仪（机型表 %d 项全部未匹配）\n",
                g_nmodels);
        libusb_free_device_list(list, 1);
        libusb_exit(ctx);
        return 2;
    }

    uint8_t bus = libusb_get_bus_number(found);
    uint8_t addr = libusb_get_device_address(found);
    if (found_name)
        INFO("[canon-scan-fix] 设备 %04x:%04x (%s) @ bus %u addr %u\n",
             CANON_VID, found_pid, found_name, bus, addr);
    else
        INFO("[canon-scan-fix] 设备 %04x:%04x (表外机型) @ bus %u addr %u\n",
             CANON_VID, found_pid, bus, addr);
    if (model_is_flatbed(found_pid) == 0)
        INFO("  注意：该机型能力表未标平板扫描，扫描通道复位可能不适用\n");

    libusb_device_handle *h = NULL;
    int rc = libusb_open(found, &h);
    if (rc != 0) {
        ERR("打开设备失败: %s\n", libusb_strerror(rc));
        libusb_free_device_list(list, 1);
        libusb_exit(ctx);
        return 2;
    }

    int ret = 0;

    if (opt_dry) {
        INFO("  dry-run：只检查，不修改\n");
        goto out;
    }

    libusb_set_auto_detach_kernel_driver(h, 1);

    if (opt_reset) {
        INFO("  [1] 整机 USB 复位 ... ");
        rc = libusb_reset_device(h);
        INFO("%s\n", rc == 0 ? "OK" : libusb_strerror(rc));
        if (rc != 0) { ret = 1; goto out; }
        sleep(3);
    }

    INFO("  [%d] 声明扫描接口 %d ... ", opt_reset ? 2 : 1, IFACE_SCAN);
    rc = libusb_claim_interface(h, IFACE_SCAN);
    if (rc != 0) {
        INFO("失败: %s\n", libusb_strerror(rc));
        ERR("设备正被其它程序占用（扫描中？），请先退出那个程序。\n");
        ret = 3;
        goto out;
    }
    INFO("OK\n");

    /* 清扫描通道三个端点的 halt（bulk IN / bulk OUT / 中断，缺一不可） */
    const int eps[3] = { EP_BULK_IN, EP_BULK_OUT, EP_INT_IN };
    int cleared = 0;
    for (int i = 0; i < 3; i++) {
        rc = libusb_clear_halt(h, eps[i]);
        INFO("  [%d] clear_halt(EP 0x%02x) ... %s\n",
             (opt_reset ? 2 : 1) + i + 1, eps[i],
             rc == 0 ? "OK" : libusb_strerror(rc));
        if (rc == 0) cleared++;
    }
    if (cleared == 0) {
        ERR("端点清除全部失败，设备可能已掉线。\n");
        ret = 1;
        goto unclaim;
    }

    if (!opt_release) {
        INFO("[canon-scan-fix] 已清除端点 halt（未动作业状态）。\n");
        goto unclaim;
    }

    /* 就绪循环：反复「StartJob → 看响应 → EndJob」直到设备真正空闲。
     *
     * 为什么光 clear_halt 不够（实测）：扫描被中断后设备内部仍认为有一份 PC
     * 扫描作业在场，清端点不改变这个逻辑状态，下一次 StartJob 依然回
     *   <ivec:response>NG</ivec:response>
     *   <ivec:response_detail>PCScanning</ivec:response_detail>
     * 必须真正走一遍完整的 StartJob/EndJob 往返，设备状态机才会复位；
     * 只有拿到 <ivec:response>OK</ivec:response> 才算真正就绪。 */
    char xml[512], xml_end[512], resp[8192];
    int len_start = xml_job("StartJob", xml, sizeof(xml));
    int len_end   = xml_job("EndJob", xml_end, sizeof(xml_end));
    int ready = 0;

    for (int attempt = 1; attempt <= opt_attempts && !ready; attempt++) {
        char rsp[64] = "", det[64] = "";

        INFO("  [*] 就绪握手 %d/%d: StartJob ... ", attempt, opt_attempts);
        fflush(stderr);

        if (ivec_cmd(h, xml, (size_t)len_start, resp, sizeof(resp)) != 0) {
            INFO("无响应\n");
            if (attempt == opt_attempts)
                break;
            for (int i = 0; i < 3; i++)
                libusb_clear_halt(h, eps[i]);
            sleep(1);
            continue;
        }
        extract(resp, "response", rsp, sizeof(rsp));
        extract(resp, "response_detail", det, sizeof(det));
        INFO("%s%s%s\n", rsp[0] ? rsp : "?", det[0] ? " / " : "", det);

        INFO("      EndJob ... ");
        fflush(stderr);
        if (ivec_cmd(h, xml_end, (size_t)len_end, resp, sizeof(resp)) == 0) {
            extract(resp, "response", rsp, sizeof(rsp));
            det[0] = '\0';
            extract(resp, "response_detail", det, sizeof(det));
            INFO("%s%s%s\n", rsp[0] ? rsp : "?", det[0] ? " / " : "", det);
        } else {
            INFO("无响应\n");
        }

        if (!strcmp(rsp, "OK")) {
            /* 本轮 StartJob 拿到 OK = 设备真正空闲；配套的 EndJob 也回了 OK，作业已收尾。 */
            ready = 1;
            break;
        }
        sleep(1);
    }

    if (ready) {
        sleep(1);
        INFO("[canon-scan-fix] 设备已就绪，可以直接扫描。\n");
    } else {
        ERR("[canon-scan-fix] 无法让设备就绪（%d 次握手都没拿到 OK）。\n",
            opt_attempts);
        ERR("请试：canon-scan-fix -R          （整机 USB 复位）\n");
        ERR("仍失败请拔插扫描仪 USB 线，或断电重启设备。\n");
        ret = 1;
    }

unclaim:
    libusb_release_interface(h, IFACE_SCAN);
out:
    libusb_close(h);
    libusb_free_device_list(list, 1);
    libusb_exit(ctx);
    return ret;
}
