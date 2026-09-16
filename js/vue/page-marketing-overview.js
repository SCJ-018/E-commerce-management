/**
 * 整体营销数据总览（首页）— Vue 版（阶段 4）
 * classic script，与 app.js 共用全局词法作用域；接口复用 ApiService（getMarketingOverview / getMarketingFilters，均已封装，零补接口）
 * 结构：页头(总营业额 chip + 日期快捷 + 平台/品牌下拉 + 双月日历) + 8 张指标卡片(带环比涨跌) + 5 个 ECharts 图表模块
 * XSS：所有动态数据走 Vue {{ }} 插值自动转义；趋势箭头/涨跌文字为纯文本（含 ↑↓ 符号），不用 v-html
 * 图表：ECharts 实例用模块级普通对象 _moCharts 保存（非响应式），onUnmounted 里 dispose 全部
 * 字段（复刻旧 renderMarketingOverview）：metrics 顶层 netPayment/payment/refundAmount/refundRate/adSpend/adTotal/roi/visitors/payers + trends[] + comparison + agg；trends 元素含 date/netPayment/refundAmount/refundRate/adSpend/roi/visitors/payers/payment/cart/convRate/orderRefundRate/aov/adTotal
 * 涨跌配色照旧（营销看板，非股票）：涨=绿(#059669 .up)、跌=红(#e11d48 .down)；退款/花费类 inverted 反转
 */
