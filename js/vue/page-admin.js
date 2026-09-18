// ==================== 账号与权限 Vue 版 ====================
// 两个 Tab：账号列表（按角色做部门按钮切换） / 角色与权限（权限分配悬浮窗）
// 接口：ApiService.getAdmins / createAdmin / updateAdmin / deleteAdmin / getAdminPassword
//       ApiService.getRoles / createRole / updateRole / deleteRole / getMyScope
//
// ★★ 权限模型（2026-09-18 调整）—— 三层：
//     第 1 层 开发人员 / 超级管理员 —— 全库账号 + 角色与权限 Tab
//     第 2 层 部门主管（leader==本人姓名）—— 只能管本部门账号；
//              「角色与权限」Tab 对其隐藏；给账号赋权 ≤ 自己的权限
//     第 3 层 普通员工 —— 无入口
//   ★ 前端只做显示层（隐藏/置灰/锁定下拉）。真正的拦截在后端
//     _my_scope() / _can_manage_roles()，即使绕过前端直接调接口也会被拒。
(function () {
  if (typeof Vue === 'undefined' || typeof ApiService === 'undefined' || typeof App === 'undefined' || typeof EcomUI === 'undefined') return;

  // ---- 权限分配 UI 用的页面清单（与 app.js PAGE_CATEGORIES / index.html 侧边栏保持一一对应） ----
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
      { id: 'toolbox-announce', name: '通告发放' },
    ]},
    { group: '系统管理', pages: [
      { id: 'admin-permissions', name: '管理员与权限' },
      { id: 'profile', name: '个人中心设置' },
    ]},
  ];

  // ---- 部门按钮 = 6 个大角色 ----
  var DEPT_TABS = [
    { key: '全部',  label: '全部',     role: '全部' },
    { key: '开发人员',   label: '开发人员',   role: '开发人员' },
    { key: '超级管理员', label: '超级管理员', role: '超级管理员' },
    { key: '人事行政部', label: '人事行政部', role: '人事行政部' },
    { key: '财务部',     label: '财务部',     role: '财务部' },
    { key: '种草部',     label: '种草部',     role: '种草部' },
    { key: '临时账号',   label: '临时账号',   role: '临时账号' },
  ];

  var GENDER_TEXT = { male: '男', female: '女' };

  // ---- 模块级状态（跨挂载/卸载保留） ----
  var _state = Vue.reactive({
    activeTab: 'accounts',
    activeDept: '全部',
    accounts: [],
    roles: [],
    keyword: '',
    pwdVisible: {},
    pwdCache: {},   // id -> 明文密码（列表接口不下发密码，点眼睛时才单条拉取）
  });

  function allPageIds() {
    var ids = [];
    PAGE_CATEGORIES.forEach(function (cat) { cat.pages.forEach(function (p) { ids.push(p.id); }); });
    return ids;
  }
  var ALL_PAGE_IDS = allPageIds();

  var AdminPage = {
    components: { 'ecom-modal': EcomUI.Modal },
    setup: function () {
      // ---- 身份与管辖范围 ----
      // 先用 app.js 里已有的静态判定兜底，再用后端 my-scope 校正（挂载时异步拉）
      var isSuper = Vue.ref(!!(App.isAccountManager && App.isAccountManager()));
      var canManageRoles = Vue.ref(!!(App.isCanManageRoles && App.isCanManageRoles()));
      var isLead = Vue.ref(!!(App.isDeptLead && App.isDeptLead()));
      var scope = Vue.reactive({ level: 'none', role: '', department: '', perms: [], allPerm: false, loaded: false });
      var canManageAccounts = Vue.ref(isSuper.value || isLead.value);

      // 主管：被锁定的一组值
      var lockRole = Vue.computed(function () { return isLead.value && !isSuper.value; });
      var lockDept = Vue.computed(function () { return isLead.value && !isSuper.value; });

      // 可授权的权限上限（主管 = 自己权限；超管 = 全部）
      var grantablePerms = Vue.computed(function () {
        if (!scope.loaded) return null;               // 未加载 = 不限制(交给后端)
        if (scope.allPerm) return null;               // 全权 = 不限制
        return scope.perms || [];
      });

      // 搜索框防浏览器自动填充：初始 readonly，首次聚焦时解除
      var searchLocked = Vue.ref(true);
      function unlockSearch() { searchLocked.value = false; }

      // 弹窗状态
      var acctModal = Vue.reactive({
        visible: false, isEdit: false,
        form: { id: '', name: '', account: '', password: '', role: '', department: '',
                subDept: '', leader: '', gender: '', status: 'enabled' },
      });
      var pwdModal = Vue.reactive({ visible: false, acctId: null, acctLabel: '', password: '' });
      var roleModal = Vue.reactive({ visible: false, isEdit: false, form: { id: '', name: '', permissions: [] } });
      var confirmBox = Vue.reactive({ visible: false, message: '' });
      var _confirmAction = null;

      // ---- 数据加载 ----
      async function loadScope() {
        var s = await ApiService.getMyScope();
        if (s && typeof s === 'object') {
          scope.level = s.level || 'none';
          scope.role = s.role || '';
          scope.department = s.department || '';
          scope.perms = Array.isArray(s.perms) ? s.perms : [];
          scope.allPerm = !!s.allPerm;
          scope.loaded = true;
          isLead.value = scope.level === 'lead';
          isSuper.value = scope.level === 'super';
          canManageRoles.value = !!s.canManageRoles;
          canManageAccounts.value = !!s.canManageAccounts;
          // ★ 主管登录时把部门按钮钉死在「全部」（= 本部门），由 visibleDeptTabs 限制可选项
          if (isLead.value && !isSuper.value) {
            _state.activeDept = '全部';
          }
        }
        return scope;
      }

      async function loadAccounts() {
        if (!canManageAccounts.value) return;
        var list = await ApiService.getAdmins();
        _state.accounts = Array.isArray(list) ? list : [];
        // 列表刷新说明密码可能已变，清掉已取出的明文与展开状态
        _state.pwdCache = {};
        _state.pwdVisible = {};
        syncRoleCounts();
      }
      async function loadRoles() {
        if (!canManageRoles.value) return;   // 主管无权拉角色表（后端也会拒）
        var roles = await ApiService.getRoles();
        if (roles && roles.length) _state.roles = roles;
        syncRoleCounts();
      }

      function syncRoleCounts() {
        // 主管拿不到角色表（_state.roles 为空）→ 直接跳过，避免空转
        if (!_state.roles.length) return;
        _state.roles.forEach(function (r) {
          r.count = _state.accounts.filter(function (a) { return a.role === r.name; }).length;
        });
      }

      // 部门按钮上的账号数
      var deptCounts = Vue.computed(function () {
        var m = {};
        _state.accounts.forEach(function (a) {
          var k = a.role || '';
          m[k] = (m[k] || 0) + 1;
        });
        m['全部'] = _state.accounts.length;
        return m;
      });

      // ★ 主管只能看到「全部」+ 自己那一个部门按钮；超管看全部
      var visibleDeptTabs = Vue.computed(function () {
        if (isLead.value && !isSuper.value) {
          return [
            { key: '全部', label: '本部门', role: '全部' },
            { key: scope.role, label: scope.role, role: scope.role },
          ];
        }
        return DEPT_TABS;
      });

      var filteredAccounts = Vue.computed(function () {
        var kw = (_state.keyword || '').trim().toLowerCase();
        return _state.accounts.filter(function (a) {
          if (_state.activeDept !== '全部' && a.role !== _state.activeDept) return false;
          if (!kw) return true;
          return (a.name || '').toLowerCase().indexOf(kw) >= 0
            || (a.account || '').toLowerCase().indexOf(kw) >= 0
            || (a.department || '').toLowerCase().indexOf(kw) >= 0
            || (a.subDept || '').toLowerCase().indexOf(kw) >= 0;
        });
      });

      // 角色下拉选项：主管被锁死为本人角色
      var roleOptions = Vue.computed(function () {
        if (isLead.value && !isSuper.value) return scope.role ? [scope.role] : [];
        return _state.roles.map(function (r) { return r.name; });
      });

      // ★ 某条账号是否可被当前者操作（后端也返回 manageable，前端优先用它）
      function canTouchAccount(a) {
        if (!a) return false;
        if (a.manageable === false) return false;
        if (isSuper.value) return true;
        if (!isLead.value) return false;
        if (a.isSelf) return false;                       // 主管不能改自己
        return (a.department || '') === scope.department; // 仅本部门
      }

      // ★ 权限是否超出可授权上限（超出则前端置灰不可勾）
      function permOverLimit(pid) {
        if (!grantablePerms.value) return false;          // null = 不限
        return grantablePerms.value.indexOf(pid) < 0;
      }

      function switchDept(key) {
        _state.activeDept = key;
        _state.keyword = _state.keyword;  // 保持搜索词，仅切换部门
      }

      function rolePermText(r) {
        var perms = r.permissions || [];
        if (Array.isArray(perms) && perms[0] === '*') return { text: '全部权限', color: '#16a34a' };
        if (perms.length > 0) return { text: perms.length + ' 项', color: '#6366f1' };
        return { text: '无权限', color: '#94a3b8' };
      }

      function genderText(g) { return GENDER_TEXT[g] || '—'; }

      // ---- 账号操作 ----
      function openAcctModal(id) {
        var a = id ? _state.accounts.find(function (x) { return x.id === id; }) : null;
        acctModal.isEdit = !!a;
        // ★ 主管新增：部门/角色/主管 三项预填并锁定为本人所属
        var locked = isLead.value && !isSuper.value;
        acctModal.form = {
          id: a ? a.id : '',
          name: a ? (a.name || '') : '',
          account: a ? (a.account || '') : '',
          password: '',
          role: a ? (a.role || '') : (locked ? scope.role
                : (_state.roles.length > 0 ? _state.roles[0].name : '')),
          department: a ? (a.department || '') : (locked ? scope.department : ''),
          subDept: a ? (a.subDept || '') : '',
          leader: a ? (a.leader || '') : (locked ? (sessionStorage.getItem('admin_current_user') || '') : ''),
          gender: a ? (a.gender || '') : '',
          status: a ? (a.status || 'enabled') : 'enabled',
        };
        acctModal.visible = true;
      }

      async function saveAcct() {
        var f = acctModal.form;
        if (!f.name.trim() || !f.account.trim() || (!acctModal.isEdit && !f.password.trim())) {
          App.showToast('请填写完整信息（姓名 / 账号 / 密码）', 'error');
          return;
        }
        var payload = {
          name: f.name.trim(), account: f.account.trim(), role: f.role, status: f.status,
          department: f.department.trim(), subDept: f.subDept.trim(),
          leader: f.leader.trim(), gender: f.gender,
        };
        if (f.password.trim()) payload.password = f.password.trim();

        var r = acctModal.isEdit
          ? await ApiService.updateAdmin(parseInt(f.id), payload)
          : await ApiService.createAdmin(payload);
        if (!r || !r.ok) { App.showToast((r && r.msg) || '保存失败', 'error'); return; }
        acctModal.visible = false;
        App.showToast(acctModal.isEdit ? '账号已更新' : '账号已创建', 'success');
        loadAccounts();
      }

      // 点「眼睛」——展开时才向 /admin/accounts/<id>/password 单条取明文
      async function togglePwdVis(id) {
        if (_state.pwdVisible[id]) { _state.pwdVisible[id] = false; return; }
        if (_state.pwdCache[id] === undefined) {
          var r = await ApiService.getAdminPassword(id);
          if (!r || !r.ok) { App.showToast((r && r.msg) || '密码获取失败', 'error'); return; }
          _state.pwdCache[id] = (r.data && r.data.password) || '';
        }
        _state.pwdVisible[id] = true;
      }

      function openPwdModal(id) {
        var a = _state.accounts.find(function (x) { return x.id === id; });
        if (!a) return;
        pwdModal.acctId = id;
        pwdModal.acctLabel = a.name + '（' + a.account + '）';
        pwdModal.password = '';
        pwdModal.visible = true;
      }

      async function savePwd() {
        if (!pwdModal.password.trim()) { App.showToast('请输入新密码', 'error'); return; }
        var r = await ApiService.updateAdmin(parseInt(pwdModal.acctId), { password: pwdModal.password.trim() });
        if (!r || !r.ok) { App.showToast((r && r.msg) || '密码更新失败', 'error'); return; }
        pwdModal.visible = false;
        App.showToast('密码已更新', 'success');
        loadAccounts();
      }

      function confirmDeleteAcct(id) {
        var a = _state.accounts.find(function (x) { return x.id === id; });
        if (!a) return;
        if (a.account === 'admin') { App.showToast('不能删除超级管理员账号', 'error'); return; }
        if (!canTouchAccount(a)) { App.showToast('只能管理本部门账号', 'error'); return; }
        confirmBox.message = '确定删除账号「' + a.name + '（' + a.account + '）」吗？此操作不可恢复。';
        _confirmAction = async function () {
          var r = await ApiService.deleteAdmin(id);
          if (!r || !r.ok) { App.showToast((r && r.msg) || '删除失败', 'error'); return; }
          confirmBox.visible = false;
          loadAccounts();
          App.showToast('账号已删除', 'success');
        };
        confirmBox.visible = true;
      }

      async function toggleAcct(id) {
        var a = _state.accounts.find(function (x) { return x.id === id; });
        if (!a) return;
        if (a.account === 'admin') { App.showToast('不能禁用超级管理员账号', 'error'); return; }
        if (!canTouchAccount(a)) { App.showToast('只能管理本部门账号', 'error'); return; }
        var newStatus = a.status === 'enabled' ? 'disabled' : 'enabled';
        var r = await ApiService.updateAdmin(id, { status: newStatus });
        if (!r || !r.ok) { App.showToast((r && r.msg) || '操作失败', 'error'); return; }
        loadAccounts();
        App.showToast('账号已' + (newStatus === 'enabled' ? '启用' : '禁用'), 'success');
      }

      function openPwdModalFor(id) {
        var a = _state.accounts.find(function (x) { return x.id === id; });
        if (!a) return;
        if (!canTouchAccount(a)) { App.showToast('只能管理本部门账号', 'error'); return; }
        openPwdModal(id);
      }

      // ---- 角色 / 权限 ----
      function openRolePermModal(id) {
        var role = id ? _state.roles.find(function (r) { return r.id === id; }) : null;
        roleModal.isEdit = !!role;
        var perms = role && role.permissions ? role.permissions : [];
        var isAll = Array.isArray(perms) && perms[0] === '*';
        roleModal.form = {
          id: role ? role.id : '',
          name: role ? (role.name || '') : '',
          permissions: isAll ? ALL_PAGE_IDS.slice() : perms.slice(),
        };
        roleModal.visible = true;
      }

      var permCount = Vue.computed(function () {
        return (roleModal.form.permissions || []).length;
      });
      var permAllChecked = Vue.computed(function () {
        return permCount.value === ALL_PAGE_IDS.length;
      });

      function toggleAllPerms() {
        if (grantablePerms.value) {
          // 主管：全选 = 只勾自己有权的那部分
          roleModal.form.permissions = ALL_PAGE_IDS.filter(function (id) {
            return !permOverLimit(id);
          });
          return;
        }
        roleModal.form.permissions = permAllChecked.value ? [] : ALL_PAGE_IDS.slice();
      }
      function invertPerms() {
        var cur = roleModal.form.permissions || [];
        roleModal.form.permissions = ALL_PAGE_IDS.filter(function (id) {
          return cur.indexOf(id) < 0 && !permOverLimit(id);
        });
      }
      function groupChecked(cat) {
        var cur = roleModal.form.permissions || [];
        return cat.pages.every(function (p) { return cur.indexOf(p.id) >= 0; });
      }
      function toggleGroup(cat) {
        var cur = (roleModal.form.permissions || []).slice();
        var on = groupChecked(cat);
        cat.pages.forEach(function (p) {
          var i = cur.indexOf(p.id);
          if (on) { if (i >= 0) cur.splice(i, 1); }
          else if (i < 0 && !permOverLimit(p.id)) { cur.push(p.id); }   // 超限的不给勾
        });
        roleModal.form.permissions = cur;
      }

      async function saveRolePerm() {
        var f = roleModal.form;
        if (!f.name.trim()) { App.showToast('请输入角色名称', 'error'); return; }
        var data = { name: f.name.trim(), permissions: f.permissions };
        var r = roleModal.isEdit
          ? await ApiService.updateRole(parseInt(f.id), data)
          : await ApiService.createRole(data);
        if (r === null) { App.showToast('保存失败', 'error'); return; }
        roleModal.visible = false;
        App.showToast(roleModal.isEdit ? '角色已更新' : '角色已创建', 'success');
        loadRoles();
      }

      function confirmDeleteRole(id) {
        var role = _state.roles.find(function (r) { return r.id === id; });
        if (!role) return;
        if (role.name === '超级管理员') { App.showToast('不能删除超级管理员角色', 'error'); return; }
        confirmBox.message = '确定删除角色「' + role.name + '」吗？';
        _confirmAction = async function () {
          await ApiService.deleteRole(id);
          confirmBox.visible = false;
          loadRoles();
          App.showToast('角色已删除', 'success');
        };
        confirmBox.visible = true;
      }

      function doConfirm() { if (_confirmAction) _confirmAction(); }

      // ★ 挂载顺序：先拉 my-scope 确定身份与管辖范围，再按权限加载数据。
      //   主管拿不到角色表（后端会拒），所以 loadRoles 在内部自行判权后跳过。
      (async function initPage() {
        await loadScope();
        if (canManageAccounts.value) await loadAccounts();
        if (canManageRoles.value) await loadRoles();
        // 主管进页面时若 my-scope 慢于首次渲染，纠正部门按钮选中项
        if (isLead.value && !isSuper.value) _state.activeDept = '全部';
      })();

      // 主管默认停在「全部」（= 本部门），避免误显示别的部门按钮计数
      if (isLead.value && !isSuper.value && _state.activeDept !== '全部') {
        _state.activeDept = '全部';
      }

      return {
        state: _state,
        PAGE_CATEGORIES, DEPT_TABS, ALL_PAGE_IDS,
        acctModal, pwdModal, roleModal, confirmBox,
        // 身份与范围
        isAccountManager: canManageAccounts, canManageRoles, isLead, isSuper, scope,
        lockRole, lockDept, visibleDeptTabs, canTouchAccount, permOverLimit, grantablePerms,
        // 原 isAccountManager 绑定点（模板里用到）保持同名，指向新的可计算值
        filteredAccounts, deptCounts, roleOptions,
        rolePermText, genderText, searchLocked, unlockSearch, switchDept,
        openAcctModal, saveAcct, togglePwdVis, openPwdModal: openPwdModalFor, savePwd,
        confirmDeleteAcct, toggleAcct,
        openRolePermModal, toggleAllPerms, invertPerms, groupChecked, toggleGroup,
        saveRolePerm, confirmDeleteRole, doConfirm, permCount, permAllChecked,
      };
    },

    template: `
<div>
  <div class="ap-tabs">
    <button class="ap-tab" :class="{ active: state.activeTab === 'accounts' }" @click="state.activeTab = 'accounts'">账号列表</button>
    <button v-if="canManageRoles" class="ap-tab" :class="{ active: state.activeTab === 'roles' }" @click="state.activeTab = 'roles'">角色与权限</button>
  </div>

  <!-- ============ 账号列表 ============ -->
  <div class="ap-panel" :class="{ active: state.activeTab === 'accounts' }">
    <div v-if="!isAccountManager && !isLead" style="background:#fff;border:1px solid #f1f5f9;border-radius:12px;padding:48px;text-align:center">
      <i class="fa-solid fa-lock" style="font-size:30px;color:#cbd5e1"></i>
      <p style="margin-top:12px;color:#94a3b8;font-size:14px">账号列表仅对「开发人员 / 超级管理员」及各部门主管开放</p>
    </div>

    <template v-else>
      <div v-if="isLead && !isSuper" class="ap-scope-tip">
        <i class="fa-solid fa-circle-info"></i>
        您是该部门主管，仅可管理本部门「{{ scope.department }}」的账号，且只能授予不超出自身范围的权限
      </div>

      <div class="acct-deptbar">
        <button v-for="d in visibleDeptTabs" :key="d.key" class="acct-dept"
                :class="{ active: state.activeDept === d.key }" @click="switchDept(d.key)">
          {{ d.label }}<span class="cnt">{{ deptCounts[d.key] || 0 }}</span>
        </button>
      </div>

      <div class="ap-toolbar">
        <div class="ap-search-wrap">
          <i class="fa-solid fa-search"></i>
          <input class="ap-search-input" v-model="state.keyword" autocomplete="off" :readonly="searchLocked" @focus="unlockSearch" placeholder="搜索姓名 / 手机号 / 部门...">
        </div>
        <button class="ap-btn-primary" @click="openAcctModal()"><i class="fa-solid fa-plus"></i> 新增账号</button>
      </div>

      <div class="ap-table-wrap">
        <table class="ap-table">
          <thead>
            <tr>
              <th style="width:170px">姓名</th>
              <th style="width:170px">部门</th>
              <th style="width:140px">账号</th>
              <th style="width:140px">密码</th>
              <th style="width:110px">角色</th>
              <th style="width:80px">状态</th>
              <th style="width:200px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in filteredAccounts" :key="a.id">
              <td>
                <div class="acct-user">
                  <img v-if="a.avatar" class="acct-avatar" :src="a.avatar" alt="">
                  <span v-else class="acct-avatar-char">{{ (a.name || '?').charAt(0) }}</span>
                  <span><strong>{{ a.name }}</strong><span v-if="a.gender" class="acct-sex">{{ genderText(a.gender) }}</span></span>
                </div>
              </td>
              <td>
                <div>{{ a.department || '—' }}</div>
                <div class="acct-sub" v-if="a.subDept || a.leader">{{ a.subDept }}<span v-if="a.leader"> · 主管 {{ a.leader }}</span></div>
              </td>
              <td style="font-family:'SF Mono','Consolas',monospace;font-size:12px">{{ a.account }}</td>
              <td>
                <div class="ap-pwd-cell">
                  <span class="ap-pwd-text">{{ state.pwdVisible[a.id] ? (state.pwdCache[a.id] || '—') : '●●●●●●' }}</span>
                  <button class="ap-pwd-toggle" :class="{ showing: state.pwdVisible[a.id] }" @click="togglePwdVis(a.id)" :title="state.pwdVisible[a.id] ? '隐藏密码' : '显示密码'">
                    <i class="fa-solid" :class="state.pwdVisible[a.id] ? 'fa-eye-slash' : 'fa-eye'"></i>
                  </button>
                </div>
              </td>
              <td>{{ a.role }}</td>
              <td><span class="status-badge" :class="a.status">{{ a.status === 'enabled' ? '已启用' : '已禁用' }}</span></td>
              <td>
                <div class="ap-actions">
                  <button v-if="canTouchAccount(a) || a.isSelf" class="ap-btn-sm pwd" @click="openPwdModal(a.id)" title="修改密码"><i class="fa-solid fa-key"></i> 密码</button>
                  <button v-if="canTouchAccount(a)" class="ap-btn-sm edit" @click="openAcctModal(a.id)"><i class="fa-solid fa-pen"></i> 编辑</button>
                  <button v-if="canTouchAccount(a) && a.account !== 'admin'" class="ap-btn-sm toggle" @click="toggleAcct(a.id)" :title="a.status === 'enabled' ? '禁用' : '启用'"><i class="fa-solid fa-power-off"></i></button>
                  <button v-if="canTouchAccount(a) && a.account !== 'admin'" class="ap-btn-sm delete" @click="confirmDeleteAcct(a.id)"><i class="fa-solid fa-trash"></i></button>
                  <span v-if="a.isSelf" class="ap-self-tag">本人</span>
                </div>
              </td>
            </tr>
            <tr v-if="filteredAccounts.length === 0"><td colspan="7" style="text-align:center;padding:32px;color:#94a3b8">该部门下暂无账号</td></tr>
          </tbody>
        </table>
      </div>
      <div class="ap-table-info">共 {{ filteredAccounts.length }} 个账号<span v-if="state.activeDept !== '全部'">（部门：{{ state.activeDept }}）</span></div>
    </template>
  </div>

  <!-- ============ 角色与权限（仅 开发人员 / 超级管理员） ============ -->
  <div v-if="canManageRoles" class="ap-panel" :class="{ active: state.activeTab === 'roles' }">
    <div style="display:flex;gap:12px;margin-bottom:16px">
      <button class="ap-btn-primary" @click="openRolePermModal()"><i class="fa-solid fa-plus"></i> 新增角色</button>
    </div>
    <div class="ap-table-wrap">
      <table class="ap-table">
        <thead><tr><th style="width:180px">角色名称</th><th>权限</th><th style="width:100px">成员数</th><th style="width:130px">创建时间</th><th style="width:150px">操作</th></tr></thead>
        <tbody>
          <tr v-for="r in state.roles" :key="r.id">
            <td><strong>{{ r.name }}</strong></td>
            <td><span :style="{ color: rolePermText(r).color, fontWeight: rolePermText(r).text === '全部权限' ? '600' : 'normal' }">{{ rolePermText(r).text }}</span></td>
            <td>{{ r.count || 0 }}</td>
            <td style="color:#94a3b8;font-size:12px">{{ r.createdAt || '' }}</td>
            <td>
              <div class="ap-actions">
                <button class="ap-btn-sm edit" @click="openRolePermModal(r.id)"><i class="fa-solid fa-pen"></i> 权限</button>
                <button v-if="r.name !== '超级管理员'" class="ap-btn-sm delete" @click="confirmDeleteRole(r.id)"><i class="fa-solid fa-trash"></i></button>
              </div>
            </td>
          </tr>
          <tr v-if="state.roles.length === 0"><td colspan="5" style="text-align:center;padding:32px;color:#94a3b8">暂无角色数据</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 新增/编辑账号弹窗 -->
  <ecom-modal :visible="acctModal.visible" :title="acctModal.isEdit ? '编辑账号' : '新增账号'" width="680px" @close="acctModal.visible = false" @save="saveAcct">
    <div v-if="lockRole" class="ap-perm-hint">
      <i class="fa-solid fa-lock"></i>
      部门主管只能在本部门「{{ scope.department }}」新增「{{ scope.role }}」角色的账号，角色与部门已锁定
    </div>
    <div class="ap-form-row">
      <div class="ap-form-group"><label>姓名</label><input class="ap-form-input" v-model="acctModal.form.name" placeholder="请输入姓名"></div>
      <div class="ap-form-group"><label>手机号（登录账号）</label><input class="ap-form-input" v-model="acctModal.form.account" placeholder="请输入手机号" autocomplete="off"></div>
    </div>
    <div class="ap-form-row">
      <div class="ap-form-group"><label>密码</label><input class="ap-form-input" v-model="acctModal.form.password" :placeholder="acctModal.isEdit ? '留空则不修改' : '请输入密码'" autocomplete="off"></div>
      <div class="ap-form-group"><label>角色</label><select class="ap-form-input" v-model="acctModal.form.role" :disabled="lockRole">
        <option v-for="r in roleOptions" :key="r" :value="r">{{ r }}</option>
      </select></div>
    </div>
    <div class="ap-form-row">
      <div class="ap-form-group"><label>部门</label><input class="ap-form-input" v-model="acctModal.form.department" :readonly="lockDept" :placeholder="lockDept ? '' : '如：总裁办 / 业务一部'"></div>
      <div class="ap-form-group"><label>细分小组</label><input class="ap-form-input" v-model="acctModal.form.subDept" placeholder="如：一部三组"></div>
    </div>
    <div class="ap-form-row">
      <div class="ap-form-group"><label>部门主管</label><input class="ap-form-input" v-model="acctModal.form.leader" :readonly="lockRole" placeholder="如：马湘湘"></div>
      <div class="ap-form-group"><label>性别</label><select class="ap-form-input" v-model="acctModal.form.gender">
        <option value="">未设置</option><option value="male">男</option><option value="female">女</option>
      </select></div>
    </div>
    <div class="ap-form-group"><label>状态</label><select class="ap-form-input" v-model="acctModal.form.status" :disabled="acctModal.form.id && isLead && !isSuper"><option value="enabled">已启用</option><option value="disabled">已禁用</option></select></div>

    <div class="ap-perm-hint" v-if="lockRole">
      <i class="fa-solid fa-shield-halved"></i>
      新账号将获得「{{ scope.role }}」角色的权限：{{ (scope.perms || []).length ? scope.perms.length + ' 项' : '无' }}（不超出您自身的权限范围）
    </div>
  </ecom-modal>

  <!-- 修改密码弹窗 -->
  <ecom-modal :visible="pwdModal.visible" title="修改密码" @close="pwdModal.visible = false" @save="savePwd">
    <div class="ap-form-group"><label>账号</label><div style="padding:9px 0;font-weight:600;color:#1e293b">{{ pwdModal.acctLabel }}</div></div>
    <div class="ap-form-group"><label>新密码</label><input class="ap-form-input" v-model="pwdModal.password" placeholder="请输入新密码" autocomplete="off"></div>
  </ecom-modal>

  <!-- 角色权限弹窗 -->
  <ecom-modal :visible="roleModal.visible" :title="roleModal.isEdit ? '编辑角色与权限' : '新增角色'" width="760px" @close="roleModal.visible = false" @save="saveRolePerm">
    <div class="ap-form-group"><label>角色名称</label><input class="ap-form-input" v-model="roleModal.form.name" placeholder="请输入角色名称"></div>

    <div class="rp-head">
      <div class="rp-head-count">已选 <b>{{ permCount }}</b> / {{ ALL_PAGE_IDS.length }} 个板块</div>
      <div style="display:flex;gap:8px">
        <button type="button" class="rp-mini" @click="toggleAllPerms">{{ permAllChecked ? '取消全选' : '全选' }}</button>
        <button type="button" class="rp-mini" @click="invertPerms">反选</button>
      </div>
    </div>

    <div class="rp-scroll">
      <div class="rp-card" v-for="cat in PAGE_CATEGORIES" :key="cat.group">
        <div class="rp-card-head">
          <div class="rp-card-title"><span class="dot"></span>{{ cat.group }}</div>
          <label class="rp-card-all" @click.prevent="toggleGroup(cat)">
            <input type="checkbox" :checked="groupChecked(cat)" style="pointer-events:none"> 全选本组
          </label>
        </div>
        <div class="rp-card-body">
          <label class="rp-item" v-for="p in cat.pages" :key="p.id"
                 :class="{ on: roleModal.form.permissions.indexOf(p.id) >= 0, off: permOverLimit(p.id) }">
            <input type="checkbox" :value="p.id" v-model="roleModal.form.permissions" :disabled="permOverLimit(p.id)">
            <span>{{ p.name }}</span>
            <em v-if="permOverLimit(p.id)" class="rp-lock">超范围</em>
          </label>
        </div>
      </div>
    </div>

    <div class="rp-foot">
      <span>提示：「个人中心设置」对所有账号强制开放，无需勾选也可访问</span>
      <span>{{ roleModal.form.name || '未命名' }}</span>
    </div>
  </ecom-modal>

  <!-- 删除确认弹窗 -->
  <ecom-modal :visible="confirmBox.visible" title="确认删除" save-text="确认删除" :danger="true" @close="confirmBox.visible = false" @save="doConfirm">
    <div style="padding:4px 0">{{ confirmBox.message }}</div>
  </ecom-modal>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _adminApp = null;

  function mountAdminVue() {
    if (_adminApp) return;
    var oldSection = document.getElementById('page-admin-permissions');
    var mount = document.getElementById('page-admin-permissions-vue');
    if (!oldSection || !mount) return;
    oldSection.style.display = 'none';
    mount.classList.remove('hidden');
    _adminApp = Vue.createApp(AdminPage);
    _adminApp.mount(mount);
  }

  function unmountAdminVue() {
    if (!_adminApp) return;
    _adminApp.unmount();
    _adminApp = null;
    _state.keyword = '';
    _state.pwdVisible = {};
    _state.pwdCache = {};
    var mount = document.getElementById('page-admin-permissions-vue');
    if (mount) { mount.classList.add('hidden'); mount.innerHTML = ''; }
    var oldSection = document.getElementById('page-admin-permissions');
    if (oldSection) oldSection.style.display = '';
  }

  function installAdminHook() {
    var oldSection = document.getElementById('page-admin-permissions');
    if (!oldSection) return;
    if (!oldSection.classList.contains('hidden')) mountAdminVue();
    var observer = new MutationObserver(function () {
      if (!oldSection.classList.contains('hidden')) mountAdminVue();
      else unmountAdminVue();
    });
    observer.observe(oldSection, { attributes: true, attributeFilter: ['class'] });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installAdminHook);
  } else {
    installAdminHook();
  }
})();
