/* Canon 扫描 —— 前端交互。进度是后端流式解析 PNM 得到的真实字节比；
   预览走 /api/live?t=<task>&n=<seq>，seq 递增即新帧，过期帧由浏览器自行丢弃。 */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const api = async (path, body) => {
  const o = body === undefined ? {} :
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) };
  const r = await fetch(path, o);
  const t = await r.text();
  try { return JSON.parse(t); } catch { return { error: t || ('HTTP ' + r.status) }; }
};

const S = {
  info: null, devices: [], dev: null, caps: {}, capsOpts: {},
  opts: {},                 // 当前参数：resolution / mode / source / gamma …
  task: null, es: null, scanning: false,
  pages: [], cur: -1,
  scanW: 0, scanH: 0, paperW: 0, paperH: 0,
  zoom: 0,                  // 0 = 适应窗口
  previewSeq: -1, previewAt: 0, pendingSeq: null, previewTimer: null,
  mmW: 216.069, mmH: 297.011, _warm: false,
  netFixed: false,          // 网络设备：出图参数由设备侧固定，界面锁死
  idMode: false,            // 身份证模式：两扫合成一页 A4
  idStep: 0,                // 0=初始 1=正面已扫待翻面 2=正在扫反面
  idFront: null, idBack: null,
};

