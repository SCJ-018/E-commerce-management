/**
 * 订单详情（单链接销售数据）— Vue 版（阶段 4）
 * classic script，与 app.js 共用全局词法作用域；接口复用 ApiService（已在 ApiService 补 getOrderDetailsData/getOrderDetailsStores）
 * 动态字段表格：4 张自选字段卡片 + 列选择面板 + 排序 + 分页 + 双月日历日期范围 + 平台/店铺联动筛选
 */
(function () {
  // ==================== 模块级状态（跨挂载/卸载保留，切走再回来不丢） ====================
  var _od = Vue.reactive({
    // 筛选
    platform: '',
    store: '',
    search: '',
    dateStart: '',
    dateEnd: '',
    dateLabel: '选择日期',
    // 分页/排序
    page: 1,
    pageSize: 20,
    sortBy: '',
    sortDir: 'desc',
    // 数据
    fields: [],          // 字段目录 [{key,label,type}]
    rows: [],
    total: 0,
    totalPages: 1,
    cardSummaries: [],   // [{key,label,type,value,agg}]
    // 配置
    cardFields: [],      // 4 个卡片字段 key
    tableCols: [],       // 表格列 key
    colPanelOpen: false,
    // 店铺下拉（选平台后联动加载）
    stores: [],
    // 双月日历
    cal: { open: false, base: null, start: null, end: null, pickStart: true },
  });

  var PLATFORMS = ['抖店', '京东', '千牛'];
  var CARD_COLORS = ['mkt-card--emerald', 'mkt-card--indigo', 'mkt-card--amber', 'mkt-card--rose'];

  // ==================== 格式化辅助（模块级，复刻旧 _odFmt*） ====================
  function _fmtMoney(v) { return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function _fmtInt(v) { return Math.round(v).toLocaleString('zh-CN'); }
  function _fmtPct(v) { return Number(v).toFixed(2) + '%'; }
  function _fmtByType(v, type) {
    if (v === null || v === undefined || v === '') return '—';
    if (type === 'money') return _fmtMoney(v);
    if (type === 'pct') return _fmtPct(v);
    if (type === 'int') return _fmtInt(v);
    if (type === 'decimal') return Number(v).toFixed(2);
    return v;
  }
  function _platformBadge(p) {
    if (p === '抖店') return 'badge-warning';
    if (p === '京东') return 'badge-danger';
    if (p === '千牛') return 'badge-info';
    return 'badge-gray';
  }
  function _fmtDate(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }
  function _parseDate(str) {
    if (!str) return null;
    var p = str.split('-');
    if (p.length !== 3) return null;
    return new Date(parseInt(p[0], 10), parseInt(p[1], 10) - 1, parseInt(p[2], 10));
  }
  function _yesterdayISO() { return new Date(Date.now() - 86400000).toISOString().slice(0, 10); }

  // ==================== 字段默认值 ====================
  function _defaultCardFields(fields) {
    return fields.filter(function (f) { return f.type === 'money' || f.type === 'int'; })
      .slice(0, 4).map(function (f) { return f.key; });
  }
  function _defaultTableCols(fields) {
    var date = fields.filter(function (f) { return f.type === 'date'; });
    var text = fields.filter(function (f) { return f.type === 'text'; });
    var num = fields.filter(function (f) { return ['money', 'int', 'pct', 'decimal'].indexOf(f.type) >= 0; });
    return date.concat(text.slice(0, 3)).concat(num.slice(0, 6)).slice(0, 10).map(function (f) { return f.key; });
  }

  // ==================== 组件 ====================
  var OrderDetailsPage = {
    setup: function () {
      // ---- 字段目录查找 ----
      function _fieldByKey(key) {
        for (var i = 0; i < _od.fields.length; i++) if (_od.fields[i].key === key) return _od.fields[i];
        return null;
      }

      // ---- 字段默认值应用（cardFields/tableCols 不合法则重算） ----
      function applyFieldDefaults() {
        var keys = _od.fields.map(function (f) { return f.key; });
        var cardValid = _od.cardFields.length === 4 && _od.cardFields.every(function (k) { return keys.indexOf(k) >= 0; });
        if (!cardValid) _od.cardFields = _defaultCardFields(_od.fields);
        var colValid = _od.tableCols.length > 0 && _od.tableCols.every(function (k) { return keys.indexOf(k) >= 0; });
        if (!colValid) _od.tableCols = _defaultTableCols(_od.fields);
        if (_od.sortBy && keys.indexOf(_od.sortBy) < 0) _od.sortBy = '';
      }

      // ---- 数据加载 ----
      async function fetchData() {
        var params = [
          'page=' + _od.page,
          'pageSize=' + _od.pageSize,
          'sortBy=' + _od.sortBy,
          'sortDir=' + _od.sortDir,
          'linkType=all',
        ];
        if (_od.dateStart) params.push('start=' + encodeURIComponent(_od.dateStart));
        if (_od.dateEnd) params.push('end=' + encodeURIComponent(_od.dateEnd));
        if (_od.platform) params.push('platform=' + encodeURIComponent(_od.platform));
        if (_od.store) params.push('store=' + encodeURIComponent(_od.store));
        if (_od.search) params.push('search=' + encodeURIComponent(_od.search));
        params.push('cardFields=' + encodeURIComponent(_od.cardFields.join(',')));

        var data = await ApiService.getOrderDetailsData(params.join('&'));
        if (!data) return;

        if (data.fields && data.fields.length) {
          var prevCardFields = _od.cardFields.join(',');
          _od.fields = data.fields;
          applyFieldDefaults();
          // cardFields 变了（首次进入/切换平台），后端这次不会返回卡片汇总，需按默认字段重拉一次
          if (_od.cardFields.join(',') !== prevCardFields) { await fetchData(); return; }
        }
        _od.rows = data.rows || [];
        _od.total = data.total || 0;
        _od.totalPages = data.totalPages || 1;
        _od.page = data.page || 1;
        _od.cardSummaries = data.cardSummaries || [];
      }

      async function loadStores(platform) {
        var stores = await ApiService.getOrderDetailsStores(platform, 'all');
        _od.stores = (stores && stores.length) ? stores : [];
      }

      // ---- 筛选交互 ----
      function onPlatformChange() {
        _od.page = 1;
        _od.cardFields = [];
        _od.tableCols = [];
        _od.sortBy = '';
        _od.sortDir = 'desc';
        _od.store = '';
        _od.stores = [];
        fetchData();
        if (_od.platform) loadStores(_od.platform);
      }
      function onStoreChange() { _od.page = 1; fetchData(); }
      function onSearchInput() { _od.page = 1; fetchData(); }
      function onPageSizeChange() { _od.pageSize = parseInt(_od.pageSize, 10) || 20; _od.page = 1; fetchData(); }

      // ---- 排序 / 列选择 / 分页 ----
      function sortBy(key) {
        if (_od.sortBy === key) _od.sortDir = _od.sortDir === 'desc' ? 'asc' : 'desc';
        else { _od.sortBy = key; _od.sortDir = 'desc'; }
        _od.page = 1;
        fetchData();
      }
      function toggleCol(key) {
        var i = _od.tableCols.indexOf(key);
        if (i >= 0) _od.tableCols.splice(i, 1);
        else _od.tableCols.push(key);
      }
      function goPage(n) {
        if (n < 1 || n > _od.totalPages || n === _od.page) return;
        _od.page = n;
        fetchData();
      }

      // ---- 日期快捷 ----
      function setDateRange(days) {
        var end = new Date(Date.now() - 86400000);
        var start = new Date(end.getTime() - (days - 1) * 86400000);
        _od.dateStart = start.toISOString().slice(0, 10);
        _od.dateEnd = end.toISOString().slice(0, 10);
        updateDateLabel();
        _od.page = 1;
        fetchData();
      }
      function isShortcutActive(days) {
        var yesterday = _yesterdayISO();
        if (days === 1) return _od.dateStart === yesterday && _od.dateEnd === yesterday;
        var calcStart = new Date(Date.now() - 86400000 - (days - 1) * 86400000).toISOString().slice(0, 10);
        return _od.dateStart === calcStart && _od.dateEnd === yesterday;
      }

      // ---- 双月日历 ----
      function updateDateLabel() {
        var s = _od.dateStart, e = _od.dateEnd;
        if (s && e) _od.dateLabel = s + ' — ' + e;
        else if (s) _od.dateLabel = s + ' — ';
        else if (e) _od.dateLabel = ' — ' + e;
        else _od.dateLabel = '选择日期';
      }
      function calToggle() {
        _od.cal.open = !_od.cal.open;
        if (_od.cal.open) {
          var now = new Date();
          _od.cal.base = new Date(now.getFullYear(), now.getMonth() - 1, 1);
          _od.cal.start = _parseDate(_od.dateStart);
          _od.cal.end = _parseDate(_od.dateEnd);
          _od.cal.pickStart = !(_od.cal.start && _od.cal.end);
        }
      }
      function _buildMonth(y, m, isLeft) {
        var st = _od.cal;
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
      var calMonths = Vue.computed(function () {
        var st = _od.cal;
        if (!st.base) return [];
        var leftY = st.base.getFullYear(), leftM = st.base.getMonth();
        var right = new Date(leftY, leftM + 1, 1);
        return [_buildMonth(leftY, leftM, true), _buildMonth(right.getFullYear(), right.getMonth(), false)];
      });
      function calPick(y, m, d) {
        var st = _od.cal;
        var dt = new Date(y, m, d);
        if (st.pickStart) {
          st.start = dt; st.end = null; st.pickStart = false;
        } else {
          if (dt < st.start) { st.end = st.start; st.start = dt; }
          else { st.end = dt; }
          _od.dateStart = _fmtDate(st.start);
          _od.dateEnd = _fmtDate(st.end);
          updateDateLabel();
          st.open = false;
          st.pickStart = true;
          _od.page = 1;
          fetchData();
        }
      }
      function calNav(delta) {
        var st = _od.cal;
        st.base = new Date(st.base.getFullYear(), st.base.getMonth() + delta, 1);
      }
      function calClear() {
        _od.dateStart = ''; _od.dateEnd = '';
        updateDateLabel();
        _od.cal.open = false;
        _od.page = 1;
        fetchData();
      }
      function calToday() {
        var now = new Date();
        var dt = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        _od.dateStart = _fmtDate(dt);
        _od.dateEnd = _fmtDate(dt);
        _od.cal.start = dt; _od.cal.end = dt;
        updateDateLabel();
        _od.cal.open = false;
        _od.page = 1;
        fetchData();
      }

      // ---- 卡片 / 表格渲染辅助 ----
      function cardSummary(key) {
        for (var i = 0; i < _od.cardSummaries.length; i++) if (_od.cardSummaries[i].key === key) return _od.cardSummaries[i];
        return null;
      }
      function cardValue(i) {
        var s = cardSummary(_od.cardFields[i]);
        return s ? _fmtByType(s.value, s.type) : '—';
      }
      function cardSub(i) {
        var s = cardSummary(_od.cardFields[i]);
        if (!s) return '';
        var aggText = s.agg === 'sum' ? '总和' : s.agg === 'avg' ? '均值' : '去重数';
        return s.label + ' · ' + aggText;
      }
      var tableColumns = Vue.computed(function () {
        return _od.tableCols.filter(function (k) { return _od.fields.some(function (f) { return f.key === k; }); })
          .map(function (k) { return _fieldByKey(k); }).filter(Boolean);
      });
      function cellStyle(f, v) {
        var isId = /ID|编码|SPU|货号/.test(f.label) && !/名称/.test(f.label);
        if (isId) return 'padding:8px;white-space:nowrap;font-family:monospace;font-size:11px;color:#6366f1';
        var isRefund = /退款|退货|差评|投诉|不满意/.test(f.label);
        var num = Number(v) || 0;
        if (isRefund) return 'padding:8px;text-align:right;white-space:nowrap;font-size:12px;color:' + (num > 0 ? '#ef4444' : '#94a3b8');
        if (f.type === 'text' || f.type === 'date') return 'padding:8px;text-align:left;white-space:nowrap;font-size:12px';
        if (f.type === 'money') return 'padding:8px;text-align:right;white-space:nowrap;font-weight:600;color:#1e293b';
        return 'padding:8px;text-align:right;white-space:nowrap;font-size:12px';
      }
      function cellPrefix(f, v) {
        var isRefund = /退款|退货|差评|投诉|不满意/.test(f.label);
        var num = Number(v) || 0;
        if (isRefund && f.type === 'money' && num > 0) return '-';
        return '';
      }
      function thAlign(f) { return (f.type === 'text' || f.type === 'date') ? 'left' : 'right'; }
      var pageList = Vue.computed(function () {
        var tp = _od.totalPages, p = _od.page, out = [];
        for (var i = 1; i <= tp; i++) {
          if (tp <= 7 || i === 1 || i === tp || (i >= p - 1 && i <= p + 1)) out.push({ t: 'page', n: i });
          else if (i === p - 2 || i === p + 2) out.push({ t: 'gap' });
        }
        return out;
      });

      // ---- 搜索框防浏览器自动填充 ----
      var searchLocked = Vue.ref(true);
      function unlockSearch() { searchLocked.value = false; }

      // ---- 点击别处关闭日期/字段悬浮框 ----
      var dateWrap = Vue.ref(null);
      var colWrap = Vue.ref(null);
      function onDocClick(e) {
        if (dateWrap.value && !dateWrap.value.contains(e.target)) _od.cal.open = false;
        if (colWrap.value && !colWrap.value.contains(e.target)) _od.colPanelOpen = false;
      }
      Vue.onMounted(function () { document.addEventListener('click', onDocClick); });
      Vue.onUnmounted(function () { document.removeEventListener('click', onDocClick); });

      // ---- 首次挂载 ----
      function init() {
        if (!_od.dateStart && !_od.dateEnd) {
          var yesterday = _yesterdayISO();
          _od.dateStart = yesterday;
          _od.dateEnd = yesterday;
          updateDateLabel();
        }
        fetchData();
      }
      init();

      return {
        od: _od,
        platforms: PLATFORMS,
        cardColors: CARD_COLORS,
        calMonths: calMonths,
        tableColumns: tableColumns,
        pageList: pageList,
        fmtInt: _fmtInt, fmtByType: _fmtByType, platformBadge: _platformBadge,
        cardValue: cardValue, cardSub: cardSub, cellStyle: cellStyle, cellPrefix: cellPrefix, thAlign: thAlign,
        fetchData: fetchData, onPlatformChange: onPlatformChange, onStoreChange: onStoreChange,
        onSearchInput: onSearchInput, onPageSizeChange: onPageSizeChange,
        sortBy: sortBy, toggleCol: toggleCol, goPage: goPage,
        setDateRange: setDateRange, isShortcutActive: isShortcutActive,
        calToggle: calToggle, calPick: calPick, calNav: calNav, calClear: calClear, calToday: calToday,
        searchLocked: searchLocked, unlockSearch: unlockSearch,
        dateWrap: dateWrap, colWrap: colWrap,
      };
    },

    template: `
<div>
  <!-- 顶部 -->
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon" style="background:linear-gradient(135deg,#6366f1,#818cf8);box-shadow:0 2px 8px rgba(99,102,241,.3)"><i class="fa-solid fa-receipt"></i></div>
      <div class="dh-title-group">
        <h2 class="dh-title">订单详情</h2>
        <span class="dh-subtitle">Order Detail Analytics</span>
        <span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>数据实时 · 每天 <b>{{ fmtInt(od.total) }}</b> 条链接记录</span>
      </div>
    </div>
    <div class="dh-filters">
      <div class="dh-date-shortcuts">
        <button class="dh-ds-btn" :class="{ active: isShortcutActive(1) }" @click="setDateRange(1)">昨日</button>
        <button class="dh-ds-btn" :class="{ active: isShortcutActive(7) }" @click="setDateRange(7)">近7天</button>
        <button class="dh-ds-btn" :class="{ active: isShortcutActive(30) }" @click="setDateRange(30)">近30天</button>
      </div>
      <select v-model="od.platform" class="dh-select" style="width:120px;min-width:120px;max-width:120px" @change="onPlatformChange">
        <option value="">全部平台</option>
        <option v-for="p in platforms" :key="p" :value="p">{{ p }}</option>
      </select>
      <select v-model="od.store" class="dh-select" style="width:150px;min-width:150px;max-width:150px" @change="onStoreChange">
        <option value="">全部店铺</option>
        <option v-for="s in od.stores" :key="s" :value="s">{{ s }}</option>
      </select>
      <div class="dh-date-picker" style="position:relative" ref="dateWrap">
        <button type="button" @click="calToggle" style="display:flex;align-items:center;gap:6px;background:#fff;border:1px solid oklch(92% 0.01 245);border-radius:9px;padding:5px 12px;height:33px;cursor:pointer;font-size:0.82rem;color:oklch(30% 0.02 245);font-family:inherit">
          <i class="fa-regular fa-calendar" style="color:oklch(65% 0.02 245);font-size:0.82rem"></i>
          <span style="white-space:nowrap">{{ od.dateLabel }}</span>
          <i class="fa-solid fa-chevron-down" style="color:oklch(65% 0.02 245);font-size:0.7rem"></i>
        </button>
        <div v-show="od.cal.open" style="position:absolute;top:40px;right:0;z-index:60;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.15);padding:14px;user-select:none">
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
            <span style="font-size:12px;color:#64748b">{{ od.cal.pickStart ? '请选择开始日期' : '请选择结束日期' }}</span>
            <div style="display:flex;gap:10px">
              <button @click="calClear" style="border:none;background:none;color:#94a3b8;font-size:12px;cursor:pointer">清除</button>
              <button @click="calToday" style="border:none;background:none;color:#6366f1;font-size:12px;cursor:pointer;font-weight:600">今天</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- 统计卡片：4 张，每张可独立选字段 -->
  <div class="mkt-cards-grid" style="margin-bottom:18px">
    <div v-for="(cc, i) in cardColors" :key="i" class="mkt-card" :class="cc">
      <div class="mkt-card-header" style="display:flex;align-items:center;gap:8px">
        <div class="mkt-card-icon"><i class="fa-solid fa-chart-simple"></i></div>
        <select v-model="od.cardFields[i]" class="od-card-sel" style="flex:1;min-width:0;border:none;background:transparent;font-size:13px;color:#64748b;font-weight:600;cursor:pointer;outline:none" @change="fetchData">
          <option v-for="f in od.fields" :key="f.key" :value="f.key">{{ f.label }}</option>
        </select>
      </div>
      <div class="mkt-card-body">
        <div class="mkt-card-value">{{ cardValue(i) }}</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:4px">{{ cardSub(i) }}</div>
      </div>
    </div>
  </div>

  <!-- 搜索栏 -->
  <div style="display:flex;gap:12px;margin-bottom:16px;align-items:center">
    <div class="ps-search-wrap" style="flex:1;max-width:360px">
      <i class="fa-solid fa-search"></i>
      <input type="text" class="ps-search-input" v-model="od.search" autocomplete="off" :readonly="searchLocked" @focus="unlockSearch" @input="onSearchInput" placeholder="搜索商品名称 / 链接ID...">
    </div>
    <div style="position:relative" ref="colWrap">
      <button @click="od.colPanelOpen = !od.colPanelOpen" style="display:flex;align-items:center;gap:6px;border:1px solid #e2e8f0;background:#fff;border-radius:8px;padding:6px 12px;font-size:13px;color:#475569;cursor:pointer">选择字段 <i class="fa-solid fa-chevron-down"></i></button>
      <div v-show="od.colPanelOpen" style="position:absolute;top:38px;right:0;z-index:60;background:#fff;border:1px solid #e2e8f0;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.14);padding:10px;min-width:210px;max-height:340px;overflow:auto">
        <label v-for="f in od.fields" :key="f.key" style="display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:6px;cursor:pointer;font-size:13px;color:#334155">
          <input type="checkbox" :checked="od.tableCols.indexOf(f.key) >= 0" @change="toggleCol(f.key)">
          <span>{{ f.label }}</span>
        </label>
      </div>
    </div>
    <span style="font-size:13px;color:#94a3b8">共 {{ fmtInt(od.total) }} 条记录</span>
  </div>

  <!-- 数据表格 -->
  <div class="ps-table-card">
    <div class="ps-table-wrap" style="max-height:calc(100vh - 360px);overflow:auto">
      <table style="min-width:1600px;width:100%;border-collapse:collapse;font-size:13px">
        <thead style="position:sticky;top:0;z-index:2;background:#f8fafc">
          <tr>
            <th style="padding:10px 8px;text-align:left;border-bottom:2px solid #e2e8f0;white-space:nowrap;color:#475569;font-weight:600">平台</th>
            <th v-for="f in tableColumns" :key="f.key" @click="sortBy(f.key)" :style="'padding:10px 8px;text-align:' + thAlign(f) + ';border-bottom:2px solid #e2e8f0;white-space:nowrap;color:#475569;font-weight:600;cursor:pointer'">{{ f.label }} <i class="fa-solid fa-sort"></i></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, ri) in od.rows" :key="ri" style="border-bottom:1px solid #f1f5f9">
            <td style="padding:8px;white-space:nowrap"><span class="badge" :class="platformBadge(r.platform)">{{ r.platform }}</span></td>
            <td v-for="f in tableColumns" :key="f.key" :style="cellStyle(f, r[f.key])">{{ cellPrefix(f, r[f.key]) }}{{ fmtByType(r[f.key], f.type) }}</td>
          </tr>
          <tr v-if="!od.rows.length"><td :colspan="tableColumns.length + 1" style="text-align:center;padding:40px;color:#94a3b8">暂无数据</td></tr>
        </tbody>
      </table>
    </div>
    <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-top:1px solid #e2e8f0">
      <div style="display:flex;align-items:center;gap:8px;font-size:13px;color:#64748b">
        <span>每页</span>
        <select v-model="od.pageSize" @change="onPageSizeChange" style="border:1px solid #e2e8f0;border-radius:6px;padding:4px 8px;font-size:13px">
          <option :value="20">20</option><option :value="50">50</option><option :value="100">100</option>
        </select>
        <span>条</span>
      </div>
      <div class="pagination" style="display:flex;gap:4px">
        <button :disabled="od.page <= 1" @click="goPage(od.page - 1)" style="padding:6px 12px;border:1px solid #e2e8f0;border-radius:6px;background:#fff;cursor:pointer">«</button>
        <template v-for="(it, idx) in pageList" :key="idx">
          <button v-if="it.t === 'page'" @click="goPage(it.n)" :style="'padding:6px 12px;border:1px solid ' + (it.n === od.page ? '#6366f1' : '#e2e8f0') + ';border-radius:6px;background:' + (it.n === od.page ? '#6366f1' : '#fff') + ';color:' + (it.n === od.page ? '#fff' : '#475569') + ';cursor:pointer;font-weight:' + (it.n === od.page ? '600' : '400')">{{ it.n }}</button>
          <span v-else style="padding:6px 8px;color:#94a3b8">...</span>
        </template>
        <button :disabled="od.page >= od.totalPages" @click="goPage(od.page + 1)" style="padding:6px 12px;border:1px solid #e2e8f0;border-radius:6px;background:#fff;cursor:pointer">»</button>
      </div>
    </div>
  </div>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _odApp = null;

  function mountOrderDetailsVue() {
    if (_odApp) return;
    var oldSection = document.getElementById('page-order-details');
    var mount = document.getElementById('page-order-details-vue');
    if (!oldSection || !mount) return;
    oldSection.style.display = 'none';
    mount.classList.remove('hidden');
    _odApp = Vue.createApp(OrderDetailsPage);
    _odApp.mount(mount);
  }

  function unmountOrderDetailsVue() {
    if (!_odApp) return;
    _odApp.unmount();
    _odApp = null;
    var mount = document.getElementById('page-order-details-vue');
    if (mount) { mount.classList.add('hidden'); mount.innerHTML = ''; }
    var oldSection = document.getElementById('page-order-details');
    if (oldSection) oldSection.style.display = '';
  }

  function initHook() {
    var oldSection = document.getElementById('page-order-details');
    if (!oldSection) return;
    var isVisible = !oldSection.classList.contains('hidden');
    if (isVisible) { mountOrderDetailsVue(); return; }

    var observer = new MutationObserver(function () {
      var nowVisible = !oldSection.classList.contains('hidden');
      if (nowVisible && !_odApp) mountOrderDetailsVue();
      else if (!nowVisible && _odApp) unmountOrderDetailsVue();
    });
    observer.observe(oldSection, { attributes: true, attributeFilter: ['class'] });
  }

  // 初始状态检查（处理刷新后 hash 直接落本页）
  initHook();
})();