(function () {
  'use strict';

  // ==================== 模块级状态（跨挂载/卸载保留，切走再回来不丢筛选） ====================
  var _mo = Vue.reactive({
    dateStart: '',
    dateEnd: '',
    dateLabel: '选择日期',
    platform: '',
    brand: '',
    platformOptions: [],
    brandOptions: [],
    metrics: null,        // 完整返回对象（netPayment/payment/refundAmount/.../trends/comparison/agg）
    loaded: false,        // 数据是否加载成功（对应旧 state.apiAvailable）
    updateTime: '更新于 --',
    cal: { open: false, base: null, start: null, end: null, pickStart: true },   // 双月日历
  });

  // ECharts 实例容器：普通对象（非响应式），避免 Vue Proxy 包裹
  var _moCharts = {};

  // ==================== 格式化辅助（复刻旧 populateMarketingCards / 图表区） ====================
  function _fmtMoney(v) { return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function _fmtNum(v) { return Math.round(v).toLocaleString('zh-CN'); }
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

  // ==================== 组件 ====================
  var MarketingOverviewPage = {
    setup() {
      // ---- refs（图表容器） ----
      var trendChartEl = Vue.ref(null);
      var funnelChartEl = Vue.ref(null);
      var adChartEl = Vue.ref(null);
      var gaugeAmountEl = Vue.ref(null);
      var gaugeOrderEl = Vue.ref(null);
      var refundMiniBarEl = Vue.ref(null);
      var miniAOVEl = Vue.ref(null);
      var miniConvEl = Vue.ref(null);
      var dateWrap = Vue.ref(null);

      // ---- 计算属性 ----
      var m = Vue.computed(function () {
        if (_mo.metrics) return _mo.metrics;
        return { netPayment: 0, payment: 0, refundAmount: 0, refundRate: 0, adSpend: 0, adTotal: 0, roi: 0, visitors: 0, payers: 0, trends: null, comparison: null, agg: null };
      });
      var hasTrends = Vue.computed(function () {
        return !!(m.value.trends && m.value.trends.length);
      });
      var refundWarn = Vue.computed(function () {
        var amountRR = m.value.refundRate || 0;
        var orderRR = (m.value.agg && m.value.agg.avgOrderRefundRate) || 0;
        return amountRR > 30 || orderRR > 30;
      });
      var latestTrend = Vue.computed(function () {
        var t = m.value.trends;
        return (t && t.length) ? t[t.length - 1] : {};
      });

      // ---- 卡片涨跌（复刻 trendIcon / setTrend） ----
      function trendInfo(key, inverted, isDelta) {
        var c = m.value.comparison;
        if (!c) return { text: '', cls: 'neutral' };
        var pct = c[key];
        if (pct === null || pct === undefined) return { text: '', cls: 'neutral' };
        var abs = Math.abs(pct);
        var up = pct > (isDelta ? 0.005 : 0);
        var down = pct < (isDelta ? -0.005 : 0);
        if (!up && !down) return { text: '持平', cls: 'neutral' };
        var suffix = isDelta ? abs.toFixed(2) + 'pp' : abs + '%';
        var cls, arrow;
        if (inverted) { cls = up ? 'down' : 'up'; arrow = up ? '↑ +' : '↓ '; }
        else { cls = up ? 'up' : 'down'; arrow = up ? '↑ +' : '↓ '; }
        var label = c.isSingleDay ? ' 环比昨日' : ' 环比上期';
        return { text: arrow + suffix + label, cls: cls };
      }

      // ---- 图表实例管理 ----
      function _echartsReady() { return typeof echarts !== 'undefined' && echarts.init; }
      function _getChart(key, dom) {
        if (!_echartsReady() || !dom) return null;
        if (_moCharts[key]) { try { _moCharts[key].dispose(); } catch (e) {} delete _moCharts[key]; }
        var c = echarts.init(dom);
        _moCharts[key] = c;
        return c;
      }
      function disposeAllCharts() {
        Object.keys(_moCharts).forEach(function (k) {
          try { _moCharts[k].dispose(); } catch (e) {}
          delete _moCharts[k];
        });
      }

      // ---- 5 个图表（复刻 chartTrend/chartFunnel/chartRefund/chartAd/chartAOV） ----
      function renderTrend() {
        var c = _getChart('trend', trendChartEl.value);
        if (!c) return;
        var trends = m.value.trends || [];
        var dates = trends.map(function (t) { return t.date; });
        c.setOption({
          tooltip: {
            trigger: 'axis',
            formatter: function (ps) { return ps.map(function (p) { return p.marker + p.seriesName + ': ' + (p.seriesName.indexOf('率') >= 0 ? p.value + '%' : '¥' + p.value.toLocaleString()); }).join('<br/>'); }
          },
          legend: { top: 0, right: 0, textStyle: { fontSize: 11 } },
          grid: { left: 50, right: 55, top: 40, bottom: 30 },
          xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
          yAxis: [
            { type: 'value', name: '金额(元)', nameTextStyle: { fontSize: 10 }, axisLabel: { fontSize: 10, formatter: function (v) { return (v / 10000).toFixed(0) + 'w'; } } },
            { type: 'value', name: '%', nameTextStyle: { fontSize: 10 }, axisLabel: { fontSize: 10, formatter: function (v) { return v + '%'; } } }
          ],
          series: [
            { name: '支付金额', type: 'line', smooth: true, data: trends.map(function (t) { return t.payment; }), lineStyle: { color: '#3B82F6' }, itemStyle: { color: '#3B82F6' }, areaStyle: { color: 'rgba(59,130,246,0.06)' }, symbol: 'none' },
            { name: '净支付金额', type: 'line', smooth: true, data: trends.map(function (t) { return t.netPayment; }), lineStyle: { color: '#10B981' }, itemStyle: { color: '#10B981' }, areaStyle: { color: 'rgba(16,185,129,0.06)' }, symbol: 'none' },
            { name: '退款金额', type: 'line', smooth: true, data: trends.map(function (t) { return t.refundAmount; }), lineStyle: { color: '#EF4444' }, itemStyle: { color: '#EF4444' }, areaStyle: { color: 'rgba(239,68,68,0.04)' }, symbol: 'none' },
            { name: '推广花费金额', type: 'line', smooth: true, data: trends.map(function (t) { return t.adSpend; }), lineStyle: { color: '#8B5CF6' }, itemStyle: { color: '#8B5CF6' }, areaStyle: { color: 'rgba(139,92,246,0.04)' }, symbol: 'none' },
            { name: '订单退款率', type: 'line', smooth: true, yAxisIndex: 1, data: trends.map(function (t) { return t.orderRefundRate; }), lineStyle: { color: '#F97316', type: 'dashed' }, itemStyle: { color: '#F97316' }, symbol: 'none' },
          ]
        });
      }
      function renderFunnel() {
        var c = _getChart('funnel', funnelChartEl.value);
        if (!c) return;
        var agg = m.value.agg || {};
        var vis = agg.totalVisitors || 0;
        var cart = agg.totalCart || 0;
        var payers = agg.totalPayers || 0;
        var rate = agg.avgConvRate || 0;
        var cartRate = vis > 0 ? (cart / vis * 100).toFixed(1) : '0';
        var payRate = cart > 0 ? (payers / cart * 100).toFixed(1) : '0';
        c.setOption({
          tooltip: { trigger: 'item', formatter: '{b}: {c}' },
          series: [{
            type: 'funnel', left: '15%', right: '15%', top: 10, bottom: 10,
            minSize: '25%', maxSize: '100%', gap: 4,
            label: { show: true, position: 'inside', fontSize: 11, formatter: '{b}' },
            labelLine: { show: false },
            data: [
              { value: vis, name: '访客数', itemStyle: { color: '#3B82F6' } },
              { value: cart, name: '加购人数 (' + cartRate + '%)', itemStyle: { color: '#6366F1' } },
              { value: payers, name: '支付买家数 (' + payRate + '%)', itemStyle: { color: '#10B981' } },
              { value: Math.round(rate * 10) / 10, name: '支付转化率 ' + rate + '%', itemStyle: { color: '#F59E0B' } },
            ]
          }]
        });
      }
      function renderRefund() {
        var amountRR = m.value.refundRate || 0;
        var orderRR = (m.value.agg && m.value.agg.avgOrderRefundRate) || 0;
        function gauge(dom, val, color) {
          var gc = _getChart(dom === 'gaugeAmount' ? 'gaugeAmount' : 'gaugeOrder', dom === 'gaugeAmount' ? gaugeAmountEl.value : gaugeOrderEl.value);
          if (!gc) return;
          gc.setOption({ series: [{ type: 'gauge', radius: '85%', center: ['50%', '55%'], startAngle: 200, endAngle: -20, min: 0, max: 100, axisLine: { show: true, lineStyle: { width: 8, color: [[val / 100, color], [1, '#f1f5f9']] } }, pointer: { show: false }, axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false }, detail: { fontSize: 16, fontWeight: 700, color: color, offsetCenter: [0, '80%'], formatter: val.toFixed(1) + '%' }, data: [{ value: val }] }] });
        }
        gauge('gaugeAmount', Math.min(amountRR, 100), amountRR > 30 ? '#e11d48' : '#f59e0b');
        gauge('gaugeOrder', Math.min(orderRR, 100), orderRR > 30 ? '#e11d48' : '#f59e0b');
        if (m.value.trends && m.value.trends.length) {
          var bc = _getChart('refundMiniBar', refundMiniBarEl.value);
          if (bc) {
            bc.setOption({ grid: { left: 0, right: 0, top: 0, bottom: 16 }, xAxis: { type: 'category', data: m.value.trends.map(function (t) { return t.date.slice(5); }), axisLabel: { fontSize: 8 }, axisLine: { show: false }, axisTick: { show: false } }, yAxis: { type: 'value', splitLine: { show: false }, axisLabel: { show: false } }, series: [{ type: 'bar', data: m.value.trends.map(function (t) { return t.refundAmount; }), itemStyle: { color: '#fca5a5', borderRadius: [3, 3, 0, 0] }, barWidth: '60%' }] });
          }
        }
      }
      function renderAd() {
        var c = _getChart('ad', adChartEl.value);
        if (!c) return;
        var trends = m.value.trends || [];
        var dates = trends.map(function (t) { return t.date; });
        c.setOption({
          tooltip: { trigger: 'axis' },
          legend: { top: 0, right: 0, textStyle: { fontSize: 10 } },
          grid: { left: 50, right: 50, top: 35, bottom: 30 },
          xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
          yAxis: [
            { type: 'value', name: '金额', axisLabel: { fontSize: 9, formatter: function (v) { return (v / 10000).toFixed(0) + 'w'; } } },
            { type: 'value', name: 'ROI', axisLabel: { fontSize: 9 } }
          ],
          series: [
            { name: '推广总成交', type: 'bar', data: trends.map(function (t) { return t.adTotal; }), itemStyle: { color: '#1E40AF' }, barGap: '10%', barWidth: '35%' },
            { name: '推广花费金额', type: 'bar', data: trends.map(function (t) { return t.adSpend; }), itemStyle: { color: '#A78BFA' }, barWidth: '35%' },
            { name: 'ROI', type: 'line', yAxisIndex: 1, data: trends.map(function (t) { return t.roi; }), lineStyle: { color: '#F97316' }, itemStyle: { color: '#F97316' }, symbol: 'circle', symbolSize: 4 },
          ]
        });
      }
      function renderAOV() {
        var trends = m.value.trends || [];
        var dates = trends.map(function (t) { return t.date; });
        function miniLine(key, dom, data, color) {
          var mc = _getChart(key, dom);
          if (!mc) return;
          mc.setOption({
            grid: { left: 0, right: 0, top: 4, bottom: 6 },
            xAxis: { type: 'category', data: dates, show: false },
            yAxis: { type: 'value', show: false, min: function (v) { return v.min - (v.max - v.min) * 0.2; } },
            series: [{
              type: 'line', data: data, smooth: true,
              lineStyle: { color: color, width: 1.5 }, itemStyle: { color: color },
              symbol: 'none', areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: color + '30' }, { offset: 1, color: color + '02' }]) }
            }]
          });
        }
        miniLine('miniAOV', miniAOVEl.value, trends.map(function (t) { return t.aov; }), '#3B82F6');
        miniLine('miniConv', miniConvEl.value, trends.map(function (t) { return t.convRate; }), '#10B981');
      }
      function renderAllCharts() {
        Vue.nextTick(function () {
          if (!hasTrends.value) { disposeAllCharts(); return; }
          renderTrend();
          renderFunnel();
          renderRefund();
          renderAd();
          renderAOV();
        });
      }

      // ---- 数据加载 ----
      function updateTime() {
        var now = new Date();
        var label = '更新于 ' + now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        if (_mo.loaded) {
          var s = _mo.dateStart, e = _mo.dateEnd;
          if (s && e && s === e) label += ' · ' + s;
          else if (s || e) label += ' · ' + (s || '...') + ' ~ ' + (e || '...');
          label += ' · 数据库实时';
        }
        _mo.updateTime = label;
      }
      async function loadData() {
        var data = null;
        try { data = await ApiService.getMarketingOverview(_mo.dateStart, _mo.dateEnd, _mo.platform, _mo.brand); } catch (e) {}
        if (data) {
          if (!data.refundRate && data.netPayment > 0) data.refundRate = (data.refundAmount || 0) / data.netPayment * 100;
          if (!data.roi && data.adSpend > 0) data.roi = (data.adTotal || 0) / data.adSpend;
          _mo.metrics = data;
          _mo.loaded = true;
          renderAllCharts();
        } else {
          _mo.metrics = null;
          _mo.loaded = false;
          disposeAllCharts();
        }
        updateTime();
      }

      function init() {
        if (!_mo.dateStart && !_mo.dateEnd) {
          var y = _yesterdayISO();
          _mo.dateStart = y; _mo.dateEnd = y;
          updateDateLabel();
        }
        if (!_mo.platformOptions.length) {
          ApiService.getMarketingFilters().then(function (filters) {
            if (filters) {
              if (filters.platforms) _mo.platformOptions = filters.platforms;
              if (filters.brands) _mo.brandOptions = filters.brands;
            }
          });
        }
        loadData();
      }

      // ---- 日期快捷 ----
      function updateDateLabel() {
        var s = _mo.dateStart, e = _mo.dateEnd;
        if (s && e) _mo.dateLabel = s + ' — ' + e;
        else if (s) _mo.dateLabel = s + ' — ';
        else if (e) _mo.dateLabel = ' — ' + e;
        else _mo.dateLabel = '选择日期';
      }
      function setDateRange(days) {
        var end = new Date(Date.now() - 86400000);
        var start = new Date(end);
        start.setDate(start.getDate() - days + 1);
        _mo.dateStart = start.toISOString().slice(0, 10);
        _mo.dateEnd = end.toISOString().slice(0, 10);
        updateDateLabel();
        loadData();
      }
      function isShortcutActive(days) {
        var yesterday = _yesterdayISO();
        if (days === 1) return _mo.dateStart === yesterday && _mo.dateEnd === yesterday;
        var calcStart = new Date(Date.now() - 86400000 - (days - 1) * 86400000).toISOString().slice(0, 10);
        return _mo.dateStart === calcStart && _mo.dateEnd === yesterday;
      }

      // ---- 平台/品牌切换 ----
      function onPlatformChange() { loadData(); }
      function onBrandChange() { loadData(); }

      // ---- 双月日历 ----
      function calToggle() {
        _mo.cal.open = !_mo.cal.open;
        if (_mo.cal.open) {
          var now = new Date();
          _mo.cal.base = new Date(now.getFullYear(), now.getMonth() - 1, 1);
          _mo.cal.start = _parseDate(_mo.dateStart);
          _mo.cal.end = _parseDate(_mo.dateEnd);
          _mo.cal.pickStart = !(_mo.cal.start && _mo.cal.end);
        }
      }
      function _buildMonth(y, mm, isLeft) {
        var st = _mo.cal;
        var first = new Date(y, mm, 1);
        var days = new Date(y, mm + 1, 0).getDate();
        var startCol = (first.getDay() + 6) % 7;
        var now = new Date();
        var cells = [];
        for (var i = 0; i < startCol; i++) cells.push({ blank: true });
        for (var d = 1; d <= days; d++) {
          var dt = new Date(y, mm, d);
          var cls = '';
          if (st.start && dt.getTime() === st.start.getTime()) cls += ' is-start';
          if (st.end && dt.getTime() === st.end.getTime()) cls += ' is-end';
          if (st.start && st.end && dt > st.start && dt < st.end) cls += ' in-range';
          if (dt.getFullYear() === now.getFullYear() && dt.getMonth() === now.getMonth() && dt.getDate() === now.getDate()) cls += ' is-today';
          cells.push({ blank: false, d: d, y: y, m: mm, cls: cls });
        }
        return { y: y, m: mm, isLeft: isLeft, cells: cells };
      }
      var calMonths = Vue.computed(function () {
        var st = _mo.cal;
        if (!st.base) return [];
        var leftY = st.base.getFullYear(), leftM = st.base.getMonth();
        var right = new Date(leftY, leftM + 1, 1);
        return [_buildMonth(leftY, leftM, true), _buildMonth(right.getFullYear(), right.getMonth(), false)];
      });
      function calPick(y, mm, d) {
        var st = _mo.cal;
        var dt = new Date(y, mm, d);
        if (st.pickStart) {
          st.start = dt; st.end = null; st.pickStart = false;
        } else {
          if (dt < st.start) { st.end = st.start; st.start = dt; }
          else { st.end = dt; }
          _mo.dateStart = _fmtDate(st.start);
          _mo.dateEnd = _fmtDate(st.end);
          updateDateLabel();
          st.open = false;
          st.pickStart = true;
          loadData();
        }
      }
      function calNav(delta) {
        _mo.cal.base = new Date(_mo.cal.base.getFullYear(), _mo.cal.base.getMonth() + delta, 1);
      }
      function calClear() {
        _mo.dateStart = ''; _mo.dateEnd = '';
        updateDateLabel();
        _mo.cal.open = false;
        loadData();
      }
      function calToday() {
        var now = new Date();
        var dt = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        _mo.dateStart = _fmtDate(dt);
        _mo.dateEnd = _fmtDate(dt);
        _mo.cal.start = dt; _mo.cal.end = dt;
        updateDateLabel();
        _mo.cal.open = false;
        loadData();
      }
      function onDocClick(e) {
        if (dateWrap.value && !dateWrap.value.contains(e.target)) _mo.cal.open = false;
      }

      Vue.onMounted(function () {
        document.addEventListener('click', onDocClick);
        init();
      });
      Vue.onUnmounted(function () {
        document.removeEventListener('click', onDocClick);
        disposeAllCharts();
      });

      return {
        mo: _mo, m: m, hasTrends: hasTrends, refundWarn: refundWarn, latestTrend: latestTrend,
        fmtMoney: _fmtMoney, fmtNum: _fmtNum,
        trendInfo: trendInfo,
        calMonths: calMonths, dateWrap: dateWrap,
        setDateRange: setDateRange, isShortcutActive: isShortcutActive,
        onPlatformChange: onPlatformChange, onBrandChange: onBrandChange,
        calToggle: calToggle, calPick: calPick, calNav: calNav, calClear: calClear, calToday: calToday,
        trendChartEl: trendChartEl, funnelChartEl: funnelChartEl, adChartEl: adChartEl,
        gaugeAmountEl: gaugeAmountEl, gaugeOrderEl: gaugeOrderEl, refundMiniBarEl: refundMiniBarEl,
        miniAOVEl: miniAOVEl, miniConvEl: miniConvEl,
      };
    },

    template: `
<div>
  <!-- ====== 页头 ====== -->
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 17 9 10 13 14 21 6"/><polyline points="17 6 21 6 21 10"/></svg></div>
      <div class="dh-title-group"><h2 class="dh-title">整体营销数据总览</h2><span class="dh-subtitle">Overall Marketing Analytics</span><span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>数据实时 · <span>{{ mo.updateTime }}</span></span></div>
      <div class="dh-chip"><span class="dh-chip-label">总营业额</span><span class="dh-chip-value">{{ fmtMoney(m.payment) }}</span></div>
    </div>
    <div class="dh-filters">
      <div class="dh-date-shortcuts">
        <button class="dh-ds-btn" :class="{ active: isShortcutActive(1) }" @click="setDateRange(1)">昨日</button>
        <button class="dh-ds-btn" :class="{ active: isShortcutActive(7) }" @click="setDateRange(7)">近7天</button>
        <button class="dh-ds-btn" :class="{ active: isShortcutActive(30) }" @click="setDateRange(30)">近30天</button>
      </div>
      <select v-model="mo.platform" class="dh-select" @change="onPlatformChange">
        <option value="">全部平台</option>
        <option v-for="p in mo.platformOptions" :key="p" :value="p">{{ p }}</option>
      </select>
      <select v-model="mo.brand" class="dh-select" @change="onBrandChange">
        <option value="">全部品牌</option>
        <option v-for="b in mo.brandOptions" :key="b" :value="b">{{ b }}</option>
      </select>
      <div class="dh-date-picker" style="position:relative" ref="dateWrap">
        <button type="button" @click="calToggle" style="display:flex;align-items:center;gap:6px;background:#fff;border:1px solid #e2e8f0;border-radius:9px;padding:5px 12px;height:33px;cursor:pointer;font-size:0.82rem;color:#334155;font-family:inherit">
          <i class="fa-regular fa-calendar" style="color:#94a3b8;font-size:0.82rem"></i>
          <span style="white-space:nowrap">{{ mo.dateLabel }}</span>
          <i class="fa-solid fa-chevron-down" style="color:#94a3b8;font-size:0.7rem"></i>
        </button>
        <div v-show="mo.cal.open" style="position:absolute;top:40px;right:0;z-index:60;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.15);padding:14px;user-select:none">
          <div class="od-cal-wrap">
            <div class="od-cal" v-for="(mo2, mi) in calMonths" :key="mi">
              <div class="od-cal-head">
                <button v-if="mo2.isLeft" type="button" class="od-cal-nav" @click="calNav(-1)">‹</button>
                <span v-else style="width:24px"></span>
                <span>{{ mo2.y }}年{{ mo2.m + 1 }}月</span>
                <button v-if="!mo2.isLeft" type="button" class="od-cal-nav" @click="calNav(1)">›</button>
                <span v-else style="width:24px"></span>
              </div>
              <div class="od-cal-week"><span v-for="w in ['一','二','三','四','五','六','日']" :key="w">{{ w }}</span></div>
              <div class="od-cal-days">
                <template v-for="(c, ci) in mo2.cells" :key="ci">
                  <span v-if="c.blank" class="od-cal-day blank"></span>
                  <button v-else type="button" class="od-cal-day" :class="c.cls" @click="calPick(c.y, c.m, c.d)">{{ c.d }}</button>
                </template>
              </div>
            </div>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-top:10px;padding-top:10px;border-top:1px solid #f1f5f9">
            <span style="font-size:12px;color:#64748b">{{ mo.cal.pickStart ? '请选择开始日期' : '请选择结束日期' }}</span>
            <div style="display:flex;gap:10px">
              <button @click="calClear" style="border:none;background:none;color:#94a3b8;font-size:12px;cursor:pointer">清除</button>
              <button @click="calToday" style="border:none;background:none;color:#6366f1;font-size:12px;cursor:pointer;font-weight:600">今天</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ====== 8 张指标卡片 ====== -->
  <div class="mkt-cards-grid">
    <div class="mkt-card mkt-card--emerald"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-yen-sign"></i></div><span class="mkt-card-label">净支付金额</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ fmtMoney(m.netPayment) }}</div><div class="mkt-card-trend" :class="trendInfo('netPayment', false, false).cls">{{ trendInfo('netPayment', false, false).text }}</div></div></div>
    <div class="mkt-card mkt-card--rose"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-rotate-left"></i></div><span class="mkt-card-label">退款金额</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ fmtMoney(m.refundAmount) }}</div><div class="mkt-card-trend" :class="trendInfo('refundAmount', true, false).cls">{{ trendInfo('refundAmount', true, false).text }}</div></div></div>
    <div class="mkt-card mkt-card--amber"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-percent"></i></div><span class="mkt-card-label">金额退款率</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ (m.refundRate || 0).toFixed(2) }}%</div><div class="mkt-card-trend" :class="trendInfo('refundRate', true, true).cls">{{ trendInfo('refundRate', true, true).text }}</div></div></div>
    <div class="mkt-card mkt-card--indigo"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-users"></i></div><span class="mkt-card-label">访客数</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ fmtNum(m.visitors) }}</div><div class="mkt-card-trend" :class="trendInfo('visitors', false, false).cls">{{ trendInfo('visitors', false, false).text }}</div></div></div>
    <div class="mkt-card mkt-card--green"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-chart-line"></i></div><span class="mkt-card-label">推广总成交金额</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ fmtMoney(m.adTotal) }}</div><div class="mkt-card-trend" :class="trendInfo('adTotal', false, false).cls">{{ trendInfo('adTotal', false, false).text }}</div></div></div>
    <div class="mkt-card mkt-card--coral"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-fire"></i></div><span class="mkt-card-label">推广花费金额</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ fmtMoney(m.adSpend) }}</div><div class="mkt-card-trend" :class="trendInfo('adSpend', true, false).cls">{{ trendInfo('adSpend', true, false).text }}</div></div></div>
    <div class="mkt-card mkt-card--cyan"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-gem"></i></div><span class="mkt-card-label">当日ROI</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ (m.roi || 0).toFixed(4) }}</div><div class="mkt-card-trend" :class="trendInfo('roi', false, true).cls">{{ trendInfo('roi', false, true).text }}</div></div></div>
    <div class="mkt-card mkt-card--violet"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-user-check"></i></div><span class="mkt-card-label">支付买家数</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ fmtNum(m.payers) }}</div><div class="mkt-card-trend" :class="trendInfo('payers', false, false).cls">{{ trendInfo('payers', false, false).text }}</div></div></div>
  </div>

  <!-- ====== 图表分析区 ====== -->
  <div class="charts-section" style="margin-top:20px">
    <div class="charts-row" style="display:grid;grid-template-columns:2fr 1fr;gap:18px;margin-bottom:18px">
      <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">核心指标趋势图</span><span style="font-size:0.75rem;color:#94a3b8;margin-left:8px;font-weight:400">净支付、退款、访客等核心指标的日度趋势，支持多指标对比</span></div><div class="chart-module-body" ref="trendChartEl" style="min-height:280px"></div></div>
      <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">转化漏斗</span><span style="font-size:0.75rem;color:#94a3b8;margin-left:8px;font-weight:400">访客→买家→成交的逐层转化，定位流失环节</span></div><div class="chart-module-body" ref="funnelChartEl" style="min-height:200px"></div></div>
    </div>
    <div class="charts-row" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:18px;margin-bottom:18px">
      <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">退款分析</span><span style="font-size:0.75rem;color:#94a3b8;margin-left:8px;font-weight:400">各平台退款金额及退款率分布，快速识别异常</span></div><div class="chart-module-body" style="min-height:200px"><template v-if="hasTrends"><div class="refund-big-num">{{ fmtMoney(m.refundAmount) }}</div><div class="refund-big-trend"><span v-if="refundWarn" style="color:#e11d48">⚠ 退款率偏高</span></div><div class="refund-gauges"><div class="refund-gauge"><div class="refund-gauge-chart" ref="gaugeAmountEl"></div><div class="refund-gauge-label" style="font-weight:600">金额退款率 {{ (m.refundRate || 0).toFixed(2) }}%</div><div class="refund-gauge-note">= 退款金额 / 净支付金额</div></div><div class="refund-gauge"><div class="refund-gauge-chart" ref="gaugeOrderEl"></div><div class="refund-gauge-label" style="font-weight:600">订单退款率 {{ (m.agg && m.agg.avgOrderRefundRate || 0).toFixed(2) }}%</div><div class="refund-gauge-note">= 退款订单数 / 支付订单数</div></div></div><div class="refund-mini-bar" ref="refundMiniBarEl"></div></template></div></div>
      <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">推广效果分析</span><span style="font-size:0.75rem;color:#94a3b8;margin-left:8px;font-weight:400">推广花费与净成交对比，评估投放ROI与推广效率</span></div><div class="chart-module-body" ref="adChartEl" style="min-height:200px"></div></div>
      <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">客单价与支付转化率</span><span style="font-size:0.75rem;color:#94a3b8;margin-left:8px;font-weight:400">客单价（ARPU）与支付转化率的日度走势，衡量用户质量与成交效率</span></div><div class="chart-module-body" style="min-height:200px"><template v-if="hasTrends"><div class="aov-conv-row"><div class="aov-conv-item"><div style="display:flex;justify-content:space-between;align-items:baseline"><span style="font-size:0.85rem;color:#64748b;font-weight:500">客单价</span><span class="aov-conv-val" style="color:#3B82F6">¥{{ (latestTrend.aov || 0).toFixed(2) }}</span><span class="aov-conv-trend" style="color:#94a3b8">= 支付金额/买家数</span></div><div class="aov-conv-chart" ref="miniAOVEl"></div></div><div class="aov-conv-item"><div style="display:flex;justify-content:space-between;align-items:baseline"><span style="font-size:0.85rem;color:#64748b;font-weight:500">支付转化率</span><span class="aov-conv-val" style="color:#10B981">{{ (latestTrend.convRate || 0).toFixed(2) }}%</span><span class="aov-conv-trend" style="color:#94a3b8">= 买家数/访客数</span></div><div class="aov-conv-chart" ref="miniConvEl"></div></div></div></template></div></div>
    </div>
  </div>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _moApp = null;

  function mountMarketingVue() {
    if (_moApp) return;
    var oldSection = document.getElementById('page-marketing-overview');
    var mount = document.getElementById('page-marketing-overview-vue');
    if (!oldSection || !mount) return;
    // 守卫：旧 section 已被导航隐藏（.hidden）时拒绝挂载，
    // 防止 observer 时序竞争导致离开本页后 Vue 容器仍占位渲染（残留内容顶跑后续页面布局）
    if (oldSection.classList.contains('hidden')) return;
    oldSection.style.display = 'none';
    mount.classList.remove('hidden');
    _moApp = Vue.createApp(MarketingOverviewPage);
    _moApp.mount(mount);
  }

  function unmountMarketingVue() {
    if (!_moApp) return;
    _moApp.unmount();
    _moApp = null;
    var mount = document.getElementById('page-marketing-overview-vue');
    if (mount) { mount.classList.add('hidden'); mount.innerHTML = ''; }
    var oldSection = document.getElementById('page-marketing-overview');
    if (oldSection) oldSection.style.display = '';
  }

  function initHook() {
    var oldSection = document.getElementById('page-marketing-overview');
    if (!oldSection) return;
    function isLoggedIn() {
      return sessionStorage.getItem('admin_logged_in') === 'true';
    }
    // 首页默认可见 → 走过 mount+return 分支后 observer 从未建立，导航离开后永远不卸载
    // （仪表盘残留 bug 根因）。修复：observer 无条件创建，回调自身用 _moApp 判幂等。
    var isVisible = !oldSection.classList.contains('hidden');
    if (isVisible && isLoggedIn()) mountMarketingVue();
    var observer = new MutationObserver(function () {
      var nowVisible = !oldSection.classList.contains('hidden');
      if (nowVisible && !_moApp && isLoggedIn()) mountMarketingVue();
      else if (!nowVisible && _moApp) unmountMarketingVue();
    });
    observer.observe(oldSection, { attributes: true, attributeFilter: ['class'] });
  }

  initHook();
})();