function toast(kind, html, ms = 4200) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  const ic = {
    ok: '<path d="M20 6L9 17l-5-5"/>',
    err: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>',
    warn: '<path d="M10.3 3.9L2.4 18a2 2 0 001.7 3h15.8a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  }[kind] || '';
  el.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="2.2" stroke-linecap="round"
      stroke-linejoin="round">${ic}</svg><span>${html}</span>`;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.transition = 'opacity .3s'; el.style.opacity = '0';
    setTimeout(() => el.remove(), 320); }, ms);
}

function dialog({ title, body, acts }) {
  $('#dlgTitle').innerHTML = title;
  $('#dlgBody').innerHTML = body || '';
  const box = $('#dlgActs'); box.innerHTML = '';
  (acts || [{ label: '知道了', pri: true }]).forEach(a => {
    const b = document.createElement('button');
    b.className = 'ab' + (a.pri ? ' pri' : '');
    b.textContent = a.label;
    b.onclick = () => { closeDialog(); a.onClick && a.onClick(); };
    box.appendChild(b);
  });
  $('#dlgOverlay').classList.add('on');
}
const closeDialog = () => $('#dlgOverlay').classList.remove('on');

function setStatus(kind, text) {
  const led = $('#stLed');
  led.className = 'led' + (kind === 'err' ? ' err' : kind === 'warn' ? ' warn'
    : kind === 'ok' ? ' ok' : '');
  $('#stText').textContent = text;
}
function setProgress(pct, running) {
  const f = $('#stBar');
  f.style.width = (pct == null ? 0 : Math.max(0, Math.min(100, pct))) + '%';
  f.classList.toggle('run', !!running);
  $('#stPct').textContent = pct == null ? '—' : pct.toFixed(0) + '%';
}
function setTag(kind, text) {
  const t = $('#stTag');
  t.className = 'tag' + (kind ? ' ' + kind : '');
  $('#stTagText').textContent = text;
}
const fmtBytes = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB'
  : n > 1024 ? (n / 1024).toFixed(0) + ' KB' : n + ' B';
const fmtMM = v => (Math.round(v * 10) / 10) + 'mm';

/* 厂商名与型号常有重复（CANON / Canon PIXMA…），去掉冗余前缀 */
function devTitle(d) {
  const v = (d.vendor || '').trim(), m = (d.model || '').trim();
  if (!v) return m || d.id;
  if (m.toLowerCase().startsWith(v.toLowerCase())) return m;
  return (v + ' ' + m).trim();
}

/* 设备副标题：USB 走 SANE 设备名；网络设备给 IP / MAC / 序列号 */
function macPretty(s) {
  const h = (s || '').replace(/[^0-9a-fA-F]/g, '');
  if (h.length !== 12) return s || '';
  return h.toUpperCase().match(/../g).join('-');
}
function devPathText(d) {
  if (d.net) {
    const bits = [d.ip || '?'];
    if (d.mac) bits.push(macPretty(d.mac));
    if (d.serial) bits.push('SN ' + d.serial);
    if (d.conn) bits.push(d.conn);
    return bits.join(' · ');
  }
  return (d.id || '') + ' · ' + (d.bus || '');
}
const NET_HINT = '网络扫描走 Canon 私有 CHMP 通道（/canon/ij/command2/port3），'
  + '与原厂扫描驱动完全一致；无需接 USB 线。';

function paperById(id) {
  return (S.caps.papers || []).find(p => p.id === id) ||
    { id: 'max', name: '整幅玻璃', w: null, h: null };
}
function pxFromMM(mm, dpi) { return Math.round(mm / 25.4 * dpi); }

function estimateSize() {
  const paper = paperById($('#paperSel').value || 'max');
  const dpi = parseInt(S.opts.resolution || 300, 10);
  const mmW = paper.w || S.mmW, mmH = paper.h || S.mmH;
  S.mmW2 = mmW; S.mmH2 = mmH;
  const w = pxFromMM(mmW, dpi), h = pxFromMM(mmH, dpi);
  const est = h * w * (S.opts.mode === 'Color' ? 3 : 1);
  const size = est > 1048576 ? (est / 1048576).toFixed(1) + ' MB'
    : (est / 1024).toFixed(0) + ' KB';
  $('#sizeTip').innerHTML = `${w} × ${h} px · 未压缩约 <b>${size}</b>`;
  $('#resNote').textContent = dpi + ' dpi';
  return { w, h };
}

const MODE_LABEL = { Color: '彩色', Gray: '灰度', Lineart: '黑白', Lineart2: '黑白' };
const SRC_LABEL = { Flatbed: '玻璃稿台', Automatic: '自动进纸', ADF: '自动进纸' };
const label = (m, v) => m[v] || v;

function renderSegs() {
  // 网络设备出图参数由发给设备的固定参数包决定（A4 / 300dpi / 彩色 / 平板），这里只做「如实显示 + 锁死」，不提供无效选项
  if (S.dev && S.dev.net) {
    S.opts.mode = 'Color';
    S.opts.resolution = '300';
    S.opts.source = 'Flatbed';
    const segM0 = $('#segMode');
    segM0.className = 'seg';
    segM0.innerHTML = '<b class="on">彩色（网络固定）</b>';
    const segR0 = $('#segRes');
    segR0.className = 'seg';
    segR0.innerHTML = '<b class="on">300 dpi（网络固定）</b>';
    const ps0 = $('#paperSel');
    if (!ps0.options.length) {
      (S.caps.papers || []).forEach(p => {
        const o = document.createElement('option');
        o.value = p.id; o.textContent = p.name; ps0.appendChild(o);
      });
    }
    ps0.value = 'a4';
    ps0.disabled = true;
    ps0.parentElement.classList.add('off');
    $('#paperTxt').textContent = paperById('a4').name;
    const ss0 = $('#srcSel');
    if (!ss0.options.length) {
      const o = document.createElement('option');
      o.value = 'Flatbed'; o.textContent = label(SRC_LABEL, 'Flatbed');
      ss0.appendChild(o);
    }
    ss0.value = 'Flatbed';
    ss0.disabled = true;
    ss0.parentElement.classList.add('off');
    $('#srcTxt').textContent = label(SRC_LABEL, 'Flatbed');
    $('#slGamma').hidden = true;
    $('#slThresh').hidden = true;
    $('#resNote').textContent = '300 dpi';
    return;
  }
  $('#paperSel').disabled = false;
  $('#paperSel').parentElement.classList.remove('off');
  $('#srcSel').disabled = false;
  $('#srcSel').parentElement.classList.remove('off');

  const mo = S.capsOpts['--mode'];
  const modes = (mo && mo.values.length) ? mo.values : ['Color', 'Gray', 'Lineart'];
  const segM = $('#segMode'); segM.innerHTML = '';
  const cls = modes.length <= 2 ? 'c2' : modes.length === 3 ? 'c3' : 'c4';
  segM.className = 'seg ' + cls;
  modes.forEach(v => {
    const b = document.createElement('b');
    b.textContent = label(MODE_LABEL, v);
    b.dataset.v = v;
    b.classList.toggle('on', v === (S.opts.mode || modes[0]));
    b.onclick = () => { S.opts.mode = v; renderSegs(); updateThreshVis(); estimateSize(); };
    segM.appendChild(b);
  });
  if (!S.opts.mode) S.opts.mode = modes[0];

  const ro = S.capsOpts['--resolution'];
  let res = (ro && ro.values.length) ? ro.values
    .map(x => parseInt(x, 10)).filter(x => x > 0) : [75, 150, 300, 600];
  res = [...new Set(res)].sort((a, b) => a - b).slice(0, 8);
  const segR = $('#segRes'); segR.innerHTML = '';
  segR.className = 'seg ' + (res.length === 2 ? 'c2' : res.length === 3 ? 'c3' : 'c4');
  res.forEach(v => {
    const b = document.createElement('b');
    b.textContent = v;
    b.classList.toggle('on', String(v) === String(S.opts.resolution));
    b.onclick = () => { S.opts.resolution = String(v); renderSegs(); estimateSize(); };
    segR.appendChild(b);
  });

  const ps = $('#paperSel');
  if (!ps.options.length) {
    (S.caps.papers || []).forEach(p => {
      const o = document.createElement('option');
      o.value = p.id; o.textContent = p.name; ps.appendChild(o);
    });
    ps.value = 'a4p';
    ps.onchange = () => {
      $('#paperTxt').textContent = paperById(ps.value).name;
      estimateSize(); layoutPaper();
    };
    $('#paperTxt').textContent = paperById(ps.value).name;
  }

  const so = S.capsOpts['--source'];
  const srcs = (so && so.values.length) ? so.values : ['Flatbed'];
  const ss = $('#srcSel');
  if (!ss.options.length) {
    srcs.forEach(v => {
      const o = document.createElement('option');
      o.value = v; o.textContent = label(SRC_LABEL, v); ss.appendChild(o);
    });
    S.opts.source = srcs[0];
    ss.onchange = () => {
      S.opts.source = ss.value;
      $('#srcTxt').textContent = label(SRC_LABEL, ss.value);
    };
    $('#srcTxt').textContent = label(SRC_LABEL, srcs[0]);
  }

  const go = S.capsOpts['--gamma'];
  $('#slGamma').hidden = !(go && !go.inactive);
  const to = S.capsOpts['--threshold'];
  updateThreshVis();
  void to;
}

function updateThreshVis() {
  const to = S.capsOpts['--threshold'];
  $('#slThresh').hidden = !(to && !to.inactive && S.opts.mode === 'Lineart');
}

function layoutPaper() {
  const W = S.paperW || S.scanW, H = S.paperH || S.scanH;
  if (!W || !H) return;
  const v = $('#view').getBoundingClientRect();
  const pad = 78;
  const availW = Math.max(80, v.width - pad), availH = Math.max(80, v.height - pad);
  let scale;
  if (!S.zoom) scale = Math.min(availW / W, availH / H);
  else scale = S.zoom;
  const w = Math.max(40, Math.round(W * scale));
  const h = Math.max(40, Math.round(H * scale));
  const pw = $('#pwrap');
  pw.hidden = false;
  pw.style.width = w + 'px'; pw.style.height = h + 'px';
  $('#zoomVal').textContent = S.zoom ? Math.round(S.zoom * 100) + '%' : '适应';
  $('#fitBtn').classList.toggle('on', !S.zoom);
  syncBeam();
}
function syncBeam() {
  const img = $('#paperImg');
  const beam = $('.beam');
  if (!beam || !img) return;
  if (img.hidden || !img.naturalWidth) { beam.style.display = 'none'; return; }
  beam.style.display = '';
  beam.style.top = Math.max(0, Math.min(img.offsetHeight, $('#paper').clientHeight)) + 'px';
}

function showEmpty(title, text) {
  $('#empty').hidden = false; $('#pwrap').hidden = true;
  if (title) $('#emptyTitle').textContent = title;
  if (text) $('#emptyText').innerHTML = text;
  $('#cornerTL').classList.add('hide'); $('#cornerTR').classList.add('hide');
  $('#fbar').classList.add('hide');
}
function showPaper() {
  $('#empty').hidden = true; $('#pwrap').hidden = false;
  $('#cornerTL').classList.remove('hide');
  $('#cornerTR').classList.remove('hide');
  $('#fbar').classList.remove('hide');
}
function setBeamVisible(on) {
  let beam = $('.beam');
  if (!on) { if (beam) beam.remove(); return; }
  if (!beam) {
    beam = document.createElement('div');
    beam.className = 'beam';
    beam.style.top = '0px';
    $('#paper').appendChild(beam);
  }
  beam.style.display = '';
}

/* 网络扫描没有可逐行解码的预览，接收整幅 JPEG 期间画面是空的——
   用「纸张 + 扫描光束动画 + 实时字节」占位，避免看起来像卡死 */
let netScanOverlay = null;
function showNetScanning() {
  if (netScanOverlay || !S.dev || !S.dev.net) return;
  const el = document.createElement('div');
  el.className = 'netscan';
  el.innerHTML = `<span class="ns-beam"></span>
    <div class="ns-mid">
      <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" opacity=".5">
        <rect x="2.5" y="6" width="19" height="12" rx="2"/>
        <circle cx="9" cy="12" r="1.5"/><circle cx="9" cy="12" r="3.4" stroke-dasharray="4 3"/>
        <path d="M18 9h.01M17.5 14.5h.01"/></svg>
      <b class="ns-st" id="nsSt">正在接收图像…</b>
      <span class="ns-by" id="nsBy"></span>
    </div>`;
  el.id = 'netScanOv';
  $('#paper').appendChild(el);
  netScanOverlay = el;
}
function setNetScan(stage, bytes) {
  const el = netScanOverlay || $('#netScanOv');
  if (!el) return;
  if (stage != null) $('#nsSt').textContent = stage;
  if (bytes != null) $('#nsBy').textContent = bytes;
}
function hideNetScanning() {
  if (netScanOverlay) { netScanOverlay.remove(); netScanOverlay = null; }
}

function buildOpts() {
  const paper = paperById($('#paperSel').value || 'max');
  const o = {
    resolution: S.opts.resolution || '300',
    mode: S.opts.mode || 'Color',
    source: S.opts.source || undefined,
    calibrate: 'Once',
    width: paper.w || S.mmW,
    height: paper.h || S.mmH,
    left: 0, top: 0,
  };
  if (S.opts.mode === 'Lineart') { delete o.calibrate; }
  const go = S.capsOpts['--gamma'];
  if (go && !go.inactive && !$('#slGamma').hidden) o.gamma = $('#gamma').value;
  const to = S.capsOpts['--threshold'];
  if (to && !to.inactive && !$('#slThresh').hidden) o.threshold = $('#thresh').value;
  return o;
}

async function startScan(kind = 'scan') {
  if (S.scanning) return;
  if (!S.dev) { toast('err', '没有可用的扫描仪'); return; }
  if (S.idMode && S.idStep === 1) {
    toast('warn', '等待翻面：请把身份证反面朝下放回同一位置并盖好盖子');
    return;
  }

  const opts = buildOpts();
  S.scanning = true; S.cur = -1; S.previewSeq = -1; S._warm = false;
  // 必须先作废旧任务的 task 与 EventSource：旧流在「服务端已断、浏览器待重连」窗口里的迟到 onerror 会误伤新扫描；listenTask 拿到新 task 后再替换
  S.task = null;
  if (S.es) { S.es.close(); S.es = null; }
  $('#btnScan').disabled = true; $('#btnPreview').disabled = true;
  $('#btnNext').disabled = true; $('#btnSave').disabled = true;

  const est = estimateSize();
  S.paperW = est.w; S.paperH = est.h;
  $('#paperImg').removeAttribute('src');
  $('#paperImg').hidden = false;
  wirePreview($('#paperImg'));
  setBeamVisible(true);
  showPaper(); layoutPaper();
  if (S.dev.net) showNetScanning();

  setStatus('', kind === 'preview' ? '快速预览中…' : '正在扫描…');
  setProgress(0, true);
  $('#cornerLed').className = 'led';
  $('#cornerText').textContent = kind === 'preview' ? '预览中' : '扫描中';
  $('#cornerPct').textContent = '';
  setTag('', '设备通信中');

  const r = await api('/api/scan', { device: S.dev.id, opts, kind });
  if (r.error || !r.task) {
    S.scanning = false; setStatus('err', '无法开始扫描');
    toast('err', r.error || '启动失败');
    resetButtons(); return;
  }
  S.task = r.task;
  listenTask(r.task);
}

function resetButtons() {
  $('#btnScan').disabled = false;
  $('#btnPreview').disabled = false;
  $('#btnNext').disabled = S.pages.length === 0;
  $('#btnSave').disabled = S.pages.length === 0;
}

async function doDiscover(quiet) {
  const b = $('#btnFind');
  b.classList.add('busy');
  if (!quiet) {
    setStatus('', '正在发现网络扫描仪…');
    setTag('warn', '网络发现中');
  }
  const r = await api('/api/discover');
  b.classList.remove('busy');
  const nets = (r.devices || []).filter(d => d.net);
  const meta = r.net || {};
  if (quiet) return nets;
  if (nets.length) {
    const d = nets[0];
    toast('ok', `发现 <b>${d.model}</b> @ <b>${d.ip}</b>`
      + (d.scan_ready ? '（扫描通道有响应 ✓）' : '')
      + `<br><span style="font-size:11px;opacity:.75">方式 ${meta.method || '-'}`
      + ` · ${meta.elapsed != null ? meta.elapsed + 's' : ''}</span>`, 7000);
    setStatus('ok', '已发现网络扫描仪');
  } else {
    toast('warn', '没发现网络上的 Canon 设备'
      + (meta.method ? `（方式 ${meta.method}）` : '')
      + '<br><span style="font-size:11px;opacity:.75">'
      + '请确认打印机与电脑在同一网段并已开机联网</span>', 7000);
    setStatus('warn', '网络未发现设备');
  }
  await loadDevices(false);
  setTag('', (S.dev && S.dev.net) ? '网络设备（可扫描）' : '设备通道就绪');
  return nets;
}

function listenTask(task) {
  if (S.es) { S.es.close(); S.es = null; }
  const es = new EventSource('/api/events?t=' + encodeURIComponent(task));
  S.es = es;
  es.onmessage = e => {
    let ev; try { ev = JSON.parse(e.data); } catch { return; }
    handleEvent(ev, task);
  };
  es.onerror = () => {
    // 只认「当前任务自身」的流错误：服务端终态主动断流 + 浏览器规范性重连产生的迟到 onerror 不能算到新扫描头上
    if (S.es === es && S.scanning && task === S.task) {
      setTimeout(() => { if (S.scanning) finishFail('与后端连接中断'); }, 900);
    }
    es.close(); if (S.es === es) S.es = null;
  };
}

function handleEvent(ev, task) {
  switch (ev.type) {
    case 'start':
      break;

    case 'progress': {
      if (ev.W && ev.H) {
        if (!S.paperW || Math.abs(S.paperW / S.paperH - ev.W / ev.H) > 0.01) {
          S.paperW = ev.W; S.paperH = ev.H;
          layoutPaper();
        }
      }
      setProgress(ev.pct, true);
      // scanimage 启动后有 6~8 秒校准/预热期（字节为 0），必须给用户交代，否则会以为卡死
      const warm = ev.pct < 0.8 && ev.elapsed > 2.2;
      if (warm !== S._warm) {
        S._warm = warm;
        $('#cornerText').textContent = warm ? '预热中' : '扫描中';
        setStatus('', warm ? '扫描头预热 / 校准中…' : '正在扫描…');
      }
      $('#cornerPct').textContent = ev.pct ? ev.pct.toFixed(0) + '%' : '';
      $('#stMeta').textContent = `${fmtBytes(ev.bytes)} / ${fmtBytes(ev.total || 0)}`
        + ` · 已用 ${ev.elapsed}s`;
      if (S.dev.net) setNetScan(null,
        `${fmtBytes(ev.bytes)} / ${fmtBytes(ev.total || 0)} · ${ev.elapsed}s`);
      if (!S.dev.net && ev.seq !== undefined && ev.seq !== S.previewSeq) {
        S.previewSeq = ev.seq;
        pushPreview(task, ev.seq);
      }
      break;
    }

    // 网络扫描（CHMP）没有逐行数据流，用后端阶段文字代替实时预览提示
    case 'stage':
      if (ev.msg) {
        setStatus('', ev.msg + '…');
        if (S.dev.net) setNetScan(ev.msg);
      }
      break;

    case 'retry':
      setStatus('warn', '自动恢复中…');
      setTag('warn', '正在复位扫描通道');
      $('#cornerText').textContent = '恢复中';
      toast('warn', ev.msg || '扫描失败，正在自动恢复…', 6000);
      break;

    case 'done': {
      hideNetScanning();
      S.scanning = false;
      setProgress(100, false);
      setStatus('ok', '扫描完成');
      setTag('', '设备通道就绪');
      $('#cornerLed').className = 'led ok';
      $('#cornerText').textContent = '已完成';
      $('#cornerPct').textContent = '';
      setBeamVisible(false);
      $('#stMeta').textContent = `${ev.W} × ${ev.H} · ${fmtBytes(ev.bytes)}`
        + ` · 用时 ${ev.elapsed}s`;
      loadFinal(task, ev);
      break;
    }

    case 'error':
      hideNetScanning();
      finishFail(ev.msg || '扫描失败', ev.hint);
      break;

    case 'cancelled':
      hideNetScanning();
      S.scanning = false;
      cancelFinishLocal();
      toast('warn', '扫描已取消');
      break;
  }
}

function wirePreview(img) {
  img.onload = () => { img.style.visibility = ''; syncBeam(); };
  img.onerror = () => { img.style.visibility = 'hidden'; };
}

function pushPreview(task, seq) {
  const now = performance.now();
  const go = () => {
    S.previewAt = performance.now();
    const s = S.pendingSeq ?? seq; S.pendingSeq = null;
    const img = $('#paperImg');
    img.hidden = false;
    wirePreview(img);
    img.src = `/api/live?t=${task}&n=${s}`;
  };
  if (now - S.previewAt >= 130) go();
  else {
    S.pendingSeq = seq;
    if (!S.previewTimer) {
      S.previewTimer = setTimeout(() => { S.previewTimer = null; go(); }, 130);
    }
  }
}

async function checkCard(task) {
  let r;
  try { r = await api(`/api/card-check?t=${encodeURIComponent(task)}`); }
  catch { return true; }                 // 接口不可用 → 放行，不误丢
  return r ? !!r.ok : true;
}

function discardScan(msg, task) {
  if (task) api('/api/drop-task', { task });
  const img = $('#paperImg');
  img.removeAttribute('src');
  img.hidden = false;
  wirePreview(img);
  setStatus('err', msg);
}

// 检测不到证件/卡片：模态弹窗，用户确认后才放行重扫（期间扫描按钮保持禁用）
function confirmRedo(msg, cb) {
  dialog({
    title: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="var(--warn)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>未检测到证件',
    body: `<b>${msg}</b><br>
      <span style="font-size:11px;opacity:.75">本次扫描数据已丢弃，不会加入画廊。请放好证件后点「重新扫描」。</span>`,
    acts: [
      { label: '取消', onClick: () => { resetButtons(); } },
      { label: '重新扫描', pri: true, onClick: () => { resetButtons(); startScan('scan'); } },
    ],
  });
}

async function loadFinal(task, ev) {
  const img = $('#paperImg');
  S.paperW = ev.W; S.paperH = ev.H;
  layoutPaper();
  const w = Math.max(1200, Math.round($('#pwrap').clientWidth * 2));
  wirePreview(img);
  img.src = `/api/result?t=${task}&w=${w}`;
  toast('ok', `扫描完成：<b>${ev.W} × ${ev.H}</b> · ${fmtBytes(ev.bytes)}`
    + (ev.retried ? '（已自动恢复）' : ''));

  // 身份证模式第一面（正面）：不加入画廊，提示翻面
  if (S.idMode && S.idStep === 0) {
    if (!(await checkCard(task))) {
      S.idFront = null; S.idStep = 0;
      discardScan('稿件台上未检测到身份证或卡片', task);
      confirmRedo('稿件台上未检测到身份证或卡片');
      return;
    }
    S.idFront = task;
    S.idStep = 1;
    resetButtons();
    promptFlip();
    return;
  }
  // 身份证模式第二面（反面）：合成一页入库
  if (S.idMode && S.idStep === 2) {
    if (!(await checkCard(task))) {
      S.idBack = null; S.idStep = 2;
      discardScan('反面未检测到身份证或卡片', task);
      confirmRedo('反面未检测到证件或卡片，正面已保留');
      return;
    }
    S.idBack = task;
    mergeIdPages();
    return;
  }

  api('/api/add-page', { task }).then(r => {
    if (r.ok) {
      S.pages.push({ index: r.index, w: ev.W, h: ev.H });
      S.cur = S.pages.length - 1;
      renderTabs(); selectPage(S.cur);
    }
    resetButtons();
  });
}

function resetIdFlow() {
  S.idStep = 0; S.idFront = null; S.idBack = null;
}

function promptFlip() {
  dialog({
    title: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="var(--ok)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="10" rx="2"/><path d="M7 20h10M8 16v4M16 16v4"/></svg>已扫描正面',
    body: `<b>请打开盖子，将身份证</b>翻面<b>（反面朝下）放回玻璃的同一位置</b>，盖好盖子。<br>
      <span style="font-size:11px;opacity:.75">放好后点「已翻面，继续」扫反面；点「放弃」则丢弃这次身份证扫描，画廊不变。</span>`,
    acts: [
      { label: '放弃', onClick: () => { resetIdFlow(); setStatus('', '已放弃身份证扫描'); } },
      { label: '已翻面，继续', pri: true, onClick: () => { S.idStep = 2; startScan('scan'); } },
    ],
  });
}

async function mergeIdPages() {
  const r = await api('/api/merge-id', { front: S.idFront, back: S.idBack, layout: 'tb' });
  const front = S.idFront;
  resetIdFlow();
  if (r.error || r.index == null) {
    toast('err', r.error || '合成失败');
    setStatus('err', '身份证合成失败');
    resetButtons();
    return;
  }
  const page = { index: r.index, w: r.W, h: r.H };
  S.pages.push(page);
  S.cur = S.pages.length - 1;
  renderTabs();
  selectPage(S.cur);
  resetButtons();
  toast('ok', `身份证正反面已合成一页：<b>${r.W} × ${r.H}</b>`
    + `<br><span style="font-size:11px;opacity:.75">正面在上半，反面在下半 · 300 dpi</span>`, 8000);
}

function finishFail(msg, hint) {
  S.scanning = false;
  resetIdFlow();
  setStatus('err', '扫描失败');
  setTag('err', '设备通道异常');
  setProgress(null, false);
  $('#cornerLed').className = 'led err';
  $('#cornerText').textContent = '失败';
  $('#stMeta').textContent = msg || '扫描失败';
  resetButtons();
  dialog({
    title: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="var(--err)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>扫描失败',
    body: `<b>${msg || '未知错误'}</b><br><br>${hint || ''}<br><br>
      点右上角的 <b>重新扫描按钮</b> 可重新枚举设备并复位 USB 扫描通道；若仍不行，请拔插扫描仪 USB 线。`,
    acts: [
      { label: '重新扫描设备', onClick: () => doRescan() },
      { label: '关闭', pri: true },
    ],
  });
  toast('err', msg || '扫描失败', 6000);
}

function cancelFinishLocal() {
  setStatus('warn', '已取消');
  setTag('warn', '正在复位扫描通道');
  setProgress(null, false);
  setBeamVisible(false);
  $('#cornerLed').className = 'led warn';
  $('#cornerText').textContent = '已取消';
  $('#stMeta').textContent = '用户取消';
  resetIdFlow();
  resetButtons();
  api('/api/fix', {}).then(() => setTag('', '设备通道就绪'));
}

async function cancelScan() {
  if (!S.scanning || !S.task) return;
  const task = S.task;
  if (S.es) { S.es.close(); S.es = null; }
  S.scanning = false;
  await api('/api/cancel', { task });
  cancelFinishLocal();
  toast('warn', '已取消扫描（通道已复位）');
}

function renderTabs() {
  const box = $('#tabs'); box.innerHTML = '';
  S.pages.forEach((p, i) => {
    const d = document.createElement('div');
    d.className = 'tab' + (i === S.cur ? ' on' : '');
    d.title = `第 ${i + 1} 页 · ${p.w} × ${p.h}`;
    d.innerHTML = `<img src="/api/thumb?i=${p.index}" alt="">` +
      `<span class="n">${i + 1}</span>`;
    d.onclick = () => selectPage(i);
    box.appendChild(d);
  });
  const add = document.createElement('div');
  add.className = 'tab add'; add.title = '扫描下一页';
  add.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
    <path d="M12 5v14M5 12h14"/></svg>`;
  add.onclick = () => scanNextPage();
  if (S.pages.length) box.appendChild(add);
  $('#pageAll').textContent = Math.max(1, S.pages.length);
}

