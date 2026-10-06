/*
 * pictureSaver 前端逻辑
 * ------------------------------------------------------------------
 * 原生 HTML + 原生 JS（ES2020），无构建步骤、无 npm、无外部网络资源。
 * 所有接口均以 window.location.origin 为基准，使用同源相对路径调用：
 *   GET  /api/health
 *   GET  /api/defaults
 *   GET  /api/browse                  （只返回文件夹；含 truncated / total_dirs / elapsed_ms）
 *   POST /api/mkdir                   （在指定父目录下新建文件夹）
 *   POST /api/jobs                    （mode = scan | preview | copy，扫描 / 预览 / 拷贝统一走任务）
 *   GET  /api/jobs/{job_id}
 *   GET  /api/jobs/{job_id}/events    （SSE，失败时降级为 500ms 轮询）
 *   POST /api/jobs/{job_id}/cancel
 * 扫描与预览不再调用同步接口 POST /api/scan、POST /api/preview，以便显示真实扫描进度。
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
    mkdir: '/api/mkdir',
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
    counting: '统计中',
    scanning: '扫描中',
    planning: '规划中',
    copying: '拷贝中',
    finished: '已完成',
    canceled: '已取消',
    failed: '失败'
  };
  //: 任务模式 → 界面文案（三种模式共用一条进度条）
  var JOB_MODES = {
    scan: { loading: '扫描中…', startToast: '正在扫描源目录并读取 EXIF 拍摄时间…', phase: 'counting' },
    preview: { loading: '计算中…', startToast: '正在扫描源目录并计算拷贝计划…', phase: 'counting' },
    copy: { loading: '启动中…', startToast: '任务已启动，正在扫描源目录…', phase: 'counting' }
  };
  var POLL_NOTICE = '实时连接已断开，已切换为轮询';
  var BASE_TITLE = 'pictureSaver · 按拍摄日期拷贝照片';
  var TERMINAL_PHASES = ['finished', 'canceled', 'failed'];
  //: 终局状态（SSE 最后一条事件带 status；进度事件不带 status 字段）
  var TERMINAL_STATUSES = ['done', 'canceled', 'failed'];
  //: 扫描类阶段（进度按已扫描 / 待扫描文件数换算）
  var SCAN_PHASES = ['counting', 'scanning'];

  /* ================= 状态 ================= */

  var state = {
    scan: null,
    scanRequest: null,
    plan: null,
    planRequest: null,
    jobId: null,
    jobMode: '',
    jobRunning: false,
    completing: false,
    es: null,
    pollTimer: null,
    pollJobId: null,
    errors: {},
    browse: { mode: 'source', seq: 0, drives: [], path: '', parent: '', busy: false }
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
    el.configFields = $('config-fields');
    el.configLockNotice = $('config-lock-notice');
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
    el.progressBytesLabel = $('progress-bytes-label');
    el.progressBytes = $('progress-bytes');
    el.progressScanFiles = $('progress-scan-files');
    el.progressScanLabel = $('progress-scan-label');
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
    el.browserMeta = $('browser-meta');
    el.browserNewName = $('browser-new-name');
    el.browserMkdir = $('browser-mkdir');
    el.browserNewHint = $('browser-new-hint');
    el.browserNewError = $('browser-new-error');
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

  /**
   * 提取后端错误信息：
   * - 业务错误 / HTTPException：``detail`` 是中文字符串；
   * - pydantic 校验失败（HTTP 422）：``detail`` 是 ``[{loc, msg, type}]`` 数组，需要拼成人话。
   */
  function errorMessage(response, data) {
    var detail = data ? data.detail : null;
    if (typeof detail === 'string' && detail) { return detail; }
    if (Array.isArray(detail)) {
      var parts = [];
      detail.forEach(function (item) {
        if (item && typeof item.msg === 'string' && item.msg) {
          var loc = Array.isArray(item.loc)
            ? item.loc.filter(function (part) { return part !== 'body' && part !== 'query'; }).join('.')
            : '';
          parts.push(loc ? (loc + '：' + item.msg) : item.msg);
        } else if (typeof item === 'string' && item) {
          parts.push(item);
        }
      });
      if (parts.length > 0) { return parts.join('；'); }
    }
    if (detail && typeof detail === 'object' && typeof detail.msg === 'string') { return detail.msg; }
    return '请求失败（HTTP ' + response.status + '）';
  }

  async function request(path, options) {
    var response = await fetch(absUrl(path), options);
    var raw = await response.text();
    var data = null;
    if (raw) {
      try { data = JSON.parse(raw); } catch (err) { data = null; }
    }
    if (!response.ok) {
      throw new Error(errorMessage(response, data));
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
    el.configFields.classList.toggle('is-locked', running);
  }

  /** 任务运行期间锁定整个配置区（fieldset.disabled 会连子元素一起禁用）。 */
  function setConfigLocked(locked) {
    el.configFields.disabled = !!locked;
    setHidden(el.configLockNotice, !locked);
  }

  /** 统一维护「任务运行中」这一状态：配置区锁定 + 按钮可用性。 */
  function setJobRunning(running) {
    state.jobRunning = !!running;
    setConfigLocked(state.jobRunning);
    updateActionState();
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
    setHint(el.browserError, '');
    setHint(el.browserNewError, '');
    el.browserNewName.value = '';
    updateMkdirState();
    var start = mode === 'source' ? sourceDirValue() : targetParentValue();
    loadBrowse(start || '');
    window.setTimeout(function () { el.browserPath.focus(); }, 0);
  }

  function closeBrowser() {
    setHidden(el.browserModal, true);
    state.browse.seq += 1;
  }

  /** 没有可用的当前目录时不允许新建文件夹。 */
  function updateMkdirState() {
    var blocked = state.browse.busy || !state.browse.path;
    el.browserMkdir.disabled = blocked;
    el.browserNewName.disabled = state.browse.busy;
  }

  async function loadBrowse(path) {
    state.browse.seq += 1;
    var seq = state.browse.seq;
    state.browse.busy = true;
    updateMkdirState();
    setHidden(el.browserLoading, false);
    setHint(el.browserError, '');
    setHint(el.browserNewError, '');
    var target = path ? API.browse + '?path=' + encodeURIComponent(path) : API.browse;
    try {
      var data = await request(target);
      if (seq !== state.browse.seq) { return; }
      renderBrowser(data);
    } catch (err) {
      if (seq !== state.browse.seq) { return; }
      setHint(el.browserError, err.message);
      setHint(el.browserMeta, '');
      // 目录列表不可用时仍允许手动输入路径，避免弹窗失去操作能力
      setText(el.browserCurrent, '当前目录：—（读取失败，可在上方手动输入路径后点「转到」）');
    } finally {
      if (seq === state.browse.seq) {
        state.browse.busy = false;
        setHidden(el.browserLoading, true);
        updateMkdirState();
      }
    }
  }

  /** 目录读取信息：总数 / 截断提示 / 后端读取耗时。 */
  function renderBrowseMeta(data) {
    if (!data || data.error) { setHint(el.browserMeta, ''); return; }
    var total = data.total_dirs === null || data.total_dirs === undefined ? null : num(data.total_dirs);
    var truncated = !!data.truncated;
    var elapsed = data.elapsed_ms === null || data.elapsed_ms === undefined ? null : num(data.elapsed_ms);
    if (total === null && !truncated && elapsed === null) { setHint(el.browserMeta, ''); return; }
    var parts = [];
    if (total !== null) {
      if (truncated) { parts.push('共 ' + formatCount(total) + ' 个文件夹，仅显示前 2000 个'); }
      else { parts.push('共 ' + formatCount(total) + ' 个文件夹'); }
    } else if (truncated) {
      parts.push('文件夹过多，仅显示前 2000 个');
    }
    if (elapsed !== null) { parts.push('读取耗时 ' + elapsed + ' ms'); }
    setHint(el.browserMeta, parts.join(' · '));
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
    renderBrowseMeta(data);
    updateMkdirState();

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

  /* ================= 新建文件夹（弹窗内联） ================= */

  function validateNewFolderName(name) {
    if (!name) { return '请输入新文件夹名称'; }
    if (name !== name.trim()) { return '名称首尾不能是空格'; }
    if (/^\./.test(name) || /\.$/.test(name)) { return '名称首尾不能是点（.）'; }
    if (name.length > 100) { return '名称过长（最多 100 个字符）'; }
    var bad = findIllegalChars(name);
    if (bad.length > 0) { return '名称包含 Windows 非法字符：' + bad.join(' '); }
    return '';
  }

  async function handleMkdir() {
    if (!state.browse.path) {
      setHint(el.browserNewError, '请先进入一个目录，再新建子文件夹');
      return;
    }
    if (state.browse.busy) { return; }
    // 保留原始输入（不 trim），让「首尾空格 / 点」这类问题由校验或后端如实报错
    var name = el.browserNewName.value;
    var issue = validateNewFolderName(name);
    if (issue) {
      setHint(el.browserNewError, issue);
      el.browserNewName.focus();
      return;
    }

    state.browse.busy = true;
    updateMkdirState();
    setHint(el.browserNewError, '');
    var label = el.browserMkdir.textContent;
    el.browserMkdir.textContent = '创建中…';
    try {
      var data = await postJson(API.mkdir, { parent: state.browse.path, name: name });
      var created = data && data.path ? data.path : '';
      el.browserNewName.value = '';
      showToast('已创建文件夹：' + (data && data.name ? data.name : name), 'ok');
      state.browse.busy = false;
      updateMkdirState();
      if (created) { await loadBrowse(created); }
      return;
    } catch (err) {
      setHint(el.browserNewError, err.message);
    } finally {
      el.browserMkdir.textContent = label;
      state.browse.busy = false;
      updateMkdirState();
    }
  }

  /* ================= 扫描 / 预览 / 拷贝：统一走后台任务 ================= */

  /**
   * 启动一个后台任务并接管进度条。
   * mode 为 scan / preview / copy；三者在运行期间共用同一条进度条与同一个取消按钮。
   */
  async function startJob(mode) {
    var conf = JOB_MODES[mode];
    if (!conf || state.jobRunning) { return; }
    var button = mode === 'scan' ? el.btnScan : (mode === 'preview' ? el.btnPreview : el.btnStart);
    if (!button || button.disabled) { return; }

    if (mode === 'scan') {
      renderValidation();
      if (!sourceDirValue() || selectedSuffixes().length === 0) {
        showToast('请先填写源目录并至少勾选一个后缀', 'error');
        return;
      }
    } else {
      renderValidation({ showSummary: true });
      if (Object.keys(state.errors).length > 0) {
        showToast('配置不完整，请先修正左侧提示', 'error');
        return;
      }
    }

    // 扫描任务不需要日期 / 目的目录 / 地点 / 备注：后端对 scan 模式只校验源目录与后缀，
    // 因此这里原样提交表单（空值以空字符串提交），不编造任何数据。
    var config = buildConfig();
    setLoading(button, true, conf.loading);
    setJobRunning(true); // 立即锁定配置区：从提交那一刻起就不允许再改
    state.jobId = null;
    state.jobMode = mode;
    state.completing = false;
    if (mode === 'copy') {
      setHidden(el.reportCard, true);
      setHidden(el.reportError, true);
      setHidden(el.failuresDetails, true);
      setHidden(el.reportNoTimeDetails, true);
    }
    resetProgress();
    setPhase(conf.phase);
    updateLiveEmpty();

    try {
      var job = await postJson(API.jobs, {
        config: config,
        mode: mode,
        dry_run: mode !== 'copy'
      });
      state.jobId = job.job_id;
      state.jobMode = job.mode || mode;
      if (mode === 'scan') {
        state.scanRequest = {
          source_dir: config.source_dir,
          suffixes: config.suffixes,
          recursive: config.recursive
        };
      } else if (mode === 'preview') {
        state.planRequest = buildConfig();
      }
      if (el.progressCard.scrollIntoView) {
        el.progressCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      showToast(conf.startToast, 'info');
      connectEvents(job.job_id);
    } catch (err) {
      state.jobId = null;
      setJobRunning(false);
      showToast(err.message, 'error');
    } finally {
      setLoading(button, false);
      renderValidation();
    }
  }

  function handleScan() { return startJob('scan'); }

  function handlePreview() { return startJob('preview'); }

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

  function handleStart() { return startJob('copy'); }

  async function handleCancel() {
    if (!state.jobId || !state.jobRunning) { return; }
    setLoading(el.btnCancel, true, '取消中…');
    updateActionState();
    try {
      var res = await postJson(API.cancel(state.jobId), { job_id: state.jobId, canceled: true });
      if (res && res.canceled === false) { showToast('任务已结束，无需取消', 'info'); }
      else { showToast('已发送取消请求：已完成的拷贝会保留', 'info'); }
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
      // 终局事件（finished / canceled / failed）不携带进度字段，直接取最终状态，避免进度被清零；
      // 注意：任务在扫描途中被取消时，最后一条事件的 phase 可能仍是 scanning/copying，
      // 但 status 已经是 canceled —— 以 status 为准，才能立刻解锁界面。
      if (TERMINAL_PHASES.indexOf(phase) >= 0 || TERMINAL_STATUSES.indexOf(data.status) >= 0) {
        completeJob(jobId, data);
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
    var keys = ['total_files', 'done_files', 'total_bytes', 'done_bytes', 'percent',
      'scanned_files', 'scan_total_files'];
    return keys.some(function (key) { return data[key] !== undefined && data[key] !== null; });
  }

  /**
   * 合并「轮询 / SSE 终局事件快照」与「重新拉取的任务详情」：
   * 详情里的 final progress 优先（避免进度被清零），report / status / error 缺失时用快照兜底。
   */
  function mergeJobState(fetched, snapshot) {
    if (!fetched) { return snapshot || null; }
    if (!snapshot) { return fetched; }
    if (!fetched.report && snapshot.report) { fetched.report = snapshot.report; }
    if (!fetched.status || fetched.status === 'running') { fetched.status = snapshot.status || fetched.status; }
    if (fetched.error === null || fetched.error === undefined) { fetched.error = snapshot.error || null; }
    if (!fetched.progress) { fetched.progress = snapshot.progress || null; }
    if (!fetched.mode && snapshot.mode) { fetched.mode = snapshot.mode; }
    return fetched;
  }

  async function completeJob(jobId, snapshot) {
    if (state.jobId !== jobId || state.completing || !state.jobRunning) { return; }
    state.completing = true;
    closeEvents();
    stopPolling();
    setBanner(false);

    var fetched = null;
    if (!snapshot || !snapshot.progress) {
      try { fetched = await request(API.job(jobId)); } catch (err) { fetched = null; }
    }
    var data = mergeJobState(fetched, snapshot);

    state.completing = false;
    setJobRunning(false); // 任务结束：解锁配置区并恢复按钮状态

    if (!data) {
      setPhase('failed');
      showToast('无法读取任务最终状态，请刷新页面确认结果', 'error');
      updateLiveEmpty();
      return;
    }

    renderProgress(normalizeProgress(data.progress));
    renderOutcome(data);
    updateLiveEmpty();

    var status = data.status || '';
    if (status === 'done') {
      setPhase('finished');
      showToast(successToast(data), 'ok');
    } else if (status === 'canceled') {
      setPhase('canceled');
      showToast('任务已取消，已完成的拷贝会保留', 'info');
    } else if (status === 'failed') {
      setPhase('failed');
      showToast('任务失败：' + (data.error || '未知错误'), 'error');
    } else {
      setPhase('finished');
    }
  }

  /** 任务成功提示：按 mode 取不同措辞。 */
  function successToast(job) {
    var mode = jobMode(job);
    var report = job.report || null;
    if (mode === 'scan') {
      var scan = pickScanReport(report);
      if (scan) {
        return '扫描完成：命中 ' + formatCount(scan.matched_files) + ' 个文件，配对 ' +
          formatCount(scan.groups) + ' 张照片';
      }
      return '扫描完成';
    }
    if (mode === 'preview') {
      var plan = report && report.plan ? report.plan : null;
      if (plan) {
        return '计划已生成：' + (plan.folder_name || '') + '（' + formatCount(plan.planned_files) + ' 个文件）';
      }
      return '计划已生成';
    }
    var copied = report ? num(report.copied_files) : 0;
    return '任务完成：成功复制 ' + formatCount(copied) + ' 个文件';
  }

  /** 任务 / 报告的实际模式（报告字段优先，其次任务字段）。 */
  function jobMode(job) {
    var report = job && job.report ? job.report : null;
    return (report && report.mode) || (job && job.mode) || state.jobMode || '';
  }

  /** scan 报告：新契约放在 report.scan；旧结构（扫描结果直接作为 report）也兼容。 */
  function pickScanReport(report) {
    if (!report || typeof report !== 'object') { return null; }
    if (report.scan && typeof report.scan === 'object') { return report.scan; }
    if (report.groups !== undefined && report.matched_files !== undefined) { return report; }
    return null;
  }

  /** 按任务模式渲染结果卡片：scan → 扫描结果；preview → 计划 + 扫描结果；copy → 执行报告。 */
  function renderOutcome(job) {
    var mode = jobMode(job);
    var report = job.report || null;
    var status = job.status || '';
    if (mode === 'scan') {
      var scan = pickScanReport(report);
      if (scan) { renderScan(scan); }
      if (status === 'failed') { renderReport(job); }
      return;
    }
    if (mode === 'preview') {
      if (report && report.plan) { renderPlan(report.plan); }
      var scanAgain = pickScanReport(report);
      if (scanAgain) { renderScan(scanAgain); }
      if (status === 'failed') { renderReport(job); }
      return;
    }
    renderReport(job);
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
      scanned_files: num(p.scanned_files),
      scan_total_files: num(p.scan_total_files),
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
    setText(el.progressBytesLabel, '已复制 / 总字节');
    setText(el.progressBytes, '0 B / 0 B');
    setText(el.progressScanFiles, '0 / 0');
    setText(el.progressScanLabel, '命中文件 / 已读 EXIF');
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

  function isScanPhase(phase) { return SCAN_PHASES.indexOf(phase) >= 0; }

  function renderProgress(p) {
    if (!p) { resetProgress(); return; }
    var percent = clamp(num(p.percent), 0, 100);
    var scanning = isScanPhase(p.phase);
    el.progressFill.style.width = percent.toFixed(2) + '%';
    el.progressBar.setAttribute('aria-valuenow', percent.toFixed(1));
    setText(el.progressPercent, percent.toFixed(1) + '%');

    if (scanning) {
      // 扫描 / 预览阶段：进度按「已扫描 / 待扫描」文件数展示
      setText(el.progressFiles, '已扫描 ' + formatCount(p.scanned_files) +
        ' / 共 ' + formatCount(p.scan_total_files) + ' 个文件');
      setText(el.progressBytesLabel, '合计体积');
      setText(el.progressBytes, formatBytes(p.total_bytes));
      setText(el.progressScanLabel, '命中文件 / 已读 EXIF');
    } else {
      setText(el.progressFiles, formatCount(p.done_files) + ' / ' + formatCount(p.total_files) + ' 个文件');
      setText(el.progressBytesLabel, '已复制 / 总字节');
      setText(el.progressBytes, formatBytes(p.done_bytes) + ' / ' + formatBytes(p.total_bytes));
      // 复制阶段这一栏显示的是「计划文件 / 已复制」，标签必须跟着换，否则会读成"已读 EXIF"
      setText(el.progressScanLabel, '计划文件 / 已复制');
    }
    setText(el.progressScanFiles, formatCount(p.total_files) + ' / ' + formatCount(p.done_files));
    setText(el.progressSpeed, formatSpeed(p.speed_bps));
    setText(el.progressEta, formatDuration(p.eta_seconds));
    setText(el.progressElapsed, formatDuration(p.elapsed_seconds));

    if (p.current_file) {
      setText(el.progressCurrentName, baseName(p.current_file));
      el.progressCurrentName.title = p.current_file;
    } else {
      setText(el.progressCurrentName, phasePlaceholder(p.phase));
      el.progressCurrentName.title = '';
    }

    setHint(el.progressMessage, p.message);
    if (p.phase) { setPhase(p.phase); }
    document.title = (isScanPhase(p.phase) || p.phase === 'copying' || p.phase === 'planning')
      ? (percent.toFixed(1) + '% · ' + BASE_TITLE)
      : BASE_TITLE;
  }

  function phasePlaceholder(phase) {
    if (phase === 'counting') { return '正在统计待扫描文件…'; }
    if (phase === 'scanning') { return '正在扫描文件并读取 EXIF 拍摄时间…'; }
    if (phase === 'planning') { return '正在规划拷贝计划…'; }
    if (phase === 'copying') { return '正在复制…'; }
    return '等待开始';
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
    el.browserMkdir.addEventListener('click', handleMkdir);
    el.browserNewName.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); handleMkdir(); }
    });
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
    setConfigLocked(false);
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
