/**
 * 运营业绩面板 — Vue 版（阶段 4，最后 3 个假数据页之二）
 * classic script，与 app.js 共用全局词法作用域；纯静态假数据（复刻 app.js getMockOpData，无真实接口）
 * XSS：全部走 {{ }} 插值自动转义，绝不用 v-html
 * 结构：页头（标题 + 日期快捷本月/本季度/本年度 + 部门筛选）+ 4 KPI 卡片 + 2 ECharts（部门GMV对比/月度趋势）+ 运营人员业绩排名表（搜索/排序/部门筛选/分页）
 */
(function () {
  'use strict';

  var OP_PAGE_SIZE = 10;

  // 图表实例容器：普通对象（非响应式），避免 Vue Proxy 包裹 ECharts 实例导致异常
  var _opCharts = {};

  // ---- 假数据生成（复刻 app.js getMockOpData） ----
  function _genData() {
    var depts = ['淘宝运营部', '京东运营部', '抖音运营部', '拼多多运营部', '快手运营部'];
    var names = ['张伟', '李娜', '王磊', '赵敏', '陈强', '刘洋', '周静', '吴鹏', '郑丽', '钱浩', '孙悦', '马超', '黄蕾', '林峰', '何琳', '罗刚'];
    var persons = [];
    for (var i = 0; i < 16; i++) {
      var dept = depts[Math.floor(Math.random() * depts.length)];
      var gmv = Math.floor(Math.random() * 800000 + 150000);
      var target = gmv * (0.85 + Math.random() * 0.3);
      var rate = (gmv / target * 100);
      var grade = rate >= 110 ? 'S' : (rate >= 100 ? 'A' : (rate >= 85 ? 'B' : 'C'));
      persons.push({
        name: names[i], dept: dept, gmv: gmv, target: target, targetRate: rate,
        roi: (Math.random() * 5 + 1.5).toFixed(2), convRate: (Math.random() * 4 + 2), aov: (Math.random() * 200 + 100).toFixed(2), grade: grade,
      });
    }
    persons.sort(function (a, b) { return b.gmv - a.gmv; });
    var totalGMV = persons.reduce(function (s, p) { return s + p.gmv; }, 0);
    var deptData = depts.map(function (d) {
      var dp = persons.filter(function (p) { return p.dept === d; });
      return { name: d, gmv: dp.reduce(function (s, p) { return s + p.gmv; }, 0), count: dp.length, targetRate: (dp.reduce(function (s, p) { return s + p.targetRate; }, 0) / dp.length) };
    });
    var months = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];
    var trends = months.map(function (m, i) { return { month: m, gmv: Math.floor(Math.random() * 3000000 + 2000000 + i * 100000), growth: (Math.random() * 15 - 3) }; });
    return {
      persons: persons, totalGMV: totalGMV,
      targetRate: (persons.reduce(function (s, p) { return s + p.targetRate; }, 0) / persons.length),
      teamSize: persons.length, avgGMV: totalGMV / persons.length,
      deptData: deptData, trends: trends,
    };
  }

  // ---- 模块级状态（跨挂载/卸载保留：搜索/排序/部门筛选条件持久化；data 每次 mount 重新生成） ----
  var _op = Vue.reactive({
    data: null, search: '', sortBy: 'gmv', deptFilter: '', page: 1, searchLocked: true,
  });

  var OperationPage = {
    components: { 'ecom-pagination': EcomUI.Pagination },
    setup() {
      var deptChart = Vue.ref(null);
      var trendChart = Vue.ref(null);

      var filtered = Vue.computed(function () {
        var d = _op.data;
        if (!d) return [];
        var persons = d.persons.slice();
        var kw = _op.search.trim().toLowerCase();
        if (kw) persons = persons.filter(function (p) { return p.name.toLowerCase().indexOf(kw) >= 0 || p.dept.toLowerCase().indexOf(kw) >= 0; });
        if (_op.deptFilter) { var df = _op.deptFilter; persons = persons.filter(function (p) { return p.dept === df; }); }
        if (_op.sortBy === 'targetRate') persons.sort(function (a, b) { return b.targetRate - a.targetRate; });
        else if (_op.sortBy === 'roi') persons.sort(function (a, b) { return b.roi - a.roi; });
        else persons.sort(function (a, b) { return b.gmv - a.gmv; });
        persons.forEach(function (p, i) { p.rank = i + 1; });
        return persons;
      });

      var totalPages = Vue.computed(function () {
        return Math.ceil(filtered.value.length / OP_PAGE_SIZE) || 1;
      });

      var paged = Vue.computed(function () {
        var page = _op.page;
        if (page > totalPages.value) page = totalPages.value;
        if (page < 1) page = 1;
        var start = (page - 1) * OP_PAGE_SIZE;
        return filtered.value.slice(start, start + OP_PAGE_SIZE);
      });

      var cards = Vue.computed(function () {
        var d = _op.data;
        if (!d) return { totalGMV: '--', targetRate: '--', teamSize: '--', avgGMV: '--' };
        return {
          totalGMV: '¥' + (d.totalGMV / 10000).toFixed(0) + '万',
          targetRate: d.targetRate.toFixed(1) + '%',
          teamSize: d.teamSize + ' 人',
          avgGMV: '¥' + (d.avgGMV / 10000).toFixed(1) + '万',
        };
      });

      function rankClass(r) { return r === 1 ? 'top1' : (r === 2 ? 'top2' : (r === 3 ? 'top3' : '')); }
      function targetColor(rate) { return rate >= 100 ? '#16a34a' : (rate >= 85 ? '#f59e0b' : '#dc2626'); }
      function gradeClass(g) { return 'op-grade op-grade-' + g.toLowerCase(); }
      function goPage(n) {
        if (n < 1 || n > totalPages.value || n === _op.page) return;
        _op.page = n;
      }
      function unlockSearch() { _op.searchLocked = false; }

      function disposeCharts() {
        Object.keys(_opCharts).forEach(function (k) { try { _opCharts[k].dispose(); } catch (e) {} delete _opCharts[k]; });
      }
      function renderCharts() {
        disposeCharts();
        if (typeof echarts === 'undefined' || !echarts.init) return;
        var d = _op.data;
        if (!d) return;
        // 各部门 GMV 对比（横向柱状）
        if (deptChart.value) {
          var c1 = echarts.init(deptChart.value);
          _opCharts.dept = c1;
          c1.setOption({
            tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
            grid: { left: 100, right: 60, top: 10, bottom: 20 },
            xAxis: { type: 'value', axisLabel: { fontSize: 10, formatter: function (v) { return (v / 10000).toFixed(0) + 'w'; } } },
            yAxis: { type: 'category', data: d.deptData.map(function (x) { return x.name; }), axisLabel: { fontSize: 10 }, inverse: true },
            series: [{
              type: 'bar',
              data: d.deptData.map(function (x, i) { return { value: x.gmv, itemStyle: { color: ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6'][i] || '#6366f1', borderRadius: [0, 4, 4, 0] } }; }),
              barWidth: '55%',
              label: { show: true, position: 'right', fontSize: 10, formatter: function (p) { return '¥' + (p.value / 10000).toFixed(0) + 'w'; } },
            }],
          });
        }
        // GMV 月度趋势（柱 + 环比增长线，双 Y 轴）
        if (trendChart.value) {
          var c2 = echarts.init(trendChart.value);
          _opCharts.trend = c2;
          c2.setOption({
            tooltip: { trigger: 'axis' },
            legend: { top: 0, right: 0, textStyle: { fontSize: 10 } },
            grid: { left: 50, right: 60, top: 35, bottom: 25 },
            xAxis: { type: 'category', data: d.trends.map(function (t) { return t.month; }), axisLabel: { fontSize: 10 } },
            yAxis: [
              { type: 'value', name: 'GMV', axisLabel: { fontSize: 9, formatter: function (v) { return (v / 10000).toFixed(0) + 'w'; } } },
              { type: 'value', name: '增长率%', axisLabel: { fontSize: 9, formatter: function (v) { return v + '%'; } } },
            ],
            series: [
              { name: 'GMV', type: 'bar', data: d.trends.map(function (t) { return t.gmv; }), itemStyle: { color: '#3b82f6', borderRadius: [4, 4, 0, 0] }, barWidth: '50%' },
              { name: '环比增长', type: 'line', yAxisIndex: 1, data: d.trends.map(function (t) { return t.growth; }), lineStyle: { color: '#10b981' }, itemStyle: { color: '#10b981' }, symbol: 'circle', symbolSize: 5 },
            ],
          });
        }
      }

      // 搜索/排序/筛选变化 → 重置到第 1 页（对齐旧版 _opPage=1）
      Vue.watch(function () { return _op.search; }, function () { _op.page = 1; });
      Vue.watch(function () { return _op.sortBy; }, function () { _op.page = 1; });
      Vue.watch(function () { return _op.deptFilter; }, function () { _op.page = 1; });
      // 总页数缩小 → 钳制当前页（对齐旧版 if(_opPage>totalPages)_opPage=totalPages）
      Vue.watch(totalPages, function (tp) { if (_op.page > tp) _op.page = tp; });

      Vue.onMounted(function () { Vue.nextTick(renderCharts); });
      Vue.onUnmounted(function () { disposeCharts(); });

      return {
        op: _op, filtered: filtered, totalPages: totalPages, paged: paged, cards: cards,
        deptChart: deptChart, trendChart: trendChart,
        rankClass: rankClass, targetColor: targetColor, gradeClass: gradeClass,
        goPage: goPage, unlockSearch: unlockSearch,
      };
    },

    template: `
<div>
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon" style="background:linear-gradient(135deg,#f59e0b,#fbbf24);box-shadow:0 2px 8px rgba(245,158,11,0.25)"><i class="fa-solid fa-chart-line" style="color:#fff;font-size:18px"></i></div>
      <div class="dh-title-group"><h2 class="dh-title">运营业绩面板</h2><span class="dh-subtitle">Operations Performance Dashboard</span><span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>实时考核 · 本月数据</span></div>
    </div>
    <div class="dh-filters">
      <div class="dh-date-shortcuts">
        <button class="dh-ds-btn" data-range="1">本月</button>
        <button class="dh-ds-btn active" data-range="3">本季度</button>
        <button class="dh-ds-btn" data-range="12">本年度</button>
      </div>
      <select v-model="op.deptFilter" class="dh-select"><option value="">全部部门</option><option>淘宝运营部</option><option>京东运营部</option><option>抖音运营部</option><option>拼多多运营部</option></select>
    </div>
  </div>

  <!-- KPI 总览卡片 -->
  <div class="mkt-cards-grid">
    <div class="mkt-card mkt-card--emerald"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-bullseye"></i></div><span class="mkt-card-label">部门总GMV</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.totalGMV }}</div><div class="mkt-card-trend"></div></div></div>
    <div class="mkt-card mkt-card--indigo"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-user-check"></i></div><span class="mkt-card-label">目标完成率</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.targetRate }}</div><div class="mkt-card-trend"></div></div></div>
    <div class="mkt-card mkt-card--cyan"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-people-group"></i></div><span class="mkt-card-label">运营团队人数</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.teamSize }}</div><div class="mkt-card-trend"></div></div></div>
    <div class="mkt-card mkt-card--amber"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-ranking-star"></i></div><span class="mkt-card-label">人均GMV</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.avgGMV }}</div><div class="mkt-card-trend"></div></div></div>
  </div>

  <!-- 图表区 -->
  <div class="charts-row" style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px">
    <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">各部门GMV对比</span><span style="font-size:0.75rem;color:#94a3b8">按月展示各部门业绩达成情况</span></div><div class="chart-module-body" ref="deptChart" style="min-height:300px"></div></div>
    <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">GMV月度趋势</span><span style="font-size:0.75rem;color:#94a3b8">各月GMV与环比增长情况</span></div><div class="chart-module-body" ref="trendChart" style="min-height:300px"></div></div>
  </div>

  <!-- 个人业绩排行榜 -->
  <div class="ps-table-card">
    <div class="ps-table-header">
      <div class="ps-table-title-group"><h3>运营人员业绩排名</h3><span class="ps-table-badge" style="background:#fef3c7;color:#d97706">共 {{ filtered.length }} 人</span></div>
      <div class="ps-table-tools">
        <div class="ps-search-wrap"><i class="fa-solid fa-search"></i><input type="text" class="ps-search-input" v-model="op.search" placeholder="搜索姓名/部门..." autocomplete="off" :readonly="op.searchLocked" @focus="unlockSearch"></div>
        <select v-model="op.sortBy" class="ps-select-sm"><option value="gmv">按GMV排序</option><option value="targetRate">按目标完成率排序</option><option value="roi">按ROI排序</option></select>
      </div>
    </div>
    <div class="ps-table-wrap">
      <table class="ps-store-table">
        <thead><tr>
          <th style="width:50px">排名</th><th style="width:80px">姓名</th><th style="width:110px">部门</th>
          <th class="ps-col-num" style="width:130px">GMV</th><th class="ps-col-num" style="width:100px">目标完成率</th>
          <th class="ps-col-num" style="width:90px">ROI</th><th class="ps-col-num" style="width:90px">转化率</th>
          <th class="ps-col-num" style="width:100px">客单价</th><th style="width:80px">绩效评级</th>
        </tr></thead>
        <tbody>
          <tr v-for="p in paged" :key="p.name + p.dept">
            <td><span class="ps-rank" :class="rankClass(p.rank)">{{ p.rank }}</span></td>
            <td><strong>{{ p.name }}</strong></td>
            <td>{{ p.dept }}</td>
            <td class="ps-col-num">¥{{ p.gmv.toLocaleString() }}</td>
            <td class="ps-col-num"><div style="display:flex;align-items:center;gap:6px"><div style="flex:1;height:6px;background:#f1f5f9;border-radius:3px;overflow:hidden"><div style="height:100%;border-radius:3px" :style="{ background: targetColor(p.targetRate), width: Math.min(100, p.targetRate) + '%' }"></div></div><span style="font-size:12px;font-weight:600">{{ p.targetRate.toFixed(1) }}%</span></div></td>
            <td class="ps-col-num">{{ p.roi }}</td>
            <td class="ps-col-num">{{ p.convRate.toFixed(2) }}%</td>
            <td class="ps-col-num">¥{{ p.aov }}</td>
            <td><span :class="gradeClass(p.grade)">{{ p.grade }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
    <ecom-pagination :page="op.page" :total-pages="totalPages" :total="filtered.length" @change="goPage"></ecom-pagination>
  </div>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _opApp = null;

  function mountOperationVue() {
    if (_opApp) return;
    var mount = document.getElementById('page-operation-performance');
    if (!mount) return;
    _op.data = _genData();      // 每次进入页面重新生成假数据（对齐旧版每次导航 renderOperationPerformance）
    _op.page = 1;               // 对齐旧版 renderOperationPerformance 里 _opPage=1
    _op.searchLocked = true;    // 每次挂载重置只读，防浏览器自动填充
    _opApp = Vue.createApp(OperationPage);
    _opApp.mount(mount);
  }

  function unmountOperationVue() {
    if (!_opApp) return;
    _opApp.unmount();
    _opApp = null;
    var mount = document.getElementById('page-operation-performance');
    if (mount) mount.innerHTML = '';
  }

  function initHook() {
    var container = document.getElementById('page-operation-performance');
    if (!container) return;
    var isVisible = !container.classList.contains('hidden');
    if (isVisible) { mountOperationVue(); return; }
    var observer = new MutationObserver(function () {
      var nowVisible = !container.classList.contains('hidden');
      if (nowVisible && !_opApp) mountOperationVue();
      else if (!nowVisible && _opApp) unmountOperationVue();
    });
    observer.observe(container, { attributes: true, attributeFilter: ['class'] });
  }

  initHook();
})();