function selectPage(i) {
  if (i < 0 || i >= S.pages.length) return;
  S.cur = i; S.zoom = 0;
  const p = S.pages[i];
  S.paperW = p.w; S.paperH = p.h;
  setBeamVisible(false);
  showPaper();
  const img = $('#paperImg');
  wirePreview(img);
  img.src = `/api/result?i=${p.index}&w=1600&_=${Date.now()}`;
  layoutPaper();
  $('#pageNow').textContent = i + 1;
  $('#tiTitle').textContent = `第 ${i + 1} 页`;
  $('#tiMeta').textContent = `${p.w} × ${p.h} · ${S.opts.mode === 'Color' ? '彩色'
    : S.opts.mode === 'Gray' ? '灰度' : '黑白'} · ${S.opts.resolution} dpi`;
  const fmt = $('#fmtSel').value;
  $('#saveLabel').textContent = fmt === 'pdf' ? '保存为 PDF' : '保存本页';
  $('#delPage').disabled = false;
  renderTabs();
}

async function scanNextPage() {
  if (S.scanning) return;
  await startScan('scan');
}

async function delCurrentPage() {
  if (S.cur < 0) return;
  const p = S.pages[S.cur];
  await api('/api/del-page', { index: p.index });
  // 删除后索引会整体前移
  S.pages.splice(S.cur, 1);
  S.pages.forEach((q, k) => { q.index = k; });
  if (!S.pages.length) {
    S.cur = -1;
    showEmpty('准备就绪',
      '把原件放在玻璃稿台上，盖好盖子，然后点左下角的<b>开始扫描</b>。');
    $('#tiTitle').textContent = '尚无扫描';
    $('#tiMeta').textContent = '选择参数后点「开始扫描」';
    $('#pageAll').textContent = '1'; $('#pageNow').textContent = '1';
    $('#saveLabel').textContent = '保存';
  } else {
    selectPage(Math.min(S.cur, S.pages.length - 1));
  }
  renderTabs();
  resetButtons();
  toast('ok', '已删除该页');
}

