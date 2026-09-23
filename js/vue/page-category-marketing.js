/**
 * 品类营销数据 — Vue 版（阶段 4）
 * classic script，与 app.js 共用全局词法作用域；接口复用 ApiService.getCategoryMarketing（已封装，零补接口）
 * 结构：日期快捷（全部/昨日/近7天/近30天 + 双月日历自定义起止日期）+ 9 张统计卡片 + 品类成交 TOP10 横向柱状图（ECharts）+ 品类成交明细表格
 * XSS：所有动态数据走 Vue {{ }} 插值自动转义；关键词用 :title 原生提示
 * 图表：ECharts 实例用模块级普通变量 _catChart 保存（非响应式），unmounted 里 dispose
 * 字段映射（复刻旧 renderCategoryMarketing/_catRenderTable）：totals 用 camelCase（adGmv），categories 元素用 snake_case（ad_gmv），照旧不改
 */
(function () {
  'use strict';

  // ==================== 模块级状态（跨挂载/卸载保留，切走再回来不丢筛选） ====================
  var _cat = Vue.reactive({
    range: '7',          // 'all' | '1' | '7' | '30' | 'custom'  ← 默认「近7天」（比「全部」快约 20 倍）
    dateStart: '',
    dateEnd: '',
    dateLabel: '近7天',
    stores: [],
    storeOptions: [],
    storeFilterOpen: false,
    cal: { open: false, base: null, start: null, end: null, pickStart: true },   // 双月日历
    totals: null,        // { payment, orders, buyers, refund, productCount, refundRate, categoryCount, spend, adGmv, roi }
    categories: [],      // [{ category, keywords[], products, payment, orders, buyers, refund, refundRate, spend, ad_gmv, roi }]
  });

  // ECharts 实例：普通变量（非响应式），避免 Vue Proxy 包裹
  var _catChart = null;

  // ==================== 格式化辅助（复刻旧 renderCategoryMarketing / _catRenderTable） ====================
  function _fmtMoney(v) { return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function _fmtNum(v) { return Math.round(v).toLocaleString('zh-CN'); }
  function _fmtPct(v) { return Number(v).toFixed(2) + '%'; }
  function _fmtDate(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }
  function _parseDate(str) {
    if (!str) return null;
    var p = str.split('-');
    if (p.length !== 3) return null;
    return new Date(parseInt(p[0], 10), parseInt(p[1], 10) - 1, parseInt(p[2], 10));
  }

  function _computeRange(range, start, end) {
    if (range === 'all') return ['', ''];
    if (range === 'custom') return [start || '', end || ''];
    // ⚠️ 必须用本地时间算日期：原写法 toISOString() 取的是 **UTC 日期**，
    // 北京时间凌晨 0~8 点会整体差一天（如 09-17 03:00 算出「昨天」= 09-15）
    var now = new Date();
    var endDate = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);      // 昨天
    var startDate = new Date(endDate.getFullYear(), endDate.getMonth(),
                             endDate.getDate() - parseInt(range, 10) + 1);
    return [_fmtDate(startDate), _fmtDate(endDate)];
  }

  // ==================== 组件 ====================
  var CategoryMarketingPage = {
    setup() {
      var barChart = Vue.ref(null);
      var dateWrap = Vue.ref(null);

      function renderChart() {
        Vue.nextTick(function () {
          var el = barChart.value;
          if (!el) return;
          if (typeof echarts === 'undefined') {
            el.innerHTML = '<div style="padding:24px;text-align:center;color:#94a3b8">图表组件未加载</div>';
            return;
          }
          if (!_catChart) _catChart = echarts.init(el);
          var top = (_cat.categories || []).slice(0, 10).slice().reverse();
          _catChart.setOption({
            grid: { left: 104, right: 60, top: 10, bottom: 24 },
            tooltip: {
              trigger: 'axis', axisPointer: { type: 'shadow' },
              formatter: function (ps) { var p = ps[0]; return p.name + '<br/>成交金额：¥' + Number(p.value).toLocaleString(); }
            },
            xAxis: { type: 'value', axisLabel: { formatter: function (v) { return (v / 10000).toFixed(0) + '万'; } } },
            yAxis: { type: 'category', data: top.map(function (c) { return c.category; }), axisLabel: { fontSize: 12, color: '#475569' } },
            series: [{ type: 'bar', data: top.map(function (c) { return c.payment; }), itemStyle: { color: '#1677ff', borderRadius: [0, 4, 4, 0] }, barMaxWidth: 18 }],
          });
          setTimeout(function () { try { _catChart.resize(); } catch (e) {} }, 60);
        });
      }

      async function loadData() {
        var range = _computeRange(_cat.range, _cat.dateStart, _cat.dateEnd);
        var data = null;
        try { data = await ApiService.getCategoryMarketing(range[0], range[1], _cat.stores); } catch (e) {}
        if (data && data.totals) {
          _cat.totals = data.totals;
          _cat.categories = data.categories || [];
          _cat.storeOptions = data.stores || [];
        } else {
          _cat.totals = null;
          _cat.categories = [];
        }
        renderChart();
      }

      function toggleStoreFilter() { _cat.storeFilterOpen = !_cat.storeFilterOpen; }
      function toggleStore(store) { var i = _cat.stores.indexOf(store); if (i >= 0) _cat.stores.splice(i, 1); else _cat.stores.push(store); loadData(); }
      function clearStores() { _cat.stores.splice(0); loadData(); }
      function storeLabel() { return _cat.stores.length ? (_cat.stores.length === 1 ? _cat.stores[0] : _cat.stores.length + '家店铺') : '全部店铺'; }

      function updateDateLabel() {
        var s = _cat.dateStart, e = _cat.dateEnd;
        if (s && e) _cat.dateLabel = s + ' — ' + e;
        else if (s) _cat.dateLabel = s + ' — ';
        else if (e) _cat.dateLabel = ' — ' + e;
        else _cat.dateLabel = '选择日期';
      }

      function setRange(range) {
        _cat.range = range;
        var r = _computeRange(range, _cat.dateStart, _cat.dateEnd);
        _cat.dateStart = r[0]; _cat.dateEnd = r[1];
        updateDateLabel();
        loadData();
      }

      // ---- 双月日历 ----
      function calToggle() {
        _cat.cal.open = !_cat.cal.open;
        if (_cat.cal.open) {
          var now = new Date();
          _cat.cal.base = new Date(now.getFullYear(), now.getMonth() - 1, 1);
          _cat.cal.start = _parseDate(_cat.dateStart);
          _cat.cal.end = _parseDate(_cat.dateEnd);
          _cat.cal.pickStart = !(_cat.cal.start && _cat.cal.end);
        }
      }
      function _buildMonth(y, m, isLeft) {
        var st = _cat.cal;
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
        var st = _cat.cal;
        if (!st.base) return [];
        var leftY = st.base.getFullYear(), leftM = st.base.getMonth();
        var right = new Date(leftY, leftM + 1, 1);
        return [_buildMonth(leftY, leftM, true), _buildMonth(right.getFullYear(), right.getMonth(), false)];
      });
      function calPick(y, m, d) {
        var st = _cat.cal;
        var dt = new Date(y, m, d);
        if (st.pickStart) {
          st.start = dt; st.end = null; st.pickStart = false;
        } else {
          if (dt < st.start) { st.end = st.start; st.start = dt; }
          else { st.end = dt; }
          _cat.dateStart = _fmtDate(st.start);
          _cat.dateEnd = _fmtDate(st.end);
          _cat.range = 'custom';
          updateDateLabel();
          st.open = false;
          st.pickStart = true;
          loadData();
        }
      }
      function calNav(delta) {
        _cat.cal.base = new Date(_cat.cal.base.getFullYear(), _cat.cal.base.getMonth() + delta, 1);
      }
      function calClear() {
        _cat.dateStart = ''; _cat.dateEnd = '';
        _cat.range = 'all';
        updateDateLabel();
        _cat.cal.open = false;
        loadData();
      }
      function calToday() {
        var now = new Date();
        var dt = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        _cat.dateStart = _fmtDate(dt);
        _cat.dateEnd = _fmtDate(dt);
        _cat.range = 'custom';
        _cat.cal.start = dt; _cat.cal.end = dt;
        updateDateLabel();
        _cat.cal.open = false;
        loadData();
      }
      function onDocClick(e) {
        if (dateWrap.value && !dateWrap.value.contains(e.target)) _cat.cal.open = false;
        if (!e.target.closest('.cat-store-filter')) _cat.storeFilterOpen = false;
      }

      function keywordsText(kw) {
        return (kw && kw.length) ? kw.join('、') : '';
      }

      Vue.onMounted(function () {
        document.addEventListener('click', onDocClick);
        // 首次进入：把非自定义范围（默认近7天）展开成具体起止日期，让日期按钮显示真实区间
        // （切走再回来时会保留用户上次选的筛选，不覆盖）
        if (!_cat.dateStart && !_cat.dateEnd && _cat.range !== 'custom' && _cat.range !== 'all') {
          var r = _computeRange(_cat.range, _cat.dateStart, _cat.dateEnd);
          _cat.dateStart = r[0]; _cat.dateEnd = r[1];
          updateDateLabel();
        }
        loadData();
      });
      Vue.onUnmounted(function () {
        document.removeEventListener('click', onDocClick);
        if (_catChart) { try { _catChart.dispose(); } catch (e) {} _catChart = null; }
      });

      return {
        cat: _cat, barChart: barChart,
        fmtMoney: _fmtMoney, fmtNum: _fmtNum, fmtPct: _fmtPct,
        setRange: setRange, keywordsText: keywordsText,
        toggleStoreFilter: toggleStoreFilter, toggleStore: toggleStore, clearStores: clearStores, storeLabel: storeLabel,
        dateWrap: dateWrap, calMonths: calMonths,
        updateDateLabel: updateDateLabel,
        calToggle: calToggle, calPick: calPick, calNav: calNav, calClear: calClear, calToday: calToday,
      };
    },

    template: `
<div>
  <!-- ====== 页头 ====== -->
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon" style="color:#fff;font-size:20px"><i class="fa-solid fa-tags"></i></div>
      <div class="dh-title-group"><h2 class="dh-title">品类营销数据</h2><span class="dh-subtitle">Category Marketing Analytics</span><span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>数据实时 · 按统一品类汇总单链接成交</span></div>
    </div>
    <div class="dh-filters">
      <div class="dh-date-shortcuts">
        <button class="cat-ds-btn" :class="{ active: cat.range === 'all' }" @click="setRange('all')">全部</button>
        <button class="cat-ds-btn" :class="{ active: cat.range === '1' }" @click="setRange('1')">昨日</button>
        <button class="cat-ds-btn" :class="{ active: cat.range === '7' }" @click="setRange('7')">近7天</button>
        <button class="cat-ds-btn" :class="{ active: cat.range === '30' }" @click="setRange('30')">近30天</button>
      </div>
      <div class="cat-store-filter" style="position:relative;margin-left:4px"><button type="button" class="dh-select" style="display:flex;align-items:center;justify-content:space-between;gap:8px;min-width:118px" @click.stop="toggleStoreFilter">{{ storeLabel() }} <i class="fa-solid fa-chevron-down" style="font-size:10px;color:#94a3b8"></i></button><div v-if="cat.storeFilterOpen" style="position:absolute;right:0;top:38px;z-index:80;min-width:190px;max-height:260px;overflow:auto;padding:8px;background:#fff;border:1px solid #e2e8f0;border-radius:9px;box-shadow:0 10px 28px rgba(15,23,42,.14)"><button type="button" @click="clearStores" style="display:block;width:100%;border:0;border-bottom:1px solid #f1f5f9;background:transparent;text-align:left;padding:6px 8px;margin-bottom:3px;color:#2563eb;font-size:12px;cursor:pointer">全部店铺</button><label v-for="s in cat.storeOptions" :key="s" style="display:block;padding:7px 8px;font-size:12px;color:#334155;white-space:nowrap;cursor:pointer"><input type="checkbox" :checked="cat.stores.indexOf(s)>=0" @change="toggleStore(s)"> {{s}}</label><div v-if="!cat.storeOptions.length" style="padding:8px;color:#94a3b8;font-size:12px">暂无店铺数据</div></div></div>
      <div class="ps-date-picker" style="position:relative;margin-left:10px" ref="dateWrap">
        <button type="button" @click="calToggle" style="display:flex;align-items:center;gap:6px;background:#fff;border:1px solid #e2e8f0;border-radius:9px;padding:5px 12px;height:33px;cursor:pointer;font-size:0.82rem;color:#334155;font-family:inherit">
          <i class="fa-regular fa-calendar" style="color:#94a3b8;font-size:0.82rem"></i>
          <span style="white-space:nowrap">{{ cat.dateLabel }}</span>
          <i class="fa-solid fa-chevron-down" style="color:#94a3b8;font-size:0.7rem"></i>
        </button>
        <div v-show="cat.cal.open" style="position:absolute;top:40px;right:0;z-index:60;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.15);padding:14px;user-select:none">
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
            <span style="font-size:12px;color:#64748b">{{ cat.cal.pickStart ? '请选择开始日期' : '请选择结束日期' }}</span>
            <div style="display:flex;gap:10px">
              <button @click="calClear" style="border:none;background:none;color:#94a3b8;font-size:12px;cursor:pointer">清除</button>
              <button @click="calToday" style="border:none;background:none;color:#6366f1;font-size:12px;cursor:pointer;font-weight:600">今天</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ====== 6 张核心卡片 ====== -->
  <div class="mkt-cards-grid" style="display:grid;grid-template-columns:repeat(6,1fr);gap:14px">
    <div class="mkt-card mkt-card--emerald"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-yen-sign"></i></div><span class="mkt-card-label">成交金额</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cat.totals ? fmtMoney(cat.totals.payment) : '--' }}</div><div class="mkt-card-trend neutral"></div></div></div>
    <div class="mkt-card mkt-card--indigo"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-receipt"></i></div><span class="mkt-card-label">订单数</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cat.totals ? fmtNum(cat.totals.orders) : '--' }}</div><div class="mkt-card-trend neutral"></div></div></div>
    <div class="mkt-card mkt-card--green"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-users"></i></div><span class="mkt-card-label">买家数</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cat.totals ? fmtNum(cat.totals.buyers) : '--' }}</div><div class="mkt-card-trend neutral"></div></div></div>
    <div class="mkt-card mkt-card--rose"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-rotate-left"></i></div><span class="mkt-card-label">退款金额</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cat.totals ? fmtMoney(cat.totals.refund) : '--' }}</div><div class="mkt-card-trend neutral"></div></div></div>
    <div class="mkt-card mkt-card--violet"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-box"></i></div><span class="mkt-card-label">商品数</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cat.totals ? fmtNum(cat.totals.productCount) : '--' }}</div><div class="mkt-card-trend neutral"></div></div></div>
    <div class="mkt-card mkt-card--amber"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-percent"></i></div><span class="mkt-card-label">金额退款率</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cat.totals ? fmtPct(cat.totals.refundRate) : '--' }}</div><div class="mkt-card-trend neutral"></div></div></div>
  </div>

  <!-- ====== 3 张推广卡片 ====== -->
  <div class="mkt-cards-grid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:14px">
    <div class="mkt-card mkt-card--coral"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-coins"></i></div><span class="mkt-card-label">消耗花费</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cat.totals ? fmtMoney(cat.totals.spend) : '--' }}</div><div class="mkt-card-trend neutral"></div></div></div>
    <div class="mkt-card mkt-card--cyan"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-chart-simple"></i></div><span class="mkt-card-label">推广产出</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cat.totals ? fmtMoney(cat.totals.adGmv) : '--' }}</div><div class="mkt-card-trend neutral"></div></div></div>
    <div class="mkt-card mkt-card--emerald"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-gauge-high"></i></div><span class="mkt-card-label">推广ROI</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cat.totals ? (cat.totals.roi || 0).toFixed(2) : '--' }}</div><div class="mkt-card-trend neutral"></div></div></div>
  </div>

  <!-- ====== 图表 + 明细表格 ====== -->
  <div class="charts-row" style="display:grid;grid-template-columns:1fr 1.7fr;gap:18px;margin-top:20px">
    <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">品类成交金额 TOP10</span><span style="font-size:0.75rem;color:#94a3b8;font-weight:400">各品类成交金额分布</span></div>
      <div ref="barChart" style="min-height:440px"></div>
    </div>
    <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">品类成交明细</span><span style="font-size:0.75rem;color:#94a3b8;font-weight:400">按成交金额降序 · 共 <span>{{ cat.totals ? cat.totals.categoryCount : '--' }}</span> 个品类</span></div>
      <div style="overflow:auto;max-height:440px">
        <table style="width:100%;border-collapse:collapse;font-size:0.85rem">
          <thead>
            <tr style="background:#f8fafc;color:#64748b;text-align:left">
              <th style="padding:10px 12px">#</th>
              <th style="padding:10px 12px">品类</th>
              <th style="padding:10px 12px;text-align:right">商品数</th>
              <th style="padding:10px 12px;text-align:right">成交金额</th>
              <th style="padding:10px 12px;text-align:right">订单数</th>
              <th style="padding:10px 12px;text-align:right">买家数</th>
              <th style="padding:10px 12px;text-align:right">退款金额</th>
              <th style="padding:10px 12px;text-align:right">退款率</th>
              <th style="padding:10px 12px;text-align:right">消耗花费</th>
              <th style="padding:10px 12px;text-align:right">推广产出</th>
              <th style="padding:10px 12px;text-align:right">推广ROI</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(c, i) in cat.categories" :key="c.category" style="border-bottom:1px solid #f1f5f9">
              <td style="padding:9px 12px;color:#94a3b8">{{ i + 1 }}</td>
              <td style="padding:9px 12px;font-weight:500;color:#1e293b">{{ c.category }}<span v-if="keywordsText(c.keywords)" :title="keywordsText(c.keywords)" style="display:inline-block;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom;color:#94a3b8;font-size:0.78rem;cursor:help">{{ keywordsText(c.keywords) }}</span></td>
              <td style="padding:9px 12px;text-align:right;color:#475569">{{ fmtNum(c.products) }}</td>
              <td style="padding:9px 12px;text-align:right;font-weight:600;color:#2563eb">{{ fmtMoney(c.payment) }}</td>
              <td style="padding:9px 12px;text-align:right;color:#475569">{{ fmtNum(c.orders) }}</td>
              <td style="padding:9px 12px;text-align:right;color:#475569">{{ fmtNum(c.buyers) }}</td>
              <td style="padding:9px 12px;text-align:right;color:#dc2626">{{ fmtMoney(c.refund) }}</td>
              <td style="padding:9px 12px;text-align:right;color:#475569">{{ fmtPct(c.refundRate) }}</td>
              <td style="padding:9px 12px;text-align:right;color:#475569">{{ fmtMoney(c.spend) }}</td>
              <td style="padding:9px 12px;text-align:right;font-weight:600;color:#16a34a">{{ fmtMoney(c.ad_gmv) }}</td>
              <td style="padding:9px 12px;text-align:right;color:#475569">{{ (c.roi || 0).toFixed(2) }}</td>
            </tr>
            <tr v-if="!cat.categories.length"><td colspan="11" style="text-align:center;color:#94a3b8;padding:24px">暂无数据</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _catApp = null;

  function mountCategoryVue() {
    if (_catApp) return;
    var oldSection = document.getElementById('page-category-marketing');
    var mount = document.getElementById('page-category-marketing-vue');
    if (!oldSection || !mount) return;
    oldSection.style.display = 'none';
    mount.classList.remove('hidden');
    _catApp = Vue.createApp(CategoryMarketingPage);
    _catApp.mount(mount);
  }

  function unmountCategoryVue() {
    if (!_catApp) return;
    _catApp.unmount();
    _catApp = null;
    var mount = document.getElementById('page-category-marketing-vue');
    if (mount) { mount.classList.add('hidden'); mount.innerHTML = ''; }
    var oldSection = document.getElementById('page-category-marketing');
    if (oldSection) oldSection.style.display = '';
  }

  function initHook() {
    var oldSection = document.getElementById('page-category-marketing');
    if (!oldSection) return;
    var isVisible = !oldSection.classList.contains('hidden');
    if (isVisible) { mountCategoryVue(); return; }
    var observer = new MutationObserver(function () {
      var nowVisible = !oldSection.classList.contains('hidden');
      if (nowVisible && !_catApp) mountCategoryVue();
      else if (!nowVisible && _catApp) unmountCategoryVue();
    });
    observer.observe(oldSection, { attributes: true, attributeFilter: ['class'] });
  }

  initHook();
})();
