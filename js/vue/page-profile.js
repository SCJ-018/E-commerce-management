/**
 * 个人中心设置页 — Vue 3
 * 只保留两块：「个人信息编辑」+「修改密码」，合并为同一页（无 Tab）。
 * 已按要求去掉：通知设置、操作日志、邮箱、微信号、安全设置模块。
 * 数据来源：GET /api/profile/me（改手机号/密码走 /api/profile/update|password，
 *           后端写的就是 admin_accounts 同一张表 → 账号列表天然同步）。
 */
(function () {
  'use strict';

  var GENDER_TEXT = { male: '男', female: '女' };

  var ProfilePage = {
    template: `
<div>
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon" style="background:linear-gradient(135deg,#2563eb,#60a5fa);box-shadow:0 2px 8px rgba(37,99,235,0.25)"><i class="fa-solid fa-id-badge" style="color:#fff;font-size:18px"></i></div>
      <div class="dh-title-group"><h2 class="dh-title">个人中心设置</h2><span class="dh-subtitle">Account &amp; Profile Settings</span></div>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:300px 1fr;gap:24px;align-items:start">
    <!-- 左：头像 + 账号概要 -->
    <div style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;padding:32px 24px;text-align:center">
      <div class="pf-av-wrap">
        <img v-if="avatar" class="pf-av-img" :src="avatar" alt="">
        <div v-else class="pf-av-char">{{ avatarChar }}</div>
        <div class="pf-av-mask" @click="pickAvatar" title="更换头像"><i class="fa-solid fa-camera"></i></div>
      </div>
      <h3 style="font-size:18px;font-weight:700;color:#1e293b;margin-bottom:4px">{{ form.name || '未命名' }}</h3>
      <p style="font-size:13px;color:#94a3b8;margin-bottom:16px">{{ role || '—' }}</p>
      <button class="btn btn-sm btn-outline" style="width:100%" :disabled="uploading" @click="pickAvatar">
        <i class="fa-solid" :class="uploading ? 'fa-spinner fa-spin' : 'fa-camera'"></i> {{ uploading ? '上传中...' : '更换头像' }}
      </button>
      <div style="font-size:11px;color:#cbd5e1;margin-top:8px">支持 png / jpg / webp / gif</div>
      <input ref="avatarInput" type="file" accept="image/png,image/jpeg,image/webp,image/gif" style="display:none" @change="onAvatarPick">

      <div style="margin-top:20px;padding-top:16px;border-top:1px solid #f1f5f9;text-align:left">
        <div class="pf-kv"><span class="pf-k">账号</span><span class="pf-v">{{ form.account || '—' }}</span></div>
        <div class="pf-kv"><span class="pf-k">角色</span><span class="pf-v">{{ role || '—' }}</span></div>
        <div class="pf-kv"><span class="pf-k">部门</span><span class="pf-v">{{ deptText }}</span></div>
        <div class="pf-kv"><span class="pf-k">注册时间</span><span class="pf-v">{{ regDate || '—' }}</span></div>
      </div>
    </div>

    <!-- 右：基本资料 + 修改密码 -->
    <div style="display:flex;flex-direction:column;gap:20px">
      <div style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;padding:24px">
        <h3 style="font-size:16px;font-weight:600;color:#1e293b;margin-bottom:20px">个人信息</h3>
        <div class="pf-row">
          <div class="form-group"><label>姓名</label><input class="form-input" v-model.trim="form.name" placeholder="请输入姓名"></div>
          <div class="form-group"><label>手机号（登录账号）</label><input class="form-input" v-model.trim="form.account" placeholder="请输入手机号" autocomplete="off"></div>
        </div>
        <div class="pf-row">
          <div class="form-group"><label>性别</label><select class="form-select" v-model="form.gender">
            <option value="">未设置</option><option value="male">男</option><option value="female">女</option>
          </select></div>
          <div class="form-group"><label>角色（不可修改）</label><input class="form-input" :value="role" readonly style="background:#f8fafc;color:#94a3b8"></div>
        </div>

        <div v-if="accountChanged" class="pf-warn">
          <i class="fa-solid fa-triangle-exclamation"></i>
          <div style="flex:1">
            <div style="font-weight:600;color:#b45309;font-size:13px">修改手机号需要验证当前密码</div>
            <input class="form-input" type="password" v-model="form.current_password" placeholder="请输入当前密码" autocomplete="new-password" style="margin-top:8px">
          </div>
        </div>

        <div style="display:flex;gap:12px;margin-top:24px">
          <button class="btn btn-primary" :disabled="saving" @click="saveProfile">{{ saving ? '保存中...' : '保存修改' }}</button>
          <button class="btn btn-outline" @click="resetProfile">重置</button>
        </div>
      </div>

      <div style="background:#fff;border-radius:12px;border:1px solid #f1f5f9;padding:24px">
        <h3 style="font-size:16px;font-weight:600;color:#1e293b;margin-bottom:6px">修改密码</h3>
        <p style="font-size:12px;color:#94a3b8;margin-bottom:18px">当前密码验证通过后才能修改，修改后请用新密码重新登录其他设备。</p>
        <div class="pf-row">
          <div class="form-group"><label>当前密码</label><input class="form-input" type="password" v-model="pwd.current" placeholder="请输入当前密码" autocomplete="new-password"></div>
          <div class="form-group"></div>
        </div>
        <div class="pf-row">
          <div class="form-group"><label>新密码</label><input class="form-input" type="password" v-model="pwd.n1" placeholder="至少 6 位" autocomplete="new-password"></div>
          <div class="form-group"><label>确认新密码</label><input class="form-input" type="password" v-model="pwd.n2" placeholder="再次输入新密码" autocomplete="new-password"></div>
        </div>
        <button class="btn btn-primary" :disabled="pwding" @click="changePassword">{{ pwding ? '提交中...' : '修改密码' }}</button>
      </div>
    </div>
  </div>
</div>
    `,
    data() {
      return {
        userName: sessionStorage.getItem('admin_current_user') || '',
        account: sessionStorage.getItem('admin_current_account') || '',
        role: sessionStorage.getItem('admin_current_role') || '',
        regDate: '',
        avatar: '',
        deptName: '',
        subDept: '',
        leader: '',
        avatarBase: '',
        uploading: false,
        saving: false,
        pwding: false,
        form: { name: '', account: '', gender: '', current_password: '' },
        pwd: { current: '', n1: '', n2: '' },
      };
    },
    computed: {
      avatarChar: function () { return this.form.name ? this.form.name.charAt(0) : '管'; },
      accountChanged: function () { return (this.form.account || '') !== (this.account || ''); },
      deptText: function () {
        if (!this.deptName) return '—';
        return this.subDept ? (this.deptName + ' · ' + this.subDept) : this.deptName;
      },
    },
    async mounted() {
      await this.loadProfile();
    },
    methods: {
      async loadProfile() {
        var d = await ApiService.getProfile();
        if (!d) {
          // 接口不可用时用 sessionStorage 兜底，至少能改密码的入口不消失
          this.form.name = this.userName || '';
          this.form.account = this.account || '';
          return;
        }
        this.userName = d.name || '';
        this.account = d.account || '';
        this.role = d.role || '';
        this.regDate = (d.createdAt || '').slice(0, 10);
        this.avatar = d.avatar || '';
        this.deptName = d.department || '';
        this.subDept = d.subDept || '';
        this.leader = d.leader || '';
        this.form.name = d.name || '';
        this.form.account = d.account || '';
        this.form.gender = d.gender || '';
        this.form.current_password = '';
        this.syncSession(d.name, d.account, d.role);
      },
      syncSession(name, account, role) {
        if (name) { sessionStorage.setItem('admin_current_user', name); }
        if (account) { sessionStorage.setItem('admin_current_account', account); }
        if (role) { sessionStorage.setItem('admin_current_role', role); }
        var topbar = document.getElementById('currentUser');
        if (topbar && name) topbar.textContent = name;
      },
      async saveProfile() {
        var name = (this.form.name || '').trim();
        if (!name) { App.showToast('请输入姓名', 'error'); return; }
        if (this.accountChanged && !(this.form.current_password || '').trim()) {
          App.showToast('修改手机号需要先输入当前密码', 'error');
          return;
        }
        this.saving = true;
        var r = await ApiService.updateProfile({
          name: name,
          account: (this.form.account || '').trim(),
          gender: this.form.gender || '',
          current_password: (this.form.current_password || '').trim(),
        });
        this.saving = false;
        if (!r || !r.ok) { App.showToast((r && r.msg) || '保存失败', 'error'); return; }
        this.userName = name;
        this.account = (r.data && r.data.account) || this.form.account;
        this.form.current_password = '';
        this.syncSession(this.userName, this.account, this.role);
        App.showToast('个人信息已保存', 'success');
      },
      resetProfile() {
        this.loadProfile();
        App.showToast('已重置', 'success');
      },
      pickAvatar() { if (this.$refs.avatarInput) this.$refs.avatarInput.click(); },
      async onAvatarPick(e) {
        var f = e.target.files && e.target.files[0];
        if (e.target) e.target.value = '';
        if (!f) return;
        if (f.size > 5 * 1024 * 1024) { App.showToast('图片不要超过 5MB', 'error'); return; }
        this.uploading = true;
        var r = await ApiService.uploadAvatar(f);
        this.uploading = false;
        if (!r || !r.ok) { App.showToast((r && r.msg) || '头像上传失败', 'error'); return; }
        this.avatar = (r.data && r.data.avatar) || '';
        App.showToast('头像已更新', 'success');
      },
      async changePassword() {
        if (!this.pwd.current) { App.showToast('请输入当前密码', 'error'); return; }
        if (!this.pwd.n1 || this.pwd.n1.length < 6) { App.showToast('新密码至少 6 位', 'error'); return; }
        if (this.pwd.n1 !== this.pwd.n2) { App.showToast('两次输入的新密码不一致', 'error'); return; }
        this.pwding = true;
        var r = await ApiService.updatePassword({ current_password: this.pwd.current, new_password: this.pwd.n1 });
        this.pwding = false;
        if (!r || !r.ok) { App.showToast((r && r.msg) || '修改失败', 'error'); return; }
        this.pwd.current = ''; this.pwd.n1 = ''; this.pwd.n2 = '';
        App.showToast('密码修改成功', 'success');
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