async function doSave() {
  const fmt = $('#fmtSel').value;
  if (fmt === 'pdf') {
    if (!S.pages.length) return;
    const r = await api('/api/save', { format: 'pdf' });
    if (r.error) { toast('err', r.error); return; }
    toast('ok', `已保存 <b>${r.path}</b>`, 9000);
    dialog({
      title: '导出完成',
      body: `已把 <b>${S.pages.length}</b> 页合并为 PDF：<br>
        <span style="font-family:'Noto Sans Mono',monospace;font-size:11px">${r.path}</span>`,
      acts: [
        { label: '在文件管理器中打开', onClick: () => api('/api/reveal', { path: r.path }) },
        { label: '好', pri: true },
      ],
    });
  } else {
    if (S.cur < 0) return;
    const p = S.pages[S.cur];
    const r = await api('/api/save', { format: fmt, page: p.index });
    if (r.error) { toast('err', r.error); return; }
    toast('ok', `已保存 <b>${r.path}</b>`, 8000);
    dialog({
      title: '保存完成',
      body: `<span style="font-family:'Noto Sans Mono',monospace;font-size:11px">${r.path}</span>`,
      acts: [
        { label: '在文件管理器中打开', onClick: () => api('/api/reveal', { path: r.path }) },
        { label: '好', pri: true },
      ],
    });
  }
}

