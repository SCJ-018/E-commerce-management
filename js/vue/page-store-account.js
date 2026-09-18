/**
 * 店铺账号管理 — Vue 版（重做：三合一 Tab，真实接口）
 * classic script，与 app.js 共用全局词法作用域。
 * - 千牛 / 抖店 / 京东 三个 Tab（品牌色图标），数据来自 backend 三张账号表
 * - 抖店 Tab 内嵌「登录邮箱」独立区域（抖店邮箱账号表）
 * - 每店「运营中」开关（关掉＝不抓数）
 * - 增删改 + 更新数据（抓取触发）悬浮弹窗
 * XSS：全部 {{ }} 插值，绝不用 v-html；模板不裸访问嵌套属性
 */
(function () {
  'use strict';

  // 各类型字段定义（前端 key -> 中文标签）
  var FIELDS = {
    qianniu: [
      { key: 'account', label: '账号', required: true, placeholder: '店铺名:子账号' },
      { key: 'password', label: '密码' },
      { key: 'shopId', label: '店铺ID' },
      { key: 'brand', label: '品牌' },
      { key: 'remark', label: '备注' },
    ],
    doudian: [
      { key: 'shopName', label: '店铺名', required: true },
      { key: 'shopId', label: '店铺ID' },
      { key: 'brand', label: '品牌' },
      { key: 'remark', label: '备注' },
    ],
    'doudian-email': [
      { key: 'email', label: '邮箱', required: true },
      { key: 'password', label: '密码' },
      { key: 'remark', label: '备注' },
    ],
    jd: [
      { key: 'account', label: '账号', required: true },
      { key: 'password', label: '密码' },
      { key: 'shopId', label: '店铺ID' },
      { key: 'shopName', label: '店铺名' },
      { key: 'remark', label: '备注' },
    ],
  };

  // 三个平台 Tab：品牌色渐变（图标不再用纯黑/纯灰）
  var TABS = [
    { key: 'qianniu', label: '千牛', icon: 'fa-shop', grad: 'linear-gradient(135deg,#ff9500,#ff5000)' },
    { key: 'doudian', label: '抖店', icon: 'fa-clapperboard', grad: 'linear-gradient(135deg,#00d2ff,#0091ff)' },
    { key: 'jd', label: '京东', icon: 'fa-cube', grad: 'linear-gradient(135deg,#ff6a6a,#e1251b)' },
  ];

  var _sa = Vue.reactive({
    tab: 'qianniu',
    qianniu: [], doudian: [], emails: [], jd: [],
    search: '', loading: false,
    // 编辑弹窗
    modal: false, modalType: 'qianniu', editingId: null, form: {}, saving: false,
    // 更新数据弹窗
    showUpdate: false, upStart: '', upEnd: '', selected: {}, upShopSearch: '',
    // 抓取任务（更新数据 → 后端唤起 /opt/pw 抓取程序，轮询进度）
    view: 'form', job: null, submitting: false, fetchReady: true, fetchErr: '',
    // 每日抓取对账记录（抓取程序跑完与账号列表对照的结果）
    recon: [], reconLoading: false,
    // 抖店「手动拖滑块」：任务只能在本机执行（服务器没窗口能拖），
    // 页面只负责下发任务 + 轮询回显，执行端是 tools/doudian_crawler/slider_agent.py
    slider: null, sliderOpen: false, sliderBusy: false, sliderErr: '',
    // 抖店邮箱账号表的登录态时效（徽章 + 「是不是该去拖滑块了」）
    loginState: null,
    // 「更新数据」被登录态时效拦下时置 true，用于在错误提示旁挂「去拖滑块」按钮
    fetchErrSlider: false,
  });

  // 轮询定时器（非响应式，避免被 Vue 代理）
  var _pollTimer = null;

  // ★ in-flight 守卫（2026-09-17）
  //   服务器跑抓取时，被 headless Chrome（swiftshader 软件渲染）吃到 CPU 满载，
  //   /api/fetch/job 的响应会从几十毫秒掉到几秒。原来 setInterval(pollJob, 3000)
  //   不等待上一次返回 → 在途请求成倍堆积；等服务器缓过来，一批响应同时到达，
  //   Vue 要一口气做完全部响应式更新 + DOM patch，渲染进程主线程被占满，
  //   浏览器标题栏直接变「未响应」。
  //   现在：上一发没回来就跳过这一拍；但超过 POLL_TIMEOUT_MS 仍未回来则放行，
  //   避免请求永久挂住（fetch 本身没有超时）把轮询彻底卡死。
  var _pollBusy = false;
  var _pollStartAt = 0;
  var POLL_TIMEOUT_MS = 15000;

  // 滑块任务轮询：同样是 3 秒一拍，同样带 in-flight 守卫（理由同上）
  var _sliderTimer = null;
  var _sliderPollBusy = false;

  // 日期选择器的原生 input 引用（用于 showPicker 强制弹出日历）
  var upStartInput = Vue.ref(null);
  var upEndInput = Vue.ref(null);

  function currentList() {
    return { qianniu: _sa.qianniu, doudian: _sa.doudian, jd: _sa.jd }[_sa.tab] || [];
  }

  function _makeForm(type) {
    var f = {};
    FIELDS[type].forEach(function (x) { f[x.key] = ''; });
    return f;
  }

  var StoreAccountPage = {
    setup() {
      var filtered = Vue.computed(function () {
        var kw = _sa.search.trim().toLowerCase();
        var list = currentList();
        if (!kw) return list;
        var keys = FIELDS[_sa.tab].map(function (f) { return f.key; });
        return list.filter(function (r) {
          return keys.some(function (k) {
            var v = r[k];
            return v != null && String(v).toLowerCase().indexOf(kw) >= 0;
          });
        });
      });
      var counts = Vue.computed(function () {
        var list = currentList();
        var active = list.filter(function (r) { return r.active; }).length;
        return { total: list.length, active: active, inactive: list.length - active };
      });
      var modalFields = Vue.computed(function () { return FIELDS[_sa.modalType]; });
      var updateShops = Vue.computed(function () {
        return [].concat(
          _sa.qianniu.map(function (r) { return { key: 'qianniu:' + r.id, label: r.account, active: r.active }; }),
          _sa.doudian.map(function (r) { return { key: 'doudian:' + r.id, label: r.shopName, active: r.active }; }),
          _sa.jd.map(function (r) { return { key: 'jd:' + r.id, label: (r.shopName || r.account), active: r.active }; })
        );
      });
      var selectedCount = Vue.computed(function () {
        return Object.keys(_sa.selected).filter(function (k) { return _sa.selected[k]; }).length;
      });
      // 按平台分组，供更新数据弹窗分区块展示
      var groupedShops = Vue.computed(function () {
        return [
          { key: 'qianniu', label: '千牛', grad: TABS[0].grad, shops: _sa.qianniu.map(function (r) { return { key: 'qianniu:' + r.id, label: r.account, active: r.active }; }) },
          { key: 'doudian', label: '抖店', grad: TABS[1].grad, shops: _sa.doudian.map(function (r) { return { key: 'doudian:' + r.id, label: r.shopName, active: r.active }; }) },
          { key: 'jd', label: '京东', grad: TABS[2].grad, shops: _sa.jd.map(function (r) { return { key: 'jd:' + r.id, label: (r.shopName || r.account), active: r.active }; }) },
        ];
      });
      var upFilteredGroups = Vue.computed(function () {
        var kw = _sa.upShopSearch.trim().toLowerCase();
        if (!kw) return groupedShops.value;
        return groupedShops.value.map(function (g) {
          return {
            key: g.key, label: g.label, grad: g.grad,
            shops: g.shops.filter(function (s) { return s.label.toLowerCase().indexOf(kw) >= 0; }),
          };
        });
      });

      // ---- 抖店「手动拖滑块」：状态文案 / 徽章 / 是否可重试 ----
      // 把后端返回的 status 映射成人话 + 图标（服务器只负责记任务，执行在本机）
      var SLIDER_TEXT = {
        idle: { icon: 'fa-circle-info', title: '尚未下发任务', desc: '稍等，正在获取当前状态…' },
        pending: { icon: 'fa-paper-plane', title: '任务已下发', desc: '正在等待本机滑块助手领取…' },
        claimed: { icon: 'fa-spinner fa-spin', title: '本机助手已领取', desc: '正在打开本机 Chrome…' },
        running: { icon: 'fa-hand-pointer', title: '请拖动滑块', desc: '本机 Chrome 已打开 —— 完成邮箱登录、把拼图滑块拖过去即可。登录态会自动上传，无需其他操作。' },
        done: { icon: 'fa-circle-check', title: '登录态已更新', desc: '已上传到服务器云库。请立刻去点「更新数据」（有效期约 40 分钟）。' },
        fail: { icon: 'fa-circle-exclamation', title: '本机登录未完成', desc: '请查看本机助手窗口里的日志，然后点「重试」。' },
        timeout: { icon: 'fa-clock', title: '等待超时', desc: '任务超过 10 分钟没有完成。请确认本机助手在运行，然后点「重试」。' },
      };
      var SLIDER_BUSY_ST = ['pending', 'claimed', 'running'];

      // ★ 这几个是 setup 的顶层绑定，模板里必须裸写（sliderInfo.title），
      //   不能写成 state.sliderInfo.title —— 那样取到 undefined、绑定静默失效。
      var sliderStatus = Vue.computed(function () {
        return (_sa.slider && _sa.slider.status) || 'idle';
      });
      var sliderInfo = Vue.computed(function () {
        return SLIDER_TEXT[sliderStatus.value] || SLIDER_TEXT.idle;
      });
      var canRetrySlider = Vue.computed(function () {
        return SLIDER_BUSY_ST.indexOf(sliderStatus.value) < 0;
      });
      // 登录邮箱徽章：把「上次保存登录态过去多久」翻译成「现在能不能抓」
      var loginBadge = Vue.computed(function () {
        var l = _sa.loginState;
        if (!l) return { text: '登录态检测中…', cls: 'sa-login-unknown', tip: '' };
        if (!l.hasState) {
          return { text: '从未登录', cls: 'sa-login-bad',
                   tip: '抖店从未保存过登录态，抓取必然失败，请先拖一次滑块' };
        }
        var age = l.ageMinutes;
        if (age === null || age === undefined) age = 0;
        if (l.expired) {
          return { text: '已过期 ' + age + ' 分钟', cls: 'sa-login-bad',
                   tip: '实测寿命约 40 分钟，已超过预检阈值 ' + l.maxMinutes + ' 分钟，抓取会被服务器拦下' };
        }
        var left = l.maxMinutes - age;
        return { text: '有效 · 剩余约 ' + left + ' 分钟',
                 cls: left <= 8 ? 'sa-login-warn' : 'sa-login-ok',
                 tip: '最后更新：' + (l.updatedAt || '') };
      });

      async function loadAll() {
        _sa.loading = true;
        var parts = await Promise.all([
          ApiService.getStoreAccounts('qianniu'),
          ApiService.getStoreAccounts('doudian'),
          ApiService.getStoreAccounts('doudian-email'),
          ApiService.getStoreAccounts('jd'),
        ]);
        _sa.qianniu = parts[0] || [];
        _sa.doudian = parts[1] || [];
        _sa.emails = parts[2] || [];
        _sa.jd = parts[3] || [];
        _sa.loading = false;
      }

      // 抓取环境是否就绪（站点与抓取程序同机时才可触发）
      async function loadFetchStatus() {
        var r = await ApiService.getFetchStatus();
        _sa.fetchReady = !r || r.ready !== false;
      }

      // 每日对账记录：抓取程序跑完 vs 账号表「运营中」列表
      async function loadRecon() {
        _sa.reconLoading = true;
        var r = await ApiService.getFetchReconcile(30);
        _sa.recon = r || [];
        _sa.reconLoading = false;
      }

      Vue.onMounted(function () {
        loadAll();
        loadFetchStatus();
        loadRecon();
        loadSliderStatus();   // 登录邮箱徽章（登录态还剩多久）
        // 刷新页面后若已有任务在跑，自动接回进度
        ApiService.getFetchLatestJob().then(function (j) {
          if (j && j.status === 'running') {
            _sa.job = j;
            _sa.view = 'run';
            _sa.showUpdate = true;
            startPoll();
          }
        });
      });

      Vue.onUnmounted(function () { stopPoll(); stopSliderPoll(); });

      // ---------------- 抓取任务轮询 ----------------
      function stopPoll() {
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
        _pollBusy = false;
        _pollStartAt = 0;
      }
      function startPoll() {
        stopPoll();
        _pollTimer = setInterval(pollJob, 3000);
      }
      async function pollJob() {
        if (!_sa.job || !_sa.job.jobId) { stopPoll(); return; }
        // 上一发还在途 → 跳过这一拍（超过兜底时限才放行，见顶部说明）
        if (_pollBusy && (Date.now() - _pollStartAt) < POLL_TIMEOUT_MS) return;
        _pollBusy = true;
        _pollStartAt = Date.now();
        var j = null;
        try {
          j = await ApiService.getFetchJob(_sa.job.jobId);
        } finally {
          _pollBusy = false;
        }
        if (!j) return;
        _sa.job = j;
        if (j.status !== 'running') {
          stopPoll();
          if (j.status === 'done') {
            App.showToast(j.missing ? '抓取完成，但有店铺未落库' : '抓取完成，全部落库', j.missing ? 'error' : 'success');
          } else {
            App.showToast('抓取任务异常结束，请查看日志', 'error');
          }
          loadRecon();
        }
      }
      async function stopJob() {
        if (!_sa.job || !_sa.job.jobId) return;
        var r = await ApiService.stopFetch(_sa.job.jobId);
        App.showToast((r && r.ok) ? '已请求停止' : ((r && r.msg) || '操作失败'), (r && r.ok) ? 'info' : 'error');
      }

      // ---------------- 抖店「手动拖滑块」----------------
      // 服务器是 IDC IP + Xvfb 虚拟屏：过不了拼图滑块，也没有窗口能让人拖。
      // 所以后台按钮只做两件事：下发任务 + 轮询回显；浏览器弹在**本机**。
      function stopSliderPoll() {
        if (_sliderTimer) { clearInterval(_sliderTimer); _sliderTimer = null; }
        _sliderPollBusy = false;
      }
      function startSliderPoll() {
        stopSliderPoll();
        _sliderTimer = setInterval(pollSlider, 3000);
      }
      async function loadSliderStatus() {
        var r = await ApiService.getDoudianSliderStatus();
        if (!r) return null;
        _sa.slider = r;
        if (r.login) _sa.loginState = r.login;
        return r;
      }
      async function pollSlider() {
        if (_sliderPollBusy) return;          // 上一发没回来就跳过这一拍（同抓取轮询）
        _sliderPollBusy = true;
        var r = null;
        try { r = await ApiService.getDoudianSliderStatus(); }
        finally { _sliderPollBusy = false; }
        if (!r) return;
        _sa.slider = r;
        if (r.login) _sa.loginState = r.login;
        if (SLIDER_BUSY_ST.indexOf(r.status) < 0) {
          stopSliderPoll();
          if (r.status === 'done') App.showToast('抖店登录态已更新，可以点「更新数据」了', 'success');
        }
      }
      // 点「手动拖滑块」= 下发任务 + 打开进度界面
      async function openSlider() {
        _sa.sliderErr = '';
        _sa.sliderOpen = true;
        await requestSlider();
      }
      async function requestSlider() {
        if (_sa.sliderBusy) return;
        _sa.sliderBusy = true;
        _sa.sliderErr = '';
        var r = await ApiService.requestDoudianSlider();
        _sa.sliderBusy = false;
        if (!r || !r.ok) {
          _sa.sliderErr = (r && r.msg) || '下发滑块任务失败，请重试';
          return;
        }
        _sa.slider = r.data;
        if (r.data && r.data.login) _sa.loginState = r.data.login;
        startSliderPoll();
      }
      function closeSlider() { _sa.sliderOpen = false; }
      // 「更新数据」被登录态时效拦下 → 关掉该弹窗，直接进滑块界面
      function goSlider() {
        _sa.showUpdate = false;
        openSlider();
      }

      function switchTab(t) { _sa.tab = t; _sa.search = ''; }

      function openCreate(type) {
        _sa.modalType = type;
        _sa.editingId = null;
        _sa.form = _makeForm(type);
        _sa.modal = true;
      }
      function openEdit(type, row) {
        _sa.modalType = type;
        _sa.editingId = row.id;
        var f = _makeForm(type);
        FIELDS[type].forEach(function (x) { f[x.key] = row[x.key] != null ? row[x.key] : ''; });
        _sa.form = f;
        _sa.modal = true;
      }
      function closeModal() { _sa.modal = false; }

      async function saveModal() {
        var payload = {};
        FIELDS[_sa.modalType].forEach(function (x) { payload[x.key] = _sa.form[x.key]; });
        var r;
        if (_sa.editingId) {
          r = await ApiService.updateStoreAccount(_sa.modalType, _sa.editingId, payload);
        } else {
          r = await ApiService.createStoreAccount(_sa.modalType, payload);
        }
        if (r === null) { App.showToast('保存失败，请重试', 'error'); return; }
        _sa.modal = false;
        App.showToast('已保存', 'success');
        await loadAll();
      }

      async function removeRow(type, row) {
        if (!confirm('确定删除该记录吗？')) return;
        var r = await ApiService.deleteStoreAccount(type, row.id);
        if (r === null) { App.showToast('删除失败，请重试', 'error'); return; }
        App.showToast('已删除', 'success');
        await loadAll();
      }

      async function toggleActive(type, row) {
        var r = await ApiService.toggleStoreAccount(type, row.id, row.active ? 0 : 1);
        if (r === null) { App.showToast('切换失败，请重试', 'error'); return; }
        await loadAll();
      }

      function toggleSelect(shopKey) {
        _sa.selected[shopKey] = !_sa.selected[shopKey];
      }
      function selectAll() {
        updateShops.value.forEach(function (s) { _sa.selected[s.key] = true; });
      }
      function clearAll() { _sa.selected = {}; }
      function selectGroup(g) {
        var allOn = g.shops.every(function (s) { return _sa.selected[s.key]; });
        g.shops.forEach(function (s) { _sa.selected[s.key] = !allOn; });
      }

      function openUpdate() {
        _sa.upShopSearch = '';
        // 已有任务在跑：直接回到进度视图，不要覆盖
        if (_sa.job && _sa.job.status === 'running') {
          _sa.view = 'run';
          _sa.showUpdate = true;
          startPoll();
          return;
        }
        _sa.upStart = '';
        _sa.upEnd = '';
        _sa.selected = {};
        _sa.view = 'form';
        _sa.job = null;
        _sa.fetchErr = '';
        _sa.showUpdate = true;
      }

      // 把勾选的店铺按平台归组，交给后端唤起对应平台的抓取程序
      function buildTriggerPayload() {
        var platforms = [], itemKeys = [];
        Object.keys(_sa.selected).forEach(function (k) {
          if (!_sa.selected[k]) return;
          itemKeys.push(k);
          var p = k.split(':')[0];
          if (platforms.indexOf(p) < 0) platforms.push(p);
        });
        return {
          platforms: platforms,
          itemKeys: itemKeys,
          start: _sa.upStart,
          end: _sa.upEnd || '',
        };
      }

      async function submitUpdate() {
        if (selectedCount.value === 0) { App.showToast('请先选择要抓取的店铺', 'error'); return; }
        if (!_sa.upStart) { App.showToast('请选择开始日期', 'error'); return; }
        if (!_sa.fetchReady) { App.showToast('本机未找到抓取程序（/opt/pw），无法触发', 'error'); return; }
        _sa.submitting = true;
        _sa.fetchErr = '';
        _sa.fetchErrSlider = false;
        var r = await ApiService.triggerFetch(buildTriggerPayload());
        _sa.submitting = false;
        if (!r || !r.ok) {
          _sa.fetchErr = (r && r.msg) || '触发失败，请重试';
          // 抖店登录态过期：后端带 sliderNeeded 标记 → 在错误提示旁挂「去拖滑块」按钮
          _sa.fetchErrSlider = !!(r && r.data && r.data.sliderNeeded);
          App.showToast(_sa.fetchErr, 'error');
          return;
        }
        _sa.job = {
          jobId: r.data.jobId, status: 'running', total: r.data.total,
          done: 0, current: '正在唤起抓取程序…', log: [],
        };
        _sa.view = 'run';
        App.showToast('抓取任务已启动', 'success');
        startPoll();
      }

      function closeUpdate() { _sa.showUpdate = false; }
      function backToForm() { _sa.view = 'form'; _sa.job = null; }

      function progressPct() {
        var j = _sa.job;
        if (!j || !j.total) return 0;
        return Math.min(100, Math.round((j.done || 0) * 100 / j.total));
      }
      function jobLogText() {
        var j = _sa.job;
        if (!j || !j.log || !j.log.length) return '';
        return j.log.slice(-60).join('\n');
      }
      function jobPlatformHint() {
        var j = _sa.job;
        if (!j || !j.platforms) return '';
        if (j.platforms.indexOf('doudian') >= 0) {
          return '抖店需邮箱登录，若登录态已过期，请保持本机浏览器可用并留意滑块提示';
        }
        return '';
      }
      function reconStatusText(r) { return r.status === 'ok' ? '全部落库' : '有缺失'; }
      function reconStatusClass(r) { return r.status === 'ok' ? 'sa-recon-ok' : 'sa-recon-bad'; }

      // 点击整个日期框触发日历（showPicker 让 Chrome 直接弹日历，不再依赖点图标）
      function pickDate(kind) {
        var el = kind === 'start' ? upStartInput.value : upEndInput.value;
        if (!el) return;
        try { el.showPicker(); } catch (e) { el.focus(); el.click(); }
      }
      // 聚焦时解除 readonly，阻止浏览器自动填充（Chrome 会无视 autocomplete=off）
      function unlockInput(e) { if (e && e.target) e.target.removeAttribute('readonly'); }

      function tabClass(t) { return _sa.tab === t.key ? 'sa-tab active' : 'sa-tab'; }
      function tabStyle(t) {
        return _sa.tab === t.key ? { background: t.grad, color: '#fff', borderColor: 'transparent' } : {};
      }
      function tabIcoStyle(t) {
        return { background: _sa.tab === t.key ? 'rgba(255,255,255,0.28)' : t.grad };
      }
      function activeText(r) { return r.active ? '运营中' : '已停用'; }

      return {
        sa: _sa, TABS: TABS, filtered: filtered, counts: counts,
        modalFields: modalFields, updateShops: updateShops, selectedCount: selectedCount,
        groupedShops: groupedShops, upFilteredGroups: upFilteredGroups,
        loadAll: loadAll, switchTab: switchTab, openCreate: openCreate, openEdit: openEdit,
        closeModal: closeModal, saveModal: saveModal, removeRow: removeRow,
        toggleActive: toggleActive, toggleSelect: toggleSelect,
        selectAll: selectAll, clearAll: clearAll, selectGroup: selectGroup,
        openUpdate: openUpdate, submitUpdate: submitUpdate, closeUpdate: closeUpdate,
        pickDate: pickDate, unlockInput: unlockInput,
        // 抖店「手动拖滑块」：这几个 computed 是顶层绑定，模板里裸写
        openSlider: openSlider, requestSlider: requestSlider, closeSlider: closeSlider,
        goSlider: goSlider, loadSliderStatus: loadSliderStatus,
        sliderStatus: sliderStatus, sliderInfo: sliderInfo,
        canRetrySlider: canRetrySlider, loginBadge: loginBadge,
        upStartInput: upStartInput, upEndInput: upEndInput,
        tabClass: tabClass, tabStyle: tabStyle, tabIcoStyle: tabIcoStyle, activeText: activeText,
        // 抓取任务 / 对账
        loadRecon: loadRecon, stopJob: stopJob, backToForm: backToForm,
        progressPct: progressPct, jobLogText: jobLogText, jobPlatformHint: jobPlatformHint,
        reconStatusText: reconStatusText, reconStatusClass: reconStatusClass,
      };
    },

    template: `
<div>
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon" style="background:linear-gradient(135deg,#6366f1,#8b5cf6);box-shadow:0 2px 8px rgba(99,102,241,0.25)"><i class="fa-solid fa-id-card" style="color:#fff;font-size:18px"></i></div>
      <div class="dh-title-group"><h2 class="dh-title">店铺账号管理</h2><span class="dh-subtitle">Store Account Management</span><span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>共 {{ counts.total }} 个店铺</span></div>
    </div>
    <div class="dh-chips">
      <div class="dh-chip"><span class="dh-chip-label">运营中</span><span class="dh-chip-value" style="color:#16a34a">{{ counts.active }}</span></div>
      <div class="dh-chip"><span class="dh-chip-label">已停用</span><span class="dh-chip-value" style="color:#9ca3af">{{ counts.inactive }}</span></div>
    </div>
  </div>

  <div class="ap-toolbar">
    <div class="sa-toolbar-left">
      <div class="ap-search-wrap"><i class="fa-solid fa-search"></i><input class="ap-search-input" v-model="sa.search" placeholder="搜索当前列表..." autocomplete="off" readonly @focus="unlockInput($event)" spellcheck="false"></div>
      <div class="sa-tabs">
        <button v-for="t in TABS" :key="t.key" type="button" :class="tabClass(t.key)" :style="tabStyle(t)" @click="switchTab(t.key)">
          <span class="sa-tab-ico" :style="tabIcoStyle(t)"><i class="fa-solid" :class="t.icon"></i></span>{{ t.label }}
        </button>
      </div>
    </div>
    <div class="sa-toolbar-right">
      <button class="ap-btn-primary sa-btn-update" @click="openUpdate"><i class="fa-solid fa-cloud-arrow-down"></i> 更新数据</button>
      <button class="ap-btn-primary" @click="openCreate(sa.tab)"><i class="fa-solid fa-plus"></i> 新增{{ sa.tab === 'qianniu' ? '千牛账号' : (sa.tab === 'doudian' ? '抖店店铺' : '京东账号') }}</button>
    </div>
  </div>

  <div v-if="sa.tab === 'doudian'" class="sa-email-panel">
    <div class="sa-email-head">
      <div><i class="fa-solid fa-envelope" style="color:#6366f1"></i> <strong>抖店登录邮箱</strong><span style="color:#94a3b8;font-size:12px;margin-left:8px">邮箱登录后逐个切换店铺抓取</span><span class="sa-login-badge" :class="loginBadge.cls" :title="loginBadge.tip">{{ loginBadge.text }}</span></div>
      <div class="sa-email-actions">
        <button class="ap-btn-sm slider" @click="openSlider"><i class="fa-solid fa-hand-pointer"></i> 手动拖滑块</button>
        <button class="ap-btn-sm edit" @click="openCreate('doudian-email')"><i class="fa-solid fa-plus"></i> 新增邮箱</button>
      </div>
    </div>
    <table class="ap-table">
      <thead><tr><th style="width:220px">邮箱</th><th style="width:160px">密码</th><th style="width:90px">状态</th><th style="width:80px">操作</th></tr></thead>
      <tbody>
        <tr v-for="e in sa.emails" :key="e.id">
          <td style="font-family:monospace">{{ e.email }}</td>
          <td style="font-family:monospace;font-size:12px">{{ e.password }}</td>
          <td><button type="button" @click="toggleActive('doudian-email', e)" :style="e.active ? 'background:#e6f7ee;color:#0f9d58;border:1px solid #bfe8d0' : 'background:#f2f3f5;color:#9aa0a6;border:1px solid #e0e0e0'" style="border-radius:20px;padding:3px 12px;font-size:12px;cursor:pointer">{{ activeText(e) }}</button></td>
          <td><div class="ap-actions">
            <button class="ap-btn-sm edit" @click="openEdit('doudian-email', e)"><i class="fa-solid fa-pen"></i></button>
            <button class="ap-btn-sm delete" @click="removeRow('doudian-email', e)"><i class="fa-solid fa-trash"></i></button>
          </div></td>
        </tr>
        <tr v-if="!sa.emails.length"><td colspan="4" style="text-align:center;padding:24px;color:#94a3b8">暂无登录邮箱，请点击右上角新增</td></tr>
      </tbody>
    </table>
  </div>

  <div class="ap-table-wrap">
    <table class="ap-table">
      <thead>
        <tr v-if="sa.tab === 'qianniu'">
          <th style="width:60px">ID</th><th style="width:220px">账号</th><th style="width:140px">密码</th><th style="width:110px">店铺ID</th><th style="width:90px">品牌</th><th style="width:100px">运营状态</th><th style="width:90px">操作</th>
        </tr>
        <tr v-else-if="sa.tab === 'jd'">
          <th style="width:60px">ID</th><th style="width:140px">账号</th><th style="width:120px">密码</th><th style="width:110px">店铺ID</th><th style="width:160px">店铺名</th><th style="width:100px">运营状态</th><th style="width:90px">操作</th>
        </tr>
        <tr v-else>
          <th style="width:60px">ID</th><th style="width:240px">店铺名</th><th style="width:110px">店铺ID</th><th style="width:90px">品牌</th><th style="width:100px">运营状态</th><th style="width:90px">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="sa.tab === 'qianniu'" v-for="r in filtered" :key="r.id">
          <td>{{ r.id }}</td>
          <td><strong>{{ r.account }}</strong></td>
          <td style="font-family:monospace;font-size:12px">{{ r.password }}</td>
          <td style="font-family:monospace;font-size:12px">{{ r.shopId || '—' }}</td>
          <td><span class="ps-store-platform">{{ r.brand }}</span></td>
          <td><button type="button" @click="toggleActive('qianniu', r)" :style="r.active ? 'background:#e6f7ee;color:#0f9d58;border:1px solid #bfe8d0' : 'background:#f2f3f5;color:#9aa0a6;border:1px solid #e0e0e0'" style="border-radius:20px;padding:3px 12px;font-size:12px;cursor:pointer">{{ activeText(r) }}</button></td>
          <td><div class="ap-actions">
            <button class="ap-btn-sm edit" @click="openEdit('qianniu', r)"><i class="fa-solid fa-pen"></i></button>
            <button class="ap-btn-sm delete" @click="removeRow('qianniu', r)"><i class="fa-solid fa-trash"></i></button>
          </div></td>
        </tr>
        <tr v-else-if="sa.tab === 'jd'" v-for="r in filtered" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.account }}</td>
          <td style="font-family:monospace;font-size:12px">{{ r.password }}</td>
          <td style="font-family:monospace;font-size:12px">{{ r.shopId }}</td>
          <td><strong>{{ r.shopName }}</strong></td>
          <td><button type="button" @click="toggleActive('jd', r)" :style="r.active ? 'background:#e6f7ee;color:#0f9d58;border:1px solid #bfe8d0' : 'background:#f2f3f5;color:#9aa0a6;border:1px solid #e0e0e0'" style="border-radius:20px;padding:3px 12px;font-size:12px;cursor:pointer">{{ activeText(r) }}</button></td>
          <td><div class="ap-actions">
            <button class="ap-btn-sm edit" @click="openEdit('jd', r)"><i class="fa-solid fa-pen"></i></button>
            <button class="ap-btn-sm delete" @click="removeRow('jd', r)"><i class="fa-solid fa-trash"></i></button>
          </div></td>
        </tr>
        <tr v-else v-for="r in filtered" :key="r.id">
          <td>{{ r.id }}</td>
          <td><strong>{{ r.shopName }}</strong></td>
          <td style="font-family:monospace;font-size:12px">{{ r.shopId }}</td>
          <td><span class="ps-store-platform">{{ r.brand }}</span></td>
          <td><button type="button" @click="toggleActive('doudian', r)" :style="r.active ? 'background:#e6f7ee;color:#0f9d58;border:1px solid #bfe8d0' : 'background:#f2f3f5;color:#9aa0a6;border:1px solid #e0e0e0'" style="border-radius:20px;padding:3px 12px;font-size:12px;cursor:pointer">{{ activeText(r) }}</button></td>
          <td><div class="ap-actions">
            <button class="ap-btn-sm edit" @click="openEdit('doudian', r)"><i class="fa-solid fa-pen"></i></button>
            <button class="ap-btn-sm delete" @click="removeRow('doudian', r)"><i class="fa-solid fa-trash"></i></button>
          </div></td>
        </tr>
        <tr v-if="!filtered.length"><td colspan="7" style="text-align:center;padding:32px;color:#94a3b8">未找到匹配的记录</td></tr>
      </tbody>
    </table>
  </div>
  <div class="ap-table-info">共 {{ filtered.length }} 条记录</div>

  <div class="sa-recon-panel">
    <div class="sa-recon-head">
      <div>
        <i class="fa-solid fa-clipboard-check" style="color:#6366f1"></i> <strong>抓取对账记录</strong>
        <span style="color:#94a3b8;font-size:12px;margin-left:8px">抓取跑完后与「运营中」列表逐店对照，缺数据的自动补抓；仍有缺失会推钉钉</span>
      </div>
      <button class="ap-btn-sm edit" @click="loadRecon"><i class="fa-solid fa-rotate"></i></button>
    </div>
    <table class="ap-table">
      <thead>
        <tr>
          <th style="width:110px">日期</th><th style="width:80px">平台</th>
          <th style="width:110px">已落库/运营中</th><th>缺失店铺</th>
          <th style="width:90px">状态</th><th style="width:150px">生成时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in sa.recon" :key="r.date + '#' + r.platform">
          <td>{{ r.date }}</td>
          <td>{{ r.platform }}</td>
          <td>{{ r.okCount }} / {{ r.activeCount }}</td>
          <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" :title="r.missingShops">{{ r.missingShops || '—' }}</td>
          <td><span :class="reconStatusClass(r)">{{ reconStatusText(r) }}</span></td>
          <td style="font-size:12px;color:#94a3b8">{{ r.createdAt }}</td>
        </tr>
        <tr v-if="!sa.recon.length"><td colspan="6" style="text-align:center;padding:24px;color:#94a3b8">暂无对账记录（每日 9:00 抓取跑完后生成）</td></tr>
      </tbody>
    </table>
  </div>

  <div v-if="sa.sliderOpen" class="sa-modal-mask" @click.self="closeSlider">
    <div class="sa-modal" style="max-width:580px">
      <div class="sa-up-head">
        <div class="sa-up-title">
          <span class="sa-up-ico"><i class="fa-solid fa-hand-pointer"></i></span>
          <div><strong>手动拖滑块</strong><span class="sa-up-sub">浏览器弹在你自己的电脑上 —— 服务器上没有窗口能拖</span></div>
        </div>
        <button type="button" class="sa-modal-close" @click="closeSlider"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="sa-modal-body">
        <div class="sa-slider-state" :class="'st-' + sliderStatus">
          <span class="sa-slider-ico"><i class="fa-solid" :class="sliderInfo.icon"></i></span>
          <div class="sa-slider-txt">
            <div class="sa-slider-title">{{ sliderInfo.title }}</div>
            <div class="sa-slider-desc">{{ sliderInfo.desc }}</div>
            <div class="sa-slider-meta">
              <span v-if="sa.slider && sa.slider.requestedAt">下发：{{ sa.slider.requestedAt }}</span>
              <span v-if="sa.slider && sa.slider.finishedAt">完成：{{ sa.slider.finishedAt }}</span>
              <span v-if="sa.slider && sa.slider.message">{{ sa.slider.message }}</span>
            </div>
          </div>
        </div>

        <div v-if="sa.slider && !sa.slider.agentOnline" class="sa-slider-warn">
          <i class="fa-solid fa-triangle-exclamation"></i>
          本机滑块助手没在运行 —— 请先在本机双击「<strong>启动滑块助手.bat</strong>」（保持窗口开着），助手启动后会自动领走这个任务。
        </div>

        <div class="sa-slider-tip">
          <i class="fa-solid fa-circle-info"></i>
          流程：下发任务 → 本机自动弹出 Chrome → 你完成邮箱登录并把拼图滑块拖过去 → 登录态自动上传服务器。有效期约 40 分钟，传完请尽快点「更新数据」。
        </div>

        <div class="sa-slider-login" v-if="sa.loginState">
          <i class="fa-solid fa-clock-rotate-left"></i> 当前登录态：<strong :class="loginBadge.cls">{{ loginBadge.text }}</strong>
          <span v-if="sa.loginState && sa.loginState.email" style="color:#94a3b8;margin-left:6px">（{{ sa.loginState.email }}）</span>
        </div>

        <div v-if="sa.sliderErr" class="sa-up-err"><i class="fa-solid fa-circle-exclamation"></i> {{ sa.sliderErr }}</div>
      </div>
      <div class="sa-modal-foot">
        <button v-if="canRetrySlider" class="ap-btn-primary sa-btn-update" :disabled="sa.sliderBusy" @click="requestSlider">
          <i class="fa-solid" :class="sa.sliderBusy ? 'fa-spinner fa-spin' : 'fa-rotate-right'"></i>
          {{ sa.sliderBusy ? '下发中…' : (sliderStatus === 'idle' ? '下发任务' : '重试') }}
        </button>
        <button class="ap-btn-plain" @click="closeSlider">关闭</button>
      </div>
    </div>
  </div>

  <div v-if="sa.modal" class="sa-modal-mask" @click.self="closeModal">
    <div class="sa-modal">
      <div class="sa-modal-head"><strong>{{ sa.editingId ? '编辑' : '新增' }}</strong><button type="button" class="sa-modal-close" @click="closeModal"><i class="fa-solid fa-xmark"></i></button></div>
      <div class="sa-modal-body">
        <div v-for="f in modalFields" :key="f.key" class="sa-field">
          <label>{{ f.label }}{{ f.required ? ' *' : '' }}</label>
          <input v-model="sa.form[f.key]" type="text" :placeholder="f.placeholder || ''" autocomplete="off" :name="'saForm_' + f.key">
        </div>
      </div>
      <div class="sa-modal-foot">
        <button class="ap-btn-primary" @click="saveModal"><i class="fa-solid fa-check"></i> 保存</button>
        <button class="ap-btn-plain" @click="closeModal">取消</button>
      </div>
    </div>
  </div>

  <div v-if="sa.showUpdate" class="sa-modal-mask" @click.self="closeUpdate">
    <div class="sa-modal" style="max-width:640px">
      <div class="sa-up-head">
        <div class="sa-up-title">
          <span class="sa-up-ico"><i class="fa-solid fa-cloud-arrow-down"></i></span>
          <div><strong>更新数据</strong><span class="sa-up-sub">选择店铺与日期范围，触发对应平台数据抓取</span></div>
        </div>
        <button type="button" class="sa-modal-close" @click="closeUpdate"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="sa-modal-body" v-if="sa.view === 'form'">
        <div class="sa-up-hint"><i class="fa-solid fa-circle-info"></i> 支持多选店铺、单日或区间抓取；已停用店铺默认不参与。</div>
        <div class="sa-date-grid">
          <div class="sa-date-field" @click="pickDate('start')">
            <span class="sa-date-field-label">开始日期</span>
            <span class="sa-date-field-value" :class="{ empty: !sa.upStart }">{{ sa.upStart || '选择日期' }}</span>
            <i class="fa-solid fa-calendar-days"></i>
            <input type="date" ref="upStartInput" v-model="sa.upStart" class="sa-date-native">
          </div>
          <div class="sa-date-field" @click="pickDate('end')">
            <span class="sa-date-field-label">结束日期</span>
            <span class="sa-date-field-value" :class="{ empty: !sa.upEnd }">{{ sa.upEnd || '单日则留空' }}</span>
            <i class="fa-solid fa-calendar-days"></i>
            <input type="date" ref="upEndInput" v-model="sa.upEnd" class="sa-date-native">
          </div>
        </div>
        <div class="sa-shop-filter">
          <div class="sa-shop-search"><i class="fa-solid fa-magnifying-glass"></i><input v-model="sa.upShopSearch" placeholder="搜索店铺名..." autocomplete="off" readonly @focus="unlockInput($event)" spellcheck="false"></div>
          <div style="display:flex;gap:4px">
            <button class="sa-link-btn" @click="selectAll">全选</button>
            <button class="sa-link-btn" @click="clearAll">清空</button>
          </div>
        </div>
        <div style="font-size:12px;color:#94a3b8;margin-bottom:8px">已选 <strong style="color:#6366f1">{{ selectedCount }}</strong> 家店铺</div>
        <div class="sa-shop-groups">
          <div v-for="g in upFilteredGroups" :key="g.key" class="sa-shop-group">
            <div class="sa-shop-group-head" @click="selectGroup(g)">
              <span class="sa-dot" :style="{ background: g.grad }"></span>
              <strong style="font-size:13px;color:#334155">{{ g.label }}</strong>
              <span class="sa-group-count">{{ g.shops.length }}</span>
            </div>
            <div class="sa-shop-grid">
              <label v-for="s in g.shops" :key="s.key" class="sa-shop-item" :class="sa.selected[s.key] ? 'on' : ''">
                <input type="checkbox" :checked="!!sa.selected[s.key]" @change="toggleSelect(s.key)">
                <span class="sa-shop-name">{{ s.label }}</span>
                <span v-if="!s.active" class="sa-shop-off">停用</span>
              </label>
            </div>
          </div>
        </div>
        <div v-if="sa.fetchErr" class="sa-up-err">
          <div><i class="fa-solid fa-circle-exclamation"></i> {{ sa.fetchErr }}</div>
          <button v-if="sa.fetchErrSlider" class="ap-btn-primary sa-btn-update" style="margin-top:10px" @click="goSlider">
            <i class="fa-solid fa-hand-pointer"></i> 去手动拖滑块
          </button>
        </div>
      </div>

      <div class="sa-modal-body" v-else-if="sa.job">
        <div class="sa-up-hint" v-if="jobPlatformHint()"><i class="fa-solid fa-triangle-exclamation"></i> {{ jobPlatformHint() }}</div>
        <div class="sa-prog-head">
          <span class="sa-prog-label">
            <i class="fa-solid" :class="sa.job.status === 'running' ? 'fa-spinner fa-spin' : (sa.job.status === 'done' ? 'fa-circle-check' : 'fa-circle-exclamation')"></i>
            {{ sa.job.status === 'running' ? '抓取中' : (sa.job.status === 'done' ? '抓取完成' : '任务异常结束') }}
          </span>
          <span class="sa-prog-step">{{ sa.job.done || 0 }} / {{ sa.job.total }} 步</span>
        </div>
        <div class="sa-prog-bar"><span :style="{ width: progressPct() + '%' }"></span></div>
        <div class="sa-prog-cur" v-if="sa.job.current">当前：{{ sa.job.current }}</div>
        <div class="sa-prog-stat" v-if="sa.job.status !== 'running'">
          <span>成功 {{ sa.job.okCount }}</span>
          <span>失败 {{ sa.job.failCount }}</span>
          <span :class="sa.job.missing ? 'sa-recon-bad' : 'sa-recon-ok'">
            {{ sa.job.missing ? ('对账未落库 ' + sa.job.missing + ' 项') : '对账全部落库' }}
          </span>
        </div>
        <pre class="sa-prog-log">{{ jobLogText() || '等待输出…' }}</pre>
      </div>

      <div class="sa-modal-foot" v-if="sa.view === 'form'">
        <button class="ap-btn-primary sa-btn-update" :disabled="sa.submitting" @click="submitUpdate">
          <i class="fa-solid" :class="sa.submitting ? 'fa-spinner fa-spin' : 'fa-play'"></i>
          {{ sa.submitting ? '正在启动…' : '开始抓取' }}
        </button>
        <button class="ap-btn-plain" @click="closeUpdate">取消</button>
      </div>
      <div class="sa-modal-foot" v-else>
        <button v-if="sa.job && sa.job.status === 'running'" class="ap-btn-plain" @click="stopJob"><i class="fa-solid fa-stop"></i> 停止</button>
        <button v-if="sa.job && sa.job.status !== 'running'" class="ap-btn-plain" @click="backToForm">再抓一批</button>
        <button class="ap-btn-primary" @click="closeUpdate"><i class="fa-solid fa-check"></i> {{ sa.job && sa.job.status === 'running' ? '后台运行' : '完成' }}</button>
      </div>
    </div>
  </div>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子（接管模式） ====================
  var _saApp = null;   // Vue app 对象（unmount 用）
  var _saVm = null;    // 根组件实例（调 setup 暴露的方法用）

  function mountStoreAccountVue() {
    if (_saVm) return;
    var mount = document.getElementById('page-store-account');
    if (!mount) return;
    _saApp = Vue.createApp(StoreAccountPage);
    _saVm = _saApp.mount(mount);
  }

  function unmountStoreAccountVue() {
    if (!_saVm) return;
    _saApp.unmount();
    _saApp = null;
    _saVm = null;
    var mount = document.getElementById('page-store-account');
    if (mount) mount.innerHTML = '';
  }

  function initHook() {
    var container = document.getElementById('page-store-account');
    if (!container) return;
    var isVisible = !container.classList.contains('hidden');
    if (isVisible) { mountStoreAccountVue(); return; }
    var observer = new MutationObserver(function () {
      var nowVisible = !container.classList.contains('hidden');
      if (nowVisible && !_saVm) mountStoreAccountVue();
      else if (!nowVisible && _saVm) unmountStoreAccountVue();
    });
    observer.observe(container, { attributes: true, attributeFilter: ['class'] });
  }

  initHook();
})();
