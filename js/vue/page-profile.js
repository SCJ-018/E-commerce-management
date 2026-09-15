/**
 * 个人中心设置页 — Vue 3 试点（阶段 2）
 * classic script（非 module），与 app.js 共用全局词法作用域：
 *   - 直接复用顶层 const：Vue（vue.global.js 注入）、App（app.js 顶层 const）
 *   - showToast 复用 App.showToast(...)
 *   - 本页为纯表单 + 静态日志，不涉及接口返回数据的动态 HTML，故无需 escapeHtml/esc
 * 状态同步：读取 sessionStorage（权威来源），保存时写回 sessionStorage + 顶栏 #currentUser DOM
 */
(function () {
  'use strict';

  // ==================== 静态数据（与旧 _pfRenderLog 一致） ====================
  var PROFILE_LOGS = [
    { time: '2026-08-04 09:15:32', type: '登录', detail: '管理员登录后台系统', ip: '192.168.1.100', device: 'Chrome / Windows' },
    { time: '2026-08-03 17:40:12', type: '修改', detail: '修改管理员「李运营」的角色为运营主管', ip: '192.168.1.100', device: 'Chrome / Windows' },
    { time: '2026-08-03 14:22:08', type: '新增', detail: '新增管理员账号「wangcaiwu」', ip: '192.168.1.100', device: 'Chrome / Windows' },
    { time: '2026-08-03 11:05:45', type: '导出', detail: '导出营销数据报表（2026-07-01 ~ 2026-07-31）', ip: '192.168.1.100', device: 'Chrome / Windows' },
    { time: '2026-08-02 16:30:18', type: '设置', detail: '修改系统通知设置', ip: '192.168.1.100', device: 'Chrome / Windows' },
    { time: '2026-08-02 10:12:55', type: '登录', detail: '管理员登录后台系统', ip: '192.168.1.100', device: 'Chrome / Windows' },
    { time: '2026-08-01 15:48:33', type: '操作', detail: '生成每日数据分析报告（2026-07-31）', ip: '192.168.1.100', device: 'Chrome / Windows' },
    { time: '2026-08-01 09:00:01', type: '登录', detail: '管理员登录后台系统', ip: '192.168.1.100', device: 'Chrome / Windows' },
    { time: '2026-07-31 18:20:10', type: '修改', detail: '修改了个人基本资料', ip: '192.168.1.105', device: 'Safari / macOS' },
    { time: '2026-07-31 14:55:40', type: '删除', detail: '删除商品「旧版充电线」', ip: '192.168.1.100', device: 'Chrome / Windows' },
  ];

  var NOTIFY_SETTINGS = [
    { name: '退款预警通知', desc: '退款率超过阈值时发送告警', checked: true },
    { name: '差评实时推送', desc: '收到差评时立即通知', checked: true },
    { name: '每日数据报告', desc: '每天早上9点推送昨日数据摘要', checked: true },
    { name: '系统更新通知', desc: '系统功能更新和维护通知', checked: false },
    { name: '营销活动提醒', desc: '大促活动开始/结束前的提醒', checked: true },
  ];

  // ==================== Vue 组件 ====================
  var ProfilePage = {
    template: `
<div>
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon" style="background:linear-gradient(135deg,#2563eb,#60a5fa);box-shadow:0 2px 8px rgba(37,99,235,0.25)"><i class="fa-solid fa-id-badge" style="color:#fff;font-size:18px"></i></div>
      <div class="dh-title-group"><h2 class="dh-title">个人中心设置</h2><span class="dh-subtitle">Account & Profile Settings</span></div>
    </div>
  </div>

  <!-- 标签页导航 -->
  <div class="ap-tabs">
    <button class="ap-tab" :class="{active: activeTab === 'profile-info'}" @click="activeTab = 'profile-info'">个人信息</button>
    <button class="ap-tab" :class="{active: activeTab === 'profile-security'}" @click="activeTab = 'profile-security'">账号安全</button>
    <button class="ap-tab" :class="{active: activeTab === 'profile-notify'}" @click="activeTab = 'profile-notify'">通知设置</button>
    <button class="ap-tab" :class="{active: activeTab === 'profile-log'}" @click="activeTab = 'profile-log'">操作日志</button>
  </div>

  <!-- 个人信息 -->
  <div class="ap-panel" :class="{active: activeTab === 'profile-info'}">
    <div style="display:grid;grid-template-columns:300px 1fr;gap:24px">
      <div style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;padding:32px 24px;text-align:center">
        <div style="width:100px;height:100px;border-radius:50%;background:linear-gradient(135deg,#3b82f6,#60a5fa);display:inline-flex;align-items:center;justify-content:center;font-size:42px;color:#fff;margin-bottom:16px;box-shadow:0 4px 16px rgba(59,130,246,0.3)">{{ avatarChar }}</div>
        <h3 style="font-size:18px;font-weight:700;color:#1e293b;margin-bottom:4px">{{ displayName }}</h3>
        <p style="font-size:13px;color:#94a3b8;margin-bottom:16px">{{ role }}</p>
        <button class="btn btn-sm btn-outline" style="width:100%" @click="changeAvatar"><i class="fa-solid fa-camera"></i> 更换头像</button>
        <div style="margin-top:20px;padding-top:16px;border-top:1px solid #f1f5f9;text-align:left">
          <div style="display:flex;justify-content:space-between;padding:6px 0;font-size:13px"><span style="color:#94a3b8">账号</span><span style="color:#334155;font-weight:500">{{ account }}</span></div>
          <div style="display:flex;justify-content:space-between;padding:6px 0;font-size:13px"><span style="color:#94a3b8">角色</span><span style="color:#334155;font-weight:500">{{ role }}</span></div>
          <div style="display:flex;justify-content:space-between;padding:6px 0;font-size:13px"><span style="color:#94a3b8">注册时间</span><span style="color:#334155;font-weight:500">{{ regDate }}</span></div>
          <div style="display:flex;justify-content:space-between;padding:6px 0;font-size:13px"><span style="color:#94a3b8">最后登录</span><span style="color:#334155;font-weight:500">{{ lastLogin }}</span></div>
        </div>
      </div>
      <div style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;padding:24px">
        <h3 style="font-size:16px;font-weight:600;color:#1e293b;margin-bottom:20px">基本资料</h3>
        <div class="form-row" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
          <div class="form-group"><label>姓名</label><input class="form-input" v-model.trim="form.name" placeholder="请输入姓名"></div>
          <div class="form-group"><label>手机号</label><input class="form-input" v-model="form.phone" placeholder="请输入手机号"></div>
        </div>
        <div class="form-row" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
          <div class="form-group"><label>邮箱</label><input class="form-input" v-model="form.email" placeholder="请输入邮箱"></div>
          <div class="form-group"><label>微信号</label><input class="form-input" v-model="form.wechat" placeholder="请输入微信号"></div>
        </div>
        <div class="form-group"><label>性别</label><select class="form-select" v-model="form.gender"><option value="male">男</option><option value="female">女</option></select></div>
        <div style="display:flex;gap:12px;margin-top:24px"><button class="btn btn-primary" @click="saveProfile">保存修改</button><button class="btn btn-outline" @click="resetProfile">重置</button></div>
      </div>
    </div>
  </div>

  <!-- 账号安全 -->
  <div class="ap-panel" :class="{active: activeTab === 'profile-security'}">
    <div style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;padding:24px;max-width:600px">
      <h3 style="font-size:16px;font-weight:600;color:#1e293b;margin-bottom:20px">修改密码</h3>
      <div class="form-group"><label>当前密码</label><div class="form-input-wrap"><input class="form-input" type="password" v-model="pwd.cur" placeholder="请输入当前密码"></div></div>
      <div class="form-group"><label>新密码</label><div class="form-input-wrap"><input class="form-input" type="password" v-model="pwd.n1" placeholder="8-20位，包含字母和数字"></div></div>
      <div class="form-group"><label>确认新密码</label><div class="form-input-wrap"><input class="form-input" type="password" v-model="pwd.n2" placeholder="再次输入新密码"></div></div>
      <button class="btn btn-primary" @click="changePassword">修改密码</button>
      <hr style="margin:28px 0;border-color:#f1f5f9">
      <h3 style="font-size:16px;font-weight:600;color:#1e293b;margin-bottom:16px">安全设置</h3>
      <div style="display:flex;flex-direction:column;gap:14px">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px;background:#f8fafc;border-radius:10px">
          <div><div style="font-weight:600;color:#1e293b;font-size:14px">两步验证 (2FA)</div><div style="font-size:12px;color:#94a3b8">通过手机验证码增加账号安全性</div></div>
          <span class="badge badge-gray">未启用</span>
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px;background:#f8fafc;border-radius:10px">
          <div><div style="font-weight:600;color:#1e293b;font-size:14px">登录设备管理</div><div style="font-size:12px;color:#94a3b8">当前 3 台设备已登录</div></div>
          <button class="btn btn-sm btn-outline">管理</button>
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px;background:#f8fafc;border-radius:10px">
          <div><div style="font-weight:600;color:#1e293b;font-size:14px">登录IP白名单</div><div style="font-size:12px;color:#94a3b8">限制仅允许指定IP登录后台</div></div>
          <span class="badge badge-gray">未设置</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 通知设置 -->
  <div class="ap-panel" :class="{active: activeTab === 'profile-notify'}">
    <div style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;padding:24px;max-width:600px">
      <h3 style="font-size:16px;font-weight:600;color:#1e293b;margin-bottom:20px">通知偏好</h3>
      <div style="display:flex;flex-direction:column;gap:12px">
        <div v-for="n in notifies" :key="n.name" style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#f8fafc;border-radius:10px">
          <div><div style="font-weight:600;color:#1e293b;font-size:14px">{{ n.name }}</div><div style="font-size:12px;color:#94a3b8">{{ n.desc }}</div></div>
          <label :class="{'pf-toggle-on': n.checked}" style="position:relative;display:inline-block;width:44px;height:24px">
            <input type="checkbox" v-model="n.checked" style="opacity:0;width:0;height:0">
            <span style="position:absolute;cursor:pointer;inset:0;background:#e2e8f0;border-radius:24px;transition:.3s"></span>
            <span style="position:absolute;height:18px;width:18px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s;box-shadow:0 1px 3px rgba(0,0,0,0.15)"></span>
          </label>
        </div>
      </div>
      <button class="btn btn-primary" style="margin-top:20px" @click="saveNotify">保存设置</button>
    </div>
  </div>

  <!-- 操作日志 -->
  <div class="ap-panel" :class="{active: activeTab === 'profile-log'}">
    <div style="background:#fff;border-radius:12px;border:1px solid #f1f5f9">
      <div style="padding:16px 20px;border-bottom:1px solid #f1f5f9;display:flex;align-items:center;gap:12px">
        <span style="font-weight:600;color:#1e293b;font-size:15px">最近操作记录</span>
        <span style="font-size:12px;color:#94a3b8">共 25 条记录</span>
      </div>
      <div style="padding:0">
        <table class="data-table"><thead><tr><th>时间</th><th>操作类型</th><th>详情</th><th>IP地址</th><th>设备</th></tr></thead>
          <tbody>
            <tr v-for="l in logs" :key="l.time">
              <td>{{ l.time }}</td>
              <td><span class="badge" :class="logTypeClass(l.type)">{{ l.type }}</span></td>
              <td>{{ l.detail }}</td>
              <td style="font-family:monospace;font-size:12px">{{ l.ip }}</td>
              <td>{{ l.device }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="ps-table-footer"><span>第 1 / 3 页，共 25 条</span><div class="ps-pagination-btns"><button disabled><i class="fa-solid fa-chevron-left"></i></button><button class="active">1</button><button>2</button><button>3</button><button><i class="fa-solid fa-chevron-right"></i></button></div></div>
    </div>
  </div>
</div>
    `,
    data() {
      var u = sessionStorage.getItem('admin_current_user') || '';
      return {
        userName: u,
        account: sessionStorage.getItem('admin_current_account') || 'admin',
        role: sessionStorage.getItem('admin_current_role') || '超级管理员',
        regDate: '2026-06-01',
        lastLogin: new Date().toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }),
        activeTab: 'profile-info',
        form: {
          name: u || '张经理',
          phone: '13800138001',
          email: 'zhangjl@julang.com',
          wechat: 'zhang_manager',
          gender: 'male',
        },
        pwd: { cur: '', n1: '', n2: '' },
        logs: PROFILE_LOGS,
        notifies: NOTIFY_SETTINGS,
      };
    },
    computed: {
      displayName: function () { return this.userName || '管理员'; },
      avatarChar: function () { return this.userName ? this.userName.charAt(0) : '管'; },
    },
    methods: {
      saveProfile: function () {
        var name = (this.form.name || '').trim();
        if (!name) { App.showToast('请输入姓名', 'error'); return; }
        this.userName = name;
        sessionStorage.setItem('admin_current_user', name);
        var topbar = document.getElementById('currentUser');
        if (topbar) topbar.textContent = name;
        App.showToast('个人信息已保存', 'success');
      },
      resetProfile: function () {
        this.form.name = this.userName || '张经理';
        this.form.phone = '13800138001';
        this.form.email = 'zhangjl@julang.com';
        this.form.wechat = 'zhang_manager';
        App.showToast('已重置为原始数据', 'success');
      },
      changeAvatar: function () {
        App.showToast('头像上传功能开发中', 'error');
      },
      changePassword: function () {
        if (!this.pwd.cur) { App.showToast('请输入当前密码', 'error'); return; }
        if (!this.pwd.n1 || this.pwd.n1.length < 6) { App.showToast('新密码至少6位', 'error'); return; }
        if (this.pwd.n1 !== this.pwd.n2) { App.showToast('两次密码不一致', 'error'); return; }
        App.showToast('密码修改成功', 'success');
        this.pwd.cur = '';
        this.pwd.n1 = '';
        this.pwd.n2 = '';
      },
      saveNotify: function () {
        App.showToast('通知设置已保存', 'success');
      },
      logTypeClass: function (type) {
        if (type === '登录') return 'badge-info';
        if (type === '修改' || type === '新增') return 'badge-success';
        if (type === '删除') return 'badge-danger';
        return 'badge-gray';
      },
    },
  };

  // ==================== 挂载 / 卸载 ====================
  var _profileApp = null;

  function mountProfileVue() {
    if (_profileApp) return;
    var oldSection = document.getElementById('page-profile');
    var mount = document.getElementById('page-profile-vue');
    if (!oldSection || !mount) return;
    // 隐藏旧页面：用 style.display 而非 class，避免触发 MutationObserver 递归
    oldSection.style.display = 'none';
    mount.classList.remove('hidden');
    _profileApp = Vue.createApp(ProfilePage);
    _profileApp.mount(mount);
  }

  function unmountProfileVue() {
    if (!_profileApp) return;
    _profileApp.unmount();
    _profileApp = null;
    var mount = document.getElementById('page-profile-vue');
    if (mount) {
      mount.classList.add('hidden');
      mount.innerHTML = '';
    }
    var oldSection = document.getElementById('page-profile');
    if (oldSection) oldSection.style.display = '';
  }

  // ==================== 新旧互斥钩子（MutationObserver） ====================
  function installProfileHook() {
    var oldSection = document.getElementById('page-profile');
    if (!oldSection) return;

    // 初始状态检查：处理刷新后 hash=#profile 直接落在该页的情况
    if (!oldSection.classList.contains('hidden')) {
      mountProfileVue();
    }

    var observer = new MutationObserver(function () {
      var visible = !oldSection.classList.contains('hidden');
      if (visible) mountProfileVue();
      else unmountProfileVue();
    });
    observer.observe(oldSection, { attributes: true, attributeFilter: ['class'] });
  }

  // app.js 的 App.init 也绑定在 DOMContentLoaded，此处在其之后安装即可（脚本顺序天然满足）
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installProfileHook);
  } else {
    installProfileHook();
  }
})();