async function loadDevices(force) {
  const r = await api('/api/devices' + (force ? '?force=1' : ''));
  S.devices = (r.devices || []);
  S.netMeta = r.net || {};
  const sel = $('#devSel'); sel.innerHTML = '';
  S.devices.forEach(d => {
    const o = document.createElement('option');
    o.value = d.id;
    o.textContent = (d.net ? '[网络] ' : '[USB] ') + d.vendor + ' ' + d.model;
    sel.appendChild(o);
  });
  if (!S.devices.length) {
    S.dev = null;
    $('#devBox').classList.remove('online');
    $('#devBox').classList.remove('netdev');
    $('#devName').textContent = '未找到扫描仪';
    $('#devPath').textContent = '请检查电源与连接，或点右上角重新扫描按钮查找设备';
    setTag('err', '未检测到设备');
    setScanAvail();
    return false;
  }
  // 默认优先挑能直接扫的（SANE/USB）；网络设备排在后面
  const pick = S.devices.find(d => !d.net) || S.devices[0];
  sel.value = pick.id;
  sel.onchange = () => {
    applyDevice(S.devices.find(x => x.id === sel.value) || S.devices[0]);
  };
  await applyDevice(pick);
  return true;
}

async function applyDevice(d) {
  S.dev = d;
  $('#devBox').classList.add('online');
  $('#devBox').classList.toggle('netdev', !!d.net);
  $('#devName').textContent = devTitle(d);
  $('#devPath').textContent = devPathText(d);
  setScanAvail();
  if (d.net) {
    setTag('', '网络设备（可直接扫描）');
    setStatus('ok', '网络扫描仪就绪');
  }
  await loadCaps();
}

