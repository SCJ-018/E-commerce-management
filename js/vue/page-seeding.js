/**
 * 种草监测中台 — Vue 版（阶段 4，第一步：左侧 账号/作品/被删 + Cookie + 抓取 + 双月日历）
 * 右侧种草智能体（文案生成）留待第二步迁移
 * classic script，与 app.js 共用全局词法作用域；接口全部复用 ApiService（零补）
 */
(function () {
  // ==================== 模块级状态（跨挂载/卸载保留，切走再回来数据不丢） ====================
  var _st = Vue.reactive({
    tab: 'works',                 // works | accounts
    platform: 'douyin',           // douyin | xhs | deleted
    deptFilter: '',               // '' | 三部 | 四部 | 五部
    accounts: [],                 // 种草账号列表
    works: [],                    // 作品列表
    deleted: [],                  // 被删作品列表
    accountSearch: '',            // 账号搜索词
    accountPlatformFilter: '',    // 账号平台筛选
    worksSearch: '',              // 作品搜索词
    sortKey: 'publishTime',       // 作品排序字段
    sortDir: -1,                  // 1 升序 / -1 降序
    dateStart: '',                // 日期范围筛选（起）
    dateEnd: '',                  // 日期范围筛选（止）
    dateLabel: '选择日期',
    updatePanelOpen: false,       // 「数据更新」悬浮面板
    cookies: { douyin: '', xhs: '' },
    scraping: false,              // 抓取进行中
    progressPercent: 0,
    progressText: '抓取中...',
    updateTime: '--',             // 数据更新时间
    accountModal: { open: false, isEdit: false, id: null, platform: 'douyin', name: '', douyinId: '', redId: '', department: '', homepage: '' },
    confirm: { open: false, msg: '', action: null },
    cal: { open: false, base: null, start: null, end: null, pickStart: true },   // 双月日历
    agent: { busy: false, input: '', result: '', meta: '', error: '' },          // 种草智能体
    infoModal: { open: false, name: '', category: '', selling: '', audience: '', platform: '', price: '', scene: '', style: '', note: '', types: [] },  // 信息填写弹窗
  });

  // ==================== 格式化辅助 ====================
  function _fmtDate(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }
  function _parseDate(str) {
    if (!str) return null;
    var p = str.split('-');
    if (p.length !== 3) return null;
    return new Date(parseInt(p[0], 10), parseInt(p[1], 10) - 1, parseInt(p[2], 10));
  }
  function _fmtTime(mtime) {
    if (!mtime) return '--';
    var d = new Date(mtime * 1000);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') +
      ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  // ==================== 组件 ====================
  var SeedingPage = {
    components: { 'ecom-modal': EcomUI.Modal },
    setup: function () {
      // ---------- 加载 ----------
      async function loadAccounts() {
        var data = await ApiService.getSeedingAccounts();
        if (Array.isArray(data)) _st.accounts = data;
      }
      async function loadWorks(platform) {
        var p = platform || _st.platform;
        var data = await ApiService.getSeedingWorks(p);
        if (Array.isArray(data) && p === _st.platform) _st.works = data;
      }
      async function loadMeta(platform) {
        var p = platform || _st.platform;
        var meta = await ApiService.getSeedingWorksMeta(p);
        if (p === _st.platform) _st.updateTime = (meta && meta.mtime) ? _fmtTime(meta.mtime) : '--';
      }
      async function loadCookie() {
        var d = await ApiService.getSeedingCookie('douyin');
        if (d && d.cookie) _st.cookies.douyin = d.cookie;
        var x = await ApiService.getSeedingCookie('xhs');
        if (x && x.cookie) _st.cookies.xhs = x.cookie;
      }
      async function loadDeleted() {
        var data = await ApiService.getSeedingDeleted();
        if (Array.isArray(data)) _st.deleted = data;
      }

      // ---------- Tab / 平台 / 部门 ----------
      function switchTab(tab) { _st.tab = tab; }
      function switchPlatform(p) {
        _st.platform = p || 'douyin';
        if (_st.platform === 'deleted') { loadDeleted(); }
        else { loadWorks(); loadMeta(); }
      }
      function filterDept(dept) { _st.deptFilter = dept || ''; }

      // 搜索框防浏览器自动填充：初始 readonly，浏览器不会填充只读框；首次聚焦时解除
      var worksSearchLocked = Vue.ref(true);
      var accountSearchLocked = Vue.ref(true);
      function unlockWorksSearch() { worksSearchLocked.value = false; }
      function unlockAccountSearch() { accountSearchLocked.value = false; }

      // ---------- 作品排序 ----------
      function sortWorks(key) {
        if (_st.sortKey === key) _st.sortDir = -_st.sortDir;
        else { _st.sortKey = key; _st.sortDir = -1; }
      }
      function sortArrow(key) {
        return _st.sortKey === key ? (_st.sortDir === -1 ? '▼' : '▲') : '';
      }

      // ---------- 部门映射（账号 → 部门） ----------
      var deptMap = Vue.computed(function () {
        var m = {};
        _st.accounts.forEach(function (a) {
          if (a.douyinId) m[a.douyinId] = a.department || '';
          if (a.redId) m[a.redId] = a.department || '';
          if (a.name) m[a.name] = a.department || '';
        });
        return m;
      });

      // ---------- 作品列表（搜索 + 部门 + 时间 + 排序） ----------
      var filteredWorks = Vue.computed(function () {
        var list = _st.works.slice();
        var kw = (_st.worksSearch || '').toLowerCase();
        if (kw) list = list.filter(function (w) {
          return (w.title || '').toLowerCase().indexOf(kw) >= 0 ||
                 (w.name || '').toLowerCase().indexOf(kw) >= 0 ||
                 (w.account || '').toLowerCase().indexOf(kw) >= 0;
        });
        if (_st.deptFilter) {
          var dm = deptMap.value;
          list = list.filter(function (w) {
            return (dm[w.account] || dm[w.name] || '') === _st.deptFilter;
          });
        }
        if (_st.dateStart || _st.dateEnd) {
          list = list.filter(function (w) {
            var d = (w.publishTime || '').slice(0, 10);
            if (_st.dateStart && d < _st.dateStart) return false;
            if (_st.dateEnd && d > _st.dateEnd) return false;
            return true;
          });
        }
        if (_st.sortKey) {
          list.sort(function (a, b) {
            var av = a[_st.sortKey], bv = b[_st.sortKey];
            var r;
            if (typeof av === 'number' && typeof bv === 'number') r = av - bv;
            else r = String(av == null ? '' : av).localeCompare(String(bv == null ? '' : bv));
            return r * _st.sortDir;
          });
        }
        return list;
      });

      // ---------- 被删作品（部门筛选） ----------
      var filteredDeleted = Vue.computed(function () {
        var list = _st.deleted.slice();
        if (_st.deptFilter) {
          var dm = deptMap.value;
          list = list.filter(function (d) {
            return (dm[d.account] || dm[d.name] || '') === _st.deptFilter;
          });
        }
        return list;
      });

      // ---------- 账号列表（搜索 + 平台筛选） ----------
      var filteredAccounts = Vue.computed(function () {
        var kw = (_st.accountSearch || '').toLowerCase();
        var list = _st.accounts.slice();
        if (kw) list = list.filter(function (a) {
          return (a.name || '').toLowerCase().indexOf(kw) >= 0 ||
                 (a.douyinId || '').toLowerCase().indexOf(kw) >= 0 ||
                 (a.redId || '').toLowerCase().indexOf(kw) >= 0 ||
                 (a.homepage || '').toLowerCase().indexOf(kw) >= 0 ||
                 (a.department || '').toLowerCase().indexOf(kw) >= 0;
        });
        if (_st.accountPlatformFilter) list = list.filter(function (a) {
          return (a.platform || 'douyin') === _st.accountPlatformFilter;
        });
        return list;
      });

      // ---------- 账号增删改 ----------
      function openAccountModal(id) {
        _st.accountModal.isEdit = !!id;
        _st.accountModal.id = id || null;
        var acc = id ? _st.accounts.find(function (a) { return a.id === id; }) : null;
        var plat = acc ? (acc.platform || 'douyin') : 'douyin';
        _st.accountModal.platform = plat;
        _st.accountModal.name = acc ? (acc.name || '') : '';
        _st.accountModal.douyinId = acc ? (acc.douyinId || '') : '';
        _st.accountModal.redId = acc ? (acc.redId || '') : '';
        _st.accountModal.department = acc ? (acc.department || '') : '';
        _st.accountModal.homepage = acc ? (acc.homepage || '') : '';
        _st.accountModal.open = true;
      }
      function closeAccountModal() { _st.accountModal.open = false; }
      async function saveAccount() {
        var f = _st.accountModal;
        var name = (f.name || '').trim();
        if (!name) { App.showToast('请输入账号名称', 'error'); return; }
        var platform = f.platform || 'douyin';
        var douyinId = (f.douyinId || '').trim();
        var redId = (f.redId || '').trim();
        var dept = (f.department || '').trim();
        var homepage = (f.homepage || '').trim();
        if (platform === 'xhs') {
          if (!redId) { App.showToast('请输入小红书号', 'error'); return; }
          homepage = '';
        } else {
          if (!homepage) { App.showToast('请输入主页链接', 'error'); return; }
          redId = '';
        }
        var payload = { platform: platform, name: name, douyinId: douyinId, redId: redId, homepage: homepage, department: dept };
        var saved = f.isEdit ? await ApiService.updateSeedingAccount(f.id, payload) : await ApiService.createSeedingAccount(payload);
        if (!saved) { App.showToast('保存失败，请重试', 'error'); return; }
        if (f.isEdit) {
          var idx = _st.accounts.findIndex(function (a) { return a.id === f.id; });
          if (idx >= 0) _st.accounts[idx] = saved; else _st.accounts.push(saved);
        } else {
          _st.accounts.push(saved);
        }
        _st.accountModal.open = false;
        App.showToast(f.isEdit ? '种草账号已更新' : '种草账号已添加');
      }
      function deleteAccount(id) {
        var acc = _st.accounts.find(function (a) { return a.id === id; });
        if (!acc) return;
        _st.confirm.msg = '确定删除种草账号「' + (acc.name || '') + '」吗？';
        _st.confirm.action = function () {
          ApiService.deleteSeedingAccount(id).then(function () {
            _st.accounts = _st.accounts.filter(function (a) { return a.id !== id; });
            App.showToast('种草账号已删除');
          });
        };
        _st.confirm.open = true;
      }

      // ---------- 被删作品 ----------
      function deleteDeleted(id) {
        _st.confirm.msg = '确定清除这条被删作品记录吗？';
        _st.confirm.action = function () {
          ApiService.deleteSeedingDeleted(id).then(function () {
            _st.deleted = _st.deleted.filter(function (d) { return d.id !== id; });
            App.showToast('记录已清除');
          });
        };
        _st.confirm.open = true;
      }
      function clearDeleted() {
        _st.confirm.msg = '确定清空全部被删作品记录吗？此操作不可恢复。';
        _st.confirm.action = function () {
          ApiService.clearSeedingDeleted().then(function () {
            _st.deleted = [];
            App.showToast('已清空全部被删作品记录');
          });
        };
        _st.confirm.open = true;
      }
      function closeConfirm() { _st.confirm.open = false; }
      function confirmAction() {
        var fn = _st.confirm.action;
        _st.confirm.open = false;
        if (fn) fn();
      }

      // ---------- Cookie / 抓取 ----------
      function toggleUpdatePanel() { _st.updatePanelOpen = !_st.updatePanelOpen; }
      async function saveCookie(platform) {
        var cookie = (_st.cookies[platform] || '').trim();
        if (!cookie) { App.showToast('请输入 Cookie', 'error'); return; }
        var res = await ApiService.saveSeedingCookie(platform, cookie);
        App.showToast(res !== null ? 'Cookie 已保存' : 'Cookie 保存失败', res !== null ? 'success' : 'error');
      }
      async function triggerScrape(platform) {
        var res = await ApiService.triggerSeedingScrape(platform);
        if (res === null) { App.showToast('触发抓取失败', 'error'); return; }
        var beforeMtime = res.mtime || 0;
        App.showToast('已触发' + (platform === 'xhs' ? '小红书' : '抖音') + '抓取，正在后台执行…');
        _pollScrape(platform, beforeMtime);
      }
      // 抓取轮询：模块级、不依赖组件生命周期（unmount 不中断，抓完自动更新模块级数据）
      function _pollScrape(platform, beforeMtime) {
        _st.scraping = true;
        _st.progressPercent = 2;
        _st.progressText = '抓取中...';
        var tries = 0, maxTries = 160; // 160 * 3s = 8min
        (function poll() {
          setTimeout(async function () {
            tries++;
            var st = await ApiService.getSeedingScrapeStatus(platform);
            if (st && st.status === 'running') {
              var done = st.done || 0, total = st.total || 0;
              _st.progressPercent = st.progress || 0;
              _st.progressText = total ? ('抓取中 ' + done + '/' + total + ' 个账号') : '抓取中...';
            }
            var meta = await ApiService.getSeedingWorksMeta(platform);
            if (meta && meta.mtime && (!beforeMtime || meta.mtime > beforeMtime)) {
              await loadWorks(platform);
              await loadDeleted();
              await loadMeta(platform);
              _st.progressPercent = 100;
              _st.progressText = '抓取完成';
              App.showToast('抓取完成，作品数据已更新');
              setTimeout(function () { _st.scraping = false; }, 3000);
              return;
            }
            if (tries >= maxTries) {
              _st.scraping = false;
              await loadWorks(platform);
              await loadDeleted();
              App.showToast('抓取可能仍在进行或未完成，请稍后手动点击「数据更新」查看', 'error');
              return;
            }
            poll();
          }, 3000);
        })();
      }

      // ---------- 双月日历 ----------
      function updateDateLabel() {
        var s = _st.dateStart, e = _st.dateEnd;
        if (s && e) _st.dateLabel = s + ' — ' + e;
        else if (s) _st.dateLabel = s + ' — ';
        else if (e) _st.dateLabel = ' — ' + e;
        else _st.dateLabel = '选择日期';
      }
      function calToggle() {
        _st.cal.open = !_st.cal.open;
        if (_st.cal.open) {
          var now = new Date();
          _st.cal.base = new Date(now.getFullYear(), now.getMonth() - 1, 1); // 左月=上个月，右月=本月
          _st.cal.start = _parseDate(_st.dateStart);
          _st.cal.end = _parseDate(_st.dateEnd);
          _st.cal.pickStart = !(_st.cal.start && _st.cal.end);
        }
      }
      function _buildMonth(y, m, isLeft) {
        var st = _st.cal;
        var first = new Date(y, m, 1);
        var days = new Date(y, m + 1, 0).getDate();
        var startCol = (first.getDay() + 6) % 7; // 周一=0
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
      var calMonths = Vue.computed(function () {
        var st = _st.cal;
        if (!st.base) return [];
        var leftY = st.base.getFullYear(), leftM = st.base.getMonth();
        var right = new Date(leftY, leftM + 1, 1);
        return [ _buildMonth(leftY, leftM, true), _buildMonth(right.getFullYear(), right.getMonth(), false) ];
      });
      function calPick(y, m, d) {
        var st = _st.cal;
        var dt = new Date(y, m, d);
        if (st.pickStart) {
          st.start = dt; st.end = null; st.pickStart = false;
        } else {
          if (dt < st.start) { st.end = st.start; st.start = dt; }
          else { st.end = dt; }
          _st.dateStart = _fmtDate(st.start);
          _st.dateEnd = _fmtDate(st.end);
          updateDateLabel();
          st.open = false;
          st.pickStart = true;
        }
      }
      function calNav(delta) {
        var st = _st.cal;
        st.base = new Date(st.base.getFullYear(), st.base.getMonth() + delta, 1);
      }
      function calClear() {
        _st.dateStart = ''; _st.dateEnd = '';
        updateDateLabel();
        _st.cal.open = false;
      }
      function calToday() {
        var now = new Date();
        var dt = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        _st.dateStart = _fmtDate(dt);
        _st.dateEnd = _fmtDate(dt);
        _st.cal.start = dt; _st.cal.end = dt;
        updateDateLabel();
        _st.cal.open = false;
      }

      // ---------- 种草智能体（文案生成） ----------
      function agentAsk(type) {
        var typeMap = { body: '种草正文', comment: '评论区文案', video: '口播文案' };
        var label = typeMap[type] || '';
        _st.infoModal.types = label ? [label] : [];
        _st.infoModal.open = true;
      }
      function closeInfoModal() { _st.infoModal.open = false; }
      function infoSubmit() {
        var m = _st.infoModal;
        if (!(m.name || '').trim()) { App.showToast('请先填写产品名称', 'error'); return; }
        if (!m.types.length) { App.showToast('请至少选择一种内容类型', 'error'); return; }
        var lines = ['产品名称：' + m.name.trim()];
        if ((m.category || '').trim()) lines.push('产品品类：' + m.category.trim());
        if ((m.selling || '').trim()) lines.push('核心卖点：' + m.selling.trim());
        if ((m.audience || '').trim()) lines.push('目标人群：' + m.audience.trim());
        if ((m.platform || '').trim()) lines.push('投放平台：' + m.platform.trim());
        if ((m.price || '').trim()) lines.push('价格区间：' + m.price.trim());
        if ((m.scene || '').trim()) lines.push('使用场景/痛点：' + m.scene.trim());
        lines.push('内容类型：' + m.types.join('、'));
        if ((m.style || '').trim()) lines.push('风格偏好：' + m.style.trim());
        if ((m.note || '').trim()) lines.push('补充说明：' + m.note.trim());
        _st.infoModal.open = false;
        runAgent(lines.join('\n'));
      }
      async function runAgent(question) {
        if (_st.agent.busy) return;
        _st.agent.busy = true;
        _st.agent.error = '';
        _st.agent.result = '';
        _st.agent.meta = '';
        try {
          var data = await ApiService.runSeedingAgent(question);
          if (!data) {
            _st.agent.error = '后端服务不可用，无法生成文案。';
          } else {
            var analysis = (data.analysis && data.analysis.trim()) || '';
            var meta = data.meta || {};
            if (analysis) {
              _st.agent.result = analysis;
            } else {
              _st.agent.error = '智能体未返回有效结果，请稍后重试。';
            }
            if (meta && meta.kb_count !== undefined) {
              _st.agent.meta = '已上传文案库样本：' + (meta.kb_count || 0) + ' 条';
            }
          }
        } catch (e) {
          _st.agent.error = '生成失败：网络异常，请稍后再试。';
        } finally {
          _st.agent.busy = false;
        }
      }
      function agentSend() {
        var q = (_st.agent.input || '').trim();
        if (!q) { App.showToast('请输入种草文案需求', 'error'); return; }
        _st.agent.input = '';
        runAgent(q);
      }

      // ---------- 首次挂载时加载数据 ----------
      loadAccounts();
      loadWorks();
      loadMeta();
      loadCookie();
      loadDeleted();

      return {
        state: _st,
        filteredWorks: filteredWorks,
        filteredDeleted: filteredDeleted,
        filteredAccounts: filteredAccounts,
        calMonths: calMonths,
        switchTab: switchTab, switchPlatform: switchPlatform, filterDept: filterDept,
        worksSearchLocked: worksSearchLocked, accountSearchLocked: accountSearchLocked, unlockWorksSearch: unlockWorksSearch, unlockAccountSearch: unlockAccountSearch,
        sortWorks: sortWorks, sortArrow: sortArrow,
        openAccountModal: openAccountModal, closeAccountModal: closeAccountModal, saveAccount: saveAccount, deleteAccount: deleteAccount,
        deleteDeleted: deleteDeleted, clearDeleted: clearDeleted,
        closeConfirm: closeConfirm, confirmAction: confirmAction,
        toggleUpdatePanel: toggleUpdatePanel, saveCookie: saveCookie, triggerScrape: triggerScrape,
        calToggle: calToggle, calPick: calPick, calNav: calNav, calClear: calClear, calToday: calToday,
        agentAsk: agentAsk, closeInfoModal: closeInfoModal, infoSubmit: infoSubmit, agentSend: agentSend,
      };
    },
    template: `
<div class="sd-layout">
  <div class="sd-left">
    <!-- 顶部 -->
    <div class="dashboard-header">
      <div class="dh-left">
        <div class="dh-icon" style="background:linear-gradient(135deg,#16a34a,#22c55e);box-shadow:0 2px 8px rgba(22,163,74,0.25)"><i class="fa-solid fa-seedling" style="color:#fff;font-size:18px"></i></div>
        <div class="dh-title-group">
          <h2 class="dh-title">种草监测中台</h2>
          <span class="dh-subtitle">Seeding Monitoring Center</span>
          <span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>监测种草账号作品数据 · 数据更新时间 <span style="color:#16a34a;font-weight:600">{{ state.updateTime }}</span></span>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:16px">
        <div class="dh-chips">
          <div class="dh-chip"><span class="dh-chip-label">种草账号</span><span class="dh-chip-value" style="color:#16a34a">{{ state.accounts.length }}</span></div>
          <div class="dh-chip"><span class="dh-chip-label">作品数</span><span class="dh-chip-value" style="color:#6366f1">{{ state.works.length }}</span></div>
        </div>
        <div class="sd-header-actions">
          <button class="btn btn-sm btn-outline" @click="toggleUpdatePanel"><i class="fa-solid fa-rotate"></i> 数据更新</button>
          <div class="sd-update-panel" :class="{ hidden: !state.updatePanelOpen }">
            <div class="sd-cookie-label"><i class="fa-solid fa-cookie-bite" style="color:#16a34a"></i>抖音 Cookie</div>
            <textarea class="sd-cookie-textarea" v-model="state.cookies.douyin" placeholder="粘贴抖音网页版登录态 Cookie..."></textarea>
            <div class="sd-cookie-actions"><button class="btn btn-primary btn-sm" @click="saveCookie('douyin')"><i class="fa-solid fa-floppy-disk"></i> 保存抖音 Cookie</button></div>
            <div style="height:1px;background:#f1f5f9;margin:14px 0"></div>
            <div class="sd-cookie-label"><i class="fa-solid fa-cookie-bite" style="color:#e11d48"></i>小红书 Cookie</div>
            <textarea class="sd-cookie-textarea" v-model="state.cookies.xhs" placeholder="粘贴小红书网页版 Cookie（a1=...; web_session=...）..."></textarea>
            <div class="sd-cookie-actions"><button class="btn btn-primary btn-sm" @click="saveCookie('xhs')"><i class="fa-solid fa-floppy-disk"></i> 保存小红书 Cookie</button></div>
            <div class="sd-cookie-hint">未变更时显示当前 Cookie；更换后点击对应「保存」即可。小红书 Cookie 也可用「扫码登录」生成。</div>
            <div class="sd-cookie-actions">
              <button class="btn btn-sm" style="background:linear-gradient(135deg,#16a34a,#22c55e);color:#fff;border:none" @click="triggerScrape('douyin')"><i class="fa-solid fa-play"></i> 触发抖音抓取</button>
              <button class="btn btn-sm" style="background:linear-gradient(135deg,#e11d48,#f43f5e);color:#fff;border:none" @click="triggerScrape('xhs')"><i class="fa-solid fa-play"></i> 触发小红书抓取</button>
            </div>
            <div class="vd-progress-wrap" v-show="state.scraping" style="margin-top:14px">
              <div class="vd-progress-bar"><div class="vd-progress-fill" :style="{ width: state.progressPercent + '%' }"></div></div>
              <div class="vd-progress-text"><span>{{ state.progressText }}</span><span>{{ state.progressPercent }}%</span></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 标签页 -->
    <div class="sd-tabs">
      <button class="sd-tab" :class="{ active: state.tab === 'works' }" @click="switchTab('works')"><i class="fa-solid fa-clapperboard" style="margin-right:6px"></i>作品数据</button>
      <button class="sd-tab" :class="{ active: state.tab === 'accounts' }" @click="switchTab('accounts')"><i class="fa-solid fa-user-group" style="margin-right:6px"></i>种草账号</button>
    </div>

    <!-- 作品数据面板 -->
    <div v-show="state.tab === 'works'">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap">
        <div style="display:flex;gap:2px;background:#f1f5f9;border-radius:8px;padding:3px">
          <button class="sd-plat-btn" :class="{ active: state.platform === 'douyin' }" @click="switchPlatform('douyin')">抖音</button>
          <button class="sd-plat-btn" :class="{ active: state.platform === 'xhs' }" @click="switchPlatform('xhs')">小红书</button>
          <button class="sd-plat-btn" :class="{ active: state.platform === 'deleted' }" @click="switchPlatform('deleted')">🗑 被删作品</button>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span style="font-size:0.82rem;color:#64748b;font-weight:500">部门筛选</span>
          <button class="sd-dept-btn" :class="{ active: state.deptFilter === '' }" @click="filterDept('')">全部</button>
          <button class="sd-dept-btn" :class="{ active: state.deptFilter === '三部' }" @click="filterDept('三部')">三部</button>
          <button class="sd-dept-btn" :class="{ active: state.deptFilter === '四部' }" @click="filterDept('四部')">四部</button>
          <button class="sd-dept-btn" :class="{ active: state.deptFilter === '五部' }" @click="filterDept('五部')">五部</button>
        </div>
      </div>

      <!-- 作品表格卡片 -->
      <div class="ps-table-card" v-show="state.platform !== 'deleted'">
        <div class="ps-table-header">
          <div class="ps-table-title-group">
            <h3><i class="fa-solid fa-clapperboard" style="color:#16a34a;margin-right:6px"></i>种草作品数据</h3>
            <span class="ps-table-badge" style="background:#f0fdf4;color:#16a34a">共 {{ filteredWorks.length }} 条</span>
          </div>
          <div class="ps-table-tools">
            <div class="ps-search-wrap"><i class="fa-solid fa-search"></i><input type="text" class="ps-search-input" v-model="state.worksSearch" autocomplete="off" :readonly="worksSearchLocked" @focus="unlockWorksSearch" placeholder="搜索标题/账号..."></div>
            <div style="position:relative">
              <button type="button" @click="calToggle" style="display:flex;align-items:center;gap:6px;background:#fff;border:1px solid #e2e8f0;border-radius:9px;padding:5px 12px;height:33px;cursor:pointer;font-size:0.82rem;color:#334155;font-family:inherit">
                <i class="fa-regular fa-calendar" style="color:#94a3b8;font-size:0.82rem"></i>
                <span style="white-space:nowrap">{{ state.dateLabel }}</span>
                <i class="fa-solid fa-chevron-down" style="color:#94a3b8;font-size:0.7rem"></i>
              </button>
              <div v-show="state.cal.open" style="position:absolute;top:40px;right:0;z-index:60;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.15);padding:14px;user-select:none">
                <div class="od-cal-wrap">
                  <div class="od-cal" v-for="(mo, mi) in calMonths" :key="mi">
                    <div class="od-cal-head">
                      <button v-if="mo.isLeft" type="button" class="od-cal-nav" @click="calNav(-1)">‹</button>
                      <span v-else style="width:24px"></span>
                      <span>{{ mo.y }}年{{ mo.m + 1 }}月</span>
                      <button v-if="!mo.isLeft" type="button" class="od-cal-nav" @click="calNav(1)">›</button>
                      <span v-else style="width:24px"></span>
                    </div>
                    <div class="od-cal-week"><span v-for="w in ['一','二','三','四','五','六','日']" :key="w">{{ w }}</span></div>
                    <div class="od-cal-days">
                      <template v-for="(c, ci) in mo.cells" :key="ci">
                        <span v-if="c.blank" class="od-cal-day blank"></span>
                        <button v-else type="button" class="od-cal-day" :class="c.cls" @click="calPick(c.y, c.m, c.d)">{{ c.d }}</button>
                      </template>
                    </div>
                  </div>
                </div>
                <div style="display:flex;justify-content:space-between;align-items:center;margin-top:10px;padding-top:10px;border-top:1px solid #f1f5f9">
                  <span style="font-size:12px;color:#64748b">{{ state.cal.pickStart ? '请选择开始日期' : '请选择结束日期' }}</span>
                  <div style="display:flex;gap:10px">
                    <button @click="calClear" style="border:none;background:none;color:#94a3b8;font-size:12px;cursor:pointer">清除</button>
                    <button @click="calToday" style="border:none;background:none;color:#6366f1;font-size:12px;cursor:pointer;font-weight:600">今天</button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="ps-table-wrap">
          <table class="ps-store-table">
            <thead><tr>
              <th style="width:140px">名称</th><th style="width:120px">账号</th><th>标题</th>
              <th class="ps-col-num" style="width:100px;cursor:pointer" @click="sortWorks('likes')">点赞 <span class="sd-sort-arrow">{{ sortArrow('likes') }}</span></th>
              <th class="ps-col-num" style="width:90px;cursor:pointer" @click="sortWorks('comments')">评论 <span class="sd-sort-arrow">{{ sortArrow('comments') }}</span></th>
              <th class="ps-col-num" style="width:90px;cursor:pointer" @click="sortWorks('collects')">收藏 <span class="sd-sort-arrow">{{ sortArrow('collects') }}</span></th>
              <th class="ps-col-num" style="width:90px;cursor:pointer" @click="sortWorks('shares')">分享 <span class="sd-sort-arrow">{{ sortArrow('shares') }}</span></th>
              <th style="width:140px">发布时间</th>
            </tr></thead>
            <tbody>
              <tr v-for="(w, wi) in filteredWorks" :key="wi">
                <td><strong>{{ w.name }}</strong></td>
                <td style="font-family:monospace;font-size:12px">{{ w.account || '-' }}</td>
                <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                  <a v-if="w.link || w.url" :href="w.link || w.url" target="_blank" rel="noopener noreferrer" style="color:#1677ff;text-decoration:none">{{ w.title }}</a>
                  <template v-else>{{ w.title }}</template>
                </td>
                <td class="ps-col-num" style="color:#dc2626;font-weight:600">{{ (w.likes || 0).toLocaleString() }}</td>
                <td class="ps-col-num">{{ (w.comments || 0).toLocaleString() }}</td>
                <td class="ps-col-num">{{ (w.collects || 0).toLocaleString() }}</td>
                <td class="ps-col-num">{{ (w.shares || 0).toLocaleString() }}</td>
                <td style="font-size:12px;color:#64748b">{{ w.publishTime || '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 被删作品卡片 -->
      <div class="ps-table-card" v-show="state.platform === 'deleted'">
        <div class="ps-table-header">
          <div class="ps-table-title-group">
            <h3><i class="fa-solid fa-trash-can" style="color:#dc2626;margin-right:6px"></i>被删作品</h3>
            <span class="ps-table-badge" style="background:#fef2f2;color:#dc2626">共 {{ filteredDeleted.length }} 条</span>
          </div>
          <div class="ps-table-tools"><button class="btn btn-danger btn-sm" @click="clearDeleted"><i class="fa-solid fa-broom" style="margin-right:6px"></i>清空记录</button></div>
        </div>
        <div style="font-size:12px;color:#94a3b8;padding:10px 14px;border-top:1px solid #f1f5f9;background:#fafbfc">数据更新后自动对比，被删除的作品会保留在此，仅可手动清除。</div>
        <div class="ps-table-wrap">
          <table class="ps-store-table">
            <thead><tr>
              <th style="width:70px">平台</th><th style="width:140px">名称</th><th style="width:120px">账号</th><th>标题</th>
              <th style="width:150px">原发布时间</th><th style="width:150px">检测删除时间</th><th style="width:80px">操作</th>
            </tr></thead>
            <tbody>
              <tr v-if="!filteredDeleted.length"><td colspan="7" style="text-align:center;color:#94a3b8;padding:24px">暂无被删除的作品记录</td></tr>
              <tr v-for="(d, di) in filteredDeleted" :key="di">
                <td><span v-if="d.platform === 'xhs'" style="font-size:11px;color:#e11d48;font-weight:600">小红书</span><span v-else style="font-size:11px;color:#0284c7;font-weight:600">抖音</span></td>
                <td><strong>{{ d.name }}</strong></td>
                <td style="font-family:monospace;font-size:12px">{{ d.account || '-' }}</td>
                <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                  <a v-if="d.link || d.url" :href="d.link || d.url" target="_blank" rel="noopener noreferrer" style="color:#1677ff;text-decoration:none">{{ d.title }}</a>
                  <template v-else>{{ d.title }}</template>
                </td>
                <td style="font-size:12px;color:#64748b">{{ d.publishTime || '-' }}</td>
                <td style="font-size:12px;color:#dc2626">{{ d.deletedAt || '-' }}</td>
                <td><button class="ap-btn-sm delete" @click="deleteDeleted(d.id)"><i class="fa-solid fa-trash"></i></button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 种草账号面板 -->
    <div v-show="state.tab === 'accounts'">
      <div class="ap-toolbar">
        <div class="ap-search-wrap"><i class="fa-solid fa-search"></i><input class="ap-search-input" v-model="state.accountSearch" autocomplete="off" :readonly="accountSearchLocked" @focus="unlockAccountSearch" placeholder="搜索账号/抖音号/小红书号/主页链接..."></div>
        <select class="dh-select" v-model="state.accountPlatformFilter"><option value="">全部平台</option><option value="douyin">抖音</option><option value="xhs">小红书</option></select>
        <button class="ap-btn-primary" @click="openAccountModal()"><i class="fa-solid fa-plus"></i> 新增种草账号</button>
      </div>
      <div class="ap-table-wrap">
        <table class="ap-table">
          <thead><tr>
            <th style="width:50px">ID</th><th style="width:70px">平台</th><th style="width:140px">账号</th><th style="width:140px">抖音号/小红书号</th>
            <th style="width:90px">部门</th><th>主页链接</th><th style="width:90px">操作</th>
          </tr></thead>
          <tbody>
            <tr v-for="a in filteredAccounts" :key="a.id">
              <td>{{ a.id }}</td>
              <td><span v-if="(a.platform || 'douyin') === 'xhs'" style="font-size:11px;color:#e11d48;font-weight:600">小红书</span><span v-else style="font-size:11px;color:#0284c7;font-weight:600">抖音</span></td>
              <td><strong>{{ a.name || '' }}</strong></td>
              <td style="font-family:monospace;font-size:12px">{{ (a.platform || 'douyin') === 'xhs' ? (a.redId || '-') : (a.douyinId || '-') }}</td>
              <td>{{ a.department || '-' }}</td>
              <td style="font-size:12px;word-break:break-all">
                <template v-if="(a.platform || 'douyin') === 'xhs'"><span style="color:#94a3b8">-</span></template>
                <a v-else-if="a.homepage" :href="a.homepage" target="_blank" rel="noopener" style="color:#2563eb;text-decoration:none">{{ a.homepage }}</a>
                <span v-else style="color:#94a3b8">-</span>
              </td>
              <td><div class="ap-actions"><button class="ap-btn-sm edit" @click="openAccountModal(a.id)"><i class="fa-solid fa-pen"></i></button><button class="ap-btn-sm delete" @click="deleteAccount(a.id)"><i class="fa-solid fa-trash"></i></button></div></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="ap-table-info">共 {{ filteredAccounts.length }} 个种草账号</div>
    </div>
  </div>

  <!-- 右侧种草智能体（文案生成） -->
  <div class="sd-right">
    <div class="sd-agent">
      <div class="sd-agent-header">
        <div class="sd-agent-title"><i class="fa-solid fa-wand-magic-sparkles" style="color:#16a34a;margin-right:8px"></i>种草智能体</div>
        <span class="sd-agent-sub">种草君 · 基于 已上传文案库 知识库</span>
      </div>
      <div class="sd-agent-suggest">
        <button class="sd-agent-chip" @click="agentAsk('body')">📝 种草正文</button>
        <button class="sd-agent-chip" @click="agentAsk('comment')">💬 评论区文案</button>
        <button class="sd-agent-chip" @click="agentAsk('video')">🎬 口播文案</button>
      </div>
      <div class="sd-agent-result">
        <div v-if="state.agent.busy" class="sa-loading" style="color:#16a34a"><i class="fa-solid fa-spinner"></i> 正在生成文案，请稍候...</div>
        <div v-else-if="state.agent.error" class="sa-analysis" style="color:#dc2626">{{ state.agent.error }}</div>
        <div v-else-if="state.agent.result">
          <div class="sa-analysis">{{ state.agent.result }}</div>
          <div v-if="state.agent.meta" class="sa-meta">{{ state.agent.meta }}</div>
        </div>
        <div v-else class="sd-agent-empty">
          <i class="fa-solid fa-robot" style="font-size:28px;color:#cbd5e1"></i>
          <p>点击上方快捷问题，或直接输入产品信息与文案需求，种草君会先学习「已上传文案库」的风格，再生成原创文案。</p>
        </div>
      </div>
      <div class="sd-agent-input">
        <textarea v-model="state.agent.input" class="sd-agent-textarea" placeholder="例如：防晒霜 / 美妆 / 清爽不油腻、油皮可用 / 小红书 / 种草正文+口播脚本..." @keydown.enter.exact.prevent="agentSend"></textarea>
        <button class="sd-agent-send" :disabled="state.agent.busy" @click="agentSend"><i class="fa-solid fa-paper-plane"></i></button>
      </div>
    </div>
  </div>
</div>

<!-- 账号增删改弹窗 -->
<ecom-modal :visible="state.accountModal.open" :title="state.accountModal.isEdit ? '编辑种草账号' : '新增种草账号'" @close="closeAccountModal" @save="saveAccount">
  <div class="ap-form-group"><label>平台</label>
    <select class="ap-form-input" v-model="state.accountModal.platform"><option value="douyin">抖音</option><option value="xhs">小红书</option></select>
  </div>
  <div class="ap-form-group"><label>账号名称</label><input class="ap-form-input" v-model="state.accountModal.name" autocomplete="off" placeholder="例如：聚浪好物研究所"></div>
  <div v-show="state.accountModal.platform === 'douyin'" class="ap-form-group"><label>抖音号</label><input class="ap-form-input" v-model="state.accountModal.douyinId" autocomplete="off" placeholder="抖音号（如 julang_haowu）"></div>
  <div v-show="state.accountModal.platform === 'xhs'" class="ap-form-group"><label>小红书号</label><input class="ap-form-input" v-model="state.accountModal.redId" autocomplete="off" placeholder="小红书号（如 18930360363）"></div>
  <div class="ap-form-group"><label>部门</label><input class="ap-form-input" v-model="state.accountModal.department" autocomplete="off" placeholder="例如：三部 / 四部 / 五部"></div>
  <div v-show="state.accountModal.platform === 'douyin'" class="ap-form-group"><label>主页链接</label><input class="ap-form-input" v-model="state.accountModal.homepage" autocomplete="off" placeholder="https://www.douyin.com/user/MS4wLjAB..."></div>
  <div style="font-size:11px;color:#94a3b8">抖音需填写主页链接（自动解析 sec_user_id）；小红书无需主页链接，抓取时按小红书号解析。</div>
</ecom-modal>

<!-- 删除确认弹窗 -->
<ecom-modal :visible="state.confirm.open" title="确认操作" save-text="确定" :danger="true" @close="closeConfirm" @save="confirmAction">
  <p style="font-size:14px;color:#334155;line-height:1.6">{{ state.confirm.msg }}</p>
</ecom-modal>

<!-- 种草信息填写弹窗 -->
<ecom-modal :visible="state.infoModal.open" title="种草信息填写" width="600px" save-text="生成文案" @close="closeInfoModal" @save="infoSubmit">
  <div class="sd-info-row">
    <div class="sd-info-field"><label class="sd-info-label">产品名称 <span class="req">*</span></label><input class="sd-info-input" v-model="state.infoModal.name" placeholder="例如：XX清爽防晒霜"></div>
    <div class="sd-info-field"><label class="sd-info-label">产品品类</label><input class="sd-info-input" v-model="state.infoModal.category" placeholder="美妆/食品/家居/数码/服饰"></div>
  </div>
  <div class="sd-info-field"><label class="sd-info-label">核心卖点 <span class="muted">（最多3个，用大白话说）</span></label><textarea class="sd-info-textarea" v-model="state.infoModal.selling" placeholder="例如：清爽不油腻、油皮也能用、平价"></textarea></div>
  <div class="sd-info-row">
    <div class="sd-info-field"><label class="sd-info-label">目标人群</label><input class="sd-info-input" v-model="state.infoModal.audience" placeholder="宝妈/学生党/上班族/成分党"></div>
    <div class="sd-info-field"><label class="sd-info-label">价格区间</label><input class="sd-info-input" v-model="state.infoModal.price" placeholder="例如：99-149元"></div>
  </div>
  <div class="sd-info-row">
    <div class="sd-info-field"><label class="sd-info-label">投放平台</label><select class="sd-info-select" v-model="state.infoModal.platform"><option value="">请选择</option><option>抖音</option><option>小红书</option></select></div>
    <div class="sd-info-field"><label class="sd-info-label">风格偏好</label><input class="sd-info-input" v-model="state.infoModal.style" placeholder="碎碎念型/激动安利型/冷静吐槽型/搞笑整活型"></div>
  </div>
  <div class="sd-info-field"><label class="sd-info-label">使用场景 / 痛点</label><textarea class="sd-info-textarea" v-model="state.infoModal.scene" placeholder="用户在什么情况下会用到这个产品，有什么痛点"></textarea></div>
  <div class="sd-info-field">
    <label class="sd-info-label">内容类型 <span class="req">*</span></label>
    <div class="sd-info-check">
      <label><input type="checkbox" value="种草正文" v-model="state.infoModal.types">种草正文</label>
      <label><input type="checkbox" value="评论区文案" v-model="state.infoModal.types">评论区文案</label>
      <label><input type="checkbox" value="口播文案" v-model="state.infoModal.types">口播文案</label>
    </div>
  </div>
  <div class="sd-info-field"><label class="sd-info-label">补充说明 <span class="muted">（可选）</span></label><textarea class="sd-info-textarea" v-model="state.infoModal.note" placeholder="任何你想强调的点"></textarea></div>
</ecom-modal>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _sdApp = null;

  function mountSeedingVue() {
    if (_sdApp) return;
    var oldSection = document.getElementById('page-seeding-monitor');
    var mount = document.getElementById('page-seeding-monitor-vue');
    if (!oldSection || !mount) return;
    oldSection.style.display = 'none';
    mount.classList.remove('hidden');
    _sdApp = Vue.createApp(SeedingPage);
    _sdApp.mount(mount);
  }

  function unmountSeedingVue() {
    if (!_sdApp) return;
    _sdApp.unmount();
    _sdApp = null;
    var mount = document.getElementById('page-seeding-monitor-vue');
    if (mount) { mount.classList.add('hidden'); mount.innerHTML = ''; }
    var oldSection = document.getElementById('page-seeding-monitor');
    if (oldSection) oldSection.style.display = '';
  }

  function installSeedingHook() {
    var oldSection = document.getElementById('page-seeding-monitor');
    if (!oldSection) return;
    if (!oldSection.classList.contains('hidden')) mountSeedingVue();
    var observer = new MutationObserver(function () {
      if (!oldSection.classList.contains('hidden')) mountSeedingVue();
      else unmountSeedingVue();
    });
    observer.observe(oldSection, { attributes: true, attributeFilter: ['class'] });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installSeedingHook);
  } else {
    installSeedingHook();
  }
})();
