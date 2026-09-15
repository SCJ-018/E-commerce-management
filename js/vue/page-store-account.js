/**
 * 店铺账号管理 — Vue 版（阶段 4，最后 3 个假数据页之一）
 * classic script，与 app.js 共用全局词法作用域；纯静态假数据（复刻 app.js getMockStoreAccounts，无真实接口）
 * XSS：全部走 {{ }} 插值自动转义，绝不用 v-html
 * 结构：页头（标题 + 启用/过期/待授权统计 chips）+ 工具栏（搜索/平台筛选/状态筛选/新增按钮）+ 账号表格 + 底部计数
 */
(function () {
  'use strict';

  // ---- 假数据生成（复刻 app.js getMockStoreAccounts） ----
  function _genData() {
    var platforms = ['淘宝', '京东', '拼多多', '抖音', '快手', '小红书', '微信小程序'];
    var authTypes = ['OAuth2.0', 'API Key', '账号密码', '扫码授权'];
    var stores = [
      { name: '聚浪旗舰店', plat: '淘宝' }, { name: '聚浪户外专营店', plat: '淘宝' }, { name: '聚浪运动旗舰店', plat: '京东' },
      { name: '聚浪自营店', plat: '京东' }, { name: '聚浪优选', plat: '拼多多' }, { name: '聚浪官方旗舰店', plat: '抖音' },
      { name: '聚浪好物店', plat: '抖音' }, { name: '聚浪快品牌', plat: '快手' }, { name: '聚浪精选', plat: '小红书' },
      { name: '聚浪小程序商城', plat: '微信小程序' }, { name: '聚浪奥莱折扣店', plat: '拼多多' }, { name: '聚浪潮品店', plat: '淘宝' }
    ];
    var now = new Date();
    return stores.map(function (s, i) {
      var statuses = ['enabled', 'enabled', 'enabled', 'enabled', 'enabled', 'expired', 'pending'];
      var status = statuses[Math.floor(Math.random() * statuses.length)];
      var expDate = new Date(now); expDate.setDate(expDate.getDate() + Math.floor(Math.random() * 180 - 30));
      var syncDate = new Date(now); syncDate.setHours(syncDate.getHours() - Math.floor(Math.random() * 48));
      return {
        id: i + 1, name: s.name, platform: s.plat,
        account: 'acct_' + s.plat.toLowerCase() + '_' + (1000 + i),
        authType: authTypes[Math.floor(Math.random() * authTypes.length)],
        expireDate: expDate.toISOString().slice(0, 10),
        status: status,
        lastSync: syncDate.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      };
    });
  }

  // ---- 模块级状态（跨挂载/卸载保留，搜索与筛选条件持久化） ----
  var _sa = Vue.reactive({
    list: [], search: '', platform: '', status: '',
  });

  var StoreAccountPage = {
    setup() {
      var filtered = Vue.computed(function () {
        var kw = _sa.search.trim().toLowerCase();
        var plat = _sa.platform, st = _sa.status;
        return _sa.list.filter(function (a) {
          if (kw && a.name.toLowerCase().indexOf(kw) < 0 && a.account.toLowerCase().indexOf(kw) < 0 && a.platform.toLowerCase().indexOf(kw) < 0) return false;
          if (plat && a.platform !== plat) return false;
          if (st && a.status !== st) return false;
          return true;
        });
      });
      var counts = Vue.computed(function () {
        var en = 0, ex = 0, pe = 0;
        _sa.list.forEach(function (a) {
          if (a.status === 'enabled') en++;
          else if (a.status === 'expired') ex++;
          else pe++;
        });
        return { total: _sa.list.length, enabled: en, expired: ex, pending: pe };
      });

      function statusClass(a) {
        return a.status === 'enabled' ? 'status-badge enabled' : (a.status === 'expired' ? 'status-badge disabled' : 'badge badge-warning');
      }
      function statusText(a) {
        return a.status === 'enabled' ? '启用中' : (a.status === 'expired' ? '已过期' : '待授权');
      }
      function platformClass(p) { return 'ps-store-platform ps-platform-' + p; }
      function isExpired(a) { return a.status === 'expired'; }
      function todo(msg) { App.showToast(msg, 'error'); }
      function addAccount() { App.showToast('新增店铺账号功能开发中', 'error'); }

      return {
        sa: _sa, filtered: filtered, counts: counts,
        statusClass: statusClass, statusText: statusText, platformClass: platformClass,
        isExpired: isExpired, todo: todo, addAccount: addAccount,
      };
    },

    template: `
<div>
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon" style="background:linear-gradient(135deg,#6366f1,#8b5cf6);box-shadow:0 2px 8px rgba(99,102,241,0.25)"><i class="fa-solid fa-id-card" style="color:#fff;font-size:18px"></i></div>
      <div class="dh-title-group"><h2 class="dh-title">店铺账号管理</h2><span class="dh-subtitle">Store Account Management</span><span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>共 {{ counts.total }} 个店铺账号</span></div>
    </div>
    <div class="dh-chips">
      <div class="dh-chip"><span class="dh-chip-label">启用中</span><span class="dh-chip-value" style="color:#16a34a">{{ counts.enabled }}</span></div>
      <div class="dh-chip"><span class="dh-chip-label">已过期</span><span class="dh-chip-value" style="color:#dc2626">{{ counts.expired }}</span></div>
      <div class="dh-chip"><span class="dh-chip-label">待授权</span><span class="dh-chip-value" style="color:#f59e0b">{{ counts.pending }}</span></div>
    </div>
  </div>
  <div class="ap-toolbar">
    <div class="ap-search-wrap"><i class="fa-solid fa-search"></i><input class="ap-search-input" v-model="sa.search" placeholder="搜索店铺名称/账号/平台..."></div>
    <div style="display:flex;gap:8px;align-items:center">
      <select v-model="sa.platform" class="dh-select"><option value="">全部平台</option><option>淘宝</option><option>京东</option><option>拼多多</option><option>抖音</option><option>快手</option><option>小红书</option><option>微信小程序</option></select>
      <select v-model="sa.status" class="dh-select"><option value="">全部状态</option><option value="enabled">启用中</option><option value="expired">已过期</option><option value="pending">待授权</option></select>
      <button class="ap-btn-primary" @click="addAccount"><i class="fa-solid fa-plus"></i> 新增店铺账号</button>
    </div>
  </div>
  <div class="ap-table-wrap">
    <table class="ap-table">
      <thead><tr>
        <th style="width:50px">ID</th><th style="width:160px">店铺名称</th><th style="width:70px">平台</th>
        <th style="width:110px">账号</th><th style="width:90px">授权方式</th>
        <th style="width:100px">授权到期</th><th style="width:80px">状态</th>
        <th style="width:130px">最后同步</th><th style="width:160px">操作</th>
      </tr></thead>
      <tbody>
        <tr v-for="a in filtered" :key="a.id">
          <td>{{ a.id }}</td>
          <td><strong>{{ a.name }}</strong></td>
          <td><span :class="platformClass(a.platform)">{{ a.platform }}</span></td>
          <td style="font-family:monospace;font-size:12px">{{ a.account }}</td>
          <td>{{ a.authType }}</td>
          <td><span v-if="isExpired(a)" style="color:#dc2626">{{ a.expireDate }}</span><template v-else>{{ a.expireDate }}</template></td>
          <td><span :class="statusClass(a)">{{ statusText(a) }}</span></td>
          <td>{{ a.lastSync }}</td>
          <td>
            <div class="ap-actions">
              <button class="ap-btn-sm edit" @click="todo('编辑功能开发中')"><i class="fa-solid fa-pen"></i></button>
              <button class="ap-btn-sm toggle" @click="todo('重新授权功能开发中')"><i class="fa-solid fa-rotate"></i></button>
              <button class="ap-btn-sm delete" @click="todo('删除功能开发中')"><i class="fa-solid fa-trash"></i></button>
            </div>
          </td>
        </tr>
        <tr v-if="!filtered.length"><td colspan="9" style="text-align:center;padding:32px;color:#94a3b8">未找到匹配的店铺账号</td></tr>
      </tbody>
    </table>
  </div>
  <div class="ap-table-info">共 {{ filtered.length }} 个店铺账号</div>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _saApp = null;

  function mountStoreAccountVue() {
    if (_saApp) return;
    var mount = document.getElementById('page-store-account');
    if (!mount) return;
    _sa.list = _genData(); // 每次进入页面重新生成假数据（对齐旧版每次导航重新渲染）
    _saApp = Vue.createApp(StoreAccountPage);
    _saApp.mount(mount);
  }

  function unmountStoreAccountVue() {
    if (!_saApp) return;
    _saApp.unmount();
    _saApp = null;
    var mount = document.getElementById('page-store-account');
    if (mount) mount.innerHTML = '';
  }

  function initHook() {
    var container = document.getElementById('page-store-account');
    if (!container) return;
    var isVisible = !container.classList.contains('hidden');
    if (isVisible) { mountStoreAccountVue(); return; }
    var observer = new MutationObserver(function () {
      var nowVisible = !container.classList.contains('hidden');
      if (nowVisible && !_saApp) mountStoreAccountVue();
      else if (!nowVisible && _saApp) unmountStoreAccountVue();
    });
    observer.observe(container, { attributes: true, attributeFilter: ['class'] });
  }

  initHook();
})();