/* 网络设备 1.0-7 起走 CHMP 通道真实取图，和 USB 一样可用；只有后端明确报告「网络扫描模块不可用」时才禁用（点下去只会拿到 409） */
function setScanAvail() {
  const isNet = !!(S.dev && S.dev.net);
  const noNet = isNet && S.info && S.info.netscan === false;
  $('#btnScan').disabled = !!noNet;
  $('#btnPreview').disabled = !!noNet;
  if (noNet) setTag('err', '网络扫描模块缺失');
}

async function loadCaps() {
  if (!S.dev) return;
  if (S.dev.net) {
    // 网络设备没有 SANE 参数表：出图参数由固定参数包（CMD_D820：A4 / 300dpi / 彩色 / 平板）决定，界面按真实出图锁定，免得选了 600dpi 却拿到 300dpi
    const rn = await api('/api/caps?d=' + encodeURIComponent(S.dev.id));
    S.caps = { papers: (rn && rn.papers) || S.caps.papers || [] };
    S.capsOpts = {};
    S.netFixed = true;
    S.opts.resolution = '300';
    S.opts.mode = 'Color';
    S.opts.source = 'Flatbed';
    S.mmW = 210.0; S.mmH = 297.0;
    $('#areaNote').textContent = 'A4 210×297 mm';
    renderSegs();
    estimateSize();
    setScanAvail();
    return;
  }
  S.netFixed = false;
  const r = await api('/api/caps?d=' + encodeURIComponent(S.dev.id));
  if (r.error) { toast('err', '读取设备参数失败：' + r.error); return; }
  S.caps = { papers: r.papers || [] };
  S.capsOpts = r.options || {};
  // 默认值：设备后端默认是 75dpi（偏低），开箱即用提升到 300dpi
  const ro = S.capsOpts['--resolution'] || {};
  const rv = (ro.values || []).map(String);
  const rd = ro.default;
  let pick = (rd && rd !== 'auto') ? String(rd) : '';
  if (rv.length && !rv.includes(pick)) pick = '';
  if (!pick || parseInt(pick, 10) < 150) {
    if (rv.includes('300')) pick = '300';
    else if (rv.length) {
      pick = rv.reduce((a, b) => Math.abs(parseInt(b, 10) - 300) <
        Math.abs(parseInt(a, 10) - 300) ? b : a, rv[0]);
    }
  }
  S.opts.resolution = pick || '300';

  const md = (S.capsOpts['--mode'] || {}).default;
  S.opts.mode = (md && md !== 'auto') ? md : 'Color';
  const xo = S.capsOpts['-x'], yo = S.capsOpts['-y'];
  if (xo && xo.range) S.mmW = xo.range[1];
  if (yo && yo.range) S.mmH = yo.range[1];
  $('#areaNote').textContent = `${formatNum(S.mmW)}×${formatNum(S.mmH)} mm`;
  renderSegs();
  const est = estimateSize();
  S.paperW = est.w; S.paperH = est.h;
  // 注意：这里**不能**调 layoutPaper()，否则空态下纸张底板会提前露出来
}
const formatNum = v => (Math.round(v * 10) / 10);

