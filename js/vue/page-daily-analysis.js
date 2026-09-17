/**
 * 每日数据分析 — Vue 版（阶段 4）
 * classic script，与 app.js 共用全局词法作用域；接口复用 ApiService（getAnalysisDates/getAnalysisReport/generateDailyReport/runAnalysisAgent，均已封装，零补接口）
 * 结构：左侧（日期下拉 + 生成按钮 + 下载PDF + 报告展示区）+ 右侧（数据分析智能体：快捷问题 + 输入框 + 结论/卡片）
 * XSS：报告正文是后端 AI 生成的可信 HTML，用 <iframe srcdoc> 隔离渲染（用户已确认）；智能体 analysis 为纯文本用 {{ }} + white-space 保留换行；cards 结构化用 {{ }} 自动转义，绝不用 v-html
 * 报告样式：iframe 无法继承父页面 CSS，故把 index.html 393-425 的报告样式内联到 iframe 的 <style> 中（REPORT_CSS 常量）；Font Awesome 图标在 iframe 内无法加载，统一 display:none（与 PDF 下载版去图标一致）
 */
(function () {
  'use strict';

  // ==================== 报告样式（iframe 隔离，内联） ====================
  var REPORT_CSS = [
    "body{margin:0;font-family:'Microsoft YaHei','微软雅黑',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f8fafc;color:#334155;line-height:1.7}",
    ".da-report{background:#fff;border-radius:16px;border:1px solid #eef2f7;box-shadow:0 8px 30px rgba(15,23,42,.06);overflow:hidden}",
    ".da-header{display:flex;align-items:center;justify-content:space-between;padding:22px 28px;background:linear-gradient(135deg,#0f766e 0%,#14b8a6 55%,#6366f1 130%);flex-wrap:wrap;gap:12px}",
    ".da-date-badge{background:rgba(255,255,255,.18);color:#fff;padding:8px 18px;border-radius:99px;font-size:0.9rem;font-weight:600;display:inline-flex;align-items:center;gap:8px;backdrop-filter:blur(4px)}",
    ".da-meta{display:flex;gap:18px;font-size:0.78rem;color:rgba(255,255,255,.85)}",
    ".da-kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;padding:24px 28px;background:#f8fafc;border-bottom:1px solid #eef2f7}",
    "@media(max-width:900px){.da-kpi-row{grid-template-columns:repeat(2,1fr)}}",
    "@media(max-width:500px){.da-kpi-row{grid-template-columns:1fr}}",
    ".da-kpi-card{padding:18px;border-radius:14px;background:#fff;border:1px solid #eef2f7;box-shadow:0 1px 3px rgba(15,23,42,.05);display:flex;flex-direction:column;gap:6px}",
    ".da-kpi-good{border-left:4px solid #10b981}",
    ".da-kpi-danger{border-left:4px solid #ef4444}",
    ".da-kpi-label{font-size:0.78rem;color:#64748b;font-weight:600}",
    ".da-kpi-value{font-size:1.5rem;font-weight:800;color:#0f172a;font-variant-numeric:tabular-nums;line-height:1.15}",
    ".da-kpi-sub{font-size:0.75rem;color:#94a3b8}",
    ".analysis-section{padding:26px 28px;border-bottom:1px solid #f1f5f9}",
    ".analysis-section:last-child{border-bottom:none}",
    ".analysis-section h3{font-size:1.08rem;font-weight:700;color:#0f172a;margin:0 0 16px;display:flex;align-items:center;gap:10px}",
    ".analysis-section p{font-size:0.9rem;color:#475569;line-height:1.7;margin-bottom:12px}",
    ".analysis-section p:last-child{margin-bottom:0}",
    ".highlight{background:#fef3c7;padding:2px 8px;border-radius:6px;font-weight:700;color:#92400e}",
    ".warn{color:#e11d48;font-weight:700}",
    ".danger{color:#dc2626;font-weight:700;background:#fee2e2;padding:2px 8px;border-radius:6px}",
    ".da-table{width:100%;border-collapse:collapse;font-size:0.85rem;margin:12px 0;border-radius:10px;overflow:hidden;box-shadow:0 1px 2px rgba(15,23,42,.04)}",
    ".da-table th{text-align:left;padding:11px 14px;background:#f1f5f9;color:#334155;font-weight:700;font-size:0.78rem;border-bottom:2px solid #e2e8f0;white-space:nowrap}",
    ".da-table td{padding:11px 14px;border-bottom:1px solid #f1f5f9;color:#334155;font-variant-numeric:tabular-nums}",
    ".da-table tbody tr:nth-child(even){background:#fafbfc}",
    ".da-table tbody tr:hover{background:#f0fdfa}",
    ".da-warn-list{list-style:none;padding:0;margin:10px 0}",
    ".da-warn-list li{padding:10px 16px;border-radius:10px;margin-bottom:8px;font-size:0.85rem;font-weight:600;line-height:1.5}",
    ".da-warn-list .da-warn-warning{background:#fff7ed;color:#c2410c;border-left:4px solid #f97316}",
    ".da-warn-list .da-warn-danger{background:#fef2f2;color:#dc2626;border-left:4px solid #ef4444}",
    "i[class*='fa-']{display:none !important}",
  ].join('\n');

  // ==================== 模块级状态（跨挂载/卸载保留） ====================
  var _da = Vue.reactive({
    dates: [],             // [{ date, status, createdAt }]
    selectedDate: '',      // 日期下拉选中值
    reportHtml: null,      // 报告 HTML（null = 无报告显示空态）
    loading: false,        // 生成中
    error: '',             // 错误信息（非空显示错误卡片）
    downloadDate: '',      // 下载按钮对应的报告日期
    agent: { busy: false, input: '', analysis: '', cards: [], meta: null, error: '', analyzedDate: '' },
    dateModal: { open: false, base: null, start: null, end: null, pickStart: true },
    pendingQuestion: '',
    // 钉钉推送设置（弹窗）
    push: {
      open: false,
      loading: false,
      saving: false,
      testing: false,
      pushing: false,
      hasSecret: false,
      hint: '',
      hintType: 'info',
      users: [],
      logs: [],
      form: { appKey: '', appSecret: '', robotCode: '', agentId: '', enabled: true, pushHour: 11, pushMinute: 0 },
      newUser: { name: '', mobile: '', userId: '', remark: '' },
    },
  });

  // ==================== 组件 ====================
  var DailyAnalysisPage = {
    components: { 'ecom-modal': EcomUI.Modal },
    setup() {
      var reportFrame = Vue.ref(null);

      // iframe 的 srcdoc：完整文档（内联样式 + 报告正文）
      var reportDoc = Vue.computed(function () {
        if (!_da.reportHtml) return '';
        return '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' + REPORT_CSS + '</style></head><body>' + _da.reportHtml + '</body></html>';
      });

      function onFrameLoad() {
        var f = reportFrame.value;
        if (!f) return;
        try {
          var h = f.contentDocument.body.scrollHeight;
          if (h > 0) f.style.height = (h + 4) + 'px';
        } catch (e) {}
      }

      // ---- 智能体卡片辅助 ----
      function tagType(t) { return ['store', 'platform', 'category', 'alert'].indexOf(t) >= 0 ? t : 'store'; }
      function splitTags(tags) {
        return String(tags || '').split(/[,，]/).filter(Boolean).map(function (x) { return x.trim(); });
      }

      // ---- 报告加载 ----
      function showReport(data) {
        _da.reportHtml = data.report || '';
        _da.downloadDate = data.reportDate || '';
        _da.error = '';
        _da.loading = false;
        if (data.reportDate) _da.selectedDate = data.reportDate;
      }
      function showEmpty() {
        _da.reportHtml = null;
        _da.downloadDate = '';
        _da.error = '';
        _da.loading = false;
      }
      function showError(msg) {
        _da.reportHtml = null;
        _da.downloadDate = '';
        _da.error = msg || '未知错误';
        _da.loading = false;
      }
      async function loadDates() {
        var dates = await ApiService.getAnalysisDates();
        if (dates) _da.dates = dates;
      }
      async function loadLatest() {
        var report = await ApiService.getAnalysisReport();
        if (report) showReport(report);
        else showEmpty();
      }
      function init() {
        loadDates();
        loadLatest();
      }

      function onDateChange() {
        var dt = _da.selectedDate;
        if (!dt) { loadLatest(); return; }
        ApiService.getAnalysisReport(dt).then(function (report) {
          if (report) showReport(report);
          else showError('未找到 ' + dt + ' 的分析报告，点击生成按钮即可创建');
        });
      }

      function generateBtnLabel() {
        return '生成' + (_da.selectedDate || '昨日') + '数据分析文档';
      }
      async function generateReport() {
        if (_da.loading) return;
        var selectedDate = _da.selectedDate || '';
        _da.loading = true;
        _da.error = '';
        _da.reportHtml = null;
        try {
          var result = await ApiService.generateDailyReport(selectedDate || null);
          _da.loading = false;
          if (result) {
            showReport(result);
            if (result.reportDate) _da.selectedDate = result.reportDate;
            loadDates();
            var label = result.generatedBy === 'ai' ? '已生成' : '已生成（模板模式）';
            App.showToast('分析报告' + label, 'success');
          } else {
            showError('生成失败，请检查后端日志或数据状态。');
          }
        } catch (e) {
          _da.loading = false;
          showError('请求异常: ' + (e.message || '未知错误'));
        }
      }

      function downloadReport() {
        var url = '/api/analysis/download';
        if (_da.downloadDate) url += '?date=' + encodeURIComponent(_da.downloadDate);
        var a = document.createElement('a');
        a.href = url;
        a.download = '';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        App.showToast('报告下载已开始', 'success');
      }

      // ---- 智能体 ----
      function _fmtDate(d) {
        return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
      }
      async function runAgent(q, start, end) {
        if (_da.agent.busy) return;
        var question = (q || '').trim();
        if (!question) { App.showToast('请输入分析需求', 'error'); return; }
        _da.agent.busy = true;
        _da.agent.input = '';
        _da.agent.analysis = '';
        _da.agent.cards = [];
        _da.agent.meta = null;
        _da.agent.error = '';
        _da.agent.analyzedDate = (start && end) ? (start === end ? start : start + ' ~ ' + end) : '';
        try {
          var data = await ApiService.runAnalysisAgent(question, start, end);
          if (data) {
            _da.agent.analysis = data.analysis || '';
            _da.agent.cards = data.cards || [];
            _da.agent.meta = data.meta || null;
          } else {
            _da.agent.error = '后端服务不可用，无法完成分析。';
          }
        } catch (e) {
          _da.agent.error = '分析失败：网络异常，请稍后再试。';
        } finally {
          _da.agent.busy = false;
        }
      }
      function agentSend() {
        runAgent(_da.agent.input, '', '');
      }
      // 快捷问题：先弹日期选择框，确认后分析
      function agentAsk(q) {
        _da.pendingQuestion = q;
        var y = new Date(Date.now() - 86400000);
        var dt = new Date(y.getFullYear(), y.getMonth(), y.getDate());
        _da.dateModal.start = dt;
        _da.dateModal.end = dt;
        _da.dateModal.pickStart = true;
        var now = new Date();
        _da.dateModal.base = new Date(now.getFullYear(), now.getMonth() - 1, 1);
        _da.dateModal.open = true;
      }
      function closeDateModal() {
        _da.dateModal.open = false;
      }
      function confirmDateModal() {
        var st = _da.dateModal;
        if (!st.start || !st.end) { App.showToast('请选择日期范围', 'error'); return; }
        var s = _fmtDate(st.start);
        var e = _fmtDate(st.end);
        _da.dateModal.open = false;
        runAgent(_da.pendingQuestion, s, e);
      }
      // 双月日历
      function _buildMonth(y, m, isLeft) {
        var st = _da.dateModal;
        var first = new Date(y, m, 1);
        var days = new Date(y, m + 1, 0).getDate();
        var startCol = (first.getDay() + 6) % 7;
        var now = new Date();
        var cells = [];
        for (var i = 0; i < startCol; i++) cells.push({ blank: true });
        for (var d = 1; d <= days; d++) {
          var dt = new Date(y, m, d);
          var cls = '';
          if (st.start && dt.getTime() === st.start.getTime()) cls += ' is-start';
          if (st.end && dt.getTime() === st.end.getTime()) cls += ' is-end';
          if (st.start && st.end && dt > st.start && dt < st.end) cls += ' in-range';
          if (dt.getFullYear() === now.getFullYear() && dt.getMonth() === now.getMonth() && dt.getDate() === now.getDate()) cls += ' is-today';
          cells.push({ blank: false, d: d, y: y, m: m, cls: cls });
        }
        return { y: y, m: m, isLeft: isLeft, cells: cells };
      }
      var modalCalMonths = Vue.computed(function () {
        var st = _da.dateModal;
        if (!st.base) return [];
        var leftY = st.base.getFullYear(), leftM = st.base.getMonth();
        var right = new Date(leftY, leftM + 1, 1);
        return [_buildMonth(leftY, leftM, true), _buildMonth(right.getFullYear(), right.getMonth(), false)];
      });
      function modalCalPick(y, m, d) {
        var st = _da.dateModal;
        var dt = new Date(y, m, d);
        if (st.pickStart) {
          st.start = dt; st.end = null; st.pickStart = false;
        } else {
          if (dt < st.start) { st.end = st.start; st.start = dt; }
          else { st.end = dt; }
          st.pickStart = true;
        }
      }
      function modalCalNav(delta) {
        _da.dateModal.base = new Date(_da.dateModal.base.getFullYear(), _da.dateModal.base.getMonth() + delta, 1);
      }
      function modalCalClear() {
        _da.dateModal.start = null; _da.dateModal.end = null; _da.dateModal.pickStart = true;
      }
      function modalCalToday() {
        var now = new Date();
        var dt = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        _da.dateModal.start = dt; _da.dateModal.end = dt; _da.dateModal.pickStart = true;
      }
      var modalDateLabel = Vue.computed(function () {
        var st = _da.dateModal;
        if (st.start && st.end) {
          var s = _fmtDate(st.start), e = _fmtDate(st.end);
          return s === e ? '已选：' + s : '已选：' + s + ' ~ ' + e;
        }
        return _da.dateModal.pickStart ? '请选择开始日期' : '请选择结束日期';
      });

      // ---- 钉钉推送设置 ----
      var hourOptions = [];
      for (var _h = 0; _h < 24; _h++) hourOptions.push(_h);
      var minuteOptions = [0, 15, 30, 45];

      function pad2(n) { return (n < 10 ? '0' : '') + n; }
      function setHint(text, type) {
        _da.push.hint = text || '';
        _da.push.hintType = type || 'info';
      }
      function logText(s) {
        return { success: '成功', partial: '部分成功', fail: '失败', skipped: '已跳过' }[s] || (s || '未知');
      }
      function logClass(s) {
        return { success: 'dt-ok', partial: 'dt-warn', fail: 'dt-bad', skipped: 'dt-off' }[s] || 'dt-off';
      }
      // ---- 弹窗顶部状态概览 ----
      var enabledUserCount = Vue.computed(function () {
        return _da.push.users.filter(function (u) { return u.enabled; }).length;
      });
      var credReady = Vue.computed(function () {
        var key = String(_da.push.form.appKey || '').trim();
        var secret = _da.push.hasSecret || !!String(_da.push.form.appSecret || '').trim();
        return !!(key && secret);
      });
      var lastLogText = Vue.computed(function () {
        var l = _da.push.logs[0];
        if (!l) return '暂无记录';
        // '2026-09-17 11:00:12' → '09-17 11:00'，概览卡一行放不下完整时间
        var t = String(l.createdAt || '');
        t = t.length >= 16 ? t.slice(5, 16) : t;
        return logText(l.status) + (t ? ' · ' + t : '');
      });
      var hintIcon = Vue.computed(function () {
        return { error: 'fa-circle-exclamation', ok: 'fa-circle-check' }[_da.push.hintType] || 'fa-circle-info';
      });
      function openPush() {
        _da.push.open = true;
        setHint('');
        loadPush();
      }
      function closePush() { _da.push.open = false; }

      async function loadPush() {
        _da.push.loading = true;
        var cfg = await ApiService.getPushConfig();
        _da.push.loading = false;
        if (!cfg) { setHint('读取推送设置失败，请检查后端服务是否正常', 'error'); return; }
        var f = _da.push.form;
        f.appKey = cfg.appKey || '';
        f.appSecret = cfg.appSecret || '';
        f.robotCode = cfg.robotCode || '';
        f.agentId = cfg.agentId || '';
        f.enabled = !!cfg.enabled;
        f.pushHour = typeof cfg.pushHour === 'number' ? cfg.pushHour : 11;
        f.pushMinute = typeof cfg.pushMinute === 'number' ? cfg.pushMinute : 0;
        _da.push.hasSecret = !!cfg.hasAppSecret;
        _da.push.users = cfg.users || [];
        _da.push.logs = cfg.logs || [];
      }

      async function savePush() {
        if (_da.push.saving) return;
        _da.push.saving = true;
        var r = await ApiService.savePushConfig(_da.push.form);
        _da.push.saving = false;
        if (r.ok) {
          App.showToast(r.msg || '设置已保存', 'success');
          setHint('设置已保存' + (_da.push.form.enabled
            ? '，将于每天 ' + pad2(_da.push.form.pushHour) + ':' + pad2(_da.push.form.pushMinute) + ' 自动推送'
            : '（自动推送当前为关闭状态）'), 'ok');
          loadPush();
        } else {
          App.showToast(r.msg || '保存失败', 'error');
          setHint(r.msg || '保存失败', 'error');
        }
      }

      async function addUser() {
        var u = _da.push.newUser;
        if (!u.name.trim()) { App.showToast('请填写成员姓名', 'error'); return; }
        if (!u.mobile.trim() && !u.userId.trim()) { App.showToast('请填写手机号或钉钉 userId', 'error'); return; }
        var r = await ApiService.addPushUser({
          name: u.name.trim(), mobile: u.mobile.trim(), userId: u.userId.trim(),
          remark: u.remark.trim(), enabled: true,
        });
        if (r.ok) {
          _da.push.newUser = { name: '', mobile: '', userId: '', remark: '' };
          App.showToast('已添加推送人', 'success');
          loadPush();
        } else {
          App.showToast(r.msg || '添加失败', 'error');
          setHint(r.msg || '添加失败', 'error');
        }
      }

      async function toggleUser(u) {
        var r = await ApiService.updatePushUser(u.id, { enabled: !u.enabled });
        if (r.ok) {
          u.enabled = !u.enabled;
          App.showToast(u.enabled ? '已启用该成员' : '已停用该成员', 'success');
        } else {
          App.showToast(r.msg || '操作失败', 'error');
        }
      }

      async function delUser(u) {
        if (!confirm('确定删除推送人「' + u.name + '」？')) return;
        var r = await ApiService.deletePushUser(u.id);
        if (r.ok) {
          App.showToast('已删除', 'success');
          loadPush();
        } else {
          App.showToast(r.msg || '删除失败', 'error');
        }
      }

      async function resolveUser(u) {
        if (!u.mobile) { App.showToast('该成员没有填手机号，请手动填写 userId', 'error'); return; }
        var r = await ApiService.resolvePushUser(u.mobile);
        if (r.ok) {
          await ApiService.updatePushUser(u.id, { userId: r.data.userId });
          App.showToast('已匹配到 userId', 'success');
          setHint('手机号 ' + u.mobile + ' 匹配到 userId：' + r.data.userId, 'ok');
          loadPush();
        } else {
          App.showToast(r.msg || '匹配失败', 'error');
          setHint(r.msg || '匹配失败', 'error');
        }
      }

      async function testPush() {
        if (_da.push.testing) return;
        _da.push.testing = true;
        setHint('正在发送测试消息...', 'info');
        var r = await ApiService.testPush();
        _da.push.testing = false;
        if (r.ok) {
          App.showToast(r.msg || '测试完成', 'success');
          setHint('测试结果：' + ((r.data && r.data.detail) || r.msg || '已完成'), 'ok');
        } else {
          App.showToast(r.msg || '测试失败', 'error');
          setHint('测试失败：' + (r.msg || '未知错误'), 'error');
        }
        loadPush();
      }

      async function pushNow() {
        if (_da.push.pushing) return;
        _da.push.pushing = true;
        setHint('正在生成昨日报告并推送（约需 20-60 秒）...', 'info');
        var r = await ApiService.pushNow('');
        _da.push.pushing = false;
        if (r.ok) {
          App.showToast('已推送昨日报告', 'success');
          setHint('推送完成（' + ((r.data && r.data.reportDate) || '') + '）：'
            + ((r.data && r.data.detail) || ''), 'ok');
        } else {
          App.showToast(r.msg || '推送失败', 'error');
          setHint('推送失败：' + (r.msg || '未知错误'), 'error');
        }
        loadPush();
      }

      Vue.onMounted(function () { init(); });

      return {
        da: _da, reportDoc: reportDoc, reportFrame: reportFrame,
        onFrameLoad: onFrameLoad,
        tagType: tagType, splitTags: splitTags,
        onDateChange: onDateChange, generateBtnLabel: generateBtnLabel,
        generateReport: generateReport, downloadReport: downloadReport,
        agentAsk: agentAsk, agentSend: agentSend, closeDateModal: closeDateModal, confirmDateModal: confirmDateModal,
        modalCalMonths: modalCalMonths, modalCalPick: modalCalPick, modalCalNav: modalCalNav, modalCalClear: modalCalClear, modalCalToday: modalCalToday,
        modalDateLabel: modalDateLabel,
        // 钉钉推送
        hourOptions: hourOptions, minuteOptions: minuteOptions, pad2: pad2,
        logText: logText, logClass: logClass,
        enabledUserCount: enabledUserCount, credReady: credReady,
        lastLogText: lastLogText, hintIcon: hintIcon,
        openPush: openPush, closePush: closePush, savePush: savePush,
        addUser: addUser, toggleUser: toggleUser, delUser: delUser, resolveUser: resolveUser,
        testPush: testPush, pushNow: pushNow,
      };
    },

    template: `
<div>
  <div class="da-layout">
    <div class="da-left">
      <!-- ====== 页头 ====== -->
      <div class="dashboard-header">
        <div class="dh-left">
          <div class="dh-icon" style="background:linear-gradient(135deg,#0d9488,#14b8a6);box-shadow:0 2px 8px rgba(13,148,136,0.25)"><i class="fa-solid fa-robot" style="color:#fff;font-size:18px"></i></div>
          <div class="dh-title-group"><h2 class="dh-title">每日数据分析</h2><span class="dh-subtitle">Daily AI-Powered Analytics</span><span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>AI驱动 · 昨日数据自动分析</span></div>
        </div>
        <div class="dh-filters">
          <select v-model="da.selectedDate" class="dh-select" style="min-width:160px" @change="onDateChange">
            <option value="">选择历史报告...</option>
            <option v-for="d in da.dates" :key="d.date" :value="d.date">{{ d.date }}<template v-if="d.status !== 'success'"> (生成失败)</template></option>
          </select>
          <button @click="generateReport" :disabled="da.loading" style="background:linear-gradient(135deg,#0d9488,#14b8a6);color:#fff;border:none;padding:8px 20px;border-radius:10px;font-weight:600;cursor:pointer;font-family:inherit;font-size:0.88rem;box-shadow:0 2px 8px rgba(13,148,136,0.2)">
            <i v-if="da.loading" class="fa-solid fa-spinner fa-spin"></i><i v-else class="fa-solid fa-wand-magic-sparkles"></i> {{ generateBtnLabel() }}
          </button>
          <button v-show="da.downloadDate" @click="downloadReport" style="background:#fff;color:#0d9488;border:1.5px solid #0d9488;padding:8px 20px;border-radius:10px;font-weight:600;cursor:pointer;font-family:inherit;font-size:0.88rem;margin-left:10px"><i class="fa-solid fa-file-pdf"></i> 下载PDF</button>
          <button @click="openPush" style="background:#fff;color:#1e80ff;border:1.5px solid #1e80ff;padding:8px 20px;border-radius:10px;font-weight:600;cursor:pointer;font-family:inherit;font-size:0.88rem;margin-left:10px"><i class="fa-solid fa-paper-plane"></i> 钉钉推送</button>
        </div>
      </div>

      <!-- ====== 加载中 ====== -->
      <div v-if="da.loading" style="text-align:center;padding:48px;color:#64748b"><i class="fa-solid fa-spinner fa-spin" style="font-size:32px;margin-bottom:16px;display:block;color:#0d9488"></i><p style="font-size:0.95rem">AI 正在分析昨日数据，请稍候...</p><p style="font-size:0.8rem;color:#94a3b8">预计耗时 10-30 秒</p></div>

      <!-- ====== 报告（iframe 隔离渲染） ====== -->
      <div v-else-if="da.reportHtml" class="da-report-wrapper">
        <iframe ref="reportFrame" :srcdoc="reportDoc" @load="onFrameLoad" style="width:100%;border:0;display:block"></iframe>
      </div>

      <!-- ====== 错误 ====== -->
      <div v-else-if="da.error" class="da-error-card">
        <i class="fa-solid fa-circle-exclamation"></i>
        <h3>报告生成失败</h3>
        <p>{{ da.error }}</p>
      </div>

      <!-- ====== 空态 ====== -->
      <div v-else class="placeholder-card"><i class="fa-solid fa-robot" style="color:#0d9488"></i><h3>每日 AI 数据分析</h3><p style="max-width:440px;margin:8px auto 20px;line-height:1.6">点击上方按钮，AI 将自动分析昨日的店铺营销数据与抖店/京东/千牛单链接销售数据，重点评估推广花费/消耗与产出的比例，生成专业的 HTML 分析报告。</p><div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap"><div style="background:#f0fdfa;border-radius:10px;padding:12px 16px;min-width:100px"><i class="fa-solid fa-chart-line" style="color:#0d9488;margin-right:6px"></i><span style="font-size:0.82rem;color:#0f766e;font-weight:500">营销数据综合</span></div><div style="background:#f0fdfa;border-radius:10px;padding:12px 16px;min-width:100px"><i class="fa-solid fa-receipt" style="color:#0d9488;margin-right:6px"></i><span style="font-size:0.82rem;color:#0f766e;font-weight:500">单链接数据</span></div><div style="background:#f0fdfa;border-radius:10px;padding:12px 16px;min-width:100px"><i class="fa-solid fa-scale-balanced" style="color:#0d9488;margin-right:6px"></i><span style="font-size:0.82rem;color:#0f766e;font-weight:500">消耗产出比</span></div></div></div>
    </div>

    <!-- ====== 数据分析智能体（右侧） ====== -->
    <div class="da-right">
      <div class="da-agent">
        <div class="da-agent-header">
          <div class="da-agent-title"><i class="fa-solid fa-chart-line" style="color:#0d9488;margin-right:8px"></i>数据分析智能体</div>
          <span class="da-agent-sub">基于 店铺营销 / 单链接 / 推广 / 品类 数据</span>
        </div>
        <div class="da-agent-suggest">
          <button class="da-agent-chip" @click="agentAsk('今天的经营健康度如何，哪些地方需要优化')">📊 经营健康度诊断</button>
          <button class="da-agent-chip" @click="agentAsk('哪个店铺最需要关注，哪些品类在亏损')">🏪 店铺与品类诊断</button>
          <button class="da-agent-chip" @click="agentAsk('各平台投放效率如何，ROI 表现怎么样')">💰 投放效率分析</button>
        </div>
        <div class="da-agent-result">
          <div v-if="da.agent.busy" class="sa-loading"><i class="fa-solid fa-spinner fa-spin"></i> 正在检索经营数据并分析，请稍候...</div>
          <div v-else-if="da.agent.analysis || da.agent.cards.length || da.agent.meta">
            <div v-if="da.agent.analysis" class="sa-analysis">{{ da.agent.analysis }}</div>
            <div v-if="da.agent.cards.length" class="sa-cards-title"><i class="fa-solid fa-lightbulb" style="color:#f59e0b"></i> 关键数据卡片</div>
            <div v-for="(c, i) in da.agent.cards" :key="i" class="sa-card">
              <div class="sa-card-top">
                <div class="sa-card-title">{{ c.title }}</div>
                <div class="sa-card-metric">{{ c.metric }}</div>
              </div>
              <div v-if="c.subtitle" class="sa-card-sub">{{ c.subtitle }}</div>
              <div v-if="splitTags(c.tags).length" class="sa-card-tags"><span v-for="(t, ti) in splitTags(c.tags)" :key="ti" class="sa-tag" :class="tagType(c.type)">{{ t }}</span></div>
              <div v-if="c.reason" class="sa-card-reason">{{ c.reason }}</div>
            </div>
            <div v-if="da.agent.meta" class="sa-meta">数据日期：{{ da.agent.meta.date }}<template v-if="da.agent.meta['净支付金额'] !== undefined"> · 净支付 {{ da.agent.meta['净支付金额'] }} 元</template><template v-if="da.agent.meta['ROI'] !== undefined"> · ROI {{ da.agent.meta['ROI'] }}</template></div>
          </div>
          <div v-else-if="da.agent.error" class="sa-analysis" style="color:#dc2626">{{ da.agent.error }}</div>
          <div v-else class="da-agent-empty">
            <i class="fa-solid fa-robot" style="font-size:28px;color:#cbd5e1"></i>
            <p>点击上方快捷问题，或输入你的经营分析需求，智能体会先检索当日经营数据，再生成分析。</p>
          </div>
        </div>
        <div class="da-agent-input">
          <textarea v-model="da.agent.input" class="da-agent-textarea" placeholder="例如：今天的退款率为什么偏高..." @keydown.enter.exact.prevent="agentSend"></textarea>
          <button class="da-agent-send" :disabled="da.agent.busy" @click="agentSend"><i class="fa-solid fa-paper-plane"></i></button>
        </div>
      </div>
    </div>
  </div>

  <!-- ====== 智能体日期选择弹窗 ====== -->
  <ecom-modal :visible="da.dateModal.open" title="选择分析日期" width="680px" save-text="确认分析" @close="closeDateModal" @save="confirmDateModal">
    <div class="od-cal-wrap" style="justify-content:center">
      <div class="od-cal" v-for="(mo, mi) in modalCalMonths" :key="mi">
        <div class="od-cal-head">
          <button v-if="mo.isLeft" type="button" class="od-cal-nav" @click="modalCalNav(-1)">‹</button>
          <span v-else style="width:24px"></span>
          <span>{{ mo.y }}年{{ mo.m + 1 }}月</span>
          <button v-if="!mo.isLeft" type="button" class="od-cal-nav" @click="modalCalNav(1)">›</button>
          <span v-else style="width:24px"></span>
        </div>
        <div class="od-cal-week"><span v-for="w in ['一','二','三','四','五','六','日']" :key="w">{{ w }}</span></div>
        <div class="od-cal-days">
          <template v-for="(c, ci) in mo.cells" :key="ci">
            <span v-if="c.blank" class="od-cal-day blank"></span>
            <button v-else type="button" class="od-cal-day" :class="c.cls" @click="modalCalPick(c.y, c.m, c.d)">{{ c.d }}</button>
          </template>
        </div>
      </div>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:10px;padding-top:10px;border-top:1px solid #f1f5f9">
      <span style="font-size:12px;color:#64748b">{{ modalDateLabel }}</span>
      <div style="display:flex;gap:10px">
        <button type="button" @click="modalCalClear" style="border:none;background:none;color:#94a3b8;font-size:12px;cursor:pointer">清除</button>
        <button type="button" @click="modalCalToday" style="border:none;background:none;color:#6366f1;font-size:12px;cursor:pointer;font-weight:600">今天</button>
      </div>
    </div>
  </ecom-modal>

  <!-- ====== 钉钉推送设置弹窗 ====== -->
  <ecom-modal :visible="da.push.open" title="钉钉推送设置" width="880px" save-text="保存设置" @close="closePush" @save="savePush">
    <div class="dt-push">
      <div v-if="da.push.hint" class="dt-hint" :class="'dt-hint-' + da.push.hintType">
        <i class="fa-solid" :class="hintIcon"></i><span>{{ da.push.hint }}</span>
      </div>
      <div v-if="da.push.loading" class="dt-loading"><i class="fa-solid fa-spinner fa-spin"></i> 正在读取设置…</div>

      <!-- 状态概览 -->
      <div v-if="!da.push.loading" class="dt-ov">
        <div class="dt-ov-item">
          <span class="dt-ov-k">自动推送</span>
          <b class="dt-ov-v" :class="da.push.form.enabled ? 'dt-v-on' : 'dt-v-off'">{{ da.push.form.enabled ? '已开启' : '已关闭' }}</b>
        </div>
        <div class="dt-ov-item">
          <span class="dt-ov-k">推送时间</span>
          <b class="dt-ov-v">每天 {{ pad2(da.push.form.pushHour) }}:{{ pad2(da.push.form.pushMinute) }}</b>
        </div>
        <div class="dt-ov-item">
          <span class="dt-ov-k">收件人</span>
          <b class="dt-ov-v">{{ enabledUserCount }} / {{ da.push.users.length }} 人启用</b>
        </div>
        <div class="dt-ov-item">
          <span class="dt-ov-k">最近一次推送</span>
          <b class="dt-ov-v">{{ lastLogText }}</b>
        </div>
      </div>

      <!-- 推送计划 + 应用凭证 -->
      <div class="dt-cols">
        <section class="dt-sec">
          <div class="dt-sec-head">
            <span class="dt-sec-ico"><i class="fa-solid fa-clock"></i></span>
            <h4>推送计划</h4>
          </div>
          <div class="dt-sec-body">
            <label class="dt-switch">
              <input type="checkbox" v-model="da.push.form.enabled">
              <span>每天自动生成<b>昨日</b>报告并推送</span>
            </label>
            <div class="dt-time-row">
              <span class="dt-time-k">推送时间</span>
              <div class="dt-time">
                <select v-model.number="da.push.form.pushHour" class="dt-input dt-input-sm">
                  <option v-for="h in hourOptions" :key="h" :value="h">{{ pad2(h) }}</option>
                </select>
                <span class="dt-colon">:</span>
                <select v-model.number="da.push.form.pushMinute" class="dt-input dt-input-sm">
                  <option v-for="m in minuteOptions" :key="m" :value="m">{{ pad2(m) }}</option>
                </select>
              </div>
            </div>
            <p class="dt-note"><i class="fa-solid fa-circle-info"></i><span>数据未就绪时每 15 分钟重试，最晚 12:30；服务重启当天未成功会自动补跑。</span></p>
          </div>
        </section>

        <section class="dt-sec">
          <div class="dt-sec-head">
            <span class="dt-sec-ico"><i class="fa-solid fa-key"></i></span>
            <h4>应用凭证</h4>
            <span class="dt-badge" :class="credReady ? 'dt-ok' : 'dt-warn'">
              <i class="fa-solid" :class="credReady ? 'fa-check' : 'fa-triangle-exclamation'"></i>{{ credReady ? '已配置' : '待配置' }}
            </span>
          </div>
          <div class="dt-sec-body">
            <div class="dt-grid">
              <label class="dt-field">
                <span>Client ID（原 AppKey）*</span>
                <input v-model="da.push.form.appKey" class="dt-input" placeholder="ding 开头，非 App ID">
              </label>
              <label class="dt-field">
                <span>Client Secret（原 AppSecret）*</span>
                <input v-model="da.push.form.appSecret" type="password" class="dt-input" :placeholder="da.push.hasSecret ? '已保存（留空不变）' : '请输入 Client Secret'">
              </label>
              <label class="dt-field">
                <span>App ID / AgentId（可选）</span>
                <input v-model="da.push.form.agentId" class="dt-input" placeholder="UUID 格式">
              </label>
              <label class="dt-field">
                <span>robotCode（可选）</span>
                <input v-model="da.push.form.robotCode" class="dt-input" placeholder="留空自动尝试">
              </label>
            </div>
            <details class="dt-faq">
              <summary>这些凭证去哪儿复制？</summary>
              <p>钉钉开放平台 → 应用开发 → 企业内部应用「小钉」→ 基础信息 → 凭证与基础信息。</p>
              <p>该页有三个值：<b>Client ID</b>（ding 开头，旧称 AppKey）与 <b>Client Secret</b> 用于换取 accessToken；<b>App ID</b>（UUID 格式，即旧版 AgentId）只是标识，填进第三格即可。填错会提示「无效的 clientId 或 clientSecret」。</p>
              <p>Client Secret 保存在服务器数据库、不回传页面；收件人必须在应用的「可见范围」内。</p>
            </details>
          </div>
        </section>
      </div>

      <!-- 推送人 -->
      <section class="dt-sec">
        <div class="dt-sec-head">
          <span class="dt-sec-ico"><i class="fa-solid fa-users"></i></span>
          <h4>推送人</h4>
          <span class="dt-sec-sub">{{ enabledUserCount }} / {{ da.push.users.length }} 人启用</span>
        </div>
        <div class="dt-sec-body">
          <div class="dt-table-wrap">
            <table class="dt-table">
              <thead>
                <tr><th>姓名</th><th>手机号</th><th>userId</th><th class="c" style="width:92px">状态</th><th class="r" style="width:180px">操作</th></tr>
              </thead>
              <tbody>
                <tr v-for="u in da.push.users" :key="u.id">
                  <td class="dt-name">{{ u.name }}</td>
                  <td class="dt-mono">{{ u.mobile || '—' }}</td>
                  <td class="dt-mono">{{ u.userId || '—' }}</td>
                  <td class="c">
                    <span class="dt-badge" :class="u.enabled ? 'dt-ok' : 'dt-off'">
                      <i class="fa-solid" :class="u.enabled ? 'fa-check' : 'fa-minus'"></i>{{ u.enabled ? '已启用' : '已停用' }}
                    </span>
                  </td>
                  <td class="r">
                    <button type="button" class="dt-mini" @click="toggleUser(u)">{{ u.enabled ? '停用' : '启用' }}</button>
                    <button type="button" class="dt-mini" @click="resolveUser(u)">匹配ID</button>
                    <button type="button" class="dt-mini dt-mini-danger" @click="delUser(u)">删除</button>
                  </td>
                </tr>
                <tr v-if="!da.push.users.length"><td colspan="5" class="dt-empty">还没有推送人，在下方添加</td></tr>
              </tbody>
            </table>
          </div>
          <div class="dt-add">
            <input v-model="da.push.newUser.name" class="dt-input" placeholder="姓名，如「乐心」">
            <input v-model="da.push.newUser.mobile" class="dt-input" placeholder="手机号（可选）">
            <input v-model="da.push.newUser.userId" class="dt-input" placeholder="userId（可选）">
            <button type="button" class="dt-btn dt-btn-primary" @click="addUser"><i class="fa-solid fa-plus"></i> 添加</button>
          </div>
          <details class="dt-faq">
            <summary>手机号和 userId 该填哪个？</summary>
            <p>两者二选一即可。<b>只填 userId 最省事</b>，不依赖任何通讯录权限。</p>
            <p>只填手机号时，首次推送会调钉钉接口换取并缓存 userId，要求应用已开通「手机号获取成员信息」权限（qyapi_get_member_by_mobile）。</p>
            <p>不论填哪种，成员都必须在该应用的「可见范围」内，否则会返回「不在可见范围」。</p>
          </details>
        </div>
      </section>

      <!-- 测试与推送 -->
      <section class="dt-sec">
        <div class="dt-sec-head">
          <span class="dt-sec-ico"><i class="fa-solid fa-paper-plane"></i></span>
          <h4>测试与手动推送</h4>
        </div>
        <div class="dt-sec-body">
          <div class="dt-actions">
            <button type="button" class="dt-btn" :disabled="da.push.testing" @click="testPush">
              <i class="fa-solid fa-vial"></i> {{ da.push.testing ? '发送中…' : '发送测试消息' }}
            </button>
            <button type="button" class="dt-btn dt-btn-primary" :disabled="da.push.pushing" @click="pushNow">
              <i class="fa-solid fa-bolt"></i> {{ da.push.pushing ? '推送中…' : '立即推送昨日报告' }}
            </button>
            <span class="dt-tip">会重新生成昨日报告，发送「指标摘要 + PDF 附件」</span>
          </div>
        </div>
      </section>

      <!-- 推送记录 -->
      <section class="dt-sec">
        <div class="dt-sec-head">
          <span class="dt-sec-ico"><i class="fa-solid fa-clock-rotate-left"></i></span>
          <h4>最近推送记录</h4>
          <span class="dt-sec-sub">共 {{ da.push.logs.length }} 条</span>
        </div>
        <div class="dt-sec-body">
          <div class="dt-table-wrap">
            <table class="dt-table">
              <thead>
                <tr><th style="width:150px">推送时间</th><th style="width:100px">报告日期</th><th style="width:96px">结果</th><th>详情</th></tr>
              </thead>
              <tbody>
                <tr v-for="l in da.push.logs" :key="l.id">
                  <td class="dt-mono">{{ l.createdAt }}</td>
                  <td>{{ l.reportDate }}</td>
                  <td><span class="dt-badge" :class="logClass(l.status)">{{ logText(l.status) }}</span></td>
                  <td class="dt-detail">{{ l.detail }}</td>
                </tr>
                <tr v-if="!da.push.logs.length"><td colspan="4" class="dt-empty">暂无推送记录</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  </ecom-modal>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _daApp = null;

  function mountDailyVue() {
    if (_daApp) return;
    var oldSection = document.getElementById('page-daily-analysis');
    var mount = document.getElementById('page-daily-analysis-vue');
    if (!oldSection || !mount) return;
    oldSection.style.display = 'none';
    mount.classList.remove('hidden');
    _daApp = Vue.createApp(DailyAnalysisPage);
    _daApp.mount(mount);
  }

  function unmountDailyVue() {
    if (!_daApp) return;
    _daApp.unmount();
    _daApp = null;
    var mount = document.getElementById('page-daily-analysis-vue');
    if (mount) { mount.classList.add('hidden'); mount.innerHTML = ''; }
    var oldSection = document.getElementById('page-daily-analysis');
    if (oldSection) oldSection.style.display = '';
  }

  function initHook() {
    var oldSection = document.getElementById('page-daily-analysis');
    if (!oldSection) return;
    var isVisible = !oldSection.classList.contains('hidden');
    if (isVisible) { mountDailyVue(); return; }
    var observer = new MutationObserver(function () {
      var nowVisible = !oldSection.classList.contains('hidden');
      if (nowVisible && !_daApp) mountDailyVue();
      else if (!nowVisible && _daApp) unmountDailyVue();
    });
    observer.observe(oldSection, { attributes: true, attributeFilter: ['class'] });
  }

  initHook();
})();
