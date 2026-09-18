/**
 * 电商后台管理系统 - 核心应用逻辑
 * E-Commerce Admin Panel
 */

// ==================== API 服务层 ====================
const ApiService = (() => {
  // 后端 API 地址（根据实际情况修改 IP 和端口）
  // 后端 API 地址 — Flask 在本机运行，用 localhost
  // 如果需要从其他电脑访问，改成 Flask 所在机器的 IP
  const BASE_URL = '/api';

  async function request(path, options = {}) {
    try {
      const res = await fetch(BASE_URL + path, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
      });
      if (res.status === 401) {
        // 登录已过期：清掉本地登录态并回到登录页
        sessionStorage.removeItem('admin_logged_in');
        sessionStorage.removeItem('admin_current_user');
        sessionStorage.removeItem('admin_current_role');
        sessionStorage.removeItem('admin_current_account');
        sessionStorage.removeItem('admin_permissions');
        location.reload();
        throw new Error('登录已过期，请重新登录');
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      if (json.code !== 0) throw new Error(json.msg);
      // 后端「成功但无返回数据」的响应是 {code:0, data:null}，而「请求失败」也是返回 null。
      // 两类调用方对返回值的用法不同，故按请求方法分别处理：
      //  - 读操作（GET）：必须原样返回 json.data —— null 就是「暂无数据」，调用方和模板依赖它走空态分支。
      //    若在此把 null 换成 true，会污染数据语义：模板里 `state.markets.x.data ? ... : 空态`
      //    会因 true 为真而走进取值分支，再访问 xxx.products.length 直接抛错，
      //    导致 Vue 渲染函数中断 → 整个组件渲染失败 → 整页空白（选品助手白屏即此原因）。
      //  - 写操作（POST/PUT/DELETE）：调用方只用返回值判断成败（`r === null` 视为失败），
      //    故成功但无数据时返回 true 作为成功标记，避免保存成功被误判为失败。
      const method = (options.method || 'GET').toUpperCase();
      if (method === 'GET') return json.data;
      return (json.data === null || json.data === undefined) ? true : json.data;
    } catch (e) {
      console.warn('[API] 请求失败:', path, e.message);
      return null;
    }
  }

  /**
   * 需要拿到后端具体报错文案的请求（保存设置、推送、测试等操作类接口）。
   * request() 只返回 null，无法把「未配置 AppSecret」这类原因透给用户，故单独提供。
   * 返回 { ok, data, msg }
   */
  async function requestFull(path, options = {}) {
    try {
      const res = await fetch(BASE_URL + path, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
      });
      if (res.status === 401) {
        sessionStorage.removeItem('admin_logged_in');
        sessionStorage.removeItem('admin_current_user');
        sessionStorage.removeItem('admin_current_role');
        sessionStorage.removeItem('admin_current_account');
        sessionStorage.removeItem('admin_permissions');
        location.reload();
        return { ok: false, msg: '登录已过期，请重新登录' };
      }
      const json = await res.json().catch(() => null);
      if (!json) return { ok: false, msg: `HTTP ${res.status}` };
      // data 也带出去：抓取触发失败时会带 {sliderNeeded:true}，前端据此挂「手动拖滑块」按钮
      if (json.code !== 0) return { ok: false, msg: json.msg || '请求失败', data: json.data };
      return { ok: true, data: json.data, msg: json.msg || '' };
    } catch (e) {
      console.warn('[API] 请求失败:', path, e.message);
      return { ok: false, msg: e.message || '网络异常' };
    }
  }

  return {
    /** 健康检查 — 判断后端是否可用 */
    async health() {
      const data = await request('/health');
      return data !== null;
    },

    /** 从远程数据库拉取全部数据并同步到 localStorage */
    async syncAll() {
      const [products, orders, customers] = await Promise.all([
        request('/products'),
        request('/orders'),
        request('/customers'),
      ]);
      if (products && orders && customers) {
        const merged = { products, orders, customers };
        localStorage.setItem('admin_data', JSON.stringify(merged));
        console.log('[API] ✅ 已从远程数据库同步 %d 商品 / %d 订单 / %d 客户',
          products.length, orders.length, customers.length);
        return merged;
      }
      return null;
    },

    /** 获取看板统计数据 */
    async getDashboardStats() {
      return request('/dashboard/stats');
    },

    /** 返回平台和品牌筛选选项 */
    async getMarketingFilters() {
      return request('/marketing/filters');
    },

    /** 获取营销数据总览，支持日期、平台、品牌筛选 */
    async getMarketingOverview(start, end, platform, brand) {
      let path = '/marketing/overview';
      const params = [];
      if (start) params.push('start=' + encodeURIComponent(start));
      if (end)   params.push('end=' + encodeURIComponent(end));
      if (platform) params.push('platform=' + encodeURIComponent(platform));
      if (brand)   params.push('brand=' + encodeURIComponent(brand));
      if (params.length) path += '?' + params.join('&');
      return request(path);
    },

    /** 获取品类营销数据，按统一品类汇总单链接成交 */
    async getCategoryMarketing(start, end) {
      let path = '/category-marketing/data';
      const params = [];
      if (start) params.push('start=' + encodeURIComponent(start));
      if (end)   params.push('end=' + encodeURIComponent(end));
      if (params.length) path += '?' + params.join('&');
      return request(path);
    },

    /** 账号列表 CRUD（department = 部门按钮对应的大角色名） */
    async getAdmins(department, keyword) {
      let path = '/admin/accounts';
      const params = [];
      if (department && department !== '全部') params.push('department=' + encodeURIComponent(department));
      if (keyword) params.push('keyword=' + encodeURIComponent(keyword));
      if (params.length) path += '?' + params.join('&');
      return request(path);
    },
    async createAdmin(data) { return requestFull('/admin/accounts', { method: 'POST', body: JSON.stringify(data) }); },
    async updateAdmin(id, data) { return requestFull('/admin/accounts/' + id, { method: 'PUT', body: JSON.stringify(data) }); },
    async deleteAdmin(id) { return request('/admin/accounts/' + id, { method: 'DELETE' }); },

    /** 单条读取账号明文密码 —— 列表不下发密码，点「眼睛」时才按 id 取这一条 */
    async getAdminPassword(id) { return requestFull('/admin/accounts/' + id + '/password'); },

    /** 角色与权限 CRUD（仅开发人员 / 超级管理员，后端有 _can_manage_roles 硬门槛） */
    async getRoles() { return request('/admin/roles'); },
    async createRole(data) { return request('/admin/roles', { method: 'POST', body: JSON.stringify(data) }); },
    async updateRole(id, data) { return request('/admin/roles/' + id, { method: 'PUT', body: JSON.stringify(data) }); },
    async deleteRole(id) { return request('/admin/roles/' + id, { method: 'DELETE' }); },

    /** 当前登录者的管辖范围（super / lead / none + 可授权限上限） */
    async getMyScope() { return request('/admin/my-scope'); },

    /** 个人中心 — 个人信息 / 修改密码 / 头像 */
    async getProfile() { return request('/profile/me'); },
    async updateProfile(data) { return requestFull('/profile/update', { method: 'POST', body: JSON.stringify(data) }); },
    async updatePassword(data) { return requestFull('/profile/password', { method: 'POST', body: JSON.stringify(data) }); },
    async uploadAvatar(file) {
      try {
        const fd = new FormData();
        fd.append('file', file);
        const res = await fetch(BASE_URL + '/profile/avatar', { method: 'POST', body: fd, credentials: 'same-origin' });
        if (res.status === 401) { location.reload(); return { ok: false, msg: '登录已过期' }; }
        const json = await res.json().catch(() => null);
        if (!json) return { ok: false, msg: 'HTTP ' + res.status };
        if (json.code !== 0) return { ok: false, msg: json.msg || '上传失败' };
        return { ok: true, data: json.data, msg: json.msg || '头像已更新' };
      } catch (e) {
        return { ok: false, msg: e.message || '网络异常' };
      }
    },

    /** 店铺账号管理：type = qianniu | doudian | doudian-email | jd */
    async getStoreAccounts(type) { return request('/store-accounts/' + type); },
    async createStoreAccount(type, data) { return request('/store-accounts/' + type, { method: 'POST', body: JSON.stringify(data) }); },
    async updateStoreAccount(type, id, data) { return request('/store-accounts/' + type + '/' + id, { method: 'PUT', body: JSON.stringify(data) }); },
    async deleteStoreAccount(type, id) { return request('/store-accounts/' + type + '/' + id, { method: 'DELETE' }); },
    async toggleStoreAccount(type, id, active) { return request('/store-accounts/' + type + '/' + id + '/toggle', { method: 'PUT', body: JSON.stringify({ active: active }) }); },

    // ---- 抓取任务（店铺账号管理「更新数据」→ 唤起 /opt/pw 抓取程序）----
    // 账号列表来源＝三张账号表的「是否运营=1」，与抓取程序 shops.py 同一份来源
    async getFetchStatus() { return request('/fetch/status'); },
    async getFetchLatestJob() { return request('/fetch/job/latest'); },
    async getFetchJob(jobId) { return request('/fetch/job/' + jobId); },
    async triggerFetch(payload) { return requestFull('/fetch/trigger', { method: 'POST', body: JSON.stringify(payload || {}) }); },
    async stopFetch(jobId) { return requestFull('/fetch/stop', { method: 'POST', body: JSON.stringify({ jobId: jobId }) }); },
    async getFetchReconcile(limit) { return request('/fetch/reconcile?limit=' + (limit || 60)); },

    // ---- 抖店「手动拖滑块」（本机常驻助手执行，见 tools/doudian_crawler/slider_agent.py）----
    // 服务器过不了滑块也没窗口能拖，所以点按钮只是「下发任务」，真正的浏览器弹在本机
    async requestDoudianSlider() { return requestFull('/fetch/doudian/slider/request', { method: 'POST', body: JSON.stringify({}) }); },
    async getDoudianSliderStatus() { return request('/fetch/doudian/slider/status'); },

    /** 员工花名册（旧接口，保留兼容） */
    async getHrEmployees() { return request('/hr/employees'); },
    async createEmployee(data) { return request('/hr/employees', { method: 'POST', body: JSON.stringify(data) }); },

    /** 人事数据中心：5 张人事表通用 CRUD */
    async getHrMeta() { return request('/hr/meta'); },
    async getHrCounts() { return request('/hr/counts'); },
    async getHrRows(key) { return request('/hr/' + encodeURIComponent(key) + '/rows'); },
    async createHrRow(key, data) { return request('/hr/' + encodeURIComponent(key) + '/rows', { method: 'POST', body: JSON.stringify(data) }); },
    async updateHrRow(key, data) { return request('/hr/' + encodeURIComponent(key) + '/rows', { method: 'PUT', body: JSON.stringify(data) }); },
    async deleteHrRow(key, data) { return request('/hr/' + encodeURIComponent(key) + '/rows', { method: 'DELETE', body: JSON.stringify(data) }); },

    /** 分平台/店铺详细数据 */
    async getPlatformStoreData(start, end, platform) {
      let path = '/platform-store/data';
      const params = [];
      if (start) params.push('start=' + encodeURIComponent(start));
      if (end)   params.push('end=' + encodeURIComponent(end));
      if (platform) params.push('platform=' + encodeURIComponent(platform));
      if (params.length) path += '?' + params.join('&');
      return request(path);
    },

    // ---- 选品助手 ----
    async getTmallList(minPrice, maxPrice, page, pageSize) {
      let path = '/product-selection/tmall';
      const params = [];
      if (minPrice !== '' && minPrice !== null && minPrice !== undefined) params.push('minPrice=' + encodeURIComponent(minPrice));
      if (maxPrice !== '' && maxPrice !== null && maxPrice !== undefined) params.push('maxPrice=' + encodeURIComponent(maxPrice));
      params.push('page=' + (page || 1));
      params.push('pageSize=' + (pageSize || 50));
      if (params.length) path += '?' + params.join('&');
      return request(path);
    },
    async getDouyinList(date) {
      let path = '/product-selection/douyin';
      if (date) path += '?date=' + encodeURIComponent(date);
      return request(path);
    },
    async getDouyinHotCookie() {
      return request('/product-selection/douyin-hot/cookie');
    },
    async saveDouyinHotCookie(cookie) {
      return request('/product-selection/douyin-hot/cookie', { method: 'POST', body: JSON.stringify({ cookie: cookie || '' }) });
    },
    async triggerDouyinHotScrape() {
      return request('/product-selection/douyin-hot/scrape', { method: 'POST', body: '{}' });
    },
    async getDouyinHotStatus() {
      return request('/product-selection/douyin-hot/status');
    },
    async getAisouCookie() {
      return request('/product-selection/aisou/cookie');
    },
    async saveAisouCookie(cookie) {
      return request('/product-selection/aisou/cookie', { method: 'POST', body: JSON.stringify({ cookie: cookie || '' }) });
    },
    async runSelection(priceRange) {
      return request('/product-selection/selection', { method: 'POST', body: JSON.stringify(priceRange || {}) });
    },
    async runSelectionAnalyze(products, priceRange) {
      return request('/product-selection/selection/analyze', {
        method: 'POST',
        body: JSON.stringify({
          products: products || [],
          minPrice: priceRange ? priceRange.min : undefined,
          maxPrice: priceRange ? priceRange.max : undefined,
        }),
      });
    },
    async getSelectionStatus() {
      return request('/product-selection/selection/status');
    },
    async getSelectionResult() {
      return request('/product-selection/selection/result');
    },
    async runRising() {
      return request('/product-selection/rising', { method: 'POST', body: '{}' });
    },
    async getHistoryDates() {
      return request('/product-selection/history/dates');
    },
    // 历史选品记录：按「单次运行」分页（每页 = 一次运行）；传 date 则只看该日期当日的记录
    async getHistoryRuns(page, pageSize, date) {
      let path = '/product-selection/history/runs?page=' + (page || 1) + '&page_size=' + (pageSize || 1);
      if (date) path += '&date=' + encodeURIComponent(date);
      return request(path);
    },
    // 按运行 id 取单次记录（备用；列表接口已直接返回完整结果）
    async getHistoryById(id) {
      return request('/product-selection/history?id=' + encodeURIComponent(id));
    },
    async getHistory(date) {
      let path = '/product-selection/history';
      if (date) path += '?date=' + encodeURIComponent(date);
      return request(path);
    },
    async getAisouList(date, keyword) {
      let path = '/product-selection/aisou';
      const params = [];
      if (date) params.push('date=' + encodeURIComponent(date));
      if (keyword) params.push('keyword=' + encodeURIComponent(keyword));
      if (params.length) path += '?' + params.join('&');
      return request(path);
    },
    async triggerTmallMarketScrape(keyword) {
      return request('/product-selection/tmall-market/scrape', {
        method: 'POST',
        body: JSON.stringify({ keyword: keyword || '' }),
      });
    },
    async getTmallMarketData() {
      return request('/product-selection/tmall-market/data');
    },
    async getTmallMarketStatus() {
      return request('/product-selection/tmall-market/status');
    },
    async getTmallMarketCookie() {
      return request('/product-selection/tmall-market/cookie');
    },
    async saveTmallMarketCookie(cookie) {
      return request('/product-selection/tmall-market/cookie', {
        method: 'POST',
        body: JSON.stringify({ cookie: cookie || '' }),
      });
    },
    async triggerDouyinMarketScrape(keyword) {
      return request('/product-selection/douyin-market/scrape', {
        method: 'POST',
        body: JSON.stringify({ keyword: keyword || '' }),
      });
    },
    async getDouyinMarketData() {
      return request('/product-selection/douyin-market/data');
    },
    async getDouyinMarketStatus() {
      return request('/product-selection/douyin-market/status');
    },
    async trigger1688MarketScrape(keyword) {
      return request('/product-selection/1688-market/scrape', {
        method: 'POST',
        body: JSON.stringify({ keyword: keyword || '' }),
      });
    },
    async get1688MarketData() {
      return request('/product-selection/1688-market/data');
    },
    async get1688MarketStatus() {
      return request('/product-selection/1688-market/status');
    },
    async get1688MarketCookie() {
      return request('/product-selection/1688-market/cookie');
    },
    async save1688MarketCookie(cookie) {
      return request('/product-selection/1688-market/cookie', {
        method: 'POST',
        body: JSON.stringify({ cookie: cookie || '' }),
      });
    },
    // ---- 每日数据分析 ----
    async generateDailyReport(date) {
      var body = date ? JSON.stringify({ date: date }) : undefined;
      return request('/analysis/generate', { method: 'POST', body: body });
    },
    async getAnalysisReport(date) {
      let path = '/analysis/report';
      if (date) path += '?date=' + encodeURIComponent(date);
      return request(path);
    },
    async getAnalysisDates() {
      return request('/analysis/dates');
    },
    async runAnalysisAgent(question, start, end) {
      return request('/analysis/agent', {
        method: 'POST',
        body: JSON.stringify({ question: question || '', start: start || '', end: end || '' }),
      });
    },

    // ---- 每日数据分析：钉钉推送设置 ----
    /** 读取推送配置（含推送人名单与最近推送记录） */
    async getPushConfig() {
      return request('/analysis/push/config');
    },
    /** 保存推送配置：AppSecret 传回掩码时后端保持原值 */
    async savePushConfig(data) {
      return requestFull('/analysis/push/config', { method: 'POST', body: JSON.stringify(data) });
    },
    async addPushUser(data) {
      return requestFull('/analysis/push/users', { method: 'POST', body: JSON.stringify(data) });
    },
    async updatePushUser(id, data) {
      return requestFull('/analysis/push/users/' + id, { method: 'PUT', body: JSON.stringify(data) });
    },
    async deletePushUser(id) {
      return requestFull('/analysis/push/users/' + id, { method: 'DELETE' });
    },
    /** 手机号 → 钉钉 userId */
    async resolvePushUser(mobile) {
      return requestFull('/analysis/push/resolve', { method: 'POST', body: JSON.stringify({ mobile: mobile || '' }) });
    },
    /** 给所有启用成员发测试消息 */
    async testPush() {
      return requestFull('/analysis/push/test', { method: 'POST', body: JSON.stringify({}) });
    },
    /** 立即生成并推送指定日期报告（不传则昨日） */
    async pushNow(date) {
      return requestFull('/analysis/push/now', { method: 'POST', body: JSON.stringify({ date: date || '' }) });
    },

    // ---- 开发自检（前端未捕获 JS 异常 → 后端转钉钉告警开发） ----
    async reportClientError(payload) {
      try {
        return await requestFull('/dev/report-error', {
          method: 'POST', body: JSON.stringify(payload || {}),
        });
      } catch (e) {
        return { ok: false, msg: '上报失败' };
      }
    },

    // ---- 种草监测中台 ----
    async getSeedingAccounts() { return request('/seeding/accounts'); },
    async createSeedingAccount(data) { return request('/seeding/accounts', { method: 'POST', body: JSON.stringify(data) }); },
    async updateSeedingAccount(id, data) { return request('/seeding/accounts/' + id, { method: 'PUT', body: JSON.stringify(data) }); },
    async deleteSeedingAccount(id) { return request('/seeding/accounts/' + id, { method: 'DELETE' }); },
    async getSeedingWorks(platform) { return request('/seeding/works' + (platform ? '?platform=' + encodeURIComponent(platform) : '')); },
    async getSeedingWorksMeta(platform) { return request('/seeding/works/meta' + (platform ? '?platform=' + encodeURIComponent(platform) : '')); },
    async getSeedingCookie(platform) { return request('/seeding/cookie' + (platform ? '?platform=' + encodeURIComponent(platform) : '')); },
    async saveSeedingCookie(platform, cookie) { return request('/seeding/cookie', { method: 'POST', body: JSON.stringify({ platform: platform || 'douyin', cookie: cookie }) }); },
    async triggerSeedingScrape(platform) { return request('/seeding/scrape', { method: 'POST', body: JSON.stringify({ platform: platform || 'douyin' }) }); },
    async getSeedingScrapeStatus(platform) { return request('/seeding/scrape/status' + (platform ? '?platform=' + encodeURIComponent(platform) : '')); },
    async runSeedingAgent(question) { return request('/seeding/agent', { method: 'POST', body: JSON.stringify({ question: question || '' }) }); },
    async getSeedingDeleted() { return request('/seeding/deleted'); },
    async deleteSeedingDeleted(id) { return request('/seeding/deleted/' + id, { method: 'DELETE' }); },
    async clearSeedingDeleted() { return request('/seeding/deleted', { method: 'DELETE' }); },
    /** 部门列表 + 部门→钉钉联系人/点赞阈值规则 + 钉钉联系人候选 */
    async getSeedingDeptConfig() { return request('/seeding/dept-config'); },
    async saveSeedingDeptConfig(data) { return request('/seeding/dept-config', { method: 'POST', body: JSON.stringify(data) }); },

    // ---- CRUD 快捷方法 ----
    create(type, data) {
      const paths = { product: '/products', order: '/orders', customer: '/customers' };
      return request(paths[type], { method: 'POST', body: JSON.stringify(data) });
    },
    update(type, id, data) {
      const paths = { product: '/products', order: '/orders', customer: '/customers' };
      return request(`${paths[type]}/${id}`, { method: 'PUT', body: JSON.stringify(data) });
    },
    del(type, id) {
      const paths = { product: '/products', order: '/orders', customer: '/customers' };
      return request(`${paths[type]}/${id}`, { method: 'DELETE' });
    },

    // ---- 订单详情（单链接销售数据） ----
    async getOrderDetailsData(params) { return request('/order-details/data?' + (params || '')); },
    async getOrderDetailsStores(platform, linkType) {
      var q = '';
      if (platform) q += 'platform=' + encodeURIComponent(platform);
      if (linkType) q += (q ? '&' : '') + 'linkType=' + encodeURIComponent(linkType);
      return request('/order-details/stores' + (q ? '?' + q : ''));
    },

    // ---- 工具箱 - 违规词检测（图片 OCR） ----
    // 说明：这两个接口返回结构特殊（顶层无 code 字段），不能复用 request()，
    // 因此在此用裸 fetch 直连，仍统一收口在 ApiService 层，不新增第二个请求层。
    async ocrDetect(images) {
      try {
        const res = await fetch(BASE_URL + '/ocr/detect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ images: images || [] }),
        });
        return await res.json();
      } catch (e) {
        console.warn('[API] OCR 识别失败:', e.message);
        return null;
      }
    },
    async violationDetect(text) {
      try {
        const res = await fetch(BASE_URL + '/violation/detect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: text || '' }),
        });
        return await res.json();
      } catch (e) {
        console.warn('[API] 违规词检测失败:', e.message);
        return null;
      }
    },
  };
})();

// ==================== 主应用 ====================

