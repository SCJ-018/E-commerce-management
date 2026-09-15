// ==================== 管理员与权限 Vue 版 ====================
// 与旧版 App 内部逻辑保持一致，仅用 Vue 响应式重写渲染层。
// 管理员接口走 ApiService（已封装）；角色接口走 ApiService.getRoles/createRole/updateRole/deleteRole（本次新增）。
// 不复刻旧版「后端不可用降级 localStorage 假数据」逻辑，正常部署下行为一致。
(function () {
  if (typeof Vue === 'undefined' || typeof ApiService === 'undefined' || typeof App === 'undefined' || typeof EcomUI === 'undefined') return;

  // ---- 权限分配 UI 用的页面清单（静态常量，与旧版 PAGE_CATEGORIES 一致） ----
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
      { id: 'data-import', name: '数据导入' },
      { id: 'toolbox-violation-check', name: '违规词检测' },
    ]},
    { group: '系统管理', pages: [
      { id: 'admin-permissions', name: '管理员与权限' },
      { id: 'profile', name: '个人中心设置' },
    ]},
  ];

  // ---- 模块级状态（跨挂载/卸载保留） ----
  var _state = Vue.reactive({
    activeTab: 'admins',
    admins: [],
    roles: [],
    search: '',
    pwdVisible: {},
  });

  function allPageIds() {
    var ids = [];
    PAGE_CATEGORIES.forEach(function (cat) { cat.pages.forEach(function (p) { ids.push(p.id); }); });
    return ids;
  }

  var AdminPage = {
    components: { 'ecom-modal': EcomUI.Modal },
    setup: function () {
      // 超管判断（会话级，来自 sessionStorage，与旧版 state.currentRole/currentAccount 同源）
      var _role = sessionStorage.getItem('admin_current_role') || '';
      var _account = sessionStorage.getItem('admin_current_account') || '';
      var isSuperAdmin = _role === '超级管理员' || _account === 'admin';
      var isSuperAdminRole = _role === '超级管理员';

      // 搜索框防浏览器自动填充：初始 readonly，浏览器不会填充只读框；首次聚焦时解除
      var searchLocked = Vue.ref(true);
      function unlockSearch() { searchLocked.value = false; }

      // 弹窗状态
      var adminModal = Vue.reactive({ visible: false, isEdit: false, form: { id: '', name: '', account: '', password: '', role: '', status: 'enabled' } });
      var pwdModal = Vue.reactive({ visible: false, adminId: null, adminLabel: '', password: '' });
      var roleModal = Vue.reactive({ visible: false, isEdit: false, form: { id: '', name: '', permissions: [] }, allChecked: false });
      var confirmBox = Vue.reactive({ visible: false, message: '' });
      var _confirmAction = null;

      // ---- 数据加载 ----
      async function loadData() {
        var admins = await ApiService.getAdmins();
        if (admins && admins.length) _state.admins = admins;
        var roles = await ApiService.getRoles();
        if (roles && roles.length) _state.roles = roles;
        syncRoleCounts();
      }

      function syncRoleCounts() {
        _state.roles.forEach(function (r) {
          r.count = _state.admins.filter(function (a) { return a.role === r.name; }).length;
        });
      }

      var filteredAdmins = Vue.computed(function () {
        var kw = (_state.search || '').toLowerCase();
        if (!kw) return _state.admins;
        return _state.admins.filter(function (a) {
          return (a.name || '').toLowerCase().indexOf(kw) >= 0
            || (a.account || '').toLowerCase().indexOf(kw) >= 0
            || (a.role || '').toLowerCase().indexOf(kw) >= 0;
        });
      });

      function rolePermText(r) {
        var perms = r.permissions || [];
        if (Array.isArray(perms) && perms[0] === '*') return { text: '全部权限', color: '#16a34a' };
        if (perms.length > 0) return { text: perms.length + ' 项', color: '#6366f1' };
        return { text: '无权限', color: '#94a3b8' };
      }

      // ---- 管理员操作 ----
      function openAdminModal(id) {
        var admin = id ? _state.admins.find(function (a) { return a.id === id; }) : null;
        adminModal.isEdit = !!admin;
        adminModal.form = {
          id: admin ? admin.id : '',
          name: admin ? (admin.name || '') : '',
          account: admin ? (admin.account || '') : '',
          password: '',
          role: admin ? (admin.role || '') : (_state.roles.length > 0 ? _state.roles[0].name : ''),
          status: admin ? (admin.status || 'enabled') : 'enabled',
        };
        adminModal.visible = true;
      }

      async function saveAdmin() {
        var f = adminModal.form;
        if (!f.name.trim() || !f.account.trim() || (!adminModal.isEdit && !f.password.trim())) {
          App.showToast('请填写完整信息', 'error');
          return;
        }
        if (adminModal.isEdit) {
          var data = { name: f.name.trim(), role: f.role, status: f.status };
          if (f.password.trim()) data.password = f.password.trim();
          var r = await ApiService.updateAdmin(parseInt(f.id), data);
          if (r === null) { App.showToast('更新失败', 'error'); return; }
          App.showToast('管理员已更新', 'success');
        } else {
          var r2 = await ApiService.createAdmin({ name: f.name.trim(), account: f.account.trim(), password: f.password.trim(), role: f.role, status: f.status });
          if (r2 === null) { App.showToast('创建失败', 'error'); return; }
          App.showToast('管理员已创建', 'success');
        }
        adminModal.visible = false;
        loadData();
      }

      function togglePwdVis(id) {
        _state.pwdVisible[id] = !_state.pwdVisible[id];
      }

      function openPwdModal(id) {
        var admin = _state.admins.find(function (a) { return a.id === id; });
        if (!admin) return;
        pwdModal.adminId = id;
        pwdModal.adminLabel = admin.name + '（' + admin.account + '）';
        pwdModal.password = '';
        pwdModal.visible = true;
      }

      async function savePwd() {
        if (!pwdModal.password.trim()) { App.showToast('请输入新密码', 'error'); return; }
        var r = await ApiService.updateAdmin(parseInt(pwdModal.adminId), { password: pwdModal.password.trim() });
        if (r === null) { App.showToast('密码更新失败', 'error'); return; }
        pwdModal.visible = false;
        App.showToast('密码已更新', 'success');
        loadData();
      }

      function confirmDeleteAdmin(id) {
        var a = _state.admins.find(function (x) { return x.id === id; });
        if (!a) return;
        if (a.account === 'admin') { App.showToast('不能删除超级管理员账号', 'error'); return; }
        confirmBox.message = '确定删除管理员「' + a.name + '」吗？此操作不可恢复。';
        _confirmAction = async function () {
          await ApiService.deleteAdmin(id);
          confirmBox.visible = false;
          loadData();
          App.showToast('管理员已删除', 'success');
        };
        confirmBox.visible = true;
      }

      async function toggleAdmin(id) {
        var a = _state.admins.find(function (x) { return x.id === id; });
        if (!a) return;
        if (a.account === 'admin') { App.showToast('不能禁用超级管理员账号', 'error'); return; }
        var newStatus = a.status === 'enabled' ? 'disabled' : 'enabled';
        var r = await ApiService.updateAdmin(id, { status: newStatus });
        if (r === null) { App.showToast('操作失败', 'error'); return; }
        loadData();
        App.showToast('管理员已' + (newStatus === 'enabled' ? '启用' : '禁用'), 'success');
      }

      // ---- 角色操作 ----
      function openRolePermModal(id) {
        var role = id ? _state.roles.find(function (r) { return r.id === id; }) : null;
        roleModal.isEdit = !!role;
        var perms = role && role.permissions ? role.permissions : [];
        var isAll = Array.isArray(perms) && perms[0] === '*';
        roleModal.form = {
          id: role ? role.id : '',
          name: role ? (role.name || '') : '',
          permissions: isAll ? allPageIds() : perms.slice(),
        };
        roleModal.allChecked = isAll;
        roleModal.visible = true;
      }

      function toggleAllPerms() {
        if (roleModal.allChecked) {
          roleModal.form.permissions = [];
          roleModal.allChecked = false;
        } else {
          roleModal.form.permissions = allPageIds();
          roleModal.allChecked = true;
        }
      }

      async function saveRolePerm() {
        var f = roleModal.form;
        if (!f.name.trim()) { App.showToast('请输入角色名称', 'error'); return; }
        var data = { name: f.name.trim(), permissions: f.permissions };
        if (roleModal.isEdit) {
          var r = await ApiService.updateRole(parseInt(f.id), data);
          if (r === null) { App.showToast('更新失败', 'error'); return; }
          App.showToast('角色已更新', 'success');
        } else {
          var r2 = await ApiService.createRole(data);
          if (r2 === null) { App.showToast('创建失败', 'error'); return; }
          App.showToast('角色已创建', 'success');
        }
        roleModal.visible = false;
        loadData();
      }

      function confirmDeleteRole(id) {
        var role = _state.roles.find(function (r) { return r.id === id; });
        if (!role) return;
        if (role.name === '超级管理员') { App.showToast('不能删除超级管理员角色', 'error'); return; }
        confirmBox.message = '确定删除角色「' + role.name + '」吗？';
        _confirmAction = async function () {
          await ApiService.deleteRole(id);
          confirmBox.visible = false;
          loadData();
          App.showToast('角色已删除', 'success');
        };
        confirmBox.visible = true;
      }

      function doConfirm() {
        if (_confirmAction) _confirmAction();
      }

      loadData();

      return {
        state: _state,
        PAGE_CATEGORIES,
        adminModal, pwdModal, roleModal, confirmBox,
        isSuperAdmin, isSuperAdminRole,
        filteredAdmins, rolePermText, searchLocked, unlockSearch,
        openAdminModal, saveAdmin, togglePwdVis, openPwdModal, savePwd,
        confirmDeleteAdmin, toggleAdmin,
        openRolePermModal, toggleAllPerms, saveRolePerm, confirmDeleteRole, doConfirm,
      };
    },

    template: `
<div>
  <div class="ap-tabs">
    <button class="ap-tab" :class="{ active: state.activeTab === 'admins' }" @click="state.activeTab = 'admins'">管理员列表</button>
    <button class="ap-tab" :class="{ active: state.activeTab === 'roles' }" @click="state.activeTab = 'roles'">角色与权限</button>
  </div>

  <!-- 管理员面板 -->
  <div class="ap-panel" :class="{ active: state.activeTab === 'admins' }">
    <div class="ap-toolbar">
      <div class="ap-search-wrap">
        <i class="fa-solid fa-search"></i>
        <input class="ap-search-input" v-model="state.search" autocomplete="off" :readonly="searchLocked" @focus="unlockSearch" placeholder="搜索管理员姓名或账号...">
      </div>
      <button class="ap-btn-primary" @click="openAdminModal()"><i class="fa-solid fa-plus"></i> 新增管理员</button>
    </div>
    <div class="ap-table-wrap">
      <table class="ap-table">
        <thead>
          <tr>
            <th style="width:50px">ID</th>
            <th style="width:100px">姓名</th>
            <th style="width:130px">账号</th>
            <th style="width:150px">密码</th>
            <th style="width:110px">角色</th>
            <th style="width:80px">状态</th>
            <th style="width:140px">最后登录</th>
            <th style="width:160px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in filteredAdmins" :key="a.id">
            <td>{{ a.id }}</td>
            <td><strong>{{ a.name }}</strong></td>
            <td>{{ a.account }}</td>
            <td>
              <div class="ap-pwd-cell">
                <span class="ap-pwd-text">{{ isSuperAdmin ? (state.pwdVisible[a.id] ? a.password : '●●●●●●') : '●●●●●●' }}</span>
                <button v-if="isSuperAdmin" class="ap-pwd-toggle" :class="{ showing: state.pwdVisible[a.id] }" @click="togglePwdVis(a.id)" :title="state.pwdVisible[a.id] ? '隐藏密码' : '显示密码'">
                  <i class="fa-solid" :class="state.pwdVisible[a.id] ? 'fa-eye-slash' : 'fa-eye'"></i>
                </button>
              </div>
            </td>
            <td>{{ a.role }}</td>
            <td><span class="status-badge" :class="a.status">{{ a.status === 'enabled' ? '已启用' : '已禁用' }}</span></td>
            <td>{{ a.lastLogin || '-' }}</td>
            <td>
              <div class="ap-actions">
                <button v-if="isSuperAdmin" class="ap-btn-sm pwd" @click="openPwdModal(a.id)" title="修改密码"><i class="fa-solid fa-key"></i> 密码</button>
                <button class="ap-btn-sm edit" @click="openAdminModal(a.id)"><i class="fa-solid fa-pen"></i> 编辑</button>
                <button v-if="a.account !== 'admin'" class="ap-btn-sm toggle" @click="toggleAdmin(a.id)" :title="a.status === 'enabled' ? '禁用' : '启用'"><i class="fa-solid fa-power-off"></i></button>
                <button v-if="a.account !== 'admin'" class="ap-btn-sm delete" @click="confirmDeleteAdmin(a.id)"><i class="fa-solid fa-trash"></i></button>
              </div>
            </td>
          </tr>
          <tr v-if="filteredAdmins.length === 0"><td colspan="8" style="text-align:center;padding:32px;color:#94a3b8">暂无管理员数据</td></tr>
        </tbody>
      </table>
    </div>
    <div class="ap-table-info">共 {{ filteredAdmins.length }} 位管理员</div>
  </div>

  <!-- 角色面板 -->
  <div class="ap-panel" :class="{ active: state.activeTab === 'roles' }">
    <div style="display:flex;gap:12px;margin-bottom:16px">
      <button class="btn btn-primary btn-sm" @click="openRolePermModal()">+ 新增角色</button>
    </div>
    <div class="data-table-wrap">
      <table class="data-table">
        <thead><tr><th>角色名称</th><th>权限</th><th>成员数</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in state.roles" :key="r.id">
            <td><strong>{{ r.name }}</strong></td>
            <td><span :style="{ color: rolePermText(r).color, fontWeight: rolePermText(r).text === '全部权限' ? '600' : 'normal' }">{{ rolePermText(r).text }}</span></td>
            <td>{{ r.count || 0 }}</td>
            <td>{{ r.createdAt || '' }}</td>
            <td>
              <button v-if="isSuperAdminRole" class="btn btn-sm btn-outline" @click="openRolePermModal(r.id)"><i class="fa-solid fa-pen"></i> 权限</button>
              <button v-if="isSuperAdminRole && r.name !== '超级管理员'" class="btn btn-sm btn-outline" style="color:#dc2626" @click="confirmDeleteRole(r.id)"><i class="fa-solid fa-trash"></i></button>
            </td>
          </tr>
          <tr v-if="state.roles.length === 0"><td colspan="5" style="text-align:center;padding:32px;color:#94a3b8">暂无角色数据</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 新增/编辑管理员弹窗 -->
  <ecom-modal :visible="adminModal.visible" :title="adminModal.isEdit ? '编辑管理员' : '新增管理员'" @close="adminModal.visible = false" @save="saveAdmin">
    <div class="ap-form-row">
      <div class="ap-form-group"><label>姓名</label><input class="ap-form-input" v-model="adminModal.form.name" placeholder="请输入姓名"></div>
      <div class="ap-form-group"><label>账号</label><input class="ap-form-input" v-model="adminModal.form.account" placeholder="请输入账号" :readonly="adminModal.isEdit"></div>
    </div>
    <div class="ap-form-row">
      <div class="ap-form-group"><label>密码</label><input class="ap-form-input" type="text" v-model="adminModal.form.password" :placeholder="adminModal.isEdit ? '留空则不修改' : '请输入密码'"></div>
      <div class="ap-form-group"><label>角色</label><select class="ap-form-input" v-model="adminModal.form.role"><option v-for="r in state.roles" :key="r.id" :value="r.name">{{ r.name }}</option></select></div>
    </div>
    <div class="ap-form-group"><label>状态</label><select class="ap-form-input" v-model="adminModal.form.status"><option value="enabled">已启用</option><option value="disabled">已禁用</option></select></div>
  </ecom-modal>

  <!-- 修改密码弹窗 -->
  <ecom-modal :visible="pwdModal.visible" title="修改密码" @close="pwdModal.visible = false" @save="savePwd">
    <div class="ap-form-group"><label>管理员</label><div style="padding:9px 0;font-weight:600;color:#1e293b">{{ pwdModal.adminLabel }}</div></div>
    <div class="ap-form-group"><label>新密码</label><input class="ap-form-input" type="text" v-model="pwdModal.password" placeholder="请输入新密码"></div>
  </ecom-modal>

  <!-- 角色权限弹窗 -->
  <ecom-modal :visible="roleModal.visible" :title="roleModal.isEdit ? '编辑角色' : '新增角色'" width="640px" @close="roleModal.visible = false" @save="saveRolePerm">
    <div class="ap-form-group"><label>角色名称</label><input class="ap-form-input" v-model="roleModal.form.name" placeholder="请输入角色名称"></div>
    <div class="ap-perm-header"><span>权限分配</span><button type="button" class="ap-perm-toggle-btn" @click="toggleAllPerms">{{ roleModal.allChecked ? '取消全选' : '全选' }}</button></div>
    <div class="ap-perm-scroll">
      <div class="ap-perm-group" v-for="cat in PAGE_CATEGORIES" :key="cat.group">
        <div class="ap-perm-group-title">{{ cat.group }}</div>
        <div class="ap-perm-group-checks">
          <label class="ap-perm-check" v-for="p in cat.pages" :key="p.id">
            <input type="checkbox" :value="p.id" v-model="roleModal.form.permissions"> {{ p.name }}
          </label>
        </div>
      </div>
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
    _state.search = '';
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
