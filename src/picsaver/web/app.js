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
 *   GET  /api/jobs?brief=1            （后台任务中心轮询用，400ms 一次，不含 report 正文）
 *   GET  /api/jobs/{job_id}           （含 report 正文）
 *   GET  /api/jobs/{job_id}/events    （SSE，失败时降级为 500ms 轮询；仅前台任务使用）
 *   POST /api/jobs/{job_id}/cancel
 *   DELETE /api/jobs/{job_id}         （关闭 / 遗忘任务，终局才允许，进行中返回 409）
 * 扫描与预览不再调用同步接口 POST /api/scan、POST /api/preview，以便显示真实扫描进度。
 * 进度全部来自后端真实数据，界面不做任何假动画。
 *
 * 后台任务中心：所有任务（前台 + 后台）在后端同一条串行队列里执行，同一时刻只有一个真正运行，
 * 其余显示「排队中」。后台任务不锁定配置区；任务中心一律用 400ms 轮询（不为每个后台任务开 SSE，
 * 避免占满 HTTP/1.1 每域 6 个连接）。前台任务继续使用 SSE。
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
    jobsBrief: '/api/jobs?brief=1',
    job: function (jobId) { return '/api/jobs/' + encodeURIComponent(jobId); },
    events: function (jobId) { return '/api/jobs/' + encodeURIComponent(jobId) + '/events'; },
    cancel: function (jobId) { return '/api/jobs/' + encodeURIComponent(jobId) + '/cancel'; }
  };

  var POLL_INTERVAL_MS = 500;
  //: 后台任务中心的轮询间隔（契约：400ms）
  var TASK_POLL_MS = 400;
  //: 刷新页面时，只恢复这段时间内完成的终局任务（秒）
  var TASK_RESTORE_SECONDS = 600;
  var LIST_LIMIT = 500;
  var SUFFIXES = ['jpg', 'arw', 'orf'];
  var ILLEGAL_CHARS = ['<', '>', ':', '"', '/', '\\', '|', '?', '*'];
  var PHASE_LABELS = {
    idle: '等待开始',
    queued: '排队中',
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
  //: 注意：queued 不是终局状态，任何判断终局的地方都不得把它算进去
  var TERMINAL_STATUSES = ['done', 'canceled', 'failed'];
  //: 非终局状态：排队中 / 运行中
  var ACTIVE_STATUSES = ['queued', 'running'];
  //: 扫描类阶段（进度按已扫描 / 待扫描文件数换算）
  var SCAN_PHASES = ['counting', 'scanning'];
  //: 后台任务卡片的模式文案
  var TASK_MODE_LABELS = { scan: '后台扫描', preview: '后台预览', copy: '后台拷贝' };

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
    //: 后台任务中心：job_id -> task 记录
    tasks: {},
    //: 卡片显示顺序（等同入队顺序）
    taskOrder: [],
    //: 任务中心轮询定时器句柄 / 是否正在拉取（避免请求叠发）
    taskPoller: null,
    taskPolling: false,
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
    el.btnBgPreview = $('btn-bg-preview');
    el.btnBgCopy = $('btn-bg-copy');
    el.btnCancel = $('btn-cancel');

    el.tasksCard = $('tasks-card');
    el.tasksCount = $('tasks-count');
    el.btnClearDone = $('btn-clear-done');
    el.tasksEmpty = $('tasks-empty');
    el.tasksList = $('tasks-list');

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
      // 把 HTTP 状态码挂在错误上：关闭任务时要区分 409（还在进行中）与 404（已不存在）
      var failure = new Error(errorMessage(response, data));
      failure.status = response.status;
      throw failure;
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
    var bgBusy = el.btnBgPreview.classList.contains('is-loading') ||
      el.btnBgCopy.classList.contains('is-loading');
    var cancelBusy = el.btnCancel.classList.contains('is-loading');
    var running = state.jobRunning;
    var configOk = Object.keys(state.errors).length === 0;
    var scanOk = !!sourceDirValue() && selectedSuffixes().length > 0;

    el.btnScan.disabled = busy || running || !scanOk;
    el.btnPreview.disabled = busy || running || !configOk;
    el.btnStart.disabled = busy || running || !configOk;
    // 后台按钮：只在「前台任务进行中（配置已锁）」或提交中禁用；其余按配置是否合法启用。
    // 后台任务本身不锁配置，也不影响这两个按钮（用户可继续启动下一个操作）。
    el.btnBgPreview.disabled = busy || bgBusy || running || !configOk;
    el.btnBgCopy.disabled = busy || bgBusy || running || !configOk;
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
    // 排队阶段（phase = queued）：进度条固定 0%，只显示排队位置；任务仍在进行，配置区保持锁定
    var queued = p.phase === 'queued';
    var percent = queued ? 0 : clamp(num(p.percent), 0, 100);
    var scanning = isScanPhase(p.phase);
    el.progressFill.style.width = percent.toFixed(2) + '%';
    el.progressBar.setAttribute('aria-valuenow', percent.toFixed(1));
    setText(el.progressPercent, percent.toFixed(1) + '%');

    if (queued) {
      setText(el.progressFiles, p.message || '排队中');
      setText(el.progressBytesLabel, '合计体积');
      setText(el.progressBytes, formatBytes(p.total_bytes));
      setText(el.progressScanLabel, '命中文件 / 已读 EXIF');
    } else if (scanning) {
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
    if (phase === 'queued') { return '排队中，等待前面的任务完成…'; }
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

  /* ================= 后台任务中心 ================= */
  /*
   * 后端所有任务（前台 + 后台）共用一条串行队列：任一时刻最多 1 个真正执行，其余 status = queued。
   * 本模块只做三件事：
   *   1) 提交后台任务（POST /api/jobs）并登记卡片；
   *   2) 400ms 轮询 GET /api/jobs?brief=1 更新进度（不为每个后台任务开 SSE）；
   *   3) 终局后拉一次 GET /api/jobs/{id} 取 report 渲染结果摘要，关闭时 DELETE。
   * 后台任务不锁定配置区（锁定只由前台任务的 setJobRunning 负责）。
   */

  function isTerminalStatus(status) { return TERMINAL_STATUSES.indexOf(status) >= 0; }

  function isActiveStatus(status) { return ACTIVE_STATUSES.indexOf(status) >= 0; }

  /** 任务是否仍在进行（排队中 / 运行中）；已失效的卡片不再视为进行中。 */
  function isTaskActive(task) { return !!task && !task.stale && isActiveStatus(task.status); }

  /** 任务卡片能否关闭：终局任务或已失效任务。 */
  function isTaskDismissible(task) { return !!task && (!!task.stale || isTerminalStatus(task.status)); }

  function activeTaskCount() {
    return state.taskOrder.filter(function (id) { return isTaskActive(state.tasks[id]); }).length;
  }

  function dismissibleTaskCount() {
    return state.taskOrder.filter(function (id) { return isTaskDismissible(state.tasks[id]); }).length;
  }

  function taskModeLabel(mode) { return TASK_MODE_LABELS[mode] || '后台任务'; }

  /** 任务卡片的阶段徽标：{phase, label}。 */
  function taskBadge(task) {
    if (task.stale) { return { phase: 'stale', label: '已失效' }; }
    if (task.status === 'queued') { return { phase: 'queued', label: PHASE_LABELS.queued }; }
    if (task.status === 'done') { return { phase: 'finished', label: PHASE_LABELS.finished }; }
    if (task.status === 'canceled') { return { phase: 'canceled', label: PHASE_LABELS.canceled }; }
    if (task.status === 'failed') { return { phase: 'failed', label: PHASE_LABELS.failed }; }
    var phase = task.progress && task.progress.phase ? task.progress.phase : '';
    if (phase === 'queued') { return { phase: 'queued', label: PHASE_LABELS.queued }; }
    if (PHASE_LABELS[phase]) { return { phase: phase, label: PHASE_LABELS[phase] }; }
    return { phase: 'running', label: '进行中' };
  }

  /** 由 brief / 详情响应构造内部任务记录。 */
  function makeTask(brief) {
    var age = brief.age_seconds === null || brief.age_seconds === undefined ? null : num(brief.age_seconds);
    return {
      job_id: brief.job_id,
      mode: brief.mode || '',
      status: brief.status || 'queued',
      dry_run: !!brief.dry_run,
      queue_position: num(brief.queue_position),
      age_seconds: age,
      has_report: !!brief.has_report,
      error: brief.error || null,
      config: brief.config || null,
      progress: normalizeProgress(brief.progress),
      report: null,
      reportLoaded: false,
      stale: false,
      staleToast: false,
      built: false,
      resultKey: '',
      nodes: {}
    };
  }

  function makeStat(labelText) {
    var root = document.createElement('div');
    root.className = 'stat';
    var label = document.createElement('span');
    label.className = 'stat__label';
    label.textContent = labelText;
    var value = document.createElement('span');
    value.className = 'stat__value stat__value--mono';
    value.textContent = '—';
    root.appendChild(label);
    root.appendChild(value);
    return { root: root, label: label, value: value };
  }

  function metaGrid(pairs) {
    var grid = document.createElement('div');
    grid.className = 'meta-grid';
    pairs.forEach(function (pair) {
      var meta = document.createElement('div');
      meta.className = 'meta';
      var label = document.createElement('span');
      label.className = 'meta__label';
      label.textContent = pair[0];
      var value = document.createElement('span');
      value.className = 'meta__value meta__value--mono' + (pair[2] ? ' ' + pair[2] : '');
      value.textContent = pair[1];
      meta.appendChild(label);
      meta.appendChild(value);
      grid.appendChild(meta);
    });
    return grid;
  }

  /** 目标目录行：路径可点击复制。 */
  function taskTargetRow(dir) {
    var row = document.createElement('div');
    row.className = 'report__target';
    var label = document.createElement('span');
    label.className = 'report__target-label';
    label.textContent = '目标目录';
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'report__target-value';
    button.textContent = dir || '—';
    button.title = dir ? ('点击复制：' + dir) : '';
    button.addEventListener('click', function () { copyAndToast(dir, '目标目录'); });
    row.appendChild(label);
    row.appendChild(button);
    return row;
  }

  /**
   * 折叠明细列表（最多展示 LIST_LIMIT = 500 条，并提示总数）。
   * items 同时兼容「无 EXIF 时间清单」（path / suffix / reason）与「失败明细」（source / error / target）。
   */
  function taskDetails(title, items) {
    var list = Array.isArray(items) ? items : [];
    var details = document.createElement('details');
    details.className = 'details';
    var summary = document.createElement('summary');
    summary.className = 'details__summary';
    summary.textContent = title + '（' + formatCount(list.length) + '）';
    var body = document.createElement('div');
    body.className = 'details__body';
    var ul = document.createElement('ul');
    ul.className = 'list-scroll';

    list.slice(0, LIST_LIMIT).forEach(function (item) {
      var li = document.createElement('li');
      li.className = 'list-item';

      var path = document.createElement('span');
      path.className = 'list-item__path';
      var pathText = item.path || item.source || '(未知路径)';
      path.textContent = pathText;
      path.title = pathText;
      li.appendChild(path);

      if (item.suffix) {
        var sfx = document.createElement('span');
        sfx.className = 'list-item__suffix';
        sfx.textContent = String(item.suffix).toUpperCase();
        li.appendChild(sfx);
      }
      var reasonText = item.reason || item.error || '';
      if (reasonText) {
        var reason = document.createElement('span');
        reason.className = 'list-item__reason';
        reason.textContent = reasonText + (item.target ? ' → ' + item.target : '');
        li.appendChild(reason);
      }
      ul.appendChild(li);
    });
    body.appendChild(ul);

    if (list.length > LIST_LIMIT) {
      var more = document.createElement('p');
      more.className = 'list-more';
      more.textContent = '已展示前 ' + LIST_LIMIT + ' 条，共 ' + formatCount(list.length) +
        ' 条，还有 ' + formatCount(list.length - LIST_LIMIT) + ' 条未显示';
      body.appendChild(more);
    }
    details.appendChild(summary);
    details.appendChild(body);
    return details;
  }

  /** 生成一张任务卡片的 DOM（只建一次，之后按 400ms 轮询做增量更新）。 */
  function buildTaskCard(task) {
    var li = document.createElement('li');
    li.className = 'card task';
    li.dataset.jobId = task.job_id;

    var head = document.createElement('div');
    head.className = 'card__head task__head';

    var mode = document.createElement('span');
    mode.className = 'task__mode';
    mode.textContent = taskModeLabel(task.mode);

    var badge = document.createElement('span');
    badge.className = 'phase-badge';
    badge.dataset.phase = 'queued';
    badge.textContent = PHASE_LABELS.queued;

    var spacer = document.createElement('span');
    spacer.className = 'task__spacer';

    var cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn btn--danger btn--sm task__cancel';
    cancel.textContent = '取消';
    cancel.addEventListener('click', function () { cancelTask(task.job_id); });

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'icon-btn task__close';
    close.textContent = '×';
    close.setAttribute('aria-label', '关闭任务卡片：' + taskModeLabel(task.mode));
    close.title = '关闭并从列表移除（不影响已复制到磁盘的文件）';
    close.hidden = true;
    close.addEventListener('click', function () { dismissTask(task.job_id); });

    head.appendChild(mode);
    head.appendChild(badge);
    head.appendChild(spacer);
    head.appendChild(cancel);
    head.appendChild(close);

    var body = document.createElement('div');
    body.className = 'card__body task__body';

    var notice = document.createElement('p');
    notice.className = 'task__queue';
    notice.hidden = true;

    var progress = document.createElement('div');
    progress.className = 'progress';
    var track = document.createElement('div');
    track.className = 'progress__track';
    var fill = document.createElement('div');
    fill.className = 'progress__fill';
    track.appendChild(fill);
    var head2 = document.createElement('div');
    head2.className = 'progress__head';
    var percent = document.createElement('span');
    percent.className = 'progress__percent';
    percent.textContent = '0.0%';
    var files = document.createElement('span');
    files.className = 'progress__files';
    files.textContent = '排队中';
    head2.appendChild(percent);
    head2.appendChild(files);

    var stats = document.createElement('div');
    stats.className = 'progress__stats';
    var statBytes = makeStat('已复制 / 总字节');
    var statSpeed = makeStat('速度');
    var statEta = makeStat('剩余时间');
    var statElapsed = makeStat('已用时间');
    [statBytes, statSpeed, statEta, statElapsed].forEach(function (stat) { stats.appendChild(stat.root); });

    var current = document.createElement('div');
    current.className = 'progress__current';
    var currentLabel = document.createElement('span');
    currentLabel.className = 'progress__current-label';
    currentLabel.textContent = '当前文件';
    var currentName = document.createElement('span');
    currentName.className = 'progress__current-name';
    currentName.textContent = '排队中';
    current.appendChild(currentLabel);
    current.appendChild(currentName);

    var message = document.createElement('p');
    message.className = 'progress__message';
    message.hidden = true;

    progress.appendChild(track);
    progress.appendChild(head2);
    progress.appendChild(stats);
    progress.appendChild(current);
    progress.appendChild(message);

    var result = document.createElement('div');
    result.className = 'task__result';
    result.hidden = true;

    var errorNode = document.createElement('p');
    errorNode.className = 'hint hint--error';
    errorNode.hidden = true;

    body.appendChild(notice);
    body.appendChild(progress);
    body.appendChild(result);
    body.appendChild(errorNode);
    li.appendChild(head);
    li.appendChild(body);

    task.nodes = {
      root: li,
      badge: badge,
      cancel: cancel,
      close: close,
      notice: notice,
      progress: progress,
      fill: fill,
      percent: percent,
      files: files,
      bytes: statBytes.value,
      bytesLabel: statBytes.label,
      speed: statSpeed.value,
      eta: statEta.value,
      elapsed: statElapsed.value,
      current: currentName,
      message: message,
      result: result,
      error: errorNode
    };
    task.built = true;
    el.tasksList.appendChild(li);
  }

  /** 任务卡片进度区（与主进度卡同一套文案规则，排队阶段固定 0%）。 */
  function renderTaskProgress(task) {
    var n = task.nodes;
    var p = task.progress || {};
    var queued = task.status === 'queued' || p.phase === 'queued';
    var percent = queued ? 0 : clamp(num(p.percent), 0, 100);

    n.fill.style.width = percent.toFixed(2) + '%';
    setText(n.percent, percent.toFixed(1) + '%');

    if (queued) {
      setText(n.files, '排队中');
      setText(n.bytesLabel, '合计体积');
      setText(n.bytes, formatBytes(p.total_bytes));
    } else if (isScanPhase(p.phase)) {
      setText(n.files, '已扫描 ' + formatCount(p.scanned_files) +
        ' / 共 ' + formatCount(p.scan_total_files) + ' 个文件');
      setText(n.bytesLabel, '合计体积');
      setText(n.bytes, formatBytes(p.total_bytes));
    } else {
      setText(n.files, formatCount(p.done_files) + ' / ' + formatCount(p.total_files) + ' 个文件');
      setText(n.bytesLabel, '已复制 / 总字节');
      setText(n.bytes, formatBytes(p.done_bytes) + ' / ' + formatBytes(p.total_bytes));
    }

    setText(n.speed, formatSpeed(p.speed_bps));
    setText(n.eta, formatDuration(p.eta_seconds));
    setText(n.elapsed, formatDuration(p.elapsed_seconds));

    if (p.current_file) {
      setText(n.current, baseName(p.current_file));
      n.current.title = p.current_file;
    } else {
      setText(n.current, phasePlaceholder(p.phase));
      n.current.title = '';
    }
    setHint(n.message, p.message || '');
  }

  /** 终局结果摘要：preview → 计划摘要；copy → 执行摘要；scan → 扫描摘要。 */
  function renderTaskResult(task) {
    var box = task.nodes.result;
    clearNode(box);
    setHidden(box, false);

    if (task.stale) {
      var staleMsg = document.createElement('p');
      staleMsg.className = 'hint hint--warn';
      staleMsg.textContent = '该任务已不在后端任务列表中（后端可能已重启，或已在其他标签页关闭）。此卡片可关闭。';
      box.appendChild(staleMsg);
      return;
    }
    if (!task.reportLoaded) {
      var pending = document.createElement('p');
      pending.className = 'hint';
      pending.textContent = '正在读取任务结果…';
      box.appendChild(pending);
      return;
    }

    var report = task.report || null;
    if (!report) {
      var none = document.createElement('p');
      none.className = 'hint';
      none.textContent = task.status === 'failed' ? '任务失败，未生成报告。' : '任务已结束，没有可显示的报告。';
      box.appendChild(none);
      return;
    }

    var mode = report.mode || task.mode;
    if (mode === 'preview') { renderTaskPlanResult(box, report); return; }
    if (mode === 'scan') { renderTaskScanResult(box, report); return; }
    renderTaskCopyResult(box, report);
  }

  function renderTaskPlanResult(box, report) {
    var plan = report.plan || null;
    if (!plan) {
      var none = document.createElement('p');
      none.className = 'hint';
      none.textContent = '任务已结束，但报告中没有拷贝计划。';
      box.appendChild(none);
      return;
    }
    box.appendChild(taskTargetRow(plan.target_dir));
    box.appendChild(metaGrid([
      ['文件夹名', plan.folder_name || '—'],
      ['计划文件数', formatCount(plan.planned_files)],
      ['计划体积', formatBytes(plan.planned_bytes)],
      ['范围外跳过', formatCount(plan.out_of_range_files)],
      ['无 EXIF 时间', formatCount(plan.no_time_files), 'meta__value--warn']
    ]));
    var noTime = Array.isArray(plan.no_time) ? plan.no_time : [];
    if (noTime.length > 0) { box.appendChild(taskDetails('无 EXIF 时间文件清单', noTime)); }
  }

  function renderTaskScanResult(box, report) {
    var scan = pickScanReport(report) || report.scan || null;
    if (!scan) {
      var none = document.createElement('p');
      none.className = 'hint';
      none.textContent = '任务已结束，没有可显示的报告。';
      box.appendChild(none);
      return;
    }
    box.appendChild(metaGrid([
      ['配对照片数', formatCount(scan.groups)],
      ['命中文件数', formatCount(scan.matched_files)],
      ['合计体积', formatBytes(scan.total_bytes)],
      ['可确定拍摄时间', formatCount(scan.with_time)],
      ['无 EXIF 时间', formatCount(scan.no_time), 'meta__value--warn']
    ]));
    var noTime = Array.isArray(scan.no_time_files) ? scan.no_time_files : [];
    if (noTime.length > 0) { box.appendChild(taskDetails('无 EXIF 时间文件清单', noTime)); }
  }

  function renderTaskCopyResult(box, report) {
    box.appendChild(taskTargetRow(report.target_dir));
    box.appendChild(metaGrid([
      ['成功复制', formatCount(report.copied_files), 'meta__value--ok'],
      ['跳过（已存在）', formatCount(report.skipped_existing)],
      ['失败', formatCount(report.failed_files), 'meta__value--warn'],
      ['无 EXIF 时间', formatCount(report.no_time_files)],
      ['已复制字节', formatBytes(report.copied_bytes) + ' / ' + formatBytes(report.total_bytes)],
      ['耗时', formatDuration(report.elapsed_seconds)]
    ]));
    var failures = Array.isArray(report.failures) ? report.failures : [];
    if (failures.length > 0) { box.appendChild(taskDetails('失败明细', failures)); }
  }

  /** 增量刷新一张任务卡片。 */
  function updateTaskCard(task) {
    if (!task || !task.built) { return; }
    var n = task.nodes;
    var badge = taskBadge(task);
    n.badge.dataset.phase = badge.phase;
    setText(n.badge, badge.label);
    n.root.dataset.status = task.stale ? 'stale' : (task.status || '');
    n.root.classList.toggle('task--active', isTaskActive(task));
    n.root.classList.toggle('task--stale', !!task.stale);

    var dismissible = isTaskDismissible(task);
    var active = isTaskActive(task);
    n.cancel.hidden = !active;
    n.cancel.disabled = !active;
    n.close.hidden = !dismissible;

    if (task.stale) {
      setText(n.notice, '已失效：后端任务列表中已不存在该任务');
      n.notice.classList.add('is-stale');
      n.notice.hidden = false;
      n.progress.hidden = true;
    } else if (active) {
      n.progress.hidden = false;
      if (task.status === 'queued' || (task.progress && task.progress.phase === 'queued')) {
        var ahead = Math.max(task.queue_position - 1, 0);
        setText(n.notice, '排队中 · 前面还有 ' + formatCount(ahead) + ' 个');
        n.notice.classList.remove('is-stale');
        n.notice.hidden = false;
      } else {
        n.notice.hidden = true;
        n.notice.textContent = '';
      }
      renderTaskProgress(task);
    } else {
      // 已完成 / 已取消 / 失败：保留进度行并固定显示最终值（与主进度卡一致），
      // 给用户"跑完了"的视觉闭环；数值来自终局详情里的 progress。
      n.progress.hidden = false;
      n.notice.hidden = true;
      n.notice.textContent = '';
      renderTaskProgress(task);
    }

    if (dismissible) {
      // 终局 / 已失效：结果摘要只在内容变化时重建（避免折叠面板被反复重置）
      var resultKey = task.stale ? 'stale'
        : (task.status + '|' + (task.report ? 'report' : (task.reportLoaded ? 'noreport' : 'pending')));
      if (task.resultKey !== resultKey) {
        task.resultKey = resultKey;
        renderTaskResult(task);
      }
    } else {
      setHidden(n.result, true);
      task.resultKey = '';
    }

    if (task.error) { setHint(n.error, '任务错误：' + task.error); }
    else { setHint(n.error, ''); }
  }

  function renderTasksMeta() {
    var total = state.taskOrder.length;
    var active = activeTaskCount();
    var done = dismissibleTaskCount();
    var text = total === 0
      ? '暂无任务'
      : ('共 ' + total + ' 个任务' + (active > 0 ? '（' + active + ' 个进行中）' : ''));
    setText(el.tasksCount, text);
    setHidden(el.tasksEmpty, total > 0);
    // 清除进行中保持禁用，避免中途被重新启用导致重复提交
    el.btnClearDone.disabled = done === 0 || el.btnClearDone.classList.contains('is-loading');
  }

  function registerTask(brief) {
    var task = makeTask(brief);
    state.tasks[task.job_id] = task;
    state.taskOrder.push(task.job_id);
    buildTaskCard(task);
    updateTaskCard(task);
    renderTasksMeta();
    return task;
  }

  function removeTask(jobId) {
    var task = state.tasks[jobId];
    if (!task) { return; }
    if (task.nodes.root && task.nodes.root.parentNode) {
      task.nodes.root.parentNode.removeChild(task.nodes.root);
    }
    delete state.tasks[jobId];
    var index = state.taskOrder.indexOf(jobId);
    if (index >= 0) { state.taskOrder.splice(index, 1); }
    renderTasksMeta();
    ensureTaskPoller();
  }

  /* ---------- 启动后台任务 ---------- */

  /** 后台任务的排队提示：运行中 / 排队中（前面还有 N 个）。 */
  function queueHint(status, position) {
    if (status === 'queued') {
      return '排队中，前面还有 ' + formatCount(Math.max(num(position) - 1, 0)) + ' 个任务';
    }
    return '正在执行';
  }

  async function startBackgroundJob(mode) {
    if (mode !== 'preview' && mode !== 'copy') { return; }
    var button = mode === 'preview' ? el.btnBgPreview : el.btnBgCopy;
    if (!button) { return; }
    if (state.jobRunning) {
      showToast('前台任务进行中，配置已锁定，请等待其结束或先取消', 'error');
      return;
    }
    if (button.disabled) {
      // 按钮禁用时（配置不合法 / 提交中）仍给出原因，避免"点了没反应"
      renderValidation({ showSummary: true });
      if (Object.keys(state.errors).length > 0) {
        showToast('配置不完整，请先修正左侧提示再启动后台任务', 'error');
      }
      return;
    }

    renderValidation({ showSummary: true });
    if (Object.keys(state.errors).length > 0) {
      showToast('配置不完整，请先修正左侧提示再启动后台任务', 'error');
      return;
    }

    var config = buildConfig();
    setLoading(button, true, '提交中…');
    try {
      var job = await postJson(API.jobs, {
        config: config,
        mode: mode,
        dry_run: mode !== 'copy',
        // 后台任务按提交顺序 FIFO 排队；不带该标记会被后端当成前台任务插到队首
        background: true
      });
      var status = job.status || 'queued';
      var position = num(job.queue_position);
      var progress;
      if (status === 'queued') {
        progress = { phase: 'queued', percent: 0, message: '排队中：前面还有 ' + Math.max(position - 1, 0) + ' 个任务' };
      } else {
        progress = { phase: 'counting', percent: 0, message: '' };
      }
      var task = registerTask({
        job_id: job.job_id,
        mode: job.mode || mode,
        status: status,
        dry_run: !!(job.dry_run === undefined ? (mode !== 'copy') : job.dry_run),
        queue_position: position,
        age_seconds: null,
        has_report: false,
        error: null,
        config: config,
        progress: progress
      });
      showToast('已加入后台队列：' + taskModeLabel(task.mode) + '（' + queueHint(status, position) + '）', 'info');
      ensureTaskPoller();
      pollTasks(); // 立刻取一次真实排队位置 / 进度，不用等满 400ms
    } catch (err) {
      // 例如同一目标目录的第二个拷贝任务：后端返回 409，这里原样显示中文提示，不建卡片
      showToast(err.message, 'error');
    } finally {
      setLoading(button, false);
      renderValidation();
    }
  }

  async function cancelTask(jobId) {
    var task = state.tasks[jobId];
    if (!task || !isTaskActive(task)) { return; }
    task.nodes.cancel.disabled = true;
    try {
      var res = await postJson(API.cancel(jobId), { job_id: jobId, canceled: true });
      if (res && res.canceled === false) { showToast('任务已结束，无需取消', 'info'); }
      else { showToast('已发送取消请求：已完成的拷贝会保留', 'info'); }
    } catch (err) {
      showToast(err.message, 'error');
    } finally {
      if (state.tasks[jobId]) { state.tasks[jobId].nodes.cancel.disabled = !isTaskActive(state.tasks[jobId]); }
    }
  }

  /* ---------- 关闭 / 清除 ---------- */

  async function dismissTask(jobId) {
    var task = state.tasks[jobId];
    if (!task) { return; }
    var close = task.nodes.close;
    if (close) { close.disabled = true; }
    try {
      await request(API.job(jobId), { method: 'DELETE' });
      removeTask(jobId);
    } catch (err) {
      if (err && err.status === 404) {
        // 后端已遗忘该任务（例如已过保留期）：卡片同样应该消失
        removeTask(jobId);
        showToast('该任务在后端已不存在，卡片已移除', 'info');
      } else if (err && err.status === 409) {
        // 任务还在进行中：保留卡片并提示，交给用户先取消
        showToast(err.message, 'error');
      } else {
        showToast('关闭任务失败：' + err.message, 'error');
      }
    } finally {
      var current = state.tasks[jobId];
      if (current && current.nodes.close) { current.nodes.close.disabled = false; }
    }
  }

  async function clearDoneTasks() {
    if (el.btnClearDone.classList.contains('is-loading')) { return; }
    var ids = state.taskOrder.filter(function (id) { return isTaskDismissible(state.tasks[id]); });
    if (ids.length === 0) { showToast('没有已完成的任务', 'info'); return; }

    setLoading(el.btnClearDone, true, '清除中…');
    var removed = 0;
    var failed = 0;
    for (var i = 0; i < ids.length; i += 1) {
      var id = ids[i];
      if (!state.tasks[id]) { continue; }
      try {
        await request(API.job(id), { method: 'DELETE' });
        removeTask(id);
        removed += 1;
      } catch (err) {
        if (err && err.status === 404) { removeTask(id); removed += 1; }
        else if (err && err.status === 409) { failed += 1; showToast(err.message, 'error'); }
        else { failed += 1; showToast('清除任务失败：' + err.message, 'error'); }
      }
    }
    setLoading(el.btnClearDone, false);
    renderTasksMeta();
    if (removed > 0) {
      showToast('已清除 ' + removed + ' 个已完成任务' + (failed > 0 ? ('，' + failed + ' 个未清除') : ''),
        failed > 0 ? 'error' : 'ok');
    }
  }

  /* ---------- 400ms 轮询 ---------- */

  function ensureTaskPoller() {
    if (activeTaskCount() === 0) { stopTaskPoller(); return; }
    if (state.taskPoller !== null) { return; }
    state.taskPoller = window.setInterval(pollTasks, TASK_POLL_MS);
  }

  function stopTaskPoller() {
    if (state.taskPoller !== null) {
      window.clearInterval(state.taskPoller);
      state.taskPoller = null;
    }
  }

  async function pollTasks() {
    if (state.taskPolling) { return; }
    if (activeTaskCount() === 0) { stopTaskPoller(); return; }
    state.taskPolling = true;
    try {
      var data = await request(API.jobsBrief);
      applyTaskBriefs(data && Array.isArray(data.jobs) ? data.jobs : []);
    } catch (err) {
      // 网络抖动 / 后端短暂不可用：保持轮询，下一轮再试，不打断界面
    } finally {
      state.taskPolling = false;
    }
    if (activeTaskCount() === 0) { stopTaskPoller(); }
  }

  function applyTaskBriefs(list) {
    var seen = {};
    list.forEach(function (item) { if (item && item.job_id) { seen[item.job_id] = item; } });
    state.taskOrder.slice().forEach(function (id) {
      var task = state.tasks[id];
      if (!task) { return; }
      var brief = seen[id];
      if (!brief) { markTaskStale(task); return; }
      applyTaskBrief(task, brief);
    });
  }

  function applyTaskBrief(task, brief) {
    var wasActive = isTaskActive(task);
    task.status = brief.status || task.status;
    task.mode = brief.mode || task.mode;
    task.dry_run = !!brief.dry_run;
    task.queue_position = num(brief.queue_position);
    task.age_seconds = brief.age_seconds === null || brief.age_seconds === undefined ? null : num(brief.age_seconds);
    task.has_report = !!brief.has_report;
    task.error = brief.error || null;
    if (brief.config) { task.config = brief.config; }
    var progress = normalizeProgress(brief.progress);
    if (progress) { task.progress = progress; }

    updateTaskCard(task);
    renderTasksMeta();

    // 非终局 → 终局：只拉一次详情补 report
    if (wasActive && !task.stale && isTerminalStatus(task.status)) {
      fetchTaskDetail(task);
    }
  }

  /** 终局后拉一次 GET /api/jobs/{id}（brief 列表不含 report 正文）。 */
  async function fetchTaskDetail(task) {
    if (!task || task.reportLoaded || task.stale) { return; }
    task.reportLoaded = true;
    try {
      var data = await request(API.job(task.job_id));
      task.report = data.report || null;
      task.status = data.status || task.status;
      // 终局详情里的 progress 才是最终值：brief 轮询可能停在 96% 这类中间值，
      // 不覆盖的话"已完成"的卡片会一直显示没到 100% 的百分比。
      var detailProgress = normalizeProgress(data.progress);
      if (detailProgress) { task.progress = detailProgress; }
      task.queue_position = num(data.queue_position);
      task.age_seconds = (data.age_seconds === null || data.age_seconds === undefined)
        ? null : num(data.age_seconds);
      task.dry_run = !!data.dry_run;
      if (data.error) { task.error = data.error; }
      if (task.mode === '' && data.mode) { task.mode = data.mode; }
    } catch (err) {
      if (err && err.status === 404) { markTaskStale(task); return; }
      task.reportLoaded = false;
      showToast('读取任务结果失败：' + err.message, 'error');
      return;
    }
    updateTaskCard(task);
    renderTasksMeta();
  }

  /** 已登记任务从 brief 列表中消失（后端重启 / 其他标签页已关闭它）。 */
  function markTaskStale(task) {
    if (!task || task.stale) { return; }
    task.stale = true;
    updateTaskCard(task);
    renderTasksMeta();
    if (!task.staleToast) {
      task.staleToast = true;
      showToast('任务 ' + taskModeLabel(task.mode) + ' 已不在后端任务列表中，卡片已标记为「已失效」', 'info');
    }
  }

  /* ---------- 刷新恢复 ---------- */

  /** 刷新页面后恢复：全部 queued / running，以及 10 分钟内完成的终局任务（需拉详情补 report）。 */
  async function restoreTasks() {
    var data = null;
    try {
      data = await request(API.jobsBrief);
    } catch (err) {
      return; // 后端未就绪时静默跳过，不影响其他功能
    }
    var list = data && Array.isArray(data.jobs) ? data.jobs : [];
    var pending = [];
    list.forEach(function (brief) {
      if (!brief || !brief.job_id || state.tasks[brief.job_id]) { return; }
      var active = isActiveStatus(brief.status || '');
      var recent = brief.age_seconds !== null && brief.age_seconds !== undefined &&
        num(brief.age_seconds) <= TASK_RESTORE_SECONDS;
      if (!active && !recent) { return; }
      var task = registerTask(brief);
      if (!active) { pending.push(task); }
    });
    ensureTaskPoller();
    for (var i = 0; i < pending.length; i += 1) {
      await fetchTaskDetail(pending[i]);
    }
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
    el.btnBgPreview.addEventListener('click', function () { startBackgroundJob('preview'); });
    el.btnBgCopy.addEventListener('click', function () { startBackgroundJob('copy'); });
    el.btnClearDone.addEventListener('click', clearDoneTasks);
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
      stopTaskPoller(); // 页面卸载：停掉后台任务中心轮询
    });
  }

  async function init() {
    cacheEls();
    setText(el.footerOrigin, ORIGIN);
    setConfigLocked(false);
    bindEvents();
    resetProgress();
    renderTasksMeta();
    updateLiveEmpty();
    await loadDefaults();
    renderValidation();
    loadHealth();
    // 刷新恢复：排队中 / 运行中的任务 + 最近 10 分钟内完成的终局任务
    await restoreTasks();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