const App = (() => {
  // ==================== 配置 & 状态 ====================
  const CONFIG = {
    adminUser: { username: 'admin', password: 'admin123' },
    pageSize: 8,
  };

  let state = {
    currentPage: 'marketing-overview',
    currentUser: null,
    currentRole: null,
    currentAccount: null,
    editingType: null,    // 'product' | 'order' | 'customer'
    editingItem: null,    // 正在编辑的数据对象
    deleteTarget: null,   // { type, id }
    sidebarOpen: false,
    apiAvailable: false,
    filters: {},
    charts: { order: null, status: null },
  };

  // ==================== 模拟数据 ====================
  function getDefaultData() {
    const now = new Date();
    const d = (days) => {
      const dt = new Date(now);
      dt.setDate(dt.getDate() - days);
      return dt.toISOString().slice(0, 10);
    };

    return {
      products: [
        { id: 1, name: '无线蓝牙耳机 Pro', category: '电子产品', price: 299, stock: 150, status: '在售' },
        { id: 2, name: '轻薄羽绒服', category: '服装鞋帽', price: 599, stock: 80, status: '在售' },
        { id: 3, name: '有机坚果礼盒', category: '食品饮料', price: 128, stock: 200, status: '在售' },
        { id: 4, name: '智能台灯', category: '家居生活', price: 189, stock: 95, status: '在售' },
        { id: 5, name: '运动跑鞋', category: '服装鞋帽', price: 459, stock: 60, status: '在售' },
        { id: 6, name: '蓝牙音箱', category: '电子产品', price: 199, stock: 0, status: '缺货' },
        { id: 7, name: '原味酸奶', category: '食品饮料', price: 49, stock: 300, status: '在售' },
        { id: 8, name: '纯棉四件套', category: '家居生活', price: 349, stock: 45, status: '在售' },
        { id: 9, name: '手机充电器', category: '电子产品', price: 89, stock: 120, status: '在售' },
        { id: 10, name: '速干T恤', category: '服装鞋帽', price: 99, stock: 180, status: '在售' },
        { id: 11, name: '咖啡豆', category: '食品饮料', price: 168, stock: 75, status: '在售' },
        { id: 12, name: '空气净化器', category: '家居生活', price: 1299, stock: 20, status: '在售' },
      ],
      orders: [
        { id: 'ORD20260101', customer: '张三', product: '无线蓝牙耳机 Pro', qty: 2, amount: 598, status: '已完成', date: d(1) },
        { id: 'ORD20260102', customer: '李四', product: '轻薄羽绒服', qty: 1, amount: 599, status: '已发货', date: d(2) },
        { id: 'ORD20260103', customer: '王五', product: '有机坚果礼盒', qty: 3, amount: 384, status: '待发货', date: d(2) },
        { id: 'ORD20260104', customer: '赵六', product: '运动跑鞋', qty: 1, amount: 459, status: '已完成', date: d(3) },
        { id: 'ORD20260105', customer: '张三', product: '智能台灯', qty: 2, amount: 378, status: '已发货', date: d(3) },
        { id: 'ORD20260106', customer: '孙七', product: '蓝牙音箱', qty: 1, amount: 199, status: '已取消', date: d(4) },
        { id: 'ORD20260107', customer: '李四', product: '纯棉四件套', qty: 1, amount: 349, status: '已完成', date: d(4) },
        { id: 'ORD20260108', customer: '王五', product: '咖啡豆', qty: 2, amount: 336, status: '待发货', date: d(5) },
        { id: 'ORD20260109', customer: '周八', product: '空气净化器', qty: 1, amount: 1299, status: '已完成', date: d(5) },
        { id: 'ORD20260110', customer: '赵六', product: '速干T恤', qty: 5, amount: 495, status: '已发货', date: d(6) },
        { id: 'ORD20260111', customer: '张三', product: '原味酸奶', qty: 4, amount: 196, status: '待发货', date: d(6) },
        { id: 'ORD20260112', customer: '孙七', product: '手机充电器', qty: 1, amount: 89, status: '已完成', date: d(7) },
        { id: 'ORD20260113', customer: '李四', product: '无线蓝牙耳机 Pro', qty: 1, amount: 299, status: '已完成', date: d(7) },
        { id: 'ORD20260114', customer: '王五', product: '有机坚果礼盒', qty: 2, amount: 256, status: '已取消', date: d(7) },
      ],
      customers: [
        { id: 1, name: '张三', phone: '13800138001', email: 'zhangsan@mail.com', address: '北京市朝阳区', regDate: d(30) },
        { id: 2, name: '李四', phone: '13800138002', email: 'lisi@mail.com', address: '上海市浦东新区', regDate: d(28) },
        { id: 3, name: '王五', phone: '13800138003', email: 'wangwu@mail.com', address: '广州市天河区', regDate: d(25) },
        { id: 4, name: '赵六', phone: '13800138004', email: 'zhaoliu@mail.com', address: '深圳市南山区', regDate: d(20) },
        { id: 5, name: '孙七', phone: '13800138005', email: 'sunqi@mail.com', address: '杭州市西湖区', regDate: d(18) },
        { id: 6, name: '周八', phone: '13800138006', email: 'zhouba@mail.com', address: '成都市武侯区', regDate: d(15) },
        { id: 7, name: '吴九', phone: '13800138007', email: 'wujiu@mail.com', address: '南京市玄武区', regDate: d(12) },
        { id: 8, name: '郑十', phone: '13800138008', email: 'zhengshi@mail.com', address: '武汉市洪山区', regDate: d(10) },
        { id: 9, name: '钱十一', phone: '13800138009', email: 'qian@mail.com', address: '重庆市渝中区', regDate: d(8) },
        { id: 10, name: '刘十二', phone: '13800138010', email: 'liu@mail.com', address: '西安市雁塔区', regDate: d(5) },
      ],
    };
  }

  function loadData() {
    const saved = localStorage.getItem('admin_data');
    if (saved) {
      try { return JSON.parse(saved); } catch (e) { /* fallback */ }
    }
    const defaults = getDefaultData();
    saveData(defaults);
    return defaults;
  }

  function saveData(data) {
    localStorage.setItem('admin_data', JSON.stringify(data));
  }

  function getStore() {
    return loadData();
  }

  // ==================== 数据操作 ====================
  function getItems(type) {
    const store = getStore();
    const keyMap = { product: 'products', order: 'orders', customer: 'customers' };
    return store[keyMap[type]] || [];
  }

  function setItems(type, items) {
    const store = getStore();
    const keyMap = { product: 'products', order: 'orders', customer: 'customers' };
    store[keyMap[type]] = items;
    saveData(store);
  }

  function addItem(type, item) {
    const items = getItems(type);
    if (type !== 'order') {
      const maxId = items.length > 0 ? Math.max(...items.map(i => i.id)) : 0;
      item.id = maxId + 1;
    }
    items.push(item);
    setItems(type, items);
    // 后台同步到远程数据库（不影响本地操作）
    if (state.apiAvailable) ApiService.create(type, item);
  }

  function updateItem(type, item) {
    const items = getItems(type);
    const idx = items.findIndex(i => i.id === item.id);
    if (idx !== -1) items[idx] = item;
    setItems(type, items);
    if (state.apiAvailable) ApiService.update(type, item.id, item);
  }

  function deleteItem(type, id) {
    const items = getItems(type);
    const filtered = items.filter(i => i.id !== id);
    setItems(type, filtered);
    if (state.apiAvailable) ApiService.del(type, id);
  }

  // ==================== 登录/登出 ====================
  async function handleLogin(e) {
    e.preventDefault();
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value.trim();
    const errEl = document.getElementById('loginError');

    if (!username || !password) {
      errEl.textContent = '请输入用户名和密码';
      return;
    }

    var adminData = null;

    // 始终先走后端数据库验证（不管 health check 结果）
    try {
      var resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username, password: password }),
      });
      var json = await resp.json();
      if (json.code === 0 && json.data) {
        adminData = json.data;
      }
    } catch (e) {
      console.warn('后端登录验证失败，降级到本地验证:', e);
    }

    // 后端不可用或失败时，降级到 localStorage 验证
    if (!adminData) {
      var admins = (function () {
        try { return JSON.parse(localStorage.getItem('admin_permissions_admins')); }
        catch (e) { return null; }
      })();
      if (!admins || !admins.length) { admins = getDefaultAdmins(); }

      var matchedAdmin = admins.find(function(a) { return a.account === username && a.password === password; });

      if (!matchedAdmin) {
        errEl.textContent = '用户名或密码错误';
        return;
      }
      if (matchedAdmin.status === 'disabled') {
        errEl.textContent = '该账号已被禁用，请联系超级管理员';
        return;
      }
      adminData = { name: matchedAdmin.name, role: matchedAdmin.role, account: matchedAdmin.account, status: matchedAdmin.status };
    }

    // 检查禁用状态
    if (adminData.status === 'disabled') {
      errEl.textContent = '该账号已被禁用，请联系超级管理员';
      return;
    }

    // 登录成功
    state.currentUser = adminData.name;
    state.currentRole = adminData.role || '';
    state.currentAccount = username;
    state.currentPermissions = adminData.permissions || [];

    // 「记住密码」已全局下线：不再留存任何账号密码；顺手清掉历史遗留的明文凭据
    try {
      localStorage.removeItem('admin_user');
      localStorage.removeItem('admin_pass');
    } catch (e) {}
    sessionStorage.setItem('admin_logged_in', 'true');
    sessionStorage.setItem('admin_current_user', state.currentUser);
    sessionStorage.setItem('admin_current_account', username);
    sessionStorage.setItem('admin_current_role', state.currentRole);
    if (state.currentPermissions && state.currentPermissions.length) {
      sessionStorage.setItem('admin_permissions', JSON.stringify(state.currentPermissions));
    }

    // 阻止浏览器「保存密码」弹窗：登录成功后 Chrome 会在此刻采样密码值，先行清空
    try {
      var __nafPwdEl = document.getElementById('password');
      if (__nafPwdEl) {
        __nafPwdEl.value = '';
        __nafPwdEl.setAttribute('autocomplete', 'off');
      }
    } catch (e) {}

    document.getElementById('loginPage').classList.add('hidden');
    document.getElementById('appPage').classList.remove('hidden');
    document.getElementById('currentUser').textContent = state.currentUser;
    // ★ 登录后立刻拉一次管辖范围：主管身份决定「管理员与权限」页能否进入、
    //   以及页内能看到哪些按钮。失败不阻断登录（退化为非主管，后端仍会拦）。
    try { await _fetchMyScope(); } catch (e) {}
    initApp();
  }

  /** 拉取后端管辖范围（模块内部用；对外暴露为 App.fetchMyScope） */
  async function _fetchMyScope() {
    try {
      const r = await fetch('/api/admin/my-scope', { credentials: 'same-origin' });
      const j = await r.json();
      if (j && j.code === 0 && j.data) {
        _MY_SCOPE = j.data;
        _IS_DEPT_LEAD = _MY_SCOPE.level === 'lead';
        _CAN_MANAGE_ACCOUNTS = !!_MY_SCOPE.canManageAccounts
          || ACCOUNT_MANAGER_ROLES.indexOf(_MY_SCOPE.role || '') >= 0;
        _CAN_MANAGE_ROLES = !!_MY_SCOPE.canManageRoles;
        sessionStorage.setItem('admin_is_account_manager', _CAN_MANAGE_ACCOUNTS ? '1' : '0');
        sessionStorage.setItem('admin_is_role_manager', _CAN_MANAGE_ROLES ? '1' : '0');
      }
    } catch (e) {}
    return _MY_SCOPE;
  }

  function handleLogout() {
    // 通知后端清除 token 会话（fire-and-forget）
    fetch('/api/auth/logout', { method: 'POST' }).catch(function() {});
    localStorage.removeItem('admin_user');
    localStorage.removeItem('admin_pass');
    sessionStorage.removeItem('admin_logged_in');
    sessionStorage.removeItem('admin_permissions');
    sessionStorage.removeItem('admin_current_user');
    state.currentUser = null;
    document.getElementById('loginPage').classList.remove('hidden');
    document.getElementById('appPage').classList.add('hidden');
    document.getElementById('loginForm').reset();
  }

  // ==================== 路由 ====================
  function _showPermissionDenied() {
    document.querySelectorAll('.page-section').forEach(function(el) { el.classList.add('hidden'); });
    var denyEl = document.getElementById('page-permission-denied');
    if (!denyEl) {
      denyEl = document.createElement('section');
      denyEl.id = 'page-permission-denied';
      denyEl.className = 'page-section';
      denyEl.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:calc(100vh - 180px);flex-direction:column;gap:14px">' +
        '<div style="width:88px;height:88px;border-radius:50%;background:#f1f5f9;display:flex;align-items:center;justify-content:center">' +
          '<i class="fa-solid fa-lock" style="font-size:36px;color:#94a3b8"></i>' +
        '</div>' +
        '<h2 style="color:#334155;font-weight:600;font-size:18px;margin:0">无权限访问</h2>' +
        '<p style="color:#94a3b8;font-size:14px;margin:0;text-align:center;max-width:320px;line-height:1.6">当前账号没有访问此页面的权限<br>请向超级管理员申请开通权限</p></div>';
      document.querySelector('.main-content').appendChild(denyEl);
    }
    denyEl.classList.remove('hidden');
    document.getElementById('pageTitle').textContent = '无权限';
  }

  function navigateTo(page) {
    // 权限检查
    // 人事中心是单页面 + 页内卡片切换，权限点按数据表拆分（hr-roster / hr-interview / ...）。
    // 只要拥有任意一个人事数据表权限，即允许进入人事数据中心页面；具体可见哪张表由页面内部再判定。
    var _hrPermHit = page === 'hr' && _ALLOWED_PAGES !== null &&
      _ALLOWED_PAGES.some(function (p) { return String(p).indexOf('hr-') === 0; });
    // ★★ 账号列表硬门槛（2026-09-18 调整）：
    //   · 超级层（开发人员/超级管理员）→ 放行
    //   · 部门主管 → 也放行（页面内再看本部门，最终以后端 my-scope 为准）
    //   · 其余角色 → 即便被分配了该板块也进不去
    //   注意：_IS_DEPT_LEAD 由 _MY_SCOPE 异步填充，首次进入页面时若尚未拉到，
    //   先按「非主管」处理，由页面内部再纠正（不会误放行敏感操作，因为后端有硬校验）。
    var _accDeny = page === 'admin-permissions'
      && !_CAN_MANAGE_ACCOUNTS && !_IS_DEPT_LEAD;

    if (_accDeny || (_ALLOWED_PAGES !== null && !_ALLOWED_PAGES.includes(page) && page !== 'profile' && !_hrPermHit)) {
      _showPermissionDenied();
      // 高亮当前点击的菜单项
      document.querySelectorAll('.nav-item').forEach(function(el) {
        el.classList.toggle('active', el.dataset.page === page);
      });
      // 展开对应的导航组和子菜单
      document.querySelectorAll('.nav-group').forEach(function(group) {
        var hasActive = group.querySelector('.nav-item[data-page="' + page + '"]');
        group.classList.toggle('open', !!hasActive);
      });
      document.querySelectorAll('.nav-submenu').forEach(function(sub) {
        var hasActive = sub.querySelector('.nav-item[data-page="' + page + '"]');
        sub.classList.toggle('open', !!hasActive);
      });
      document.getElementById('pageTitle').textContent = '无权限';
      return;
    }
    state.currentPage = page;
    // Update nav active state
    document.querySelectorAll('.nav-item').forEach(el => {
      el.classList.toggle('active', el.dataset.page === page);
    });
    // Open parent group for active item
    document.querySelectorAll('.nav-group').forEach(group => {
      const hasActive = group.querySelector('.nav-item[data-page="' + page + '"]');
      group.classList.toggle('open', !!hasActive);
    });
    // Open parent submenu for active item
    document.querySelectorAll('.nav-submenu').forEach(sub => {
      const hasActive = sub.querySelector('.nav-item[data-page="' + page + '"]');
      sub.classList.toggle('open', !!hasActive);
    });
    // Update page sections
    document.querySelectorAll('.page-section').forEach(el => el.classList.add('hidden'));
    const target = document.getElementById('page-' + page);
    if (target) target.classList.remove('hidden');
    // Update title
    const titles = {
      'marketing-overview': '整体营销数据总览',
      'platform-store': '分平台 / 店铺详细数据',
      'daily-analysis': '每日数据分析',
      'store-account': '店铺账号管理',
      'operation-performance': '运营业绩面板',
      'product-selection': '选品助手',
      finance: '财务中心',
      hr: '人事数据中心',
      'admin-permissions': '管理员与权限',
      profile: '个人中心设置',
      'toolbox-violation-check': '违规词检测',
      'order-details': '订单详情',
      'category-marketing': '品类营销数据',
      'seeding-monitor': '种草监测中台',
    };
    document.getElementById('pageTitle').textContent = titles[page] || page;
    // ★★ 已迁移到 Vue 的页面：**跳过旧渲染函数**（2026-09-17 性能修复）
    //
    // 症状：这些页面迁到 Vue 之后，旧渲染函数仍在被调用 → 旧版 + Vue 版**同时跑**：
    //   · 同一接口在一次「进入页面」里被请求 2 次（nginx access.log 实证：
    //     品类营销 15:02:39 / 15:02:41、订单详情 15:03:00 各出现两条完全相同的请求）；
    //   · 旧 DOM 还白算一遍（含 ECharts 初始化），用户侧表现为
    //     「先闪一下旧版数据 → 再被 Vue 版替换」，体感慢一倍。
    //
    // 为什么可以直接跳过：旧 section 在 Vue 挂载时会被 `style.display='none'` 隐藏，
    //   **用户根本看不到旧渲染的结果** → 跳过它零视觉损失，纯粹消除浪费。
    //   （Vue 版也不依赖 App IIFE 内的 state/全局函数，各页面自带 loadStores 等实现。）
    //
    // 判据用「-vue 挂载容器是否存在」：容器缺失时自动回退到旧渲染，不会白屏。
    const _VUE_PAGES = ['marketing-overview', 'platform-store', 'daily-analysis',
      'product-selection', 'admin-permissions', 'profile', 'toolbox-violation-check',
      'order-details', 'category-marketing', 'seeding-monitor'];
    const _skipLegacyRender = _VUE_PAGES.indexOf(page) >= 0
      && !!document.getElementById('page-' + page + '-vue');
    if (!_skipLegacyRender) {
      if (page === 'marketing-overview') renderMarketingOverview();
      if (page === 'platform-store') renderPlatformStore();
      if (page === 'daily-analysis') renderDailyAnalysis();
      if (page === 'product-selection') renderProductSelection();
      if (page === 'admin-permissions') renderAdminPermissions();
      if (page === 'profile') renderProfile();
      if (page === 'toolbox-violation-check') renderToolboxViolationCheck();
      if (page === 'order-details') renderOrderDetails();
      if (page === 'category-marketing') renderCategoryMarketing();
      if (page === 'seeding-monitor') renderSeedingMonitor();
    }
    // Close sidebar on mobile
    if (window.innerWidth <= 768) toggleSidebar(false);
  }

  // ==================== 品类营销数据 ====================
  let _catRange = 'all';
  let _catStart = '', _catEnd = '';

  function _catComputeRange() {
    if (_catRange === 'all') return ['', ''];
    if (_catRange === 'custom') return [_catStart, _catEnd];
    const endDate = new Date(Date.now() - 86400000);
    const startDate = new Date(endDate);
    startDate.setDate(startDate.getDate() - parseInt(_catRange, 10) + 1);
    return [startDate.toISOString().slice(0, 10), endDate.toISOString().slice(0, 10)];
  }

  async function renderCategoryMarketing() {
    document.querySelectorAll('.cat-ds-btn').forEach(b => b.classList.toggle('active', b.dataset.range === _catRange));
    const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    const ds = document.getElementById('catDateStart');
    const de = document.getElementById('catDateEnd');
    if (ds) ds.max = yesterday;
    if (de) de.max = yesterday;
    const range = _catComputeRange();
    const start = range[0], end = range[1];

    const fmtMoney = v => '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const fmtNum = v => Math.round(v).toLocaleString('zh-CN');
    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

    let data = null;
    if (state.apiAvailable) data = await ApiService.getCategoryMarketing(start, end);

    if (data && data.totals) {
      const t = data.totals;
      setText('catPayment', fmtMoney(t.payment));
      setText('catOrders', fmtNum(t.orders));
      setText('catBuyers', fmtNum(t.buyers));
      setText('catRefund', fmtMoney(t.refund));
      setText('catProducts', fmtNum(t.productCount));
      setText('catRefundRate', (t.refundRate || 0).toFixed(2) + '%');
      setText('catCategoryCount', t.categoryCount);
      setText('catSpend', fmtMoney(t.spend));
      setText('catAdGmv', fmtMoney(t.adGmv));
      setText('catRoi', (t.roi || 0).toFixed(2));
      _catRenderTable(data.categories || []);
      _catRenderChart(data.categories || []);
    } else {
      ['catPayment', 'catOrders', 'catBuyers', 'catRefund', 'catProducts', 'catRefundRate', 'catCategoryCount', 'catSpend', 'catAdGmv', 'catRoi'].forEach(id => setText(id, '--'));
      _catRenderTable([]);
      _catRenderChart([]);
    }
  }

  function setCatRange(range) {
    _catRange = range;
    const r = _catComputeRange();
    _catStart = r[0]; _catEnd = r[1];
    const ds = document.getElementById('catDateStart');
    const de = document.getElementById('catDateEnd');
    if (ds) ds.value = r[0];
    if (de) de.value = r[1];
    renderCategoryMarketing();
  }

  function setCatCustomDate() {
    const ds = document.getElementById('catDateStart');
    const de = document.getElementById('catDateEnd');
    _catStart = ds ? ds.value : '';
    _catEnd = de ? de.value : '';
    _catRange = 'custom';
    renderCategoryMarketing();
  }

  function _catKeywordSpan(keywords) {
    if (!keywords || !keywords.length) return '';
    const text = keywords.join('、');
    return ' <span title="' + text + '" style="display:inline-block;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom;color:#94a3b8;font-size:0.78rem;cursor:help">' + text + '</span>';
  }

  function _catRenderTable(categories) {
    const tbody = document.getElementById('catTableBody');
    if (!tbody) return;
    if (!categories.length) {
      tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#94a3b8;padding:24px">暂无数据</td></tr>';
      return;
    }
    const fmtMoney = v => '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const fmtNum = v => Math.round(v).toLocaleString('zh-CN');
    tbody.innerHTML = categories.map((c, i) => (
      '<tr style="border-bottom:1px solid #f1f5f9">' +
      '<td style="padding:9px 12px;color:#94a3b8">' + (i + 1) + '</td>' +
      '<td style="padding:9px 12px;font-weight:500;color:#1e293b">' + c.category +
      _catKeywordSpan(c.keywords) + '</td>' +
      '<td style="padding:9px 12px;text-align:right;color:#475569">' + fmtNum(c.products) + '</td>' +
      '<td style="padding:9px 12px;text-align:right;font-weight:600;color:#2563eb">' + fmtMoney(c.payment) + '</td>' +
      '<td style="padding:9px 12px;text-align:right;color:#475569">' + fmtNum(c.orders) + '</td>' +
      '<td style="padding:9px 12px;text-align:right;color:#475569">' + fmtNum(c.buyers) + '</td>' +
      '<td style="padding:9px 12px;text-align:right;color:#dc2626">' + fmtMoney(c.refund) + '</td>' +
      '<td style="padding:9px 12px;text-align:right;color:#475569">' + (c.refundRate || 0).toFixed(2) + '%</td>' +
      '<td style="padding:9px 12px;text-align:right;color:#475569">' + fmtMoney(c.spend) + '</td>' +
      '<td style="padding:9px 12px;text-align:right;font-weight:600;color:#16a34a">' + fmtMoney(c.ad_gmv) + '</td>' +
      '<td style="padding:9px 12px;text-align:right;color:#475569">' + (c.roi || 0).toFixed(2) + '</td>' +
      '</tr>'
    )).join('');
  }

  function _catRenderChart(categories) {
    const el = document.getElementById('catBarChart');
    if (!el) return;
    if (typeof echarts === 'undefined') {
      el.innerHTML = '<div style="padding:24px;text-align:center;color:#94a3b8">图表组件未加载</div>';
      return;
    }
    if (!el._catChart) el._catChart = echarts.init(el);
    const top = categories.slice(0, 10).slice().reverse();
    const chart = el._catChart;
    chart.setOption({
      grid: { left: 104, right: 60, top: 10, bottom: 24 },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: function (ps) { const p = ps[0]; return p.name + '<br/>成交金额：¥' + Number(p.value).toLocaleString(); } },
      xAxis: { type: 'value', axisLabel: { formatter: function (v) { return (v / 10000).toFixed(0) + '万'; } } },
      yAxis: { type: 'category', data: top.map(c => c.category), axisLabel: { fontSize: 12, color: '#475569' } },
      series: [{ type: 'bar', data: top.map(c => c.payment), itemStyle: { color: '#1677ff', borderRadius: [0, 4, 4, 0] }, barMaxWidth: 18 }],
    });
    setTimeout(function () { try { chart.resize(); } catch (e) {} }, 60);
  }

  function showCatKeywords(evt, el) {
    const kw = el.getAttribute('data-keywords');
    if (!kw) return;
    let tip = document.getElementById('catTooltip');
    if (!tip) {
      tip = document.createElement('div');
      tip.id = 'catTooltip';
      tip.className = 'cat-tooltip';
      document.body.appendChild(tip);
    }
    tip.textContent = '关键词：' + kw;
    tip.style.display = 'block';
    const r = el.getBoundingClientRect();
    let left = r.left + r.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - 190));
    tip.style.left = left + 'px';
    tip.style.top = (r.bottom + 8) + 'px';
  }

  function hideCatKeywords() {
    const tip = document.getElementById('catTooltip');
    if (tip) tip.style.display = 'none';
  }

  // ==================== 营销数据总览 ====================
  async function renderMarketingOverview(startDate, endDate) {
    const dateStart = document.getElementById('mktDateStart');
    const dateEnd   = document.getElementById('mktDateEnd');
    const platform  = document.getElementById('mktPlatform');
    const brand     = document.getElementById('mktBrand');

    // 默认昨天，限制最大日期为昨天（今日无数据，不可选）
    const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    if (dateStart) { dateStart.max = yesterday; if (!dateStart.value) dateStart.value = startDate || yesterday; }
    if (dateEnd)   { dateEnd.max   = yesterday; if (!dateEnd.value)   dateEnd.value   = endDate   || yesterday; }
    _calUpdateLabel('mkt');

    const start = dateStart?.value || '';
    const end   = dateEnd?.value   || '';
    const plat  = platform?.value  || '';
    const brd   = brand?.value     || '';

    // 更新快捷按钮状态
    updateDateShortcuts(start, end);

    // 从 API 获取数据
    let metrics = null;
    console.log('[MKT] renderMarketingOverview apiAvailable=' + state.apiAvailable + ' start=' + start + ' end=' + end);
    if (state.apiAvailable) {
      metrics = await ApiService.getMarketingOverview(start, end, plat, brd);
      console.log('[MKT] API返回:', metrics ? ('netPayment=' + metrics.netPayment + ', trends=' + (metrics.trends||[]).length) : 'null');
    }

    if (metrics) {
      populateMarketingCards(metrics, start, end);
      renderChartsSection(metrics);
    } else {
      console.warn('[MKT] 指标为null，显示0。apiAvailable=' + state.apiAvailable);
      populateMarketingCards({
        netPayment: 0, payment: 0, refundAmount: 0, refundRate: 0,
        adSpend: 0, adTotal: 0, roi: 0,
        visitors: 0, payers: 0, trends: null,
      }, start, end);
      clearChartsSection();
    }
  }

  function setDateRange(days) {
    // days = 1 表示昨天, 7 = 近7天, 30 = 近30天
    const end = new Date(Date.now() - 86400000);
    const start = new Date(end);
    start.setDate(start.getDate() - days + 1);
    const fmt = d => d.toISOString().slice(0, 10);
    document.getElementById('mktDateStart').value = fmt(start);
    document.getElementById('mktDateEnd').value   = fmt(end);
    renderMarketingOverview(fmt(start), fmt(end));
  }

  function updateDateShortcuts(s, e) {
    const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    document.querySelectorAll('.dh-ds-btn').forEach(btn => {
      const range = parseInt(btn.dataset.range);
      // 无 data-range（或非数字）时 range 为 NaN，下方 new Date(NaN).toISOString() 会抛
      // RangeError: Invalid time value，直接跳过该按钮。
      if (!Number.isFinite(range)) { btn.classList.remove('active'); return; }
      let match = false;
      if (range === 1) {
        match = (s === yesterday && e === yesterday);
      } else {
        const calcStart = new Date(Date.now() - 86400000 - (range - 1) * 86400000).toISOString().slice(0, 10);
        match = (s === calcStart && e === yesterday);
      }
      btn.classList.toggle('active', match);
    });
  }

  function populateMarketingCards(m, dateStart, dateEnd) {
    if (!m.refundRate && m.netPayment > 0) { m.refundRate = (m.refundAmount || 0) / m.netPayment * 100; }
    if (!m.roi && m.adSpend > 0) { m.roi = (m.adTotal || 0) / m.adSpend; }

    function fmtMoney(v) { return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
    function fmtNum(v)   { return Math.round(v).toLocaleString('zh-CN'); }

    document.getElementById('netPayment').textContent  = fmtMoney(m.netPayment);
    document.getElementById('totalRevenue').textContent = fmtMoney(m.payment);
    document.getElementById('refundAmount').textContent = fmtMoney(m.refundAmount);
    document.getElementById('refundRate').textContent   = (m.refundRate || 0).toFixed(2) + '%';
    document.getElementById('adSpend').textContent      = fmtMoney(m.adSpend);
    document.getElementById('adRevenue').textContent    = fmtMoney(m.adTotal);
    document.getElementById('roi').textContent          = (m.roi || 0).toFixed(4);
    document.getElementById('visitors').textContent     = fmtNum(m.visitors);
    document.getElementById('payers').textContent       = fmtNum(m.payers);

    function trendIcon(pct, inverted, isDelta) {
      if (pct === null || pct === undefined) return ['', 'neutral'];
      const abs = Math.abs(pct);
      const up = pct > (isDelta ? 0.005 : 0);
      const down = pct < (isDelta ? -0.005 : 0);
      if (!up && !down) return ['持平', 'neutral'];
      let suffix = isDelta ? abs.toFixed(2) + 'pp' : abs + '%';
      let cls, arrow;
      if (inverted) { cls = up ? 'down' : 'up'; arrow = up ? '↑ +' : '↓ '; }
      else { cls = up ? 'up' : 'down'; arrow = up ? '↑ +' : '↓ '; }
      return [arrow + suffix, cls];
    }
    if (m.comparison) {
      const c = m.comparison;
      const label = c.isSingleDay ? ' 环比昨日' : ' 环比上期';
      function setTrend(id, pct, inverted, isDelta) {
        const el = document.getElementById(id);
        if (!el) return;
        const [html, cls] = trendIcon(pct, inverted, isDelta);
        el.innerHTML = html ? '<span>' + html + '</span>' + label : '';
        el.className = 'mkt-card-trend ' + cls;
      }
      setTrend('netPaymentTrend',   c.netPayment,   false, false);
      setTrend('refundAmountTrend', c.refundAmount, true,  false);
      setTrend('refundRateTrend',   c.refundRate,   true,  true);
      setTrend('visitorsTrend',     c.visitors,     false, false);
      setTrend('adRevenueTrend',    c.adTotal,      false, false);
      setTrend('adSpendTrend',      c.adSpend,      true,  false);
      setTrend('roiTrend',          c.roi,          false, true);
      setTrend('payersTrend',       c.payers,       false, false);
    }

    // ---- 更新时间 ----
    const now = new Date();
    const timeEl = document.getElementById('dhUpdateTime');
    if (timeEl) {
      let label = '更新于 ' + now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      if (state.apiAvailable) {
        if (dateStart && dateEnd && dateStart === dateEnd) {
          label += ' · ' + dateStart;
        } else if (dateStart || dateEnd) {
          label += ' · ' + (dateStart || '...') + ' ~ ' + (dateEnd || '...');
        }
        label += ' · 数据库实时';
      }
      timeEl.textContent = label;
    }
  }

  function drawSparkline(canvasId, data, color) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !data.length) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = 36 * dpr;
    canvas.style.width = rect.width + 'px';
    canvas.style.height = '36px';
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    const w = rect.width, h = 36, pad = 2;
    const max = Math.max(...data, 1);
    const min = Math.min(...data, 0);
    const range = max - min || 1;

    ctx.clearRect(0, 0, w, h);
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    const stepX = (w - pad * 2) / (data.length - 1);
    data.forEach((v, i) => {
      const x = pad + i * stepX;
      const y = h - pad - ((v - min) / range) * (h - pad * 2);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // subtle fill
    ctx.lineTo(pad + (data.length - 1) * stepX, h - pad);
    ctx.lineTo(pad, h - pad);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, color + '30');
    grad.addColorStop(1, color + '04');
    ctx.fillStyle = grad;
    ctx.fill();
  }

  // ==================== 图表分析区 (ECharts) ====================
  const _chartInstances = {};

  function _echartsReady() { return typeof echarts !== 'undefined' && echarts.init; }

  function getOrCreateChart(domId) {
    if (!_echartsReady()) return null;
    const dom = document.getElementById(domId);
    if (!dom) return null;
    if (_chartInstances[domId]) _chartInstances[domId].dispose();
    const c = echarts.init(dom);
    _chartInstances[domId] = c;
    return c;
  }

  function resizeAllCharts() {
    Object.values(_chartInstances).forEach(c => { try { c.resize(); } catch(e) {} });
  }

  function clearChartsSection() {
    Object.values(_chartInstances).forEach(c => { try { c.dispose(); } catch(e) {} });
    for (const k of Object.keys(_chartInstances)) delete _chartInstances[k];
  }

  function renderChartsSection(m) {
    if (!_echartsReady()) return;
    try {
      if (!m.trends || !m.trends.length) { clearChartsSection(); return; }
      const dates = m.trends.map(t => t.date);
      chartTrend(dates, m.trends);
      chartFunnel(m.agg);
      chartRefund(m);
      chartAd(dates, m.trends);
      chartAOV(dates, m.trends);
      window.addEventListener('resize', resizeAllCharts, { once: true });
    } catch(e) { console.warn('[Charts] render error:', e.message); }
  }

  // ── 模块1：核心指标趋势图 ──
  function chartTrend(dates, trends) {
    const c = getOrCreateChart('chartTrend');
    if (!c) return;
    c.setOption({
      tooltip: {
        trigger: 'axis',
        formatter: function(ps) { return ps.map(p => p.marker + p.seriesName + ': ' + (p.seriesName.includes('率') ? p.value + '%' : '¥' + p.value.toLocaleString())).join('<br/>'); }
      },
      legend: { top: 0, right: 0, textStyle: { fontSize: 11 } },
      grid: { left: 50, right: 55, top: 40, bottom: 30 },
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
      yAxis: [
        { type: 'value', name: '金额(元)', nameTextStyle: { fontSize: 10 }, axisLabel: { fontSize: 10, formatter: v => (v/10000).toFixed(0)+'w' } },
        { type: 'value', name: '%', nameTextStyle: { fontSize: 10 }, axisLabel: { fontSize: 10, formatter: v => v+'%' } }
      ],
      series: [
        { name: '支付金额', type: 'line', smooth: true, data: trends.map(t => t.payment), lineStyle: { color: '#3B82F6'}, itemStyle: { color: '#3B82F6'}, areaStyle: { color: 'rgba(59,130,246,0.06)' }, symbol: 'none' },
        { name: '净支付金额', type: 'line', smooth: true, data: trends.map(t => t.netPayment), lineStyle: { color: '#10B981'}, itemStyle: { color: '#10B981'}, areaStyle: { color: 'rgba(16,185,129,0.06)' }, symbol: 'none' },
        { name: '退款金额', type: 'line', smooth: true, data: trends.map(t => t.refundAmount), lineStyle: { color: '#EF4444'}, itemStyle: { color: '#EF4444'}, areaStyle: { color: 'rgba(239,68,68,0.04)' }, symbol: 'none' },
        { name: '推广花费金额', type: 'line', smooth: true, data: trends.map(t => t.adSpend), lineStyle: { color: '#8B5CF6'}, itemStyle: { color: '#8B5CF6'}, areaStyle: { color: 'rgba(139,92,246,0.04)' }, symbol: 'none' },
        { name: '订单退款率', type: 'line', smooth: true, yAxisIndex: 1, data: trends.map(t => t.orderRefundRate), lineStyle: { color: '#F97316', type: 'dashed' }, itemStyle: { color: '#F97316' }, symbol: 'none' },
      ]
    });
  }

  // ── 模块2：转化漏斗 ──
  function chartFunnel(agg) {
    const c = getOrCreateChart('chartFunnel');
    if (!c) return;
    const vis = agg.totalVisitors || 0;
    const cart = agg.totalCart || 0;
    const payers = agg.totalPayers || 0;
    const rate = agg.avgConvRate || 0;
    const cartRate = vis > 0 ? (cart/vis*100).toFixed(1) : '0';
    const payRate = cart > 0 ? (payers/cart*100).toFixed(1) : '0';

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
          { value: Math.round(rate*10)/10, name: '支付转化率 ' + rate + '%', itemStyle: { color: '#F59E0B' } },
        ]
      }]
    });
  }

  // ── 模块3：退款分析 ──
  function chartRefund(m) {
    const dom = document.getElementById('chartRefund');
    if (!dom) return;
    const refundAmount = m.refundAmount || 0;
    const amountRR = m.refundRate || 0;
    const orderRR = m.agg?.avgOrderRefundRate || 0;
    const warn30 = amountRR > 30 || orderRR > 30;

    dom.innerHTML = '<div class="refund-big-num">¥' + refundAmount.toLocaleString('zh-CN', {minimumFractionDigits:2}) + '</div><div class="refund-big-trend">' + (warn30 ? '<span style="color:#e11d48">⚠ 退款率偏高</span>' : '') + '</div><div class="refund-gauges"><div class="refund-gauge"><div class="refund-gauge-chart" id="gaugeAmount"></div><div class="refund-gauge-label" style="font-weight:600">金额退款率 ' + amountRR.toFixed(2) + '%</div><div class="refund-gauge-note">= 退款金额 / 净支付金额</div></div><div class="refund-gauge"><div class="refund-gauge-chart" id="gaugeOrder"></div><div class="refund-gauge-label" style="font-weight:600">订单退款率 ' + orderRR.toFixed(2) + '%</div><div class="refund-gauge-note">= 退款订单数 / 支付订单数</div></div></div><div class="refund-mini-bar" id="refundMiniBar"></div>';

    const gauge = (domId, val, color) => {
      const gc = echarts.init(document.getElementById(domId));
      gc.setOption({series:[{type:'gauge',radius:'85%',center:['50%','55%'],startAngle:200,endAngle:-20,min:0,max:100,axisLine:{show:true,lineStyle:{width:8,color:[[val/100,color],[1,'#f1f5f9']]}},pointer:{show:false},axisTick:{show:false},splitLine:{show:false},axisLabel:{show:false},detail:{fontSize:16,fontWeight:700,color:color,offsetCenter:[0,'80%'],formatter:val.toFixed(1)+'%'},data:[{value:val}]}]});
    };
    gauge('gaugeAmount', Math.min(amountRR, 100), amountRR > 30 ? '#e11d48' : '#f59e0b');
    gauge('gaugeOrder', Math.min(orderRR, 100), orderRR > 30 ? '#e11d48' : '#f59e0b');

    if (m.trends && m.trends.length) {
      const bc = echarts.init(document.getElementById('refundMiniBar'));
      bc.setOption({grid:{left:0,right:0,top:0,bottom:16},xAxis:{type:'category',data:m.trends.map(t=>t.date.slice(5)),axisLabel:{fontSize:8},axisLine:{show:false},axisTick:{show:false}},yAxis:{type:'value',splitLine:{show:false},axisLabel:{show:false}},series:[{type:'bar',data:m.trends.map(t=>t.refundAmount),itemStyle:{color:'#fca5a5',borderRadius:[3,3,0,0]},barWidth:'60%'}]});
    }
  }

  // ── 模块4：推广效果分析 ──
  function chartAd(dates, trends) {
    const c = getOrCreateChart('chartAd');
    if (!c) return;
    c.setOption({
      tooltip: { trigger: 'axis' },
      legend: { top: 0, right: 0, textStyle: { fontSize: 10 } },
      grid: { left: 50, right: 50, top: 35, bottom: 30 },
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
      yAxis: [
        { type: 'value', name: '金额', axisLabel: { fontSize: 9, formatter: v => (v/10000).toFixed(0)+'w' } },
        { type: 'value', name: 'ROI', axisLabel: { fontSize: 9 } }
      ],
      series: [
        { name: '推广总成交', type: 'bar', data: trends.map(t => t.adTotal), itemStyle: { color: '#1E40AF' }, barGap: '10%', barWidth: '35%' },
        { name: '推广花费金额', type: 'bar', data: trends.map(t => t.adSpend), itemStyle: { color: '#A78BFA' }, barWidth: '35%' },
        { name: 'ROI', type: 'line', yAxisIndex: 1, data: trends.map(t => t.roi), lineStyle: { color: '#F97316' }, itemStyle: { color: '#F97316' }, symbol: 'circle', symbolSize: 4 },
      ]
    });
  }

  // ── 模块5：客单价与支付转化率 ──
  function chartAOV(dates, trends) {
    const dom = document.getElementById('chartAOV');
    if (!dom) return;
    const latest = trends[trends.length - 1] || {};
    const aov = latest.aov || 0;
    const convRate = latest.convRate || 0;

    dom.innerHTML = `
      <div class="aov-conv-row">
        <div class="aov-conv-item">
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <span style="font-size:0.85rem;color:#64748b;font-weight:500">客单价</span>
            <span class="aov-conv-val" style="color:#3B82F6">¥${aov.toFixed(2)}</span>
            <span class="aov-conv-trend" style="color:#94a3b8">= 支付金额/买家数</span>
          </div>
          <div class="aov-conv-chart" id="miniAOV"></div>
        </div>
        <div class="aov-conv-item">
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <span style="font-size:0.85rem;color:#64748b;font-weight:500">支付转化率</span>
            <span class="aov-conv-val" style="color:#10B981">${convRate.toFixed(2)}%</span>
            <span class="aov-conv-trend" style="color:#94a3b8">= 买家数/访客数</span>
          </div>
          <div class="aov-conv-chart" id="miniConv"></div>
        </div>
      </div>
    `;

    const miniLine = (domId, data, color) => {
      const mc = echarts.init(document.getElementById(domId));
      mc.setOption({
        grid: { left: 0, right: 0, top: 4, bottom: 6 },
        xAxis: { type: 'category', data: dates, show: false },
        yAxis: { type: 'value', show: false, min: v => v.min - (v.max-v.min)*0.2 },
        series: [{
          type: 'line', data: data, smooth: true,
          lineStyle: { color: color, width: 1.5 }, itemStyle: { color: color },
          symbol: 'none', areaStyle: { color: new echarts.graphic.LinearGradient(0,0,0,1, [{offset:0,color:color+'30'},{offset:1,color:color+'02'}])}
        }]
      });
    }
    miniLine('miniAOV', trends.map(t => t.aov), '#3B82F6');
    miniLine('miniConv', trends.map(t => t.convRate), '#10B981');
  }

  function renderMarketingCharts(netPayment, refundAmount, adSpend, adTotal) {
    // 销毁旧图表
    if (state.charts.marketingTrend) state.charts.marketingTrend.destroy();
    if (state.charts.marketingPie)  state.charts.marketingPie.destroy();

    const now = new Date();
    const days = [];
    for (let i = 6; i >= 0; i--) {
      const d = new Date(now);
      d.setDate(d.getDate() - i);
      days.push((d.getMonth() + 1) + '/' + d.getDate());
    }

    // 按日均值拆分每日随机数据
    function daily(val) {
      return days.map(() => Math.max(0, Math.round((val / 7) * (0.55 + Math.random() * 0.9))));
    }
    state._mktData = {
      days,
      revenueDaily: daily(netPayment),
      adSpendDaily: daily(adSpend),
      adTotalDaily: daily(adTotal),
    };

    // ---- 折线图：净支付金额（默认） ----
    const trendCtx = document.getElementById('marketingTrendChart');
    if (trendCtx) {
      state.charts.marketingTrend = new Chart(trendCtx, {
        type: 'line',
        data: {
          labels: days,
          datasets: [{
            label: '净支付金额',
            data: state._mktData.revenueDaily,
            borderColor: '#1677ff',
            backgroundColor: 'rgba(22,119,255,0.08)',
            fill: true, tension: 0.3,
            pointRadius: 4, pointBackgroundColor: '#1677ff',
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            y: {
              ticks: { callback: v => '¥' + (v / 1000).toFixed(0) + 'k' },
            },
          },
        },
      });
    }

    // ---- 环形图：花费 vs 退款 vs 净利润 ----
    const pieCtx = document.getElementById('marketingPieChart');
    if (pieCtx) {
      const profit = Math.max(0, adTotal - adSpend);
      state.charts.marketingPie = new Chart(pieCtx, {
        type: 'doughnut',
        data: {
          labels: ['推广花费', '退款金额', '净利润（成交-花费）'],
          datasets: [{
            data: [Math.round(adSpend), Math.round(refundAmount), Math.round(profit)],
            backgroundColor: ['#f59e0b', '#dc2626', '#16a34a'],
            borderWidth: 0,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { position: 'bottom', labels: { padding: 16, usePointStyle: true } },
          },
        },
      });
    }

    // 重置切换按钮
    document.querySelectorAll('[data-mkt-chart]').forEach((b, i) => b.classList.toggle('active', i === 0));
  }

  /** 使用 API 返回的趋势数据渲染图表 */
  function renderMarketingChartsFromTrends(trends, refundAmount, adSpend, adTotal) {
    if (state.charts.marketingTrend) state.charts.marketingTrend.destroy();
    if (state.charts.marketingPie)  state.charts.marketingPie.destroy();

    const days  = trends.map(t => t.date.slice(5));          // "MM-DD"
    const revenueData = trends.map(t => t.revenue || 0);
    // 按比例估算每日推广花费和成交
    const ratioSpend  = adTotal > 0 ? adSpend / adTotal : 0.15;
    const adSpendDaily   = revenueData.map(v => Math.round(v * ratioSpend));
    const adTotalDaily = revenueData.map(v => Math.round(v * 0.7));

    state._mktData = { days, revenueDaily: revenueData, adSpendDaily, adTotalDaily };

    // 折线图
    const trendCtx = document.getElementById('marketingTrendChart');
    if (trendCtx) {
      state.charts.marketingTrend = new Chart(trendCtx, {
        type: 'line',
        data: {
          labels: days,
          datasets: [{
            label: '净支付金额', data: revenueData,
            borderColor: '#1677ff', backgroundColor: 'rgba(22,119,255,0.08)',
            fill: true, tension: 0.3, pointRadius: 4, pointBackgroundColor: '#1677ff',
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { y: { ticks: { callback: v => '¥' + (v / 1000).toFixed(0) + 'k' } } },
        },
      });
    }

    // 环形图
    const pieCtx = document.getElementById('marketingPieChart');
    if (pieCtx) {
      const profit = Math.max(0, adTotal - adSpend);
      state.charts.marketingPie = new Chart(pieCtx, {
        type: 'doughnut',
        data: {
          labels: ['推广花费', '退款金额', '净利润（成交-花费）'],
          datasets: [{
            data: [Math.round(adSpend), Math.round(refundAmount), Math.round(profit)],
            backgroundColor: ['#f59e0b', '#dc2626', '#16a34a'],
            borderWidth: 0,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { position: 'bottom', labels: { padding: 16, usePointStyle: true } } },
        },
      });
    }
  }

  function switchMarketingChart(type) {
    if (!state._mktData || !state.charts.marketingTrend) return;
    const chart = state.charts.marketingTrend;
    const { days, adSpendDaily, adTotalDaily } = state._mktData;

    if (type === 'ad') {
      chart.data.datasets = [
        {
          label: '推广花费',
          data: adSpendDaily,
          borderColor: '#f59e0b',
          backgroundColor: 'rgba(245,158,11,0.08)',
          fill: true, tension: 0.3,
          pointRadius: 4, pointBackgroundColor: '#f59e0b',
        },
        {
          label: '推广总成交',
          data: adTotalDaily,
          borderColor: '#16a34a',
          backgroundColor: 'rgba(22,163,74,0.06)',
          fill: true, tension: 0.3,
          pointRadius: 4, pointBackgroundColor: '#16a34a',
        },
      ];
    } else {
      chart.data.datasets = [{
        label: '净支付金额',
        data: state._mktData.revenueDaily,
        borderColor: '#1677ff',
        backgroundColor: 'rgba(22,119,255,0.08)',
        fill: true, tension: 0.3,
        pointRadius: 4, pointBackgroundColor: '#1677ff',
      }];
    }
    chart.update();
  }

  // ==================== 表格渲染 ====================
  function renderTable(type) {
    const tbodyId = type + 'TableBody';
    const paginationId = type + 'Pagination';
    const tbody = document.getElementById(tbodyId);
    const paginationEl = document.getElementById(paginationId);
    if (!tbody || !paginationEl) return;

    let items = getItems(type);

    // Apply filters
    const filter = state.filters[type] || {};
    if (filter.search) {
      const s = filter.search.toLowerCase();
      if (type === 'product') items = items.filter(i => i.name.toLowerCase().includes(s));
      else if (type === 'order') items = items.filter(i => i.id.toLowerCase().includes(s) || i.customer.toLowerCase().includes(s));
      else if (type === 'customer') items = items.filter(i => i.name.toLowerCase().includes(s) || i.phone.toLowerCase().includes(s));
    }
    if (filter.status && type === 'order') items = items.filter(i => i.status === filter.status);
    if (filter.category && type === 'product') items = items.filter(i => i.category === filter.category);

    // Pagination
    const pageSize = CONFIG.pageSize;
    const totalPages = Math.ceil(items.length / pageSize) || 1;
    state.filters[type] = { ...filter, currentPage: Math.min(filter.currentPage || 1, totalPages) };
    const page = state.filters[type].currentPage;
    const pageItems = items.slice((page - 1) * pageSize, page * pageSize);

    // Render rows
    const renderers = {
      product: (item) => `
        <tr>
          <td>${item.id}</td>
          <td>${escapeHtml(item.name)}</td>
          <td>${item.category}</td>
          <td>¥${item.price.toLocaleString()}</td>
          <td>${item.stock}</td>
          <td><span class="badge ${item.status === '在售' ? 'badge-success' : 'badge-danger'}">${item.status}</span></td>
          <td class="actions">
            <button class="btn btn-sm btn-outline" onclick="App.openFormModal('product', ${item.id})">编辑</button>
            <button class="btn btn-sm btn-danger" onclick="App.confirmDelete('product', ${item.id})">删除</button>
          </td>
        </tr>`,
      order: (item) => `
        <tr>
          <td>${item.id}</td>
          <td>${escapeHtml(item.customer)}</td>
          <td>${escapeHtml(item.product)}</td>
          <td>${item.qty}</td>
          <td>¥${item.amount.toLocaleString()}</td>
          <td><span class="badge ${statusBadge(item.status)}">${item.status}</span></td>
          <td>${item.date}</td>
          <td class="actions">
            <button class="btn btn-sm btn-outline" onclick="App.openFormModal('order', '${item.id}')">编辑</button>
            <button class="btn btn-sm btn-danger" onclick="App.confirmDelete('order', '${item.id}')">删除</button>
          </td>
        </tr>`,
      customer: (item) => `
        <tr>
          <td>${item.id}</td>
          <td>${escapeHtml(item.name)}</td>
          <td>${item.phone}</td>
          <td>${item.email}</td>
          <td>${escapeHtml(item.address)}</td>
          <td>${item.regDate}</td>
          <td class="actions">
            <button class="btn btn-sm btn-outline" onclick="App.openFormModal('customer', ${item.id})">编辑</button>
            <button class="btn btn-sm btn-danger" onclick="App.confirmDelete('customer', ${item.id})">删除</button>
          </td>
        </tr>`,
    };

    tbody.innerHTML = pageItems.length > 0
      ? pageItems.map(renderers[type]).join('')
      : '<tr><td class="empty-state" colspan="10"><div class="empty-icon">&#128230;</div><p>暂无数据</p></td></tr>';

    // Render pagination
    let paginationHTML = '';
    if (totalPages > 1) {
      paginationHTML += `<button ${page <= 1 ? 'disabled' : ''} onclick="App.goToPage('${type}', ${page - 1})">&laquo;</button>`;
      for (let i = 1; i <= totalPages; i++) {
        if (totalPages <= 7 || i === 1 || i === totalPages || (i >= page - 1 && i <= page + 1)) {
          paginationHTML += `<button class="${i === page ? 'active' : ''}" onclick="App.goToPage('${type}', ${i})">${i}</button>`;
        } else if (i === page - 2 || i === page + 2) {
          paginationHTML += `<button disabled>...</button>`;
        }
      }
      paginationHTML += `<button ${page >= totalPages ? 'disabled' : ''} onclick="App.goToPage('${type}', ${page + 1})">&raquo;</button>`;
    }
    paginationEl.innerHTML = paginationHTML;
  }

  function renderProductTable() { renderTable('product'); }
  function renderOrderTable() { renderTable('order'); }
  function renderCustomerTable() { renderTable('customer'); }

  function goToPage(type, page) {
    if (!state.filters[type]) state.filters[type] = {};
    state.filters[type].currentPage = page;
    renderTable(type);
  }

  function filterTable(type, searchVal, selectVal) {
    if (!state.filters[type]) state.filters[type] = {};
    const filter = state.filters[type];
    if (searchVal !== null && searchVal !== undefined) filter.search = searchVal;
    if (selectVal !== null && selectVal !== undefined) {
      if (type === 'product') filter.category = selectVal;
      else if (type === 'order') filter.status = selectVal;
    }
    filter.currentPage = 1;
    renderTable(type);
  }

  // ==================== 表单弹窗 ====================
  function openFormModal(type, id) {
    state.editingType = type;
    const items = getItems(type);

    if (id !== undefined && id !== null) {
      state.editingItem = type === 'order'
        ? items.find(i => i.id === id) || null
        : items.find(i => i.id === id) || null;
    } else {
      state.editingItem = null;
    }

    const titles = { product: '商品', order: '订单', customer: '客户' };
    const verb = state.editingItem ? '编辑' : '添加';
    document.getElementById('modalTitle').textContent = verb + titles[type];

    const formHTMLs = {
      product: `
        <div class="form-row">
          <div class="form-group">
            <label>商品名称 *</label>
            <input type="text" class="form-input" id="f_name" value="${esc(state.editingItem?.name)}" placeholder="请输入商品名称">
          </div>
          <div class="form-group">
            <label>分类 *</label>
            <select class="form-select" id="f_category">
              <option value="电子产品" ${state.editingItem?.category === '电子产品' ? 'selected' : ''}>电子产品</option>
              <option value="服装鞋帽" ${state.editingItem?.category === '服装鞋帽' ? 'selected' : ''}>服装鞋帽</option>
              <option value="食品饮料" ${state.editingItem?.category === '食品饮料' ? 'selected' : ''}>食品饮料</option>
              <option value="家居生活" ${state.editingItem?.category === '家居生活' ? 'selected' : ''}>家居生活</option>
            </select>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>价格 (¥) *</label>
            <input type="number" class="form-input" id="f_price" value="${state.editingItem?.price || ''}" placeholder="0" min="0" step="0.01">
          </div>
          <div class="form-group">
            <label>库存 *</label>
            <input type="number" class="form-input" id="f_stock" value="${state.editingItem?.stock ?? ''}" placeholder="0" min="0">
          </div>
        </div>
        <div class="form-group">
          <label>状态</label>
          <select class="form-select" id="f_status">
            <option value="在售" ${state.editingItem?.status === '在售' ? 'selected' : ''}>在售</option>
            <option value="缺货" ${state.editingItem?.status === '缺货' ? 'selected' : ''}>缺货</option>
          </select>
        </div>`,
      order: `
        <div class="form-row">
          <div class="form-group">
            <label>客户 *</label>
            <input type="text" class="form-input" id="f_customer" value="${esc(state.editingItem?.customer)}" placeholder="客户姓名">
          </div>
          <div class="form-group">
            <label>商品名称 *</label>
            <input type="text" class="form-input" id="f_product" value="${esc(state.editingItem?.product)}" placeholder="商品名称">
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>数量 *</label>
            <input type="number" class="form-input" id="f_qty" value="${state.editingItem?.qty ?? '1'}" placeholder="1" min="1">
          </div>
          <div class="form-group">
            <label>金额 (¥) *</label>
            <input type="number" class="form-input" id="f_amount" value="${state.editingItem?.amount || ''}" placeholder="0" min="0" step="0.01">
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>状态</label>
            <select class="form-select" id="f_status">
              <option value="待发货" ${state.editingItem?.status === '待发货' ? 'selected' : ''}>待发货</option>
              <option value="已发货" ${state.editingItem?.status === '已发货' ? 'selected' : ''}>已发货</option>
              <option value="已完成" ${state.editingItem?.status === '已完成' ? 'selected' : ''}>已完成</option>
              <option value="已取消" ${state.editingItem?.status === '已取消' ? 'selected' : ''}>已取消</option>
            </select>
          </div>
          <div class="form-group">
            <label>日期</label>
            <input type="date" class="form-input" id="f_date" value="${state.editingItem?.date || new Date().toISOString().slice(0, 10)}">
          </div>
        </div>`,
      customer: `
        <div class="form-row">
          <div class="form-group">
            <label>姓名 *</label>
            <input type="text" class="form-input" id="f_name" value="${esc(state.editingItem?.name)}" placeholder="客户姓名">
          </div>
          <div class="form-group">
            <label>电话 *</label>
            <input type="text" class="form-input" id="f_phone" value="${state.editingItem?.phone || ''}" placeholder="手机号码">
          </div>
        </div>
        <div class="form-group">
          <label>邮箱</label>
          <input type="email" class="form-input" id="f_email" value="${state.editingItem?.email || ''}" placeholder="邮箱地址">
        </div>
        <div class="form-group">
          <label>地址</label>
          <input type="text" class="form-input" id="f_address" value="${esc(state.editingItem?.address)}" placeholder="收货地址">
        </div>
        ${!state.editingItem ? `
        <div class="form-group">
          <label>注册时间</label>
          <input type="date" class="form-input" id="f_regDate" value="${new Date().toISOString().slice(0, 10)}">
        </div>` : ''}`,
    };

    document.getElementById('modalBody').innerHTML = formHTMLs[type] || '';
    document.getElementById('formModal').classList.remove('hidden');

    // Bind save
    document.getElementById('modalSaveBtn').onclick = () => saveFormModal(type);
  }

  function closeFormModal() {
    document.getElementById('formModal').classList.add('hidden');
    state.editingType = null;
    state.editingItem = null;
  }

  function saveFormModal(type) {
    const getVal = (id) => document.getElementById(id)?.value ?? '';
    const getNum = (id) => parseFloat(document.getElementById(id)?.value) || 0;
    const getInt = (id) => parseInt(document.getElementById(id)?.value) || 0;

    let item = {};
    let isValid = true;

    if (type === 'product') {
      const name = getVal('f_name');
      if (!name) { showToast('请输入商品名称', 'error'); return; }
      item = {
        id: state.editingItem?.id,
        name, category: getVal('f_category'), price: getNum('f_price'),
        stock: getInt('f_stock'), status: getVal('f_status'),
      };
    } else if (type === 'order') {
      const customer = getVal('f_customer');
      const product = getVal('f_product');
      if (!customer || !product) { showToast('请填写客户和商品名称', 'error'); return; }
      item = {
        id: state.editingItem?.id,
        customer, product, qty: getInt('f_qty'), amount: getNum('f_amount'),
        status: getVal('f_status'), date: getVal('f_date'),
      };
    } else if (type === 'customer') {
      const name = getVal('f_name');
      const phone = getVal('f_phone');
      if (!name || !phone) { showToast('请填写姓名和电话', 'error'); return; }
      item = {
        id: state.editingItem?.id,
        name, phone, email: getVal('f_email'), address: getVal('f_address'),
      };
      if (!state.editingItem) {
        item.regDate = getVal('f_regDate') || new Date().toISOString().slice(0, 10);
      } else {
        item.regDate = state.editingItem.regDate;
      }
    }

    if (state.editingItem) {
      updateItem(type, item);
      showToast('更新成功', 'success');
    } else {
      addItem(type, item);
      showToast('添加成功', 'success');
    }

    closeFormModal();
    // Refresh
    if (state.currentPage === type) renderTable(type);
  }

  // ==================== 删除确认 ====================
  function confirmDelete(type, id) {
    state.deleteTarget = { type, id };
    const labels = { product: '商品', order: '订单', customer: '客户' };
    document.getElementById('confirmMsg').textContent = `确定要删除该${labels[type]}吗？此操作不可撤销。`;
    document.getElementById('confirmModal').classList.remove('hidden');
    document.getElementById('confirmDeleteBtn').onclick = () => executeDelete();
  }

  function closeConfirmModal() {
    document.getElementById('confirmModal').classList.add('hidden');
    state.deleteTarget = null;
  }

  function executeDelete() {
    if (!state.deleteTarget) return;
    const { type, id } = state.deleteTarget;
    deleteItem(type, id);
    showToast('删除成功', 'success');
    closeConfirmModal();
    if (state.currentPage === type) renderTable(type);
  }

  // ==================== Toast ====================
  function showToast(message, type = 'success') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = `toast toast-${type}`;
    toast.classList.remove('hidden');
    clearTimeout(toast._timeout);
    toast._timeout = setTimeout(() => toast.classList.add('hidden'), 2500);
  }

  // ==================== 侧边栏 ====================
  function toggleSidebar(force) {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.querySelector('.sidebar-overlay');
    const open = force !== undefined ? force : !state.sidebarOpen;
    state.sidebarOpen = open;
    sidebar.classList.toggle('open', open);
    if (overlay) overlay.classList.toggle('show', open);
  }

  // ==================== 时间更新 ====================
  function updateTime() {
    const now = new Date();
    const str = now.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const el = document.getElementById('topbarTime');
    if (el) el.textContent = str;
  }

  // ==================== 辅助函数 ====================
  function statusBadge(status) {
    const map = { '已完成': 'badge-success', '待发货': 'badge-warning', '已发货': 'badge-info', '已取消': 'badge-gray' };
    return map[status] || 'badge-gray';
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function esc(val) {
    return val !== undefined && val !== null ? escapeHtml(String(val)) : '';
  }


  // ==================== 分平台/店铺详细数据 ====================
  const _psCharts = {};
  let _psData = null;
  let _psPage = 1;
  let _psSortBy = 'netPayment';
  let _psSortDir = 'desc';
  let _psSearch = '';
  let _psExpandedStore = null;
  const PS_PAGE_SIZE = 15;

  function _psDisposeCharts() {
    Object.values(_psCharts).forEach(function(c) { try { c.dispose(); } catch(e) {} });
    for (var k in _psCharts) delete _psCharts[k];
  }

  function _psGetChart(domId) {
    if (typeof echarts === 'undefined' || !echarts.init) return null;
    var dom = document.getElementById(domId);
    if (!dom) return null;
    if (_psCharts[domId]) _psCharts[domId].dispose();
    var c = echarts.init(dom);
    _psCharts[domId] = c;
    return c;
  }

  function _psColorForPlatform(name) {
    var map = { '淘宝': '#FF6A00', '天猫': '#FF6A00', '千牛': '#FF6A00', '京东': '#3B82F6', '拼多多': '#E8453C', '抖音': '#8B5CF6', '抖店': '#8B5CF6', '快手': '#FF4906', '小红书': '#FE2C55', '微信小程序': '#07C160' };
    return map[name] || '#6366F1';
  }

  function _psFmtMoney(v) { return '\u00a5' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 0 }); }
  function _psFmtMoneyD(v) { return '\u00a5' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function _psFmtNum(v) { return Math.round(v).toLocaleString('zh-CN'); }
  function _psFmtPct(v) { return Number(v).toFixed(2) + '%'; }

  async function renderPlatformStore(startDate, endDate) {
    var dateStart = document.getElementById('psDateStart');
    var dateEnd = document.getElementById('psDateEnd');
    var platform = document.getElementById('psPlatform');
    var yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);

    if (dateStart) { dateStart.max = yesterday; if (!dateStart.value) dateStart.value = startDate || yesterday; }
    if (dateEnd) { dateEnd.max = yesterday; if (!dateEnd.value) dateEnd.value = endDate || yesterday; }
    _calUpdateLabel('ps');

    var start = dateStart ? dateStart.value : '';
    var end = dateEnd ? dateEnd.value : '';
    var plat = platform ? platform.value : '';

    _psUpdateDateShortcuts(start, end);

    var data = null;
    if (state.apiAvailable) {
      data = await ApiService.getPlatformStoreData(start, end, plat);
    }

    if (data) {
      _psData = data;
      _psPage = 1;
      _psExpandedStore = null;
      document.getElementById('psDetailPanel').classList.add('hidden');
      _psRenderHeader(data);
      _psRenderPlatformBars(data);
      _psRenderCharts(data);
      _psRenderTable(data);
    } else {
      _psData = null;
      document.getElementById('psPlatformCount').textContent = '--';
      document.getElementById('psStoreCount').textContent = '--';
      document.getElementById('psTotalNet').textContent = '--';
      document.getElementById('psPlatformBars').innerHTML = '<div style="text-align:center;padding:24px;color:#94a3b8">\u6682\u65e0\u6570\u636e\uff0c\u8bf7\u68c0\u67e5\u6570\u636e\u5e93\u8fde\u63a5</div>';
      document.getElementById('psStoreTbody').innerHTML = '<tr><td colspan="14" class="empty-state">\u6682\u65e0\u6570\u636e</td></tr>';
    }
  }

  function _psSetDateRange(days) {
    var end = new Date(Date.now() - 86400000);
    var start = new Date(end);
    start.setDate(start.getDate() - days + 1);
    var fmt = function(d) { return d.toISOString().slice(0, 10); };
    document.getElementById('psDateStart').value = fmt(start);
    document.getElementById('psDateEnd').value = fmt(end);
    renderPlatformStore(fmt(start), fmt(end));
  }

  function _psUpdateDateShortcuts(s, e) {
    var yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    document.querySelectorAll('.ps-ds-btn').forEach(function(btn) {
      var range = parseInt(btn.dataset.range);
      var match = false;
      if (range === 1) {
        match = (s === yesterday && e === yesterday);
      } else {
        var calcStart = new Date(Date.now() - 86400000 - (range - 1) * 86400000).toISOString().slice(0, 10);
        match = (s === calcStart && e === yesterday);
      }
      btn.classList.toggle('active', match);
    });
  }

  function _psRenderHeader(data) {
    document.getElementById('psPlatformCount').textContent = data.platforms.length;
    document.getElementById('psStoreCount').textContent = data.totalStores;
    document.getElementById('psTotalNet').textContent = _psFmtMoney(data.totalNetPayment);
    // 显示/隐藏返回按钮
    var backBtn = document.getElementById('psBackBtn');
    var currentPlat = document.getElementById('psPlatform').value;
    if (backBtn) backBtn.classList.toggle('hidden', !currentPlat);
  }

  function _psRenderPlatformBars(data) {
    var container = document.getElementById('psPlatformBars');
    if (!data.platforms || !data.platforms.length) {
      container.innerHTML = '<div style="text-align:center;padding:24px;color:#94a3b8">\u6682\u65e0\u5e73\u53f0\u6570\u636e</div>';
      return;
    }
    var maxShare = Math.max.apply(null, data.platforms.map(function(p) { return p.share; }));
    var currentPlat = document.getElementById('psPlatform').value;

    container.innerHTML = data.platforms.map(function(p) {
      var color = _psColorForPlatform(p.name);
      var isActive = currentPlat === p.name;
      return '<div class="ps-platform-bar' + (isActive ? ' active' : '') + '" onclick="App.filterByPlatform(\'' + p.name + '\')">' +
        '<div class="ps-pb-color" style="background:' + color + '"></div>' +
        '<div class="ps-pb-info"><div class="ps-pb-name">' + p.name + '</div><div class="ps-pb-stores">' + p.storeCount + ' \u5bb6\u5e97\u94fa</div></div>' +
        '<div class="ps-pb-metrics">' +
          '<div class="ps-pb-metric"><span class="ps-pb-metric-label">\u603b\u652f\u4ed8\u91d1\u989d</span><span class="ps-pb-metric-value">' + _psFmtMoney(p.payment) + '</span></div>' +
          '<div class="ps-pb-metric"><span class="ps-pb-metric-label">\u51c0\u652f\u4ed8</span><span class="ps-pb-metric-value">' + _psFmtMoney(p.netPayment) + '</span></div>' +
          '<div class="ps-pb-metric"><span class="ps-pb-metric-label">\u8bbf\u5ba2</span><span class="ps-pb-metric-value">' + _psFmtNum(p.visitors) + '</span></div>' +
          '<div class="ps-pb-metric"><span class="ps-pb-metric-label">\u4e70\u5bb6</span><span class="ps-pb-metric-value">' + _psFmtNum(p.payers) + '</span></div>' +
          '<div class="ps-pb-metric"><span class="ps-pb-metric-label">\u63a8\u5e7f\u82b1\u8d39</span><span class="ps-pb-metric-value">' + _psFmtMoney(p.adSpend) + '</span></div>' +
          '<div class="ps-pb-metric"><span class="ps-pb-metric-label">ROI</span><span class="ps-pb-metric-value">' + (p.adSpend > 0 ? (p.adTotal / p.adSpend).toFixed(2) : '-') + '</span></div>' +
        '</div>' +
        '<div class="ps-pb-share"><div class="ps-pb-share-top"><span class="ps-pb-share-pct">' + p.share.toFixed(1) + '%</span><span class="ps-pb-share-label">\u5360\u6bd4</span></div>' +
          '<div class="ps-pb-share-bar"><div class="ps-pb-share-fill" style="width:' + (p.share / maxShare * 100) + '%;background:' + color + '"></div></div></div>' +
        '<div class="ps-pb-trend neutral">--</div>' +
      '</div>';
    }).join('');
  }

  function _psRenderCharts(data) {
    _psRenderCompareChart(data);
    _psRenderPieChart(data);
  }

  function _psRenderCompareChart(data) {
    var c = _psGetChart('chartPlatformCompare');
    if (!c || !data.platforms || !data.platforms.length) return;
    var sel = document.getElementById('psCompareMetric');
    var metric = sel ? sel.value : 'netPayment';
    var opt = sel ? sel.selectedOptions[0] : null;
    var metricLabel = opt ? opt.text : '\u51c0\u652f\u4ed8\u91d1\u989d';
    var platforms = data.platforms.map(function(p) { return p.name; });
    var values = data.platforms.map(function(p) { return p[metric] || 0; });
    var colors = data.platforms.map(function(p) { return _psColorForPlatform(p.name); });
    var isMoney = metricLabel.indexOf('\u91d1\u989d') >= 0 || metricLabel.indexOf('\u82b1\u8d39') >= 0 || metricLabel.indexOf('\u6210\u4ea4') >= 0;

    c.setOption({
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: function(ps) {
          return ps.map(function(p) { return p.marker + p.name + ': ' + (isMoney ? _psFmtMoneyD(p.value) : _psFmtNum(p.value)); }).join('<br/>');
        }
      },
      grid: { left: 100, right: 50, top: 10, bottom: 20 },
      xAxis: { type: 'value', axisLabel: { fontSize: 10, formatter: function(v) { return isMoney ? (v/10000).toFixed(0)+'w' : _psFmtNum(v); } } },
      yAxis: { type: 'category', data: platforms.slice().reverse(), axisLabel: { fontSize: 11, fontWeight: 600 }, inverse: true },
      series: [{
        type: 'bar', data: values.slice().reverse().map(function(v, i) {
          return { value: v, itemStyle: { color: colors.slice().reverse()[i], borderRadius: [0, 4, 4, 0] } };
        }), barWidth: '55%', label: { show: true, position: 'right', fontSize: 10, color: '#64748b',
          formatter: function(p) { return isMoney ? _psFmtMoney(p.value) : _psFmtNum(p.value); }
        }
      }]
    });
  }

  function _psRenderPieChart(data) {
    var c = _psGetChart('chartPlatformPie');
    if (!c || !data.platforms || !data.platforms.length) return;
    var pieData = data.platforms.map(function(p) {
      return { name: p.name, value: p.netPayment, itemStyle: { color: _psColorForPlatform(p.name) } };
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

  function _psRenderTable(data) {
    var stores = (data.stores || []).slice();
    if (_psSearch) {
      var kw = _psSearch.toLowerCase();
      stores = stores.filter(function(s) {
        return s.name.toLowerCase().indexOf(kw) >= 0 || s.platform.toLowerCase().indexOf(kw) >= 0 || s.fullName.toLowerCase().indexOf(kw) >= 0;
      });
    }
    stores.sort(function(a, b) {
      var va = a[_psSortBy] || 0;
      var vb = b[_psSortBy] || 0;
      return _psSortDir === 'desc' ? vb - va : va - vb;
    });
    stores.forEach(function(s, i) { s.rank = i + 1; });

    document.getElementById('psStoreTotal').textContent = '\u5171 ' + stores.length + ' \u5bb6\u5e97\u94fa';

    var totalPages = Math.ceil(stores.length / PS_PAGE_SIZE) || 1;
    if (_psPage > totalPages) _psPage = totalPages;
    var pageStores = stores.slice((_psPage - 1) * PS_PAGE_SIZE, _psPage * PS_PAGE_SIZE);

    var maxNet = Math.max.apply(null, stores.map(function(s) { return s.netPayment; })) || 1;
    var maxVisitors = Math.max.apply(null, stores.map(function(s) { return s.visitors; })) || 1;

    var tbody = document.getElementById('psStoreTbody');
    tbody.innerHTML = pageStores.map(function(s) {
      var rankCls = s.rank === 1 ? 'top1' : (s.rank === 2 ? 'top2' : (s.rank === 3 ? 'top3' : ''));
      var platformCls = 'ps-platform-' + (s.platform || '\u5176\u4ed6');
      var isExpanded = _psExpandedStore && _psExpandedStore.fullName === s.fullName;
      var safeName = s.fullName.replace(/'/g, '\\x27').replace(/"/g, '&quot;');
      return '<tr class="' + (isExpanded ? 'expanded' : '') + '" data-store="' + safeName + '" onclick="App.toggleStoreDetail(\'' + s.fullName.replace(/'/g, '\\x27') + '\')">' +
        '<td><span class="ps-rank ' + rankCls + '">' + s.rank + '</span></td>' +
        '<td><span class="ps-store-name">' + s.name + '</span></td>' +
        '<td><span class="ps-store-platform ' + platformCls + '">' + s.platform + '</span></td>' +
        '<td class="ps-col-num">' + _psFmtMoney(s.netPayment) + '<span class="ps-inline-bar" style="width:' + Math.max(4, (s.netPayment / maxNet * 80)) + 'px;background:#6366f1"></span></td>' +
        '<td class="ps-col-num">' + _psFmtNum(s.visitors) + '<span class="ps-inline-bar" style="width:' + Math.max(4, (s.visitors / maxVisitors * 80)) + 'px;background:#10b981"></span></td>' +
        '<td class="ps-col-num">' + _psFmtNum(s.payers) + '</td>' +
        '<td class="ps-col-num">' + _psFmtMoney(s.adSpend) + '</td>' +
        '<td class="ps-col-num">' + _psFmtMoney(s.adTotal) + '</td>' +
        '<td class="ps-col-num">' + (s.roi || 0).toFixed(2) + '</td>' +
        '<td class="ps-col-num">' + _psFmtPct(s.convRate) + '</td>' +
        '<td class="ps-col-num">' + _psFmtMoneyD(s.aov) + '</td>' +
        '<td class="ps-col-num">' + _psFmtPct(s.refundRate) + '</td>' +
        '<td><div class="ps-sparkline-cell"><canvas id="spark-' + s.rank + '" width="100" height="36"></canvas></div></td>' +
        '<td><button class="ps-expand-btn' + (isExpanded ? ' active' : '') + '" onclick="event.stopPropagation();App.toggleStoreDetail(\'' + s.fullName.replace(/'/g, '\\x27') + '\')"><i class="fa-solid fa-' + (isExpanded ? 'chevron-up' : 'chevron-down') + '"></i></button></td>' +
      '</tr>';
    }).join('');

    var footer = document.getElementById('psPagination');
    var pageInfo = '\u7b2c ' + _psPage + ' / ' + totalPages + ' \u9875\uff0c\u5171 ' + stores.length + ' \u6761';
    var btnsHtml = '';
    btnsHtml += '<button ' + (_psPage <= 1 ? 'disabled' : '') + ' onclick="App.psGoPage(' + (_psPage - 1) + ')"><i class="fa-solid fa-chevron-left"></i></button>';
    for (var i = 1; i <= totalPages; i++) {
      if (totalPages <= 7 || i === 1 || i === totalPages || (i >= _psPage - 1 && i <= _psPage + 1)) {
        btnsHtml += '<button class="' + (i === _psPage ? 'active' : '') + '" onclick="App.psGoPage(' + i + ')">' + i + '</button>';
      } else if (i === _psPage - 2 || i === _psPage + 2) {
        btnsHtml += '<button disabled>...</button>';
      }
    }
    btnsHtml += '<button ' + (_psPage >= totalPages ? 'disabled' : '') + ' onclick="App.psGoPage(' + (_psPage + 1) + ')"><i class="fa-solid fa-chevron-right"></i></button>';
    footer.innerHTML = '<span>' + pageInfo + '</span><div class="ps-pagination-btns">' + btnsHtml + '</div>';

    setTimeout(function() {
      pageStores.forEach(function(s) {
        var canvas = document.getElementById('spark-' + s.rank);
        if (!canvas) return;
        var trends = (data.storeTrends && data.storeTrends[s.fullName]) || [];
        _psDrawSparkline(canvas, trends.map(function(t) { return t.netPayment; }), '#6366f1');
      });
    }, 50);
  }

  function _psDrawSparkline(canvas, values, color) {
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
    values.forEach(function(v, i) {
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

  function toggleStoreDetail(fullName) {
    if (!_psData) return;
    if (_psExpandedStore && _psExpandedStore.fullName === fullName) {
      closePsDetail();
      return;
    }
    var store = _psData.stores.find(function(s) { return s.fullName === fullName; });
    if (!store) return;
    _psExpandedStore = store;

    document.getElementById('psDetailPlatform').textContent = store.platform;
    document.getElementById('psDetailPlatform').className = 'ps-detail-platform ps-platform-' + (store.platform || '\u5176\u4ed6');
    document.getElementById('psDetailName').textContent = store.name;

    var metricsHtml = [
      { label: '\u51c0\u652f\u4ed8\u91d1\u989d', value: _psFmtMoney(store.netPayment) },
      { label: '\u9000\u6b3e\u91d1\u989d', value: _psFmtMoney(store.refundAmount) },
      { label: '\u9000\u6b3e\u7387', value: _psFmtPct(store.refundRate) },
      { label: '\u8bbf\u5ba2\u6570', value: _psFmtNum(store.visitors) },
      { label: '\u652f\u4ed8\u4e70\u5bb6\u6570', value: _psFmtNum(store.payers) },
      { label: '\u652f\u4ed8\u8f6c\u5316\u7387', value: _psFmtPct(store.convRate) },
      { label: '\u63a8\u5e7f\u82b1\u8d39', value: _psFmtMoney(store.adSpend) },
      { label: '\u63a8\u5e7f\u603b\u6210\u4ea4', value: _psFmtMoney(store.adTotal) },
      { label: 'ROI', value: (store.roi || 0).toFixed(4) },
      { label: '\u5ba2\u5355\u4ef7', value: _psFmtMoneyD(store.aov) },
      { label: '\u52a0\u8d2d\u4eba\u6570', value: _psFmtNum(store.cart || 0) },
    ].map(function(m) {
      return '<div class="ps-dm-item"><span class="ps-dm-label">' + m.label + '</span><span class="ps-dm-value">' + m.value + '</span></div>';
    }).join('');
    document.getElementById('psDetailMetrics').innerHTML = metricsHtml;

    document.getElementById('psDetailPanel').classList.remove('hidden');
    document.getElementById('psDetailPanel').scrollIntoView({ behavior: 'smooth', block: 'center' });

    _psUpdateExpandedRow();
    _psRenderStoreDetailCharts(store);
  }

  function closePsDetail() {
    _psExpandedStore = null;
    document.getElementById('psDetailPanel').classList.add('hidden');
    // 只销毁店铺详情图表，保留平台上方的对比图和饼图
    ['chartStoreTrend','chartStoreTraffic'].forEach(function(id) {
      if (_psCharts[id]) { try { _psCharts[id].dispose(); } catch(e) {} delete _psCharts[id]; }
    });
    _psUpdateExpandedRow();
  }

  function _psUpdateExpandedRow() {
    var rows = document.querySelectorAll('#psStoreTbody tr');
    rows.forEach(function(row) {
      row.classList.remove('expanded');
      var btn = row.querySelector('.ps-expand-btn');
      if (btn) {
        btn.classList.remove('active');
        btn.innerHTML = '<i class="fa-solid fa-chevron-down"></i>';
      }
    });
    if (_psExpandedStore) {
      var safeName = _psExpandedStore.fullName.replace(/'/g, '\\x27').replace(/"/g, '&quot;');
      var targetRow = document.querySelector('#psStoreTbody tr[data-store="' + safeName + '"]');
      if (targetRow) {
        targetRow.classList.add('expanded');
        var btn = targetRow.querySelector('.ps-expand-btn');
        if (btn) {
          btn.classList.add('active');
          btn.innerHTML = '<i class="fa-solid fa-chevron-up"></i>';
        }
      }
    }
  }

  function _psRenderStoreDetailCharts(store) {
    if (typeof echarts === 'undefined' || !echarts.init) return;
    var trends = (_psData.storeTrends && _psData.storeTrends[store.fullName]) || [];
    if (!trends.length) return;

    // 只销毁店铺详情图表，保留平台上方的对比图和饼图
    ['chartStoreTrend','chartStoreTraffic'].forEach(function(id) {
      if (_psCharts[id]) { try { _psCharts[id].dispose(); } catch(e) {} delete _psCharts[id]; }
    });
    var dates = trends.map(function(t) { return t.date.slice(5); });

    var c1 = _psGetChart('chartStoreTrend');
    if (c1) {
      c1.setOption({
        tooltip: { trigger: 'axis' },
        legend: { top: 0, right: 0, textStyle: { fontSize: 10 } },
        grid: { left: 50, right: 20, top: 35, bottom: 25 },
        xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
        yAxis: [
          { type: 'value', name: '\u91d1\u989d', axisLabel: { fontSize: 9, formatter: function(v) { return (v/10000).toFixed(0)+'w'; } } }
        ],
        series: [
          { name: '\u51c0\u652f\u4ed8', type: 'line', smooth: true, data: trends.map(function(t) { return t.netPayment; }), lineStyle: { color: '#6366f1' }, itemStyle: { color: '#6366f1' }, symbol: 'circle', symbolSize: 4, areaStyle: { color: 'rgba(99,102,241,0.06)' } },
          { name: '\u9000\u6b3e', type: 'line', smooth: true, data: trends.map(function(t) { return t.refundAmount; }), lineStyle: { color: '#ef4444' }, itemStyle: { color: '#ef4444' }, symbol: 'circle', symbolSize: 4, areaStyle: { color: 'rgba(239,68,68,0.04)' } },
          { name: '\u63a8\u5e7f\u82b1\u8d39', type: 'line', smooth: true, data: trends.map(function(t) { return t.adSpend; }), lineStyle: { color: '#f59e0b', type: 'dashed' }, itemStyle: { color: '#f59e0b' }, symbol: 'diamond', symbolSize: 3 }
        ]
      });
    }

    var c2 = _psGetChart('chartStoreTraffic');
    if (c2) {
      c2.setOption({
        tooltip: { trigger: 'axis' },
        legend: { top: 0, right: 0, textStyle: { fontSize: 10 } },
        grid: { left: 50, right: 20, top: 35, bottom: 25 },
        xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 9 } },
        yAxis: [
          { type: 'value', name: '\u4eba\u6570', axisLabel: { fontSize: 9 } }
        ],
        series: [
          { name: '\u8bbf\u5ba2\u6570', type: 'bar', data: trends.map(function(t) { return t.visitors; }), itemStyle: { color: '#818cf8', borderRadius: [4,4,0,0] }, barGap: '15%', barWidth: '35%' },
          { name: '\u4e70\u5bb6\u6570', type: 'bar', data: trends.map(function(t) { return t.payers; }), itemStyle: { color: '#34d399', borderRadius: [4,4,0,0] }, barWidth: '35%' }
        ]
      });
    }
  }

  function psGoPage(page) {
    _psPage = page;
    if (_psData) _psRenderTable(_psData);
    _psExpandedStore = null;
    document.getElementById('psDetailPanel').classList.add('hidden');
  }

  function filterByPlatform(plat) {
    document.getElementById('psPlatform').value = plat;
    _psPage = 1;
    _psExpandedStore = null;
    document.getElementById('psDetailPanel').classList.add('hidden');
    renderPlatformStore();
  }

  // ==================== 每日数据分析 ====================
  async function renderDailyAnalysis() {
    if (state.apiAvailable) {
      var dates = await ApiService.getAnalysisDates();
      var sel = document.getElementById('daDateSelect');
      if (sel && dates) {
        sel.innerHTML = '<option value="">选择历史报告...</option>';
        dates.forEach(function(d) {
          var opt = document.createElement('option');
          opt.value = d.date;
          opt.textContent = d.date + (d.status === 'success' ? '' : ' (生成失败)');
          sel.appendChild(opt);
        });
      }

      var latest = await ApiService.getAnalysisReport();
      if (latest) {
        showReport(latest);
      } else {
        showEmpty();
      }
    } else {
      showEmpty();
    }
  }

  async function generateDailyReport() {
    if (!state.apiAvailable) {
      showToast('后端API不可用，请检查服务连接', 'error');
      return;
    }

    // 读取选中的日期
    var sel = document.getElementById('daDateSelect');
    var selectedDate = sel && sel.value ? sel.value : null;

    var dateLabel = selectedDate || '昨日';
    document.getElementById('daEmpty').classList.add('hidden');
    document.getElementById('daReportContainer').innerHTML = '';
    document.getElementById('daLoading').classList.remove('hidden');
    var btn = document.getElementById('daGenerateBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 分析中...';

    try {
      var result = await ApiService.generateDailyReport(selectedDate);
      document.getElementById('daLoading').classList.add('hidden');
      btn.disabled = false;
      btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i> 生成' + dateLabel + '数据分析文档';

      if (result) {
        showReport(result);
        // 更新日期下拉为刚生成的日期
        if (sel && result.reportDate) sel.value = result.reportDate;
        renderDailyAnalysis(); // 刷新日期列表
        var label = result.generatedBy === 'ai' ? '已生成' : '已生成（模板模式）';
        showToast('分析报告' + label, 'success');
      } else {
        showError('生成失败，请检查后端日志或数据状态。');
      }
    } catch (e) {
      document.getElementById('daLoading').classList.add('hidden');
      btn.disabled = false;
      btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i> 生成' + dateLabel + '数据分析文档';
      showError('请求异常: ' + (e.message || '未知错误'));
    }
  }

  function showReport(data) {
    document.getElementById('daEmpty').classList.add('hidden');
    document.getElementById('daLoading').classList.add('hidden');
    document.getElementById('daReportContainer').innerHTML = data.report || '';
    // 显示下载按钮
    var downloadBtn = document.getElementById('daDownloadBtn');
    if (downloadBtn) { downloadBtn.classList.remove('hidden'); downloadBtn.dataset.reportDate = data.reportDate || ''; }
  }

  function downloadReport() {
    var btn = document.getElementById('daDownloadBtn');
    var reportDate = btn ? btn.dataset.reportDate : '';
    var url = '/api/analysis/download';
    if (reportDate) url += '?date=' + encodeURIComponent(reportDate);
    // 用隐藏 <a download> 触发下载，避免 window.open 打开空白页
    var a = document.createElement('a');
    a.href = url;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    showToast('报告下载已开始', 'success');
  }

  function showEmpty() {
    document.getElementById('daLoading').classList.add('hidden');
    document.getElementById('daReportContainer').innerHTML = '';
    document.getElementById('daEmpty').classList.remove('hidden');
    var downloadBtn = document.getElementById('daDownloadBtn');
    if (downloadBtn) downloadBtn.classList.add('hidden');
  }

  // ==================== 每日数据分析智能体 ====================
  var _daAgentBusy = false;

  function daAgentAsk(question) {
    const input = document.getElementById('daAgentInput');
    if (input) input.value = question;
    daAgentSend();
  }

  async function daAgentSend() {
    if (_daAgentBusy) return;
    const input = document.getElementById('daAgentInput');
    const btn = document.getElementById('daAgentSendBtn');
    const result = document.getElementById('daAgentResult');
    if (!input || !result) return;
    const question = input.value.trim();
    if (!question) { showToast('请输入分析需求', 'error'); return; }

    _daAgentBusy = true;
    if (btn) btn.disabled = true;
    input.value = '';
    result.innerHTML = '<div class="sa-loading"><i class="fa-solid fa-spinner"></i> 正在检索经营数据并分析，请稍候...</div>';

    try {
      let data = null;
      if (state.apiAvailable) data = await ApiService.runAnalysisAgent(question, '');
      renderDaAgentResult(data);
    } catch (e) {
      result.innerHTML = '<div class="sa-analysis" style="color:#dc2626">分析失败：网络异常，请稍后再试。</div>';
    } finally {
      _daAgentBusy = false;
      if (btn) btn.disabled = false;
    }
  }

  function renderDaAgentResult(data) {
    const result = document.getElementById('daAgentResult');
    if (!result) return;

    if (!data) {
      result.innerHTML = '<div class="sa-analysis" style="color:#dc2626">后端服务不可用，无法完成分析。</div>';
      return;
    }

    const analysis = (data.analysis && data.analysis.trim()) || '';
    const cards = data.cards || [];
    const meta = data.meta || {};

    let html = '';
    if (analysis) {
      html += '<div class="sa-analysis">' + analysis.replace(/\n/g, '<br>') + '</div>';
    }
    if (cards.length) {
      html += '<div class="sa-cards-title"><i class="fa-solid fa-lightbulb" style="color:#f59e0b"></i> 关键数据卡片</div>';
      html += cards.map(function (c) {
        const type = ['store', 'platform', 'category', 'alert'].indexOf(c.type) >= 0 ? c.type : 'store';
        const tags = String(c.tags || '').split(/[,，]/).filter(Boolean).map(function (t) {
          return '<span class="sa-tag ' + type + '">' + esc(t.trim()) + '</span>';
        }).join('');
        return '<div class="sa-card">' +
          '<div class="sa-card-top">' +
            '<div class="sa-card-title">' + esc(c.title || '') + '</div>' +
            '<div class="sa-card-metric">' + esc(c.metric || '') + '</div>' +
          '</div>' +
          (c.subtitle ? '<div class="sa-card-sub">' + esc(c.subtitle) + '</div>' : '') +
          (tags ? '<div class="sa-card-tags">' + tags + '</div>' : '') +
          (c.reason ? '<div class="sa-card-reason">' + esc(c.reason) + '</div>' : '') +
          '</div>';
      }).join('');
    } else if (!analysis) {
      html += '<div class="sa-analysis">智能体未返回有效结果，请稍后重试。</div>';
    }
    if (meta && meta.date) {
      html += '<div class="sa-meta">数据日期：' + esc(meta.date) +
        (meta['净支付金额'] !== undefined ? ' · 净支付 ' + esc(meta['净支付金额']) + ' 元' : '') +
        (meta['ROI'] !== undefined ? ' · ROI ' + esc(meta['ROI']) : '') + '</div>';
    }
    result.innerHTML = html;
  }

  function showError(msg) {
    document.getElementById('daLoading').classList.add('hidden');
    document.getElementById('daEmpty').classList.add('hidden');
    var downloadBtn = document.getElementById('daDownloadBtn');
    if (downloadBtn) downloadBtn.classList.add('hidden');
    document.getElementById('daReportContainer').innerHTML =
      '<div class="da-error-card">' +
      '<i class="fa-solid fa-circle-exclamation"></i>' +
      '<h3>报告生成失败</h3>' +
      '<p>' + (msg || '未知错误') + '</p>' +
      '</div>';
  }

  // ==================== 角色权限配置（数据库驱动） ====================
  var _ALLOWED_PAGES = null;  // null = 全部页面，array = 限定页面

  // ★★ 「管理员与权限」页面权限模型（2026-09-18 调整）
  //
  //   第 1 层  开发人员 / 超级管理员 —— 全库账号 + 角色与权限，无限制
  //   第 2 层  部门主管             —— 只能管本部门账号；看不到「角色与权限」
  //   第 3 层  普通员工             —— 无账号管理入口
  //
  // ★ 与原行为差异：ACCOUNT_MANAGER_ROLES 由 {开发人员,超级管理员,人事行政部} 收窄为
  //   前两者。判定最终以后端 /api/admin/my-scope 为准（前端只做显示层，
  //   真正的拦截在后端 _my_scope / _can_manage_roles）。
  var ACCOUNT_MANAGER_ROLES = ['开发人员', '超级管理员'];
  var ROLE_PERM_MANAGER_ROLES = ['开发人员', '超级管理员'];
  var _CAN_MANAGE_ACCOUNTS = false;
  var _CAN_MANAGE_ROLES = false;
  var _IS_DEPT_LEAD = false;
  // 后端 /api/admin/my-scope 的返回（进入页面时拉一次），主管相关显示都基于它
  var _MY_SCOPE = null;

  function _initPermissions() {
    // 优先从 sessionStorage 读取（由登录 API 返回的 permissions 数组）
    var perms = state.currentPermissions;
    if (!perms || !perms.length) {
      try { perms = JSON.parse(sessionStorage.getItem('admin_permissions')); }
      catch (e) { perms = null; }
    }

    var role = state.currentRole || sessionStorage.getItem('admin_current_role') || '';
    _CAN_MANAGE_ACCOUNTS = ACCOUNT_MANAGER_ROLES.indexOf(role) >= 0;
    _CAN_MANAGE_ROLES = ROLE_PERM_MANAGER_ROLES.indexOf(role) >= 0;
    sessionStorage.setItem('admin_is_account_manager', _CAN_MANAGE_ACCOUNTS ? '1' : '0');
    sessionStorage.setItem('admin_is_role_manager', _CAN_MANAGE_ROLES ? '1' : '0');


    // '*' 表示全部权限
    if (perms === '*' || (Array.isArray(perms) && perms[0] === '*')) {
      _ALLOWED_PAGES = null;
      return;
    }

    // ★ 空权限 = 只能进「个人中心设置」（个人中心对所有账号强制开放，见 navigateTo）
    perms = Array.isArray(perms) ? perms.slice() : [];
    if (perms.indexOf('profile') < 0) perms.push('profile');
    _ALLOWED_PAGES = perms;
    sessionStorage.setItem('admin_permissions', JSON.stringify(perms));
  }

  // 所有可用页面及其分组（用于权限分配 UI）
  var PAGE_CATEGORIES = [
    { group: '营销数据', pages: [
      { id: 'marketing-overview', name: '整体营销数据总览' },
      { id: 'platform-store', name: '分平台/店铺详细数据' },
      { id: 'daily-analysis', name: '每日数据分析' },
      { id: 'order-details', name: '订单详情' },
      { id: 'category-marketing', name: '品类营销数据' },
    ]},
    { group: '店铺运营', pages: [
      { id: 'store-account', name: '店铺账号管理' },
      { id: 'operation-performance', name: '运营业绩面板' },
      { id: 'product-selection', name: '选品助手' },
      { id: 'seeding-monitor', name: '种草监测中台' },
    ]},
    { group: '财务中心', pages: [
      { id: 'finance', name: '财务中心' },
    ]},
    { group: '人事中心', pages: [
      { id: 'hr-roster', name: '员工花名册' },
      { id: 'hr-interview', name: '面试信息登记表' },
      { id: 'hr-onboarding', name: '入职人员信息统计表' },
      { id: 'hr-salary-a', name: '人员薪资标准（表 a）' },
      { id: 'hr-salary-b', name: '人员薪资标准（表 b）' },
    ]},
    { group: '工具箱', pages: [
      { id: 'toolbox-violation-check', name: '违规词检测' },
    ]},
    { group: '系统管理', pages: [
      { id: 'admin-permissions', name: '管理员与权限' },
      { id: 'profile', name: '个人中心设置' },
    ]},
  ];

  // ==================== 管理员与权限 ====================
  const ADMIN_KEY = 'admin_permissions_admins';
  const ROLE_KEY = 'admin_permissions_roles';
  let _apAdmins = [];
  let _apPwdVisible = {};
  let _apRoles = [];  // 数据库加载的角色列表

  function getDefaultAdmins() {
    return [
      { id: 1, name: '张经理', account: 'admin', password: 'admin123', role: '超级管理员', status: 'enabled', lastLogin: '2026-07-30 09:15' },
      { id: 2, name: '李运营', account: 'liyunying', password: '123456', role: '运营主管', status: 'enabled', lastLogin: '2026-07-29 17:40' },
      { id: 3, name: '王财务', account: 'wangcaiwu', password: '123456', role: '财务专员', status: 'disabled', lastLogin: '2026-07-20 14:22' },
    ];
  }

  function getDefaultRoles() {
    return [
      { id: 1, name: '超级管理员', desc: '拥有系统全部权限，可管理所有模块', count: 1, createdAt: '2026-06-01' },
      { id: 2, name: '运营主管', desc: '可管理商品、订单、营销数据，查看运营报表', count: 3, createdAt: '2026-06-15' },
      { id: 3, name: '财务专员', desc: '可查看财务中心、导出财务报表、处理退款', count: 2, createdAt: '2026-07-01' },
      { id: 4, name: '客服人员', desc: '可查看订单详情、处理售后、回复评价', count: 5, createdAt: '2026-07-10' },
    ];
  }

  function getRoles() {
    try { return JSON.parse(localStorage.getItem(ROLE_KEY)) || getDefaultRoles(); }
    catch (e) { return getDefaultRoles(); }
  }
  function saveRoles(roles) { localStorage.setItem(ROLE_KEY, JSON.stringify(roles)); }

  function syncRoleCounts() {
    if (_apRoles.length > 0) {
      _apRoles.forEach(function(r) { r.count = _apAdmins.filter(function(a) { return a.role === r.name; }).length; });
    } else {
      var roles = getRoles();
      roles.forEach(function(r) { r.count = _apAdmins.filter(function(a) { return a.role === r.name; }).length; });
      saveRoles(roles);
    }
  }

  async function renderAdminPermissions() {
    // 加载管理员
    if (state.apiAvailable) {
      var dbAdmins = await ApiService.getAdmins();
      if (dbAdmins && dbAdmins.length) {
        _apAdmins = dbAdmins;
      } else {
        try { _apAdmins = JSON.parse(localStorage.getItem(ADMIN_KEY)) || getDefaultAdmins(); }
        catch (e) { _apAdmins = getDefaultAdmins(); }
      }
      // 同时加载角色
      try {
        var rolesResp = await fetch('/api/admin/roles');
        var rolesJson = await rolesResp.json();
        if (rolesJson.code === 0 && rolesJson.data) {
          _apRoles = rolesJson.data;
        }
      } catch (e) { console.warn('加载角色列表失败'); }
    } else {
      try { _apAdmins = JSON.parse(localStorage.getItem(ADMIN_KEY)) || getDefaultAdmins(); }
      catch (e) { _apAdmins = getDefaultAdmins(); }
      _apRoles = [];
    }
    syncRoleCounts();
    renderAdminTable();
    renderRoleTable();
    var activeTab = document.querySelector('.ap-tab.active');
    if (!activeTab) {
      var tab = document.querySelector('.ap-tab[data-tab="admins"]');
      if (tab) tab.classList.add('active');
      var panel = document.getElementById('ap-panel-admins');
      if (panel) panel.classList.add('active');
    }
  }

  function switchAdminTab(tabName) {
    document.querySelectorAll('.ap-tab').forEach(function(t) { t.classList.toggle('active', t.dataset.tab === tabName); });
    document.querySelectorAll('.ap-panel').forEach(function(p) { p.classList.toggle('active', p.id === 'ap-panel-' + tabName); });
  }

  function renderAdminTable(filter) {
    var admins = _apAdmins.slice();
    if (filter) {
      var kw = filter.toLowerCase();
      admins = admins.filter(function(a) { return a.name.toLowerCase().indexOf(kw) >= 0 || a.account.toLowerCase().indexOf(kw) >= 0 || a.role.toLowerCase().indexOf(kw) >= 0; });
    }
    var tbody = document.getElementById('adminTableBody');
    if (!tbody) return;
    var isSuperAdmin = state.currentRole === '超级管理员' || state.currentAccount === 'admin';

    tbody.innerHTML = admins.map(function(a) {
      var pwdVisible = _apPwdVisible[a.id] || false;
      var pwdDisplay = isSuperAdmin ? (pwdVisible ? a.password : '●●●●●●') : '●●●●●●';
      return '<tr>' +
        '<td>' + a.id + '</td>' +
        '<td><strong>' + esc(a.name) + '</strong></td>' +
        '<td>' + esc(a.account) + '</td>' +
        '<td><div class="ap-pwd-cell">' +
          '<span class="ap-pwd-text">' + pwdDisplay + '</span>' +
          (isSuperAdmin ? '<button class="ap-pwd-toggle' + (pwdVisible ? ' showing' : '') + '" onclick="App.togglePwdVis(' + a.id + ')" title="显示/隐藏密码"><i class="fa-solid fa-' + (pwdVisible ? 'eye-slash' : 'eye') + '"></i></button>' : '') +
        '</div></td>' +
        '<td>' + esc(a.role) + '</td>' +
        '<td><span class="status-badge ' + a.status + '">' + (a.status === 'enabled' ? '已启用' : '已禁用') + '</span></td>' +
        '<td>' + esc(a.lastLogin || '-') + '</td>' +
        '<td><div class="ap-actions">' +
          (isSuperAdmin ? '<button class="ap-btn-sm pwd" onclick="App.openPwdModal(' + a.id + ')" title="修改密码"><i class="fa-solid fa-key"></i> 密码</button>' : '') +
          '<button class="ap-btn-sm edit" onclick="App.openAdminModal(' + a.id + ')"><i class="fa-solid fa-pen"></i> 编辑</button>' +
          (a.account !== 'admin' ? '<button class="ap-btn-sm toggle" onclick="App.toggleAdmin(' + a.id + ')" title="' + (a.status === 'enabled' ? '禁用' : '启用') + '"><i class="fa-solid fa-power-off"></i></button>' : '') +
          (a.account !== 'admin' ? '<button class="ap-btn-sm delete" onclick="App.deleteAdmin(' + a.id + ')"><i class="fa-solid fa-trash"></i></button>' : '') +
        '</div></td>' +
      '</tr>';
    }).join('');

    document.getElementById('adminTableInfo').textContent = '共 ' + admins.length + ' 位管理员';
  }

  function renderRoleTable() {
    var tbody = document.getElementById('roleTableBody');
    if (!tbody) return;
    var isSuperAdmin = state.currentRole === '超级管理员';
    // 优先显示数据库角色，降级到本地
    var roles = _apRoles.length > 0 ? _apRoles : getRoles();
    tbody.innerHTML = roles.map(function(r) {
      var perms = r.permissions || [];
      var permDisplay = '';
      if (Array.isArray(perms) && perms[0] === '*') {
        permDisplay = '<span style="color:#16a34a;font-weight:600">全部权限</span>';
      } else if (perms.length > 0) {
        permDisplay = '<span style="color:#6366f1">' + perms.length + ' 项</span>';
      } else {
        permDisplay = '<span style="color:#94a3b8">无权限</span>';
      }
      return '<tr>' +
        '<td><strong>' + esc(r.name) + '</strong></td>' +
        '<td>' + permDisplay + '</td>' +
        '<td>' + (r.count || 0) + '</td>' +
        '<td>' + esc(r.createdAt || '') + '</td>' +
        '<td>' +
          (isSuperAdmin ? '<button class="btn btn-sm btn-outline" onclick="App.openRolePermModal(' + r.id + ')"><i class="fa-solid fa-pen"></i> 权限</button>' : '') +
          (isSuperAdmin && r.name !== '超级管理员' ? '<button class="btn btn-sm btn-outline" onclick="App.deleteRole(' + r.id + ')" style="color:var(--danger)"><i class="fa-solid fa-trash"></i></button>' : '') +
        '</td>' +
      '</tr>';
    }).join('');
  }

  function togglePwdVis(id) {
    _apPwdVisible[id] = !_apPwdVisible[id];
    renderAdminTable(document.getElementById('adminSearch').value);
  }

  function openPwdModal(id) {
    var admin = _apAdmins.find(function(a) { return a.id === id; });
    if (!admin) return;
    var html = '<div class="ap-form-group"><label>管理员</label><div style="padding:9px 0;font-weight:600;color:#1e293b">' + esc(admin.name) + '（' + esc(admin.account) + '）</div></div>' +
      '<div class="ap-form-group"><label>新密码</label><input class="ap-form-input" type="text" id="apNewPwd" placeholder="请输入新密码"></div>' +
      '<input type="hidden" id="apPwdId" value="' + admin.id + '">';

    document.getElementById('modalTitle').textContent = '修改密码';
    document.getElementById('modalBody').innerHTML = html;
    document.getElementById('modalSaveBtn').onclick = savePwd;
    document.getElementById('formModal').classList.remove('hidden');
  }

  async function savePwd() {
    var id = parseInt(document.getElementById('apPwdId').value);
    var pwd = document.getElementById('apNewPwd').value.trim();
    if (!pwd) { showToast('请输入新密码', 'error'); return; }

    if (state.apiAvailable) {
      var result = await ApiService.updateAdmin(id, { password: pwd });
      if (result !== null) {
        document.getElementById('formModal').classList.add('hidden');
        showToast('密码已更新');
        renderAdminPermissions();
        return;
      }
    }
    // 回退 localStorage
    var admins = getDefaultAdmins();
    try { admins = JSON.parse(localStorage.getItem(ADMIN_KEY)) || getDefaultAdmins(); } catch(e) {}
    var idx = admins.findIndex(function(a) { return a.id === id; });
    if (idx >= 0) { admins[idx].password = pwd; localStorage.setItem(ADMIN_KEY, JSON.stringify(admins)); }
    document.getElementById('formModal').classList.add('hidden');
    showToast('密码已更新');
    renderAdminPermissions();
  }

  function openAdminModal(id) {
    var admin = id ? _apAdmins.find(function(a) { return a.id === id; }) : null;
    var title = admin ? '编辑管理员' : '新增管理员';
    // 优先用数据库角色
    var roles = _apRoles.length > 0 ? _apRoles : getRoles();
    var roleOpts = roles.map(function(r) { return '<option value="' + esc(r.name) + '" ' + (admin && admin.role === r.name ? 'selected' : '') + '>' + esc(r.name) + '</option>'; }).join('');

    var html = '<div class="ap-form-row"><div class="ap-form-group"><label>姓名</label><input class="ap-form-input" id="apAdminName" value="' + esc(admin ? admin.name : '') + '" placeholder="请输入姓名"></div>' +
      '<div class="ap-form-group"><label>账号</label><input class="ap-form-input" id="apAdminAccount" value="' + esc(admin ? admin.account : '') + '" placeholder="请输入账号" ' + (admin ? 'readonly' : '') + '></div></div>' +
      '<div class="ap-form-row"><div class="ap-form-group"><label>密码</label><input class="ap-form-input" type="text" id="apAdminPwd" placeholder="' + (admin ? '留空则不修改' : '请输入密码') + '"></div>' +
      '<div class="ap-form-group"><label>角色</label><select class="ap-form-input" id="apAdminRole">' + roleOpts + '</select></div></div>' +
      '<div class="ap-form-group"><label>状态</label><select class="ap-form-input" id="apAdminStatus">' +
        '<option value="enabled" ' + (admin && admin.status === 'enabled' ? 'selected' : '') + '>已启用</option>' +
        '<option value="disabled" ' + (admin && admin.status === 'disabled' ? 'selected' : '') + '>已禁用</option>' +
      '</select></div>' +
      '<input type="hidden" id="apAdminId" value="' + (admin ? admin.id : '') + '">';

    document.getElementById('modalTitle').textContent = title;
    document.getElementById('modalBody').innerHTML = html;
    document.getElementById('modalSaveBtn').onclick = saveAdmin;
    document.getElementById('formModal').classList.remove('hidden');
  }

  function editAdmin(id) { openAdminModal(id); }

  async function saveAdmin() {
    var id = document.getElementById('apAdminId').value;
    var name = document.getElementById('apAdminName').value.trim();
    var account = document.getElementById('apAdminAccount').value.trim();
    var pwd = document.getElementById('apAdminPwd').value.trim();
    var role = document.getElementById('apAdminRole').value;
    var status = document.getElementById('apAdminStatus').value;

    if (!name || !account || (!id && !pwd)) { showToast('请填写完整信息', 'error'); return; }

    if (state.apiAvailable) {
      var result;
      if (id) {
        var data = { name: name, role: role, status: status };
        if (pwd) data.password = pwd;
        result = await ApiService.updateAdmin(parseInt(id), data);
      } else {
        result = await ApiService.createAdmin({ name: name, account: account, password: pwd, role: role, status: status });
      }
      if (result !== null) {
        document.getElementById('formModal').classList.add('hidden');
        showToast(id ? '管理员已更新' : '管理员已创建');
        renderAdminPermissions();
        return;
      }
    }

    // 回退 localStorage
    var admins = getDefaultAdmins();
    try { admins = JSON.parse(localStorage.getItem(ADMIN_KEY)) || getDefaultAdmins(); } catch(e) {}
    if (!id && admins.find(function(a) { return a.account === account; })) { showToast('账号已存在', 'error'); return; }
    if (id) {
      var idx = admins.findIndex(function(a) { return a.id === parseInt(id); });
      if (idx >= 0) { admins[idx].name = name; admins[idx].role = role; admins[idx].status = status; if (pwd) admins[idx].password = pwd; }
    } else {
      var newId = admins.length ? Math.max.apply(null, admins.map(function(a) { return a.id; })) + 1 : 1;
      admins.push({ id: newId, name: name, account: account, password: pwd, role: role, status: status, lastLogin: '-' });
    }
    localStorage.setItem(ADMIN_KEY, JSON.stringify(admins));
    _apAdmins = admins;
    document.getElementById('formModal').classList.add('hidden');
    showToast(id ? '管理员已更新' : '管理员已创建');
    renderAdminPermissions();
  }

  async function deleteAdmin(id) {
    var a = _apAdmins.find(function(x) { return x.id === id; });
    if (!a) return;
    if (a.account === 'admin') { showToast('不能删除超级管理员账号', 'error'); return; }
    document.getElementById('confirmMsg').textContent = '确定删除管理员「' + a.name + '」吗？此操作不可恢复。';
    document.getElementById('confirmDeleteBtn').onclick = async function() {
      if (state.apiAvailable) {
        await ApiService.deleteAdmin(id);
      } else {
        var admins = getDefaultAdmins();
        try { admins = JSON.parse(localStorage.getItem(ADMIN_KEY)) || getDefaultAdmins(); } catch(e) {}
        admins = admins.filter(function(x) { return x.id !== id; });
        localStorage.setItem(ADMIN_KEY, JSON.stringify(admins));
        _apAdmins = admins;
      }
      closeConfirmModal();
      renderAdminPermissions();
      showToast('管理员已删除');
    };
    document.getElementById('confirmModal').classList.remove('hidden');
  }

  async function toggleAdmin(id) {
    var a = _apAdmins.find(function(x) { return x.id === id; });
    if (!a) return;
    if (a.account === 'admin') { showToast('不能禁用超级管理员账号', 'error'); return; }
    var newStatus = a.status === 'enabled' ? 'disabled' : 'enabled';
    if (state.apiAvailable) {
      await ApiService.updateAdmin(id, { status: newStatus });
    } else {
      a.status = newStatus;
      var admins = getDefaultAdmins();
      try { admins = JSON.parse(localStorage.getItem(ADMIN_KEY)) || getDefaultAdmins(); } catch(e) {}
      var idx = admins.findIndex(function(x) { return x.id === id; });
      if (idx >= 0) { admins[idx].status = newStatus; localStorage.setItem(ADMIN_KEY, JSON.stringify(admins)); }
    }
    renderAdminPermissions();
    showToast('管理员已' + (newStatus === 'enabled' ? '启用' : '禁用'));
  }

  function openRolePermModal(id) {
    var role = id ? _apRoles.find(function(r) { return r.id === id; }) : null;
    var isNew = !role;
    var title = isNew ? '新增角色' : '编辑角色 - ' + esc(role.name);
    var perms = role && role.permissions ? role.permissions : [];
    var isAll = Array.isArray(perms) && perms[0] === '*';

    // Build permission checkbox groups from PAGE_CATEGORIES
    var permGroupsHtml = '';
    var allPageIds = [];
    PAGE_CATEGORIES.forEach(function(cat) {
      var checksHtml = cat.pages.map(function(p) {
        allPageIds.push(p.id);
        var checked = isAll || perms.indexOf(p.id) >= 0 ? ' checked' : '';
        return '<label class="ap-perm-check"><input type="checkbox" value="' + p.id + '"' + checked + '> ' + esc(p.name) + '</label>';
      }).join('');
      permGroupsHtml += '<div class="ap-perm-group"><div class="ap-perm-group-title">' + esc(cat.group) + '</div><div class="ap-perm-group-checks">' + checksHtml + '</div></div>';
    });

    var html = '<div class="form-group"><label>角色名称</label><input class="form-input" id="apRoleName" value="' + esc(role ? role.name : '') + '" placeholder="请输入角色名称"></div>' +
      '<div class="ap-perm-header"><span>权限分配</span>' +
        '<button type="button" class="ap-perm-toggle-btn" onclick="App.apToggleAllPerms(this)">全选</button>' +
      '</div>' +
      '<div class="ap-perm-scroll" id="apPermCheckboxes">' + permGroupsHtml + '</div>' +
      '<input type="hidden" id="apRoleId" value="' + (role ? role.id : '') + '">';

    document.getElementById('modalTitle').textContent = title;
    document.getElementById('modalBody').innerHTML = html;
    document.getElementById('modalSaveBtn').onclick = saveRolePerm;
    document.getElementById('formModal').classList.remove('hidden');
  }

  function apToggleAllPerms(el) {
    var container = document.getElementById('apPermCheckboxes');
    if (!container) return;
    var checkboxes = container.querySelectorAll('input[type="checkbox"]');
    var allChecked = el.textContent === '全选';
    checkboxes.forEach(function(cb) { cb.checked = allChecked; });
    el.textContent = allChecked ? '取消全选' : '全选';
  }

  async function saveRolePerm() {
    var id = document.getElementById('apRoleId').value;
    var name = document.getElementById('apRoleName').value.trim();
    if (!name) { showToast('请输入角色名称', 'error'); return; }
    var container = document.getElementById('apPermCheckboxes');
    var perms = [];
    if (container) {
      container.querySelectorAll('input[type="checkbox"]:checked').forEach(function(cb) {
        perms.push(cb.value);
      });
    }
    if (state.apiAvailable) {
      try {
        var resp;
        if (id) {
          resp = await fetch('/api/admin/roles/' + id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name, permissions: perms }),
          });
        } else {
          resp = await fetch('/api/admin/roles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name, permissions: perms }),
          });
        }
        var json = await resp.json();
        if (json.code === 0) {
          document.getElementById('formModal').classList.add('hidden');
          showToast(id ? '角色已更新' : '角色已创建');
          renderAdminPermissions();
          return;
        } else {
          showToast(json.msg || '操作失败', 'error');
          return;
        }
      } catch (e) {
        showToast('请求失败: ' + e.message, 'error');
        return;
      }
    }
    // 降级 localStorage
    var roles = getRoles();
    if (id) {
      var idx = roles.findIndex(function(r) { return r.id === parseInt(id); });
      if (idx >= 0) { roles[idx].name = name; roles[idx].permissions = perms; }
    } else {
      var newId = roles.length ? Math.max.apply(null, roles.map(function(r) { return r.id; })) + 1 : 1;
      roles.push({ id: newId, name: name, permissions: perms, count: 0, createdAt: new Date().toISOString().slice(0, 10) });
    }
    saveRoles(roles);
    _apRoles = roles;
    document.getElementById('formModal').classList.add('hidden');
    showToast(id ? '角色已更新' : '角色已创建');
    renderAdminPermissions();
  }

  async function deleteRole(id) {
    if (state.apiAvailable) {
      var a = _apRoles.find(function(r) { return r.id === id; });
      if (!a) return;
      if (a.name === '超级管理员') { showToast('不能删除超级管理员角色', 'error'); return; }
      document.getElementById('confirmMsg').textContent = '确定删除角色「' + a.name + '」吗？';
      document.getElementById('confirmDeleteBtn').onclick = async function() {
        try {
          var resp = await fetch('/api/admin/roles/' + id, { method: 'DELETE' });
          var json = await resp.json();
          if (json.code === 0) {
            closeConfirmModal();
            renderAdminPermissions();
            showToast('角色已删除');
          } else {
            closeConfirmModal();
            showToast(json.msg || '删除失败', 'error');
          }
        } catch (e) {
          closeConfirmModal();
          showToast('删除失败: ' + e.message, 'error');
        }
      };
      document.getElementById('confirmModal').classList.remove('hidden');
      return;
    }
    // 降级 localStorage
    var roles = getRoles();
    var r = roles.find(function(x) { return x.id === id; });
    if (!r) return;
    if (r.name === '超级管理员') { showToast('不能删除超级管理员角色', 'error'); return; }
    document.getElementById('confirmDeleteBtn').onclick = function() {
      saveRoles(roles.filter(function(x) { return x.id !== id; }));
      syncRoleCounts();
      closeConfirmModal();
      renderAdminPermissions();
      showToast('角色已删除');
    };
    document.getElementById('confirmMsg').textContent = '确定删除角色「' + r.name + '」吗？删除后对应管理员将失去角色关联。';
    document.getElementById('confirmModal').classList.remove('hidden');
  }

  function filterAdminTable() {
    var kw = document.getElementById('adminSearch').value;
    renderAdminTable(kw);
  }

  // ==================== 初始化 ====================
  async function initApp() {
    // ---- 清除旧的 localStorage 假数据（迁移到数据库直读）----
    if (localStorage.getItem('admin_data') && !localStorage.getItem('admin_data_migrated')) {
      console.log('[App] 清除旧的本地模拟数据，改用数据库直读');
      localStorage.removeItem('admin_data');
      localStorage.setItem('admin_data_migrated', '1');
    }

    // ---- 检测后端 API 是否可用（重试2次） ----
    for (let retry = 0; retry < 3; retry++) {
      state.apiAvailable = await ApiService.health();
      if (state.apiAvailable) break;
      if (retry < 2) await new Promise(r => setTimeout(r, 600));
    }
    console.log('[App] API状态:', state.apiAvailable ? '已连接' : '不可用（3次重试后）');

    // Nav click events
    document.querySelectorAll('.nav-item').forEach(el => {
      el.addEventListener('click', function(e) {
        e.preventDefault();
        navigateTo(this.dataset.page);
      });
    });

    // Nav group expand/collapse
    document.querySelectorAll('.nav-group-title').forEach(el => {
      el.addEventListener('click', function(e) {
        e.preventDefault();
        this.closest('.nav-group').classList.toggle('open');
      });
    });

    // Nav submenu expand/collapse
    document.querySelectorAll('.nav-submenu-toggle').forEach(el => {
      el.addEventListener('click', function(e) {
        e.preventDefault();
        e.stopPropagation();
        this.closest('.nav-submenu').classList.toggle('open');
      });
    });

    // Menu toggle
    document.getElementById('menuToggle').addEventListener('click', () => toggleSidebar());

    // Sidebar overlay
    let overlay = document.querySelector('.sidebar-overlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.className = 'sidebar-overlay';
      document.getElementById('appPage').appendChild(overlay);
      overlay.addEventListener('click', () => toggleSidebar(false));
    }

    // Admin-Permissions tab clicks
    document.querySelectorAll('.ap-tab').forEach(tab => {
      tab.addEventListener('click', function () {
        switchAdminTab(this.dataset.tab);
      });
    });

    // Marketing date shortcuts
    document.querySelectorAll('.dh-ds-btn').forEach(btn => {
      btn.addEventListener('click', function () {
        setDateRange(parseInt(this.dataset.range));
      });
    });
    // Marketing filter selects - load options & bind changes
    const platSel = document.getElementById('mktPlatform');
    const brandSel = document.getElementById('mktBrand');
    if (platSel) platSel.addEventListener('change', function () { renderMarketingOverview(); });
    if (brandSel) brandSel.addEventListener('change', function () { renderMarketingOverview(); });
    // 加载平台/品牌选项
    if (state.apiAvailable) {
      ApiService.getMarketingFilters().then(filters => {
        if (filters) {
          if (platSel) {
            platSel.innerHTML = '<option value="">全部平台</option>';
            filters.platforms.forEach(p => { const o = document.createElement('option'); o.value = p; o.textContent = p; platSel.appendChild(o); });
          }
          if (brandSel) {
            brandSel.innerHTML = '<option value="">全部品牌</option>';
            filters.brands.forEach(b => { const o = document.createElement('option'); o.value = b; o.textContent = b; brandSel.appendChild(o); });
          }
        }
      });
    }

    // Platform-store date shortcuts
    document.querySelectorAll('.ps-ds-btn').forEach(function(btn) {
      btn.addEventListener('click', function () {
        _psSetDateRange(parseInt(this.dataset.range));
      });
    });
    // Platform-store platform filter
    var psPlat = document.getElementById('psPlatform');
    if (psPlat) {
      psPlat.addEventListener('change', function () { _psPage = 1; _psExpandedStore = null; document.getElementById('psDetailPanel').classList.add('hidden'); renderPlatformStore(); });
      // Load platform options
      if (state.apiAvailable) {
        ApiService.getMarketingFilters().then(function(filters) {
          if (filters && psPlat) {
            psPlat.innerHTML = '<option value="">全部平台</option>';
            filters.platforms.forEach(function(p) { var o = document.createElement('option'); o.value = p; o.textContent = p; psPlat.appendChild(o); });
          }
        });
      }
    }
    // Platform-store compare metric change
    var psMetric = document.getElementById('psCompareMetric');
    if (psMetric) psMetric.addEventListener('change', function () { if (_psData) _psRenderCompareChart(_psData); });
    // Platform-store search
    var psSearch = document.getElementById('psStoreSearch');
    if (psSearch) psSearch.addEventListener('input', function () { _psSearch = this.value; _psPage = 1; if (_psData) _psRenderTable(_psData); });
    // Platform-store sort
    var psSort = document.getElementById('psSortBy');
    if (psSort) psSort.addEventListener('change', function () { _psSortBy = this.value; _psPage = 1; if (_psData) _psRenderTable(_psData); });

    // Daily analysis date selector
    var daDateSel = document.getElementById('daDateSelect');
    if (daDateSel) {
      daDateSel.addEventListener('change', async function() {
        var dt = this.value;
        // 更新按钮文字
        var btn = document.getElementById('daGenerateBtn');
        if (btn) {
          btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i> 生成' + (dt || '昨日') + '数据分析文档';
        }
        if (!dt) { renderDailyAnalysis(); return; }
        if (state.apiAvailable) {
          var report = await ApiService.getAnalysisReport(dt);
          if (report) { showReport(report); }
          else { showError('未找到 ' + dt + ' 的分析报告，点击生成按钮即可创建'); }
        }
      });
    }

    // ---- 初始化权限 ----
    _initPermissions();

    // Hash routing（权限受限用户跳转到首个允许的页面）
    const validPages = [
      'marketing-overview', 'platform-store',
      'store-account', 'operation-performance', 'product-selection', 'finance', 'hr',
      'admin-permissions', 'profile', 'daily-analysis', 'toolbox-violation-check', 'order-details', 'category-marketing',
      'seeding-monitor',
      'data-import',
    ];
    const hash = window.location.hash.replace('#', '');
    if (validPages.includes(hash)) {
      navigateTo(hash);
    } else {
      var defaultPage = (_ALLOWED_PAGES !== null && _ALLOWED_PAGES.length > 0)
        ? _ALLOWED_PAGES[0]
        : 'marketing-overview';
      // 人事数据表权限（hr-*）统一落到「人事数据中心」页面，由页内卡片选择器再细分
      if (String(defaultPage).indexOf('hr-') === 0) defaultPage = 'hr';
      navigateTo(defaultPage);
    }

    window.addEventListener('hashchange', () => {
      const h = window.location.hash.replace('#', '');
      if (validPages.includes(h)) navigateTo(h);
    });

    // 种草监测中台「数据更新」悬浮面板：点击页面其它区域关闭
    document.addEventListener('click', function(e) {
      var panel = document.getElementById('sdUpdatePanel');
      if (!panel || panel.classList.contains('hidden')) return;
      var anchor = document.querySelector('#page-seeding-monitor .sd-header-actions');
      if (panel.contains(e.target) || (anchor && anchor.contains(e.target))) return;
      panel.classList.add('hidden');
    });

    // Violation word search（仅当旧版元素存在时绑定）
    var vwSearch = document.getElementById('violationWordSearch');
    if (vwSearch) {
      vwSearch.addEventListener('input', function() { _renderViolationWordTable(); });
    }

    // Order Details date shortcuts
    document.querySelectorAll('.od-ds-btn').forEach(function(btn) {
      btn.addEventListener('click', function() { _odSetDateRange(parseInt(this.dataset.range)); });
    });
    // Order Details filters
    var odPlat = document.getElementById('odPlatform');
    var odStore = document.getElementById('odStore');
    var odSearch = document.getElementById('odSearch');
    var odPageSize = document.getElementById('odPageSize');
    var odDateStart = document.getElementById('odDateStart');
    var odDateEnd = document.getElementById('odDateEnd');
    if (odPlat) {
      odPlat.addEventListener('change', function() {
        _odPage = 1;
        _odCardFields = [];
        _odTableCols = [];
        _odSortBy = '';
        _odSortDir = 'desc';
        _odLinkType = 'all';
        // 切换平台时先清空店铺选择，避免用旧平台的店铺值过滤新平台数据
        if (odStore) { odStore.innerHTML = '<option value="">全部店铺</option>'; odStore.value = ''; }
        _odFetchData();
        // 联动加载店铺列表
        var plat = this.value;
        if (plat) {
          fetch('/api/order-details/stores?platform=' + encodeURIComponent(plat) + '&linkType=all')
            .then(function(r) { return r.json(); })
            .then(function(j) {
              if (j.code === 0 && j.data && odStore) {
                odStore.innerHTML = '<option value="">全部店铺</option>' + j.data.map(function(s) { return '<option value="' + s + '">' + s + '</option>'; }).join('');
              }
            });
        }
      });
      // 初始加载平台列表
      fetch('/api/order-details/stores').then(function(r) { return r.json(); }).then(function(j) {
        if (j.code === 0 && j.data && odPlat) {
          var platforms = ['抖店','京东','千牛'];
          platforms.forEach(function(p) { var o = document.createElement('option'); o.value = p; o.textContent = p; odPlat.appendChild(o); });
        }
      });
    }
    if (odStore) odStore.addEventListener('change', function() { _odPage = 1; _odFetchData(); });
    if (odSearch) { odSearch.addEventListener('input', function() { _odPage = 1; _odFetchData(); }); }
    if (odPageSize) odPageSize.addEventListener('change', function() { _odPageSize = parseInt(this.value); _odPage = 1; _odFetchData(); });
    if (odDateStart) odDateStart.addEventListener('change', function() { _odUpdateDateLabel(); _odPage = 1; _odFetchData(); });
    if (odDateEnd) odDateEnd.addEventListener('change', function() { _odUpdateDateLabel(); _odPage = 1; _odFetchData(); });
    // 点击别处关闭日期/字段悬浮框
    document.addEventListener('click', function(e) {
      [['odDatePanel', 'odDateBtn'], ['mktDatePanel', 'mktDateBtn'], ['psDatePanel', 'psDateBtn'], ['sdDatePanel', 'sdDateBtn']].forEach(function(pair) {
        var panel = document.getElementById(pair[0]);
        var btn = document.getElementById(pair[1]);
        if (panel && btn && !panel.contains(e.target) && !btn.contains(e.target)) panel.style.display = 'none';
      });
      var colPanel = document.getElementById('odColPanel');
      var colBtn = document.getElementById('odColBtn');
      if (colPanel && colBtn && !colPanel.contains(e.target) && !colBtn.contains(e.target)) colPanel.style.display = 'none';
    });
    // Order Details 卡片字段下拉（4 个，绑定 change）
    for (var ci = 0; ci < 4; ci++) {
      (function(idx) {
        var sel = document.getElementById('odCardSel' + idx);
        if (sel) sel.addEventListener('change', function() { _odCardFields[idx] = this.value; _odFetchData(); });
      })(ci);
    }

    // Time
    updateTime();
    setInterval(updateTime, 30000);
  }

  function init() {
    // Login form
    document.getElementById('loginForm').addEventListener('submit', handleLogin);
    document.getElementById('logoutBtn').addEventListener('click', handleLogout);

    // 「记住密码」已下线：一次性清空历史遗留的明文账号密码，输入框始终保持空白
    try {
      localStorage.removeItem('admin_user');
      localStorage.removeItem('admin_pass');
    } catch (e) {}
    var __nafU = document.getElementById('username');
    var __nafP = document.getElementById('password');
    if (__nafU) __nafU.value = '';
    if (__nafP) __nafP.value = '';

    // 刷新后自动恢复登录状态
    if (sessionStorage.getItem('admin_logged_in') === 'true') {
      const user = sessionStorage.getItem('admin_current_user') || '管理员';
      state.currentUser = user;
      state.currentRole = sessionStorage.getItem('admin_current_role') || '';
      state.currentAccount = sessionStorage.getItem('admin_current_account') || '';
      try { state.currentPermissions = JSON.parse(sessionStorage.getItem('admin_permissions') || 'null') || []; }
      catch (e) { state.currentPermissions = []; }
      document.getElementById('loginPage').classList.add('hidden');
      document.getElementById('appPage').classList.remove('hidden');
      document.getElementById('currentUser').textContent = user;
      initApp();
      return;
    }
  }

  // ==================== 个人中心设置 ====================
  function renderProfile() {
    document.getElementById('pfDisplayName').textContent = state.currentUser || '管理员';
    document.getElementById('pfAccount').textContent = state.currentAccount || 'admin';
    document.getElementById('pfRole').textContent = state.currentRole || '超级管理员';
    document.getElementById('pfRoleBadge').textContent = state.currentRole || '超级管理员';
    document.getElementById('pfLastLogin').textContent = new Date().toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
    document.getElementById('pfRegDate').textContent = '2026-06-01';
    document.getElementById('pfName').value = state.currentUser || '张经理';
    // Avatar initial
    var avatarEl = document.getElementById('pfAvatar');
    if (avatarEl && state.currentUser) avatarEl.textContent = state.currentUser.charAt(0);

    // Profile tabs
    document.querySelectorAll('#profileTabs .ap-tab').forEach(function(tab){
      tab.onclick = function(){
        document.querySelectorAll('#profileTabs .ap-tab').forEach(function(t){t.classList.remove('active');});
        this.classList.add('active');
        var tabName = this.dataset.tab;
        document.querySelectorAll('[id^="pf-panel-"]').forEach(function(p){p.classList.remove('active');});
        var panel = document.getElementById('pf-panel-'+tabName);
        if (panel) { panel.classList.add('active');
          if (tabName === 'profile-log') _pfRenderLog(); }
      };
    });
    _pfRenderLog();
  }

  function saveProfile() {
    var name = document.getElementById('pfName').value.trim();
    if (!name) { showToast('请输入姓名','error'); return; }
    state.currentUser = name;
    document.getElementById('pfDisplayName').textContent = name;
    document.getElementById('pfAvatar').textContent = name.charAt(0);
    document.getElementById('currentUser').textContent = name;
    sessionStorage.setItem('admin_current_user', name);
    showToast('个人信息已保存','success');
  }

  function resetProfile() {
    document.getElementById('pfName').value = state.currentUser || '张经理';
    document.getElementById('pfPhone').value = '13800138001';
    document.getElementById('pfEmail').value = 'zhangjl@julang.com';
    document.getElementById('pfWechat').value = 'zhang_manager';
    showToast('已重置为原始数据','success');
  }

  function changePassword() {
    var cur = document.getElementById('pfCurPwd').value;
    var n1 = document.getElementById('pfNewPwd').value;
    var n2 = document.getElementById('pfNewPwd2').value;
    if (!cur) { showToast('请输入当前密码','error'); return; }
    if (!n1 || n1.length < 6) { showToast('新密码至少6位','error'); return; }
    if (n1 !== n2) { showToast('两次密码不一致','error'); return; }
    showToast('密码修改成功','success');
    document.getElementById('pfCurPwd').value = '';
    document.getElementById('pfNewPwd').value = '';
    document.getElementById('pfNewPwd2').value = '';
  }

  function _pfRenderLog() {
    var tbody = document.getElementById('pfLogTbody');
    if (!tbody) return;
    var logs = [
      {time:'2026-08-04 09:15:32',type:'登录',detail:'管理员登录后台系统',ip:'192.168.1.100',device:'Chrome / Windows'},
      {time:'2026-08-03 17:40:12',type:'修改',detail:'修改管理员「李运营」的角色为运营主管',ip:'192.168.1.100',device:'Chrome / Windows'},
      {time:'2026-08-03 14:22:08',type:'新增',detail:'新增管理员账号「wangcaiwu」',ip:'192.168.1.100',device:'Chrome / Windows'},
      {time:'2026-08-03 11:05:45',type:'导出',detail:'导出营销数据报表（2026-07-01 ~ 2026-07-31）',ip:'192.168.1.100',device:'Chrome / Windows'},
      {time:'2026-08-02 16:30:18',type:'设置',detail:'修改系统通知设置',ip:'192.168.1.100',device:'Chrome / Windows'},
      {time:'2026-08-02 10:12:55',type:'登录',detail:'管理员登录后台系统',ip:'192.168.1.100',device:'Chrome / Windows'},
      {time:'2026-08-01 15:48:33',type:'操作',detail:'生成每日数据分析报告（2026-07-31）',ip:'192.168.1.100',device:'Chrome / Windows'},
      {time:'2026-08-01 09:00:01',type:'登录',detail:'管理员登录后台系统',ip:'192.168.1.100',device:'Chrome / Windows'},
      {time:'2026-07-31 18:20:10',type:'修改',detail:'修改了个人基本资料',ip:'192.168.1.105',device:'Safari / macOS'},
      {time:'2026-07-31 14:55:40',type:'删除',detail:'删除商品「旧版充电线」',ip:'192.168.1.100',device:'Chrome / Windows'},
    ];
    tbody.innerHTML = logs.map(function(l){
      var typeCls = l.type==='登录'?'badge-info':(l.type==='修改'||l.type==='新增'?'badge-success':(l.type==='删除'?'badge-danger':'badge-gray'));
      return '<tr><td>'+l.time+'</td><td><span class="badge '+typeCls+'">'+l.type+'</span></td><td>'+l.detail+'</td><td style="font-family:monospace;font-size:12px">'+l.ip+'</td><td>'+l.device+'</td></tr>';
    }).join('');
  }

  // ==================== 工具箱 - 违规词检测（图片OCR） ====================
  var _vdImages = [];        // {id, file, name, dataUrl, ocrText, violations}
  var _vdNextImgId = 1;
  var _vdOcrRunning = false;

  // OCR 上传压缩参数：长边超过上限则等比缩小，减小请求体
  var _VD_OCR_MAX_SIDE = 1600;
  var _VD_OCR_JPEG_QUALITY = 0.9;

  // 压缩图片用于 OCR 上传，返回 dataUrl；失败时回退原图。
  // 仅缩小尺寸 + 适度重编码，不做去色（后端 PaddleOCR 会自行预处理）。
  function _vdCompressForOcr(dataUrl) {
    return new Promise(function(resolve) {
      var img = new Image();
      img.onload = function() {
        var w = img.width, h = img.height;
        var maxSide = Math.max(w, h);
        var changed = false;
        if (maxSide > _VD_OCR_MAX_SIDE) {
          var scale = _VD_OCR_MAX_SIDE / maxSide;
          w = Math.max(1, Math.round(w * scale));
          h = Math.max(1, Math.round(h * scale));
          changed = true;
        }
        // 原图已经够小，直接复用，避免无谓重编码（保留 PNG 截图清晰度）
        if (!changed && dataUrl.length < 400 * 1024) {
          resolve(dataUrl);
          return;
        }
        var canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        var ctx = canvas.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL('image/jpeg', _VD_OCR_JPEG_QUALITY));
      };
      img.onerror = function() { resolve(dataUrl); };
      img.src = dataUrl;
    });
  }

  var _violationWords = [
    { id: 1, word: '违禁品', level: '严重', time: '2026-08-01 10:30:00' },
    { id: 2, word: '假货', level: '严重', time: '2026-08-01 10:32:00' },
    { id: 3, word: '虚假宣传', level: '严重', time: '2026-08-02 14:15:00' },
    { id: 4, word: '绝对化用语', level: '一般', time: '2026-08-03 09:20:00' },
    { id: 5, word: '侵权', level: '严重', time: '2026-08-04 11:00:00' },
    { id: 6, word: '低俗', level: '一般', time: '2026-08-05 16:45:00' },
  ];
  var _violationNextId = 7;

  // ========== 页面渲染入口 ==========
  function renderToolboxViolationCheck() {
    // 检测进行中不重置，保留已有数据
    if (_vdOcrRunning) {
      // 检测还在跑：显示扫描动画 + 已有缩略图
      setTimeout(function() {
        _vdRenderThumbs();
        var scanning = document.getElementById('vdScanning');
        var resultEmpty = document.getElementById('vdResultEmpty');
        var resultPass = document.getElementById('vdPassAnim');
        var resultList = document.getElementById('vdResultList');
        if (scanning) scanning.style.display = 'flex';
        if (resultEmpty) resultEmpty.style.display = 'none';
        if (resultPass) resultPass.style.display = 'none';
        if (resultList) resultList.innerHTML = '';
      }, 100);
    } else if (_vdImages.some(function(img) { return img.ocrText !== null; })) {
      // 检测已完成：恢复缩略图和结果
      setTimeout(function() { _vdRenderThumbs(); _vdUpdateResults(); }, 100);
    } else {
      _vdResetState();
    }
    // 兼容旧版违规词库表格（如果存在）
    var vwTbody = document.getElementById('violationWordTbody');
    if (vwTbody) _renderViolationWordTable();
  }

  function _vdResetState() {
    _vdImages = [];
    _vdNextImgId = 1;
    _vdOcrRunning = false;
    _vdHideProgress();
    // 缩略图区
    document.getElementById('vdThumbList').innerHTML = '';
    var emptyEl = document.getElementById('vdThumbEmpty');
    if (emptyEl) emptyEl.style.display = '';
    // 结果区
    var resultEmpty = document.getElementById('vdResultEmpty');
    var resultList = document.getElementById('vdResultList');
    var passAnim = document.getElementById('vdPassAnim');
    var scanning = document.getElementById('vdScanning');
    if (resultEmpty) resultEmpty.style.display = '';
    if (resultList) resultList.innerHTML = '';
    if (passAnim) passAnim.style.display = 'none';
    if (scanning) scanning.style.display = 'none';
    // 摘要 & 按钮
    var summary = document.getElementById('vdResultSummary');
    if (summary) { summary.textContent = '等待检测'; summary.style.color = '#94a3b8'; }
    var startBtn = document.getElementById('vdStartBtn');
    if (startBtn) startBtn.disabled = false;
    // 重置统计卡片
    var statTotal = document.getElementById('vdStatTotal');
    var statProcessing = document.getElementById('vdStatProcessing');
    var statHits = document.getElementById('vdStatHits');
    var statPassRate = document.getElementById('vdStatPassRate');
    if (statTotal) statTotal.textContent = '0';
    if (statProcessing) statProcessing.textContent = '0';
    if (statHits) statHits.textContent = '0';
    if (statPassRate) statPassRate.textContent = '--';
  }

  // ========== 图片选择 ==========
  function vdHandleFiles(files) {
    if (!files || files.length === 0) return;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      if (!f.type.match(/image\/(jpeg|png|webp)/)) continue;
      // 避免重复
      if (_vdImages.some(function(img) { return img.name === f.name && img.file.size === f.size; })) continue;
      var reader = new FileReader();
      reader.onload = (function(file, name) {
        return function(e) {
          var originalDataUrl = e.target.result;
          var imgObj = {
            id: _vdNextImgId++,
            file: file,
            name: name,
            dataUrl: originalDataUrl,          // 原图，仅用于缩略图显示
            ocrDataUrl: originalDataUrl,        // OCR 上传用图，压缩完成后替换
            ocrText: null,
            violations: null
          };
          _vdImages.push(imgObj);
          _vdRenderThumbs();
          // 异步压缩上传用图，减小请求体（不影响缩略图显示）
          _vdCompressForOcr(originalDataUrl).then(function(compressed) {
            if (compressed) imgObj.ocrDataUrl = compressed;
          });
        };
      })(f, f.name);
      reader.readAsDataURL(f);
    }
    // 重置 file input 允许重复选同一个文件
    document.getElementById('vdFileInput').value = '';
  }

  function vdHandleDrop(e) {
    e.preventDefault();
    e.target.style.borderColor = '#e2e8f0';
    e.target.style.background = '#fafbfc';
    var items = e.dataTransfer.items;
    var files = [];
    // 递归遍历文件夹
    function scanEntries(entries, cb) {
      var pending = entries.length;
      if (pending === 0) cb([]);
      var results = [];
      for (var i = 0; i < entries.length; i++) {
        (function(entry) {
          if (entry.isFile) {
            entry.file(function(f) { results.push(f); pending--; if (pending === 0) cb(results); });
          } else if (entry.isDirectory) {
            var reader = entry.createReader();
            reader.readEntries(function(entries) { scanEntries(entries, function(sub) { results = results.concat(sub); pending--; if (pending === 0) cb(results); }); });
          } else { pending--; if (pending === 0) cb(results); }
        })(entries[i]);
      }
    }
    if (items) {
      var entries = [];
      for (var i = 0; i < items.length; i++) {
        var entry = items[i].webkitGetAsEntry ? items[i].webkitGetAsEntry() : null;
        if (entry) entries.push(entry);
      }
      if (entries.length > 0) {
        scanEntries(entries, function(fs) { vdHandleFiles(fs); });
        return;
      }
    }
    // 回退：直接处理文件
    vdHandleFiles(e.dataTransfer.files);
  }

  function _vdRenderThumbs() {
    var container = document.getElementById('vdThumbList');
    // 不在违规词检测页面时跳过 DOM 操作
    if (!container) return;
    var emptyEl = document.getElementById('vdThumbEmpty');

    // 更新统计卡片
    var statTotal = document.getElementById('vdStatTotal');
    if (statTotal) statTotal.textContent = _vdImages.length;

    // 切换空状态 / 图片列表
    if (_vdImages.length === 0) {
      container.innerHTML = '';
      if (emptyEl) emptyEl.style.display = '';
    } else {
      if (emptyEl) emptyEl.style.display = 'none';
      var html = '';
      for (var i = 0; i < _vdImages.length; i++) {
        var img = _vdImages[i];
        var statusIcon = '';
        var statusStyle = '';
        if (img.violations && img.violations.length > 0) {
          statusIcon = '<i class="fa-solid fa-circle-exclamation"></i> ';
          statusStyle = 'border-color:#ef4444';
        } else if (img.ocrText !== null) {
          statusIcon = '<i class="fa-solid fa-circle-check"></i> ';
          statusStyle = 'border-color:#16a34a';
        }
        html += '<div style="position:relative;width:64px;height:64px;border-radius:8px;overflow:hidden;border:2px solid ' + (statusStyle ? statusStyle.replace('border-color:','') : '#e2e8f0') + ';flex-shrink:0;cursor:pointer" onclick="App.vdRemoveImage(' + img.id + ')" title="' + img.name + ' (点击移除)">' +
          '<img src="' + img.dataUrl + '" style="width:100%;height:100%;object-fit:cover">' +
          '<span style="position:absolute;top:2px;right:2px;font-size:10px;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,0.6)">' + statusIcon + '</span>' +
          '<span style="position:absolute;bottom:0;left:0;right:0;font-size:9px;color:#fff;background:rgba(0,0,0,0.5);padding:1px 4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + img.name + '</span>' +
          '</div>';
      }
      container.innerHTML = html;
    }
  }

  function vdRemoveImage(id) {
    _vdImages = _vdImages.filter(function(img) { return img.id !== id; });
    _vdRenderThumbs();
    _vdUpdateResults();
  }

  // ========== OCR 检测核心 ==========
  // 图片预处理：仅放大 + 去色，保留原始细节，让 Tesseract 内部二值化处理
  function _vdPreprocessImage(dataUrl) {
    return new Promise(function(resolve) {
      var img = new Image();
      img.onload = function() {
        var w = img.width, h = img.height;
        // 缩放：确保短边至少 800px，给 Tesseract 足够像素
        var minSide = Math.min(w, h);
        if (minSide < 800) {
          var scale = 800 / minSide;
          w = Math.round(w * scale);
          h = Math.round(h * scale);
        }

        var canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        var ctx = canvas.getContext('2d');
        // 使用高质量缩放
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(img, 0, 0, w, h);

        // 仅做去色处理：保留亮度，移除色彩（Tesseract 内部会自己二值化）
        var imageData = ctx.getImageData(0, 0, w, h);
        var data = imageData.data;
        for (var i = 0; i < w * h; i++) {
          var r = data[i * 4], g = data[i * 4 + 1], b = data[i * 4 + 2];
          // 加权灰度：人眼感知权重
          var gray = Math.round(0.299 * r + 0.587 * g + 0.114 * b);
          data[i * 4] = gray;
          data[i * 4 + 1] = gray;
          data[i * 4 + 2] = gray;
        }
        ctx.putImageData(imageData, 0, 0);
        resolve(canvas.toDataURL('image/png'));
      };
      img.onerror = function() { resolve(dataUrl); };
      img.src = dataUrl;
    });
  }

  // OCR 结果清洗：清理明显的乱码，保留正常文本
  function _vdCleanOcrText(text) {
    if (!text) return '';
    // 1. 移除 Unicode 替换字符和各种控制字符
    var cleaned = text.replace(/[� --​-‏- ﻿]/g, '');
    // 2. 将连续空白统一为单个空格/换行
    cleaned = cleaned.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n');
    // 3. 按行处理
    var lines = cleaned.split('\n');
    var result = [];
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      if (!line) { result.push(''); continue; }
      // 移除行内乱码：保留中文、英文、数字、常见标点
      // 中文范围: 一-鿿 (基本) + 㐀-䶿 (扩展A)
      line = line.replace(/[^一-鿿㐀-䶿a-zA-Z0-9\s.,;:!?()\[\]{}"'\-+/=<>@#$%&*_|~\\^`'"，。；：？！…—·《》【】「」『』、（）％＃＠＆￥]/g, '');
      line = line.trim();
      if (!line) { result.push(''); continue; }
      // 跳过纯符号/数字行（大概率噪声）
      var hasCJK = /[一-鿿㐀-䶿]/.test(line);
      var hasWord = /[a-zA-Z0-9]{2,}/.test(line);
      if (!hasCJK && !hasWord && line.length < 6) { result.push(''); continue; }
      result.push(line);
    }
    // 4. 收尾：去掉首尾空行，合并连续空行
    var finalText = result.join('\n').replace(/^\n+/, '').replace(/\n+$/, '').replace(/\n{3,}/g, '\n\n');
    return finalText;
  }

  // 每批发送的图片数：单批请求体过大会被后端拒收/超时，
  // 分批（少量多批）与小批量手动上传的行为一致，避免 28 张一次性失败。
  var _VD_BATCH_SIZE = 5;

  // 对一批图片调用后端 OCR + 违规词检测，结果写回图片对象
  async function _vdProcessBatch(batch) {
    var imageDataUrls = batch.map(function(img) { return img.ocrDataUrl || img.dataUrl; });
    var resp = await fetch('/api/ocr/detect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ images: imageDataUrls }),
    });
    var json = await resp.json();
    var data = (json && json.results) ? json.results : [];

    for (var j = 0; j < batch.length; j++) {
      var img = batch[j];
      var result = data[j];
      img.ocrText = (result && result.text) ? result.text : '';
      img.ocrLines = (result && result.lines) ? result.lines : 0;
      img.ocrConfidence = (result && result.confidence) ? result.confidence : 0;
      img.ocrFallback = (result && result.fallback) ? result.fallback : false;
      img.ocrError = (result && result.error) ? result.error : null;
      // 后端整体报错（如请求体过大 413）时，把顶层错误兜底到每张图
      if (!img.ocrText && !img.ocrError && json && json.error) {
        img.ocrError = json.error;
      }

      // 调用后端违规词检测
      img.violations = [];
      img.suspectedWords = [];
      if (img.ocrText && img.ocrText.trim()) {
        try {
          var vResp = await fetch('/api/violation/detect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: img.ocrText }),
          });
          var vJson = await vResp.json();
          if (vJson && vJson.success) {
            img.violations = vJson.confirmed || [];
            img.suspectedWords = vJson.suspected || [];
          }
        } catch (ve) {
          console.warn('违规词检测失败:', ve);
        }
      }
    }
  }

  function _vdShowProgress(pct, text) {
    var wrap = document.getElementById('vdProgressWrap');
    var fill = document.getElementById('vdProgressFill');
    var txt = document.getElementById('vdProgressText');
    var pctEl = document.getElementById('vdProgressPercent');
    if (wrap) wrap.style.display = '';
    if (fill) fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
    if (txt && text) txt.textContent = text;
    if (pctEl) pctEl.textContent = Math.max(0, Math.min(100, pct)) + '%';
  }

  function _vdHideProgress() {
    var wrap = document.getElementById('vdProgressWrap');
    if (wrap) wrap.style.display = 'none';
  }

  async function vdStartOCR() {
    if (_vdOcrRunning) return;
    if (_vdImages.length === 0) {
      showToast('请先选择图片', 'warning');
      return;
    }

    _vdOcrRunning = true;
    var startBtn = document.getElementById('vdStartBtn');
    var scanningEl = document.getElementById('vdScanning');
    var resultEmpty = document.getElementById('vdResultEmpty');
    var passAnim = document.getElementById('vdPassAnim');

    if (startBtn) startBtn.disabled = true;
    if (scanningEl) scanningEl.style.display = 'flex';
    if (resultEmpty) resultEmpty.style.display = 'none';
    if (passAnim) passAnim.style.display = 'none';

    // 更新统计：检测中
    var statProcessing = document.getElementById('vdStatProcessing');
    if (statProcessing) statProcessing.textContent = _vdImages.length;

    var failedCount = 0;
    _vdShowProgress(0, '正在识别 0/' + _vdImages.length);

    // 分批处理，避免一次性发送超大请求体
    for (var start = 0; start < _vdImages.length; start += _VD_BATCH_SIZE) {
      var batch = _vdImages.slice(start, start + _VD_BATCH_SIZE);
      try {
        await _vdProcessBatch(batch);
      } catch (e) {
        console.error('OCR API error:', e);
        failedCount += batch.length;
        batch.forEach(function(img) {
          img.ocrText = '';
          img.ocrError = e.message;
          img.violations = [];
          img.suspectedWords = [];
        });
      }
      // 每批完成后刷新剩余待处理数与进度条
      var done = start + batch.length;
      var remaining = Math.max(0, _vdImages.length - done);
      if (statProcessing) statProcessing.textContent = remaining;
      _vdShowProgress(Math.round(done / _vdImages.length * 100), '正在识别 ' + done + '/' + _vdImages.length);
      if (state.currentPage === 'toolbox-violation-check') _vdRenderThumbs();
    }

    // 汇总命中数
    var totalImagesWithViolations = 0;
    _vdImages.forEach(function(img) {
      if ((img.violations && img.violations.length > 0) || (img.suspectedWords && img.suspectedWords.length > 0)) {
        totalImagesWithViolations++;
      }
    });

    _vdOcrRunning = false;
    _vdHideProgress();

    // 如果已切换到其他页面，只更新数据不操作 DOM
    if (state.currentPage !== 'toolbox-violation-check') return;

    if (startBtn) startBtn.disabled = false;
    if (scanningEl) scanningEl.style.display = 'none';
    _vdRenderThumbs();
    _vdUpdateResults();

    if (failedCount > 0) {
      showToast('检测完成：' + _vdImages.length + ' 张图片，' + failedCount + ' 张识别失败', 'error');
    } else {
      showToast('检测完成：' + _vdImages.length + ' 张图片，' + totalImagesWithViolations + ' 张含违规词', totalImagesWithViolations > 0 ? 'warning' : 'success');
    }
  }

  function _vdUpdateResults() {
    var resultList = document.getElementById('vdResultList');
    var resultEmpty = document.getElementById('vdResultEmpty');
    var resultPass = document.getElementById('vdPassAnim');
    var resultSummary = document.getElementById('vdResultSummary');

    if (!resultList) return;

    if (_vdImages.length === 0) {
      resultList.innerHTML = '';
      if (resultEmpty) resultEmpty.style.display = '';
      if (resultPass) resultPass.style.display = 'none';
      if (resultSummary) { resultSummary.textContent = '等待检测'; resultSummary.style.color = '#94a3b8'; }
      return;
    }

    var allProcessed = _vdImages.every(function(img) { return img.ocrText !== null; });
    if (!allProcessed) {
      resultList.innerHTML = '';
      if (resultEmpty) resultEmpty.style.display = '';
      if (resultPass) resultPass.style.display = 'none';
      if (resultSummary) { resultSummary.textContent = _vdImages.length + ' 张待检测'; resultSummary.style.color = '#94a3b8'; }
      return;
    }

    var totalConfirmed = 0;
    var totalSuspected = 0;
    var totalImagesWithViolations = 0;
    var html = '';

    for (var i = 0; i < _vdImages.length; i++) {
      var img = _vdImages[i];
      var hasViolations = (img.violations && img.violations.length > 0);
      var hasSuspected = (img.suspectedWords && img.suspectedWords.length > 0);

      if (hasViolations || hasSuspected) {
        totalConfirmed += img.violations ? img.violations.length : 0;
        totalSuspected += img.suspectedWords ? img.suspectedWords.length : 0;
        totalImagesWithViolations++;
      }

      // 高亮标记：精确违规（红色）+ 疑似违规（橙色）
      var highlightedText = img.ocrText
        ? img.ocrText
        : (img.ocrError
            ? '<span style="color:#dc2626;font-style:italic">[识别失败: ' + img.ocrError + ']</span>'
            : '<span style="color:#94a3b8;font-style:italic">[未检测到文字]</span>');
      var allMarks = [];
      if (img.violations) {
        img.violations.forEach(function(v) {
          if (v.position !== undefined && v.length) {
            allMarks.push({ type: 'confirmed', start: v.position, end: v.position + v.length, word: v.word });
          }
        });
      }
      if (img.suspectedWords) {
        img.suspectedWords.forEach(function(v) {
          if (v.position !== undefined && v.length) {
            allMarks.push({ type: 'suspected', start: v.position, end: v.position + v.length, word: v.word });
          }
        });
      }
      // 按位置从后往前替换，避免偏移
      allMarks.sort(function(a, b) { return b.start - a.start; });
      allMarks.forEach(function(mk) {
        var match = highlightedText.slice(mk.start, mk.end);
        var title = mk.type === 'confirmed' ? '精确违规: ' + mk.word : '疑似违规: ' + mk.word;
        if (mk.type === 'confirmed') {
          highlightedText = highlightedText.slice(0, mk.start) +
            '<mark style="background:#fee2e2;color:#dc2626;padding:1px 3px;border-radius:3px;border:1px solid #fecaca;font-weight:600;cursor:help" title="' + title + '">' + match + '</mark>' +
            highlightedText.slice(mk.end);
        } else {
          highlightedText = highlightedText.slice(0, mk.start) +
            '<mark style="background:#fff7ed;color:#ea580c;padding:1px 3px;border-radius:3px;border:1px solid #fed7aa;font-weight:600;cursor:help" title="' + title + '">' + match + '</mark>' +
            highlightedText.slice(mk.end);
        }
      });

      var hasError = !!(img.ocrError && !img.ocrText);
      var hasAnyIssue = hasViolations || hasSuspected;
      var headerColor = (hasAnyIssue || hasError) ? '#ef4444' : '#16a34a';
      var headerIcon = (hasAnyIssue || hasError) ? 'fa-circle-exclamation' : 'fa-circle-check';
      var statusParts = [];
      if (hasViolations) statusParts.push('<b style="color:#dc2626">' + img.violations.length + '</b> 个精确违规');
      if (hasSuspected) statusParts.push('<b style="color:#ea580c">' + img.suspectedWords.length + '</b> 个疑似');
      var statusText = hasAnyIssue ? '发现 ' + statusParts.join('，') : (hasError ? '识别失败' : '未发现违规词');

      html += '<div style="margin-bottom:16px;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden">' +
        '<div style="display:flex;align-items:center;gap:10px;padding:10px 14px;background:#f8fafc;border-bottom:1px solid #e2e8f0">' +
          '<img src="' + img.dataUrl + '" style="width:40px;height:40px;border-radius:6px;object-fit:cover">' +
          '<div style="flex:1;min-width:0">' +
            '<div style="font-size:13px;font-weight:600;color:#1e293b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + img.name + '">' + img.name + '</div>' +
            '<div style="font-size:12px;color:' + headerColor + '"><i class="fa-solid ' + headerIcon + '"></i> ' + statusText + '</div>' +
          '</div>' +
          '<div style="font-size:11px;color:#94a3b8">' + (img.ocrConfidence ? '置信度 ' + Math.round(img.ocrConfidence * 100) + '%' : '') + (img.ocrFallback ? ' · 降级识别' : '') + '</div>' +
        '</div>' +
        '<div style="padding:10px 14px;font-size:13px;line-height:1.8;max-height:200px;overflow-y:auto;word-break:break-all">' + highlightedText + '</div>' +
      '</div>';
    }

    // 更新统计卡片
    var statHits = document.getElementById('vdStatHits');
    var statPassRate = document.getElementById('vdStatPassRate');
    var statProcessing = document.getElementById('vdStatProcessing');
    if (statHits) statHits.textContent = totalImagesWithViolations;
    if (statProcessing) statProcessing.textContent = '0';
    if (statPassRate) {
      var passRate = _vdImages.length > 0 ? Math.round((_vdImages.length - totalImagesWithViolations) / _vdImages.length * 100) : 100;
      statPassRate.textContent = passRate + '%';
    }

    // 隐藏空状态，显示结果
    if (resultEmpty) resultEmpty.style.display = 'none';

    if (totalImagesWithViolations > 0) {
      if (resultPass) resultPass.style.display = 'none';
      resultList.innerHTML = html || '<div style="padding:20px;text-align:center;color:#94a3b8">无检测结果</div>';
      var parts = [];
      if (totalConfirmed > 0) parts.push(totalConfirmed + ' 处精确违规');
      if (totalSuspected > 0) parts.push(totalSuspected + ' 处疑似违规');
      if (resultSummary) { resultSummary.textContent = _vdImages.length + ' 张图片 · ' + totalImagesWithViolations + ' 张命中 · ' + parts.join(' + '); resultSummary.style.color = '#ef4444'; }
    } else {
      // 全部合规 — 显示合规横幅 + 提取的文字
      if (resultPass) resultPass.style.display = '';
      resultList.innerHTML = html || '<div style="padding:20px;text-align:center;color:#94a3b8">无检测结果</div>';
      if (resultSummary) { resultSummary.textContent = _vdImages.length + ' 张图片 · 全部合规'; resultSummary.style.color = '#16a34a'; }
    }
  }

  function vdClearAll() {
    _vdResetState();
  }

  // ========== 违规词库管理 ==========
  function _renderViolationWordTable() {
    var tbody = document.getElementById('violationWordTbody');
    var searchTerm = (document.getElementById('violationWordSearch') || {}).value || '';
    var filtered = _violationWords.filter(function(item) {
      return !searchTerm || item.word.indexOf(searchTerm) !== -1;
    });
    document.getElementById('violationWordCount').textContent = '共 ' + _violationWords.length + ' 条';
    if (filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#94a3b8;padding:32px">暂无违规词数据</td></tr>';
    } else {
      tbody.innerHTML = filtered.map(function(item, i) {
        var levelBadge = item.level === '严重' ? 'badge-danger' : 'badge-warning';
        return '<tr>' +
          '<td>' + (i + 1) + '</td>' +
          '<td><strong>' + item.word + '</strong></td>' +
          '<td><span class="badge ' + levelBadge + '">' + item.level + '</span></td>' +
          '<td>' + item.time + '</td>' +
          '<td><button class="btn btn-sm" style="color:#dc2626;background:none;border:none;cursor:pointer" onclick="App.vdDeleteWord(' + item.id + ')"><i class="fa-solid fa-trash"></i> 删除</button></td>' +
          '</tr>';
      }).join('');
    }
  }

  function vdOpenAddWordModal() {
    var word = prompt('请输入要添加的违规词：');
    if (!word || !word.trim()) return;
    var level = confirm('是否为严重级别？（确定=严重，取消=一般）') ? '严重' : '一般';
    _violationWords.push({
      id: _violationNextId++,
      word: word.trim(),
      level: level,
      time: new Date().toISOString().replace('T', ' ').substring(0, 19)
    });
    _renderViolationWordTable();
    showToast('违规词「' + word.trim() + '」已添加', 'success');
  }

  function vdDeleteWord(id) {
    var item = _violationWords.find(function(w) { return w.id === id; });
    if (!item) return;
    if (!confirm('确定要删除违规词「' + item.word + '」吗？')) return;
    _violationWords = _violationWords.filter(function(w) { return w.id !== id; });
    _renderViolationWordTable();
    showToast('违规词「' + item.word + '」已删除', 'success');
  }

  // ==================== 订单详情 - 单链接销售数据 ====================
  var _odPage = 1;
  var _odPageSize = 20;
  var _odSortBy = '';
  var _odSortDir = 'desc';
  var _odData = null;
  var _odFields = [];
  var _odCardFields = [];
  var _odTableCols = [];
  var _odLinkType = 'all';

  function _odFmtMoney(v) { return '¥' + Number(v).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
  function _odFmtInt(v) { return Math.round(v).toLocaleString('zh-CN'); }
  function _odFmtPct(v) { return Number(v).toFixed(2) + '%'; }
  function _odFmtByType(v, type) {
    if (v === null || v === undefined || v === '') return '—';
    if (type === 'money') return _odFmtMoney(v);
    if (type === 'pct') return _odFmtPct(v);
    if (type === 'int') return _odFmtInt(v);
    if (type === 'decimal') return Number(v).toFixed(2);
    return v;
  }
  function _odPlatformBadge(p) {
    if (p === '抖店') return 'badge-warning';
    if (p === '京东') return 'badge-danger';
    if (p === '千牛') return 'badge-info';
    return 'badge-gray';
  }

  async function renderOrderDetails() {
    var yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    var dateStart = document.getElementById('odDateStart');
    var dateEnd = document.getElementById('odDateEnd');
    if (dateStart) { dateStart.max = yesterday; if (!dateStart.value) dateStart.value = yesterday; }
    if (dateEnd) { dateEnd.max = yesterday; if (!dateEnd.value) dateEnd.value = yesterday; }
    _odUpdateDateLabel();
    await _odFetchData();
  }

  async function _odFetchData() {
    var params = [];
    params.push('page=' + _odPage);
    params.push('pageSize=' + _odPageSize);
    params.push('sortBy=' + _odSortBy);
    params.push('sortDir=' + _odSortDir);
    var start = (document.getElementById('odDateStart') || {}).value || '';
    var end = (document.getElementById('odDateEnd') || {}).value || '';
    var plat = (document.getElementById('odPlatform') || {}).value || '';
    var store = (document.getElementById('odStore') || {}).value || '';
    var search = (document.getElementById('odSearch') || {}).value || '';
    if (start) params.push('start=' + encodeURIComponent(start));
    if (end) params.push('end=' + encodeURIComponent(end));
    if (plat) params.push('platform=' + encodeURIComponent(plat));
    if (store) params.push('store=' + encodeURIComponent(store));
    if (search) params.push('search=' + encodeURIComponent(search));
    params.push('linkType=' + encodeURIComponent(_odLinkType));
    params.push('cardFields=' + encodeURIComponent(_odCardFields.join(',')));

    try {
      var resp = await fetch('/api/order-details/data?' + params.join('&'));
      var json = await resp.json();
      if (json.code === 0 && json.data) {
        _odData = json.data;
        if (json.data.fields) {
          _odFields = json.data.fields;
          var _prevCardFields = _odCardFields.join(',');
          _odApplyFieldDefaults();
          _odPopulateCardSelects();
          _odRenderColPanel();
          // 首次进入/切换平台时 cardFields 为空，后端不会返回卡片汇总，
          // 需要按默认字段重新拉取一次，否则上方数字卡片会一直显示“—”
          if (_odCardFields.join(',') !== _prevCardFields) { _odFetchData(); return; }
        }
        _odRenderCards(json.data);
        _odRenderTable(json.data);
        _odRenderPagination(json.data);
        _odUpdateDateShortcuts(start, end);
      }
    } catch (e) {
      console.error('订单详情加载失败:', e);
    }
  }

  function _odAvailableFields() {
    // 后端已按当前平台返回对应字段目录，直接使用即可
    return _odFields;
  }

  function _odDefaultCardFields(fields) {
    return fields.filter(function(f) { return f.type === 'money' || f.type === 'int'; })
      .slice(0, 4).map(function(f) { return f.key; });
  }

  function _odDefaultTableCols(fields) {
    var date = fields.filter(function(f) { return f.type === 'date'; });
    var text = fields.filter(function(f) { return f.type === 'text'; });
    var num = fields.filter(function(f) { return ['money', 'int', 'pct', 'decimal'].indexOf(f.type) >= 0; });
    return date.concat(text.slice(0, 3)).concat(num.slice(0, 6)).slice(0, 10).map(function(f) { return f.key; });
  }

  function _odApplyFieldDefaults() {
    var keys = _odFields.map(function(f) { return f.key; });
    var cardValid = _odCardFields.length === 4 && _odCardFields.every(function(k) { return keys.indexOf(k) >= 0; });
    if (!cardValid) _odCardFields = _odDefaultCardFields(_odFields);
    var colValid = _odTableCols.length > 0 && _odTableCols.every(function(k) { return keys.indexOf(k) >= 0; });
    if (!colValid) _odTableCols = _odDefaultTableCols(_odFields);
    if (_odSortBy && keys.indexOf(_odSortBy) < 0) _odSortBy = '';
  }

  function _odPopulateCardSelects() {
    var avail = _odAvailableFields();
    for (var i = 0; i < 4; i++) {
      var sel = document.getElementById('odCardSel' + i);
      if (!sel) continue;
      var cur = _odCardFields[i];
      var opts = '';
      avail.forEach(function(f) {
        opts += '<option value="' + f.key + '"' + (f.key === cur ? ' selected' : '') + '>' + f.label + '</option>';
      });
      sel.innerHTML = opts;
      if (sel.value !== cur) { _odCardFields[i] = sel.value || (avail[0] ? avail[0].key : ''); }
    }
  }

  function _odRenderColPanel() {
    var panel = document.getElementById('odColPanel');
    if (!panel) return;
    var avail = _odAvailableFields();
    var html = '';
    avail.forEach(function(f) {
      var checked = _odTableCols.indexOf(f.key) >= 0;
      html += '<label style="display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:6px;cursor:pointer;font-size:13px;color:#334155">' +
        '<input type="checkbox" data-key="' + f.key + '"' + (checked ? ' checked' : '') + ' onchange="App.toggleOdCol(this)">' +
        '<span>' + f.label + '</span>' +
      '</label>';
    });
    panel.innerHTML = html || '<div style="padding:10px;color:#94a3b8;font-size:12px">无可选字段</div>';
  }

  function _odRenderCards(data) {
    var summaries = (data && data.cardSummaries) || [];
    var sumMap = {};
    summaries.forEach(function(s) { sumMap[s.key] = s; });
    for (var i = 0; i < 4; i++) {
      var val = document.getElementById('odCardValue' + i);
      var sub = document.getElementById('odCardSub' + i);
      if (!val) continue;
      var key = _odCardFields[i];
      var s = sumMap[key];
      if (s) {
        val.textContent = _odFmtByType(s.value, s.type);
        var aggText = s.agg === 'sum' ? '总和' : s.agg === 'avg' ? '均值' : '去重数';
        if (sub) sub.textContent = s.label + ' · ' + aggText;
      } else {
        val.textContent = '—';
        if (sub) sub.textContent = '';
      }
    }
    document.getElementById('odTotalCount').textContent = _odFmtInt(_odData ? _odData.total : 0);
    document.getElementById('odPageInfo').textContent = '共 ' + _odFmtInt(_odData ? _odData.total : 0) + ' 条记录';
  }

  function _odRenderTable(data) {
    var tbody = document.getElementById('odTableBody');
    var head = document.getElementById('odTableHead');
    if (!tbody || !data || !data.rows) return;
    var cols = _odTableCols.filter(function(k) { return _odFields.some(function(f) { return f.key === k; }); });
    if (head) {
      var hh = '<th style="padding:10px 8px;text-align:left;border-bottom:2px solid #e2e8f0;white-space:nowrap;color:#475569;font-weight:600">平台</th>';
      cols.forEach(function(k) {
        var f = _odFields.find(function(x) { return x.key === k; });
        if (!f) return;
        var align = (f.type === 'text' || f.type === 'date') ? 'left' : 'right';
        hh += '<th data-sort="' + f.key + '" style="padding:10px 8px;text-align:' + align + ';border-bottom:2px solid #e2e8f0;white-space:nowrap;color:#475569;font-weight:600;cursor:pointer">' + f.label + ' <i class="fa-solid fa-sort"></i></th>';
      });
      head.innerHTML = hh;
      head.querySelectorAll('th[data-sort]').forEach(function(th) {
        th.onclick = function() {
          var sortBy = th.dataset.sort;
          if (_odSortBy === sortBy) { _odSortDir = _odSortDir === 'desc' ? 'asc' : 'desc'; }
          else { _odSortBy = sortBy; _odSortDir = 'desc'; }
          _odPage = 1; _odFetchData();
        };
      });
    }
    var html = '';
    for (var i = 0; i < data.rows.length; i++) {
      var r = data.rows[i];
      html += '<tr style="border-bottom:1px solid #f1f5f9">' +
        '<td style="padding:8px;white-space:nowrap"><span class="badge ' + _odPlatformBadge(r.platform) + '">' + r.platform + '</span></td>';
      cols.forEach(function(k) {
        var f = _odFields.find(function(x) { return x.key === k; });
        if (!f) return;
        var v = r[k];
        var display = _odFmtByType(v, f.type);
        var isId = /ID|编码|SPU|货号/.test(f.label) && !/名称/.test(f.label);
        var isRefund = /退款|退货|差评|投诉|不满意/.test(f.label);
        if (isId) {
          html += '<td style="padding:8px;white-space:nowrap;font-family:monospace;font-size:11px;color:#6366f1">' + display + '</td>';
        } else if (isRefund) {
          var num = Number(v) || 0;
          var c = (num > 0) ? 'color:#ef4444' : 'color:#94a3b8';
          html += '<td style="padding:8px;text-align:right;white-space:nowrap;font-size:12px;' + c + '">' + (f.type === 'money' && num > 0 ? '-' : '') + display + '</td>';
        } else if (f.type === 'text' || f.type === 'date') {
          html += '<td style="padding:8px;text-align:left;white-space:nowrap;font-size:12px">' + display + '</td>';
        } else if (f.type === 'money') {
          html += '<td style="padding:8px;text-align:right;white-space:nowrap;font-weight:600;color:#1e293b">' + display + '</td>';
        } else {
          html += '<td style="padding:8px;text-align:right;white-space:nowrap;font-size:12px">' + display + '</td>';
        }
      });
      html += '</tr>';
    }
    tbody.innerHTML = html || '<tr><td colspan="' + (cols.length + 1) + '" style="text-align:center;padding:40px;color:#94a3b8">暂无数据</td></tr>';
  }

  function _odRenderPagination(data) {
    var el = document.getElementById('odPagination');
    if (!el) return;
    var totalPages = data.totalPages || 1;
    var page = data.page || 1;
    var html = '';
    if (totalPages <= 1) { el.innerHTML = ''; return; }
    html += '<button ' + (page <= 1 ? 'disabled' : '') + ' onclick="App._odGoPage(' + (page - 1) + ')" style="padding:6px 12px;border:1px solid #e2e8f0;border-radius:6px;background:#fff;cursor:pointer">&laquo;</button>';
    for (var i = 1; i <= totalPages; i++) {
      if (totalPages <= 7 || i === 1 || i === totalPages || (i >= page - 1 && i <= page + 1)) {
        html += '<button onclick="App._odGoPage(' + i + ')" style="padding:6px 12px;border:1px solid ' + (i === page ? '#6366f1' : '#e2e8f0') + ';border-radius:6px;background:' + (i === page ? '#6366f1' : '#fff') + ';color:' + (i === page ? '#fff' : '#475569') + ';cursor:pointer;font-weight:' + (i === page ? '600' : '400') + '">' + i + '</button>';
      } else if (i === page - 2 || i === page + 2) {
        html += '<span style="padding:6px 8px;color:#94a3b8">...</span>';
      }
    }
    html += '<button ' + (page >= totalPages ? 'disabled' : '') + ' onclick="App._odGoPage(' + (page + 1) + ')" style="padding:6px 12px;border:1px solid #e2e8f0;border-radius:6px;background:#fff;cursor:pointer">&raquo;</button>';
    el.innerHTML = html;
  }

  function _odGoPage(p) { _odPage = p; _odFetchData(); }

  function toggleOdColPanel() {
    var panel = document.getElementById('odColPanel');
    if (panel) panel.style.display = (panel.style.display === 'none' || panel.style.display === '') ? 'block' : 'none';
  }

  function toggleOdCol(cb) {
    var key = cb.dataset.key;
    if (cb.checked) {
      if (_odTableCols.indexOf(key) < 0) _odTableCols.push(key);
    } else {
      _odTableCols = _odTableCols.filter(function(k) { return k !== key; });
    }
    if (_odData) _odRenderTable(_odData);
  }

  function _odUpdateDateLabel() {
    var s = (document.getElementById('odDateStart') || {}).value || '';
    var e = (document.getElementById('odDateEnd') || {}).value || '';
    var lbl = document.getElementById('odDateLabel');
    if (!lbl) return;
    if (s && e) lbl.textContent = s + ' — ' + e;
    else if (s) lbl.textContent = s + ' — ';
    else if (e) lbl.textContent = ' — ' + e;
    else lbl.textContent = '选择日期';
  }

  function toggleOdDatePicker(e) { _calToggle('od', e); }

  // ===== 通用双月日历（订单详情 / 营销总览 / 分平台店铺 三处复用）=====
  var _calState = {};

  function _calCtx(prefix) {
    if (!_calState[prefix]) _calState[prefix] = { base: null, start: null, end: null, pickStart: true };
    return _calState[prefix];
  }

  function _calFetch(prefix) {
    if (prefix === 'od') { _odPage = 1; _odFetchData(); }
    else if (prefix === 'mkt') { renderMarketingOverview(); }
    else if (prefix === 'ps') { renderPlatformStore(); }
    else if (prefix === 'sd') { sdRenderWorks(); }
  }

  function _calParseDate(str) {
    if (!str) return null;
    var p = str.split('-');
    if (p.length !== 3) return null;
    return new Date(parseInt(p[0], 10), parseInt(p[1], 10) - 1, parseInt(p[2], 10));
  }

  function _calFmtDate(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  function _calUpdateLabel(prefix) {
    var s = (document.getElementById(prefix + 'DateStart') || {}).value || '';
    var e = (document.getElementById(prefix + 'DateEnd') || {}).value || '';
    var lbl = document.getElementById(prefix + 'DateLabel');
    if (!lbl) return;
    if (s && e) lbl.textContent = s + ' — ' + e;
    else if (s) lbl.textContent = s + ' — ';
    else if (e) lbl.textContent = ' — ' + e;
    else lbl.textContent = '选择日期';
  }

  function _calReset(prefix) {
    var st = _calCtx(prefix);
    var now = new Date();
    st.base = new Date(now.getFullYear(), now.getMonth() - 1, 1); // 左月=上个月，右月=本月
    st.start = _calParseDate((document.getElementById(prefix + 'DateStart') || {}).value || '');
    st.end = _calParseDate((document.getElementById(prefix + 'DateEnd') || {}).value || '');
    st.pickStart = !(st.start && st.end);
  }

  function _calMonth(prefix, y, m, isLeft) {
    var st = _calCtx(prefix);
    var first = new Date(y, m, 1);
    var days = new Date(y, m + 1, 0).getDate();
    var startCol = (first.getDay() + 6) % 7; // 周一=0
    var now = new Date();
    var html = '<div class="od-cal">';
    html += '<div class="od-cal-head">' +
      (isLeft ? '<button type="button" class="od-cal-nav" onclick="App.' + prefix + 'CalNav(-1)">‹</button>' : '<span style="width:24px"></span>') +
      '<span>' + y + '年' + (m + 1) + '月</span>' +
      (!isLeft ? '<button type="button" class="od-cal-nav" onclick="App.' + prefix + 'CalNav(1)">›</button>' : '<span style="width:24px"></span>') +
      '</div>';
    html += '<div class="od-cal-week">';
    ['一', '二', '三', '四', '五', '六', '日'].forEach(function(w) { html += '<span>' + w + '</span>'; });
    html += '</div><div class="od-cal-days">';
    for (var i = 0; i < startCol; i++) html += '<span class="od-cal-day blank"></span>';
    for (var d = 1; d <= days; d++) {
      var dt = new Date(y, m, d);
      var cls = 'od-cal-day';
      if (st.start && dt.getTime() === st.start.getTime()) cls += ' is-start';
      if (st.end && dt.getTime() === st.end.getTime()) cls += ' is-end';
      if (st.start && st.end && dt > st.start && dt < st.end) cls += ' in-range';
      if (dt.getFullYear() === now.getFullYear() && dt.getMonth() === now.getMonth() && dt.getDate() === now.getDate()) cls += ' is-today';
      html += '<button type="button" class="' + cls + '" onclick="App.' + prefix + 'CalPick(' + y + ',' + m + ',' + d + ')">' + d + '</button>';
    }
    html += '</div></div>';
    return html;
  }

  function _calRender(prefix) {
    var st = _calCtx(prefix);
    var panel = document.getElementById(prefix + 'DatePanel');
    if (!panel) return;
    var leftY = st.base.getFullYear(), leftM = st.base.getMonth();
    var right = new Date(leftY, leftM + 1, 1);
    var html = '<div class="od-cal-wrap">' +
      _calMonth(prefix, leftY, leftM, true) +
      _calMonth(prefix, right.getFullYear(), right.getMonth(), false) +
      '</div>';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:10px;padding-top:10px;border-top:1px solid #f1f5f9">' +
      '<span style="font-size:12px;color:#64748b">' + (st.pickStart ? '请选择开始日期' : '请选择结束日期') + '</span>' +
      '<div style="display:flex;gap:10px">' +
      '<button onclick="App.' + prefix + 'CalClear()" style="border:none;background:none;color:#94a3b8;font-size:12px;cursor:pointer">清除</button>' +
      '<button onclick="App.' + prefix + 'CalToday()" style="border:none;background:none;color:#6366f1;font-size:12px;cursor:pointer;font-weight:600">今天</button>' +
      '</div></div>';
    panel.innerHTML = html;
  }

  function _calToggle(prefix, e) {
    if (e) e.stopPropagation();
    var panel = document.getElementById(prefix + 'DatePanel');
    if (!panel) return;
    var isHidden = panel.style.display === 'none' || panel.style.display === '';
    panel.style.display = isHidden ? 'block' : 'none';
    if (isHidden) { _calReset(prefix); _calRender(prefix); }
  }

  function _calPick(prefix, y, m, d) {
    var st = _calCtx(prefix);
    var dt = new Date(y, m, d);
    if (st.pickStart) {
      st.start = dt; st.end = null; st.pickStart = false;
      _calRender(prefix);
    } else {
      if (dt < st.start) { st.end = st.start; st.start = dt; }
      else { st.end = dt; }
      var s = document.getElementById(prefix + 'DateStart');
      var e = document.getElementById(prefix + 'DateEnd');
      if (s) s.value = _calFmtDate(st.start);
      if (e) e.value = _calFmtDate(st.end);
      _calUpdateLabel(prefix);
      _calFetch(prefix);
      var panel = document.getElementById(prefix + 'DatePanel');
      if (panel) panel.style.display = 'none';
      st.pickStart = true;
    }
  }

  function _calNav(prefix, delta) {
    var st = _calCtx(prefix);
    st.base = new Date(st.base.getFullYear(), st.base.getMonth() + delta, 1);
    _calRender(prefix);
  }

  function _calClear(prefix) {
    var s = document.getElementById(prefix + 'DateStart');
    var e = document.getElementById(prefix + 'DateEnd');
    if (s) s.value = '';
    if (e) e.value = '';
    _calUpdateLabel(prefix);
    _calFetch(prefix);
    _calReset(prefix);
    _calRender(prefix);
  }

  function _calToday(prefix) {
    var now = new Date();
    var dt = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var s = document.getElementById(prefix + 'DateStart');
    var e = document.getElementById(prefix + 'DateEnd');
    if (s) s.value = _calFmtDate(dt);
    if (e) e.value = _calFmtDate(dt);
    var st = _calCtx(prefix);
    st.start = dt; st.end = dt;
    _calUpdateLabel(prefix);
    _calFetch(prefix);
    var panel = document.getElementById(prefix + 'DatePanel');
    if (panel) panel.style.display = 'none';
    st.pickStart = true;
  }

  // ===== 三个页面的日历入口（供 HTML onclick 调用）=====
  function odCalPick(y, m, d) { _calPick('od', y, m, d); }
  function odCalNav(delta) { _calNav('od', delta); }
  function odCalClear() { _calClear('od'); }
  function odCalToday() { _calToday('od'); }

  function toggleMktDatePicker(e) { _calToggle('mkt', e); }
  function mktCalPick(y, m, d) { _calPick('mkt', y, m, d); }
  function mktCalNav(delta) { _calNav('mkt', delta); }
  function mktCalClear() { _calClear('mkt'); }
  function mktCalToday() { _calToday('mkt'); }

  function togglePsDatePicker(e) { _calToggle('ps', e); }
  function psCalPick(y, m, d) { _calPick('ps', y, m, d); }
  function psCalNav(delta) { _calNav('ps', delta); }
  function psCalClear() { _calClear('ps'); }
  function psCalToday() { _calToday('ps'); }

  function _odSetDateRange(days) {
    var end = new Date(Date.now() - 86400000);
    var start = new Date(end);
    start.setDate(start.getDate() - days + 1);
    var fmt = function(d) { return d.toISOString().slice(0, 10); };
    document.getElementById('odDateStart').value = fmt(start);
    document.getElementById('odDateEnd').value = fmt(end);
    _odUpdateDateLabel();
    _odUpdateDateShortcuts(fmt(start), fmt(end));
    _odPage = 1; _odFetchData();
  }

  function _odUpdateDateShortcuts(s, e) {
    var yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    document.querySelectorAll('.od-ds-btn').forEach(function(btn) {
      var range = parseInt(btn.dataset.range);
      var match = false;
      if (range === 1) { match = (s === yesterday && e === yesterday); }
      else {
        var calcStart = new Date(Date.now() - 86400000 - (range - 1) * 86400000).toISOString().slice(0, 10);
        match = (s === calcStart && e === yesterday);
      }
      btn.classList.toggle('active', match);
    });
  }

  // ==================== 智能体助手 - 浪小助 ====================
  var _aiChatOpen = false;
  var _aiChatHistory = [];

  function aiToggleChat() {
    _aiChatOpen = !_aiChatOpen;
    var win = document.getElementById('aiChatWindow');
    var btn = document.getElementById('aiFloatBtn');
    if (win) win.classList.toggle('collapsed', !_aiChatOpen);
    if (btn) btn.style.display = _aiChatOpen ? 'none' : '';
    if (_aiChatOpen) {
      var input = document.getElementById('aiChatInput');
      if (input) setTimeout(function() { input.focus(); }, 300);
    }
  }

  async function aiSendMessage() {
    var input = document.getElementById('aiChatInput');
    var sendBtn = document.getElementById('aiChatSendBtn');
    var body = document.getElementById('aiChatBody');
    if (!input || !body) return;

    var message = input.value.trim();
    if (!message) return;

    // 显示用户消息
    body.innerHTML += '<div class="ai-msg user"><div class="ai-msg-bubble">' + esc(message) + '</div></div>';
    input.value = '';
    if (sendBtn) sendBtn.disabled = true;

    // 显示打字中
    var typingId = 'aiTyping_' + Date.now();
    body.innerHTML += '<div class="ai-msg" id="' + typingId + '"><div class="ai-msg-avatar"><i class="fa-solid fa-robot"></i></div><div class="ai-msg-bubble"><em style="color:#94a3b8">思考中...</em></div></div>';
    body.scrollTop = body.scrollHeight;

    _aiChatHistory.push({ role: 'user', content: message });

    try {
      var resp = await fetch('/api/ai/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: message, history: _aiChatHistory }),
      });
      var json = await resp.json();
      var reply = (json && json.code === 0 && json.data) ? json.data.reply : '抱歉，AI服务暂时不可用，请稍后再试。';
      _aiChatHistory.push({ role: 'assistant', content: reply });

      // 替换打字指示器
      var typingEl = document.getElementById(typingId);
      if (typingEl) {
        typingEl.outerHTML = '<div class="ai-msg"><div class="ai-msg-avatar"><i class="fa-solid fa-robot"></i></div><div class="ai-msg-bubble">' + reply.replace(/\n/g, '<br>') + '</div></div>';
      }
    } catch (e) {
      var typingEl = document.getElementById(typingId);
      if (typingEl) {
        typingEl.outerHTML = '<div class="ai-msg"><div class="ai-msg-avatar"><i class="fa-solid fa-robot"></i></div><div class="ai-msg-bubble" style="color:#dc2626">网络异常，请稍后再试</div></div>';
      }
    }

    if (sendBtn) sendBtn.disabled = false;
    body.scrollTop = body.scrollHeight;
  }

  // ==================== Public API ====================
  // ==================== 选品助手 ====================
  function renderProductSelection() {
    switchSelectionTab('tmall');
    loadTmallList();
    loadDouyinList();
    loadAisouList();
    loadTmallMarket();
    loadDouyinMarket();
    load1688Market();
  }

  function switchSelectionTab(tab) {
    document.querySelectorAll('#page-product-selection .sel-tab').forEach(function (b) {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    document.querySelectorAll('#page-product-selection .sel-panel').forEach(function (p) {
      p.classList.toggle('active', p.dataset.tab === tab);
    });
  }

  var _tmallPage = 1;
  var _tmallPageSize = 50;

  async function loadTmallList() {
    const minPrice = document.getElementById('tmallMinPrice')?.value || '';
    const maxPrice = document.getElementById('tmallMaxPrice')?.value || '';
    let data = null;
    if (state.apiAvailable) data = await ApiService.getTmallList(minPrice, maxPrice, _tmallPage, _tmallPageSize);

    const tbody = document.getElementById('tmallTbody');
    if (!tbody) return;
    const items = (data && data.items) || [];
    const total = data ? data.total : 0;
    const totalEl = document.getElementById('tmallTotal');
    if (totalEl) totalEl.textContent = '共 ' + total + ' 条';

    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#94a3b8;padding:32px">暂无数据</td></tr>';
    } else {
      tbody.innerHTML = items.map(function (r) {
        const price = (r['价格'] === null || r['价格'] === undefined) ? '--' : '¥' + Number(r['价格']).toFixed(2);
        return '<tr>' +
          '<td>' + esc(r['类别名']) + '</td>' +
          '<td>' + esc(r['排行榜名']) + '</td>' +
          '<td style="font-weight:600">' + esc(r['产品名']) + '</td>' +
          '<td class="ps-col-num" style="font-weight:600;color:#0ea5e9">' + price + '</td>' +
          '<td>' + esc(r['日期']) + '</td>' +
          '</tr>';
      }).join('');
    }
    _renderTmallPagination(total);
  }

  function _renderTmallPagination(total) {
    const footer = document.getElementById('tmallPagination');
    if (!footer) return;
    const totalPages = Math.max(1, Math.ceil(total / _tmallPageSize));
    if (_tmallPage > totalPages) _tmallPage = totalPages;
    let btns = '';
    btns += '<button ' + (_tmallPage <= 1 ? 'disabled' : '') + ' onclick="App.tmallGoPage(' + (_tmallPage - 1) + ')"><i class="fa-solid fa-chevron-left"></i></button>';
    for (let i = 1; i <= totalPages; i++) {
      if (totalPages <= 7 || i === 1 || i === totalPages || (i >= _tmallPage - 1 && i <= _tmallPage + 1)) {
        btns += '<button class="' + (i === _tmallPage ? 'active' : '') + '" onclick="App.tmallGoPage(' + i + ')">' + i + '</button>';
      } else if (i === _tmallPage - 2 || i === _tmallPage + 2) {
        btns += '<button disabled>...</button>';
      }
    }
    btns += '<button ' + (_tmallPage >= totalPages ? 'disabled' : '') + ' onclick="App.tmallGoPage(' + (_tmallPage + 1) + ')"><i class="fa-solid fa-chevron-right"></i></button>';
    footer.innerHTML = '<span>第 ' + _tmallPage + ' / ' + totalPages + ' 页，共 ' + total + ' 条</span><div class="ps-pagination-btns">' + btns + '</div>';
  }

  function tmallGoPage(page) {
    _tmallPage = page;
    loadTmallList();
  }

  function filterTmall() { _tmallPage = 1; loadTmallList(); }

  function resetTmall() {
    const minEl = document.getElementById('tmallMinPrice');
    const maxEl = document.getElementById('tmallMaxPrice');
    if (minEl) minEl.value = '';
    if (maxEl) maxEl.value = '';
    _tmallPage = 1;
    loadTmallList();
  }

  async function loadDouyinList() {
    const date = document.getElementById('douyinDate')?.value || '';
    let data = null;
    if (state.apiAvailable) data = await ApiService.getDouyinList(date);

    const dateSel = document.getElementById('douyinDate');
    const dates = (data && data.dates) || [];
    if (dateSel) {
      if (dates.length) {
        const cur = dateSel.value;
        dateSel.innerHTML = dates.map(function (d) {
          return '<option value="' + d + '"' + (d === cur ? ' selected' : '') + '>' + d + '</option>';
        }).join('');
        // 首次加载未选日期时，默认选中最新一天（后端默认返回该天数据）
        if (!date) dateSel.value = dates[0];
      } else {
        dateSel.innerHTML = '<option value="" disabled selected>暂无日期</option>';
      }
    }

    const tbody = document.getElementById('douyinTbody');
    if (!tbody) return;
    const items = (data && data.items) || [];
    const totalEl = document.getElementById('douyinTotal');
    if (totalEl) totalEl.textContent = '共 ' + (data ? data.total : 0) + ' 条';

    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#94a3b8;padding:32px">暂无数据</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(function (r, i) {
      return '<tr>' +
        '<td style="color:#94a3b8">' + (i + 1) + '</td>' +
        '<td style="font-weight:600">' + esc(r['热搜名']) + '</td>' +
        '<td class="ps-col-num" style="font-weight:600;color:#ef4444">' + esc(r['热搜值']) + '</td>' +
        '<td>' + (r['品类'] ? esc(r['品类']) : '<span style="color:#cbd5e1">--</span>') + '</td>' +
        '<td>' + esc(r['日期']) + '</td>' +
        '</tr>';
    }).join('');
  }

  function filterDouyin() { loadDouyinList(); }

  function dyHotToggleCookiePanel() {
    const panel = document.getElementById('dyHotCookiePanel');
    if (!panel) return;
    if (panel.classList.contains('hidden')) {
      dyHotLoadCookie();
      panel.classList.remove('hidden');
    } else {
      panel.classList.add('hidden');
    }
  }

  function dyHotLoadCookie() {
    const el = document.getElementById('dyHotCookie');
    if (!state.apiAvailable) {
      if (el) { el.value = ''; el.placeholder = '后端不可用，无法读取 Cookie'; }
      return;
    }
    ApiService.getDouyinHotCookie().then(function (data) {
      if (el && data && data.cookie) { el.value = data.cookie; el.title = data.cookie; }
    });
  }

  async function dyHotSaveCookie() {
    const el = document.getElementById('dyHotCookie');
    const cookie = (el && el.value || '').trim();
    if (!cookie) { showToast('请输入 Cookie', 'error'); return; }
    if (!state.apiAvailable) { showToast('后端不可用，无法保存 Cookie', 'error'); return; }
    const res = await ApiService.saveDouyinHotCookie(cookie);
    showToast(res !== null ? '抖音热点宝 Cookie 已保存' : 'Cookie 保存失败', res !== null ? 'success' : 'error');
    dyHotToggleCookiePanel();
  }

  async function dyHotScrape() {
    if (!state.apiAvailable) { showToast('后端服务不可用', 'error'); return; }
    const res = await ApiService.triggerDouyinHotScrape();
    if (!res) { showToast('触发失败，请查看后端日志', 'error'); return; }
    showToast('已触发热点宝抓取，完成后自动筛选', 'success');
    const btn = document.getElementById('dyHotScrapeBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 抓取中...'; }
    for (let i = 0; i < 300; i++) {
      await new Promise(function (r) { setTimeout(r, 3000); });
      const st = await ApiService.getDouyinHotStatus();
      if (st && st.status === 'done') {
        showToast('抓取并筛选完成：同步 ' + (st.matched || 0) + ' 条电商热搜', 'success');
        await loadDouyinList();
        break;
      }
      if (st && st.status === 'error') {
        showToast('抓取失败：' + (st.message || ''), 'error');
        break;
      }
    }
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-bolt"></i> 抓取并筛选'; }
  }

  async function loadAisouList() {
    const date = document.getElementById('aisouDate')?.value || '';
    const keyword = document.getElementById('aisouKeyword')?.value || '';
    let data = null;
    if (state.apiAvailable) data = await ApiService.getAisouList(date, keyword);

    const dateSel = document.getElementById('aisouDate');
    const dates = (data && data.dates) || [];
    if (dateSel) {
      if (dates.length) {
        const cur = dateSel.value;
        const opts = ['<option value="all">全部日期</option>'];
        opts.push(dates.map(function (d) {
          return '<option value="' + d + '"' + (d === cur ? ' selected' : '') + '>' + d + '</option>';
        }).join(''));
        dateSel.innerHTML = opts.join('');
        // 首次加载未选日期时，默认选中最新一天（后端默认返回该天数据）
        if (!date) dateSel.value = dates[0];
      } else {
        dateSel.innerHTML = '<option value="" disabled selected>暂无日期</option>';
      }
    }

    const tbody = document.getElementById('aisouTbody');
    if (!tbody) return;
    const items = (data && data.items) || [];
    const totalEl = document.getElementById('aisouTotal');
    if (totalEl) totalEl.textContent = '共 ' + (data ? data.total : 0) + ' 条';

    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:32px">暂无数据（点击上方 Cookie 保存后由智能体自动抓取）</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(function (r) {
      const type = r['词类型'] || '';
      const typeColor = type === '电商词' ? '#0ea5e9' : type === '搜索词' ? '#8b5cf6' : type === '相关词' ? '#16a34a' : '#f97316';
      return '<tr>' +
        '<td>' + esc(r['日期']) + '</td>' +
        '<td style="font-weight:600">' + esc(r['来源词']) + '</td>' +
        '<td><span style="font-size:11px;padding:2px 8px;border-radius:999px;background:#f1f5f9;color:' + typeColor + '">' + esc(type) + '</span></td>' +
        '<td style="font-weight:600">' + esc(r['词名称']) + '</td>' +
        '<td class="ps-col-num" style="font-weight:600;color:#8b5cf6">' + esc(r['月覆盖人次']) + '</td>' +
        '<td class="ps-col-num">' + esc(r['七日搜索人次']) + '</td>' +
        '</tr>';
    }).join('');
  }

  function filterAisou() { loadAisouList(); }

  function resetAisou() {
    const kwEl = document.getElementById('aisouKeyword');
    const dateSel = document.getElementById('aisouDate');
    if (kwEl) kwEl.value = '';
    if (dateSel) dateSel.value = '';
    loadAisouList();
  }

  // ==================== 市场抓取（天猫市场 / 抖音市场 / 1688市场 共用） ====================
  var _marketPolling = { tmall: false, douyin: false, '1688': false };
  var _MARKET = {
    tmall: {
      label: '天猫市场',
      keywordId: 'tmallMarketKeyword', btnId: 'tmallMarketScrapeBtn',
      totalId: 'tmallMarketTotal', tbodyId: 'tmallMarketTbody',
      wrapId: 'tmallMarketProgressWrap', fillId: 'tmallMarketProgressFill',
      textId: 'tmallMarketProgressText', pctId: 'tmallMarketProgressPercent',
      trigger: function (kw) { return ApiService.triggerTmallMarketScrape(kw); },
      getData: function () { return ApiService.getTmallMarketData(); },
      getStatus: function () { return ApiService.getTmallMarketStatus(); },
      hint: '抓取中，首次运行需在弹出的 Chrome 里扫码登录淘宝...',
      emptyMsg: '抓取结果为空（可能未登录或该关键词无结果）'
    },
    douyin: {
      label: '抖音市场',
      keywordId: 'douyinMarketKeyword', btnId: 'douyinMarketScrapeBtn',
      totalId: 'douyinMarketTotal', tbodyId: 'douyinMarketTbody',
      wrapId: 'douyinMarketProgressWrap', fillId: 'douyinMarketProgressFill',
      textId: 'douyinMarketProgressText', pctId: 'douyinMarketProgressPercent',
      trigger: function (kw) { return ApiService.triggerDouyinMarketScrape(kw); },
      getData: function () { return ApiService.getDouyinMarketData(); },
      getStatus: function () { return ApiService.getDouyinMarketStatus(); },
      hint: '抓取中，正在后台执行抖音商城销量前50抓取...',
      emptyMsg: '抓取结果为空（该关键词暂无销量数据）'
    },
    '1688': {
      label: '1688市场',
      keywordId: 'm1688MarketKeyword', btnId: 'm1688MarketScrapeBtn',
      totalId: 'm1688MarketTotal', tbodyId: 'm1688MarketTbody',
      wrapId: 'm1688MarketProgressWrap', fillId: 'm1688MarketProgressFill',
      textId: 'm1688MarketProgressText', pctId: 'm1688MarketProgressPercent',
      trigger: function (kw) { return ApiService.trigger1688MarketScrape(kw); },
      getData: function () { return ApiService.get1688MarketData(); },
      getStatus: function () { return ApiService.get1688MarketStatus(); },
      hasSales: false,
      hint: '抓取中，正在后台执行1688前10页抓取（约需2~5分钟）...',
      emptyMsg: '抓取结果为空（可能未保存 Cookie 或该关键词无结果）'
    }
  };

  function _marketSetProgress(platform, pct, text) {
    const c = _MARKET[platform];
    const wrap = document.getElementById(c.wrapId);
    const fill = document.getElementById(c.fillId);
    const txt = document.getElementById(c.textId);
    const pctEl = document.getElementById(c.pctId);
    if (wrap) wrap.style.display = '';
    if (fill) fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
    if (txt && text) txt.textContent = text;
    if (pctEl) pctEl.textContent = Math.max(0, Math.min(100, pct)) + '%';
  }

  function _marketHideProgress(platform) {
    const wrap = document.getElementById(_MARKET[platform].wrapId);
    if (wrap) wrap.style.display = 'none';
  }

  function _renderMarket(platform, data) {
    const c = _MARKET[platform];
    const kwEl = document.getElementById(c.keywordId);
    const badge = document.getElementById(c.totalId);
    const tbody = document.getElementById(c.tbodyId);
    const hasSales = c.hasSales !== false;
    const colspan = hasSales ? 6 : 5;
    if (!data) {
      if (badge) badge.textContent = '尚未抓取';
      if (tbody) tbody.innerHTML = '<tr><td colspan="' + colspan + '" style="text-align:center;color:#94a3b8;padding:32px">暂无数据，输入关键词后点击抓取按钮</td></tr>';
      return;
    }
    if (kwEl && data.keyword) kwEl.value = data.keyword;
    const items = (data.products || []);
    if (badge) badge.textContent = (data.keyword ? ('“' + data.keyword + '” ') : '') + '共 ' + (data.count !== undefined ? data.count : items.length) + ' 条';
    if (!tbody) return;
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="' + colspan + '" style="text-align:center;color:#94a3b8;padding:32px">' + c.emptyMsg + '</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(function (r) {
      const img = r.image ? '<img src="' + esc(r.image) + '" alt="" referrerpolicy="no-referrer" style="width:48px;height:48px;object-fit:cover;border-radius:6px;background:#f1f5f9" loading="lazy">' : '<span style="color:#cbd5e1">--</span>';
      const price = (r.price === null || r.price === undefined) ? '--' : esc(r.price);
      return '<tr>' +
        '<td style="color:#94a3b8">' + (r.rank || '') + '</td>' +
        '<td>' + img + '</td>' +
        '<td style="font-weight:600;max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="' + esc(r.title || '') + '">' +
          (r.link ? '<a href="' + esc(r.link) + '" target="_blank" rel="noopener noreferrer" style="color:#1677ff;text-decoration:none">' + esc(r.title || '--') + '</a>' : esc(r.title || '--')) +
        '</td>' +
        '<td class="ps-col-num" style="font-weight:600;color:#0ea5e9">' + price + '</td>' +
        (hasSales ? '<td class="ps-col-num">' + esc(r.sales || '--') + '</td>' : '') +
        '<td>' + esc(r.shop || '--') + '</td>' +
        '</tr>';
    }).join('');
  }

  function _loadMarket(platform) {
    const c = _MARKET[platform];
    if (!state.apiAvailable) { _renderMarket(platform, null); return; }
    c.getData().then(function (data) { _renderMarket(platform, data); });
  }

  async function _triggerMarketScrape(platform) {
    if (_marketPolling[platform]) return;
    if (!state.apiAvailable) { showToast('后端服务不可用', 'error'); return; }
    const c = _MARKET[platform];
    const kwEl = document.getElementById(c.keywordId);
    const keyword = (kwEl ? kwEl.value : '').trim();
    if (!keyword) { showToast('请输入要抓取的商品关键词', 'error'); return; }

    const btn = document.getElementById(c.btnId);
    _marketPolling[platform] = true;
    if (btn) btn.disabled = true;
    _marketSetProgress(platform, 2, c.hint);

    try {
      const res = await c.trigger(keyword);
      if (!res) { showToast('触发失败，请查看后端日志', 'error'); _marketHideProgress(platform); return; }
      showToast('已触发' + c.label + '抓取：' + keyword, 'success');
      for (let i = 0; i < 150; i++) {
        await new Promise(function (r) { setTimeout(r, 3000); });
        const st = await c.getStatus();
        if (st) {
          if (st.status === 'error') {
            _marketHideProgress(platform);
            showToast(c.label + '抓取失败：' + (st.error || '未知错误'), 'error');
            return;
          }
          _marketSetProgress(platform, st.progress || 0, '抓取中 ' + (st.done || 0) + '/' + (st.total || 50));
          if (st.finished) {
            const data = await c.getData();
            _renderMarket(platform, data);
            const n = (data && data.count !== undefined) ? data.count : (data && data.products ? data.products.length : 0);
            _marketSetProgress(platform, 100, '抓取完成：共 ' + n + ' 条');
            showToast(c.label + '抓取完成：共 ' + n + ' 条', 'success');
            setTimeout(function () { _marketHideProgress(platform); }, 3000);
            return;
          }
        }
      }
      _marketHideProgress(platform);
      showToast('抓取超时，请确认后重试', 'error');
    } catch (e) {
      _marketHideProgress(platform);
      showToast('抓取请求异常', 'error');
    } finally {
      _marketPolling[platform] = false;
      if (btn) btn.disabled = false;
    }
  }

  function loadTmallMarket() { _loadMarket('tmall'); }
  function triggerTmallMarketScrape() { _triggerMarketScrape('tmall'); }
  function loadDouyinMarket() { _loadMarket('douyin'); }
  function triggerDouyinMarketScrape() { _triggerMarketScrape('douyin'); }
  function load1688Market() { _loadMarket('1688'); }
  function trigger1688MarketScrape() { _triggerMarketScrape('1688'); }

  function tmToggleCookiePanel() {
    const panel = document.getElementById('tmCookiePanel');
    if (!panel) return;
    if (panel.classList.contains('hidden')) {
      tmLoadCookie();
      panel.classList.remove('hidden');
    } else {
      panel.classList.add('hidden');
    }
  }

  function tmLoadCookie() {
    const el = document.getElementById('tmTaobaoCookie');
    if (!state.apiAvailable) {
      if (el) { el.value = ''; el.placeholder = '后端不可用，无法读取 Cookie'; }
      return;
    }
    ApiService.getTmallMarketCookie().then(function (data) {
      if (el && data && data.cookie) { el.value = data.cookie; el.title = data.cookie; }
    });
  }

  async function tmSaveCookie() {
    const el = document.getElementById('tmTaobaoCookie');
    const cookie = (el && el.value || '').trim();
    if (!cookie) { showToast('请输入 Cookie', 'error'); return; }
    if (!state.apiAvailable) { showToast('后端不可用，无法保存 Cookie', 'error'); return; }
    const res = await ApiService.saveTmallMarketCookie(cookie);
    showToast(res !== null ? '淘宝 Cookie 已保存' : 'Cookie 保存失败', res !== null ? 'success' : 'error');
    tmToggleCookiePanel();
  }

  function m1688ToggleCookiePanel() {
    const panel = document.getElementById('m1688CookiePanel');
    if (!panel) return;
    if (panel.classList.contains('hidden')) {
      m1688LoadCookie();
      panel.classList.remove('hidden');
    } else {
      panel.classList.add('hidden');
    }
  }

  function m1688LoadCookie() {
    const el = document.getElementById('m1688Cookie');
    if (!state.apiAvailable) {
      if (el) { el.value = ''; el.placeholder = '后端不可用，无法读取 Cookie'; }
      return;
    }
    ApiService.get1688MarketCookie().then(function (data) {
      if (el && data && data.cookie) { el.value = data.cookie; el.title = data.cookie; }
    });
  }

  async function m1688SaveCookie() {
    const el = document.getElementById('m1688Cookie');
    const cookie = (el && el.value || '').trim();
    if (!cookie) { showToast('请输入 Cookie', 'error'); return; }
    if (!state.apiAvailable) { showToast('后端不可用，无法保存 Cookie', 'error'); return; }
    const res = await ApiService.save1688MarketCookie(cookie);
    showToast(res !== null ? '1688 Cookie 已保存' : 'Cookie 保存失败', res !== null ? 'success' : 'error');
    m1688ToggleCookiePanel();
  }

  // ==================== 选品助手智能体 ====================
  var _psCards = [];            // 第一轮 50 个商品卡片
  var _psPriceRange = null;     // {min, max}
  var _psBusy = false;

  // ---- 爱搜 Cookie ----
  function aisouToggleCookiePanel() {
    const panel = document.getElementById('aisouCookiePanel');
    if (!panel) return;
    if (panel.classList.contains('hidden')) {
      aisouLoadCookie();
      panel.classList.remove('hidden');
    } else {
      panel.classList.add('hidden');
    }
  }

  function aisouLoadCookie() {
    const el = document.getElementById('aisouCookie');
    if (!state.apiAvailable) {
      if (el) { el.value = ''; el.placeholder = '后端不可用，无法读取 Cookie'; }
      return;
    }
    ApiService.getAisouCookie().then(function (data) {
      if (el && data && data.cookie) { el.value = data.cookie; el.title = data.cookie; }
    });
  }

  async function aisouSaveCookie() {
    const el = document.getElementById('aisouCookie');
    const cookie = (el && el.value || '').trim();
    if (!cookie) { showToast('请输入 Cookie', 'error'); return; }
    if (!state.apiAvailable) { showToast('后端不可用，无法保存 Cookie', 'error'); return; }
    const res = await ApiService.saveAisouCookie(cookie);
    showToast(res !== null ? '爱搜 Cookie 已保存' : 'Cookie 保存失败', res !== null ? 'success' : 'error');
    aisouToggleCookiePanel();
  }

  // ---- 价格区间弹窗（当日热搜选品分析） ----
  function psOpenPriceModal() {
    const modal = document.getElementById('psPriceModal');
    if (modal) modal.classList.remove('hidden');
  }

  function psClosePriceModal() {
    const modal = document.getElementById('psPriceModal');
    if (modal) modal.classList.add('hidden');
  }

  async function psConfirmPrice() {
    const minEl = document.getElementById('psPriceMin');
    const maxEl = document.getElementById('psPriceMax');
    let min = minEl ? minEl.value.trim() : '';
    let max = maxEl ? maxEl.value.trim() : '';
    if (!min && !max) { showToast('请至少填写一个价格边界', 'error'); return; }
    min = min === '' ? undefined : Number(min);
    max = max === '' ? undefined : Number(max);
    _psPriceRange = { min: min, max: max };
    psClosePriceModal();
    if (_psBusy) return;
    _psBusy = true;
    const result = document.getElementById('selAgentResult');
    if (result) result.innerHTML = '<div class="sa-loading"><i class="fa-solid fa-spinner"></i> 正在分析当日热搜选品，请稍候...</div>';
    try {
      let data = null;
      if (state.apiAvailable) data = await ApiService.runSelection({ minPrice: min, maxPrice: max });
      _psRenderCards(data);
    } catch (e) {
      if (result) result.innerHTML = '<div class="sa-analysis" style="color:#dc2626">分析失败：网络异常，请稍后再试。</div>';
    } finally {
      _psBusy = false;
    }
  }

  function _psRenderCards(data) {
    const result = document.getElementById('selAgentResult');
    if (!result) return;
    if (!data || !(data.cards && data.cards.length)) {
      result.innerHTML = '<div class="sa-analysis">智能体未返回有效商品，请稍后重试。</div>';
      return;
    }
    _psCards = data.cards;
    const n = _psCards.length;
    result.innerHTML =
      '<div class="ps-round-box">' +
        '<div class="ps-round-head">' +
          '<span><i class="fa-solid fa-lightbulb" style="color:#f59e0b"></i> 共筛选出 ' + n + ' 个潜力商品（价格区间 ' + esc(data.priceRange && data.priceRange.min !== null ? '¥' + data.priceRange.min : '不限') + ' ~ ' + esc(data.priceRange && data.priceRange.max !== null ? '¥' + data.priceRange.max : '不限') + '）</span>' +
          '<button class="ps-expand-btn" onclick="App.psExpandCards()"><i class="fa-solid fa-up-right-and-down-left-from-center"></i> 展开勾选</button>' +
        '</div>' +
        '<div class="ps-round-tip">点击「展开勾选」进入大面板，勾选 10 个看好的品后提交分析。</div>' +
      '</div>';
  }

  // ---- 50 卡片大面板 ----
  function psExpandCards() {
    const panel = document.getElementById('psCardsPanel');
    const grid = document.getElementById('psCardsGrid');
    if (!panel || !grid) return;
    grid.innerHTML = _psCards.map(function (c, i) {
      return '<label class="ps-pick-card">' +
        '<input type="checkbox" class="ps-pick-check" value="' + i + '" onchange="App.psUpdateCardCount()">' +
        '<div class="ps-pick-body">' +
          '<div class="ps-pick-name">' + esc(c.name || c.title || '') + '</div>' +
          '<div class="ps-pick-tags"><span>' + esc(c.category || '') + '</span><span class="price">' + esc(c.price_range || '') + '</span></div>' +
          (c.reason ? '<div class="ps-pick-reason">' + esc(c.reason) + '</div>' : '') +
        '</div>' +
      '</label>';
    }).join('');
    panel.classList.remove('hidden');
    psUpdateCardCount();
  }

  function psCloseCardsPanel() {
    const panel = document.getElementById('psCardsPanel');
    if (panel) panel.classList.add('hidden');
  }

  function psUpdateCardCount() {
    const checks = document.querySelectorAll('#psCardsGrid .ps-pick-check:checked');
    const el = document.getElementById('psCardCount');
    if (el) el.textContent = checks.length;
    const btn = document.getElementById('psSubmitCardsBtn');
    if (btn) btn.disabled = checks.length === 0;
  }

  async function psSubmitCards() {
    const checks = document.querySelectorAll('#psCardsGrid .ps-pick-check:checked');
    const selected = Array.from(checks).map(function (c) {
      const card = _psCards[Number(c.value)];
      return card ? (card.name || card.title || '') : '';
    }).filter(Boolean);
    if (!selected.length) { showToast('请先勾选商品', 'error'); return; }
    if (selected.length > 10) { showToast('最多勾选 10 个', 'error'); return; }
    psCloseCardsPanel();
    const result = document.getElementById('selAgentResult');
    if (result) result.innerHTML = '<div class="sa-loading"><i class="fa-solid fa-spinner"></i> 已提交 ' + selected.length + ' 个商品，正在爱搜/天猫过数据...</div>';
    try {
      let res = null;
      if (state.apiAvailable) res = await ApiService.runSelectionAnalyze(selected, _psPriceRange);
      if (!res) { if (result) result.innerHTML = '<div class="sa-analysis" style="color:#dc2626">提交失败，请查看后端日志。</div>'; return; }
      // 轮询任务状态
      for (let i = 0; i < 600; i++) {
        await new Promise(function (r) { setTimeout(r, 3000); });
        const st = await ApiService.getSelectionStatus();
        if (st && st.status === 'done') {
          const final = await ApiService.getSelectionResult();
          _psRenderFinal(final);
          return;
        }
        if (st && st.status === 'error') {
          if (result) result.innerHTML = '<div class="sa-analysis" style="color:#dc2626">选品分析失败：' + esc(st.message || '') + '</div>';
          return;
        }
        if (result) {
          const msg = st && st.message ? st.message : '处理中';
          result.innerHTML = '<div class="sa-loading"><i class="fa-solid fa-spinner"></i> ' + esc(msg) + '...</div>';
        }
      }
      if (result) result.innerHTML = '<div class="sa-analysis" style="color:#dc2626">选品分析超时，请稍后查看历史记录。</div>';
    } catch (e) {
      if (result) result.innerHTML = '<div class="sa-analysis" style="color:#dc2626">提交异常：网络错误。</div>';
    }
  }

  function _psFinalHtml(result) {
    if (!result || !result.data) return '<div class="sa-analysis">未获取到选品结果。</div>';
    const d = result.data;
    const products = d.products || [];
    let html = '<div class="ps-final-head">' +
      '<i class="fa-solid fa-clipboard-check" style="color:#16a34a"></i> 当日选品结果 · ' + esc(result.priceLabel || result.date || '') +
      '</div>';
    html += products.map(function (p) {
      const segs = (p.price_segments || []).map(function (s) {
        return '<span class="ps-seg">' + esc(s.range || '') + ' · 销量 ' + esc(s.sales || '--') + '</span>';
      }).join('');
      const variants = (p.variants || []).map(function (v) {
        return '<div class="ps-variant"><span class="ps-v-name">' + esc(v.name || '') + '</span>' +
          (v.summary ? '<span class="ps-v-sum">' + esc(v.summary) + '</span>' : '') + '</div>';
      }).join('');
      return '<div class="ps-big-card">' +
        '<div class="ps-big-head"><span class="ps-big-name">' + esc(p.name || '') + '</span><span class="ps-big-cat">' + esc(p.category || '') + '</span></div>' +
        (p.summary ? '<div class="ps-big-sum">' + esc(p.summary) + '</div>' : '') +
        (segs ? '<div class="ps-segs">' + segs + '</div>' : '') +
        (p.profit ? '<div class="ps-profit"><i class="fa-solid fa-coins" style="color:#f59e0b"></i> ' + esc(p.profit) + '</div>' : '') +
        (variants ? '<div class="ps-variants">' + variants + '</div>' : '') +
      '</div>';
    }).join('');
    if (d.advice) {
      html += '<div class="ps-advice"><div class="ps-advice-title"><i class="fa-solid fa-file-lines"></i> 当日选品建议</div>' + esc(d.advice) + '</div>';
    }
    return html;
  }

  function _psRenderFinal(result) {
    const el = document.getElementById('selAgentResult');
    if (!el) return;
    el.innerHTML = _psFinalHtml(result);
    el.scrollTop = 0;
  }

  // ---- 爱搜上升词 ----
  async function psRising() {
    if (_psBusy) return;
    _psBusy = true;
    const result = document.getElementById('selAgentResult');
    if (result) result.innerHTML = '<div class="sa-loading"><i class="fa-solid fa-spinner"></i> 正在分析爱搜上升词，请稍候...</div>';
    try {
      let data = null;
      if (state.apiAvailable) data = await ApiService.runRising();
      if (!result) return;
      if (!data || !(data.cards && data.cards.length)) {
        result.innerHTML = '<div class="sa-analysis">未分析出上升词，请先由智能体抓取爱搜数据。</div>';
        return;
      }
      let html = '<div class="ps-round-head"><span><i class="fa-solid fa-chart-line" style="color:#8b5cf6"></i> 近期热度上升的产品词（5 个）</span></div>';
      html += data.cards.map(function (c) {
        return '<div class="sa-card"><div class="sa-card-top">' +
          '<div class="sa-card-title">' + esc(c.word || c.name || '') + '</div>' +
          '<div class="sa-card-metric">' + esc(c.type || '') + '</div>' +
          '</div>' +
          (c.reason ? '<div class="sa-card-reason">' + esc(c.reason) + '</div>' : '') +
          '</div>';
      }).join('');
      result.innerHTML = html;
    } catch (e) {
      if (result) result.innerHTML = '<div class="sa-analysis" style="color:#dc2626">分析失败：网络异常。</div>';
    } finally {
      _psBusy = false;
    }
  }

  // ---- 历史选品记录 ----
  function psHistoryToggle() {
    const panel = document.getElementById('psHistoryPanel');
    if (!panel) return;
    if (panel.classList.contains('hidden')) {
      psLoadHistoryDates();
      panel.classList.remove('hidden');
    } else {
      panel.classList.add('hidden');
    }
  }

  function psCloseHistory() {
    const panel = document.getElementById('psHistoryPanel');
    if (panel) panel.classList.add('hidden');
  }

  async function psLoadHistoryDates() {
    const sel = document.getElementById('psHistoryDate');
    if (!sel) return;
    let data = null;
    if (state.apiAvailable) data = await ApiService.getHistoryDates();
    const dates = (data && data.dates) || [];
    if (dates.length) {
      sel.innerHTML = dates.map(function (d) {
        return '<option value="' + d + '">' + d + '</option>';
      }).join('');
      sel.value = dates[0];
      psLoadHistory(dates[0]);
    } else {
      sel.innerHTML = '<option value="" disabled selected>暂无记录</option>';
      const body = document.getElementById('psHistoryBody');
      if (body) body.innerHTML = '<div style="color:#94a3b8;padding:24px;text-align:center">暂无选品记录</div>';
    }
  }

  async function psLoadHistory(date) {
    const body = document.getElementById('psHistoryBody');
    if (!body) return;
    if (!date) return;
    let data = null;
    if (state.apiAvailable) data = await ApiService.getHistory(date);
    if (!data || !data.data) {
      body.innerHTML = '<div style="color:#94a3b8;padding:24px;text-align:center">该日期无选品记录</div>';
      return;
    }
    body.innerHTML = _psFinalHtml(data);
  }

  // ==================== 种草监测中台 ====================

  // 种草智能体（文案生成）
  var _sdAgentBusy = false;

  function sdAgentAsk(type) {
    // 快捷按钮：直接弹出信息填写卡片，并预选对应内容类型
    const typeMap = { body: '种草正文', comment: '评论区文案', video: '口播文案' };
    const label = typeMap[type] || '';
    document.querySelectorAll('input[name="sdInfoType"]').forEach(function (b) {
      b.checked = !!label && b.value === label;
    });
    const modal = document.getElementById('sdInfoModal');
    if (modal) modal.classList.remove('hidden');
  }

  function closeSdInfoModal() {
    const modal = document.getElementById('sdInfoModal');
    if (modal) modal.classList.add('hidden');
  }

  function sdInfoSubmit() {
    const g = function (id) { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
    const name = g('sdInfoName');
    if (!name) { showToast('请先填写产品名称', 'error'); return; }
    const types = [];
    document.querySelectorAll('input[name="sdInfoType"]:checked').forEach(function (b) { types.push(b.value); });
    if (!types.length) { showToast('请至少选择一种内容类型', 'error'); return; }

    const lines = ['产品名称：' + name];
    if (g('sdInfoCategory')) lines.push('产品品类：' + g('sdInfoCategory'));
    if (g('sdInfoSelling')) lines.push('核心卖点：' + g('sdInfoSelling'));
    if (g('sdInfoAudience')) lines.push('目标人群：' + g('sdInfoAudience'));
    if (g('sdInfoPlatform')) lines.push('投放平台：' + g('sdInfoPlatform'));
    if (g('sdInfoPrice')) lines.push('价格区间：' + g('sdInfoPrice'));
    if (g('sdInfoScene')) lines.push('使用场景/痛点：' + g('sdInfoScene'));
    lines.push('内容类型：' + types.join('、'));
    if (g('sdInfoStyle')) lines.push('风格偏好：' + g('sdInfoStyle'));
    if (g('sdInfoNote')) lines.push('补充说明：' + g('sdInfoNote'));

    closeSdInfoModal();
    _sdAgentRun(lines.join('\n'));
  }

  async function _sdAgentRun(question) {
    if (_sdAgentBusy) return;
    const btn = document.getElementById('sdAgentSendBtn');
    const result = document.getElementById('sdAgentResult');
    if (!result) return;

    _sdAgentBusy = true;
    if (btn) btn.disabled = true;
    result.innerHTML = '<div class="sa-loading" style="color:#16a34a"><i class="fa-solid fa-spinner"></i> 正在生成文案，请稍候...</div>';

    try {
      let data = null;
      if (state.apiAvailable) data = await ApiService.runSeedingAgent(question);
      renderSdAgentResult(data);
    } catch (e) {
      result.innerHTML = '<div class="sa-analysis" style="color:#dc2626">生成失败：网络异常，请稍后再试。</div>';
    } finally {
      _sdAgentBusy = false;
      if (btn) btn.disabled = false;
    }
  }

  function sdAgentSend() {
    const input = document.getElementById('sdAgentInput');
    const question = input ? input.value.trim() : '';
    if (!question) { showToast('请输入种草文案需求', 'error'); return; }
    if (input) input.value = '';
    _sdAgentRun(question);
  }

  function renderSdAgentResult(data) {
    const result = document.getElementById('sdAgentResult');
    if (!result) return;

    if (!data) {
      result.innerHTML = '<div class="sa-analysis" style="color:#dc2626">后端服务不可用，无法生成文案。</div>';
      return;
    }

    const analysis = (data.analysis && data.analysis.trim()) || '';
    const meta = data.meta || {};

    let html = '';
    if (analysis) {
      html += '<div class="sa-analysis">' + analysis.replace(/\n/g, '<br>') + '</div>';
    } else {
      html += '<div class="sa-analysis">智能体未返回有效结果，请稍后重试。</div>';
    }
    if (meta && meta.kb_count !== undefined) {
      html += '<div class="sa-meta">已上传文案库样本：' + (meta.kb_count || 0) + ' 条</div>';
    }
    result.innerHTML = html;
  }

  var _sdAccounts = [];
  var _sdWorks = [];
  var _sdDeleted = [];
  var _sdTab = 'works';
  var _sdAccountModalId = null;
  var _sdWorksSortKey = 'publishTime';
  var _sdWorksSortDir = -1;
  var _sdDeptFilter = '';
  var _sdPlatform = 'douyin';
  var _sdAccountPlatformFilter = '';

  function renderSeedingMonitor() {
    _sdLoadCookie();
    _sdLoadAccounts();
    _sdLoadWorks();
    _sdLoadMeta();
    sdSwitchTab(_sdTab);
  }

  function _sdLoadCookie() {
    var elD = document.getElementById('sdDouyinCookie');
    var elX = document.getElementById('sdXhsCookie');
    if (!state.apiAvailable) {
      if (elD) { elD.value = ''; elD.placeholder = '后端不可用，无法读取 Cookie'; }
      if (elX) { elX.value = ''; elX.placeholder = '后端不可用，无法读取 Cookie'; }
      return;
    }
    ApiService.getSeedingCookie('douyin').then(function(data) {
      if (elD && data && data.cookie) { elD.value = data.cookie; elD.title = data.cookie; }
    });
    ApiService.getSeedingCookie('xhs').then(function(data) {
      if (elX && data && data.cookie) { elX.value = data.cookie; elX.title = data.cookie; }
    });
  }

  function _sdLoadAccounts() {
    if (state.apiAvailable) {
      ApiService.getSeedingAccounts().then(function(data) {
        if (Array.isArray(data)) { _sdAccounts = data; sdRenderAccounts(); sdRenderWorks(); _sdUpdateHeader(); }
      });
    }
    sdRenderAccounts();
  }

  function _sdLoadWorks(platform) {
    var p = platform || _sdPlatform;
    if (!state.apiAvailable) {
      sdRenderWorks();
      return Promise.resolve();
    }
    var promise = ApiService.getSeedingWorks(p).then(function(data) {
      if (Array.isArray(data) && p === _sdPlatform) { _sdWorks = data; sdRenderWorks(); _sdUpdateHeader(); }
    });
    if (!platform) sdRenderWorks();
    return promise;
  }

  function _sdUpdateHeader() {
    var ac = document.getElementById('sdAccountCount');
    var wc = document.getElementById('sdWorksCount');
    if (ac) ac.textContent = _sdAccounts.length;
    if (wc) wc.textContent = _sdWorks.length;
  }

  function _sdLoadMeta(platform) {
    var p = platform || _sdPlatform;
    if (!state.apiAvailable) { _sdRenderUpdateTime(null); return Promise.resolve(); }
    return ApiService.getSeedingWorksMeta(p).then(function(meta) {
      if (meta && p === _sdPlatform) _sdRenderUpdateTime(meta.mtime);
    });
  }

  function _sdRenderUpdateTime(mtime) {
    var el = document.getElementById('sdUpdateTime');
    if (!el) return;
    if (!mtime) { el.textContent = '--'; return; }
    var d = new Date(mtime * 1000);
    el.textContent = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') + ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  function sdSwitchTab(tab) {
    _sdTab = tab;
    document.querySelectorAll('#page-seeding-monitor .sd-tab').forEach(function(t) {
      t.classList.toggle('active', t.dataset.tab === tab);
    });
    document.querySelectorAll('#page-seeding-monitor .sd-panel').forEach(function(p) {
      p.classList.toggle('active', p.dataset.tab === tab);
    });
  }

  function sdToggleUpdatePanel() {
    var panel = document.getElementById('sdUpdatePanel');
    if (panel) panel.classList.toggle('hidden');
  }

  function toggleSdDatePicker(e) { _calToggle('sd', e); }
  function sdCalPick(y, m, d) { _calPick('sd', y, m, d); }
  function sdCalNav(delta) { _calNav('sd', delta); }
  function sdCalClear() { _calClear('sd'); }
  function sdCalToday() { _calToday('sd'); }

  function sdSortWorks(key) {
    if (_sdWorksSortKey === key) {
      _sdWorksSortDir = -_sdWorksSortDir;
    } else {
      _sdWorksSortKey = key;
      _sdWorksSortDir = -1;
    }
    sdRenderWorks();
  }

  function sdFilterDept(dept) {
    _sdDeptFilter = dept || '';
    document.querySelectorAll('#page-seeding-monitor .sd-dept-btn').forEach(function(b) {
      b.classList.toggle('active', (b.dataset.dept || '') === _sdDeptFilter);
    });
    sdRenderWorks();
    sdRenderDeleted();
  }

  function sdSwitchPlatform(platform) {
    _sdPlatform = platform || 'douyin';
    document.querySelectorAll('#page-seeding-monitor .sd-plat-btn').forEach(function(b) {
      b.classList.toggle('active', b.dataset.plat === _sdPlatform);
    });
    var worksCard = document.getElementById('sdWorksCard');
    var deletedCard = document.getElementById('sdDeletedCard');
    if (worksCard) worksCard.style.display = (_sdPlatform === 'deleted') ? 'none' : '';
    if (deletedCard) deletedCard.style.display = (_sdPlatform === 'deleted') ? '' : 'none';
    if (_sdPlatform === 'deleted') {
      _sdLoadDeleted();
    } else {
      _sdLoadCookie();
      _sdLoadWorks();
      _sdLoadMeta();
    }
  }

  function sdFilterAccountPlatform(platform) {
    _sdAccountPlatformFilter = platform || '';
    sdRenderAccounts();
  }

  function sdAccPlatformChange() {
    var plat = (document.getElementById('sdAccPlatform') || {}).value || 'douyin';
    var df = document.getElementById('sdAccDouyinField');
    var rf = document.getElementById('sdAccRedField');
    var hf = document.getElementById('sdAccHomepageField');
    if (df) df.style.display = (plat === 'douyin' ? '' : 'none');
    if (rf) rf.style.display = (plat === 'xhs' ? '' : 'none');
    if (hf) hf.style.display = (plat === 'douyin' ? '' : 'none');
  }

  function sdRenderAccounts() {
    var el = document.getElementById('sdAccountSearch');
    var kw = (el && el.value || '').toLowerCase();
    var list = _sdAccounts.slice();
    if (kw) list = list.filter(function(a) {
      return (a.name || '').toLowerCase().indexOf(kw) >= 0 ||
             (a.douyinId || '').toLowerCase().indexOf(kw) >= 0 ||
             (a.redId || '').toLowerCase().indexOf(kw) >= 0 ||
             (a.homepage || '').toLowerCase().indexOf(kw) >= 0 ||
             (a.department || '').toLowerCase().indexOf(kw) >= 0;
    });
    if (_sdAccountPlatformFilter) list = list.filter(function(a) {
      return (a.platform || 'douyin') === _sdAccountPlatformFilter;
    });
    var info = document.getElementById('sdAccountInfo');
    if (info) info.textContent = '共 ' + list.length + ' 个种草账号';
    var tbody = document.getElementById('sdAccountTbody');
    if (!tbody) return;
    tbody.innerHTML = list.map(function(a) {
      var plat = a.platform || 'douyin';
      var platBadge = plat === 'xhs'
        ? '<span style="font-size:11px;color:#e11d48;font-weight:600">小红书</span>'
        : '<span style="font-size:11px;color:#0284c7;font-weight:600">抖音</span>';
      var idField = plat === 'xhs' ? (a.redId || '-') : (a.douyinId || '-');
      var hp = plat === 'xhs'
        ? '<span style="color:#94a3b8">-</span>'
        : (a.homepage
            ? '<a href="' + esc(a.homepage) + '" target="_blank" rel="noopener" style="color:#2563eb;text-decoration:none">' + esc(a.homepage) + '</a>'
            : '<span style="color:#94a3b8">-</span>');
      return '<tr><td>' + a.id + '</td><td>' + platBadge + '</td><td><strong>' + esc(a.name || '') + '</strong></td>' +
        '<td style="font-family:monospace;font-size:12px">' + esc(idField) + '</td>' +
        '<td>' + esc(a.department || '-') + '</td>' +
        '<td style="font-size:12px;word-break:break-all">' + hp + '</td>' +
        '<td><div class="ap-actions">' +
        '<button class="ap-btn-sm edit" onclick="App.openSdAccountModal(' + a.id + ')"><i class="fa-solid fa-pen"></i></button>' +
        '<button class="ap-btn-sm delete" onclick="App.deleteSdAccount(' + a.id + ')"><i class="fa-solid fa-trash"></i></button>' +
        '</div></td></tr>';
    }).join('');
  }

  function sdTitleCell(w) {
    var t = esc(w.title || '');
    var link = (w.link || w.url || '').toString().trim();
    if (!link) return t;
    return '<a href="' + esc(link) + '" target="_blank" rel="noopener noreferrer" ' +
      'style="color:#1677ff;text-decoration:none" title="点击跳转作品：' + t + '">' + t + '</a>';
  }

  function _sdDeptMap() {
    var m = {};
    _sdAccounts.forEach(function(a) {
      if (a.douyinId) m[a.douyinId] = a.department || '';
      if (a.redId) m[a.redId] = a.department || '';
      if (a.name) m[a.name] = a.department || '';
    });
    return m;
  }

  function sdRenderWorks() {
    var searchEl = document.getElementById('sdWorksSearch');
    var kw = (searchEl && searchEl.value || '').toLowerCase();
    var list = _sdWorks.slice();

    // 关键词筛选
    if (kw) list = list.filter(function(w) {
      return (w.title || '').toLowerCase().indexOf(kw) >= 0 ||
             (w.name || '').toLowerCase().indexOf(kw) >= 0 ||
             (w.account || '').toLowerCase().indexOf(kw) >= 0;
    });

    // 部门筛选（按账号所属部门过滤作品）
    if (_sdDeptFilter) {
      var deptMap = _sdDeptMap();
      list = list.filter(function(w) {
        return (deptMap[w.account] || deptMap[w.name] || '') === _sdDeptFilter;
      });
    }

    // 时间筛选
    var ds = document.getElementById('sdDateStart');
    var de = document.getElementById('sdDateEnd');
    var dateStart = (ds && ds.value || '').trim();
    var dateEnd = (de && de.value || '').trim();
    if (dateStart || dateEnd) {
      list = list.filter(function(w) {
        var d = (w.publishTime || '').slice(0, 10);
        if (dateStart && d < dateStart) return false;
        if (dateEnd && d > dateEnd) return false;
        return true;
      });
    }

    // 排序（默认按发布时间倒序；点赞/评论/收藏/分享可切换）
    if (_sdWorksSortKey) {
      list.sort(function(a, b) {
        var av = a[_sdWorksSortKey];
        var bv = b[_sdWorksSortKey];
        var r;
        if (typeof av === 'number' && typeof bv === 'number') {
          r = av - bv;
        } else {
          r = String(av == null ? '' : av).localeCompare(String(bv == null ? '' : bv));
        }
        return r * _sdWorksSortDir;
      });
    }

    // 更新排序箭头
    ['likes', 'comments', 'collects', 'shares'].forEach(function(k) {
      var arrow = document.getElementById('sdSortArrow-' + k);
      if (arrow) arrow.textContent = (_sdWorksSortKey === k) ? (_sdWorksSortDir === -1 ? '▼' : '▲') : '';
    });

    var badge = document.getElementById('sdWorksBadge');
    if (badge) badge.textContent = '共 ' + list.length + ' 条';
    var tbody = document.getElementById('sdWorksTbody');
    if (!tbody) return;
    tbody.innerHTML = list.map(function(w) {
      return '<tr><td><strong>' + esc(w.name || '') + '</strong></td>' +
        '<td style="font-family:monospace;font-size:12px">' + esc(w.account || '-') + '</td>' +
        '<td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + sdTitleCell(w) + '</td>' +
        '<td class="ps-col-num" style="color:#dc2626;font-weight:600">' + (w.likes || 0).toLocaleString() + '</td>' +
        '<td class="ps-col-num">' + (w.comments || 0).toLocaleString() + '</td>' +
        '<td class="ps-col-num">' + (w.collects || 0).toLocaleString() + '</td>' +
        '<td class="ps-col-num">' + (w.shares || 0).toLocaleString() + '</td>' +
        '<td style="font-size:12px;color:#64748b">' + esc(w.publishTime || '-') + '</td></tr>';
    }).join('');
  }

  function _sdLoadDeleted() {
    if (!state.apiAvailable) { sdRenderDeleted(); return Promise.resolve(); }
    return ApiService.getSeedingDeleted().then(function(data) {
      if (Array.isArray(data)) { _sdDeleted = data; sdRenderDeleted(); }
    });
  }

  function sdRenderDeleted() {
    var badge = document.getElementById('sdDeletedBadge');
    var list = _sdDeleted.slice();
    if (_sdDeptFilter) {
      var deptMap = _sdDeptMap();
      list = list.filter(function(d) {
        return (deptMap[d.account] || deptMap[d.name] || '') === _sdDeptFilter;
      });
    }
    if (badge) badge.textContent = '共 ' + list.length + ' 条';
    var tbody = document.getElementById('sdDeletedTbody');
    if (!tbody) return;
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#94a3b8;padding:24px">暂无被删除的作品记录</td></tr>';
      return;
    }
    tbody.innerHTML = list.map(function(d) {
      var plat = d.platform === 'xhs'
        ? '<span style="font-size:11px;color:#e11d48;font-weight:600">小红书</span>'
        : '<span style="font-size:11px;color:#0284c7;font-weight:600">抖音</span>';
      return '<tr><td>' + plat + '</td>' +
        '<td><strong>' + esc(d.name || '') + '</strong></td>' +
        '<td style="font-family:monospace;font-size:12px">' + esc(d.account || '-') + '</td>' +
        '<td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + sdTitleCell(d) + '</td>' +
        '<td style="font-size:12px;color:#64748b">' + esc(d.publishTime || '-') + '</td>' +
        '<td style="font-size:12px;color:#dc2626">' + esc(d.deletedAt || '-') + '</td>' +
        '<td><button class="ap-btn-sm delete" onclick="App.sdDeleteDeleted(' + d.id + ')"><i class="fa-solid fa-trash"></i></button></td></tr>';
    }).join('');
  }

  function sdDeleteDeleted(id) {
    document.getElementById('confirmMsg').textContent = '确定清除这条被删作品记录吗？';
    document.getElementById('confirmDeleteBtn').onclick = async function() {
      if (state.apiAvailable) await ApiService.deleteSeedingDeleted(id);
      _sdDeleted = _sdDeleted.filter(function(d) { return d.id !== id; });
      document.getElementById('confirmModal').classList.add('hidden');
      showToast('记录已清除');
      sdRenderDeleted();
    };
    document.getElementById('confirmModal').classList.remove('hidden');
  }

  function sdClearDeleted() {
    document.getElementById('confirmMsg').textContent = '确定清空全部被删作品记录吗？此操作不可恢复。';
    document.getElementById('confirmDeleteBtn').onclick = async function() {
      if (state.apiAvailable) await ApiService.clearSeedingDeleted();
      _sdDeleted = [];
      document.getElementById('confirmModal').classList.add('hidden');
      showToast('已清空全部被删作品记录');
      sdRenderDeleted();
    };
    document.getElementById('confirmModal').classList.remove('hidden');
  }

  function openSdAccountModal(id) {
    _sdAccountModalId = id || null;
    var acc = id ? _sdAccounts.find(function(a) { return a.id === id; }) : null;
    var plat = acc ? (acc.platform || 'douyin') : 'douyin';
    document.getElementById('modalTitle').textContent = acc ? '编辑种草账号' : '新增种草账号';
    document.getElementById('modalBody').innerHTML =
      '<div class="ap-form-group"><label>平台</label><select class="ap-form-input" id="sdAccPlatform" onchange="App.sdAccPlatformChange()">' +
        '<option value="douyin"' + (plat === 'douyin' ? ' selected' : '') + '>抖音</option>' +
        '<option value="xhs"' + (plat === 'xhs' ? ' selected' : '') + '>小红书</option>' +
      '</select></div>' +
      '<div class="ap-form-group"><label>账号名称</label><input class="ap-form-input" id="sdAccName" autocomplete="off" value="' + esc(acc ? acc.name || '' : '') + '" placeholder="例如：聚浪好物研究所"></div>' +
      '<div id="sdAccDouyinField"><div class="ap-form-group"><label>抖音号</label><input class="ap-form-input" id="sdAccDouyin" autocomplete="off" value="' + esc(acc ? acc.douyinId || '' : '') + '" placeholder="抖音号（如 julang_haowu）"></div></div>' +
      '<div id="sdAccRedField"><div class="ap-form-group"><label>小红书号</label><input class="ap-form-input" id="sdAccRed" autocomplete="off" value="' + esc(acc ? acc.redId || '' : '') + '" placeholder="小红书号（如 18930360363）"></div></div>' +
      '<div class="ap-form-group"><label>部门</label><input class="ap-form-input" id="sdAccDept" autocomplete="off" value="' + esc(acc ? acc.department || '' : '') + '" placeholder="例如：三部 / 四部 / 五部"></div>' +
      '<div id="sdAccHomepageField"><div class="ap-form-group"><label>主页链接</label><input class="ap-form-input" id="sdAccHomepage" autocomplete="off" value="' + esc(acc ? acc.homepage || '' : '') + '" placeholder="https://www.douyin.com/user/MS4wLjAB..."></div>' +
      '<div style="font-size:11px;color:#94a3b8">抖音需填写主页链接（自动解析 sec_user_id）；小红书无需主页链接，抓取时按小红书号解析。</div></div>';
    document.getElementById('modalSaveBtn').onclick = saveSdAccount;
    document.getElementById('formModal').classList.remove('hidden');
    sdAccPlatformChange();
  }

  async function saveSdAccount() {
    var platform = (document.getElementById('sdAccPlatform').value || 'douyin').trim();
    var name = (document.getElementById('sdAccName').value || '').trim();
    var douyinId = (document.getElementById('sdAccDouyin').value || '').trim();
    var redId = (document.getElementById('sdAccRed').value || '').trim();
    var dept = (document.getElementById('sdAccDept').value || '').trim();
    var homepage = (document.getElementById('sdAccHomepage').value || '').trim();
    if (!name) { showToast('请输入账号名称', 'error'); return; }
    if (platform === 'xhs') {
      if (!redId) { showToast('请输入小红书号', 'error'); return; }
      homepage = '';
    } else {
      if (!homepage) { showToast('请输入主页链接', 'error'); return; }
      redId = '';
    }

    var payload = { platform: platform, name: name, douyinId: douyinId, redId: redId, homepage: homepage, department: dept };
    var saved = null;

    if (state.apiAvailable) {
      if (_sdAccountModalId) {
        saved = await ApiService.updateSeedingAccount(_sdAccountModalId, payload);
      } else {
        saved = await ApiService.createSeedingAccount(payload);
      }
    }

    // 立即更新本地列表并渲染
    if (saved) {
      if (_sdAccountModalId) {
        var idx = _sdAccounts.findIndex(function(a) { return a.id === _sdAccountModalId; });
        if (idx >= 0) { _sdAccounts[idx] = saved; } else { _sdAccounts.push(saved); }
      } else {
        _sdAccounts.push(saved);
      }
    } else if (!state.apiAvailable) {
      if (_sdAccountModalId) {
        var t = _sdAccounts.find(function(a) { return a.id === _sdAccountModalId; });
        if (t) { t.platform = platform; t.name = name; t.douyinId = douyinId; t.redId = redId; t.homepage = homepage; t.department = dept; }
      } else {
        var newId = _sdAccounts.length ? Math.max.apply(null, _sdAccounts.map(function(a) { return a.id; })) + 1 : 1;
        _sdAccounts.push({ id: newId, platform: platform, name: name, douyinId: douyinId, redId: redId, homepage: homepage, department: dept });
      }
    }

    document.getElementById('formModal').classList.add('hidden');
    showToast(_sdAccountModalId ? '种草账号已更新' : '种草账号已添加');
    sdRenderAccounts();
    _sdUpdateHeader();
    _sdLoadAccounts();
  }

  function deleteSdAccount(id) {
    var acc = _sdAccounts.find(function(a) { return a.id === id; });
    if (!acc) return;
    document.getElementById('confirmMsg').textContent = '确定删除种草账号「' + (acc.name || '') + '」吗？';
    document.getElementById('confirmDeleteBtn').onclick = async function() {
      if (state.apiAvailable) {
        await ApiService.deleteSeedingAccount(id);
      }
      // 立即从本地列表移除并渲染
      _sdAccounts = _sdAccounts.filter(function(a) { return a.id !== id; });
      document.getElementById('confirmModal').classList.add('hidden');
      showToast('种草账号已删除');
      sdRenderAccounts();
      _sdUpdateHeader();
      _sdLoadAccounts();
    };
    document.getElementById('confirmModal').classList.remove('hidden');
  }

  async function sdSaveCookie(platform) {
    platform = platform || _sdPlatform;
    var el = platform === 'xhs' ? document.getElementById('sdXhsCookie') : document.getElementById('sdDouyinCookie');
    var cookie = (el && el.value || '').trim();
    if (!cookie) { showToast('请输入 Cookie', 'error'); return; }
    if (!state.apiAvailable) { showToast('后端不可用，无法保存 Cookie', 'error'); return; }
    var res = await ApiService.saveSeedingCookie(platform, cookie);
    showToast(res !== null ? 'Cookie 已保存' : 'Cookie 保存失败', res !== null ? 'success' : 'error');
  }

  async function sdTriggerScrape(platform) {
    platform = platform || _sdPlatform;
    if (!state.apiAvailable) { showToast('后端不可用，无法触发抓取', 'error'); return; }
    var res = await ApiService.triggerSeedingScrape(platform);
    if (res === null) { showToast('触发抓取失败', 'error'); return; }
    var beforeMtime = res.mtime || 0;
    showToast('已触发' + (platform === 'xhs' ? '小红书' : '抖音') + '抓取，正在后台执行…');
    _sdPollScrape(platform, beforeMtime);
  }

  function _sdShowProgress(pct, text) {
    var wrap = document.getElementById('sdProgressWrap');
    var fill = document.getElementById('sdProgressFill');
    var txt = document.getElementById('sdProgressText');
    var pctEl = document.getElementById('sdProgressPercent');
    if (wrap) wrap.style.display = '';
    if (fill) fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
    if (txt && text) txt.textContent = text;
    if (pctEl) pctEl.textContent = Math.max(0, Math.min(100, pct)) + '%';
  }

  function _sdHideProgress() {
    var wrap = document.getElementById('sdProgressWrap');
    if (wrap) wrap.style.display = 'none';
  }

  function _sdPollScrape(platform, beforeMtime) {
    _sdShowProgress(2, '抓取中...');
    var tries = 0;
    var maxTries = 160; // 160 * 3s = 8min，覆盖多账号慢抓
    var timer = setInterval(async function() {
      tries++;
      var st = await ApiService.getSeedingScrapeStatus(platform);
      if (st && st.status === 'error') {
        clearInterval(timer);
        _sdHideProgress();
        await _sdLoadWorks(platform);
        _sdLoadDeleted();
        var emsg = (st.msg && String(st.msg).trim()) || '抓取失败，请检查 Cookie 是否有效';
        showToast(emsg, 'error');
        return;
      }
      if (st && st.status === 'running') {
        var done = st.done || 0, total = st.total || 0;
        _sdShowProgress(st.progress || 0, total ? ('抓取中 ' + done + '/' + total + ' 个账号') : '抓取中...');
      }
      var meta = await ApiService.getSeedingWorksMeta(platform);
      if (meta && meta.mtime && (!beforeMtime || meta.mtime > beforeMtime)) {
        clearInterval(timer);
        await _sdLoadWorks(platform);
        _sdLoadDeleted();
        _sdLoadMeta(platform);
        _sdShowProgress(100, '抓取完成');
        showToast('抓取完成，作品数据已更新');
        setTimeout(_sdHideProgress, 3000);
        return;
      }
      if (tries >= maxTries) {
        clearInterval(timer);
        _sdHideProgress();
        await _sdLoadWorks(platform);
        _sdLoadDeleted();
        showToast('抓取超时未完成，请稍后手动点击「数据更新」查看', 'error');
      }
    }, 3000);
  }

  return {
    init, initApp, navigateTo,
    openFormModal, closeFormModal,
    confirmDelete, closeConfirmModal,
    goToPage, filterTable,
    showToast, toggleSidebar,
    logout: handleLogout,
    // ★ 账号列表权限判定
    //   isAccountManager = 超级层（全库）；isCanManageRoles = 角色与权限 Tab
    isAccountManager: function () { return _CAN_MANAGE_ACCOUNTS; },
    isCanManageRoles: function () { return _CAN_MANAGE_ROLES; },
    isDeptLead: function () { return _IS_DEPT_LEAD; },
    /** 后端 my-scope 拉取结果（页面挂载时取一次；失败返回 null，前端退化为「非主管」） */
    fetchMyScope: function () { return _fetchMyScope(); },
    getMyScope: function () { return _MY_SCOPE; },
    // Admin & Permissions
    openAdminModal, editAdmin, deleteAdmin, toggleAdmin, togglePwdVis, openPwdModal,
    openRolePermModal, deleteRole, apToggleAllPerms,
    filterAdminTable, switchAdminTab,
    // Marketing
    renderMarketingOverview, setDateRange, toggleMktDatePicker, mktCalPick, mktCalNav, mktCalClear, mktCalToday,
    renderCategoryMarketing, setCatRange, setCatCustomDate, showCatKeywords, hideCatKeywords,
    // Platform & Store
    renderPlatformStore, toggleStoreDetail, closePsDetail, psGoPage, filterByPlatform, togglePsDatePicker, psCalPick, psCalNav, psCalClear, psCalToday,
    generateDailyReport, renderDailyAnalysis, downloadReport,
    daAgentAsk, daAgentSend,
    // Seeding Monitor
    renderSeedingMonitor, sdSwitchTab, sdToggleUpdatePanel, sdSortWorks, sdFilterDept, sdSwitchPlatform, sdFilterAccountPlatform, sdAccPlatformChange, toggleSdDatePicker, sdCalPick, sdCalNav, sdCalClear, sdCalToday, sdRenderAccounts, sdRenderWorks, openSdAccountModal, saveSdAccount, deleteSdAccount, sdSaveCookie, sdTriggerScrape,
    sdDeleteDeleted, sdClearDeleted,
    sdAgentAsk, sdAgentSend, closeSdInfoModal, sdInfoSubmit,
    // Product Selection
    renderProductSelection, switchSelectionTab, filterTmall, resetTmall, tmallGoPage, filterDouyin,
    filterAisou, resetAisou,
    loadTmallMarket, triggerTmallMarketScrape,
    loadDouyinMarket, triggerDouyinMarketScrape,
    load1688Market, trigger1688MarketScrape,
    m1688ToggleCookiePanel, m1688SaveCookie,
    tmToggleCookiePanel, tmSaveCookie,
    // Profile
    renderProfile, saveProfile, resetProfile, changePassword,
    // Toolbox - Violation Word Detection (OCR)
    renderToolboxViolationCheck, vdHandleFiles, vdHandleDrop, vdRemoveImage,
    vdStartOCR, vdClearAll, vdOpenAddWordModal, vdDeleteWord,
    // AI Assistant
    aiToggleChat, aiSendMessage,
    // Order Details
    renderOrderDetails, _odGoPage, _odSetDateRange, toggleOdColPanel, toggleOdCol, toggleOdDatePicker, odCalPick, odCalNav, odCalClear, odCalToday,
  };
})();

