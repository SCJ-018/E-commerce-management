/**
 * 人事数据中心（人事中心板块）
 * 单页面 + 卡片选择器切换 5 张人事数据表；每张表一个独立页面组件（HR.makePage 生成）。
 * 权限：每张数据表对应一个独立权限点（hr-roster / hr-interview / hr-onboarding / hr-salary-a / hr-salary-b），
 *       由「管理员与权限」模块按需勾选，未勾选的表不会出现在卡片选择器中。
 * 旧入口：page-hr 仍为唯一导航页（侧边栏「人事中心 > 人事数据中心」）。
 */
(function () {
  'use strict';
  if (typeof Vue === 'undefined' || typeof HR === 'undefined' || typeof window.HR_TABLES === 'undefined') return;

  // ==================== 权限 ====================
  function storedPerms() {
    var raw = sessionStorage.getItem('admin_permissions');
    if (raw === null || raw === undefined) return null;   // 未配置 → 不做限制（超管/兼容）
    try {
      var p = JSON.parse(raw);
      return Array.isArray(p) ? p : null;
    } catch (e) { return null; }
  }

  function hasPerm(perm) {
    var p = storedPerms();
    if (p === null) return true;               // 无权限配置 → 全开
    if (p.length && p[0] === '*') return true; // 全部权限
    if (p.indexOf(perm) >= 0) return true;     // 该表权限
    return p.indexOf('hr') >= 0;               // 旧版「人事中心」权限 → 视为全开（向后兼容）
  }

  // ==================== 组件装配 ====================
  var components = { 'hr-card-selector': HR.CardSelector };
  window.HR_TABLES.forEach(function (cfg) {
    components['hr-page-' + cfg.key] = HR.makePage(cfg);
  });

  var HrHub = {
    components: components,
    data: function () {
      return {
        tables: window.HR_TABLES.slice(),
        counts: {},
        activeKey: sessionStorage.getItem('hr_active_table') || '',
      };
    },
    computed: {
      allowed: function () {
        var self = this;
        return this.tables.filter(function (t) { return hasPerm(t.perm); });
      },
      cards: function () {
        var self = this;
        return this.allowed.map(function (t) {
          return {
            key: t.key, label: t.label, desc: t.desc, icon: t.icon, color: t.color,
            count: self.counts[t.key] === undefined ? null : self.counts[t.key],
          };
        });
      },
      current: function () {
        if (!this.allowed.length) return '';
        var exists = this.allowed.some(function (t) { return t.key === this.activeKey; }, this);
        return exists ? 'hr-page-' + this.activeKey : 'hr-page-' + this.allowed[0].key;
      },
    },
    methods: {
      switchTo: function (key) {
        this.activeKey = key;
        sessionStorage.setItem('hr_active_table', key);
      },
      fetchCounts: function () {
        var self = this;
        ApiService.getHrCounts().then(function (c) { if (c) self.counts = c; });
      },
    },
    mounted: function () {
      var self = this;
      this.fetchCounts();
      HR.onDataChange = function () { self.fetchCounts(); };
    },
    beforeUnmount: function () {
      HR.onDataChange = null;
    },
    template: `
<div class="hr-hub">
  <div class="hr-hub-head">
    <div class="hr-hub-brand">
      <i class="fa-solid fa-building-user"></i>
      <h2>人事数据中心</h2>
      <div class="hr-hub-brand-sub">
        <span class="hr-hub-rule"></span>
        <p>人事数据统一管理，集中维护人事相关业务数据</p>
        <span class="hr-hub-rule"></span>
      </div>
    </div>
  </div>

  <hr-card-selector :cards="cards" :active="activeKey || (allowed[0] && allowed[0].key)" @select="switchTo"></hr-card-selector>

  <component v-if="current" :is="current" :key="current"></component>

  <div v-if="!allowed.length" class="hr-no-perm">
    <i class="fa-solid fa-lock"></i>
    <h3>暂无已开通的人事数据表</h3>
    <p>请联系超级管理员在「管理员与权限」中为你勾选需要的人事数据模块。</p>
  </div>
</div>
    `,
  };

  // ==================== 挂载 / 卸载（与既有 Vue 页面同款互斥逻辑） ====================
  var _app = null;

  function mount() {
    if (_app) return;
    var el = document.getElementById('page-hr');
    if (!el) return;
    _app = Vue.createApp(HrHub);
    _app.mount(el);
  }

  function unmount() {
    if (!_app) return;
    _app.unmount();
    _app = null;
    var el = document.getElementById('page-hr');
    if (el) el.innerHTML = '';
  }

  function install() {
    var el = document.getElementById('page-hr');
    if (!el) return;
    if (!el.classList.contains('hidden')) mount();
    var ob = new MutationObserver(function () {
      if (!el.classList.contains('hidden')) mount();
      else unmount();
    });
    ob.observe(el, { attributes: true, attributeFilter: ['class'] });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install);
  } else {
    install();
  }
})();
