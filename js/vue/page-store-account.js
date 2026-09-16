/**
 * 店铺账号管理 — Vue 版（重做：三合一 Tab，真实接口）
 * classic script，与 app.js 共用全局词法作用域。
 * - 千牛 / 抖店 / 京东 三个 Tab（品牌色图标），数据来自 backend 三张账号表
 * - 抖店 Tab 内嵌「登录邮箱」独立区域（抖店邮箱账号表）
 * - 每店「运营中」开关（关掉＝不抓数）
 * - 增删改 + 更新数据（抓取触发）悬浮弹窗
 * XSS：全部 {{ }} 插值，绝不用 v-html；模板不裸访问嵌套属性
 */
(function () {
  'use strict';

  // 各类型字段定义（前端 key -> 中文标签）
  var FIELDS = {
    qianniu: [
      { key: 'account', label: '账号', required: true, placeholder: '店铺名:子账号' },
      { key: 'password', label: '密码' },
      { key: 'shopId', label: '店铺ID' },
      { key: 'brand', label: '品牌' },
      { key: 'remark', label: '备注' },
    ],
    doudian: [
      { key: 'shopName', label: '店铺名', required: true },
      { key: 'shopId', label: '店铺ID' },
      { key: 'brand', label: '品牌' },
      { key: 'remark', label: '备注' },
    ],
    'doudian-email': [
      { key: 'email', label: '邮箱', required: true },
      { key: 'password', label: '密码' },
      { key: 'remark', label: '备注' },
    ],
    jd: [
      { key: 'account', label: '账号', required: true },
      { key: 'password', label: '密码' },
      { key: 'shopId', label: '店铺ID' },
      { key: 'shopName', label: '店铺名' },
      { key: 'remark', label: '备注' },
    ],
  };

  // 三个平台 Tab：品牌色渐变（图标不再用纯黑/纯灰）
  var TABS = [
    { key: 'qianniu', label: '千牛', icon: 'fa-shop', grad: 'linear-gradient(135deg,#ff9500,#ff5000)' },
    { key: 'doudian', label: '抖店', icon: 'fa-clapperboard', grad: 'linear-gradient(135deg,#00d2ff,#0091ff)' },
    { key: 'jd', label: '京东', icon: 'fa-cube', grad: 'linear-gradient(135deg,#ff6a6a,#e1251b)' },
  ];

  var _sa = Vue.reactive({
    tab: 'qianniu',
    qianniu: [], doudian: [], emails: [], jd: [],
    search: '', loading: false,
    // 编辑弹窗
    modal: false, modalType: 'qianniu', editingId: null, form: {}, saving: false,
    // 更新数据弹窗
    showUpdate: false, upStart: '', upEnd: '', selected: {}, upShopSearch: '',
  });

  // 日期选择器的原生 input 引用（用于 showPicker 强制弹出日历）
  var upStartInput = Vue.ref(null);
  var upEndInput = Vue.ref(null);

  function currentList() {
    return { qianniu: _sa.qianniu, doudian: _sa.doudian, jd: _sa.jd }[_sa.tab] || [];
  }

  function _makeForm(type) {
    var f = {};
    FIELDS[type].forEach(function (x) { f[x.key] = ''; });
    return f;
  }

  var StoreAccountPage = {
    setup() {
      var filtered = Vue.computed(function () {
        var kw = _sa.search.trim().toLowerCase();
        var list = currentList();
        if (!kw) return list;
        var keys = FIELDS[_sa.tab].map(function (f) { return f.key; });
        return list.filter(function (r) {
          return keys.some(function (k) {
            var v = r[k];
            return v != null && String(v).toLowerCase().indexOf(kw) >= 0;
          });
        });
      });
      var counts = Vue.computed(function () {
        var list = currentList();
        var active = list.filter(function (r) { return r.active; }).length;
        return { total: list.length, active: active, inactive: list.length - active };
      });
      var modalFields = Vue.computed(function () { return FIELDS[_sa.modalType]; });
      var updateShops = Vue.computed(function () {
        return [].concat(
          _sa.qianniu.map(function (r) { return { key: 'qianniu:' + r.id, label: r.account, active: r.active }; }),
          _sa.doudian.map(function (r) { return { key: 'doudian:' + r.id, label: r.shopName, active: r.active }; }),
          _sa.jd.map(function (r) { return { key: 'jd:' + r.id, label: (r.shopName || r.account), active: r.active }; })
        );
      });
      var selectedCount = Vue.computed(function () {
        return Object.keys(_sa.selected).filter(function (k) { return _sa.selected[k]; }).length;
      });
      // 按平台分组，供更新数据弹窗分区块展示
      var groupedShops = Vue.computed(function () {
        return [
          { key: 'qianniu', label: '千牛', grad: TABS[0].grad, shops: _sa.qianniu.map(function (r) { return { key: 'qianniu:' + r.id, label: r.account, active: r.active }; }) },
          { key: 'doudian', label: '抖店', grad: TABS[1].grad, shops: _sa.doudian.map(function (r) { return { key: 'doudian:' + r.id, label: r.shopName, active: r.active }; }) },
          { key: 'jd', label: '京东', grad: TABS[2].grad, shops: _sa.jd.map(function (r) { return { key: 'jd:' + r.id, label: (r.shopName || r.account), active: r.active }; }) },
        ];
      });
      var upFilteredGroups = Vue.computed(function () {
        var kw = _sa.upShopSearch.trim().toLowerCase();
        if (!kw) return groupedShops.value;
        return groupedShops.value.map(function (g) {
          return {
            key: g.key, label: g.label, grad: g.grad,
            shops: g.shops.filter(function (s) { return s.label.toLowerCase().indexOf(kw) >= 0; }),
          };
        });
      });

      async function loadAll() {
        _sa.loading = true;
        var parts = await Promise.all([
          ApiService.getStoreAccounts('qianniu'),
          ApiService.getStoreAccounts('doudian'),
          ApiService.getStoreAccounts('doudian-email'),
          ApiService.getStoreAccounts('jd'),
        ]);
        _sa.qianniu = parts[0] || [];
        _sa.doudian = parts[1] || [];
        _sa.emails = parts[2] || [];
        _sa.jd = parts[3] || [];
        _sa.loading = false;
      }

      Vue.onMounted(function () { loadAll(); });

      function switchTab(t) { _sa.tab = t; _sa.search = ''; }

      function openCreate(type) {
        _sa.modalType = type;
        _sa.editingId = null;
        _sa.form = _makeForm(type);
        _sa.modal = true;
      }
      function openEdit(type, row) {
        _sa.modalType = type;
        _sa.editingId = row.id;
        var f = _makeForm(type);
        FIELDS[type].forEach(function (x) { f[x.key] = row[x.key] != null ? row[x.key] : ''; });
        _sa.form = f;
        _sa.modal = true;
      }
      function closeModal() { _sa.modal = false; }

      async function saveModal() {
        var payload = {};
        FIELDS[_sa.modalType].forEach(function (x) { payload[x.key] = _sa.form[x.key]; });
        var r;
        if (_sa.editingId) {
          r = await ApiService.updateStoreAccount(_sa.modalType, _sa.editingId, payload);
        } else {
          r = await ApiService.createStoreAccount(_sa.modalType, payload);
        }
        if (r === null) { App.showToast('保存失败，请重试', 'error'); return; }
        _sa.modal = false;
        App.showToast('已保存', 'success');
        await loadAll();
      }

      async function removeRow(type, row) {
        if (!confirm('确定删除该记录吗？')) return;
        var r = await ApiService.deleteStoreAccount(type, row.id);
        if (r === null) { App.showToast('删除失败，请重试', 'error'); return; }
        App.showToast('已删除', 'success');
        await loadAll();
      }

      async function toggleActive(type, row) {
        var r = await ApiService.toggleStoreAccount(type, row.id, row.active ? 0 : 1);
        if (r === null) { App.showToast('切换失败，请重试', 'error'); return; }
        await loadAll();
      }

      function toggleSelect(shopKey) {
        _sa.selected[shopKey] = !_sa.selected[shopKey];
      }
      function selectAll() {
        updateShops.value.forEach(function (s) { _sa.selected[s.key] = true; });
      }
      function clearAll() { _sa.selected = {}; }
      function selectGroup(g) {
        var allOn = g.shops.every(function (s) { return _sa.selected[s.key]; });
        g.shops.forEach(function (s) { _sa.selected[s.key] = !allOn; });
      }

      function openUpdate() {
        _sa.upStart = '';
        _sa.upEnd = '';
        _sa.selected = {};
        _sa.upShopSearch = '';
        _sa.showUpdate = true;
      }
      function submitUpdate() {
        if (selectedCount.value === 0) { App.showToast('请先选择要抓取的店铺', 'error'); return; }
        if (!_sa.upStart) { App.showToast('请选择开始日期', 'error'); return; }
        App.showToast('抓取引擎将在后续模块接入，当前先完成账号配置', 'info');
      }
      function closeUpdate() { _sa.showUpdate = false; }

      // 点击整个日期框触发日历（showPicker 让 Chrome 直接弹日历，不再依赖点图标）
      function pickDate(kind) {
        var el = kind === 'start' ? upStartInput.value : upEndInput.value;
        if (!el) return;
        try { el.showPicker(); } catch (e) { el.focus(); el.click(); }
      }
      // 聚焦时解除 readonly，阻止浏览器自动填充（Chrome 会无视 autocomplete=off）
      function unlockInput(e) { if (e && e.target) e.target.removeAttribute('readonly'); }

      function tabClass(t) { return _sa.tab === t.key ? 'sa-tab active' : 'sa-tab'; }
      function tabStyle(t) {
        return _sa.tab === t.key ? { background: t.grad, color: '#fff', borderColor: 'transparent' } : {};
      }
      function tabIcoStyle(t) {
        return { background: _sa.tab === t.key ? 'rgba(255,255,255,0.28)' : t.grad };
      }
      function activeText(r) { return r.active ? '运营中' : '已停用'; }

      return {
        sa: _sa, TABS: TABS, filtered: filtered, counts: counts,
        modalFields: modalFields, updateShops: updateShops, selectedCount: selectedCount,
        groupedShops: groupedShops, upFilteredGroups: upFilteredGroups,
        loadAll: loadAll, switchTab: switchTab, openCreate: openCreate, openEdit: openEdit,
        closeModal: closeModal, saveModal: saveModal, removeRow: removeRow,
        toggleActive: toggleActive, toggleSelect: toggleSelect,
        selectAll: selectAll, clearAll: clearAll, selectGroup: selectGroup,
        openUpdate: openUpdate, submitUpdate: submitUpdate, closeUpdate: closeUpdate,
        pickDate: pickDate, unlockInput: unlockInput,
        upStartInput: upStartInput, upEndInput: upEndInput,
        tabClass: tabClass, tabStyle: tabStyle, tabIcoStyle: tabIcoStyle, activeText: activeText,
      };
    },

    template: `
<div>
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon" style="background:linear-gradient(135deg,#6366f1,#8b5cf6);box-shadow:0 2px 8px rgba(99,102,241,0.25)"><i class="fa-solid fa-id-card" style="color:#fff;font-size:18px"></i></div>
      <div class="dh-title-group"><h2 class="dh-title">店铺账号管理</h2><span class="dh-subtitle">Store Account Management</span><span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>共 {{ counts.total }} 个店铺</span></div>
    </div>
    <div class="dh-chips">
      <div class="dh-chip"><span class="dh-chip-label">运营中</span><span class="dh-chip-value" style="color:#16a34a">{{ counts.active }}</span></div>
      <div class="dh-chip"><span class="dh-chip-label">已停用</span><span class="dh-chip-value" style="color:#9ca3af">{{ counts.inactive }}</span></div>
    </div>
  </div>

  <div class="ap-toolbar">
    <div class="sa-toolbar-left">
      <div class="ap-search-wrap"><i class="fa-solid fa-search"></i><input class="ap-search-input" v-model="sa.search" placeholder="搜索当前列表..." autocomplete="off" readonly @focus="unlockInput($event)" spellcheck="false"></div>
      <div class="sa-tabs">
        <button v-for="t in TABS" :key="t.key" type="button" :class="tabClass(t.key)" :style="tabStyle(t)" @click="switchTab(t.key)">
          <span class="sa-tab-ico" :style="tabIcoStyle(t)"><i class="fa-solid" :class="t.icon"></i></span>{{ t.label }}
        </button>
      </div>
    </div>
    <div class="sa-toolbar-right">
      <button class="ap-btn-primary sa-btn-update" @click="openUpdate"><i class="fa-solid fa-cloud-arrow-down"></i> 更新数据</button>
      <button class="ap-btn-primary" @click="openCreate(sa.tab)"><i class="fa-solid fa-plus"></i> 新增{{ sa.tab === 'qianniu' ? '千牛账号' : (sa.tab === 'doudian' ? '抖店店铺' : '京东账号') }}</button>
    </div>
  </div>

  <div v-if="sa.tab === 'doudian'" class="sa-email-panel">
    <div class="sa-email-head">
      <div><i class="fa-solid fa-envelope" style="color:#6366f1"></i> <strong>抖店登录邮箱</strong><span style="color:#94a3b8;font-size:12px;margin-left:8px">邮箱登录后逐个切换店铺抓取</span></div>
      <button class="ap-btn-sm edit" @click="openCreate('doudian-email')"><i class="fa-solid fa-plus"></i> 新增邮箱</button>
    </div>
    <table class="ap-table">
      <thead><tr><th style="width:220px">邮箱</th><th style="width:160px">密码</th><th style="width:90px">状态</th><th style="width:80px">操作</th></tr></thead>
      <tbody>
        <tr v-for="e in sa.emails" :key="e.id">
          <td style="font-family:monospace">{{ e.email }}</td>
          <td style="font-family:monospace;font-size:12px">{{ e.password }}</td>
          <td><button type="button" @click="toggleActive('doudian-email', e)" :style="e.active ? 'background:#e6f7ee;color:#0f9d58;border:1px solid #bfe8d0' : 'background:#f2f3f5;color:#9aa0a6;border:1px solid #e0e0e0'" style="border-radius:20px;padding:3px 12px;font-size:12px;cursor:pointer">{{ activeText(e) }}</button></td>
          <td><div class="ap-actions">
            <button class="ap-btn-sm edit" @click="openEdit('doudian-email', e)"><i class="fa-solid fa-pen"></i></button>
            <button class="ap-btn-sm delete" @click="removeRow('doudian-email', e)"><i class="fa-solid fa-trash"></i></button>
          </div></td>
        </tr>
        <tr v-if="!sa.emails.length"><td colspan="4" style="text-align:center;padding:24px;color:#94a3b8">暂无登录邮箱，请点击右上角新增</td></tr>
      </tbody>
    </table>
  </div>

  <div class="ap-table-wrap">
    <table class="ap-table">
      <thead>
        <tr v-if="sa.tab === 'qianniu'">
          <th style="width:60px">ID</th><th style="width:220px">账号</th><th style="width:140px">密码</th><th style="width:110px">店铺ID</th><th style="width:90px">品牌</th><th style="width:100px">运营状态</th><th style="width:90px">操作</th>
        </tr>
        <tr v-else-if="sa.tab === 'jd'">
          <th style="width:60px">ID</th><th style="width:140px">账号</th><th style="width:120px">密码</th><th style="width:110px">店铺ID</th><th style="width:160px">店铺名</th><th style="width:100px">运营状态</th><th style="width:90px">操作</th>
        </tr>
        <tr v-else>
          <th style="width:60px">ID</th><th style="width:240px">店铺名</th><th style="width:110px">店铺ID</th><th style="width:90px">品牌</th><th style="width:100px">运营状态</th><th style="width:90px">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="sa.tab === 'qianniu'" v-for="r in filtered" :key="r.id">
          <td>{{ r.id }}</td>
          <td><strong>{{ r.account }}</strong></td>
          <td style="font-family:monospace;font-size:12px">{{ r.password }}</td>
          <td style="font-family:monospace;font-size:12px">{{ r.shopId || '—' }}</td>
          <td><span class="ps-store-platform">{{ r.brand }}</span></td>
          <td><button type="button" @click="toggleActive('qianniu', r)" :style="r.active ? 'background:#e6f7ee;color:#0f9d58;border:1px solid #bfe8d0' : 'background:#f2f3f5;color:#9aa0a6;border:1px solid #e0e0e0'" style="border-radius:20px;padding:3px 12px;font-size:12px;cursor:pointer">{{ activeText(r) }}</button></td>
          <td><div class="ap-actions">
            <button class="ap-btn-sm edit" @click="openEdit('qianniu', r)"><i class="fa-solid fa-pen"></i></button>
            <button class="ap-btn-sm delete" @click="removeRow('qianniu', r)"><i class="fa-solid fa-trash"></i></button>
          </div></td>
        </tr>
        <tr v-else-if="sa.tab === 'jd'" v-for="r in filtered" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.account }}</td>
          <td style="font-family:monospace;font-size:12px">{{ r.password }}</td>
          <td style="font-family:monospace;font-size:12px">{{ r.shopId }}</td>
          <td><strong>{{ r.shopName }}</strong></td>
          <td><button type="button" @click="toggleActive('jd', r)" :style="r.active ? 'background:#e6f7ee;color:#0f9d58;border:1px solid #bfe8d0' : 'background:#f2f3f5;color:#9aa0a6;border:1px solid #e0e0e0'" style="border-radius:20px;padding:3px 12px;font-size:12px;cursor:pointer">{{ activeText(r) }}</button></td>
          <td><div class="ap-actions">
            <button class="ap-btn-sm edit" @click="openEdit('jd', r)"><i class="fa-solid fa-pen"></i></button>
            <button class="ap-btn-sm delete" @click="removeRow('jd', r)"><i class="fa-solid fa-trash"></i></button>
          </div></td>
        </tr>
        <tr v-else v-for="r in filtered" :key="r.id">
          <td>{{ r.id }}</td>
          <td><strong>{{ r.shopName }}</strong></td>
          <td style="font-family:monospace;font-size:12px">{{ r.shopId }}</td>
          <td><span class="ps-store-platform">{{ r.brand }}</span></td>
          <td><button type="button" @click="toggleActive('doudian', r)" :style="r.active ? 'background:#e6f7ee;color:#0f9d58;border:1px solid #bfe8d0' : 'background:#f2f3f5;color:#9aa0a6;border:1px solid #e0e0e0'" style="border-radius:20px;padding:3px 12px;font-size:12px;cursor:pointer">{{ activeText(r) }}</button></td>
          <td><div class="ap-actions">
            <button class="ap-btn-sm edit" @click="openEdit('doudian', r)"><i class="fa-solid fa-pen"></i></button>
            <button class="ap-btn-sm delete" @click="removeRow('doudian', r)"><i class="fa-solid fa-trash"></i></button>
          </div></td>
        </tr>
        <tr v-if="!filtered.length"><td colspan="7" style="text-align:center;padding:32px;color:#94a3b8">未找到匹配的记录</td></tr>
      </tbody>
    </table>
  </div>
  <div class="ap-table-info">共 {{ filtered.length }} 条记录</div>

  <div v-if="sa.modal" class="sa-modal-mask" @click.self="closeModal">
    <div class="sa-modal">
      <div class="sa-modal-head"><strong>{{ sa.editingId ? '编辑' : '新增' }}</strong><button type="button" class="sa-modal-close" @click="closeModal"><i class="fa-solid fa-xmark"></i></button></div>
      <div class="sa-modal-body">
        <div v-for="f in modalFields" :key="f.key" class="sa-field">
          <label>{{ f.label }}{{ f.required ? ' *' : '' }}</label>
          <input v-model="sa.form[f.key]" type="text" :placeholder="f.placeholder || ''" autocomplete="off" :name="'saForm_' + f.key">
        </div>
      </div>
      <div class="sa-modal-foot">
        <button class="ap-btn-primary" @click="saveModal"><i class="fa-solid fa-check"></i> 保存</button>
        <button class="ap-btn-plain" @click="closeModal">取消</button>
      </div>
    </div>
  </div>

  <div v-if="sa.showUpdate" class="sa-modal-mask" @click.self="closeUpdate">
    <div class="sa-modal" style="max-width:640px">
      <div class="sa-up-head">
        <div class="sa-up-title">
          <span class="sa-up-ico"><i class="fa-solid fa-cloud-arrow-down"></i></span>
          <div><strong>更新数据</strong><span class="sa-up-sub">选择店铺与日期范围，触发对应平台数据抓取</span></div>
        </div>
        <button type="button" class="sa-modal-close" @click="closeUpdate"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="sa-modal-body">
        <div class="sa-up-hint"><i class="fa-solid fa-circle-info"></i> 支持多选店铺、单日或区间抓取；已停用店铺默认不参与。</div>
        <div class="sa-date-grid">
          <div class="sa-date-field" @click="pickDate('start')">
            <span class="sa-date-field-label">开始日期</span>
            <span class="sa-date-field-value" :class="{ empty: !sa.upStart }">{{ sa.upStart || '选择日期' }}</span>
            <i class="fa-solid fa-calendar-days"></i>
            <input type="date" ref="upStartInput" v-model="sa.upStart" class="sa-date-native">
          </div>
          <div class="sa-date-field" @click="pickDate('end')">
            <span class="sa-date-field-label">结束日期</span>
            <span class="sa-date-field-value" :class="{ empty: !sa.upEnd }">{{ sa.upEnd || '单日则留空' }}</span>
            <i class="fa-solid fa-calendar-days"></i>
            <input type="date" ref="upEndInput" v-model="sa.upEnd" class="sa-date-native">
          </div>
        </div>
        <div class="sa-shop-filter">
          <div class="sa-shop-search"><i class="fa-solid fa-magnifying-glass"></i><input v-model="sa.upShopSearch" placeholder="搜索店铺名..." autocomplete="off" readonly @focus="unlockInput($event)" spellcheck="false"></div>
          <div style="display:flex;gap:4px">
            <button class="sa-link-btn" @click="selectAll">全选</button>
            <button class="sa-link-btn" @click="clearAll">清空</button>
          </div>
        </div>
        <div style="font-size:12px;color:#94a3b8;margin-bottom:8px">已选 <strong style="color:#6366f1">{{ selectedCount }}</strong> 家店铺</div>
        <div class="sa-shop-groups">
          <div v-for="g in upFilteredGroups" :key="g.key" class="sa-shop-group">
            <div class="sa-shop-group-head" @click="selectGroup(g)">
              <span class="sa-dot" :style="{ background: g.grad }"></span>
              <strong style="font-size:13px;color:#334155">{{ g.label }}</strong>
              <span class="sa-group-count">{{ g.shops.length }}</span>
            </div>
            <div class="sa-shop-grid">
              <label v-for="s in g.shops" :key="s.key" class="sa-shop-item" :class="sa.selected[s.key] ? 'on' : ''">
                <input type="checkbox" :checked="!!sa.selected[s.key]" @change="toggleSelect(s.key)">
                <span class="sa-shop-name">{{ s.label }}</span>
                <span v-if="!s.active" class="sa-shop-off">停用</span>
              </label>
            </div>
          </div>
        </div>
      </div>
      <div class="sa-modal-foot">
        <button class="ap-btn-primary sa-btn-update" @click="submitUpdate"><i class="fa-solid fa-play"></i> 开始抓取</button>
        <button class="ap-btn-plain" @click="closeUpdate">取消</button>
      </div>
    </div>
  </div>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子（接管模式） ====================
  var _saApp = null;   // Vue app 对象（unmount 用）
  var _saVm = null;    // 根组件实例（调 setup 暴露的方法用）

  function mountStoreAccountVue() {
    if (_saVm) return;
    var mount = document.getElementById('page-store-account');
    if (!mount) return;
    _saApp = Vue.createApp(StoreAccountPage);
    _saVm = _saApp.mount(mount);
  }

  function unmountStoreAccountVue() {
    if (!_saVm) return;
    _saApp.unmount();
    _saApp = null;
    _saVm = null;
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
      if (nowVisible && !_saVm) mountStoreAccountVue();
      else if (!nowVisible && _saVm) unmountStoreAccountVue();
    });
    observer.observe(container, { attributes: true, attributeFilter: ['class'] });
  }

  initHook();
})();