/* 重新扫描：重新枚举 USB/其它后端并主动发现网络扫描仪，顺带尝试复位扫描通道。
   复位不成也不报错 —— 只接网络设备时本来就没有 USB 通道可复位，报「复位失败」会让「重新扫描」看起来出错。 */
async function doRescan() {
  const b = $('#btnFix');
  if (b.classList.contains('busy')) return null;
  b.classList.add('busy');
  setTag('warn', '正在重新扫描设备');
  setStatus('', '正在重新枚举设备并发现网络扫描仪…');

  let fixed = false;
  try {
    const rf = await api('/api/fix', { reset: false });
    fixed = !!rf.ok;
  } catch (e) { fixed = false; }

  let r;
  try {
    r = await api('/api/discover');
  } catch (e) {
    r = { devices: [], net: { method: 'error', error: String((e && e.message) || e) } };
  }
  b.classList.remove('busy');

  const devs = r.devices || [];
  const usb = devs.filter(d => !d.net);
  const nets = devs.filter(d => d.net);
  const meta = r.net || {};
  await loadDevices(false);

  if (devs.length) {
    setTag('', (S.dev && S.dev.net) ? '网络设备（可扫描）' : '设备通道就绪');
    setStatus('ok', `已发现 ${devs.length} 台设备：USB ${usb.length} · 网络 ${nets.length}`);
    toast('ok', `重新扫描完成：USB <b>${usb.length}</b> 台 · 网络 <b>${nets.length}</b> 台`
      + (fixed ? '<br><span style="font-size:11px;opacity:.75">USB 扫描通道已复位</span>' : '')
      + (nets.length
        ? `<br><span style="font-size:11px;opacity:.75">方式 ${meta.method || '-'}`
          + `${meta.elapsed != null ? ' · ' + meta.elapsed + 's' : ''}</span>`
        : ''), 7000);
  } else {
    setTag('warn', '未找到设备');
    setStatus('warn', '未发现扫描仪');
    toast('warn', '没找到扫描仪：请确认打印机已开机'
      + (meta.method && meta.method !== 'error' ? `（发现方式 ${meta.method}）` : '')
      + '<br><span style="font-size:11px;opacity:.75">'
      + 'USB 请检查数据线；网络请确认与电脑在同一网段</span>', 8000);
  }
  return r;
}

