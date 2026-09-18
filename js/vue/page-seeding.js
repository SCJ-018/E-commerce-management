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
    deptFilter: '',               // '' | 部门名（可选部门见 deptConfig.departments，已动态化）
    accounts: [],                 // 种草账号列表
    works: [],                    // 作品列表
    deleted: [],                  // 被删作品列表
    accountSearch: '',            // 账号搜索词
    // ★★ 账号列表面板筛选（2026-09-18）：与「作品数据」同款式按钮组，**去掉平台下拉框**。
    //   故意与作品页的 platform / deptFilter 分开存：作品页 platform 还兼着「加载哪个平台的
    //   作品数据」，共用会把「切去账号页筛抖音」变成「顺手改了作品页的数据源」。
    accountPlatformFilter: '',    // '' | douyin | xhs（'' = 全部平台）
    accountDeptFilter: '',        // '' | 部门名（'' = 全部部门）
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
    // ★★ 双平台更新时间分别记录（2026-09-18 改）。
    //   原来只有单个 updateTime，取的是「当前选中平台」的 mtime ——
    //   而手机/宽屏下默认平台是 douyin，于是小红书已停更 4.9 小时时，
    //   顶部依旧显示抖音的 08:24，看起来"整页数据都很新鲜"（实际是假的）。
    //   现在两平台各记一份，顶部并列展示，哪个掉了直接看得见。
    updateTimes: { douyin: '--', xhs: '--' },
    // ★★ 勾选态（2026-09-18 补）：作品数据 / 种草账号 各一份，键分别是「作品唯一键」「账号 id」。
    //   为什么用「键」而不是行下标：表格可搜索/可排序，下标随时会变，
    //   勾了第 3 行再搜一下，下标 3 已经是另一条数据了。
    worksSelected: {},            // { 作品唯一键: true }
    accountsSelected: {},         // { 账号 id: true }

    meta: {
      douyin: { mtime: 0, rows: 0, source: '' },
      xhs:    { mtime: 0, rows: 0, source: '' },
    },
    accountModal: { open: false, isEdit: false, id: null, platform: 'douyin', name: '', douyinId: '', redId: '', department: '', homepage: '' },
    deptConfig: { departments: [], rules: {}, candidates: [] },  // 部门列表 + 部门→钉钉推送规则 + 联系人候选
    deptPanelOpen: false,         // 部门「+」浮层
    deptNewName: '',              // 待新增的部门名
    // ★ 部门拖拽排序（2026-09-18 补）：from = 拖起时的下标，over = 当前悬停下标。
    //   -1 = 无拖拽进行中。仅浮层内用，落盘后由后端 JSON 数组顺序持久化。
    deptDrag: { from: -1, over: -1 },
    // 「钉钉推送」配置悬浮窗：contacts = 部门 → {name,mobile,userId} 的输入态
    pushModal: { open: false, saving: false, matching: '', contacts: {}, uidOpen: {} },
    confirm: { open: false, msg: '', action: null },
    cal: { open: false, base: null, start: null, end: null, pickStart: true },   // 双月日历
    agent: { busy: false, input: '', result: '', meta: '', error: '' },          // 种草智能体
    infoModal: { open: false, name: '', category: '', selling: '', audience: '', platform: '', price: '', scene: '', style: '', note: '', types: [] },  // 信息填写弹窗
    // 「投喂爆文」弹窗：粘贴爆文 → 上传进「已上传爆文库」，智能体生成时会参考
    hotModal: { open: false, title: '', content: '', saving: false, list: [], loading: false },
    // 「优化建议」弹窗：提交后由后端落库并钉钉单聊发给开发人员
    fbModal: { open: false, content: '', sending: false },
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

  // ★★ 数据新鲜度分级（2026-09-18 补；同日改「时间窗感知」）
  //   为什么需要：原先只有一个「数据更新时间」，且取的是当前选中平台，
  //   导致小红书停更 4.9 小时时顶部仍显示抖音的 08:24 —— 页面在说谎。
  //   ★ 为什么不能按「距今多少小时」判（时间窗改造后）：
  //     自动抓取只在 09:00~19:00 之间进行，夜里 19:00 → 次日 09:00 本来就没数据。
  //     若按「距今」判，每天早上开跑前两个平台必然全红，是纯误报。
  //     所以改成对比「最近一次本应完成的抓取时刻」，夜间空档自然被排除：
  //       ok    滞后 < 1 小时  → 绿（正常，30 分钟一轮）
  //       warn  滞后 1~3 小时  → 橙（漏了 2~6 轮，需留意）
  //       stale 滞后 > 3 小时  → 红（与 tools/seeding_health.py 的 STALE_HOURS 对齐）
  //       none  无数据         → 灰
  //   ⚠ 时间窗 / 缓冲 / 阈值三处常量必须与 backend/app.py、tools/seeding_health.py 一致。
  var SCHED_START_HOUR = 9;
  var SCHED_END_HOUR = 19;
  var SCHED_STEP_MIN = 30;
  var FRESH_GRACE_MS = 40 * 60 * 1000;   // 与后端 _SEEDING_FRESH_GRACE（40 分钟）一致
  var FRESH_WARN_HOURS = 1.0;
  var FRESH_STALE_HOURS = 3.0;   // ⚠ 必须与 tools/seeding_health.py 的 STALE_HOURS 一致

  /** 某天时间窗内的全部抓取时刻（时间戳数组）：09:00 起每 30 分钟，含 19:00 */
  function _scheduledSlots(dayBase) {
    var slots = [];
    var t = new Date(dayBase.getFullYear(), dayBase.getMonth(), dayBase.getDate(),
                     SCHED_START_HOUR, 0, 0, 0).getTime();
    var end = new Date(dayBase.getFullYear(), dayBase.getMonth(), dayBase.getDate(),
                       SCHED_END_HOUR, 0, 0, 0).getTime();
    while (t <= end) { slots.push(t); t += SCHED_STEP_MIN * 60000; }
    return slots;
  }

  /** 最近一次「本应已完成」的抓取时刻（时间戳）；窗口外回落到上一窗口末 */
  function _lastExpectedRunMs() {
    var ref = Date.now() - FRESH_GRACE_MS;
    var d = new Date(ref);
    for (var i = 0; i < 2; i++) {
      var base = new Date(d.getFullYear(), d.getMonth(), d.getDate() - i);
      var slots = _scheduledSlots(base).filter(function (s) { return s <= ref; });
      if (slots.length) return slots[slots.length - 1];
    }
    var y = new Date(d.getFullYear(), d.getMonth(), d.getDate() - 1);
    return new Date(y.getFullYear(), y.getMonth(), y.getDate(), SCHED_END_HOUR, 0, 0, 0).getTime();
  }

  function _freshness(mtime) {
    if (!mtime) {
      return { level: 'none', color: '#94a3b8', text: '无数据', ageText: '尚无数据' };
    }
    // 展示文案仍用「距今多久」——它是客观事实；颜色才表达「是否按计划更新」
    var ageH = (Date.now() / 1000 - mtime) / 3600.0;
    var ageText;
    if (ageH < 1 / 60) ageText = '刚刚';
    else if (ageH < 1) ageText = Math.round(ageH * 60) + ' 分钟前';
    else if (ageH < 24) ageText = ageH.toFixed(1) + ' 小时前';
    else ageText = Math.floor(ageH / 24) + ' 天前';
    // 滞后量：数据比「最近一次应抓取时刻」旧了多少小时（负数 = 比应抓时刻还新）
    var lagH = (_lastExpectedRunMs() - mtime * 1000) / 3600000;
    var level, color;
    if (lagH < FRESH_WARN_HOURS) { level = 'ok'; color = '#16a34a'; }
    else if (lagH <= FRESH_STALE_HOURS) { level = 'warn'; color = '#d97706'; }
    else { level = 'stale'; color = '#dc2626'; }
    return { level: level, color: color, text: ageText, ageText: ageText,
             ageHours: ageH, lagHours: lagH };
  }

  // 作品唯一键：优先用链接（同一列表内唯一），无链接退回「账号+标题」。
  // 仅用于前端勾选态与本地过滤，不参与后端对账，所以不做 query 归一化。
  function _workKey(w) {
    w = w || {};
    var link = String(w.link || w.url || '').trim();
    if (link) return 'link:' + link;
    return 't:' + String(w.account || '').trim() + '|' + String(w.title || '').trim();
  }

  // 顶部状态圆点：跟随「最差」的那个平台，有平台掉线就不再是绿色
  function _worstLevel() {
    var order = { none: 0, ok: 1, warn: 2, stale: 3 };
    var worst = 'ok';
    ['douyin', 'xhs'].forEach(function (p) {
      var lv = _freshness((_st.meta[p] || {}).mtime).level;
      if (order[lv] > order[worst]) worst = lv;
    });
    return worst;
  }
  var _LEVEL_COLOR = { ok: '#10b981', warn: '#f59e0b', stale: '#ef4444', none: '#94a3b8' };

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
        // ★ 顶部要并列展示「两个平台各自的新鲜度」，所以优先用一次请求拿全：
        //   后端 /works/meta 已返回 platforms={douyin:{...},xhs:{...}}。
        //   老版本后端无该字段时，退化为按平台各拉一次（向后兼容）。
        var meta = await ApiService.getSeedingWorksMeta(platform || 'douyin');
        if (!meta) return;
        if (meta.platforms) {
          ['douyin', 'xhs'].forEach(function (p) {
            var m = meta.platforms[p] || {};
            _st.meta[p] = { mtime: m.mtime || 0, rows: m.rows || 0, source: m.source || '', level: m.level || '' };
            _st.updateTimes[p] = m.mtime ? _fmtTime(m.mtime) : '--';
          });
          return;
        }
        // 兼容旧后端：逐个平台拉
        var targets = platform ? [platform] : ['douyin', 'xhs'];
        for (var i = 0; i < targets.length; i++) {
          var p = targets[i];
          if (p !== 'douyin' && p !== 'xhs') continue;
          var m2 = await ApiService.getSeedingWorksMeta(p);
          if (!m2) continue;
          _st.meta[p] = { mtime: m2.mtime || 0, rows: m2.rows || 0, source: m2.source || '', level: m2.level || '' };
          _st.updateTimes[p] = m2.mtime ? _fmtTime(m2.mtime) : '--';
        }
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
        // 切平台/切到被删作品时清空勾选：勾的是上一个平台的作品，留着会误删
        _st.worksSelected = {};
        if (_st.platform === 'deleted') { loadDeleted(); }
        else { loadWorks(); loadMeta(); }
      }
      function filterDept(dept) { _st.deptFilter = dept || ''; }
      // 账号列表面板的两个筛选（按钮组，与作品页同款；空串 = 全部）
      function filterAccountPlatform(p) { _st.accountPlatformFilter = p || ''; }
      function filterAccountDept(d) { _st.accountDeptFilter = d || ''; }

      // ---------- 部门管理（动态列表；新增/删除都立即落盘） ----------
      async function loadDeptConfig() {
        var d = await ApiService.getSeedingDeptConfig();
        if (d && Array.isArray(d.departments)) {
          _st.deptConfig.departments = d.departments;
          _st.deptConfig.rules = d.rules || {};
          _st.deptConfig.candidates = d.candidates || [];
        }
      }
      function toggleDeptPanel() {
        _st.deptPanelOpen = !_st.deptPanelOpen;
        if (_st.deptPanelOpen) _st.deptNewName = '';
      }
      // ---------- 「+」浮层：点页面任意别处即关（2026-09-18 修） ----------
      //   原来只有一个 toggle，点哪儿都关不掉，必须回头再点一次「+」→ 很反直觉。
      //   这里对齐 page-order-details / page-category-marketing 的既有做法：
      //   document 级 click + wrap.contains 判定 —— 点在「+ 按钮 + 浮层」这个容器之外就关。
      //   ⚠ 容器必须同时含「+ 按钮」，否则按钮自身的 click 冒泡到 document 会立刻把刚打开的浮层关掉。
      var deptWrap = Vue.ref(null);
      function onDocClickDept(e) {
        if (!_st.deptPanelOpen) return;                                    // 没开就什么都不做
        if (deptWrap.value && deptWrap.value.contains(e.target)) return;  // 点在 + 或浮层内部
        _st.deptPanelOpen = false;
      }
      Vue.onMounted(function () { document.addEventListener('click', onDocClickDept); });
      Vue.onUnmounted(function () { document.removeEventListener('click', onDocClickDept); });
      /** 部门列表变化后同步规则表：保留同名部门的已有配置，丢弃已删部门 */
      function _syncDeptRules(depts) {
        var old = _st.deptConfig.rules || {};
        var nr = {};
        depts.forEach(function (d) {
          nr[d] = old[d] || { userId: '', userName: '', threshold: 0, enabled: false };
        });
        _st.deptConfig.rules = nr;
      }
      function _saveDeptConfig() {
        return ApiService.saveSeedingDeptConfig({
          departments: _st.deptConfig.departments,
          rules: _st.deptConfig.rules
        });
      }
      async function addDept() {
        var name = (_st.deptNewName || '').trim();
        if (!name) { App.showToast('请输入部门名称', 'error'); return; }
        if (_st.deptConfig.departments.indexOf(name) >= 0) {
          App.showToast('部门「' + name + '」已存在', 'error'); return;
        }
        var next = _st.deptConfig.departments.concat([name]);
        _st.deptConfig.departments = next;
        _syncDeptRules(next);
        var r = await _saveDeptConfig();
        if (r === null) {                       // request() 失败一律返回 null
          _st.deptConfig.departments = next.filter(function (x) { return x !== name; });
          _syncDeptRules(_st.deptConfig.departments);
          App.showToast('添加失败，请重试', 'error');
          return;
        }
        _st.deptNewName = '';
        App.showToast('已添加部门「' + name + '」');
      }
      async function removeDept(d) {
        if (_st.deptConfig.departments.length <= 1) {
          App.showToast('至少保留一个部门', 'error'); return;
        }
        var next = _st.deptConfig.departments.filter(function (x) { return x !== d; });
        _st.deptConfig.departments = next;
        _syncDeptRules(next);
        if (_st.deptFilter === d) _st.deptFilter = '';
        var r = await _saveDeptConfig();
        if (r === null) { App.showToast('删除失败，请重试', 'error'); }
        else { App.showToast('已删除部门「' + d + '」'); }
      }

      // ---------- 部门拖拽排序（2026-09-18 补） ----------
      // 拖完立即落盘：部门顺序会被后端 JSON 数组原样保存，进而决定
      // 「部门筛选」按钮顺序 + 钉钉推送弹窗表格行序 —— 顺序对业务是有意义的
      // （默认把最常看的部门拖到最前）。所以不做「暂存等保存」，松手即存。
      function deptDragStart(di, ev) {
        _st.deptDrag.from = di;
        _st.deptDrag.over = di;
        if (ev && ev.dataTransfer) {
          ev.dataTransfer.effectAllowed = 'move';
          // Firefox 必须 setData 才会真正启动拖拽；值本身不用
          try { ev.dataTransfer.setData('text/plain', String(di)); } catch (e) {}
        }
      }
      function deptDragOver(di) {
        if (_st.deptDrag.from < 0) return;
        _st.deptDrag.over = di;
      }
      function deptDragEnd() {
        _st.deptDrag.from = -1;
        _st.deptDrag.over = -1;
      }
      /** 把 from 位置的部门移动到 to 位置，并整份落盘（失败回滚） */
      async function moveDept(from, to) {
        var before = _st.deptConfig.departments.slice();
        if (from === to || from < 0 || to < 0 || from >= before.length || to >= before.length) return;
        var arr = before.slice();
        arr.splice(to, 0, arr.splice(from, 1)[0]);
        if (arr.join('\u0001') === before.join('\u0001')) return;
        _st.deptConfig.departments = arr;
        _syncDeptRules(arr);                 // 让 rules 键序跟随新顺序（同名部门配置不丢）
        var r = await _saveDeptConfig();
        if (r === null) {
          _st.deptConfig.departments = before;
          _syncDeptRules(before);
          App.showToast('排序保存失败，已还原', 'error');
        } else {
          App.showToast('部门顺序已保存');
        }
      }
      function deptDrop(di) {
        var from = _st.deptDrag.from;
        deptDragEnd();
        if (from >= 0 && from !== di) moveDept(from, di);
      }

      // ---------- 钉钉推送配置（部门 → 联系人 + 点赞阈值 N） ----------
      function openPushModal() {
        _st.pushModal.open = true;
        _st.pushModal.matching = '';
        _st.pushModal.uidOpen = {};
        loadDeptConfig().then(_fillPushContacts);   // 打开时拉最新名单（与「每日数据分析 → 钉钉推送」同一份）
      }
      /** 用已保存的绑定回填每行的「姓名 / 手机号」（手机号从候选名单里反查） */
      function _fillPushContacts() {
        var cs = {};
        (_st.deptConfig.departments || []).forEach(function (d) {
          var r = _st.deptConfig.rules[d] || {};
          var hit = (_st.deptConfig.candidates || []).filter(function (c) {
            return c.userId && c.userId === r.userId;
          })[0];
          cs[d] = { name: r.userName || (hit ? hit.name : '') || '',
                    mobile: (hit && hit.mobile) || '',
                    userId: r.userId || '' };
        });
        _st.pushModal.contacts = cs;
      }
      /** 取（必要时新建）某部门的输入对象——模板里所有输入框都直接读写它 */
      function pushContact(dept) {
        var m = _st.pushModal.contacts;
        if (!m[dept]) m[dept] = { name: '', mobile: '', userId: '' };
        return m[dept];
      }
      function toggleUidInput(dept) {
        var k = 'k_' + dept;
        _st.pushModal.uidOpen[k] = !_st.pushModal.uidOpen[k];
      }
      function uidInputOpen(dept) { return !!_st.pushModal.uidOpen['k_' + dept]; }
      function closePushModal() { _st.pushModal.open = false; }
      function _ruleOf(dept) {
        var r = _st.deptConfig.rules[dept];
        if (!r) {
          r = { userId: '', userName: '', threshold: 0, enabled: false };
          _st.deptConfig.rules[dept] = r;
        }
        return r;
      }
      /** 把某人绑到某部门：写 rules + 同步输入框显示 */
      function _bindContact(dept, userName, userId) {
        var r = _ruleOf(dept);
        r.userId = userId || '';
        r.userName = userName || '';
        var c = pushContact(dept);
        c.userId = r.userId;
        if (userName) c.name = userName;
      }
      /** 解绑（本地生效，点「保存配置」后落盘） */
      function clearPushContact(dept) {
        var r = _ruleOf(dept);
        r.userId = '';
        r.userName = '';
        var c = pushContact(dept);
        c.name = ''; c.mobile = ''; c.userId = '';
        App.showToast('已解绑「' + dept + '」的联系人（记得点「保存配置」）');
      }
      /** 姓名框输入：与名单里某人同名时自动带出手机号 / userId（等于快捷选已有成员） */
      function onPushNameInput(dept, v) {
        var c = pushContact(dept);
        c.name = v;
        var kw = (v || '').trim();
        if (!kw) return;
        var hit = (_st.deptConfig.candidates || []).filter(function (x) { return (x.name || '') === kw; })[0];
        if (hit) {
          if (hit.mobile) c.mobile = hit.mobile;
          if (hit.userId) c.userId = hit.userId;
        }
      }
      function setPushThreshold(dept, v) {
        _ruleOf(dept).threshold = Math.max(0, parseInt(v, 10) || 0);
      }
      function togglePushEnabled(dept) {
        var r = _ruleOf(dept);
        r.enabled = !r.enabled;
      }
      async function savePushModal() {
        var depts = _st.deptConfig.departments;
        for (var i = 0; i < depts.length; i++) {
          var r = _ruleOf(depts[i]);
          if (r.enabled && !r.userId) {
            App.showToast('「' + depts[i] + '」已启用，但还没选钉钉联系人', 'error'); return;
          }
          if (r.enabled && !(parseInt(r.threshold, 10) > 0)) {
            App.showToast('「' + depts[i] + '」已启用，但还没填点赞阈值', 'error'); return;
          }
        }
        _st.pushModal.saving = true;
        var res = await _saveDeptConfig();
        _st.pushModal.saving = false;
        if (res === null) { App.showToast('保存失败，请重试', 'error'); return; }
        _st.pushModal.open = false;
        App.showToast('钉钉推送配置已保存');
      }

      // ---------- 钉钉联系人：姓名 + 手机号 → 匹配 userId → 绑定到部门 ----------
      // ★★ 2026-09-18 改：名单**独立**（seeding_push_users），不再与「每日数据分析 → 钉钉推送」共用。
      //   两个场景的人本来就不是一拨人，共用一份会互相污染：这里为了点赞推送新增一个部门对接人，
      //   会凭空出现在日报收件人列表里；日报那边删人也会把这里绑定的人删掉。
      //   现在这边新增/查询只动种草名单，只有钉钉应用凭证（AppKey/Secret）继续共用。
      //   删除/停用本名单里的人 → 用弹窗底部的「已登记联系人」列表（或直接解绑部门）。
      async function matchPushContact(dept) {
        var c = pushContact(dept);
        var name = (c.name || '').trim();
        var mobile = (c.mobile || '').trim();
        var uid = (c.userId || '').trim();
        if (!name) { App.showToast('请先填联系人姓名', 'error'); return; }
        if (!mobile && !uid) { App.showToast('请填手机号，或展开「或直接填 userId」后填 userId', 'error'); return; }
        var cands = _st.deptConfig.candidates || [];
        _st.pushModal.matching = dept;
        try {
          // 1) 种草名单里已有同一个手机号（或同一个 userId）→ 直接复用那条记录，缺 userId 就顺手补上
          var exist = mobile ? cands.filter(function (x) { return (x.mobile || '') === mobile; })[0] : null;
          if (!exist && uid) exist = cands.filter(function (x) { return x.userId === uid; })[0];
          if (exist) {
            if (mobile && !exist.userId) {
              var mr = await ApiService.resolveSeedingPushUser(mobile);
              if (!mr.ok || !(mr.data && mr.data.userId)) {
                App.showToast('手机号 ' + mobile + ' 没匹配到钉钉 userId：' + ((mr && mr.msg) || '请确认号码正确且在应用可见范围内'), 'error');
                return;
              }
              var uw = await ApiService.updateSeedingPushUser(exist.id, { userId: mr.data.userId });
              if (!uw.ok) { App.showToast(uw.msg || '写入 userId 失败', 'error'); return; }
              exist.userId = mr.data.userId;
            }
            _bindContact(dept, exist.name || name, exist.userId || uid);
            App.showToast('已绑定名单里的「' + (exist.name || name) + '」到「' + dept + '」');
            return;
          }
          // 2) 名单里没有 → 用手机号换 userId（换不到就等于推不出去，必须拿到）
          if (mobile && !uid) {
            var r = await ApiService.resolveSeedingPushUser(mobile);
            if (!r.ok || !(r.data && r.data.userId)) {
              App.showToast('手机号 ' + mobile + ' 没匹配到钉钉 userId：' + ((r && r.msg) || '请确认号码正确且在应用可见范围内'), 'error');
              return;
            }
            uid = r.data.userId;
          }
          // 3) 新增到种草名单，并本地并入候选（不整表重载，免得冲掉其他部门还没保存的输入）
          var ar = await ApiService.addSeedingPushUser({ name: name, mobile: mobile, userId: uid, enabled: true });
          if (!ar.ok) { App.showToast(ar.msg || '新增联系人失败', 'error'); return; }
          cands.push({ id: (ar.data && ar.data.id) || ('new-' + mobile + '-' + uid), name: name,
                       mobile: mobile, userId: uid, enabled: 1 });
          _bindContact(dept, name, uid);
          App.showToast('已匹配并绑定「' + name + '」到「' + dept + '」（记得点「保存配置」）');
        } finally {
          _st.pushModal.matching = '';
        }
      }
      /** 从种草名单里删除一个联系人（部门已绑定时一并解绑） */
      function removePushContact(c) {
        if (!c || !c.id) return;
        _st.confirm.msg = '确定从「种草推送名单」里删除「' + (c.name || '') + '」吗？'
          + '（部门若已绑定该联系人，绑定也会一并清除，需点「保存配置」生效）';
        _st.confirm.action = function () {
          ApiService.deleteSeedingPushUser(c.id).then(function (res) {
            // requestFull 返回 {ok,data,msg}，失败时 ok=false（不会返回 null）
            if (!res || res.ok === false) {
              App.showToast((res && res.msg) || '删除失败，请重试', 'error'); return;
            }
            _st.deptConfig.candidates = (_st.deptConfig.candidates || []).filter(function (x) {
              return x.id !== c.id;
            });
            (_st.deptConfig.departments || []).forEach(function (d) {
              var r = _st.deptConfig.rules[d];
              if (r && r.userId && r.userId === c.userId) {
                r.userId = ''; r.userName = '';
                var cc = pushContact(d);
                cc.name = ''; cc.mobile = ''; cc.userId = '';
              }
            });
            App.showToast('已从种草名单删除「' + (c.name || '') + '」');
          });
        };
        _st.confirm.open = true;
      }

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

      // ---------- 账号列表（搜索 + 平台筛选 + 部门筛选） ----------
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
        // 部门筛选：口径与作品页一致（空 department 视为不属于任何部门，只在「全部」里出现）
        if (_st.accountDeptFilter) list = list.filter(function (a) {
          return (a.department || '') === _st.accountDeptFilter;
        });
        return list;
      });

      // ---------- 勾选态（作品数据 / 种草账号） ----------
      // 全选只作用于「当前筛选出来的行」——搜索后再点全选，删的才是眼前这些，
      // 不会把看不见的行一起删掉（这是最容易出事故的地方）。
      var worksSelCount = Vue.computed(function () {
        return filteredWorks.value.filter(function (w) { return !!_st.worksSelected[_workKey(w)]; }).length;
      });
      var allWorksSelected = Vue.computed(function () {
        var list = filteredWorks.value;
        return list.length > 0 && worksSelCount.value === list.length;
      });
      var worksSelectedItems = Vue.computed(function () {
        return filteredWorks.value.filter(function (w) { return !!_st.worksSelected[_workKey(w)]; });
      });
      var accountsSelCount = Vue.computed(function () {
        return filteredAccounts.value.filter(function (a) { return !!_st.accountsSelected[a.id]; }).length;
      });
      var allAccountsSelected = Vue.computed(function () {
        var list = filteredAccounts.value;
        return list.length > 0 && accountsSelCount.value === list.length;
      });

      function toggleWorkSel(w) {
        var k = _workKey(w);
        if (_st.worksSelected[k]) delete _st.worksSelected[k];
        else _st.worksSelected[k] = true;
      }
      function toggleAllWorks() {
        var next = {};
        if (!allWorksSelected.value) {
          filteredWorks.value.forEach(function (w) { next[_workKey(w)] = true; });
        }
        _st.worksSelected = next;   // 已全选时再点 = 取消全选（赋空对象）
      }
      function toggleAccountSel(id) {
        if (_st.accountsSelected[id]) delete _st.accountsSelected[id];
        else _st.accountsSelected[id] = true;
      }
      function toggleAllAccounts() {
        var next = {};
        if (!allAccountsSelected.value) {
          filteredAccounts.value.forEach(function (a) { next[a.id] = true; });
        }
        _st.accountsSelected = next;
      }

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
        _st.confirm.msg = '确定删除种草账号「' + (acc.name || '') + '」吗？'
          + '该账号的作品数据与「被删作品」记录会一并清空。';
        _st.confirm.action = function () {
          ApiService.deleteSeedingAccount(id).then(function (res) {
            if (res === null) { App.showToast('删除失败，请重试', 'error'); return; }
            _st.accounts = _st.accounts.filter(function (a) { return a.id !== id; });
            delete _st.accountsSelected[id];
            _reloadAfterAccountRemoved();
            App.showToast(_purgeMsg('种草账号已删除', res));
          });
        };
        _st.confirm.open = true;
      }
      /** 批量删除种草账号（只删当前勾选的） */
      function deleteAccountsSelected() {
        var ids = filteredAccounts.value
          .filter(function (a) { return !!_st.accountsSelected[a.id]; })
          .map(function (a) { return a.id; });
        if (!ids.length) { App.showToast('请先勾选要删除的种草账号', 'error'); return; }
        _st.confirm.msg = '确定批量删除选中的 ' + ids.length + ' 个种草账号吗？'
          + '这些账号的作品数据与「被删作品」记录会一并清空。';
        _st.confirm.action = function () {
          ApiService.batchDeleteSeedingAccounts(ids).then(function (res) {
            if (res === null) { App.showToast('批量删除失败，请重试', 'error'); return; }
            var idset = {};
            ids.forEach(function (i) { idset[i] = true; });
            _st.accounts = _st.accounts.filter(function (a) { return !idset[a.id]; });
            _st.accountsSelected = {};
            _reloadAfterAccountRemoved();
            App.showToast(_purgeMsg('已删除 ' + ((res && res.deleted) || ids.length) + ' 个种草账号', res));
          });
        };
        _st.confirm.open = true;
      }
      /** 删除账号后重拉作品侧数据：后端已把该账号的作品数据与被删记录一并清空，
       *  本地内存里的副本还是旧的，不重拉就会「删了账号作品还挂在列表上」。 */
      function _reloadAfterAccountRemoved() {
        if (_st.platform !== 'deleted') { loadWorks(); loadMeta(); }
        loadDeleted();
      }
      /** 提示文案：带上后端回报的「同步清空」数量，用户才知道作品也被一起清掉了 */
      function _purgeMsg(base, res) {
        var w = (res && res.works) || 0, d = (res && res.deletedWorks) || 0;
        if (!w && !d) return base;
        return base + '，同步清空 ' + w + ' 条作品数据、' + d + ' 条被删作品记录';
      }

      // ---------- 作品数据删除（单条 / 批量） ----------
      /** 删除作品数据：items = 要删的作品行（单条就传 [w]）。后端记入删除名单，抓取后也不会复活。 */
      function deleteWorks(items) {
        var list = (items || []).filter(Boolean);
        if (!list.length) { App.showToast('请先勾选要删除的作品数据', 'error'); return; }
        var tip = list.length === 1 ? '确定删除「' + ((list[0].title || '').slice(0, 30) || '这条作品') + '」吗？'
                                    : '确定删除选中的 ' + list.length + ' 条作品数据吗？';
        _st.confirm.msg = tip + '删除后不再显示，也不会再被点赞推送。';
        _st.confirm.action = function () {
          var platform = _st.platform === 'xhs' ? 'xhs' : 'douyin';
          ApiService.deleteSeedingWorks(platform, list).then(function (res) {
            if (res === null) { App.showToast('删除失败，请重试', 'error'); return; }
            var keys = {};
            list.forEach(function (w) { keys[_workKey(w)] = true; });
            _st.works = _st.works.filter(function (w) { return !keys[_workKey(w)]; });
            _st.worksSelected = {};
            App.showToast('已删除 ' + ((res && res.deleted) || list.length) + ' 条作品数据');
          });
        };
        _st.confirm.open = true;
      }
      function deleteWorksSelected() { deleteWorks(worksSelectedItems.value); }

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
        // ★ 没有可用账号时后端返回 triggered=false（不是错误）：只作提示，不报错、不轮询进度
        if (res.triggered === false) { App.showToast(res.reason || '暂无可抓取的账号，已跳过'); return; }
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
            if (meta && (meta.kb_count !== undefined || meta.hot_count !== undefined)) {
              _st.agent.meta = '知识库 —— 已上传文案库 ' + (meta.kb_count || 0)
                + ' 条 · 爆文库 ' + (meta.hot_count || 0) + ' 条';
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

      // ---------- 投喂爆文（存进「已上传爆文库」，智能体生成时参考） ----------
      async function openHotModal() {
        _st.hotModal.open = true;
        _st.hotModal.title = '';
        _st.hotModal.content = '';
        loadHotArticles();
      }
      function closeHotModal() { _st.hotModal.open = false; }
      async function loadHotArticles() {
        _st.hotModal.loading = true;
        try {
          var d = await ApiService.getSeedingHotArticles();
          _st.hotModal.list = (d && Array.isArray(d.items)) ? d.items : [];
        } finally {
          _st.hotModal.loading = false;
        }
      }
      async function submitHotArticle() {
        if (_st.hotModal.saving) return;   // 防连点重复上传
        var content = (_st.hotModal.content || '').trim();
        if (!content) { App.showToast('请先粘贴爆文内容', 'error'); return; }
        _st.hotModal.saving = true;
        try {
          var r = await ApiService.addSeedingHotArticle({
            title: (_st.hotModal.title || '').trim(),
            content: content,
            source: '人工投喂',
          });
          if (!r.ok) { App.showToast(r.msg || '上传失败，请重试', 'error'); return; }
          _st.hotModal.content = '';
          _st.hotModal.title = '';
          App.showToast(r.msg || '爆文已上传');
          loadHotArticles();
        } finally {
          _st.hotModal.saving = false;
        }
      }
      function deleteHotArticle(item) {
        if (!item || !item.id) return;
        _st.confirm.msg = '确定删除这条爆文吗？（原标题：' + ((item.title || '').slice(0, 20) || '无标题') + '）';
        _st.confirm.action = function () {
          ApiService.deleteSeedingHotArticle(item.id).then(function (r) {
            if (r.ok === false) { App.showToast(r.msg || '删除失败，请重试', 'error'); return; }
            _st.hotModal.list = _st.hotModal.list.filter(function (x) { return x.id !== item.id; });
            App.showToast('已删除该爆文');
          });
        };
        _st.confirm.open = true;
      }

      // ---------- 优化建议（落库 + 钉钉发给开发人员） ----------
      function openFbModal() {
        _st.fbModal.open = true;
        _st.fbModal.content = '';
      }
      function closeFbModal() { _st.fbModal.open = false; }
      async function submitFeedback() {
        if (_st.fbModal.sending) return;   // 防连点重复提交
        var content = (_st.fbModal.content || '').trim();
        if (!content) { App.showToast('请输入优化建议内容', 'error'); return; }
        _st.fbModal.sending = true;
        try {
          var r = await ApiService.submitSeedingFeedback({
            content: content,
            userName: sessionStorage.getItem('admin_current_user') || '',
            role: sessionStorage.getItem('admin_current_role') || '',
          });
          if (!r.ok) { App.showToast(r.msg || '提交失败，请重试', 'error'); return; }
          _st.fbModal.open = false;
          _st.fbModal.content = '';
          App.showToast('优化建议已发送给开发人员，感谢反馈！');
        } finally {
          _st.fbModal.sending = false;
        }
      }

      // ---------- 首次挂载时加载数据 ----------
      loadAccounts();
      loadWorks();
      loadMeta();
      loadCookie();
      loadDeleted();
      loadDeptConfig();

      // ---------- 新鲜度（响应式计算） ----------
      //   fresh：两平台各自的 {level,color,text,ageHours}，供顶部与平台按钮染色
      //   statusDotColor：顶部圆点，跟随「最差」平台 —— 有平台掉线就不再是绿色
      //   ⚠ 依赖 _st.meta[p].mtime（reactive），meta 更新后自动重算
      var fresh = Vue.computed(function () {
        return {
          douyin: _freshness((_st.meta.douyin || {}).mtime),
          xhs: _freshness((_st.meta.xhs || {}).mtime),
        };
      });
      var statusDotColor = Vue.computed(function () {
        return _LEVEL_COLOR[_worstLevel()] || _LEVEL_COLOR.ok;
      });
      // ★★ 「数据更新」（Cookie + 触发抓取）只对开发人员账号显示（2026-09-18）。
      //   角色存 sessionStorage（登录时写入），页面生命周期内不会变，直接取即可。
      //   注意：这里只是**显示层**收敛；后端接口未按角色拦截，普通账号理论上仍可直接调接口。
      var isDeveloper = (sessionStorage.getItem('admin_current_role') || '') === '开发人员';
      // 顶部整体是否有平台异常（用于提示文案「有平台数据滞后」）
      var hasStale = Vue.computed(function () {
        var lv = _worstLevel();
        return lv === 'warn' || lv === 'stale';
      });
      var worstAgeText = Vue.computed(function () {
        var f = fresh.value;
        var worst = null;
        ['douyin', 'xhs'].forEach(function (p) {
          var o = { none: 0, ok: 1, warn: 2, stale: 3 };
          if (!worst || o[f[p].level] > o[f[worst].level]) worst = p;
        });
        return worst ? ((worst === 'xhs' ? '小红书' : '抖音') + ' ' + f[worst].text) : '';
      });

      return {
        state: _st,
        fresh: fresh, statusDotColor: statusDotColor,
        hasStale: hasStale, worstAgeText: worstAgeText,
        filteredWorks: filteredWorks,
        filteredDeleted: filteredDeleted,
        filteredAccounts: filteredAccounts,
        calMonths: calMonths,
        // 勾选态 + 删除（作品单条/批量、账号批量）
        workKey: _workKey,
        worksSelCount: worksSelCount, allWorksSelected: allWorksSelected,
        accountsSelCount: accountsSelCount, allAccountsSelected: allAccountsSelected,
        toggleWorkSel: toggleWorkSel, toggleAllWorks: toggleAllWorks,
        toggleAccountSel: toggleAccountSel, toggleAllAccounts: toggleAllAccounts,
        deleteWorks: deleteWorks, deleteWorksSelected: deleteWorksSelected,
        deleteAccountsSelected: deleteAccountsSelected,
        switchTab: switchTab, switchPlatform: switchPlatform, filterDept: filterDept,
        filterAccountPlatform: filterAccountPlatform, filterAccountDept: filterAccountDept,
        worksSearchLocked: worksSearchLocked, accountSearchLocked: accountSearchLocked, unlockWorksSearch: unlockWorksSearch, unlockAccountSearch: unlockAccountSearch,
        sortWorks: sortWorks, sortArrow: sortArrow,
        openAccountModal: openAccountModal, closeAccountModal: closeAccountModal, saveAccount: saveAccount, deleteAccount: deleteAccount,
        deleteDeleted: deleteDeleted, clearDeleted: clearDeleted,
        closeConfirm: closeConfirm, confirmAction: confirmAction,
        toggleUpdatePanel: toggleUpdatePanel, saveCookie: saveCookie, triggerScrape: triggerScrape,
        calToggle: calToggle, calPick: calPick, calNav: calNav, calClear: calClear, calToday: calToday,
        agentAsk: agentAsk, closeInfoModal: closeInfoModal, infoSubmit: infoSubmit, agentSend: agentSend,
        // 部门管理 + 钉钉推送配置
        deptWrap: deptWrap,
        loadDeptConfig: loadDeptConfig, toggleDeptPanel: toggleDeptPanel, addDept: addDept, removeDept: removeDept,
        deptDragStart: deptDragStart, deptDragOver: deptDragOver, deptDragEnd: deptDragEnd, deptDrop: deptDrop,
        openPushModal: openPushModal, closePushModal: closePushModal,
        pushContact: pushContact, onPushNameInput: onPushNameInput,
        matchPushContact: matchPushContact, clearPushContact: clearPushContact,
        removePushContact: removePushContact,
        toggleUidInput: toggleUidInput, uidInputOpen: uidInputOpen,
        setPushThreshold: setPushThreshold,
        togglePushEnabled: togglePushEnabled, savePushModal: savePushModal,
        // 投喂爆文 / 优化建议 / 开发人员可见性
        isDeveloper: isDeveloper,
        openHotModal: openHotModal, closeHotModal: closeHotModal,
        submitHotArticle: submitHotArticle, deleteHotArticle: deleteHotArticle,
        openFbModal: openFbModal, closeFbModal: closeFbModal, submitFeedback: submitFeedback,
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
          <span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;margin-right:4px" :style="{ color: statusDotColor }"></i>监测种草账号作品数据 · 数据更新时间
            <span style="font-weight:600;margin-left:2px" :style="{ color: fresh.douyin.color }">抖音 {{ state.updateTimes.douyin }}</span>
            <span style="color:#cbd5e1;margin:0 6px">|</span>
            <span style="font-weight:600" :style="{ color: fresh.xhs.color }">小红书 {{ state.updateTimes.xhs }}</span>
          </span>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:16px">
        <div class="dh-chips">
          <div class="dh-chip"><span class="dh-chip-label">种草账号</span><span class="dh-chip-value" style="color:#16a34a">{{ state.accounts.length }}</span></div>
          <div class="dh-chip"><span class="dh-chip-label">作品数</span><span class="dh-chip-value" style="color:#6366f1">{{ state.works.length }}</span></div>
        </div>
        <div class="sd-header-actions">
          <button class="btn btn-sm btn-outline" @click="openPushModal"><i class="fa-solid fa-bell"></i> 钉钉推送</button>
          <!-- ★ 数据更新（Cookie + 触发抓取）只对开发人员显示：非开发人员看不到入口，也就点不到面板 -->
          <button v-if="isDeveloper" class="btn btn-sm btn-outline" @click="toggleUpdatePanel"><i class="fa-solid fa-rotate"></i> 数据更新</button>
          <div v-if="isDeveloper" class="sd-update-panel" :class="{ hidden: !state.updatePanelOpen }">
            <!-- ★★ 双平台抓取状态（2026-09-18 补）：一眼看出哪个平台在掉数据 -->
            <div class="sd-plat-status">
              <div class="sd-plat-status-item">
                <div class="sd-plat-status-head">
                  <i class="fa-solid fa-circle" style="font-size:7px" :style="{ color: fresh.douyin.color }"></i>
                  <span class="sd-plat-status-name">抖音</span>
                  <span class="sd-plat-status-age" :style="{ color: fresh.douyin.color }">{{ fresh.douyin.text }}</span>
                </div>
                <div class="sd-plat-status-meta">{{ state.updateTimes.douyin }} · {{ state.meta.douyin.rows }} 条作品</div>
              </div>
              <div class="sd-plat-status-item">
                <div class="sd-plat-status-head">
                  <i class="fa-solid fa-circle" style="font-size:7px" :style="{ color: fresh.xhs.color }"></i>
                  <span class="sd-plat-status-name">小红书</span>
                  <span class="sd-plat-status-age" :style="{ color: fresh.xhs.color }">{{ fresh.xhs.text }}</span>
                </div>
                <div class="sd-plat-status-meta">{{ state.updateTimes.xhs }} · {{ state.meta.xhs.rows }} 条作品</div>
              </div>
            </div>
            <div v-if="hasStale" class="sd-stale-hint">
              <i class="fa-solid fa-triangle-exclamation"></i> {{ worstAgeText }}未更新数据，请检查登录态是否过期
            </div>
            <div style="height:1px;background:#f1f5f9;margin:14px 0"></div>
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
          <button class="sd-plat-btn" :class="{ active: state.platform === 'douyin' }" @click="switchPlatform('douyin')">抖音<span class="sd-plat-age" :style="{ color: fresh.douyin.color }">{{ fresh.douyin.text }}</span></button>
          <button class="sd-plat-btn" :class="{ active: state.platform === 'xhs' }" @click="switchPlatform('xhs')">小红书<span class="sd-plat-age" :style="{ color: fresh.xhs.color }">{{ fresh.xhs.text }}</span></button>
          <button class="sd-plat-btn" :class="{ active: state.platform === 'deleted' }" @click="switchPlatform('deleted')">🗑 被删作品</button>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;position:relative">
          <span style="font-size:0.82rem;color:#64748b;font-weight:500">部门筛选</span>
          <button class="sd-dept-btn" :class="{ active: state.deptFilter === '' }" @click="filterDept('')">全部</button>
          <button class="sd-dept-btn" v-for="d in state.deptConfig.departments" :key="d" :class="{ active: state.deptFilter === d }" @click="filterDept(d)">{{ d }}</button>
          <!-- ★ 「+」按钮与浮层同在 deptWrap 内：document click 靠这个容器判定「点在不在外面」。
               容器必须把「+」按钮一起包进来，否则按钮自身的 click 冒泡到 document 会把刚打开的浮层立刻关掉。 -->
          <span ref="deptWrap" style="position:relative;display:inline-flex">
          <button class="sd-dept-btn" style="font-weight:700;padding:0 10px" title="添加部门" @click="toggleDeptPanel">+</button>
          <!-- 「+」浮层：新增部门 / 删除已有部门（点页面别处自动关闭） -->
          <div v-show="state.deptPanelOpen" style="position:absolute;top:36px;left:0;z-index:60;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.15);padding:14px;min-width:250px">
            <div style="font-size:12px;color:#64748b;margin-bottom:8px;font-weight:600">添加部门</div>
            <div style="display:flex;gap:6px">
              <input class="ap-form-input" v-model="state.deptNewName" autocomplete="off" placeholder="例如：六部" style="height:32px" @keyup.enter="addDept">
              <button class="btn btn-primary btn-sm" @click="addDept">添加</button>
            </div>
            <div style="height:1px;background:#f1f5f9;margin:12px 0"></div>
            <div style="font-size:12px;color:#64748b;margin-bottom:6px;font-weight:600">已有部门（拖动排序 · 点 × 删除）</div>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              <span v-for="(d, di) in state.deptConfig.departments" :key="d"
                    draggable="true"
                    :title="'按住拖动排序（当前第 ' + (di + 1) + ' 位）'"
                    @dragstart="deptDragStart(di, $event)"
                    @dragover.prevent="deptDragOver(di)"
                    @drop.prevent="deptDrop(di)"
                    @dragend="deptDragEnd"
                    :style="{
                      display:'inline-flex',alignItems:'center',gap:'6px',borderRadius:'6px',padding:'3px 8px',
                      fontSize:'12px',color:'#334155',userSelect:'none',cursor:'grab',
                      background: state.deptDrag.from === di ? '#e0f2fe' : (state.deptDrag.over === di ? '#dbeafe' : '#f1f5f9'),
                      border: '1px solid ' + (state.deptDrag.over === di ? '#38bdf8' : 'transparent'),
                      opacity: state.deptDrag.from === di ? 0.45 : 1
                    }">
                <i class="fa-solid fa-grip-vertical" style="color:#94a3b8;font-size:11px"></i>
                {{ d }}
                <i class="fa-solid fa-xmark" style="cursor:pointer;color:#94a3b8" @click="removeDept(d)"></i>
              </span>
            </div>
            <div style="font-size:11px;color:#94a3b8;margin-top:8px;line-height:1.6">顺序 = 「部门筛选」按钮顺序与推送配置的行序，松手即自动保存。</div>
          </div>
          </span>
        </div>
      </div>

      <!-- 作品表格卡片 -->
      <div class="ps-table-card" v-show="state.platform !== 'deleted'">
        <div class="ps-table-header">
          <div class="ps-table-title-group">
            <h3><i class="fa-solid fa-clapperboard" style="color:#16a34a;margin-right:6px"></i>种草作品数据</h3>
            <span class="ps-table-badge" style="background:#f0fdf4;color:#16a34a">共 {{ filteredWorks.length }} 条</span>
            <span v-if="worksSelCount" class="ps-table-badge" style="background:#fef2f2;color:#dc2626">已选 {{ worksSelCount }} 条</span>
          </div>
          <div class="ps-table-tools">
            <!-- 批量删除：只在有勾选时出现，避免误点 -->
            <button v-if="worksSelCount" class="btn btn-danger btn-sm" @click="deleteWorksSelected"><i class="fa-solid fa-trash-can" style="margin-right:6px"></i>删除选中 ({{ worksSelCount }})</button>
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
              <th style="width:40px;text-align:center"><input type="checkbox" style="width:15px;height:15px;cursor:pointer" title="全选当前筛选结果" :checked="allWorksSelected" @change="toggleAllWorks"></th>
              <th style="width:140px">名称</th><th style="width:120px">账号</th><th>标题</th>
              <th class="ps-col-num" style="width:100px;cursor:pointer" @click="sortWorks('likes')">点赞 <span class="sd-sort-arrow">{{ sortArrow('likes') }}</span></th>
              <th class="ps-col-num" style="width:90px;cursor:pointer" @click="sortWorks('comments')">评论 <span class="sd-sort-arrow">{{ sortArrow('comments') }}</span></th>
              <th class="ps-col-num" style="width:90px;cursor:pointer" @click="sortWorks('collects')">收藏 <span class="sd-sort-arrow">{{ sortArrow('collects') }}</span></th>
              <th class="ps-col-num" style="width:90px;cursor:pointer" @click="sortWorks('shares')">分享 <span class="sd-sort-arrow">{{ sortArrow('shares') }}</span></th>
              <th style="width:140px">发布时间</th>
              <th style="width:60px">操作</th>
            </tr></thead>
            <tbody>
              <tr v-if="!filteredWorks.length"><td colspan="10" style="text-align:center;color:#94a3b8;padding:24px">暂无作品数据</td></tr>
              <tr v-for="(w, wi) in filteredWorks" :key="wi">
                <td style="text-align:center"><input type="checkbox" style="width:15px;height:15px;cursor:pointer" :checked="!!state.worksSelected[workKey(w)]" @change="toggleWorkSel(w)"></td>
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
                <td><button class="ap-btn-sm delete" title="删除这条作品数据" @click="deleteWorks([w])"><i class="fa-solid fa-trash"></i></button></td>
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
        <div style="display:flex;align-items:center;gap:10px">
          <!-- 批量删除：只在有勾选时出现，避免误点 -->
          <button v-if="accountsSelCount" class="btn btn-danger" style="height:38px" @click="deleteAccountsSelected"><i class="fa-solid fa-trash-can" style="margin-right:6px"></i>批量删除 ({{ accountsSelCount }})</button>
          <button class="ap-btn-primary" @click="openAccountModal()"><i class="fa-solid fa-plus"></i> 新增种草账号</button>
        </div>
      </div>
      <!-- ★ 筛选条（2026-09-18）：与「作品数据」同款按钮组，原先的平台下拉框已去掉。
           平台与部门各用一套独立 state，不跟作品页的 platform / deptFilter 互相干扰。 -->
      <div style="display:flex;align-items:center;gap:14px;margin-bottom:14px;flex-wrap:wrap">
        <div style="display:flex;gap:2px;background:#f1f5f9;border-radius:8px;padding:3px">
          <button class="sd-plat-btn" :class="{ active: state.accountPlatformFilter === '' }" @click="filterAccountPlatform('')">全部平台</button>
          <button class="sd-plat-btn" :class="{ active: state.accountPlatformFilter === 'douyin' }" @click="filterAccountPlatform('douyin')">抖音</button>
          <button class="sd-plat-btn" :class="{ active: state.accountPlatformFilter === 'xhs' }" @click="filterAccountPlatform('xhs')">小红书</button>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span style="font-size:0.82rem;color:#64748b;font-weight:500">部门筛选</span>
          <button class="sd-dept-btn" :class="{ active: state.accountDeptFilter === '' }" @click="filterAccountDept('')">全部</button>
          <button class="sd-dept-btn" v-for="d in state.deptConfig.departments" :key="d" :class="{ active: state.accountDeptFilter === d }" @click="filterAccountDept(d)">{{ d }}</button>
        </div>
      </div>
      <div class="ap-table-wrap" id="sdAccountsCard">
        <table class="ap-table">
          <thead><tr>
            <th style="width:40px;text-align:center"><input type="checkbox" style="width:15px;height:15px;cursor:pointer" title="全选当前筛选结果" :checked="allAccountsSelected" @change="toggleAllAccounts"></th>
            <th style="width:50px">ID</th><th style="width:70px">平台</th><th style="width:140px">账号</th><th style="width:140px">抖音号/小红书号</th>
            <th style="width:90px">部门</th><th>主页链接</th><th style="width:90px">操作</th>
          </tr></thead>
          <tbody>
            <!-- 筛选/搜索后为空时的空态（原先没有，加了筛选就必然碰得到） -->
            <tr v-if="!filteredAccounts.length"><td colspan="8" style="text-align:center;color:#94a3b8;padding:24px">没有符合条件的种草账号</td></tr>
            <tr v-for="a in filteredAccounts" :key="a.id">
              <td style="text-align:center"><input type="checkbox" style="width:15px;height:15px;cursor:pointer" :checked="!!state.accountsSelected[a.id]" @change="toggleAccountSel(a.id)"></td>
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
        <span class="sd-agent-sub">种草君 · 基于 已上传文案库 + 爆文库</span>
      </div>
      <div class="sd-agent-suggest">
        <button class="sd-agent-chip" @click="agentAsk('body')">📝 种草正文</button>
        <button class="sd-agent-chip" @click="agentAsk('comment')">💬 评论区文案</button>
        <button class="sd-agent-chip" @click="agentAsk('video')">🎬 口播文案</button>
      </div>
      <div class="sd-agent-suggest" style="margin-top:-4px">
        <button class="sd-agent-chip" style="border-color:#fca5a5;color:#b91c1c;background:#fef2f2" @click="openHotModal">🔥 投喂爆文</button>
        <button class="sd-agent-chip" style="border-color:#93c5fd;color:#1d4ed8;background:#eff6ff" @click="openFbModal">💡 优化建议</button>
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
          <p>点击上方快捷问题，或直接输入产品信息与文案需求。种草君会先学习「已上传文案库」的风格与「爆文库」的爆款结构，再生成原创文案；也可以点「投喂爆文」上传你看中的爆款，「优化建议」把想法直接发给开发人员。</p>
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
  <div class="ap-form-group"><label>账号名称 <span style="color:#94a3b8;font-weight:400">（姓名+账号名）</span></label><input class="ap-form-input" v-model="state.accountModal.name" autocomplete="off" placeholder="例如：张三-聚浪好物研究所"></div>
  <div v-show="state.accountModal.platform === 'douyin'" class="ap-form-group"><label>抖音号</label><input class="ap-form-input" v-model="state.accountModal.douyinId" autocomplete="off" placeholder="抖音号（如 julang_haowu）"></div>
  <div v-show="state.accountModal.platform === 'xhs'" class="ap-form-group"><label>小红书号</label><input class="ap-form-input" v-model="state.accountModal.redId" autocomplete="off" placeholder="小红书号（如 18930360363）"></div>
  <div class="ap-form-group"><label>部门</label><input class="ap-form-input" v-model="state.accountModal.department" autocomplete="off" placeholder="例如：三部 / 四部 / 五部"></div>
  <div v-show="state.accountModal.platform === 'douyin'" class="ap-form-group"><label>主页链接 <span style="color:#94a3b8;font-weight:400">（仅链接剔除文本）</span></label><input class="ap-form-input" v-model="state.accountModal.homepage" autocomplete="off" placeholder="只粘贴链接，例如 https://v.douyin.com/gktlai2Rq9U/"></div>
  <div style="font-size:11px;color:#94a3b8">账号名称请按「姓名+账号名」填写（如 张三-聚浪好物研究所），方便按人员区分账号。<br>抖音主页链接请<b>只粘贴链接本身</b>：从抖音分享文案里复制时会带「长按复制此条消息，打开抖音搜索…」等文字，需把文字剔除后再粘贴，系统会自动解析 sec_user_id。<br>小红书无需主页链接，抓取时按小红书号解析。</div>
</ecom-modal>

<!-- 钉钉推送配置弹窗：部门 → 钉钉联系人 + 点赞阈值 N -->
<ecom-modal :visible="state.pushModal.open" title="钉钉推送设置" width="900px" saveText="保存配置" @close="closePushModal" @save="savePushModal">
  <div style="font-size:12px;color:#475569;line-height:1.75;margin-bottom:12px;background:#f8fafc;border-radius:8px;padding:10px 12px">
    为每个部门指定一位钉钉联系人并设置点赞阈值 <b>N</b>。<br>
    抓取完成后自动检查：作品点赞达到 N 时，把<b>作品链接</b>推送给该部门对应的联系人（同一作品只推一次）。<br>
    <span style="color:#94a3b8">联系人<b>直接填姓名 + 手机号</b>点「匹配并绑定」：系统按手机号换取钉钉 userId，名单里没这个人就自动新增。<br>
    ★ 这里维护的是<b>种草专用名单</b>，与「每日数据分析 → 钉钉推送」<b>完全独立</b>：在这边加的部门对接人不会出现在日报收件人里，那边删人也不影响这里的绑定。两边只共用钉钉应用凭证。</span>
  </div>
  <div class="ap-table-wrap" v-if="state.deptConfig.departments.length">
    <table class="ap-table">
      <thead><tr>
        <th style="width:80px">部门</th>
        <th>钉钉联系人（姓名 + 手机号匹配）</th>
        <th style="width:120px">点赞阈值 N</th>
        <th style="width:60px">启用</th>
      </tr></thead>
      <tbody>
        <tr v-for="(d, di) in state.deptConfig.departments" :key="d">
          <td><strong>{{ d }}</strong></td>
          <td>
            <div style="display:flex;gap:6px;align-items:center">
              <input class="ap-form-input" style="width:104px;height:32px" autocomplete="off"
                     :list="'sd-push-cand-' + di" placeholder="姓名"
                     :value="pushContact(d).name" @input="onPushNameInput(d, $event.target.value)">
              <input class="ap-form-input" style="width:130px;height:32px" autocomplete="off"
                     placeholder="手机号" :value="pushContact(d).mobile"
                     @input="pushContact(d).mobile = $event.target.value">
              <button type="button" :disabled="state.pushModal.matching === d"
                      style="height:32px;flex:0 0 auto;padding:0 10px;border:1px solid #16a34a;background:#f0fdf4;border-radius:6px;color:#15803d;cursor:pointer;font-size:12px"
                      @click="matchPushContact(d)">
                {{ state.pushModal.matching === d ? '匹配中…' : '匹配并绑定' }}
              </button>
              <datalist :id="'sd-push-cand-' + di">
                <option v-for="c in state.deptConfig.candidates" :key="'c' + c.id" :value="c.name"></option>
              </datalist>
            </div>
            <div style="font-size:11px;margin-top:4px;line-height:1.7">
              <template v-if="(state.deptConfig.rules[d] || {}).userId">
                <span style="color:#16a34a">✓ 已绑定「{{ (state.deptConfig.rules[d] || {}).userName }}」</span>
                <span style="color:#94a3b8;font-family:ui-monospace,Menlo,Consolas,monospace;margin-left:6px">userId {{ (state.deptConfig.rules[d] || {}).userId }}</span>
                <a href="javascript:;" style="color:#dc2626;margin-left:12px" @click="clearPushContact(d)">解绑</a>
              </template>
              <span v-else style="color:#94a3b8">未绑定 —— 填好姓名 + 手机号后点「匹配并绑定」</span>
              <a href="javascript:;" style="color:#2563eb;margin-left:12px" @click="toggleUidInput(d)">{{ uidInputOpen(d) ? '收起 userId' : '或直接填 userId' }}</a>
            </div>
            <div v-if="uidInputOpen(d)" style="margin-top:5px">
              <input class="ap-form-input" style="width:100%;height:28px;font-size:12px" autocomplete="off"
                     placeholder="钉钉 userId（手机号匹配不到时手填，如 17841605101729565）"
                     :value="pushContact(d).userId" @input="pushContact(d).userId = $event.target.value">
            </div>
          </td>
          <td>
            <input class="ap-form-input" type="number" min="0" step="100" style="height:32px"
                   :value="(state.deptConfig.rules[d] || {}).threshold || 0"
                   placeholder="如 1000" @input="setPushThreshold(d, $event.target.value)">
          </td>
          <td style="text-align:center">
            <input type="checkbox" style="width:16px;height:16px;cursor:pointer"
                   :checked="!!(state.deptConfig.rules[d] || {}).enabled" @change="togglePushEnabled(d)">
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <div v-else style="font-size:12px;color:#94a3b8;padding:10px 0">还没有部门，请先在「部门筛选」处点 + 添加部门。</div>

  <!-- 种草专有名单：在这里增/删，完全独立于「每日数据分析 → 钉钉推送」 -->
  <div style="margin-top:14px;border-top:1px solid #f1f5f9;padding-top:10px">
    <div style="font-size:12px;color:#334155;font-weight:600;margin-bottom:6px">
      已登记联系人（种草推送名单）<span style="color:#94a3b8;font-weight:400">共 {{ (state.deptConfig.candidates || []).length }} 人</span>
    </div>
    <div v-if="(state.deptConfig.candidates || []).length" style="display:flex;flex-wrap:wrap;gap:6px">
      <span v-for="c in state.deptConfig.candidates" :key="'pc' + c.id"
            style="display:inline-flex;align-items:center;gap:6px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:3px 8px;font-size:12px;color:#334155">
        {{ c.name || '(未命名)' }}
        <span style="color:#94a3b8;font-size:11px">{{ c.mobile || c.userId || '未填手机号' }}</span>
        <i class="fa-solid fa-xmark" style="cursor:pointer;color:#dc2626" title="从本名单删除" @click="removePushContact(c)"></i>
      </span>
    </div>
    <div v-else style="font-size:12px;color:#94a3b8">本名单还是空的 —— 在上面的表格里填姓名 + 手机号点「匹配并绑定」即可登记。</div>
  </div>

  <div style="font-size:11px;color:#94a3b8;margin-top:10px;line-height:1.75">
    ⚠️ 手机号匹配依赖钉钉应用的「手机号获取成员信息」权限；万一换不到 userId，点「或直接填 userId」手填即可。成员必须在该应用的「可见范围」内，否则推送会返回「不在可见范围」。<br>
    未勾选「启用」的部门不推送。本弹窗管理的是<b>种草专用名单</b>，「每日数据分析 → 钉钉推送」那边的人不在这里显示，反之亦然。
  </div>
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

<!-- 投喂爆文弹窗：粘贴爆文 → 存入「已上传爆文库」，智能体生成时参考 -->
<ecom-modal :visible="state.hotModal.open" title="投喂爆文" width="720px" save-text="上传到爆文库" @close="closeHotModal" @save="submitHotArticle">
  <div style="font-size:12px;color:#475569;line-height:1.75;margin-bottom:12px;background:#fef2f2;border-radius:8px;padding:10px 12px;border:1px solid #fecaca">
    🔥 把你看中的<b>爆款文案</b>整篇粘贴进来点「上传」，它会被存进独立的<b>爆文库</b>。<br>
    种草智能体生成文案前会学习爆文库里的<b>结构、开头钩子和节奏</b>，但<b>不会照抄句子</b>——投喂越多，写出来的爆款感越准。
  </div>
  <div class="ap-form-group">
    <label>标题 <span style="color:#94a3b8;font-weight:400">（可选，方便以后辨认）</span></label>
    <input class="ap-form-input" v-model="state.hotModal.title" autocomplete="off" placeholder="例如：油皮防晒爆文-小红书10w赞">
  </div>
  <div class="ap-form-group">
    <label>爆文内容 <span style="color:#dc2626">*</span></label>
    <textarea class="ap-form-input" v-model="state.hotModal.content" rows="10"
              style="height:auto;min-height:180px;line-height:1.7;resize:vertical"
              placeholder="把爆款文案原文整篇粘贴到这里（标题+正文都可以）..."></textarea>
  </div>
  <div style="margin-top:16px;border-top:1px solid #f1f5f9;padding-top:10px">
    <div style="font-size:12px;color:#334155;font-weight:600;margin-bottom:6px">
      已投喂爆文<span style="color:#94a3b8;font-weight:400">共 {{ (state.hotModal.list || []).length }} 篇</span>
    </div>
    <div v-if="state.hotModal.loading" style="font-size:12px;color:#94a3b8">加载中…</div>
    <template v-else>
      <div v-if="(state.hotModal.list || []).length" class="ap-table-wrap" style="max-height:240px;overflow:auto">
        <table class="ap-table">
          <thead><tr><th style="width:60px">ID</th><th>标题</th><th style="width:90px">字数</th><th style="width:150px">投喂时间</th><th style="width:70px">操作</th></tr></thead>
          <tbody>
            <tr v-for="h in state.hotModal.list" :key="h.id">
              <td>{{ h.id }}</td>
              <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ h.title || '(无标题)' }}</td>
              <td>{{ h.words }} 字</td>
              <td style="font-size:12px;color:#64748b">{{ (h.createdAt || '').slice(0, 16) }}</td>
              <td><button class="ap-btn-sm delete" title="删除这条爆文" @click="deleteHotArticle(h)"><i class="fa-solid fa-trash"></i></button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else style="font-size:12px;color:#94a3b8">还没有投喂过爆文。</div>
    </template>
  </div>
</ecom-modal>

<!-- 优化建议弹窗：提交后由后端落库并钉钉单聊发给开发人员 -->
<ecom-modal :visible="state.fbModal.open" title="优化建议" width="620px" save-text="提交给开发人员" @close="closeFbModal" @save="submitFeedback">
  <div style="font-size:12px;color:#475569;line-height:1.75;margin-bottom:12px;background:#eff6ff;border-radius:8px;padding:10px 12px;border:1px solid #bfdbfe">
    💡 想加什么功能、哪里不好用、文案效果不理想……都可以写在这里。<br>
    提交后会<b>直接钉钉发送给开发人员</b>，并留档一条记录。
  </div>
  <div class="ap-form-group">
    <label>建议内容 <span style="color:#dc2626">*</span></label>
    <textarea class="ap-form-input" v-model="state.fbModal.content" rows="8"
              style="height:auto;min-height:160px;line-height:1.7;resize:vertical"
              placeholder="例如：希望能按部门导出作品数据；口播文案希望支持指定时长；爆文库能不能加导入 Excel..."></textarea>
  </div>
  <div style="font-size:11px;color:#94a3b8">提交时会自动带上你的账号与角色，无需填写。</div>
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