// Boot
document.addEventListener('DOMContentLoaded', () => App.init());

// ==================== 前端未捕获异常上报 ====================
// 目的：Vue 渲染函数一抛错就是整页空白，以前只能等用户反馈；现在直接钉钉告警开发（李自豪）。
// 约束：同一条错误每会话只报一次、最多 10 条、失败静默——上报本身绝不能影响页面。
(function () {
  if (typeof window === 'undefined' || window.__ecomErrReporterInstalled) return;
  window.__ecomErrReporterInstalled = true;
  var seen = {};
  var sent = 0;

  function send(message, stack) {
    try {
      if (!message || sent >= 10) return;
      var key = String(message).slice(0, 120);
      if (seen[key]) return;
      seen[key] = true;
      sent++;
      var api = (typeof ApiService !== 'undefined') ? ApiService : null;
      if (!api || !api.reportClientError) return;
      api.reportClientError({
        message: String(message).slice(0, 500),
        stack: String(stack || '').slice(0, 1500),
        page: (window.location && window.location.pathname) || '',
        location: (window.location && window.location.href) || '',
      });
    } catch (e) { /* 上报失败就算了，绝不能因为上报再引发错误 */ }
  }

  window.addEventListener('error', function (e) {
    try {
      if (!e) return;
      // 资源加载失败（img/script 的 error 事件没有 message、target 是元素）不算 JS 异常
      if (e.target && e.target.tagName) return;
      send(e.message, e.error && e.error.stack);
    } catch (err) { /* ignore */ }
  });

  window.addEventListener('unhandledrejection', function (e) {
    try {
      var r = e && e.reason;
      send('Promise 未捕获：' + ((r && (r.message || r)) || 'unknown'), r && r.stack);
    } catch (err) { /* ignore */ }
  });
})();