function bind() {
  $('#btnScan').onclick = () => {
    if (S.idMode && S.idStep === 1) { S.idStep = 2; startScan('scan'); return; }
    startScan('scan');
  };
  $('#btnPreview').onclick = () => startScan('preview');
  $('#btnNext').onclick = () => scanNextPage();
  $('#btnSave').onclick = () => doSave();
  $('#saveOne').onclick = () => doSave();
  $('#delPage').onclick = () => delCurrentPage();
  $('#btnFix').onclick = () => doRescan();
  $('#btnFind').onclick = () => doDiscover();
  $('#fitBtn').onclick = () => { S.zoom = 0; layoutPaper(); };
  $('#realBtn').onclick = () => { S.zoom = 1; layoutPaper(); };
  $('#zoomIn').onclick = () => {
    S.zoom = Math.min(4, (S.zoom || (S.paperW ? $('#pwrap').clientWidth / S.paperW : 1)) * 1.25);
    layoutPaper();
  };
  $('#zoomOut').onclick = () => {
    S.zoom = Math.max(0.1, (S.zoom || (S.paperW ? $('#pwrap').clientWidth / S.paperW : 1)) / 1.25);
    if (S.zoom < 0.12) S.zoom = 0;
    layoutPaper();
  };

  /* 身份证 / 整页 扫描模式（与设备参数无关，一次构建） */
  {
    const kinds = [['page', '整页'], ['id', '身份证']];
    const seg = $('#segKind');
    const draw = () => {
      seg.className = 'seg c2';
      seg.innerHTML = '';
      kinds.forEach(([v, txt]) => {
        const b = document.createElement('b');
        b.textContent = txt;
        b.dataset.v = v;
        b.classList.toggle('on', (v === 'id') === !!S.idMode);
        b.onclick = () => { S.idMode = v === 'id'; resetIdFlow(); draw(); };
        seg.appendChild(b);
      });
      $('#idTip').hidden = !S.idMode;
      $('#modeNote').textContent = S.idMode ? '两扫一页 · A4' : '';
    };
    draw();
  }

  $('#fmtSel').onchange = () => {
    const v = $('#fmtSel').value;
    $('#fmtTxt').textContent = { png: 'PNG 图像', jpg: 'JPEG 图像',
      tif: 'TIFF 图像', pdf: '多页 PDF' }[v];
    $('#saveLabel').textContent = v === 'pdf' ? '保存为 PDF' : '保存本页';
  };
  $('#dirTxt').parentElement.onclick = async e => {
    if (e.target.tagName === 'SELECT') return;
    await api('/api/reveal', {});
  };

  $('#gamma').oninput = () => { $('#gammaVal').textContent = $('#gamma').value; };
  $('#thresh').oninput = () => { $('#threshVal').textContent = $('#thresh').value; };

  $('#btnTheme').onclick = () => {
    const light = document.body.classList.toggle('light');
    localStorage.setItem('cs-theme', light ? 'light' : 'dark');
  };

  $('#btnAbout').onclick = () => dialog({
    title: 'Canon 扫描 1.0',
    body: `界面：HTML/CSS/JS（WebKitGTK 原生窗口，非 Qt 非 Electron）<br>
      后端：Python 3 标准库，扫描协议由 <b>SANE pixma 后端</b>原生实现<br>
      <b>网络发现</b>：复用原厂扫描驱动同款调用链 <code>CNNET2_Search</code> /
      <code>CNNET2_EnumSearchInfo</code>（Canon 私有 SNMP，企业号 1602），
      扫描服务地址按原厂 <code>libcnnet3_url.h</code> 定位到
      <code>${(S.dev && S.dev.channel) || '/canon/ij/command2/port3'}</code><br>
      架构：<b>loong64 全原生</b>，无 x86-64 指令翻译<br>
      输出目录：<span style="font-family:'Noto Sans Mono',monospace;font-size:11px">${(S.info && S.info.outdir) || '~/扫描'}</span><br><br>
      设备卡住时点右上角 <b>复位</b> 图标即可恢复，无需拔插 USB。`,
    acts: [{ label: '好', pri: true }],
  });

  // 设备框点击（排除 select 自身）
  $('#devSel').addEventListener('click', e => e.stopPropagation());
  $('#devBox').onclick = e => {
    if (e.target.tagName === 'SELECT') return;
    $('#devSel').click();
  };

  window.addEventListener('resize', () => layoutPaper());

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if ($('#dlgOverlay').classList.contains('on')) closeDialog();
      else if (S.scanning) cancelScan();
      return;
    }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); startScan('scan'); }
    if (e.key === '+' || e.key === '=') { e.preventDefault(); $('#zoomIn').click(); }
    if (e.key === '-') { e.preventDefault(); $('#zoomOut').click(); }
    if (e.key === '0') { e.preventDefault(); $('#fitBtn').click(); }
  });

  $('#dlgOverlay').onclick = e => { if (e.target.id === 'dlgOverlay') closeDialog(); };
}

async function main() {
  if (localStorage.getItem('cs-theme') === 'light') {
    document.body.className = 'light';
  }
  bind();
  S.info = await api('/api/info');
  if (S.info && S.info.outdir) {
    $('#dirTxt').textContent = S.info.outdir.replace(/^\/home\/[^/]+/, '~');
  }
  if (S.info && !S.info.pil) {
    toast('warn', '本机缺少 Pillow，实时预览与 PDF 导出不可用', 9000);
  }
  const ok = await loadDevices(false);   // 首次调用即会做一次网络主动发现
  if (ok) {
    const isNet = !!(S.dev && S.dev.net);
    setStatus('ok', isNet ? '网络扫描仪就绪' : '就绪');
    setTag('', isNet ? '网络设备（可直接扫描）' : '设备通道就绪');
  } else {
    setStatus('err', '未检测到扫描仪');
    showEmpty('未找到扫描仪',
      '已自动做过一次网络主动发现。请确认设备已通电并联网'
      + '（USB 方式请接好线），然后点右上角 <b>放大镜</b> 再发现一次。');
  }
  $('#delPage').disabled = true;
}

document.addEventListener('DOMContentLoaded', main);
