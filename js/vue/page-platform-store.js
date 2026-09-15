/**
 * 分平台/店铺详细数据 — Vue 版（阶段 4）
 * classic script，与 app.js 共用全局词法作用域；接口复用 ApiService（getPlatformStoreData / getMarketingFilters，均已封装，零补接口）
 * 结构：页头统计 + 平台横条 + 对比图/饼图 + 店铺排行表格（搜索/排序/分页/7天趋势 sparkline）+ 店铺详情展开面板（2 趋势图）
 * XSS：所有动态数据走 Vue {{ }} 插值自动转义
 * 图表：ECharts 实例用模块级普通对象 _psCharts 保存（非响应式），unmounted 里 dispose
 * 修复旧版 bug：对比图指标与排序下拉里的「推广总成交」旧代码字段名写成 adRevenue（实际字段是 adTotal），导致选它无效果；此处统一改为 adTotal
 */
(function () {
  'use strict';

  var PS_PAGE_SIZE = 15;

  // ==================== 模块级状态（跨挂载/卸载保留，切走再回来不丢筛选） ====================
  var _ps = Vue.reactive({
    data: null,              // { platforms, stores, storeTrends, totalStores, totalNetPayment }
    // 日期
    dateStart: '',
    dateEnd: '',
    dateLabel: '选择日期',
    // 平台
    platformOptions: [],     // 来自 getMarketingFilters
    platform: '',            // 当前筛选的平台（空=全部）
    // 对比图指标
    compareMetric: 'netPayment',
    // 表格
    search: '',
    sortBy: 'netPayment',
    page: 1,
    // 详情展开
    expandedStore: null,     // fullName
    // 双月日历
    cal: { open: false, base: null, start: null, end: null, pickStart: true },
  });

  // 图表实例容器：普通对象（非响应式），避免 Vue Proxy 包裹 ECharts 实例
  var _psCharts = {};

  // ==================== 格式化辅助（复刻旧 _psFmt* / _psColorForPlatform） ====================
  function _fmtMoney(v) { return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 0 }); }
  function _fmtMoneyD(v) { return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
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
  function _yesterdayISO() { return new Date(Date.now() - 86400000).toISOString().slice(0, 10); }
  function _colorForPlatform(name) {
    var map = { '淘宝': '#FF6A00', '天猫': '#FF6A00', '千牛': '#FF6A00', '京东': '#3B82F6', '拼多多': '#E8453C', '抖音': '#8B5CF6', '抖店': '#8B5CF6', '快手': '#FF4906', '小红书': '#FE2C55', '微信小程序': '#07C160' };
    return map[name] || '#6366F1';
  }

  // ==================== 组件 ====================
  var PlatformStorePage = {
    setup: function () {
      // ---- 计算属性 ----
      var platforms = Vue.computed(function () {
        return (_ps.data && _ps.data.platforms) ? _ps.data.platforms : [];
      });
      var filteredStores = Vue.computed(function () {
        var stores = (_ps.data && _ps.data.stores) ? _ps.data.stores.slice() : [];
        if (_ps.search) {
          var kw = _ps.search.toLowerCase();
          stores = stores.filter(function (s) {
            return (s.name || '').toLowerCase().indexOf(kw) >= 0 ||
                   (s.platform || '').toLowerCase().indexOf(kw) >= 0 ||
                   (s.fullName || '').toLowerCase().indexOf(kw) >= 0;
          });
        }
        var sortBy = _ps.sortBy || 'netPayment';
        stores.sort(function (a, b) {
          var va = a[sortBy] || 0;
          var vb = b[sortBy] || 0;
          return vb - va; // 固定降序
        });
        stores.forEach(function (s, i) { s.rank = i + 1; });
        return stores;
      });
      var maxNet = Vue.computed(function () {
        var m = 0;
        filteredStores.value.forEach(function (s) { if (s.netPayment > m) m = s.netPayment; });
        return m || 1;
      });
      var maxVisitors = Vue.computed(function () {
        var m = 0;
        filteredStores.value.forEach(function (s) { if (s.visitors > m) m = s.visitors; });
        return m || 1;
      });
      var maxShare = Vue.computed(function () {
        var m = 0;
        platforms.value.forEach(function (p) { if (p.share > m) m = p.share; });
        return m || 1;
      });
      var totalPages = Vue.computed(function () {
        return Math.ceil(filteredStores.value.length / PS_PAGE_SIZE) || 1;
      });
      var pageStores = Vue.computed(function () {
        var tp = totalPages.value;
        var p = _ps.page;
        if (p > tp) p = tp;
        var start = (p - 1) * PS_PAGE_SIZE;
        return filteredStores.value.slice(start, start + PS_PAGE_SIZE);
      });
      var pageList = Vue.computed(function () {
        var tp = totalPages.value, p = _ps.page, out = [];
        for (var i = 1; i <= tp; i++) {
          if (tp <= 7 || i === 1 || i === tp || (i >= p - 1 && i <= p + 1)) out.push({ t: 'page', n: i });
          else if (i === p - 2 || i === p + 2) out.push({ t: 'gap' });
        }
        return out;
      });
      var expandedStoreObj = Vue.computed(function () {
        if (!_ps.expandedStore || !_ps.data) return null;
        var stores = _ps.data.stores || [];
        for (var i = 0; i < stores.length; i++) if (stores[i].fullName === _ps.expandedStore) return stores[i];
        return null;
      });
      var detailMetrics = Vue.computed(function () {
        var s = expandedStoreObj.value;
        if (!s) return [];
        return [
          { label: '净支付金额', value: _fmtMoney(s.netPayment) },
          { label: '退款金额', value: _fmtMoney(s.refundAmount) },
          { label: '退款率', value: _fmtPct(s.refundRate) },
          { label: '访客数', value: _fmtNum(s.visitors) },
          { label: '支付买家数', value: _fmtNum(s.payers) },
          { label: '支付转化率', value: _fmtPct(s.convRate) },
          { label: '推广花费', value: _fmtMoney(s.adSpend) },
          { label: '推广总成交', value: _fmtMoney(s.adTotal) },
          { label: 'ROI', value: (s.roi || 0).toFixed(4) },
          { label: '客单价', value: _fmtMoneyD(s.aov) },
          { label: '加购人数', value: _fmtNum(s.cart || 0) },
        ];
      });
      function platformClass(p) { return 'ps-platform-' + (p || '其他'); }
      function platformColor(name) { return _colorForPlatform(name); }

      // ---- 双月日历 ----
      function updateDateLabel() {
        var s = _ps.dateStart, e = _ps.dateEnd;
        if (s && e) _ps.dateLabel = s + ' — ' + e;
        else if (s) _ps.dateLabel = s + ' — ';
        else if (e) _ps.dateLabel = ' — ' + e;
        else _ps.dateLabel = '选择日期';
      }
      function calToggle() {
        _ps.cal.open = !_ps.cal.open;
        if (_ps.cal.open) {
          var now = new Date();
          _ps.cal.base = new Date(now.getFullYear(), now.getMonth() - 1, 1);
          _ps.cal.start = _parseDate(_ps.dateStart);
          _ps.cal.end = _parseDate(_ps.dateEnd);
          _ps.cal.pickStart = !(_ps.cal.start && _ps.cal.end);
        }
      }
      function _buildMonth(y, m, isLeft) {
        var st = _ps.cal;
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
        var st = _ps.cal;
        if (!st.base) return [];
        var leftY = st.base.getFullYear(), leftM = st.base.getMonth();
        var right = new Date(leftY, leftM + 1, 1);
        return [_buildMonth(leftY, leftM, true), _buildMonth(right.getFullYear(), right.getMonth(), false)];
      });
      function calPick(y, m, d) {
        var st = _ps.cal;
        var dt = new Date(y, m, d);
        if (st.pickStart) {
          st.start = dt; st.end = null; st.pickStart = false;
        } else {
          if (dt < st.start) { st.end = st.start; st.start = dt; }
          else { st.end = dt; }
          _ps.dateStart = _fmtDate(st.start);
          _ps.dateEnd = _fmtDate(st.end);
          updateDateLabel();
          st.open = false;
          st.pickStart = true;
          _ps.page = 1;
          fetchData();
        }
      }
      function calNav(delta) {
        _ps.cal.base = new Date(_ps.cal.base.getFullYear(), _ps.cal.base.getMonth() + delta, 1);
      }
      function calClear() {
        _ps.dateStart = ''; _ps.dateEnd = '';
        updateDateLabel();
        _ps.cal.open = false;
        _ps.page = 1;
        fetchData();
      }
      function calToday() {
        var now = new Date();
        var dt = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        _ps.dateStart = _fmtDate(dt);
        _ps.dateEnd = _fmtDate(dt);
        _ps.cal.start = dt; _ps.cal.end = dt;
        updateDateLabel();
        _ps.cal.open = false;
        _ps.page = 1;
        fetchData();
      }

      // ---- 数据加载 ----
      async function fetchData() {
        var data = await ApiService.getPlatformStoreData(_ps.dateStart, _ps.dateEnd, _ps.platform);
        if (!data) { _ps.data = null; return; }
        _ps.data = data;
        _ps.expandedStore = null;
      }
      function init() {
        if (!_ps.dateStart && !_ps.dateEnd) {
          var y = _yesterdayISO();
          _ps.dateStart = y; _ps.dateEnd = y;
          updateDateLabel();
        }
        if (!_ps.platformOptions.length) {
          ApiService.getMarketingFilters().then(function (filters) {
            if (filters && filters.platforms) _ps.platformOptions = filters.platforms;
          });
        }
        fetchData();
      }

      // ---- 交互 ----
      function onPlatformChange() {
        _ps.page = 1;
        _ps.expandedStore = null;
        fetchData();
      }
      function filterByPlatform(plat) {
        _ps.platform = plat || '';
        _ps.page = 1;
        _ps.expandedStore = null;
        fetchData();
      }
      function setDateRange(days) {
        var end = new Date(Date.now() - 86400000);
        var start = new Date(end.getTime() - (days - 1) * 86400000);
        _ps.dateStart = start.toISOString().slice(0, 10);
        _ps.dateEnd = end.toISOString().slice(0, 10);
        updateDateLabel();
        _ps.page = 1;
        fetchData();
      }
      function isShortcutActive(days) {
        var yesterday = _yesterdayISO();
        if (days === 1) return _ps.dateStart === yesterday && _ps.dateEnd === yesterday;
        var calcStart = new Date(Date.now() - 86400000 - (days - 1) * 86400000).toISOString().slice(0, 10);
        return _ps.dateStart === calcStart && _ps.dateEnd === yesterday;
      }
      function goPage(n) {
        if (n < 1 || n > totalPages.value || n === _ps.page) return;
        _ps.page = n;
        _ps.expandedStore = null;
      }
      function toggleStoreDetail(fullName) {
        if (!_ps.data) return;
        if (_ps.expandedStore === fullName) { closeDetail(); return; }
        _ps.expandedStore = fullName;
      }
      function closeDetail() {
        _ps.expandedStore = null;
        disposeDetailCharts();
      }

      // ---- 图表 ----
      function _getChart(key, dom) {
        if (typeof echarts === 'undefined' || !echarts.init || !dom) return null;
        if (_psCharts[key]) { try { _psCharts[key].dispose(); } catch (e) {} delete _psCharts[key]; }
        var c = echarts.init(dom);
        _psCharts[key] = c;
        return c;
      }
      function renderCompareChart() {
        var c = _getChart('compare', compareChart.value);
        var pl = platforms.value;
        if (!c || !pl.length) return;
        var metric = _ps.compareMetric || 'netPayment';
        var labelMap = { netPayment: '净支付金额', visitors: '访客数', payers: '支付买家数', adSpend: '推广花费', adTotal: '推广总成交', refundAmount: '退款金额' };
        var metricLabel = labelMap[metric] || '净支付金额';
        var names = pl.map(function (p) { return p.name; });
        var values = pl.map(function (p) { return p[metric] || 0; });
        var colors = pl.map(function (p) { return _colorForPlatform(p.name); });
        var isMoney = metricLabel.indexOf('金额') >= 0 || metricLabel.indexOf('花费') >= 0 || metricLabel.indexOf('成交') >= 0;
        c.setOption({
          tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
            formatter: function (ps) {
              return ps.map(function (p) { return p.marker + p.name + ': ' + (isMoney ? _fmtMoneyD(p.value) : _fmtNum(p.value)); }).join('<br/>');
            }
          },
          grid: { left: 100, right: 50, top: 10, bottom: 20 },
          xAxis: { type: 'value', axisLabel: { fontSize: 10, formatter: function (v) { return isMoney ? (v / 10000).toFixed(0) + 'w' : _fmtNum(v); } } },
          yAxis: { type: 'category', data: names.slice().reverse(), axisLabel: { fontSize: 11, fontWeight: 600 }, inverse: true },
          series: [{
            type: 'bar', data: values.slice().reverse().map(function (v, i) {
              return { value: v, itemStyle: { color: colors.slice().reverse()[i], borderRadius: [0, 4, 4, 0] } };
            }), barWidth: '55%', label: { show: true, position: 'right', fontSize: 10, color: '#64748b',
              formatter: function (p) { return isMoney ? _fmtMoney(p.value) : _fmtNum(p.value); }
            }
          }]
        });
      }
      function renderPieChart() {
        var c = _getChart('pie', pieChart.value);
        var pl = platforms.value;
        if (!c || !pl.length) return;
        var pieData = pl.map(function (p) {
          return { name: p.name, value: p.netPayment, itemStyle: { color: _colorForPlatform(p.name) } };
        });
        c.setOption({
          tooltip: { trigger: 'item', formatter: '{b}: {d}%' },
          series: [{
            type: 'pie', radius: ['50%', '78%'], center: ['50%', '48%'],
            emphasis: { label: { fontSize: 16, fontWeight: 'bold' } },
            label: { formatter: '{b}\n{d}%', fontSize: 11 },
            data: pieData,
            itemStyle: { borderColor: '#fff', borderWidth: 2 }
          }]
        });
      }
      function renderCharts() {
        renderCompareChart();
        renderPieChart();
      }
      function renderDetailCharts() {
        if (typeof echarts === 'undefined' || !echarts.init) return;
        var s = expandedStoreObj.value;
        if (!s) return;
        var trends = (_ps.data && _ps.data.storeTrends && _ps.data.storeTrends[s.fullName]) || [];
        if (!trends.length) return;
        disposeDetailCharts();
        var dates = trends.map(function (t) { return t.date.slice(5); });

        var c1 = _getChart('storeTrend', storeTrendChart.value);
        if (c1) {
          c1.setOption({
            tooltip: { trigger: 'axis' },
            legend: { top: 0, right: 0, textStyle: { fontSize: 10 } },
            grid: { left: 50, right: 20, top: 35, bottom: 25 },
            xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
            yAxis: [{ type: 'value', name: '金额', axisLabel: { fontSize: 9, formatter: function (v) { return (v / 10000).toFixed(0) + 'w'; } } }],
            series: [
              { name: '净支付', type: 'line', smooth: true, data: trends.map(function (t) { return t.netPayment; }), lineStyle: { color: '#6366f1' }, itemStyle: { color: '#6366f1' }, symbol: 'circle', symbolSize: 4, areaStyle: { color: 'rgba(99,102,241,0.06)' } },
              { name: '退款', type: 'line', smooth: true, data: trends.map(function (t) { return t.refundAmount; }), lineStyle: { color: '#ef4444' }, itemStyle: { color: '#ef4444' }, symbol: 'circle', symbolSize: 4, areaStyle: { color: 'rgba(239,68,68,0.04)' } },
              { name: '推广花费', type: 'line', smooth: true, data: trends.map(function (t) { return t.adSpend; }), lineStyle: { color: '#f59e0b', type: 'dashed' }, itemStyle: { color: '#f59e0b' }, symbol: 'diamond', symbolSize: 3 }
            ]
          });
        }
        var c2 = _getChart('storeTraffic', storeTrafficChart.value);
        if (c2) {
          c2.setOption({
            tooltip: { trigger: 'axis' },
            legend: { top: 0, right: 0, textStyle: { fontSize: 10 } },
            grid: { left: 50, right: 20, top: 35, bottom: 25 },
            xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
            yAxis: [{ type: 'value', name: '人数', axisLabel: { fontSize: 9 } }],
            series: [
              { name: '访客数', type: 'bar', data: trends.map(function (t) { return t.visitors; }), itemStyle: { color: '#818cf8', borderRadius: [4, 4, 0, 0] }, barGap: '15%', barWidth: '35%' },
              { name: '买家数', type: 'bar', data: trends.map(function (t) { return t.payers; }), itemStyle: { color: '#34d399', borderRadius: [4, 4, 0, 0] }, barWidth: '35%' }
            ]
          });
        }
      }
      function disposeDetailCharts() {
        ['storeTrend', 'storeTraffic'].forEach(function (id) {
          if (_psCharts[id]) { try { _psCharts[id].dispose(); } catch (e) {} delete _psCharts[id]; }
        });
      }
      function disposeAllCharts() {
        Object.keys(_psCharts).forEach(function (k) {
          try { _psCharts[k].dispose(); } catch (e) {}
          delete _psCharts[k];
        });
      }

      // ---- 7 天趋势 sparkline（canvas 手绘，非 ECharts） ----
      function drawSparklines() {
        var el = rootEl.value;
        if (!el || !_ps.data) return;
        var canvases = el.querySelectorAll('.ps-sparkline-canvas');
        for (var i = 0; i < canvases.length; i++) {
          var canvas = canvases[i];
          var fullName = canvas.getAttribute('data-fullname');
          var trends = (_ps.data.storeTrends && _ps.data.storeTrends[fullName]) || [];
          _drawSparkline(canvas, trends.map(function (t) { return t.netPayment; }), '#6366f1');
        }
      }
      function _drawSparkline(canvas, values, color) {
        if (!canvas || !values || !values.length) return;
        var dpr = window.devicePixelRatio || 1;
        var w = 100, h = 36;
        canvas.width = w * dpr;
        canvas.height = h * dpr;
        canvas.style.width = w + 'px';
        canvas.style.height = h + 'px';
        var ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);
        var pad = 4;
        var max = Math.max.apply(null, values) || 1;
        var min = Math.min.apply(null, values) || 0;
        var range = max - min || 1;
        ctx.clearRect(0, 0, w, h);
        ctx.beginPath();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        var stepX = (w - pad * 2) / (values.length - 1);
        values.forEach(function (v, i) {
          var x = pad + i * stepX;
          var y = h - pad - ((v - min) / range) * (h - pad * 2);
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
        ctx.lineTo(pad + (values.length - 1) * stepX, h - pad);
        ctx.lineTo(pad, h - pad);
        ctx.closePath();
        var grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, color + '25');
        grad.addColorStop(1, color + '02');
        ctx.fillStyle = grad;
        ctx.fill();
      }

      // ---- 搜索框防浏览器自动填充 ----
      var searchLocked = Vue.ref(true);
      function unlockSearch() { searchLocked.value = false; }

      // ---- refs ----
      var rootEl = Vue.ref(null);
      var compareChart = Vue.ref(null);
      var pieChart = Vue.ref(null);
      var storeTrendChart = Vue.ref(null);
      var storeTrafficChart = Vue.ref(null);
      var dateWrap = Vue.ref(null);

      // ---- 点击别处关闭日历 ----
      function onDocClick(e) {
        if (dateWrap.value && !dateWrap.value.contains(e.target)) _ps.cal.open = false;
      }

      // ---- 数据变化后重渲染图表 / sparkline ----
      Vue.watch(function () { return _ps.data; }, function () {
        Vue.nextTick(function () { renderCharts(); drawSparklines(); });
      });
      Vue.watch(function () { return _ps.expandedStore; }, function (val) {
        if (val) Vue.nextTick(renderDetailCharts);
        else disposeDetailCharts();
      });
      Vue.watch(filteredStores, function () {
        Vue.nextTick(drawSparklines);
      });

      Vue.onMounted(function () {
        document.addEventListener('click', onDocClick);
        if (_ps.data) { Vue.nextTick(function () { renderCharts(); drawSparklines(); }); }
      });
      Vue.onUnmounted(function () {
        document.removeEventListener('click', onDocClick);
        disposeAllCharts();
      });

      init();

      return {
        ps: _ps,
        platforms: platforms,
        filteredStores: filteredStores,
        maxNet: maxNet, maxVisitors: maxVisitors, maxShare: maxShare,
        totalPages: totalPages, pageStores: pageStores, pageList: pageList,
        expandedStoreObj: expandedStoreObj, detailMetrics: detailMetrics, platformClass: platformClass, platformColor: platformColor,
        calMonths: calMonths,
        fmtMoney: _fmtMoney, fmtNum: _fmtNum, fmtPct: _fmtPct, fmtMoneyD: _fmtMoneyD,
        updateDateLabel: updateDateLabel,
        calToggle: calToggle, calPick: calPick, calNav: calNav, calClear: calClear, calToday: calToday,
        onPlatformChange: onPlatformChange, filterByPlatform: filterByPlatform,
        setDateRange: setDateRange, isShortcutActive: isShortcutActive,
        goPage: goPage, toggleStoreDetail: toggleStoreDetail, closeDetail: closeDetail,
        renderCompareChart: renderCompareChart,
        searchLocked: searchLocked, unlockSearch: unlockSearch,
        rootEl: rootEl, compareChart: compareChart, pieChart: pieChart,
        storeTrendChart: storeTrendChart, storeTrafficChart: storeTrafficChart,
        dateWrap: dateWrap,
      };
    },

    template: `
<div ref="rootEl">
  <!-- ====== 页头 ====== -->
  <div class="ps-header">
    <div class="ps-header-left">
      <div class="ps-header-icon"><i class="fa-solid fa-shop"></i></div>
      <div class="ps-header-title-group">
        <h2 class="ps-header-title">分平台 / 店铺详细数据</h2>
        <span class="ps-header-sub">Platform & Store Analytics</span>
      </div>
    </div>
    <div class="ps-header-stats">
      <div class="ps-hs-item"><span class="ps-hs-label">平台数</span><span class="ps-hs-value">{{ ps.data ? platforms.length : '--' }}</span></div>
      <div class="ps-hs-item"><span class="ps-hs-label">店铺数</span><span class="ps-hs-value">{{ ps.data ? ps.data.totalStores : '--' }}</span></div>
      <div class="ps-hs-item"><span class="ps-hs-label">总净支付</span><span class="ps-hs-value">{{ ps.data ? fmtMoney(ps.data.totalNetPayment) : '--' }}</span></div>
    </div>
    <button v-show="ps.platform" class="ps-back-btn" @click="filterByPlatform('')"><i class="fa-solid fa-arrow-left"></i> 返回全部平台</button>
    <div class="ps-header-filters">
      <div class="ps-date-shortcuts">
        <button class="ps-ds-btn" :class="{ active: isShortcutActive(1) }" @click="setDateRange(1)">昨日</button>
        <button class="ps-ds-btn" :class="{ active: isShortcutActive(7) }" @click="setDateRange(7)">近7天</button>
        <button class="ps-ds-btn" :class="{ active: isShortcutActive(30) }" @click="setDateRange(30)">近30天</button>
      </div>
      <select v-model="ps.platform" class="ps-select" @change="onPlatformChange">
        <option value="">全部平台</option>
        <option v-for="p in ps.platformOptions" :key="p" :value="p">{{ p }}</option>
      </select>
      <div class="ps-date-picker" style="position:relative" ref="dateWrap">
        <button type="button" @click="calToggle" style="display:flex;align-items:center;gap:6px;background:#fff;border:1px solid #e2e8f0;border-radius:9px;padding:5px 12px;height:33px;cursor:pointer;font-size:0.82rem;color:#334155;font-family:inherit">
          <i class="fa-regular fa-calendar" style="color:#94a3b8;font-size:0.82rem"></i>
          <span style="white-space:nowrap">{{ ps.dateLabel }}</span>
          <i class="fa-solid fa-chevron-down" style="color:#94a3b8;font-size:0.7rem"></i>
        </button>
        <div v-show="ps.cal.open" style="position:absolute;top:40px;right:0;z-index:60;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.15);padding:14px;user-select:none">
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
            <span style="font-size:12px;color:#64748b">{{ ps.cal.pickStart ? '请选择开始日期' : '请选择结束日期' }}</span>
            <div style="display:flex;gap:10px">
              <button @click="calClear" style="border:none;background:none;color:#94a3b8;font-size:12px;cursor:pointer">清除</button>
              <button @click="calToday" style="border:none;background:none;color:#6366f1;font-size:12px;cursor:pointer;font-weight:600">今天</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ====== 平台概览横条 ====== -->
  <div class="ps-platform-bars">
    <div v-for="p in platforms" :key="p.name" class="ps-platform-bar" :class="{ active: ps.platform === p.name }" @click="filterByPlatform(p.name)">
      <div class="ps-pb-color" :style="'background:' + platformColor(p.name)"></div>
      <div class="ps-pb-info"><div class="ps-pb-name">{{ p.name }}</div><div class="ps-pb-stores">{{ p.storeCount }} 家店铺</div></div>
      <div class="ps-pb-metrics">
        <div class="ps-pb-metric"><span class="ps-pb-metric-label">总支付金额</span><span class="ps-pb-metric-value">{{ fmtMoney(p.payment) }}</span></div>
        <div class="ps-pb-metric"><span class="ps-pb-metric-label">净支付</span><span class="ps-pb-metric-value">{{ fmtMoney(p.netPayment) }}</span></div>
        <div class="ps-pb-metric"><span class="ps-pb-metric-label">访客</span><span class="ps-pb-metric-value">{{ fmtNum(p.visitors) }}</span></div>
        <div class="ps-pb-metric"><span class="ps-pb-metric-label">买家</span><span class="ps-pb-metric-value">{{ fmtNum(p.payers) }}</span></div>
        <div class="ps-pb-metric"><span class="ps-pb-metric-label">推广花费</span><span class="ps-pb-metric-value">{{ fmtMoney(p.adSpend) }}</span></div>
        <div class="ps-pb-metric"><span class="ps-pb-metric-label">ROI</span><span class="ps-pb-metric-value">{{ p.adSpend > 0 ? (p.adTotal / p.adSpend).toFixed(2) : '-' }}</span></div>
      </div>
      <div class="ps-pb-share">
        <div class="ps-pb-share-top"><span class="ps-pb-share-pct">{{ p.share.toFixed(1) }}%</span><span class="ps-pb-share-label">占比</span></div>
        <div class="ps-pb-share-bar"><div class="ps-pb-share-fill" :style="'width:' + (p.share / maxShare * 100) + '%;background:' + platformColor(p.name)"></div></div>
      </div>
      <div class="ps-pb-trend neutral">--</div>
    </div>
    <div v-if="!platforms.length" style="text-align:center;padding:24px;color:#94a3b8">暂无平台数据</div>
  </div>

  <!-- ====== 图表对比区 ====== -->
  <div class="ps-charts-row">
    <div class="ps-chart-card" style="flex:1.2">
      <div class="ps-chart-card-header">
        <h3>平台核心指标对比</h3>
        <select v-model="ps.compareMetric" class="ps-select-sm" @change="renderCompareChart">
          <option value="netPayment">净支付金额</option>
          <option value="visitors">访客数</option>
          <option value="payers">支付买家数</option>
          <option value="adSpend">推广花费</option>
          <option value="adTotal">推广总成交</option>
          <option value="refundAmount">退款金额</option>
        </select>
      </div>
      <div class="ps-chart-body" ref="compareChart" style="height:300px"></div>
    </div>
    <div class="ps-chart-card" style="flex:0.8">
      <div class="ps-chart-card-header"><h3>平台营收占比</h3></div>
      <div class="ps-chart-body" ref="pieChart" style="height:300px"></div>
    </div>
  </div>

  <!-- ====== 店铺排行榜 ====== -->
  <div class="ps-table-card">
    <div class="ps-table-header">
      <div class="ps-table-title-group">
        <h3>店铺数据排行榜</h3>
        <span class="ps-table-badge">共 {{ filteredStores.length }} 家店铺</span>
      </div>
      <div class="ps-table-tools">
        <div class="ps-search-wrap">
          <i class="fa-solid fa-search"></i>
          <input type="text" class="ps-search-input" v-model="ps.search" autocomplete="off" :readonly="searchLocked" @focus="unlockSearch" placeholder="搜索店铺或品牌...">
        </div>
        <select v-model="ps.sortBy" class="ps-select-sm">
          <option value="netPayment">按净支付排序</option>
          <option value="visitors">按访客数排序</option>
          <option value="payers">按买家数排序</option>
          <option value="adSpend">按推广花费排序</option>
          <option value="adTotal">按推广成交排序</option>
          <option value="convRate">按转化率排序</option>
          <option value="aov">按客单价排序</option>
        </select>
      </div>
    </div>
    <div class="ps-table-wrap">
      <table class="ps-store-table">
        <thead>
          <tr>
            <th style="width:50px">排名</th>
            <th style="width:180px">店铺名</th>
            <th style="width:80px">平台</th>
            <th class="ps-col-num">净支付金额</th>
            <th class="ps-col-num">访客数</th>
            <th class="ps-col-num">支付买家数</th>
            <th class="ps-col-num">推广花费</th>
            <th class="ps-col-num">推广总成交</th>
            <th class="ps-col-num">ROI</th>
            <th class="ps-col-num">转化率</th>
            <th class="ps-col-num">客单价</th>
            <th class="ps-col-num">退款率</th>
            <th style="width:100px">7天趋势</th>
            <th style="width:60px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in pageStores" :key="s.fullName" :class="{ expanded: ps.expandedStore === s.fullName }" @click="toggleStoreDetail(s.fullName)">
            <td><span class="ps-rank" :class="s.rank === 1 ? 'top1' : (s.rank === 2 ? 'top2' : (s.rank === 3 ? 'top3' : ''))">{{ s.rank }}</span></td>
            <td><span class="ps-store-name">{{ s.name }}</span></td>
            <td><span class="ps-store-platform" :class="platformClass(s.platform)">{{ s.platform }}</span></td>
            <td class="ps-col-num">{{ fmtMoney(s.netPayment) }}<span class="ps-inline-bar" :style="'width:' + Math.max(4, (s.netPayment / maxNet * 80)) + 'px;background:#6366f1'"></span></td>
            <td class="ps-col-num">{{ fmtNum(s.visitors) }}<span class="ps-inline-bar" :style="'width:' + Math.max(4, (s.visitors / maxVisitors * 80)) + 'px;background:#10b981'"></span></td>
            <td class="ps-col-num">{{ fmtNum(s.payers) }}</td>
            <td class="ps-col-num">{{ fmtMoney(s.adSpend) }}</td>
            <td class="ps-col-num">{{ fmtMoney(s.adTotal) }}</td>
            <td class="ps-col-num">{{ (s.roi || 0).toFixed(2) }}</td>
            <td class="ps-col-num">{{ fmtPct(s.convRate) }}</td>
            <td class="ps-col-num">{{ fmtMoneyD(s.aov) }}</td>
            <td class="ps-col-num">{{ fmtPct(s.refundRate) }}</td>
            <td><div class="ps-sparkline-cell"><canvas class="ps-sparkline-canvas" :data-fullname="s.fullName" width="100" height="36"></canvas></div></td>
            <td><button class="ps-expand-btn" :class="{ active: ps.expandedStore === s.fullName }" @click.stop="toggleStoreDetail(s.fullName)"><i class="fa-solid" :class="ps.expandedStore === s.fullName ? 'fa-chevron-up' : 'fa-chevron-down'"></i></button></td>
          </tr>
          <tr v-if="!pageStores.length"><td colspan="14" style="text-align:center;padding:40px;color:#94a3b8">暂无数据</td></tr>
        </tbody>
      </table>
    </div>
    <div class="ps-table-footer">
      <span>第 {{ ps.page }} / {{ totalPages }} 页，共 {{ filteredStores.length }} 条</span>
      <div class="ps-pagination-btns">
        <button :disabled="ps.page <= 1" @click="goPage(ps.page - 1)"><i class="fa-solid fa-chevron-left"></i></button>
        <template v-for="(it, idx) in pageList" :key="idx">
          <button v-if="it.t === 'page'" :class="{ active: it.n === ps.page }" @click="goPage(it.n)">{{ it.n }}</button>
          <button v-else disabled>...</button>
        </template>
        <button :disabled="ps.page >= totalPages" @click="goPage(ps.page + 1)"><i class="fa-solid fa-chevron-right"></i></button>
      </div>
    </div>
  </div>

  <!-- ====== 店铺详情展开面板 ====== -->
  <div class="ps-detail-panel" :class="{ hidden: !ps.expandedStore }">
    <div class="ps-detail-header">
      <div class="ps-detail-title-group">
        <span class="ps-detail-platform" :class="expandedStoreObj ? platformClass(expandedStoreObj.platform) : ''">{{ expandedStoreObj ? expandedStoreObj.platform : '' }}</span>
        <h3>{{ expandedStoreObj ? expandedStoreObj.name : '' }}</h3>
      </div>
      <button class="ps-detail-close" @click="closeDetail"><i class="fa-solid fa-xmark"></i></button>
    </div>
    <div class="ps-detail-metrics">
      <div class="ps-dm-item" v-for="(m, mi) in detailMetrics" :key="mi"><span class="ps-dm-label">{{ m.label }}</span><span class="ps-dm-value">{{ m.value }}</span></div>
    </div>
    <div class="ps-detail-charts">
      <div class="ps-chart-card">
        <div class="ps-chart-card-header"><h3>营收与退款趋势</h3></div>
        <div class="ps-chart-body" ref="storeTrendChart" style="height:260px"></div>
      </div>
      <div class="ps-chart-card">
        <div class="ps-chart-card-header"><h3>流量与转化趋势</h3></div>
        <div class="ps-chart-body" ref="storeTrafficChart" style="height:260px"></div>
      </div>
    </div>
  </div>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _psApp = null;

  function mountPlatformStoreVue() {
    if (_psApp) return;
    var oldSection = document.getElementById('page-platform-store');
    var mount = document.getElementById('page-platform-store-vue');
    if (!oldSection || !mount) return;
    oldSection.style.display = 'none';
    mount.classList.remove('hidden');
    _psApp = Vue.createApp(PlatformStorePage);
    _psApp.mount(mount);
  }

  function unmountPlatformStoreVue() {
    if (!_psApp) return;
    _psApp.unmount();
    _psApp = null;
    var mount = document.getElementById('page-platform-store-vue');
    if (mount) { mount.classList.add('hidden'); mount.innerHTML = ''; }
    var oldSection = document.getElementById('page-platform-store');
    if (oldSection) oldSection.style.display = '';
  }

  function initHook() {
    var oldSection = document.getElementById('page-platform-store');
    if (!oldSection) return;
    var isVisible = !oldSection.classList.contains('hidden');
    if (isVisible) { mountPlatformStoreVue(); return; }
    var observer = new MutationObserver(function () {
      var nowVisible = !oldSection.classList.contains('hidden');
      if (nowVisible && !_psApp) mountPlatformStoreVue();
      else if (!nowVisible && _psApp) unmountPlatformStoreVue();
    });
    observer.observe(oldSection, { attributes: true, attributeFilter: ['class'] });
  }

  initHook();
})();
