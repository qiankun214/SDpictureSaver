/*
 * pictureSaver 前端逻辑
 * ------------------------------------------------------------------
 * 原生 HTML + 原生 JS（ES2020），无构建步骤、无 npm、无外部网络资源。
 * 所有接口均以 window.location.origin 为基准，使用同源相对路径调用：
 *   GET  /api/health
 *   GET  /api/defaults
 *   GET  /api/browse
 *   POST /api/scan
 *   POST /api/preview
 *   POST /api/jobs
 *   GET  /api/jobs/{job_id}
 *   GET  /api/jobs/{job_id}/events    （SSE，失败时降级为 500ms 轮询）
 *   POST /api/jobs/{job_id}/cancel
 * 进度全部来自后端真实数据，界面不做任何假动画。
 */
'use strict';

(function () {

  /* ================= 常量：接口契约 ================= */

  var ORIGIN = window.location.origin;

  var API = {
    health: '/api/health',
    defaults: '/api/defaults',
    browse: '/api/browse',
    scan: '/api/scan',
    preview: '/api/preview',
    jobs: '/api/jobs',
    job: function (jobId) { return '/api/jobs/' + encodeURIComponent(jobId); },
    events: function (jobId) { return '/api/jobs/' + encodeURIComponent(jobId) + '/events'; },
    cancel: function (jobId) { return '/api/jobs/' + encodeURIComponent(jobId) + '/cancel'; }
  };

  var POLL_INTERVAL_MS = 500;
  var LIST_LIMIT = 500;
  var SUFFIXES = ['jpg', 'arw', 'orf'];
  var ILLEGAL_CHARS = ['<', '>', ':', '"', '/', '\\', '|', '?', '*'];
  var PHASE_LABELS = {
    idle: '等待开始',
    planning: '规划中',
    copying: '拷贝中',
    finished: '已完成',
    canceled: '已取消',
    failed: '失败'
  };
  var POLL_NOTICE = '实时连接已断开，已切换为轮询';
  var BASE_TITLE = 'pictureSaver · 按拍摄日期拷贝照片';
  var TERMINAL_PHASES = ['finished', 'canceled', 'failed'];

  /* ================= 状态 ================= */

  var state = {
    scan: null,
    scanRequest: null,
    plan: null,
    planRequest: null,
    jobId: null,
    jobRunning: false,
    completing: false,
    es: null,
    pollTimer: null,
    pollJobId: null,
    errors: {},
    browse: { mode: 'source', seq: 0, drives: [], path: '', parent: '' }
  };

  var el = {};

  /* ================= 通用工具 ================= */

  function $(id) { return document.getElementById(id); }

  function cacheEls() {
    el.healthDot = $('health-dot');
    el.healthText = $('health-text');
    el.healthVersion = $('health-version');
    el.connectionBanner = $('connection-banner');
    el.connectionBannerText = $('connection-banner-text');
    el.btnReconnectSse = $('btn-reconnect-sse');

    el.configForm = $('config-form');
    el.sourceDir = $('source-dir');
    el.btnBrowseSource = $('btn-browse-source');
    el.sourceDirError = $('source-dir-error');

    el.suffixJpg = $('suffix-jpg');
    el.suffixArw = $('suffix-arw');
    el.suffixOrf = $('suffix-orf');
    el.countJpg = $('count-jpg');
    el.countArw = $('count-arw');
    el.countOrf = $('count-orf');
    el.suffixHint = $('suffix-hint');
    el.suffixError = $('suffix-error');
    el.recursive = $('recursive');

    el.dateFrom = $('date-from');
    el.dateTo = $('date-to');
    el.dateHint = $('date-hint');
    el.dateError = $('date-error');

    el.targetParent = $('target-parent');
    el.btnBrowseTarget = $('btn-browse-target');
    el.targetParentError = $('target-parent-error');

    el.place = $('place');
    el.placeError = $('place-error');
    el.note = $('note');
    el.noteError = $('note-error');

    el.folderPreviewText = $('folder-preview-text');
    el.btnCopyFolder = $('btn-copy-folder');
    el.folderError = $('folder-error');

    el.formError = $('form-error');
    el.actions = $('actions');
    el.btnScan = $('btn-scan');
    el.btnPreview = $('btn-preview');
    el.btnStart = $('btn-start');
    el.btnCancel = $('btn-cancel');

    el.progressCard = $('progress-card');
    el.progressPhase = $('progress-phase');
    el.progressBar = $('progress-bar');
    el.progressFill = $('progress-fill');
    el.progressPercent = $('progress-percent');
    el.progressFiles = $('progress-files');
    el.progressBytes = $('progress-bytes');
    el.progressSpeed = $('progress-speed');
    el.progressEta = $('progress-eta');
    el.progressElapsed = $('progress-elapsed');
    el.progressCurrentName = $('progress-current-name');
    el.progressMessage = $('progress-message');

    el.scanCard = $('scan-card');
    el.scanElapsed = $('scan-elapsed');
    el.sumGroups = $('sum-groups');
    el.sumMatched = $('sum-matched');
    el.sumBytes = $('sum-bytes');
    el.sumWithTime = $('sum-with-time');
    el.sumNoTime = $('sum-no-time');
    el.scanSuffixChips = $('scan-suffix-chips');
    el.scanStale = $('scan-stale');
    el.scanNoTimeDetails = $('scan-no-time-details');
    el.scanNoTimeCount = $('scan-no-time-count');
    el.scanNoTimeList = $('scan-no-time-list');
    el.scanNoTimeMore = $('scan-no-time-more');

    el.planCard = $('plan-card');
    el.planMeta = $('plan-meta');
    el.planTarget = $('plan-target');
    el.planFolder = $('plan-folder');
    el.planFiles = $('plan-files');
    el.planBytes = $('plan-bytes');
    el.planOutOfRange = $('plan-out-of-range');
    el.planNoTime = $('plan-no-time');
    el.planSuffixChips = $('plan-suffix-chips');
    el.planStale = $('plan-stale');
    el.planNoTimeDetails = $('plan-no-time-details');
    el.planNoTimeCount = $('plan-no-time-count');
    el.planNoTimeList = $('plan-no-time-list');
    el.planNoTimeMore = $('plan-no-time-more');

    el.reportCard = $('report-card');
    el.reportStatus = $('report-status');
    el.reportTarget = $('report-target');
    el.reportCopied = $('report-copied');
    el.reportSkipped = $('report-skipped');
    el.reportFailed = $('report-failed');
    el.reportNoTime = $('report-no-time');
    el.reportOutOfRange = $('report-out-of-range');
    el.reportBytes = $('report-bytes');
    el.reportElapsed = $('report-elapsed');
    el.reportMode = $('report-mode');
    el.reportError = $('report-error');
    el.failuresDetails = $('failures-details');
    el.failuresCount = $('failures-count');
    el.failuresList = $('failures-list');
    el.failuresMore = $('failures-more');
    el.reportNoTimeDetails = $('report-no-time-details');
    el.reportNoTimeCount = $('report-no-time-count');
    el.reportNoTimeList = $('report-no-time-list');
    el.reportNoTimeMore = $('report-no-time-more');

    el.liveEmpty = $('live-empty');
    el.footerOrigin = $('footer-origin');

    el.browserModal = $('browser-modal');
    el.browserBackdrop = $('browser-backdrop');
    el.browserTitle = $('browser-title');
    el.browserClose = $('browser-close');
    el.browserUp = $('browser-up');
    el.browserPath = $('browser-path');
    el.browserGo = $('browser-go');
    el.browserError = $('browser-error');
    el.browserDrives = $('browser-drives');
    el.browserList = $('browser-list');
    el.browserLoading = $('browser-loading');
    el.browserCurrent = $('browser-current');
    el.browserCancel = $('browser-cancel');
    el.browserSelect = $('browser-select');

    el.toastStack = $('toast-stack');
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function num(value) {
    var n = Number(value);
    return isFinite(n) ? n : 0;
  }

  function clamp(value, min, max) { return Math.min(max, Math.max(min, value)); }

  function setText(node, text) { if (node) { node.textContent = String(text); } }

  function setHidden(node, hidden) { if (node) { node.hidden = !!hidden; } }

  function setHint(node, message) {
    if (!node) { return; }
    if (message) { node.textContent = message; node.hidden = false; }
    else { node.textContent = ''; node.hidden = true; }
  }

  function markInvalid(node, invalid) {
    if (!node) { return; }
    if (invalid) { node.classList.add('is-invalid'); }
    else { node.classList.remove('is-invalid'); }
  }

  function clearNode(node) { if (node) { while (node.firstChild) { node.removeChild(node.firstChild); } } }

  function absUrl(path) { return new URL(path, ORIGIN).toString(); }

  /* ================= 格式化 ================= */

  function formatBytes(bytes) {
    var n = num(bytes);
    if (n <= 0) { return '0 B'; }
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0;
    var v = n;
    while (v >= 1024 && i < units.length - 1) { v = v / 1024; i += 1; }
    var text = i === 0 ? String(Math.round(v)) : (v >= 100 ? v.toFixed(0) : v.toFixed(1));
    return text + ' ' + units[i];
  }

  function formatSpeed(bps) {
    var n = num(bps);
    if (n <= 0) { return '—'; }
    var mb = n / 1048576;
    return (mb >= 10 ? mb.toFixed(1) : mb.toFixed(2)) + ' MB/s';
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(Number(seconds)) || Number(seconds) < 0) { return '—'; }
    var total = Math.round(Number(seconds));
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    if (h > 0) { return h + ':' + pad2(m) + ':' + pad2(s); }
    return pad2(m) + ':' + pad2(s);
  }

  function formatCount(value) { return num(value).toLocaleString('zh-CN'); }

  function baseName(filePath) {
    if (!filePath) { return '—'; }
    var parts = String(filePath).split(/[\\/]/);
    return parts[parts.length - 1] || String(filePath);
  }

  /* ================= 提示条 / Toast ================= */

  function showToast(message, kind, timeoutMs) {
    if (!message) { return; }
    var type = kind === 'ok' ? 'ok' : (kind === 'error' ? 'err' : 'info');
    var box = document.createElement('div');
    box.className = 'toast toast--' + type;
    box.setAttribute('role', type === 'err' ? 'alert' : 'status');

    var text = document.createElement('span');
    text.className = 'toast__text';
    text.textContent = String(message);

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast__close';
    close.setAttribute('aria-label', '关闭提示');
    close.textContent = '×';
    close.addEventListener('click', function () { removeToast(box); });

    box.appendChild(text);
    box.appendChild(close);
    el.toastStack.appendChild(box);
    while (el.toastStack.children.length > 5) { el.toastStack.removeChild(el.toastStack.firstChild); }
    window.setTimeout(function () { removeToast(box); }, timeoutMs || (type === 'err' ? 9000 : 5000));
  }

  function removeToast(box) {
    if (box && box.parentNode) { box.parentNode.removeChild(box); }
  }

  function setBanner(visible, message) {
    if (message) { setText(el.connectionBannerText, message); }
    setHidden(el.connectionBanner, !visible);
  }

  function setHealth(ok, text, version) {
    el.healthDot.dataset.state = ok ? 'ok' : 'down';
    setText(el.healthText, text);
    setText(el.healthVersion, version || '');
  }

  /* ================= 接口请求 ================= */

  async function request(path, options) {
    var response = await fetch(absUrl(path), options);
    var raw = await response.text();
    var data = null;
    if (raw) {
      try { data = JSON.parse(raw); } catch (err) { data = null; }
    }
    if (!response.ok) {
      var detail = data && typeof data.detail === 'string' ? data.detail : '';
      throw new Error(detail || ('请求失败（HTTP ' + response.status + '）'));
    }
    if (data === null) { throw new Error('后端返回了无法解析的内容'); }
    return data;
  }

  function postJson(path, body) {
    return request(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
  }

  /* ================= 表单状态 ================= */

  function selectedSuffixes() {
    var picked = [];
    SUFFIXES.forEach(function (sfx) {
      var box = sfx === 'jpg' ? el.suffixJpg : (sfx === 'arw' ? el.suffixArw : el.suffixOrf);
      if (box && box.checked) { picked.push(sfx); }
    });
    return picked;
  }

  function sourceDirValue() { return el.sourceDir.value.trim(); }
  function targetParentValue() { return el.targetParent.value.trim(); }
  function placeValue() { return el.place.value.trim(); }
  function noteValue() { return el.note.value.trim(); }

  function findIllegalChars(text) {
    var found = [];
    String(text || '').split('').forEach(function (ch) {
      if (ILLEGAL_CHARS.indexOf(ch) >= 0 && found.indexOf(ch) < 0) { found.push(ch); }
    });
    return found;
  }

  function summarizeIllegal(label, chars) {
    return label + '包含 Windows 非法字符：' + chars.join(' ') + '（不能用于文件夹名）';
  }

  function dayCount(from, to) {
    if (!from || !to) { return 0; }
    var a = Date.parse(from + 'T00:00:00Z');
    var b = Date.parse(to + 'T00:00:00Z');
    if (isNaN(a) || isNaN(b)) { return 0; }
    return Math.round((b - a) / 86400000) + 1;
  }

  function folderName() {
    var from = el.dateFrom.value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(from)) { return ''; }
    return '[' + from.replace(/-/g, '') + ']' + placeValue() + '-' + noteValue();
  }

  function folderPreviewText() {
    var from = el.dateFrom.value;
    var datePart = /^\d{4}-\d{2}-\d{2}$/.test(from) ? from.replace(/-/g, '') : 'YYYYMMDD';
    return '[' + datePart + ']' + (placeValue() || '地点') + '-' + (noteValue() || '备注');
  }

  function buildConfig() {
    return {
      source_dir: sourceDirValue(),
      suffixes: selectedSuffixes(),
      date_from: el.dateFrom.value,
      date_to: el.dateTo.value,
      target_parent: targetParentValue(),
      place: placeValue(),
      note: noteValue(),
      recursive: !!el.recursive.checked
    };
  }

  function collectErrors() {
    var errors = {};

    if (!sourceDirValue()) { errors.source = '请填写源目录（可点击「浏览…」选择）'; }
    if (selectedSuffixes().length === 0) { errors.suffix = '请至少勾选一个后缀'; }

    var from = el.dateFrom.value;
    var to = el.dateTo.value;
    if (!from || !to) { errors.date = '请选择完整的开始与结束日期'; }
    else if (from > to) { errors.date = '开始日期不能晚于结束日期'; }

    if (!targetParentValue()) { errors.target = '请填写目的父文件夹（可点击「浏览…」选择）'; }

    var place = placeValue();
    if (!place) { errors.place = '请填写地点'; }
    else {
      var placeBad = findIllegalChars(place);
      if (placeBad.length > 0) { errors.place = summarizeIllegal('地点', placeBad); }
    }

    var note = noteValue();
    if (!note) { errors.note = '请填写备注'; }
    else {
      var noteBad = findIllegalChars(note);
      if (noteBad.length > 0) { errors.note = summarizeIllegal('备注', noteBad); }
    }

    return errors;
  }

  function renderValidation(options) {
    var opts = options || {};
    state.errors = collectErrors();
    var errors = state.errors;

    setHint(el.sourceDirError, errors.source);
    markInvalid(el.sourceDir, !!errors.source);
    setHint(el.suffixError, errors.suffix);
    setHint(el.dateError, errors.date);
    markInvalid(el.dateFrom, !!errors.date);
    markInvalid(el.dateTo, !!errors.date);
    setHint(el.targetParentError, errors.target);
    markInvalid(el.targetParent, !!errors.target);
    setHint(el.placeError, errors.place);
    markInvalid(el.place, !!errors.place);
    setHint(el.noteError, errors.note);
    markInvalid(el.note, !!errors.note);

    var folderIssue = '';
    if (errors.place && errors.place.indexOf('非法') >= 0) { folderIssue = errors.place; }
    else if (errors.note && errors.note.indexOf('非法') >= 0) { folderIssue = errors.note; }
    setHint(el.folderError, folderIssue);

    var preview = folderPreviewText();
    setText(el.folderPreviewText, preview);
    if (placeValue() && noteValue() && /^\d{4}-\d{2}-\d{2}$/.test(el.dateFrom.value)) {
      el.folderPreviewText.classList.remove('is-placeholder');
    } else {
      el.folderPreviewText.classList.add('is-placeholder');
    }

    var days = dayCount(el.dateFrom.value, el.dateTo.value);
    if (errors.date) { setHint(el.dateHint, ''); }
    else { setHint(el.dateHint, '共 ' + days + ' 天 · 文件夹日期 ' + el.dateFrom.value.replace(/-/g, '')); }

    if (opts.showSummary) {
      var order = ['source', 'suffix', 'date', 'target', 'place', 'note'];
      var first = '';
      for (var i = 0; i < order.length; i += 1) {
        if (errors[order[i]]) { first = errors[order[i]]; break; }
      }
      setHint(el.formError, first);
    } else {
      setHint(el.formError, '');
    }

    renderStale();
    updateActionState();
  }

  function sameStringSet(a, b) {
    if (!Array.isArray(a) || !Array.isArray(b)) { return false; }
    if (a.length !== b.length) { return false; }
    return a.every(function (item) { return b.indexOf(item) >= 0; });
  }

  function renderStale() {
    var scanStale = false;
    if (state.scanRequest) {
      scanStale = state.scanRequest.source_dir !== sourceDirValue() ||
        !sameStringSet(state.scanRequest.suffixes, selectedSuffixes()) ||
        state.scanRequest.recursive !== !!el.recursive.checked;
    }
    setHidden(el.scanStale, !scanStale);

    var planStale = false;
    if (state.planRequest) {
      planStale = state.planRequest.source_dir !== sourceDirValue() ||
        !sameStringSet(state.planRequest.suffixes, selectedSuffixes()) ||
        state.planRequest.date_from !== el.dateFrom.value ||
        state.planRequest.date_to !== el.dateTo.value ||
        state.planRequest.target_parent !== targetParentValue() ||
        state.planRequest.place !== placeValue() ||
        state.planRequest.note !== noteValue() ||
        state.planRequest.recursive !== !!el.recursive.checked;
    }
    setHidden(el.planStale, !planStale);
  }

  function updateActionState() {
    var busy = el.btnScan.classList.contains('is-loading') ||
      el.btnPreview.classList.contains('is-loading') ||
      el.btnStart.classList.contains('is-loading');
    var cancelBusy = el.btnCancel.classList.contains('is-loading');
    var running = state.jobRunning;
    var configOk = Object.keys(state.errors).length === 0;
    var scanOk = !!sourceDirValue() && selectedSuffixes().length > 0;

    el.btnScan.disabled = busy || running || !scanOk;
    el.btnPreview.disabled = busy || running || !configOk;
    el.btnStart.disabled = busy || running || !configOk;
    el.btnCancel.disabled = !running || cancelBusy;
    el.actions.classList.toggle('is-running', running);
  }

  function setLoading(button, loading, loadingText) {
    if (!button) { return; }
    if (loading) {
      if (!button.dataset.label) { button.dataset.label = button.textContent; }
      button.textContent = loadingText;
      button.classList.add('is-loading');
      button.disabled = true;
    } else {
      if (button.dataset.label) { button.textContent = button.dataset.label; }
      button.classList.remove('is-loading');
    }
  }

  /* ================= 目录浏览器 ================= */

  function openBrowser(mode) {
    state.browse.mode = mode;
    setText(el.browserTitle, mode === 'source' ? '选择源目录（SD 卡 / 相机存储卡）' : '选择目的父文件夹');
    setHidden(el.browserModal, false);
    setHidden(el.browserError, true);
    var start = mode === 'source' ? sourceDirValue() : targetParentValue();
    loadBrowse(start || '');
    window.setTimeout(function () { el.browserPath.focus(); }, 0);
  }

  function closeBrowser() {
    setHidden(el.browserModal, true);
    state.browse.seq += 1;
  }

  async function loadBrowse(path) {
    state.browse.seq += 1;
    var seq = state.browse.seq;
    setHidden(el.browserLoading, false);
    setHint(el.browserError, '');
    var target = path ? API.browse + '?path=' + encodeURIComponent(path) : API.browse;
    try {
      var data = await request(target);
      if (seq !== state.browse.seq) { return; }
      renderBrowser(data);
    } catch (err) {
      if (seq !== state.browse.seq) { return; }
      setHint(el.browserError, err.message);
      // 目录列表不可用时仍允许手动输入路径，避免弹窗失去操作能力
      setText(el.browserCurrent, '当前目录：—（读取失败，可在上方手动输入路径后点「转到」）');
    } finally {
      if (seq === state.browse.seq) { setHidden(el.browserLoading, true); }
    }
  }

  function isDriveEntry(entry) { return /^[A-Za-z]:$/.test(String(entry.name || '')); }

  function renderBrowser(data) {
    var currentPath = data && data.path ? data.path : '';
    var parent = data && data.parent ? data.parent : '';
    var dirs = data && Array.isArray(data.dirs) ? data.dirs : [];

    state.browse.path = currentPath;
    state.browse.parent = parent;
    el.browserPath.value = currentPath;
    el.browserUp.disabled = !parent;
    el.browserSelect.disabled = !currentPath;
    setText(el.browserCurrent, '当前目录：' + (currentPath || '（驱动器列表，请先选择磁盘）'));
    setHint(el.browserError, data && data.error ? data.error : '');

    dirs.forEach(function (entry) {
      if (!isDriveEntry(entry)) { return; }
      var exists = state.browse.drives.some(function (d) { return d.path === entry.path; });
      if (!exists) { state.browse.drives.push(entry); }
    });

    clearNode(el.browserDrives);
    state.browse.drives.forEach(function (entry) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'drive-chip' + (entry.path === currentPath ? ' is-current' : '');
      chip.textContent = entry.name;
      chip.title = entry.path;
      chip.addEventListener('click', function () { loadBrowse(entry.path); });
      el.browserDrives.appendChild(chip);
    });

    clearNode(el.browserList);
    if (!currentPath) {
      appendBrowserPlaceholder('请选择一个驱动器，或直接输入路径后点「转到」');
      return;
    }
    if (dirs.length === 0) {
      appendBrowserPlaceholder('该目录下没有子目录，可直接「选择此目录」');
      return;
    }
    dirs.forEach(function (entry) {
      var item = document.createElement('li');
      item.className = 'browser-item';
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'browser-item__btn';
      button.title = entry.path;

      var icon = document.createElement('span');
      icon.className = 'browser-item__icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = '▸';

      var name = document.createElement('span');
      name.className = 'browser-item__name';
      name.textContent = entry.name;

      button.appendChild(icon);
      button.appendChild(name);
      button.addEventListener('click', function () { loadBrowse(entry.path); });
      item.appendChild(button);
      el.browserList.appendChild(item);
    });
  }

  function appendBrowserPlaceholder(text) {
    var item = document.createElement('li');
    item.className = 'browser-item browser-item--empty';
    item.textContent = text;
    el.browserList.appendChild(item);
  }

  function confirmBrowser() {
    var picked = el.browserPath.value.trim() || state.browse.path;
    if (!picked) { showToast('请先选择一个目录', 'error'); return; }
    if (state.browse.mode === 'source') { el.sourceDir.value = picked; }
    else { el.targetParent.value = picked; }
    closeBrowser();
    renderValidation();
  }

  /* ================= 扫描 ================= */

  async function handleScan() {
    if (el.btnScan.disabled) { return; }
    setLoading(el.btnScan, true, '扫描中…');
    renderValidation();
    showToast('正在扫描源目录并读取 EXIF 拍摄时间…', 'info');
    try {
      var data = await postJson(API.scan, {
        source_dir: sourceDirValue(),
        suffixes: selectedSuffixes(),
        recursive: !!el.recursive.checked
      });
      state.scan = data;
      state.scanRequest = {
        source_dir: sourceDirValue(),
        suffixes: selectedSuffixes(),
        recursive: !!el.recursive.checked
      };
      renderScan(data);
      updateLiveEmpty();
      showToast('扫描完成：命中 ' + formatCount(data.matched_files) + ' 个文件，配对 ' + formatCount(data.groups) + ' 张照片', 'ok');
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      setLoading(el.btnScan, false);
      renderValidation();
    }
  }

  function makeChip(label, value) {
    var chip = document.createElement('span');
    chip.className = 'chip';
    var name = document.createElement('span');
    name.className = 'chip__label';
    name.textContent = label;
    var val = document.createElement('span');
    val.className = 'chip__value';
    val.textContent = value;
    chip.appendChild(name);
    chip.appendChild(val);
    return chip;
  }

  function renderSuffixChips(container, counts, dirs) {
    clearNode(container);
    if (counts && typeof counts === 'object') {
      Object.keys(counts).forEach(function (key) {
        container.appendChild(makeChip(String(key).toUpperCase(), formatCount(counts[key]) + ' 个'));
      });
      if (Object.keys(counts).length === 0) { container.appendChild(makeChip('无命中', '0')); }
      return;
    }
    if (Array.isArray(dirs)) {
      if (dirs.length === 0) { container.appendChild(makeChip('无目标子目录', '—')); return; }
      dirs.forEach(function (dir) { container.appendChild(makeChip(String(dir), '子目录')); });
    }
  }

  function renderScan(data) {
    setHidden(el.scanCard, false);
    state.scan = data;
    setText(el.sumGroups, formatCount(data.groups));
    setText(el.sumMatched, formatCount(data.matched_files));
    setText(el.sumBytes, formatBytes(data.total_bytes));
    setText(el.sumWithTime, formatCount(data.with_time));
    setText(el.sumNoTime, formatCount(data.no_time));
    setText(el.scanElapsed, '耗时 ' + num(data.elapsed_seconds).toFixed(2) + ' 秒');

    var counts = data.suffix_counts || {};
    SUFFIXES.forEach(function (sfx) {
      var node = sfx === 'jpg' ? el.countJpg : (sfx === 'arw' ? el.countArw : el.countOrf);
      var value = counts[sfx];
      if (value === undefined || value === null) { setHidden(node, true); }
      else { setText(node, formatCount(value)); setHidden(node, false); }
    });
    setText(el.suffixHint, '扫描结果：' + SUFFIXES.map(function (sfx) {
      return sfx + ' ' + formatCount(counts[sfx] || 0);
    }).join(' · '));

    renderSuffixChips(el.scanSuffixChips, counts);

    var noTime = Array.isArray(data.no_time_files) ? data.no_time_files : [];
    setHidden(el.scanNoTimeDetails, noTime.length === 0);
    setText(el.scanNoTimeCount, formatCount(noTime.length));
    renderFileList(el.scanNoTimeList, el.scanNoTimeMore, noTime);

    renderStale();
  }

  /* ================= 预览计划 ================= */

  async function handlePreview() {
    if (el.btnPreview.disabled) { return; }
    setLoading(el.btnPreview, true, '计算中…');
    renderValidation({ showSummary: true });
    try {
      var data = await postJson(API.preview, buildConfig());
      state.plan = data;
      state.planRequest = buildConfig();
      renderPlan(data);
      updateLiveEmpty();
      showToast('计划已生成：' + data.folder_name + '（' + formatCount(data.planned_files) + ' 个文件）', 'ok');
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      setLoading(el.btnPreview, false);
      renderValidation();
    }
  }

  function renderPlan(data) {
    setHidden(el.planCard, false);
    setText(el.planMeta, '未写入磁盘');
    setText(el.planFolder, data.folder_name || '—');
    setText(el.planTarget, data.target_dir || '—');
    el.planTarget.title = data.target_dir ? ('点击复制：' + data.target_dir) : '';
    setText(el.planFiles, formatCount(data.planned_files));
    setText(el.planBytes, formatBytes(data.planned_bytes));
    setText(el.planOutOfRange, formatCount(data.out_of_range_files));
    setText(el.planNoTime, formatCount(data.no_time_files));
    renderSuffixChips(el.planSuffixChips, null, data.suffix_dirs);

    var noTime = Array.isArray(data.no_time) ? data.no_time : [];
    setHidden(el.planNoTimeDetails, noTime.length === 0);
    setText(el.planNoTimeCount, formatCount(noTime.length));
    renderFileList(el.planNoTimeList, el.planNoTimeMore, noTime);
    renderStale();
  }

  /* ================= 任务：开始 / 进度 / 取消 ================= */

  async function handleStart() {
    if (el.btnStart.disabled) { return; }
    renderValidation({ showSummary: true });
    if (Object.keys(state.errors).length > 0) { showToast('配置不完整，请先修正左侧提示', 'error'); return; }

    setLoading(el.btnStart, true, '启动中…');
    renderValidation();
    try {
      var job = await postJson(API.jobs, { config: buildConfig(), dry_run: false });
      state.jobId = job.job_id;
      state.jobRunning = true;
      state.completing = false;
      setHidden(el.reportCard, true);
      setHidden(el.reportError, true);
      setHidden(el.failuresDetails, true);
      setHidden(el.reportNoTimeDetails, true);
      resetProgress();
      setPhase('planning');
      updateActionState();
      updateLiveEmpty();
      if (el.progressCard.scrollIntoView) {
        el.progressCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      showToast('任务已启动，正在规划拷贝计划…', 'info');
      connectEvents(job.job_id);
    } catch (err) {
      showToast(err.message, 'error');
      state.jobRunning = false;
    } finally {
      setLoading(el.btnStart, false);
      renderValidation();
    }
  }

  async function handleCancel() {
    if (!state.jobId || !state.jobRunning) { return; }
    setLoading(el.btnCancel, true, '取消中…');
    updateActionState();
    try {
      var res = await postJson(API.cancel(state.jobId), { job_id: state.jobId, canceled: true });
      if (res && res.canceled === false) { showToast('任务已结束，无需取消', 'info'); }
      else { showToast('已发送取消请求：已复制的文件会保留', 'info'); }
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      setLoading(el.btnCancel, false);
      updateActionState();
    }
  }

  function connectEvents(jobId) {
    closeEvents();
    stopPolling();
    setBanner(false);
    var source;
    try {
      source = new EventSource(absUrl(API.events(jobId)));
    } catch (err) {
      setBanner(true, POLL_NOTICE);
      startPolling(jobId);
      return;
    }
    state.es = source;

    source.onopen = function () {
      if (state.jobId !== jobId || !state.jobRunning) { return; }
      setBanner(false);
      stopPolling();
    };

    source.onmessage = function (event) {
      if (state.jobId !== jobId || !state.jobRunning) { return; }
      var data = null;
      try { data = JSON.parse(event.data); } catch (err) { data = null; }
      if (!data) { return; }
      var phase = data.phase || '';
      // 终局事件（finished / canceled / failed）不携带进度字段，直接取最终状态，避免进度被清零
      if (TERMINAL_PHASES.indexOf(phase) >= 0) {
        completeJob(jobId);
        return;
      }
      if (hasProgressFields(data)) { renderProgress(normalizeProgress(data)); }
    };

    source.onerror = function () {
      if (state.jobId !== jobId || !state.jobRunning) { return; }
      // EventSource 会自动重连；同时立刻降级为轮询，避免进度停滞。
      setBanner(true, POLL_NOTICE);
      startPolling(jobId);
      if (source.readyState === EventSource.CLOSED) { state.es = null; }
    };
  }

  function closeEvents() {
    if (state.es) {
      try { state.es.close(); } catch (err) { /* 忽略关闭异常 */ }
      state.es = null;
    }
  }

  function startPolling(jobId) {
    if (state.pollJobId === jobId && state.pollTimer !== null) { return; }
    stopPolling();
    state.pollJobId = jobId;
    pollOnce(jobId);
    state.pollTimer = window.setInterval(function () { pollOnce(jobId); }, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (state.pollTimer !== null) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    state.pollJobId = null;
  }

  async function pollOnce(jobId) {
    if (state.jobId !== jobId || !state.jobRunning) { stopPolling(); return; }
    try {
      var data = await request(API.job(jobId));
      if (state.jobId !== jobId) { return; }
      applyJobSnapshot(data, jobId);
    } catch (err) {
      // 网络抖动：保持轮询，不打断界面
    }
  }

  function applyJobSnapshot(data, jobId) {
    var progress = normalizeProgress(data.progress);
    if (progress) { renderProgress(progress); }
    var status = data.status || '';
    var phase = data.progress && data.progress.phase ? data.progress.phase : '';
    var terminal = status === 'done' || status === 'failed' || status === 'canceled' ||
      TERMINAL_PHASES.indexOf(phase) >= 0;
    if (terminal) { completeJob(jobId, data); }
  }

  function hasProgressFields(data) {
    var keys = ['total_files', 'done_files', 'total_bytes', 'done_bytes', 'percent'];
    return keys.some(function (key) { return data[key] !== undefined && data[key] !== null; });
  }

  async function completeJob(jobId, snapshot) {
    if (state.jobId !== jobId || state.completing || !state.jobRunning) { return; }
    state.completing = true;
    closeEvents();
    stopPolling();
    setBanner(false);

    var data = snapshot || null;
    if (!data) {
      try { data = await request(API.job(jobId)); } catch (err) { data = null; }
    }

    state.jobRunning = false;
    state.completing = false;
    updateActionState();

    if (!data) {
      setPhase('failed');
      showToast('无法读取任务最终状态，请刷新页面确认结果', 'error');
      return;
    }

    renderProgress(normalizeProgress(data.progress));
    renderReport(data);
    updateLiveEmpty();

    var status = data.status || '';
    if (status === 'done') {
      setPhase('finished');
      var copied = data.report ? num(data.report.copied_files) : 0;
      showToast('任务完成：成功复制 ' + formatCount(copied) + ' 个文件', 'ok');
    } else if (status === 'canceled') {
      setPhase('canceled');
      showToast('任务已取消，已复制的文件保留', 'info');
    } else if (status === 'failed') {
      setPhase('failed');
      showToast('任务失败：' + (data.error || '未知错误'), 'error');
    } else {
      setPhase('finished');
    }
  }

  function normalizeProgress(source) {
    if (!source) { return null; }
    var p = source.progress && typeof source.progress === 'object' ? source.progress : source;
    var totalBytes = num(p.total_bytes);
    var doneBytes = num(p.done_bytes);
    var percent = p.percent === null || p.percent === undefined
      ? (totalBytes > 0 ? (doneBytes / totalBytes) * 100 : 0)
      : num(p.percent);
    return {
      phase: p.phase || source.phase || '',
      total_files: num(p.total_files),
      done_files: num(p.done_files),
      total_bytes: totalBytes,
      done_bytes: doneBytes,
      current_file: p.current_file || '',
      percent: percent,
      speed_bps: num(p.speed_bps),
      eta_seconds: p.eta_seconds === null || p.eta_seconds === undefined ? null : num(p.eta_seconds),
      elapsed_seconds: p.elapsed_seconds === null || p.elapsed_seconds === undefined ? null : num(p.elapsed_seconds),
      message: p.message || ''
    };
  }

  function resetProgress() {
    el.progressFill.style.width = '0%';
    el.progressBar.setAttribute('aria-valuenow', '0');
    setText(el.progressPercent, '0.0%');
    setText(el.progressFiles, '0 / 0 个文件');
    setText(el.progressBytes, '0 B / 0 B');
    setText(el.progressSpeed, '—');
    setText(el.progressEta, '—');
    setText(el.progressElapsed, '00:00');
    setText(el.progressCurrentName, '等待开始');
    el.progressCurrentName.title = '';
    setHint(el.progressMessage, '');
    setPhase('idle');
  }

  function setPhase(phase) {
    var key = PHASE_LABELS[phase] ? phase : 'idle';
    el.progressPhase.dataset.phase = key;
    setText(el.progressPhase, PHASE_LABELS[key]);
  }

  function renderProgress(p) {
    if (!p) { resetProgress(); return; }
    var percent = clamp(num(p.percent), 0, 100);
    el.progressFill.style.width = percent.toFixed(2) + '%';
    el.progressBar.setAttribute('aria-valuenow', percent.toFixed(1));
    setText(el.progressPercent, percent.toFixed(1) + '%');
    setText(el.progressFiles, formatCount(p.done_files) + ' / ' + formatCount(p.total_files) + ' 个文件');
    setText(el.progressBytes, formatBytes(p.done_bytes) + ' / ' + formatBytes(p.total_bytes));
    setText(el.progressSpeed, formatSpeed(p.speed_bps));
    setText(el.progressEta, formatDuration(p.eta_seconds));
    setText(el.progressElapsed, formatDuration(p.elapsed_seconds));

    if (p.current_file) {
      setText(el.progressCurrentName, baseName(p.current_file));
      el.progressCurrentName.title = p.current_file;
    } else {
      setText(el.progressCurrentName, p.phase === 'planning' ? '正在规划拷贝计划…' : '等待开始');
      el.progressCurrentName.title = '';
    }

    setHint(el.progressMessage, p.message);
    if (p.phase) { setPhase(p.phase); }
    document.title = (p.phase === 'copying' || p.phase === 'planning')
      ? (percent.toFixed(1) + '% · ' + BASE_TITLE)
      : BASE_TITLE;
  }

  /* ================= 报告 ================= */

  function renderReport(job) {
    setHidden(el.reportCard, false);
    var report = job.report || null;
    var status = job.status || '';

    var label = '已结束';
    var phase = 'finished';
    if (status === 'failed') { label = '失败'; phase = 'failed'; }
    else if (status === 'canceled' || (report && report.canceled)) { label = '已取消'; phase = 'canceled'; }
    else if (status === 'done') { label = '已完成'; phase = 'finished'; }
    el.reportStatus.dataset.phase = phase;
    setText(el.reportStatus, label);

    if (job.error) { setHint(el.reportError, '任务错误：' + job.error); }
    else { setHint(el.reportError, ''); }

    if (!report) {
      setText(el.reportTarget, '—');
      setText(el.reportCopied, '—');
      setText(el.reportSkipped, '—');
      setText(el.reportFailed, '—');
      setText(el.reportNoTime, '—');
      setText(el.reportOutOfRange, '—');
      setText(el.reportBytes, '—');
      setText(el.reportElapsed, '—');
      setText(el.reportMode, '—');
      setHidden(el.failuresDetails, true);
      setHidden(el.reportNoTimeDetails, true);
      return;
    }

    setText(el.reportTarget, report.target_dir || '—');
    el.reportTarget.title = report.target_dir ? ('点击复制：' + report.target_dir) : '';
    setText(el.reportCopied, formatCount(report.copied_files));
    setText(el.reportSkipped, formatCount(report.skipped_existing));
    setText(el.reportFailed, formatCount(report.failed_files));
    setText(el.reportNoTime, formatCount(report.no_time_files));
    setText(el.reportOutOfRange, formatCount(report.out_of_range_files));
    setText(el.reportBytes, formatBytes(report.copied_bytes) + ' / ' + formatBytes(report.total_bytes));
    setText(el.reportElapsed, formatDuration(report.elapsed_seconds));
    setText(el.reportMode, report.dry_run ? '演练（未落盘）' : '实际拷贝');

    var failures = Array.isArray(report.failures) ? report.failures : [];
    setHidden(el.failuresDetails, failures.length === 0);
    setText(el.failuresCount, formatCount(failures.length));
    renderFailureList(failures);

    var noTime = Array.isArray(report.no_time) ? report.no_time : [];
    setHidden(el.reportNoTimeDetails, noTime.length === 0);
    setText(el.reportNoTimeCount, formatCount(noTime.length));
    renderFileList(el.reportNoTimeList, el.reportNoTimeMore, noTime);
  }

  function renderFileList(listNode, moreNode, items) {
    clearNode(listNode);
    var list = Array.isArray(items) ? items : [];
    list.slice(0, LIST_LIMIT).forEach(function (item) {
      var li = document.createElement('li');
      li.className = 'list-item';

      var path = document.createElement('span');
      path.className = 'list-item__path';
      path.textContent = item.path || '(未知路径)';
      path.title = item.path || '';
      li.appendChild(path);

      if (item.suffix) {
        var sfx = document.createElement('span');
        sfx.className = 'list-item__suffix';
        sfx.textContent = String(item.suffix).toUpperCase();
        li.appendChild(sfx);
      }
      if (item.reason) {
        var reason = document.createElement('span');
        reason.className = 'list-item__reason';
        reason.textContent = item.reason;
        li.appendChild(reason);
      }
      listNode.appendChild(li);
    });
    renderMore(moreNode, list.length);
  }

  function renderFailureList(items) {
    clearNode(el.failuresList);
    var list = Array.isArray(items) ? items : [];
    list.slice(0, LIST_LIMIT).forEach(function (item) {
      var li = document.createElement('li');
      li.className = 'list-item';

      var path = document.createElement('span');
      path.className = 'list-item__path';
      path.textContent = item.source || '(未知来源)';
      path.title = item.source || '';
      li.appendChild(path);

      var reason = document.createElement('span');
      reason.className = 'list-item__reason';
      reason.textContent = (item.error || '未知错误') + (item.target ? ' → ' + item.target : '');
      li.appendChild(reason);
      el.failuresList.appendChild(li);
    });
    renderMore(el.failuresMore, list.length);
  }

  function renderMore(moreNode, total) {
    if (total > LIST_LIMIT) {
      moreNode.textContent = '已展示前 ' + LIST_LIMIT + ' 条，共 ' + formatCount(total) +
        ' 条，还有 ' + formatCount(total - LIST_LIMIT) + ' 条未显示';
      moreNode.hidden = false;
    } else {
      moreNode.textContent = '';
      moreNode.hidden = true;
    }
  }

  function updateLiveEmpty() {
    var anyVisible = !el.scanCard.hidden || !el.planCard.hidden || !el.reportCard.hidden || state.jobRunning;
    setHidden(el.liveEmpty, anyVisible);
  }

  /* ================= 复制路径 ================= */

  async function copyText(text) {
    if (!text) { return false; }
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (err) { /* 继续尝试兜底方案 */ }
    try {
      var area = document.createElement('textarea');
      area.className = 'copy-textarea';
      area.value = text;
      area.setAttribute('readonly', 'readonly');
      document.body.appendChild(area);
      area.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(area);
      return ok;
    } catch (err) {
      return false;
    }
  }

  async function copyAndToast(text, label) {
    if (!text || text === '—') { showToast('暂无可复制的路径', 'info'); return; }
    var ok = await copyText(text);
    if (ok) { showToast(label + '已复制到剪贴板', 'ok'); }
    else { showToast('复制失败，请手动选择文本复制', 'error'); }
  }

  /* ================= 默认值与初始化 ================= */

  function padDate(date) {
    return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate());
  }

  function shiftIsoDate(iso, days) {
    var parts = String(iso).split('-');
    var d = new Date(Date.UTC(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2])));
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  }

  async function loadHealth() {
    try {
      var data = await request(API.health);
      setHealth(data.status === 'ok', data.status === 'ok' ? '后端已连接' : ('后端状态：' + data.status), data.version ? ('v' + data.version) : '');
    } catch (err) {
      setHealth(false, '后端未连接（请确认服务已启动）', '');
    }
  }

  async function loadDefaults() {
    var today = padDate(new Date());
    try {
      var data = await request(API.defaults);
      if (data.today && /^\d{4}-\d{2}-\d{2}$/.test(data.today)) { today = data.today; }
      if (data.source_dir) { el.sourceDir.value = data.source_dir; }
      if (data.target_parent) { el.targetParent.value = data.target_parent; }
      if (Array.isArray(data.suffixes) && data.suffixes.length > 0) {
        SUFFIXES.forEach(function (sfx) {
          var box = sfx === 'jpg' ? el.suffixJpg : (sfx === 'arw' ? el.suffixArw : el.suffixOrf);
          box.checked = data.suffixes.indexOf(sfx) >= 0;
        });
      }
    } catch (err) {
      showToast('读取默认配置失败：' + err.message, 'error');
    }
    el.dateTo.value = today;
    el.dateFrom.value = shiftIsoDate(today, -7);
  }

  function bindEvents() {
    el.configForm.addEventListener('submit', function (event) { event.preventDefault(); });

    [el.sourceDir, el.targetParent, el.place, el.note, el.dateFrom, el.dateTo].forEach(function (node) {
      node.addEventListener('input', function () { renderValidation(); });
      node.addEventListener('change', function () { renderValidation(); });
    });

    SUFFIXES.forEach(function (sfx) {
      var box = sfx === 'jpg' ? el.suffixJpg : (sfx === 'arw' ? el.suffixArw : el.suffixOrf);
      box.addEventListener('change', function () { renderValidation(); });
    });
    el.recursive.addEventListener('change', function () { renderValidation(); });

    el.btnBrowseSource.addEventListener('click', function () { openBrowser('source'); });
    el.btnBrowseTarget.addEventListener('click', function () { openBrowser('target'); });
    el.btnScan.addEventListener('click', handleScan);
    el.btnPreview.addEventListener('click', handlePreview);
    el.btnStart.addEventListener('click', handleStart);
    el.btnCancel.addEventListener('click', handleCancel);
    el.btnReconnectSse.addEventListener('click', function () {
      if (!state.jobId || !state.jobRunning) { showToast('当前没有运行中的任务', 'info'); return; }
      showToast('正在重试实时连接…', 'info');
      connectEvents(state.jobId);
    });

    el.btnCopyFolder.addEventListener('click', function () { copyAndToast(folderName(), '文件夹名'); });
    el.planTarget.addEventListener('click', function () { copyAndToast(el.planTarget.textContent, '目标目录'); });
    el.reportTarget.addEventListener('click', function () { copyAndToast(el.reportTarget.textContent, '目标目录'); });

    el.browserClose.addEventListener('click', closeBrowser);
    el.browserCancel.addEventListener('click', closeBrowser);
    el.browserBackdrop.addEventListener('click', closeBrowser);
    el.browserSelect.addEventListener('click', confirmBrowser);
    el.browserGo.addEventListener('click', function () { loadBrowse(el.browserPath.value.trim()); });
    el.browserPath.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); loadBrowse(el.browserPath.value.trim()); }
    });
    el.browserUp.addEventListener('click', function () {
      if (!state.browse.path) { return; }
      loadBrowse(state.browse.parent || '');
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !el.browserModal.hidden) { closeBrowser(); }
    });

    window.addEventListener('beforeunload', function () {
      closeEvents();
      stopPolling();
    });
  }

  async function init() {
    cacheEls();
    setText(el.footerOrigin, ORIGIN);
    bindEvents();
    resetProgress();
    updateLiveEmpty();
    await loadDefaults();
    renderValidation();
    loadHealth();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
