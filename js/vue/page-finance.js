/**
 * 财务中心 — Vue 版（阶段 4，最后 3 个假数据页之三，也是最后一页）
 * classic script，与 app.js 共用全局词法作用域；纯静态假数据（复刻 app.js getMockFinanceData，无真实接口）
 * XSS：全部走 {{ }} 插值自动转义，绝不用 v-html
 * 结构：页头（标题 + 更新时间 + 日期快捷本月/本季度/本年度 + 平台下拉）+ 8 财务概览卡片 + 2 ECharts（月度营收利润趋势 / 成本结构饼图）+ 收支明细表（搜索/类型筛选/分页）
 */
(function () {
  'use strict';

  var FIN_PAGE_SIZE = 10;

  // 图表实例容器：普通对象（非响应式），避免 Vue Proxy 包裹 ECharts 实例导致异常
  var _finCharts = {};

  // ---- 假数据生成（复刻 app.js getMockFinanceData） ----
  function _genData() {
    var months = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];
    var revenue = 0, cost = 0, profit = 0, refund = 0, adSpend = 0, logistics = 0, receivable = 0;
    var monthlyData = months.map(function (m, i) {
      var r = Math.floor(Math.random() * 2000000 + 1500000 + i * 80000);
      var c = r * (0.55 + Math.random() * 0.2);
      var p = r - c;
      revenue += r; cost += c; profit += p;
      refund += r * (0.03 + Math.random() * 0.05);
      adSpend += r * (0.08 + Math.random() * 0.12);
      logistics += r * (0.04 + Math.random() * 0.04);
      receivable += r * (0.05 + Math.random() * 0.08);
      return { month: m, revenue: r, cost: c, profit: p };
    });
    var margin = revenue > 0 ? (profit / revenue * 100) : 0;

    var txTypes = ['income', 'income', 'income', 'expense', 'expense', 'refund'];
    var txPlatforms = ['淘宝', '京东', '拼多多', '抖音', '快手'];
    var payMethods = ['支付宝', '微信支付', '银行卡', '花呗', '京东支付'];
    var txStatuses = ['已完成', '已完成', '已完成', '处理中', '待审核'];
    var summaries = ['商品销售收入', '平台推广费', '物流运费', '退款-质量问题', '退款-物流', '包装耗材', '仓储费', '技术服务费', '营销活动费', '佣金支出'];
    var transactions = [];
    for (var i = 0; i < 40; i++) {
      var type = txTypes[Math.floor(Math.random() * txTypes.length)];
      var amt = type === 'refund' ? Math.floor(Math.random() * 500 + 30) : (type === 'expense' ? Math.floor(Math.random() * 8000 + 500) : Math.floor(Math.random() * 30000 + 1000));
      var date = new Date(); date.setDate(date.getDate() - Math.floor(Math.random() * 90));
      transactions.push({
        date: date.toISOString().slice(0, 10), type: type,
        orderId: type === 'income' ? 'ORD' + Math.floor(Math.random() * 90000 + 10000) : (type === 'refund' ? 'RF' + Math.floor(Math.random() * 90000 + 10000) : '—'),
        summary: summaries[Math.floor(Math.random() * summaries.length)],
        platform: txPlatforms[Math.floor(Math.random() * txPlatforms.length)],
        amount: amt, payMethod: payMethods[Math.floor(Math.random() * payMethods.length)],
        status: txStatuses[Math.floor(Math.random() * txStatuses.length)],
      });
    }
    transactions.sort(function (a, b) { return b.date.localeCompare(a.date); });
    return { revenue: revenue, cost: cost, profit: profit, margin: margin, refund: refund, adSpend: adSpend, logistics: logistics, receivable: receivable, monthlyData: monthlyData, transactions: transactions };
  }

  // ---- 模块级状态（跨挂载保留：搜索/类型筛选条件；data 每次 mount 重新生成） ----
  var _fin = Vue.reactive({
    data: null, search: '', txType: '', page: 1, searchLocked: true,
    updateTime: '--',
  });

  var FinancePage = {
    components: { 'ecom-pagination': EcomUI.Pagination },
    setup() {
      var trendChart = Vue.ref(null);
      var costChart = Vue.ref(null);

      var filtered = Vue.computed(function () {
        var d = _fin.data;
        if (!d) return [];
        var txs = d.transactions.slice();
        var kw = _fin.search.trim().toLowerCase();
        if (kw) txs = txs.filter(function (t) { return t.summary.toLowerCase().indexOf(kw) >= 0 || t.orderId.toLowerCase().indexOf(kw) >= 0; });
        if (_fin.txType) { var tt = _fin.txType; txs = txs.filter(function (t) { return t.type === tt; }); }
        return txs;
      });

      var totalPages = Vue.computed(function () {
        return Math.ceil(filtered.value.length / FIN_PAGE_SIZE) || 1;
      });

      var paged = Vue.computed(function () {
        var page = _fin.page;
        if (page > totalPages.value) page = totalPages.value;
        if (page < 1) page = 1;
        var start = (page - 1) * FIN_PAGE_SIZE;
        return filtered.value.slice(start, start + FIN_PAGE_SIZE);
      });

      var cards = Vue.computed(function () {
        var d = _fin.data;
        if (!d) return { revenue: '--', cost: '--', profit: '--', margin: '--', refund: '--', adSpend: '--', logistics: '--', receivable: '--' };
        function wan(v) { return '¥' + (v / 10000).toFixed(1) + '万'; }
        return {
          revenue: wan(d.revenue), cost: wan(d.cost), profit: wan(d.profit), margin: d.margin.toFixed(2) + '%',
          refund: wan(d.refund), adSpend: wan(d.adSpend), logistics: wan(d.logistics), receivable: wan(d.receivable),
        };
      });

      // 收支明细：类型/金额色 class 函数（复刻旧版 fin-income/fin-expense/fin-refund）
      function txClass(t) { return t === 'income' ? 'fin-income' : (t === 'expense' ? 'fin-expense' : 'fin-refund'); }
      function txText(t) { return t === 'income' ? '收入' : (t === 'expense' ? '支出' : '退款'); }
      function amtPrefix(t) { return t === 'income' ? '+' : '-'; }
      function statusClass(s) { return s === '已完成' ? 'badge-success' : (s === '处理中' ? 'badge-info' : 'badge-warning'); }
      function goPage(n) {
        if (n < 1 || n > totalPages.value || n === _fin.page) return;
        _fin.page = n;
      }
      function unlockSearch() { _fin.searchLocked = false; }

      function disposeCharts() {
        Object.keys(_finCharts).forEach(function (k) { try { _finCharts[k].dispose(); } catch (e) {} delete _finCharts[k]; });
      }
      function renderCharts() {
        disposeCharts();
        if (typeof echarts === 'undefined' || !echarts.init) return;
        var d = _fin.data;
        if (!d) return;
        // 月度营收与利润趋势（营收柱 + 成本柱 + 利润线）
        if (trendChart.value) {
          var c1 = echarts.init(trendChart.value);
          _finCharts.trend = c1;
          c1.setOption({
            tooltip: { trigger: 'axis' },
            legend: { top: 0, right: 0, textStyle: { fontSize: 10 } },
            grid: { left: 55, right: 60, top: 35, bottom: 25 },
            xAxis: { type: 'category', data: d.monthlyData.map(function (m) { return m.month; }), axisLabel: { fontSize: 10 } },
            yAxis: { type: 'value', axisLabel: { fontSize: 9, formatter: function (v) { return (v / 10000).toFixed(0) + 'w'; } } },
            series: [
              { name: '营收', type: 'bar', data: d.monthlyData.map(function (m) { return m.revenue; }), itemStyle: { color: '#3b82f6', borderRadius: [4, 4, 0, 0] }, barWidth: '35%' },
              { name: '成本', type: 'bar', data: d.monthlyData.map(function (m) { return m.cost; }), itemStyle: { color: '#f87171', borderRadius: [4, 4, 0, 0] }, barWidth: '35%' },
              { name: '利润', type: 'line', data: d.monthlyData.map(function (m) { return m.profit; }), lineStyle: { color: '#10b981', width: 2 }, itemStyle: { color: '#10b981' }, symbol: 'circle', symbolSize: 5 },
            ],
          });
        }
        // 成本结构分析（环形饼图 7 类）
        if (costChart.value) {
          var costBreakdown = [
            { name: '推广费用', value: d.adSpend }, { name: '物流成本', value: d.logistics }, { name: '退款损失', value: d.refund },
            { name: '商品成本', value: d.cost * 0.55 }, { name: '平台佣金', value: d.cost * 0.2 }, { name: '人员成本', value: d.cost * 0.15 }, { name: '其他费用', value: d.cost * 0.1 },
          ];
          var c2 = echarts.init(costChart.value);
          _finCharts.cost = c2;
          c2.setOption({
            tooltip: { trigger: 'item', formatter: '{b}: ¥{c} ({d}%)' },
            color: ['#ef4444', '#f59e0b', '#f97316', '#6366f1', '#8b5cf6', '#ec4899', '#06b6d4'],
            series: [{
              type: 'pie', radius: ['45%', '75%'], center: ['50%', '50%'], data: costBreakdown,
              label: { fontSize: 10, formatter: '{b}\n{d}%' }, emphasis: { label: { fontSize: 14, fontWeight: 'bold' } },
              itemStyle: { borderColor: '#fff', borderWidth: 2 },
            }],
          });
        }
      }

      // 搜索/类型筛选变化 → 重置第 1 页（对齐旧版 _finPage=1）
      Vue.watch(function () { return _fin.search; }, function () { _fin.page = 1; });
      Vue.watch(function () { return _fin.txType; }, function () { _fin.page = 1; });
      // 总页数缩小 → 钳制当前页
      Vue.watch(totalPages, function (tp) { if (_fin.page > tp) _fin.page = tp; });

      Vue.onMounted(function () { Vue.nextTick(renderCharts); });
      Vue.onUnmounted(function () { disposeCharts(); });

      return {
        fin: _fin, filtered: filtered, totalPages: totalPages, paged: paged, cards: cards,
        txClass: txClass, txText: txText, amtPrefix: amtPrefix, statusClass: statusClass,
        goPage: goPage, unlockSearch: unlockSearch,
        trendChart: trendChart, costChart: costChart,
      };
    },

    template: `
<div>
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon" style="background:linear-gradient(135deg,#0891b2,#22d3ee);box-shadow:0 2px 8px rgba(8,145,178,0.25)"><i class="fa-solid fa-coins" style="color:#fff;font-size:18px"></i></div>
      <div class="dh-title-group"><h2 class="dh-title">财务中心</h2><span class="dh-subtitle">Finance Center</span><span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>实时核算 · 更新于 {{ fin.updateTime }}</span></div>
    </div>
    <div class="dh-filters">
      <div class="dh-date-shortcuts">
        <button class="dh-ds-btn" data-range="1">本月</button>
        <button class="dh-ds-btn active" data-range="3">本季度</button>
        <button class="dh-ds-btn" data-range="12">本年度</button>
      </div>
      <select class="dh-select"><option value="">全部平台</option><option>淘宝</option><option>京东</option><option>拼多多</option><option>抖音</option><option>快手</option></select>
    </div>
  </div>

  <!-- 财务概览卡片（8 项） -->
  <div class="mkt-cards-grid">
    <div class="mkt-card mkt-card--emerald"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-sack-dollar"></i></div><span class="mkt-card-label">总营收</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.revenue }}</div><div class="mkt-card-trend"></div></div></div>
    <div class="mkt-card mkt-card--coral"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-arrow-trend-down"></i></div><span class="mkt-card-label">总成本</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.cost }}</div><div class="mkt-card-trend"></div></div></div>
    <div class="mkt-card mkt-card--indigo"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-chart-pie"></i></div><span class="mkt-card-label">净利润</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.profit }}</div><div class="mkt-card-trend"></div></div></div>
    <div class="mkt-card mkt-card--cyan"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-percent"></i></div><span class="mkt-card-label">利润率</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.margin }}</div><div class="mkt-card-trend"></div></div></div>
    <div class="mkt-card mkt-card--rose"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-rotate-left"></i></div><span class="mkt-card-label">退款金额</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.refund }}</div><div class="mkt-card-trend"></div></div></div>
    <div class="mkt-card mkt-card--amber"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-receipt"></i></div><span class="mkt-card-label">推广花费</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.adSpend }}</div><div class="mkt-card-trend"></div></div></div>
    <div class="mkt-card mkt-card--violet"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-truck"></i></div><span class="mkt-card-label">物流成本</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.logistics }}</div><div class="mkt-card-trend"></div></div></div>
    <div class="mkt-card mkt-card--green"><div class="mkt-card-header"><div class="mkt-card-icon"><i class="fa-solid fa-hand-holding-dollar"></i></div><span class="mkt-card-label">应收账款</span></div><div class="mkt-card-body"><div class="mkt-card-value">{{ cards.receivable }}</div><div class="mkt-card-trend"></div></div></div>
  </div>

  <!-- 图表区 -->
  <div class="charts-row" style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px">
    <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">月度营收与利润趋势</span><span style="font-size:0.75rem;color:#94a3b8">近12个月收入、成本、利润走势</span></div><div class="chart-module-body" ref="trendChart" style="min-height:300px"></div></div>
    <div class="chart-module" style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;box-shadow:0 1px 4px rgba(0,0,0,0.03);padding:18px 20px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px"><span style="font-size:1rem;font-weight:600;color:#1e293b">成本结构分析</span><span style="font-size:0.75rem;color:#94a3b8">按类别拆分的成本构成占比</span></div><div class="chart-module-body" ref="costChart" style="min-height:300px"></div></div>
  </div>

  <!-- 收支明细表 -->
  <div class="ps-table-card">
    <div class="ps-table-header">
      <div class="ps-table-title-group"><h3>收支明细</h3><span class="ps-table-badge" style="background:#ecfeff;color:#0891b2">共 {{ filtered.length }} 条</span></div>
      <div class="ps-table-tools">
        <div class="ps-search-wrap"><i class="fa-solid fa-search"></i><input type="text" class="ps-search-input" v-model="fin.search" placeholder="搜索摘要/订单号..." autocomplete="off" :readonly="fin.searchLocked" @focus="unlockSearch"></div>
        <select v-model="fin.txType" class="ps-select-sm"><option value="">全部类型</option><option value="income">收入</option><option value="expense">支出</option><option value="refund">退款</option></select>
      </div>
    </div>
    <div class="ps-table-wrap">
      <table class="ps-store-table">
        <thead><tr>
          <th style="width:100px">日期</th><th style="width:110px">类型</th><th style="width:130px">订单号</th>
          <th style="width:160px">摘要</th><th style="width:80px">平台</th>
          <th class="ps-col-num" style="width:110px">金额</th><th style="width:90px">支付方式</th><th style="width:80px">状态</th>
        </tr></thead>
        <tbody>
          <tr v-for="(t, i) in paged" :key="t.date + t.orderId + t.summary + i">
            <td>{{ t.date }}</td>
            <td><span :class="txClass(t.type)">{{ txText(t.type) }}</span></td>
            <td style="font-family:monospace;font-size:12px">{{ t.orderId }}</td>
            <td>{{ t.summary }}</td>
            <td>{{ t.platform }}</td>
            <td class="ps-col-num" :class="txClass(t.type)">{{ amtPrefix(t.type) }}¥{{ t.amount.toLocaleString() }}</td>
            <td>{{ t.payMethod }}</td>
            <td><span class="badge" :class="statusClass(t.status)">{{ t.status }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
    <ecom-pagination :page="fin.page" :total-pages="totalPages" :total="filtered.length" @change="goPage"></ecom-pagination>
  </div>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _finApp = null;

  function mountFinanceVue() {
    if (_finApp) return;
    var mount = document.getElementById('page-finance');
    if (!mount) return;
    _fin.data = _genData();       // 每次进入重新生成假数据（对齐旧版 renderFinance）
    _fin.page = 1;                // 对齐旧版 renderFinance 里 _finPage=1
    _fin.searchLocked = true;     // 每次挂载重置只读，防浏览器自动填充
    _fin.updateTime = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    _finApp = Vue.createApp(FinancePage);
    _finApp.mount(mount);
  }

  function unmountFinanceVue() {
    if (!_finApp) return;
    _finApp.unmount();
    _finApp = null;
    var mount = document.getElementById('page-finance');
    if (mount) mount.innerHTML = '';
  }

  function initHook() {
    var container = document.getElementById('page-finance');
    if (!container) return;
    var isVisible = !container.classList.contains('hidden');
    if (isVisible) { mountFinanceVue(); return; }
    var observer = new MutationObserver(function () {
      var nowVisible = !container.classList.contains('hidden');
      if (nowVisible && !_finApp) mountFinanceVue();
      else if (!nowVisible && _finApp) unmountFinanceVue();
    });
    observer.observe(container, { attributes: true, attributeFilter: ['class'] });
  }

  initHook();
})();
