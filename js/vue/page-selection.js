// ==================== 选品助手 Vue 版（第一步：左侧 6 榜单 + 4 Cookie 面板 + 爬虫抓取） ====================
// 第二步将迁移右侧 AI 选品智能体 + 历史记录，本步右侧先放占位。
// 所有接口均已封装在 ApiService，本步零补接口。
// classic script IIFE，复用全局 ApiService / App.showToast / EcomUI.Pagination。
(function () {
  if (typeof Vue === 'undefined' || typeof ApiService === 'undefined' || typeof App === 'undefined') return;

  // ---- 市场抓取配置（label / 是否有销量列 / 接口方法） ----
  var _MARKET = {
    tmall: {
      label: '天猫市场',
      hasSales: true,
      trigger: function (kw) { return ApiService.triggerTmallMarketScrape(kw); },
      getData: function () { return ApiService.getTmallMarketData(); },
      getStatus: function () { return ApiService.getTmallMarketStatus(); },
      hint: '抓取中，首次运行需在弹出的 Chrome 里扫码登录淘宝...',
      emptyMsg: '抓取结果为空（可能未登录或该关键词无结果）',
    },
    douyin: {
      label: '抖音市场',
      hasSales: true,
      trigger: function (kw) { return ApiService.triggerDouyinMarketScrape(kw); },
      getData: function () { return ApiService.getDouyinMarketData(); },
      getStatus: function () { return ApiService.getDouyinMarketStatus(); },
      hint: '抓取中，正在后台执行抖音商城销量前50抓取...',
      emptyMsg: '抓取结果为空（该关键词暂无销量数据）',
    },
    '1688': {
      label: '1688市场',
      hasSales: false,
      trigger: function (kw) { return ApiService.trigger1688MarketScrape(kw); },
      getData: function () { return ApiService.get1688MarketData(); },
      getStatus: function () { return ApiService.get1688MarketStatus(); },
      hint: '抓取中，正在后台执行1688前10页抓取（约需2~5分钟）...',
      emptyMsg: '抓取结果为空（可能未保存 Cookie 或该关键词无结果）',
    },
  };

  // ---- Cookie 面板配置 ----
  var _COOKIES = {
    dyHot: { get: 'getDouyinHotCookie', save: 'saveDouyinHotCookie', label: '抖音热点宝 Cookie' },
    tm: { get: 'getTmallMarketCookie', save: 'saveTmallMarketCookie', label: '淘宝 Cookie' },
    aisou: { get: 'getAisouCookie', save: 'saveAisouCookie', label: '爱搜登录态' },
    m1688: { get: 'get1688MarketCookie', save: 'save1688MarketCookie', label: '1688 Cookie' },
  };

  // 历史记录翻页序号：每次切页自增，回来时若序号已变则丢弃过期响应（防连点错配）
  var _histSeq = 0;

  // ---- 模块级状态（跨挂载/卸载保留，切走再回来数据不丢） ----
  var _st = Vue.reactive({
    activeTab: 'tmall',
    tmall: { items: [], total: 0, page: 1, pageSize: 50, minPrice: '', maxPrice: '' },
    douyin: { items: [], total: 0, dates: [], date: '', scraping: false },
    aisou: { items: [], total: 0, dates: [], date: '', keyword: '' },
    markets: {
      tmall: { keyword: '', data: null, scraping: false, progress: 0, progressText: '', showProgress: false },
      douyin: { keyword: '', data: null, scraping: false, progress: 0, progressText: '', showProgress: false },
      '1688': { keyword: '', data: null, scraping: false, progress: 0, progressText: '', showProgress: false },
    },
    cookiePanels: { dyHot: false, tm: false, aisou: false, m1688: false },
    cookies: { dyHot: '', tm: '', aisou: '', m1688: '' },
    agent: {
      busy: false,
      mode: 'empty',   // empty | loading | cards | final | rising | error
      loadingText: '',
      errorText: '',
      cards: [],
      priceRange: { min: null, max: null },
      priceLabel: '',
      risingCards: [],
      final: null,
    },
    priceModal: { open: false, min: '', max: '' },
    cardsPanel: { open: false },
    // 历史选品记录：累计的次数一行一条，前端一页展示一次运行
    historyPanel: {
      open: false,
      runs: 0,        // 累计运行次数（后端 total）
      page: 1,        // 当前第几次运行（= 第几页）
      totalPages: 1,
      meta: null,     // 当前这次运行的摘要（时间 / 价格区间 / 入选商品）
      body: null,     // 当前这次运行的完整结果（selection-final 用）
      loading: false,
      error: '',
    },
  });

  // ---- 格式化辅助 ----
  function tmallPrice(r) {
    var p = r['价格'];
    if (p === null || p === undefined) return '--';
    return '¥' + Number(p).toFixed(2);
  }
  function aisouTypeColor(type) {
    if (type === '电商词') return '#0ea5e9';
    if (type === '搜索词') return '#8b5cf6';
    if (type === '相关词') return '#16a34a';
    return '#f97316';
  }
  function marketPrice(r) {
    return (r.price === null || r.price === undefined) ? '--' : r.price;
  }

  // ---- 市场抓取结果的安全访问 ----
  // 接口无数据时 data 为 null，但也可能返回结构不完整的对象（如旧版本抓取文件只剩 {keyword}）。
  // 模板里直接写 data.products.length 会抛 TypeError，而 Vue 渲染函数一旦抛错，
  // 整个组件都会渲染失败 → 页面完全空白。故统一走这两个函数兜底。
  function marketRows(data) {
    return (data && Array.isArray(data.products)) ? data.products : [];
  }
  function marketBadge(data) {
    if (!data) return '尚未抓取';
    var n = (data.count !== undefined && data.count !== null)
      ? data.count
      : marketRows(data).length;
    return '共 ' + n + ' 条';
  }

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // ---- 最终选品结果子组件（实时结果区 + 历史记录面板共用） ----
  var SelectionFinal = {
    props: { final: Object, title: { type: String, default: '' } },
    computed: {
      // 标题文案在 JS 里拼好，模板只渲染字符串（模板不裸访问嵌套属性）
      headText: function () {
        var f = this.final || {};
        var extra = f.priceLabel || f.date || '';
        var label = this.title || '当日选品结果';
        return extra ? (label + ' · ' + extra) : label;
      },
    },
    template: `
<div v-if="final && final.data">
  <div class="ps-final-head"><i class="fa-solid fa-clipboard-check" style="color:#16a34a"></i> {{ headText }}</div>
  <div v-for="(p, i) in final.data.products" :key="i" class="ps-big-card">
    <div class="ps-big-head"><span class="ps-big-name">{{ p.name }}</span><span class="ps-big-cat">{{ p.category }}</span></div>
    <div v-if="p.summary" class="ps-big-sum">{{ p.summary }}</div>
    <div v-if="p.price_segments && p.price_segments.length" class="ps-segs">
      <span v-for="(s, j) in p.price_segments" :key="j" class="ps-seg">{{ s.range }} · 销量 {{ s.sales || '--' }}</span>
    </div>
    <div v-if="p.profit" class="ps-profit"><i class="fa-solid fa-coins" style="color:#f59e0b"></i> {{ p.profit }}</div>
    <div v-if="p.variants && p.variants.length" class="ps-variants">
      <div v-for="(v, k) in p.variants" :key="k" class="ps-variant"><span class="ps-v-name">{{ v.name }}</span><span v-if="v.summary" class="ps-v-sum">{{ v.summary }}</span></div>
    </div>
  </div>
  <div v-if="final.data.advice" class="ps-advice"><div class="ps-advice-title"><i class="fa-solid fa-file-lines"></i> 当日选品建议</div>{{ final.data.advice }}</div>
</div>
<div v-else class="sa-analysis">未获取到选品结果。</div>
`,
  };

  // ---- 选品助手页组件 ----
  var SelectionPage = {
    components: { 'ecom-pagination': EcomUI.Pagination, 'selection-final': SelectionFinal },
    setup: function () {
      // ============ 天猫榜单 ============
      async function loadTmall() {
        var data = await ApiService.getTmallList(_st.tmall.minPrice, _st.tmall.maxPrice, _st.tmall.page, _st.tmall.pageSize);
        _st.tmall.items = (data && data.items) || [];
        _st.tmall.total = data ? (data.total || 0) : 0;
        var tp = Math.max(1, Math.ceil(_st.tmall.total / _st.tmall.pageSize));
        if (_st.tmall.page > tp) _st.tmall.page = tp;
      }
      function filterTmall() { _st.tmall.page = 1; loadTmall(); }
      function resetTmall() { _st.tmall.minPrice = ''; _st.tmall.maxPrice = ''; _st.tmall.page = 1; loadTmall(); }
      function tmallGoPage(p) { _st.tmall.page = p; loadTmall(); }

      // ============ 抖音热搜榜 ============
      async function loadDouyin() {
        var data = await ApiService.getDouyinList(_st.douyin.date);
        if (data && data.dates && data.dates.length) {
          var dates = data.dates;
          if (!_st.douyin.date) _st.douyin.date = dates[0];
          _st.douyin.dates = dates;
        } else {
          _st.douyin.dates = [];
        }
        _st.douyin.items = (data && data.items) || [];
        _st.douyin.total = data ? (data.total || 0) : 0;
      }
      function filterDouyin() { loadDouyin(); }

      async function dyHotScrape() {
        if (_st.douyin.scraping) return;
        var res = await ApiService.triggerDouyinHotScrape();
        if (!res) { App.showToast('触发失败，请查看后端日志', 'error'); return; }
        App.showToast('已触发热点宝抓取，完成后自动筛选', 'success');
        _st.douyin.scraping = true;
        for (var i = 0; i < 300; i++) {
          await new Promise(function (r) { setTimeout(r, 3000); });
          var st = await ApiService.getDouyinHotStatus();
          if (st && st.status === 'done') {
            App.showToast('抓取并筛选完成：同步 ' + (st.matched || 0) + ' 条电商热搜', 'success');
            await loadDouyin();
            break;
          }
          if (st && st.status === 'error') {
            App.showToast('抓取失败：' + (st.message || ''), 'error');
            break;
          }
        }
        _st.douyin.scraping = false;
      }

      // ============ 爱搜数据 ============
      async function loadAisou() {
        var data = await ApiService.getAisouList(_st.aisou.date, _st.aisou.keyword);
        if (data && data.dates && data.dates.length) {
          var dates = data.dates;
          if (!_st.aisou.date) _st.aisou.date = dates[0];
          _st.aisou.dates = dates;
        } else {
          _st.aisou.dates = [];
        }
        _st.aisou.items = (data && data.items) || [];
        _st.aisou.total = data ? (data.total || 0) : 0;
      }
      function filterAisou() { loadAisou(); }
      function resetAisou() { _st.aisou.keyword = ''; _st.aisou.date = ''; loadAisou(); }

      // ============ 市场抓取（tmall / douyin / 1688） ============
      async function loadMarket(platform) {
        var c = _MARKET[platform];
        var data = await c.getData();
        var m = _st.markets[platform];
        m.data = data;
        if (data && data.keyword) m.keyword = data.keyword;
      }

      async function triggerMarketScrape(platform) {
        var c = _MARKET[platform];
        var m = _st.markets[platform];
        if (m.scraping) return;
        var keyword = (m.keyword || '').trim();
        if (!keyword) { App.showToast('请输入要抓取的商品关键词', 'error'); return; }

        m.scraping = true;
        m.showProgress = true;
        m.progress = 2;
        m.progressText = c.hint;

        try {
          var res = await c.trigger(keyword);
          if (!res) { App.showToast('触发失败，请查看后端日志', 'error'); m.showProgress = false; return; }
          App.showToast('已触发' + c.label + '抓取：' + keyword, 'success');
          for (var i = 0; i < 150; i++) {
            await new Promise(function (r) { setTimeout(r, 3000); });
            var st = await c.getStatus();
            if (st) {
              if (st.status === 'error') {
                m.showProgress = false;
                App.showToast(c.label + '抓取失败：' + (st.error || '未知错误'), 'error');
                return;
              }
              m.progress = st.progress || 0;
              m.progressText = '抓取中 ' + (st.done || 0) + '/' + (st.total || 50);
              if (st.finished) {
                var data = await c.getData();
                m.data = data;
                if (data && data.keyword) m.keyword = data.keyword;
                var n = (data && data.count !== undefined) ? data.count : (data && data.products ? data.products.length : 0);
                m.progress = 100;
                m.progressText = '抓取完成：共 ' + n + ' 条';
                App.showToast(c.label + '抓取完成：共 ' + n + ' 条', 'success');
                setTimeout(function () { m.showProgress = false; }, 3000);
                return;
              }
            }
          }
          m.showProgress = false;
          App.showToast('抓取超时，请确认后重试', 'error');
        } catch (e) {
          m.showProgress = false;
          App.showToast('抓取请求异常', 'error');
        } finally {
          m.scraping = false;
        }
      }

      // ============ Cookie 面板 ============
      function toggleCookie(key) {
        _st.cookiePanels[key] = !_st.cookiePanels[key];
        if (_st.cookiePanels[key]) {
          ApiService[_COOKIES[key].get]().then(function (data) {
            if (data && data.cookie) _st.cookies[key] = data.cookie;
          });
        }
      }
      async function saveCookie(key) {
        var cookie = (_st.cookies[key] || '').trim();
        if (!cookie) { App.showToast('请输入 Cookie', 'error'); return; }
        var res = await ApiService[_COOKIES[key].save](cookie);
        App.showToast(res !== null ? _COOKIES[key].label + ' 已保存' : '保存失败', res !== null ? 'success' : 'error');
        _st.cookiePanels[key] = false;
      }

      // ============ Tab 切换 ============
      function switchTab(tab) { _st.activeTab = tab; }

      // ============ 右侧 AI 智能体：当日热搜选品分析 ============
      function openPriceModal() { _st.priceModal.open = true; }
      function closePriceModal() { _st.priceModal.open = false; }

      async function confirmPrice() {
        var rawMin = _st.priceModal.min, rawMax = _st.priceModal.max;
        var min = (rawMin === '' || rawMin === null || rawMin === undefined) ? '' : String(rawMin).trim();
        var max = (rawMax === '' || rawMax === null || rawMax === undefined) ? '' : String(rawMax).trim();
        if (!min && !max) { App.showToast('请至少填写一个价格边界', 'error'); return; }
        var mn = min === '' ? undefined : Number(min);
        var mx = max === '' ? undefined : Number(max);
        _st.agent.priceRange = { min: mn, max: mx };
        closePriceModal();
        if (_st.agent.busy) return;
        _st.agent.busy = true;
        _st.agent.mode = 'loading';
        _st.agent.loadingText = '正在分析当日热搜选品，请稍候...';
        try {
          var data = await ApiService.runSelection({ minPrice: mn, maxPrice: mx });
          if (!data || !(data.cards && data.cards.length)) {
            _st.agent.mode = 'error';
            _st.agent.errorText = '智能体未返回有效商品，请稍后重试。';
            return;
          }
          _st.agent.cards = data.cards.map(function (c) {
            return { name: c.name, title: c.title, category: c.category, price_range: c.price_range, reason: c.reason, checked: false };
          });
          var pr = data.priceRange || {};
          _st.agent.priceLabel = '价格区间 ' + (pr.min !== null && pr.min !== undefined ? '¥' + pr.min : '不限') + ' ~ ' + (pr.max !== null && pr.max !== undefined ? '¥' + pr.max : '不限');
          _st.agent.mode = 'cards';
        } catch (e) {
          _st.agent.mode = 'error';
          _st.agent.errorText = '分析失败：网络异常，请稍后再试。';
        } finally {
          _st.agent.busy = false;
        }
      }

      function expandCards() { _st.cardsPanel.open = true; }
      function closeCardsPanel() { _st.cardsPanel.open = false; }

      var checkedCount = Vue.computed(function () {
        return _st.agent.cards.filter(function (c) { return c.checked; }).length;
      });

      async function submitCards() {
        var selected = _st.agent.cards.filter(function (c) { return c.checked; }).map(function (c) { return c.name || c.title || ''; }).filter(Boolean);
        if (!selected.length) { App.showToast('请先勾选商品', 'error'); return; }
        if (selected.length > 10) { App.showToast('最多勾选 10 个', 'error'); return; }
        closeCardsPanel();
        _st.agent.mode = 'loading';
        _st.agent.loadingText = '已提交 ' + selected.length + ' 个商品，正在爱搜/天猫过数据...';
        var epoch = _agentEpoch;
        try {
          var res = await ApiService.runSelectionAnalyze(selected, _st.agent.priceRange);
          if (epoch !== _agentEpoch) return; // 页面已卸载
          if (!res) { _st.agent.mode = 'error'; _st.agent.errorText = '提交失败，请查看后端日志。'; return; }
          for (var i = 0; i < 600; i++) {
            if (epoch !== _agentEpoch) return;
            await sleep(3000);
            if (epoch !== _agentEpoch) return;
            var st = await ApiService.getSelectionStatus();
            if (st && st.status === 'done') {
              var final = await ApiService.getSelectionResult();
              if (epoch !== _agentEpoch) return;
              _st.agent.final = final;
              _st.agent.mode = 'final';
              return;
            }
            if (st && st.status === 'error') {
              _st.agent.mode = 'error';
              _st.agent.errorText = '选品分析失败：' + (st.message || '');
              return;
            }
            _st.agent.loadingText = (st && st.message) ? st.message : '处理中';
          }
          _st.agent.mode = 'error';
          _st.agent.errorText = '选品分析超时，请稍后查看历史记录。';
        } catch (e) {
          if (epoch !== _agentEpoch) return;
          _st.agent.mode = 'error';
          _st.agent.errorText = '提交异常：网络错误。';
        }
      }

      // ============ 右侧 AI 智能体：爱搜上升词 ============
      async function rising() {
        if (_st.agent.busy) return;
        _st.agent.busy = true;
        _st.agent.mode = 'loading';
        _st.agent.loadingText = '正在分析爱搜上升词，请稍候...';
        try {
          var data = await ApiService.runRising();
          if (!data || !(data.cards && data.cards.length)) {
            _st.agent.mode = 'error';
            _st.agent.errorText = '未分析出上升词，请先由智能体抓取爱搜数据。';
            return;
          }
          _st.agent.risingCards = data.cards;
          _st.agent.mode = 'rising';
        } catch (e) {
          _st.agent.mode = 'error';
          _st.agent.errorText = '分析失败：网络异常。';
        } finally {
          _st.agent.busy = false;
        }
      }

      // ============ 历史选品记录（一页 = 一次运行，只累计不覆盖） ============
      function historyToggle() {
        if (_st.historyPanel.open) { _st.historyPanel.open = false; return; }
        _st.historyPanel.open = true;
        loadHistoryPage(_st.historyPanel.page || 1);
      }
      function closeHistory() { _st.historyPanel.open = false; }

      // 翻页：始终请求「每页 1 条」，页号即第几次运行
      async function loadHistoryPage(page) {
        var seq = ++_histSeq;
        var hp = _st.historyPanel;
        hp.loading = true;
        hp.error = '';
        var data = await ApiService.getHistoryRuns(page, 1);
        if (seq !== _histSeq) return;          // 已连点翻到别页 → 丢弃过期响应
        hp.loading = false;
        if (!data || !Array.isArray(data.items)) {
          hp.runs = 0; hp.page = 1; hp.totalPages = 1; hp.meta = null; hp.body = null;
          hp.error = '历史记录加载失败，请稍后重试';
          return;
        }
        hp.runs = data.total || 0;
        hp.page = data.page || page || 1;
        hp.totalPages = data.totalPages || 1;
        var run = data.items[0] || null;
        if (!run) { hp.meta = null; hp.body = null; return; }
        hp.meta = {
          id: run.id,
          seq: run.seq || hp.page,
          date: run.date || '',
          createdAt: run.createdAt || run.date || '',
          priceLabel: run.priceLabel || '',
          productCount: run.productCount || 0,
          products: run.products || [],
        };
        hp.body = (run.result && run.result.data) ? run.result : null;
      }
      function historyGoPage(p) {
        var tp = _st.historyPanel.totalPages || 1;
        if (!p || p < 1 || p > tp || p === _st.historyPanel.page) return;
        loadHistoryPage(p);
      }
      // 入选商品摘要（模板里不裸访问嵌套属性）
      function runProductsText(meta) {
        if (!meta || !meta.products || !meta.products.length) return '';
        var txt = meta.products.join(' / ');
        if (meta.productCount > meta.products.length) txt += ' 等 ' + meta.productCount + ' 个';
        return txt;
      }
      function runTimeText(meta) {
        if (!meta) return '';
        return meta.createdAt || meta.date || '';
      }

      // ============ 初始化：一次性加载 6 个榜单 ============
      loadTmall();
      loadDouyin();
      loadAisou();
      loadMarket('tmall');
      loadMarket('douyin');
      loadMarket('1688');

      return {
        state: _st,
        switchTab,
        tmallPrice, aisouTypeColor, marketPrice,
        marketRows, marketBadge,
        loadTmall, filterTmall, resetTmall, tmallGoPage,
        loadDouyin, filterDouyin, dyHotScrape,
        loadAisou, filterAisou, resetAisou,
        loadMarket, triggerMarketScrape,
        toggleCookie, saveCookie,
        openPriceModal, closePriceModal, confirmPrice,
        expandCards, closeCardsPanel, submitCards, checkedCount,
        rising, historyToggle, closeHistory, loadHistoryPage, historyGoPage,
        runProductsText, runTimeText,
      };
    },

    template: `
<div class="sel-layout">
  <div class="sel-left">
    <div class="dashboard-header">
      <div class="dh-left">
        <div class="dh-icon" style="background:linear-gradient(135deg,#0ea5e9,#22d3ee);box-shadow:0 2px 8px rgba(14,165,233,0.25)"><i class="fa-solid fa-box-open" style="color:#fff;font-size:18px"></i></div>
        <div class="dh-title-group"><h2 class="dh-title">选品助手</h2><span class="dh-subtitle">Product Selection Assistant</span><span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>数据实时 · 天猫榜单 / 抖音热搜榜 / 爱搜数据</span></div>
      </div>
    </div>

    <div class="sel-tabs">
      <button class="sel-tab" :class="{ active: state.activeTab === 'tmall' }" @click="switchTab('tmall')"><i class="fa-solid fa-ranking-star" style="margin-right:6px"></i>天猫榜单</button>
      <button class="sel-tab" :class="{ active: state.activeTab === 'douyin' }" @click="switchTab('douyin')"><i class="fa-solid fa-fire" style="margin-right:6px"></i>抖音热搜榜</button>
      <button class="sel-tab" :class="{ active: state.activeTab === 'aisou' }" @click="switchTab('aisou')"><i class="fa-solid fa-magnifying-glass-chart" style="margin-right:6px"></i>爱搜数据</button>
      <button class="sel-tab" :class="{ active: state.activeTab === 'tmall-market' }" @click="switchTab('tmall-market')"><i class="fa-solid fa-store" style="margin-right:6px"></i>天猫市场</button>
      <button class="sel-tab" :class="{ active: state.activeTab === 'douyin-market' }" @click="switchTab('douyin-market')"><i class="fa-brands fa-tiktok" style="margin-right:6px"></i>抖音市场</button>
      <button class="sel-tab" :class="{ active: state.activeTab === '1688-market' }" @click="switchTab('1688-market')"><i class="fa-solid fa-warehouse" style="margin-right:6px"></i>1688市场</button>
    </div>

    <!-- ====== 天猫榜单 ====== -->
    <div v-show="state.activeTab === 'tmall'">
      <div class="ps-table-card">
        <div class="ps-table-header">
          <div class="ps-table-title-group">
            <h3><i class="fa-solid fa-ranking-star" style="color:#0ea5e9;margin-right:6px"></i>天猫榜单</h3>
            <span class="ps-table-badge" style="background:#e0f2fe;color:#0ea5e9">共 {{ state.tmall.total }} 条</span>
          </div>
          <div class="ps-table-tools">
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:0.82rem;color:#94a3b8">价格区间</span>
              <input type="number" v-model="state.tmall.minPrice" min="0" step="0.01" placeholder="最低价" style="border:1px solid #e2e8f0;border-radius:8px;padding:5px 10px;height:33px;font-size:0.82rem;color:#334155;font-family:inherit;outline:none;width:90px">
              <span style="font-size:0.82rem;color:#94a3b8">-</span>
              <input type="number" v-model="state.tmall.maxPrice" min="0" step="0.01" placeholder="最高价" style="border:1px solid #e2e8f0;border-radius:8px;padding:5px 10px;height:33px;font-size:0.82rem;color:#334155;font-family:inherit;outline:none;width:90px">
              <button class="ap-btn-primary" @click="filterTmall" style="height:33px"><i class="fa-solid fa-filter"></i> 筛选</button>
              <button class="ap-btn-sm" @click="resetTmall" style="height:33px;background:#fff">重置</button>
            </div>
          </div>
        </div>
        <div class="ps-table-wrap">
          <table class="ps-store-table">
            <thead><tr>
              <th>类别名</th><th>排行榜名</th><th>产品名</th>
              <th class="ps-col-num">价格</th><th>日期</th>
            </tr></thead>
            <tbody v-if="state.tmall.items.length">
              <tr v-for="(r, i) in state.tmall.items" :key="i">
                <td>{{ r['类别名'] }}</td>
                <td>{{ r['排行榜名'] }}</td>
                <td style="font-weight:600">{{ r['产品名'] }}</td>
                <td class="ps-col-num" style="font-weight:600;color:#0ea5e9">{{ tmallPrice(r) }}</td>
                <td>{{ r['日期'] }}</td>
              </tr>
            </tbody>
            <tbody v-else><tr><td colspan="5" style="text-align:center;color:#94a3b8;padding:32px">暂无数据</td></tr></tbody>
          </table>
        </div>
        <ecom-pagination :page="state.tmall.page" :total-pages="Math.max(1, Math.ceil(state.tmall.total / state.tmall.pageSize))" :total="state.tmall.total" @change="tmallGoPage"></ecom-pagination>
      </div>
    </div>

    <!-- ====== 抖音热搜榜 ====== -->
    <div v-show="state.activeTab === 'douyin'">
      <div class="ps-table-card">
        <div class="ps-table-header">
          <div class="ps-table-title-group">
            <h3><i class="fa-solid fa-fire" style="color:#ef4444;margin-right:6px"></i>抖音热搜榜</h3>
            <span class="ps-table-badge" style="background:#fee2e2;color:#ef4444">共 {{ state.douyin.total }} 条</span>
          </div>
          <div class="ps-table-tools">
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:0.82rem;color:#94a3b8">日期</span>
              <select v-model="state.douyin.date" @change="filterDouyin" class="ps-select-sm" style="height:34px;min-width:150px">
                <option v-for="d in state.douyin.dates" :key="d" :value="d">{{ d }}</option>
                <option v-if="state.douyin.dates.length === 0" value="" disabled selected>加载中...</option>
              </select>
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <button class="ap-btn-primary" :disabled="state.douyin.scraping" @click="dyHotScrape" style="height:34px">
                <i class="fa-solid" :class="state.douyin.scraping ? 'fa-spinner fa-spin' : 'fa-bolt'"></i> {{ state.douyin.scraping ? '抓取中...' : '抓取并筛选' }}
              </button>
              <div style="position:relative">
                <button class="ap-btn-sm" @click="toggleCookie('dyHot')" style="height:34px;background:#fff"><i class="fa-solid fa-cookie-bite" style="color:#ef4444"></i> 抖音 Cookie</button>
                <div v-show="state.cookiePanels.dyHot" style="position:absolute;top:calc(100% + 8px);right:0;z-index:90;width:460px;max-width:90vw;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.15);padding:16px 18px">
                  <div style="font-size:13px;font-weight:600;color:#1e293b;margin-bottom:8px;display:flex;align-items:center;gap:6px"><i class="fa-solid fa-cookie-bite" style="color:#ef4444"></i>抖音热点宝 Cookie</div>
                  <textarea v-model="state.cookies.dyHot" placeholder="粘贴抖音网页版登录态 Cookie（用于热点宝热搜抓取）..." style="width:100%;min-height:60px;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;resize:vertical;outline:none;color:#334155;line-height:1.6;box-sizing:border-box"></textarea>
                  <div style="display:flex;gap:10px;margin-top:12px">
                    <button class="btn btn-primary btn-sm" @click="saveCookie('dyHot')"><i class="fa-solid fa-floppy-disk"></i> 保存 Cookie</button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="ps-table-wrap">
          <table class="ps-store-table">
            <thead><tr>
              <th style="width:60px">排名</th><th>热搜名</th><th class="ps-col-num">热搜值</th><th>品类</th><th>日期</th>
            </tr></thead>
            <tbody v-if="state.douyin.items.length">
              <tr v-for="(r, i) in state.douyin.items" :key="i">
                <td style="color:#94a3b8">{{ i + 1 }}</td>
                <td style="font-weight:600">{{ r['热搜名'] }}</td>
                <td class="ps-col-num" style="font-weight:600;color:#ef4444">{{ r['热搜值'] }}</td>
                <td><span v-if="r['品类']">{{ r['品类'] }}</span><span v-else style="color:#cbd5e1">--</span></td>
                <td>{{ r['日期'] }}</td>
              </tr>
            </tbody>
            <tbody v-else><tr><td colspan="5" style="text-align:center;color:#94a3b8;padding:32px">暂无数据</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ====== 爱搜数据 ====== -->
    <div v-show="state.activeTab === 'aisou'">
      <div class="ps-table-card">
        <div class="ps-table-header">
          <div class="ps-table-title-group">
            <h3><i class="fa-solid fa-magnifying-glass-chart" style="color:#8b5cf6;margin-right:6px"></i>爱搜数据</h3>
            <span class="ps-table-badge" style="background:#f3e8ff;color:#8b5cf6">共 {{ state.aisou.total }} 条</span>
          </div>
          <div class="ps-table-tools">
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:0.82rem;color:#94a3b8">日期</span>
              <select v-model="state.aisou.date" @change="filterAisou" class="ps-select-sm" style="height:34px;min-width:150px">
                <option value="all">全部日期</option>
                <option v-for="d in state.aisou.dates" :key="d" :value="d">{{ d }}</option>
              </select>
              <div class="ps-search-wrap"><i class="fa-solid fa-search"></i><input type="text" class="ps-search-input" v-model="state.aisou.keyword" placeholder="词名称 / 来源词..."></div>
              <button class="ap-btn-primary" @click="filterAisou" style="height:34px"><i class="fa-solid fa-filter"></i> 筛选</button>
              <button class="ap-btn-sm" @click="resetAisou" style="height:34px;background:#fff">重置</button>
            </div>
            <div style="position:relative">
              <button class="ap-btn-sm" @click="toggleCookie('aisou')" style="height:34px;background:#fff"><i class="fa-solid fa-cookie-bite" style="color:#8b5cf6"></i> 爱搜 Cookie</button>
              <div v-show="state.cookiePanels.aisou" style="position:absolute;top:calc(100% + 8px);right:0;z-index:90;width:460px;max-width:90vw;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.15);padding:16px 18px">
                <div style="font-size:13px;font-weight:600;color:#1e293b;margin-bottom:8px;display:flex;align-items:center;gap:6px"><i class="fa-solid fa-key" style="color:#8b5cf6"></i>爱搜登录态</div>
                <textarea v-model="state.cookies.aisou" placeholder="登录 dso.aidso.com 后，在浏览器控制台执行 JSON.stringify(localStorage)，把输出整段粘贴到这里..." style="width:100%;min-height:60px;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;resize:vertical;outline:none;color:#334155;line-height:1.6;box-sizing:border-box"></textarea>
                <div style="font-size:11px;color:#94a3b8;margin-top:6px">爱搜登录态存在 localStorage（token），非 cookie。粘贴 JSON.stringify(localStorage) 的整段输出即可。</div>
                <div style="display:flex;gap:10px;margin-top:12px">
                  <button class="btn btn-primary btn-sm" @click="saveCookie('aisou')"><i class="fa-solid fa-floppy-disk"></i> 保存登录态</button>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div class="ps-table-wrap">
          <table class="ps-store-table">
            <thead><tr>
              <th>日期</th><th>来源词</th><th>词类型</th><th>词名称</th>
              <th class="ps-col-num">月覆盖人次</th><th class="ps-col-num">七日搜索人次</th>
            </tr></thead>
            <tbody v-if="state.aisou.items.length">
              <tr v-for="(r, i) in state.aisou.items" :key="i">
                <td>{{ r['日期'] }}</td>
                <td style="font-weight:600">{{ r['来源词'] }}</td>
                <td><span style="font-size:11px;padding:2px 8px;border-radius:999px;background:#f1f5f9;color:{{ aisouTypeColor(r['词类型']) }}">{{ r['词类型'] }}</span></td>
                <td style="font-weight:600">{{ r['词名称'] }}</td>
                <td class="ps-col-num" style="font-weight:600;color:#8b5cf6">{{ r['月覆盖人次'] }}</td>
                <td class="ps-col-num">{{ r['七日搜索人次'] }}</td>
              </tr>
            </tbody>
            <tbody v-else><tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:32px">暂无数据（点击上方 Cookie 保存后由智能体自动抓取）</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ====== 天猫市场 ====== -->
    <div v-show="state.activeTab === 'tmall-market'">
      <div class="ps-table-card">
        <div class="ps-table-header">
          <div class="ps-table-title-group">
            <h3><i class="fa-solid fa-store" style="color:#f97316;margin-right:6px"></i>天猫市场 · 淘宝销量前50</h3>
            <span class="ps-table-badge" style="background:#fff7ed;color:#ea580c">{{ marketBadge(state.markets.tmall.data) }}</span>
          </div>
          <div class="ps-table-tools">
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:0.82rem;color:#94a3b8">关键词</span>
              <input type="text" v-model="state.markets.tmall.keyword" placeholder="输入要抓取的商品关键词，如 拼豆智能板" style="border:1px solid #e2e8f0;border-radius:8px;padding:5px 10px;height:33px;font-size:0.82rem;color:#334155;font-family:inherit;outline:none;width:230px">
              <button class="ap-btn-primary" :disabled="state.markets.tmall.scraping" @click="triggerMarketScrape('tmall')" style="height:33px"><i class="fa-solid fa-bolt"></i> 抓取销量前50</button>
              <button class="ap-btn-sm" @click="loadMarket('tmall')" style="height:33px;background:#fff">刷新</button>
            </div>
            <div style="position:relative">
              <button class="ap-btn-sm" @click="toggleCookie('tm')" style="height:33px;background:#fff"><i class="fa-solid fa-cookie-bite" style="color:#f97316"></i> Cookie</button>
              <div v-show="state.cookiePanels.tm" style="position:absolute;top:calc(100% + 8px);right:0;z-index:90;width:440px;max-width:90vw;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.15);padding:16px 18px">
                <div style="font-size:13px;font-weight:600;color:#1e293b;margin-bottom:8px;display:flex;align-items:center;gap:6px"><i class="fa-solid fa-cookie-bite" style="color:#f97316"></i>淘宝 Cookie</div>
                <textarea v-model="state.cookies.tm" placeholder="粘贴淘宝网页版登录态 Cookie（留空则抓取时改用扫码登录）..." style="width:100%;min-height:60px;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;resize:vertical;outline:none;color:#334155;line-height:1.6;box-sizing:border-box"></textarea>
                <div style="font-size:11px;color:#94a3b8;margin-top:6px">留空时，抓取会弹出 Chrome 扫码登录；填写后优先用 Cookie 登录。</div>
                <div style="display:flex;gap:10px;margin-top:12px">
                  <button class="btn btn-primary btn-sm" @click="saveCookie('tm')"><i class="fa-solid fa-floppy-disk"></i> 保存淘宝 Cookie</button>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div v-if="state.markets.tmall.showProgress" class="vd-progress-wrap" style="padding:0 16px 8px">
          <div class="vd-progress-bar"><div class="vd-progress-fill" :style="{ width: state.markets.tmall.progress + '%' }"></div></div>
          <div class="vd-progress-text"><span>{{ state.markets.tmall.progressText }}</span><span>{{ state.markets.tmall.progress }}%</span></div>
        </div>
        <div class="ps-table-wrap">
          <table class="ps-store-table">
            <thead><tr>
              <th style="width:50px">排名</th><th style="width:76px">封面图</th><th>标题</th>
              <th class="ps-col-num">价格</th><th class="ps-col-num">销量</th><th>店铺名</th>
            </tr></thead>
            <tbody v-if="marketRows(state.markets.tmall.data).length">
              <tr v-for="(r, i) in marketRows(state.markets.tmall.data)" :key="i">
                <td style="color:#94a3b8">{{ r.rank }}</td>
                <td><img v-if="r.image" :src="r.image" alt="" referrerpolicy="no-referrer" style="width:48px;height:48px;object-fit:cover;border-radius:6px;background:#f1f5f9" loading="lazy"><span v-else style="color:#cbd5e1">--</span></td>
                <td style="font-weight:600;max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" :title="r.title"><a v-if="r.link" :href="r.link" target="_blank" rel="noopener noreferrer" style="color:#1677ff;text-decoration:none">{{ r.title }}</a><template v-else>{{ r.title }}</template></td>
                <td class="ps-col-num" style="font-weight:600;color:#0ea5e9">{{ marketPrice(r) }}</td>
                <td class="ps-col-num">{{ r.sales || '--' }}</td>
                <td>{{ r.shop || '--' }}</td>
              </tr>
            </tbody>
            <tbody v-else><tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:32px">暂无数据，输入关键词后点击抓取按钮</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ====== 抖音市场 ====== -->
    <div v-show="state.activeTab === 'douyin-market'">
      <div class="ps-table-card">
        <div class="ps-table-header">
          <div class="ps-table-title-group">
            <h3><i class="fa-brands fa-tiktok" style="color:#FE2C55;margin-right:6px"></i>抖音市场 · 抖音商城销量前50</h3>
            <span class="ps-table-badge" style="background:#fff1f2;color:#e11d48">{{ marketBadge(state.markets.douyin.data) }}</span>
          </div>
          <div class="ps-table-tools">
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:0.82rem;color:#94a3b8">关键词</span>
              <input type="text" v-model="state.markets.douyin.keyword" placeholder="输入要抓取的商品关键词，如 汽车脚垫" style="border:1px solid #e2e8f0;border-radius:8px;padding:5px 10px;height:33px;font-size:0.82rem;color:#334155;font-family:inherit;outline:none;width:230px">
              <button class="ap-btn-primary" :disabled="state.markets.douyin.scraping" @click="triggerMarketScrape('douyin')" style="height:33px"><i class="fa-solid fa-bolt"></i> 抓取销量前50</button>
              <button class="ap-btn-sm" @click="loadMarket('douyin')" style="height:33px;background:#fff">刷新</button>
            </div>
          </div>
        </div>
        <div v-if="state.markets.douyin.showProgress" class="vd-progress-wrap" style="padding:0 16px 8px">
          <div class="vd-progress-bar"><div class="vd-progress-fill" :style="{ width: state.markets.douyin.progress + '%' }"></div></div>
          <div class="vd-progress-text"><span>{{ state.markets.douyin.progressText }}</span><span>{{ state.markets.douyin.progress }}%</span></div>
        </div>
        <div class="ps-table-wrap">
          <table class="ps-store-table">
            <thead><tr>
              <th style="width:50px">排名</th><th style="width:76px">封面图</th><th>标题</th>
              <th class="ps-col-num">价格</th><th class="ps-col-num">销量</th><th>店铺名</th>
            </tr></thead>
            <tbody v-if="marketRows(state.markets.douyin.data).length">
              <tr v-for="(r, i) in marketRows(state.markets.douyin.data)" :key="i">
                <td style="color:#94a3b8">{{ r.rank }}</td>
                <td><img v-if="r.image" :src="r.image" alt="" referrerpolicy="no-referrer" style="width:48px;height:48px;object-fit:cover;border-radius:6px;background:#f1f5f9" loading="lazy"><span v-else style="color:#cbd5e1">--</span></td>
                <td style="font-weight:600;max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" :title="r.title"><a v-if="r.link" :href="r.link" target="_blank" rel="noopener noreferrer" style="color:#1677ff;text-decoration:none">{{ r.title }}</a><template v-else>{{ r.title }}</template></td>
                <td class="ps-col-num" style="font-weight:600;color:#0ea5e9">{{ marketPrice(r) }}</td>
                <td class="ps-col-num">{{ r.sales || '--' }}</td>
                <td>{{ r.shop || '--' }}</td>
              </tr>
            </tbody>
            <tbody v-else><tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:32px">暂无数据，输入关键词后点击抓取按钮</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ====== 1688 市场 ====== -->
    <div v-show="state.activeTab === '1688-market'">
      <div class="ps-table-card">
        <div class="ps-table-header">
          <div class="ps-table-title-group">
            <h3><i class="fa-solid fa-warehouse" style="color:#0f766e;margin-right:6px"></i>1688 市场 · 前10页货源</h3>
            <span class="ps-table-badge" style="background:#ccfbf1;color:#0f766e">{{ marketBadge(state.markets['1688'].data) }}</span>
          </div>
          <div class="ps-table-tools">
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:0.82rem;color:#94a3b8">关键词</span>
              <input type="text" v-model="state.markets['1688'].keyword" placeholder="输入要抓取的商品关键词，如 汽车脚垫" style="border:1px solid #e2e8f0;border-radius:8px;padding:5px 10px;height:33px;font-size:0.82rem;color:#334155;font-family:inherit;outline:none;width:230px">
              <button class="ap-btn-primary" :disabled="state.markets['1688'].scraping" @click="triggerMarketScrape('1688')" style="height:33px"><i class="fa-solid fa-bolt"></i> 抓取前10页</button>
              <button class="ap-btn-sm" @click="loadMarket('1688')" style="height:33px;background:#fff">刷新</button>
            </div>
            <div style="position:relative">
              <button class="ap-btn-sm" @click="toggleCookie('m1688')" style="height:33px;background:#fff"><i class="fa-solid fa-cookie-bite" style="color:#0f766e"></i> Cookie</button>
              <div v-show="state.cookiePanels.m1688" style="position:absolute;top:calc(100% + 8px);right:0;z-index:90;width:440px;max-width:90vw;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.15);padding:16px 18px">
                <div style="font-size:13px;font-weight:600;color:#1e293b;margin-bottom:8px;display:flex;align-items:center;gap:6px"><i class="fa-solid fa-cookie-bite" style="color:#0f766e"></i>1688 Cookie</div>
                <textarea v-model="state.cookies.m1688" placeholder="粘贴 1688 登录态 Cookie（k1=v1; k2=v2 ...）..." style="width:100%;min-height:60px;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;resize:vertical;outline:none;color:#334155;line-height:1.6;box-sizing:border-box"></textarea>
                <div style="font-size:11px;color:#94a3b8;margin-top:6px">Cookie 需包含 _m_h5_tk 等 1688 登录态字段，否则抓取会失败。</div>
                <div style="display:flex;gap:10px;margin-top:12px">
                  <button class="btn btn-primary btn-sm" @click="saveCookie('m1688')"><i class="fa-solid fa-floppy-disk"></i> 保存 1688 Cookie</button>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div v-if="state.markets['1688'].showProgress" class="vd-progress-wrap" style="padding:0 16px 8px">
          <div class="vd-progress-bar"><div class="vd-progress-fill" :style="{ width: state.markets['1688'].progress + '%' }"></div></div>
          <div class="vd-progress-text"><span>{{ state.markets['1688'].progressText }}</span><span>{{ state.markets['1688'].progress }}%</span></div>
        </div>
        <div class="ps-table-wrap">
          <table class="ps-store-table">
            <thead><tr>
              <th style="width:50px">排名</th><th style="width:76px">封面图</th><th>标题</th>
              <th class="ps-col-num">价格</th><th>店铺名</th>
            </tr></thead>
            <tbody v-if="marketRows(state.markets['1688'].data).length">
              <tr v-for="(r, i) in marketRows(state.markets['1688'].data)" :key="i">
                <td style="color:#94a3b8">{{ r.rank }}</td>
                <td><img v-if="r.image" :src="r.image" alt="" referrerpolicy="no-referrer" style="width:48px;height:48px;object-fit:cover;border-radius:6px;background:#f1f5f9" loading="lazy"><span v-else style="color:#cbd5e1">--</span></td>
                <td style="font-weight:600;max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" :title="r.title"><a v-if="r.link" :href="r.link" target="_blank" rel="noopener noreferrer" style="color:#1677ff;text-decoration:none">{{ r.title }}</a><template v-else>{{ r.title }}</template></td>
                <td class="ps-col-num" style="font-weight:600;color:#0ea5e9">{{ marketPrice(r) }}</td>
                <td>{{ r.shop || '--' }}</td>
              </tr>
            </tbody>
            <tbody v-else><tr><td colspan="5" style="text-align:center;color:#94a3b8;padding:32px">暂无数据，输入关键词后点击抓取按钮</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>
  </div><!-- /.sel-left -->

  <!-- ====== 右侧 AI 选品智能体 ====== -->
  <div class="sel-right">
    <div class="sel-agent">
      <div class="sel-agent-header">
        <div style="flex:1;min-width:0">
          <div class="sel-agent-title"><i class="fa-solid fa-wand-magic-sparkles" style="color:#8b5cf6;margin-right:8px"></i>选品智能体</div>
          <span class="sel-agent-sub">抖音热搜 / 天猫榜单 / 爱搜数据</span>
        </div>
        <button class="ps-history-btn" @click="historyToggle"><i class="fa-solid fa-clock-rotate-left"></i> 历史选品记录</button>
      </div>

      <div class="sel-agent-suggest">
        <button class="sel-agent-chip" @click="openPriceModal">📈 当日热搜选品分析</button>
        <button class="sel-agent-chip" @click="rising">🔍 爱搜上升词</button>
      </div>

      <div class="sel-agent-result">
        <div v-if="state.agent.mode === 'empty'" class="sel-agent-empty">
          <i class="fa-solid fa-robot" style="font-size:28px;color:#cbd5e1"></i>
          <p>点击「当日热搜选品分析」输入价格区间，智能体对比抖音热搜 / 天猫榜单 / 市场客单价，输出约 50 个潜力商品，勾选 10 个后过爱搜与天猫数据，给出最终选品建议。</p>
        </div>
        <div v-else-if="state.agent.mode === 'loading'" class="sa-loading"><i class="fa-solid fa-spinner"></i> {{ state.agent.loadingText }}</div>
        <div v-else-if="state.agent.mode === 'error'" class="sa-analysis" style="color:#dc2626">{{ state.agent.errorText }}</div>
        <div v-else-if="state.agent.mode === 'cards'">
          <div class="ps-round-box">
            <div class="ps-round-head">
              <span><i class="fa-solid fa-lightbulb" style="color:#f59e0b"></i> 共筛选出 {{ state.agent.cards.length }} 个潜力商品（{{ state.agent.priceLabel }}）</span>
              <button class="ps-expand-btn" @click="expandCards"><i class="fa-solid fa-up-right-and-down-left-from-center"></i> 展开勾选</button>
            </div>
            <div class="ps-round-tip">点击「展开勾选」进入大面板，勾选 10 个看好的品后提交分析。</div>
          </div>
        </div>
        <div v-else-if="state.agent.mode === 'rising'">
          <div class="ps-round-head"><span><i class="fa-solid fa-chart-line" style="color:#8b5cf6"></i> 近期热度上升的产品词（5 个）</span></div>
          <div v-for="(c, i) in state.agent.risingCards" :key="i" class="sa-card">
            <div class="sa-card-top">
              <div class="sa-card-title">{{ c.word || c.name || '' }}</div>
              <div class="sa-card-metric">{{ c.type || '' }}</div>
            </div>
            <div v-if="c.reason" class="sa-card-reason">{{ c.reason }}</div>
          </div>
        </div>
        <div v-else-if="state.agent.mode === 'final'">
          <selection-final :final="state.agent.final"></selection-final>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ====== 弹窗 1：当日热搜选品分析（价格区间） ====== -->
<teleport to="body">
  <div v-if="state.priceModal.open" class="ps-overlay">
    <div class="ps-modal" style="width:480px">
      <div class="ps-modal-head">
        <div class="ps-modal-title"><i class="fa-solid fa-tags" style="color:#8b5cf6"></i> 当日热搜选品分析</div>
        <button class="ps-modal-close" @click="closePriceModal"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="ps-modal-body">
        <p style="font-size:13px;color:#64748b;margin-bottom:14px;line-height:1.7">请输入目标客单价区间（大众能接受的价格区间）。智能体将对比抖音热搜品类与天猫榜单价格、市场客单价，返回约 50 个潜力商品。</p>
        <div class="ps-price-inputs">
          <input type="number" v-model="state.priceModal.min" min="0" placeholder="最低价（可留空）">
          <span style="color:#94a3b8">~</span>
          <input type="number" v-model="state.priceModal.max" min="0" placeholder="最高价（可留空）">
        </div>
      </div>
      <div class="ps-modal-foot">
        <button class="btn btn-outline" @click="closePriceModal">取消</button>
        <button class="btn btn-primary" @click="confirmPrice"><i class="fa-solid fa-wand-magic-sparkles"></i> 开始分析</button>
      </div>
    </div>
  </div>
</teleport>

<!-- ====== 弹窗 2：50 卡片勾选大面板 ====== -->
<teleport to="body">
  <div v-if="state.cardsPanel.open" class="ps-overlay">
    <div class="ps-modal ps-cards-panel">
      <div class="ps-modal-head">
        <div class="ps-modal-title"><i class="fa-solid fa-list-check" style="color:#8b5cf6"></i> 潜力商品勾选</div>
        <button class="ps-modal-close" @click="closeCardsPanel"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="ps-modal-body" style="flex:1">
        <div class="ps-cards-grid">
          <label v-for="(c, i) in state.agent.cards" :key="i" class="ps-pick-card">
            <input type="checkbox" class="ps-pick-check" v-model="c.checked">
            <div class="ps-pick-body">
              <div class="ps-pick-name">{{ c.name || c.title || '' }}</div>
              <div class="ps-pick-tags"><span>{{ c.category || '' }}</span><span class="price">{{ c.price_range || '' }}</span></div>
              <div v-if="c.reason" class="ps-pick-reason">{{ c.reason }}</div>
            </div>
          </label>
        </div>
      </div>
      <div class="ps-modal-foot">
        <span style="font-size:13px;color:#64748b">已勾选 <b style="color:#8b5cf6">{{ checkedCount }}</b> / 10 个</span>
        <button class="btn btn-primary" :disabled="checkedCount === 0" @click="submitCards"><i class="fa-solid fa-check"></i> 提交这 10 个品</button>
      </div>
    </div>
  </div>
</teleport>

<!-- ====== 弹窗 3：历史选品记录（一页 = 一次运行） ====== -->
<teleport to="body">
  <div v-if="state.historyPanel.open" class="ps-overlay">
    <div class="ps-modal" style="width:780px;height:88vh">
      <div class="ps-modal-head">
        <div class="ps-modal-title"><i class="fa-solid fa-clock-rotate-left" style="color:#8b5cf6"></i> 历史选品记录</div>
        <button class="ps-modal-close" @click="closeHistory"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="ps-modal-body" style="padding-top:14px">
        <div class="ps-hist-bar">
          <span class="ps-hist-total">累计 {{ state.historyPanel.runs }} 次选品运行（每次运行独立存档，不会覆盖）</span>
          <span v-if="state.historyPanel.runs" class="ps-hist-pos">第 {{ state.historyPanel.page }} / {{ state.historyPanel.totalPages }} 次</span>
        </div>
        <div v-if="state.historyPanel.loading" class="sa-loading"><i class="fa-solid fa-spinner"></i> 正在加载历史记录...</div>
        <div v-else-if="state.historyPanel.error" class="sa-analysis" style="color:#dc2626">{{ state.historyPanel.error }}</div>
        <div v-else-if="!state.historyPanel.meta" style="color:#94a3b8;padding:24px;text-align:center">
          {{ state.historyPanel.runs ? '该次运行未取到选品结果' : '暂无选品记录' }}
        </div>
        <template v-else>
          <div class="ps-run-head">
            <div class="ps-run-line">
              <span class="ps-run-tag">第 {{ state.historyPanel.meta.seq }} 次运行</span>
              <span class="ps-run-time"><i class="fa-regular fa-clock"></i> {{ runTimeText(state.historyPanel.meta) }}</span>
              <span v-if="state.historyPanel.meta.priceLabel" class="ps-run-price">价格区间 {{ state.historyPanel.meta.priceLabel }}</span>
            </div>
            <div v-if="runProductsText(state.historyPanel.meta)" class="ps-run-products">
              入选：{{ runProductsText(state.historyPanel.meta) }}
            </div>
          </div>
          <selection-final v-if="state.historyPanel.body" :final="state.historyPanel.body" :title="'第 ' + state.historyPanel.meta.seq + ' 次运行结果'"></selection-final>
          <div v-else class="sa-analysis">该次运行存档里没有选品结果（可能当时分析失败）。</div>
        </template>
      </div>
      <div class="ps-modal-foot" style="padding:0;justify-content:center">
        <ecom-pagination :page="state.historyPanel.page" :total-pages="state.historyPanel.totalPages" :total="state.historyPanel.runs" unit="次运行" @change="historyGoPage"></ecom-pagination>
      </div>
    </div>
  </div>
</teleport>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _selApp = null;
  var _agentEpoch = 0;   // 每次卸载递增，让进行中的轮询循环失效

  function mountSelectionVue() {
    if (_selApp) return;
    var oldSection = document.getElementById('page-product-selection');
    var mount = document.getElementById('page-product-selection-vue');
    if (!oldSection || !mount) return;
    oldSection.style.display = 'none';
    mount.classList.remove('hidden');
    _selApp = Vue.createApp(SelectionPage);
    _selApp.mount(mount);
  }

  function unmountSelectionVue() {
    if (!_selApp) return;
    _agentEpoch++;                                       // 让进行中的轮询循环失效
    if (_st.agent.mode === 'loading') _st.agent.mode = 'empty';  // 瞬态 loading 不跨挂载保留
    _selApp.unmount();
    _selApp = null;
    var mount = document.getElementById('page-product-selection-vue');
    if (mount) { mount.classList.add('hidden'); mount.innerHTML = ''; }
    var oldSection = document.getElementById('page-product-selection');
    if (oldSection) oldSection.style.display = '';
  }

  function installSelectionHook() {
    var oldSection = document.getElementById('page-product-selection');
    if (!oldSection) return;
    if (!oldSection.classList.contains('hidden')) mountSelectionVue();
    var observer = new MutationObserver(function () {
      if (!oldSection.classList.contains('hidden')) mountSelectionVue();
      else unmountSelectionVue();
    });
    observer.observe(oldSection, { attributes: true, attributeFilter: ['class'] });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installSelectionHook);
  } else {
    installSelectionHook();
  }
})();
