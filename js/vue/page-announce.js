// ==================== 工具箱 - 通告发放（Vue 版） ====================
// 功能：
//   ① 内容输入：文本（支持直接粘贴图片）、图片、办公文件（拖拽 / 点击选择）
//   ② 接收人：网站账号列表 / 网站整个部门 / 钉钉组织架构 / 钉钉联系人（姓名匹配）
//   ③ 发送：走后端 /api/announce/send → 钉钉机器人单聊（文本 markdown / 图片 sampleImage / 文件 sampleFile）
// ★ 2026-09-19：钉钉凭证由服务端统一下发（announce_config 内置默认值），前端配置页已下线；
//   钉钉名单（组织架构/联系人）由服务端每天 08:30 自动刷新并落库，前端进页面只读缓存，
//   无任何「同步 / 加载」按钮，也不需要人工点击。
(function () {
  if (typeof Vue === 'undefined' || typeof ApiService === 'undefined' || typeof App === 'undefined') return;

  var _nextId = 1;

  var _state = Vue.reactive({
    // ---- 内容 ----
    title: '',
    text: '',
    beautifying: false,
    images: [],      // {id, file(markRaw), name, size, dataUrl}
    docs: [],        // {id, file(markRaw), name, size}
    dragOver: false,
    // ---- 接收人 ----
    picked: [],      // {key, source, name, userId, mobile, dept}
    activeTab: 'accounts',
    // ---- 网站账号数据源 ----
    optionsLoaded: false,
    optionsLoading: false,
    optionsError: '',
    accounts: [],
    departments: [],
    accountKw: '',
    // ---- 钉钉名单（服务端每天 08:30 自动刷新并落库，前端只读） ----
    roster: null,            // {ready, userCount, deptCount, syncedAt}
    contacts: null,          // {departments, users, syncedAt}
    contactsLoading: false,
    contactsError: '',
    contactKw: '',
    expanded: {},
    // ---- 发送 ----
    sending: false,
    result: null,    // {ok, msg, data}
  });

  // ---- 工具 ----
  function fmtSize(n) {
    if (n == null) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function fileIcon(name) {
    var ext = (name.split('.').pop() || '').toLowerCase();
    if (['doc', 'docx'].indexOf(ext) >= 0) return 'fa-regular fa-file-word';
    if (['xls', 'xlsx', 'csv'].indexOf(ext) >= 0) return 'fa-regular fa-file-excel';
    if (['ppt', 'pptx'].indexOf(ext) >= 0) return 'fa-regular fa-file-powerpoint';
    if (ext === 'pdf') return 'fa-regular fa-file-pdf';
    if (['zip', 'rar', '7z'].indexOf(ext) >= 0) return 'fa-regular fa-file-zipper';
    return 'fa-regular fa-file-lines';
  }

  // 钉钉图片消息仅支持 jpg/png，其余格式画到 canvas 转成 jpeg 再上传
  function toSupportedBlob(file) {
    if (/image\/(jpeg|png)/.test(file.type || '')) return Promise.resolve(file);
    return new Promise(function (resolve) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        try {
          var canvas = document.createElement('canvas');
          canvas.width = img.naturalWidth || img.width;
          canvas.height = img.naturalHeight || img.height;
          canvas.getContext('2d').drawImage(img, 0, 0);
          canvas.toBlob(function (blob) {
            resolve(blob || file);
          }, 'image/jpeg', 0.92);
        } catch (e) { resolve(file); }
        finally { URL.revokeObjectURL(url); }
      };
      img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }

  function tsName(ext) {
    var d = new Date();
    var p = function (v) { return (v < 10 ? '0' : '') + v; };
    return '剪贴板_' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate())
      + '_' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '.' + ext;
  }

  // ---- 附件 ----
  function addFiles(files) {
    if (!files || !files.length) return;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      if (!f) continue;
      var isImage = (f.type || '').indexOf('image/') === 0;
      if (isImage && f.size > 10 * 1024 * 1024) {
        _state.result = { ok: false, msg: '图片「' + f.name + '」超过 10MB 上限，请压缩后再添加' };
        continue;
      }
      if (!isImage && f.size > 20 * 1024 * 1024) {
        _state.result = { ok: false, msg: '附件「' + f.name + '」超过 20MB 上限，请拆分后再添加' };
        continue;
      }
      if (isImage) {
        (function (file) {
          var item = Vue.reactive({
            id: _nextId++, file: Vue.markRaw(file),
            name: file.name || tsName('png'), size: file.size, dataUrl: '',
          });
          _state.images.push(item);
          var reader = new FileReader();
          reader.onload = function (e) { item.dataUrl = e.target.result; };
          reader.readAsDataURL(file);
        })(f);
      } else {
        _state.docs.push({
          id: _nextId++, file: Vue.markRaw(f),
          name: f.name || ('附件_' + _nextId), size: f.size,
        });
      }
    }
  }

  // 粘贴图片（textarea / 编辑卡片任意位置）
  function onPaste(e) {
    var items = (e.clipboardData || {}).items || [];
    var files = [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].kind === 'file' && (items[i].type || '').indexOf('image/') === 0) {
        var f = items[i].getAsFile();
        if (f) files.push(new File([f], f.name && f.name.indexOf('.') >= 0 ? f.name : tsName('png'), { type: f.type }));
      }
    }
    if (files.length) {
      e.preventDefault();
      addFiles(files);
    }
  }

  function onDrop(e) {
    e.preventDefault();
    _state.dragOver = false;
    var files = [];
    if (e.dataTransfer && e.dataTransfer.files) {
      for (var i = 0; i < e.dataTransfer.files.length; i++) files.push(e.dataTransfer.files[i]);
    }
    addFiles(files);
  }

  // ---- 接收人 ----
  function pickKey(p) {
    return p.userId ? 'uid:' + p.userId : 'nm:' + (p.name || '');
  }

  function addPicked(p) {
    p.key = pickKey(p);
    var dup = _state.picked.some(function (x) { return x.key === p.key; });
    if (!dup) _state.picked.push(p);
  }

  function removePicked(key) {
    _state.picked = _state.picked.filter(function (x) { return x.key !== key; });
  }

  function isPicked(p) {
    var key = pickKey(p);
    return _state.picked.some(function (x) { return x.key === key; });
  }

  function toggleAccount(acc) {
    var p = { source: 'account', name: acc.name, userId: '', mobile: '' };
    if (isPicked(p)) removePicked(pickKey(p));
    else addPicked(p);
  }

  function accountsInDept(dept) {
    return _state.accounts.filter(function (a) {
      return (a.department || '') === dept;
    });
  }

  function isDeptPicked(dept) {
    var list = accountsInDept(dept);
    if (!list.length) return false;
    return list.every(function (a) {
      return isPicked({ source: 'dept', name: a.name });
    });
  }

  function toggleDept(dept) {
    var list = accountsInDept(dept);
    if (!list.length) return;
    if (isDeptPicked(dept)) {
      list.forEach(function (a) { removePicked(pickKey({ source: 'dept', name: a.name })); });
    } else {
      list.forEach(function (a) {
        var p = { source: 'dept', name: a.name, userId: '', mobile: '', dept: dept };
        if (!isPicked(p)) addPicked(p);
      });
    }
  }

  function toggleDingUser(u) {
    var p = { source: 'dingtalk', name: u.name, userId: u.userid, mobile: '' };
    var key = pickKey(p);
    if (isPicked(p)) removePicked(key);
    else addPicked(p);
  }

  function togglePushUser(u) {
    var p = { source: 'contact', name: u.name, userId: u.userId || '', mobile: u.mobile || '' };
    if (isPicked(p)) removePicked(pickKey(p));
    else addPicked(p);
  }

  // ---- 数据加载（进页面自动执行，前端无任何「加载/同步」入口） ----
  async function loadOptions() {
    if (_state.optionsLoading) return;
    _state.optionsLoading = true;
    _state.optionsError = '';
    try {
      var data = await ApiService.getAnnounceOptions();
      if (data) {
        _state.accounts = data.accounts || [];
        _state.departments = data.departments || [];
        if (data.roster) _state.roster = data.roster;
        _state.optionsLoaded = true;
      } else if (!_state.optionsLoaded) {
        _state.optionsError = '账号列表加载失败，请刷新页面重试';
      }
    } catch (e) {
      if (!_state.optionsLoaded) {
        _state.optionsError = (e && e.message) || '账号列表加载失败，请刷新页面重试';
      }
    } finally {
      _state.optionsLoading = false;
    }
  }

  // 读取服务端已生成好的钉钉名单（后端落库 + 每日 08:30 自动更新，此处只读，秒回）
  async function syncContacts() {
    if (_state.contactsLoading) return;
    _state.contactsLoading = true;
    _state.contactsError = '';
    var r = await ApiService.getAnnounceContacts();
    _state.contactsLoading = false;
    if (!r.ok) {
      _state.contactsError = r.msg || '名单读取失败';
      _state.contacts = null;
      return;
    }
    _state.contacts = r.data;
    if (r.data) {
      _state.roster = {
        ready: true, userCount: r.data.userCount || (r.data.users || []).length,
        deptCount: r.data.deptCount || (r.data.departments || []).length,
        syncedAt: r.data.syncedAt || '',
      };
    }
    // 默认展开根部门
    var ex = {};
    ((r.data || {}).departments || []).forEach(function (d) {
      if (d.parentId === 0 || d.parentId === 1) ex[d.deptId] = true;
    });
    _state.expanded = ex;
  }

  // 进页面自动拉取（账号列表 + 钉钉名单），失败自动重试一次
  function autoLoad() {
    loadOptions();
    syncContacts();
    setTimeout(function () {
      if (!_state.optionsLoaded && !_state.optionsLoading) loadOptions();
      if (!_state.contacts && !_state.contactsLoading && !_state.contactsError) syncContacts();
    }, 2500);
  }

  // ---- 组织架构树（扁平化渲染，避免递归组件） ----
  var deptChildren = Vue.computed(function () {
    var map = {};
    ((_state.contacts || {}).departments || []).forEach(function (d) {
      (map[d.parentId] = map[d.parentId] || []).push(d);
    });
    return map;
  });

  var usersByDept = Vue.computed(function () {
    var map = {};
    ((_state.contacts || {}).users || []).forEach(function (u) {
      (u.deptIds || []).forEach(function (did) {
        (map[did] = map[did] || []).push(u);
      });
    });
    return map;
  });

  function collectDeptUserIds(nodes, out) {
    out = out || [];
    (nodes || []).forEach(function (n) {
      (usersByDept.value[n.deptId] || []).forEach(function (u) { out.push(u); });
      collectDeptUserIds(deptChildren.value[n.deptId] || [], out);
    });
    return out;
  }

  function pickDeptWhole(node) {
    var users = collectDeptUserIds([node]);
    users.forEach(function (u) {
      addPicked({ source: 'dingtalk', name: u.name, userId: u.userid, mobile: '' });
    });
    if (!users.length) {
      _state.result = { ok: false, msg: '部门「' + node.name + '」下没有可选取的成员' };
    }
  }

  var treeRows = Vue.computed(function () {
    var rows = [];
    var roots = deptChildren.value[0] || [];
    (function walk(nodes, depth) {
      nodes.forEach(function (n) {
        var open = !!_state.expanded[n.deptId];
        rows.push({ type: 'dept', node: n, depth: depth, open: open });
        if (open) {
          (usersByDept.value[n.deptId] || []).forEach(function (u) {
            rows.push({ type: 'user', user: u, depth: depth + 1 });
          });
          walk(deptChildren.value[n.deptId] || [], depth + 1);
        }
      });
    })(roots, 0);
    return rows;
  });

  var filteredAccounts = Vue.computed(function () {
    var kw = (_state.accountKw || '').trim().toLowerCase();
    if (!kw) return _state.accounts;
    return _state.accounts.filter(function (a) {
      return (a.name + ' ' + (a.account || '') + ' ' + (a.department || '') + ' ' + (a.role || '')).toLowerCase().indexOf(kw) >= 0;
    });
  });

  var filteredContacts = Vue.computed(function () {
    var kw = (_state.contactKw || '').trim().toLowerCase();
    var out = [];
    var seen = {};
    if (_state.contacts && _state.contacts.users) {
      _state.contacts.users.forEach(function (u) {
        if (kw && (u.name || '').toLowerCase().indexOf(kw) < 0) return;
        var key = u.userid || ('n:' + u.name);
        if (seen[key]) return;
        seen[key] = 1;
        out.push({ name: u.name, userId: u.userid, mobile: '', from: '钉钉通讯录' });
      });
    }
    return out.slice(0, 200);
  });

  // ---- 发送 ----
  async function beautifyText() {
    var text = (_state.text || '').trim();
    if (!text || _state.beautifying) return;
    _state.beautifying = true;
    try {
      var r = await ApiService.beautifyAnnouncement(text);
      if (!r.ok || !r.data || !r.data.text) {
        App.showToast((r && r.msg) || 'AI 美化失败，请稍后重试', 'error');
        return;
      }
      _state.text = r.data.text;
      App.showToast('已完成 AI 美化，请确认后发送', 'success');
    } finally {
      _state.beautifying = false;
    }
  }

  async function send() {
    if (_state.sending) return;
    if (!_state.title.trim() && !_state.text.trim() && !_state.images.length && !_state.docs.length) {
      _state.result = { ok: false, msg: '通告内容为空：请填写文字、粘贴图片或添加附件' };
      return;
    }
    if (!_state.picked.length) {
      _state.result = { ok: false, msg: '请先选择接收人' };
      return;
    }
    _state.sending = true;
    _state.result = null;
    try {
      var fd = new FormData();
      var payload = {
        title: _state.title.trim(),
        text: _state.text,
        recipients: _state.picked.map(function (p) {
          return { source: p.source, name: p.name, userId: p.userId || '', mobile: p.mobile || '' };
        }),
      };
      fd.append('payload', JSON.stringify(payload));
      for (var i = 0; i < _state.images.length; i++) {
        var blob = await toSupportedBlob(_state.images[i].file);
        fd.append('files', blob, _state.images[i].name);
      }
      for (var j = 0; j < _state.docs.length; j++) {
        fd.append('files', _state.docs[j].file, _state.docs[j].name);
      }
      var res = await fetch('/api/announce/send', {
        method: 'POST', body: fd, credentials: 'same-origin',
      });
      if (res.status === 401) {
        sessionStorage.removeItem('admin_logged_in');
        location.reload();
        return;
      }
      var json = await res.json().catch(function () { return null; });
      if (!json) throw new Error('HTTP ' + res.status);
      if (json.code !== 0) {
        _state.result = { ok: false, msg: json.msg || '发送失败' };
        return;
      }
      _state.result = { ok: true, msg: json.msg || '发送成功', data: json.data };
    } catch (e) {
      _state.result = { ok: false, msg: (e && e.message) || '网络异常，发送失败' };
    } finally {
      _state.sending = false;
    }
  }

  function clearContent() {
    _state.title = '';
    _state.text = '';
    _state.images = [];
    _state.docs = [];
  }

  // ==================== 组件 ====================
  var AnnouncePage = {
    name: 'AnnouncePage',
    setup: function () {
      Vue.onMounted(function () { autoLoad(); });

      return {
        state: _state,
        fmtSize: fmtSize,
        fileIcon: fileIcon,
        onPaste: onPaste,
        onDrop: onDrop,
        addFiles: addFiles,
        clearContent: clearContent,
        toggleAccount: toggleAccount,
        toggleDept: toggleDept,
        isDeptPicked: isDeptPicked,
        toggleDingUser: toggleDingUser,
        togglePushUser: togglePushUser,
        removePicked: removePicked,
        isPicked: isPicked,
        syncContacts: syncContacts,
        loadOptions: loadOptions,
        autoLoad: autoLoad,
        treeRows: treeRows,
        filteredAccounts: filteredAccounts,
        filteredContacts: filteredContacts,
        pickDeptWhole: pickDeptWhole,
        beautifyText: beautifyText,
        send: send,
        srcLabel: function (s) {
          return { account: '网站账号', dept: '网站部门', dingtalk: '钉钉', contact: '钉钉联系人' }[s] || s;
        },
        pickFile: function () {
          var input = document.getElementById('anFileInput');
          if (input) input.click();
        },
      };
    },
    template: `
<div class="an-wrap" @paste="onPaste">
  <!-- 主体双栏（钉钉凭证由服务端统一下发，前端不再有配置页） -->
  <div class="an-main-grid">
    <!-- 左栏：通告内容 -->
    <div>
      <div class="an-card">
        <div class="an-card-head">
          <span class="an-card-title"><i class="fa-solid fa-pen-to-square" style="color:#6366f1"></i> 通告内容</span>
          <span class="an-card-hint">支持文本 / 图片（可直接粘贴）/ 办公文件</span>
        </div>
        <div class="an-card-body">
          <div class="an-field">
            <label>通告标题</label>
            <input class="an-input" v-model="state.title" maxlength="60" placeholder="例如：关于国庆放假安排的通知">
          </div>
          <div class="an-field">
            <label>文字内容（在此粘贴图片可直接添加为附件）</label>
            <div style="position:relative">
              <textarea class="an-textarea" v-model="state.text" style="padding-bottom:42px"
                placeholder="输入通告正文…&#10;提示：截图后直接 Ctrl+V 即可把图片粘贴进来，与文字一并发送"></textarea>
              <button type="button" @click="beautifyText" :disabled="!state.text.trim() || state.beautifying"
                      title="一键 AI 美化通告内容" aria-label="一键 AI 美化通告内容"
                      style="position:absolute;right:10px;bottom:10px;width:30px;height:30px;border:0;border-radius:8px;background:#eef2ff;color:#4f46e5;cursor:pointer;font-size:14px;box-shadow:0 1px 3px rgba(79,70,229,.18)">
                <i class="fa-solid" :class="state.beautifying ? 'fa-spinner fa-spin' : 'fa-wand-magic-sparkles'"></i>
              </button>
            </div>
          </div>
          <div class="an-field">
            <label>图片与附件</label>
            <div class="an-drop" :class="{over: state.dragOver}"
                 @click="pickFile()"
                 @dragover.prevent="state.dragOver = true"
                 @dragleave.prevent="state.dragOver = false"
                 @drop="onDrop">
              <div class="an-drop-icon"><i class="fa-solid fa-cloud-arrow-up"></i></div>
              <div class="an-drop-main">拖拽文件到此处，或<b>点击选择</b></div>
              <div class="an-drop-sub">图片（jpg/png/webp 等，≤10MB）· 办公文件（doc/docx/xls/xlsx/ppt/pptx/pdf 等，≤20MB）</div>
            </div>
            <input type="file" id="anFileInput" multiple style="display:none"
                   accept="image/*,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.pdf,.txt,.csv,.zip,.rar"
                   @change="addFiles($event.target.files); $event.target.value = ''">
            <div class="an-att-list" v-if="state.images.length || state.docs.length">
              <div class="an-att-img" v-for="img in state.images" :key="img.id" :title="img.name">
                <img v-if="img.dataUrl" :src="img.dataUrl" alt="">
                <button class="an-att-del" @click="state.images = state.images.filter(x => x.id !== img.id)" title="移除"><i class="fa-solid fa-xmark"></i></button>
              </div>
              <div class="an-att-file" v-for="doc in state.docs" :key="doc.id">
                <i :class="fileIcon(doc.name)"></i>
                <span class="an-att-name" :title="doc.name">{{ doc.name }}</span>
                <span class="an-att-size">{{ fmtSize(doc.size) }}</span>
                <button class="an-att-x" @click="state.docs = state.docs.filter(x => x.id !== doc.id)" title="移除"><i class="fa-solid fa-xmark"></i></button>
              </div>
            </div>
          </div>
        </div>
        <div class="an-send-bar">
          <span class="an-card-hint" v-if="state.images.length || state.docs.length">
            {{ state.images.length }} 张图片 · {{ state.docs.length }} 个附件
          </span>
          <button class="an-send-btn" :disabled="state.sending" @click="send()">
            <i class="fa-solid" :class="state.sending ? 'fa-spinner fa-spin' : 'fa-paper-plane'"></i>
            {{ state.sending ? '正在发送…' : '发送通告' }}
          </button>
        </div>
        <div class="an-send-result" v-if="state.result"
             :style="state.result.ok ? 'background:#f0fdf4;border:1px solid #bbf7d0;color:#15803d' : 'background:#fef2f2;border:1px solid #fecaca;color:#b91c1c'">
          <b><i class="fa-solid" :class="state.result.ok ? 'fa-circle-check' : 'fa-circle-xmark'"></i> {{ state.result.msg }}</b>
          <template v-if="state.result.data">
            <div style="margin-top:4px">成功 {{ state.result.data.okUsers }} / {{ state.result.data.totalUsers }} 位成员</div>
            <div v-if="state.result.data.resolveFails && state.result.data.resolveFails.length" style="margin-top:6px">
              <b>未匹配到钉钉账号的接收人：</b>
              <div v-for="f in state.result.data.resolveFails" :key="f">· {{ f }}</div>
            </div>
            <div v-if="state.result.data.sendDetails && state.result.data.sendDetails.length" style="margin-top:6px">
              <b>发送明细：</b>
              <div v-for="d in state.result.data.sendDetails" :key="d">· {{ d }}</div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 右栏：接收人 -->
    <div class="an-card">
      <div class="an-card-head">
        <span class="an-card-title"><i class="fa-solid fa-user-plus" style="color:#6366f1"></i> 接收人</span>
        <span class="an-card-hint">
          <template v-if="state.roster && state.roster.ready">
            <i class="fa-solid fa-circle-check" style="color:#16a34a"></i>
            钉钉名单 {{ state.roster.userCount }} 人 · 更新于 {{ state.roster.syncedAt }}
          </template>
          <template v-else-if="state.contactsError">
            <i class="fa-solid fa-circle-exclamation" style="color:#ea580c"></i> 钉钉名单读取异常
          </template>
          <template v-else><i class="fa-solid fa-spinner fa-spin"></i> 名单加载中…</template>
          · 已选 {{ state.picked.length }} 人
        </span>
      </div>
      <div class="an-tabs">
        <button class="an-tab" :class="{active: state.activeTab === 'accounts'}" @click="state.activeTab = 'accounts'">
          <i class="fa-solid fa-list-check"></i> 账号列表
        </button>
        <button class="an-tab" :class="{active: state.activeTab === 'dept'}" @click="state.activeTab = 'dept'">
          <i class="fa-solid fa-sitemap"></i> 整个部门
        </button>
        <button class="an-tab" :class="{active: state.activeTab === 'org'}" @click="state.activeTab = 'org'">
          <i class="fa-solid fa-building"></i> 钉钉组织架构
        </button>
        <button class="an-tab" :class="{active: state.activeTab === 'contact'}" @click="state.activeTab = 'contact'">
          <i class="fa-solid fa-address-book"></i> 钉钉联系人
        </button>
      </div>

      <!-- Tab 1：网站账号列表 -->
      <template v-if="state.activeTab === 'accounts'">
        <div class="an-search">
          <i class="fa-solid fa-magnifying-glass"></i>
          <input v-model="state.accountKw" placeholder="搜索姓名 / 账号 / 部门 / 角色">
        </div>
        <div class="an-empty" v-if="state.optionsLoading"><i class="fa-solid fa-spinner fa-spin"></i>正在自动加载账号列表…</div>
        <div class="an-notice err" v-else-if="state.optionsError">
          {{ state.optionsError }}<br>
          <button style="border:none;background:none;color:#1d4ed8;cursor:pointer;font-size:12.5px;padding:0" @click="loadOptions()">重新加载</button>
        </div>
        <div class="an-empty" v-else-if="!state.optionsLoaded"><i class="fa-solid fa-spinner fa-spin"></i>正在自动加载账号列表…</div>
        <div class="an-list" v-else>
          <div class="an-empty" v-if="!filteredAccounts.length"><i class="fa-solid fa-user-slash"></i>没有匹配的账号</div>
          <label class="an-row" v-for="a in filteredAccounts" :key="a.id">
            <input type="checkbox" :checked="isPicked({source:'account', name: a.name})" @change="toggleAccount(a)">
            <span class="an-row-name">{{ a.name }}</span>
            <span class="an-row-meta">{{ a.department }}<template v-if="a.subDept"> · {{ a.subDept }}</template></span>
          </label>
        </div>
      </template>

      <!-- Tab 2：网站整个部门 -->
      <template v-if="state.activeTab === 'dept'">
        <div class="an-notice info">点击部门即选择该部门下全部网站账号（按姓名匹配钉钉账号后发送）</div>
        <div class="an-card-body" style="padding-top:8px">
          <div class="an-empty" v-if="!state.departments.length && state.optionsLoaded"><i class="fa-solid fa-folder-open"></i>暂无部门数据</div>
          <button class="an-dept-chip" :class="{picked: isDeptPicked(d)}" v-for="d in state.departments" :key="d" @click="toggleDept(d)">
            <i class="fa-solid fa-users"></i> {{ d }}
            <i class="fa-solid" :class="isDeptPicked(d) ? 'fa-check' : 'fa-plus'"></i>
          </button>
        </div>
      </template>

      <!-- Tab 3：钉钉组织架构（服务端每日 08:30 自动更新，前端只读） -->
      <template v-if="state.activeTab === 'org'">
        <div class="an-card-body" style="padding-top:10px">
          <span class="an-card-hint" v-if="state.contactsLoading"><i class="fa-solid fa-spinner fa-spin"></i> 正在读取钉钉组织架构…</span>
          <span class="an-card-hint" v-else-if="state.contacts"><i class="fa-solid fa-circle-check" style="color:#16a34a"></i> 名单更新于 {{ state.contacts.syncedAt }}（服务端每日自动刷新，无需手动同步）</span>
        </div>
        <div class="an-notice err" v-if="state.contactsError">
          <b>不能实现该功能</b>：{{ state.contactsError }}<br>
          请联系管理员检查服务端钉钉凭证与通讯录权限。
        </div>
        <div class="an-list" v-if="state.contacts">
          <div class="an-empty" v-if="!treeRows.length"><i class="fa-solid fa-building-circle-exclamation"></i>组织架构为空</div>
          <template v-for="(row, idx) in treeRows" :key="(row.type === 'dept' ? 'd' : 'u') + idx">
            <div class="an-tree-dept" v-if="row.type === 'dept'" :class="{open: row.open}"
                 :style="{paddingLeft: (row.depth * 18) + 'px'}"
                 @click="state.expanded[row.node.deptId] = !state.expanded[row.node.deptId]">
              <i class="fa-solid fa-chevron-right an-tree-arrow"></i>
              <i class="fa-solid fa-folder" style="color:#f59e0b"></i>
              <span>{{ row.node.name }}</span>
              <button class="an-tree-add" @click.stop="pickDeptWhole(row.node)">整部门</button>
            </div>
            <label class="an-tree-user" v-else
                   :style="{paddingLeft: (row.depth * 18 + 14) + 'px'}">
              <input type="checkbox" :checked="isPicked({source:'dingtalk', name: row.user.name, userId: row.user.userid})" @change="toggleDingUser(row.user)">
              <span>{{ row.user.name }}</span>
              <span v-if="row.user.title" class="an-row-meta">{{ row.user.title }}</span>
            </label>
          </template>
        </div>
        <div class="an-empty" v-else-if="!state.contactsLoading && !state.contactsError">
          <i class="fa-solid fa-cloud-arrow-down"></i>正在读取服务端名单…
        </div>
      </template>

      <!-- Tab 4：钉钉联系人（服务端名单，按姓名搜索） -->
      <template v-if="state.activeTab === 'contact'">
        <div class="an-search">
          <i class="fa-solid fa-magnifying-glass"></i>
          <input v-model="state.contactKw" placeholder="输入姓名搜索钉钉联系人">
        </div>
        <div class="an-card-body" style="padding-top:4px">
          <span class="an-card-hint" v-if="state.contactsLoading"><i class="fa-solid fa-spinner fa-spin"></i> 正在读取钉钉联系人…</span>
          <span class="an-card-hint" v-else-if="state.contacts"><i class="fa-solid fa-circle-check" style="color:#16a34a"></i> 联系人名单 {{ (state.contacts.users || []).length }} 人 · 更新于 {{ state.contacts.syncedAt }}</span>
        </div>
        <div class="an-notice err" v-if="state.contactsError">
          <b>不能实现该功能</b>：{{ state.contactsError }}
        </div>
        <div class="an-notice info" v-if="!state.contacts && !state.contactsError">
          正在读取服务端名单，请稍候…
        </div>
        <div class="an-list" v-if="filteredContacts.length">
          <label class="an-row" v-for="(c, idx) in filteredContacts" :key="c.userId || (c.name + idx)">
            <input type="checkbox" :checked="isPicked({source:'contact', name: c.name, userId: c.userId})" @change="togglePushUser(c)">
            <span class="an-row-name">{{ c.name }}</span>
            <span class="an-row-meta">{{ c.from }}</span>
          </label>
        </div>
        <div class="an-empty" v-else-if="!state.contactsLoading && !state.contactsError">
          <i class="fa-solid fa-address-card"></i>{{ state.contactKw ? '没有匹配到联系人' : '联系人列表为空' }}
        </div>
      </template>

      <!-- 已选接收人 -->
      <div class="an-card-body" style="border-top:1px solid #f1f5f9">
        <div class="an-picked" v-if="state.picked.length">
          <span class="an-picked-chip" v-for="p in state.picked" :key="p.key">
            <span class="src">{{ srcLabel(p.source) }}</span>{{ p.name }}
            <button @click="removePicked(p.key)" title="移除"><i class="fa-solid fa-xmark"></i></button>
          </span>
        </div>
        <div class="an-empty" v-else style="padding:14px"><i class="fa-solid fa-user-plus"></i>尚未选择接收人</div>
      </div>
    </div>
  </div>
</div>`,
  };

  // ==================== 挂载（与违规词检测页同款：观察 section 显隐） ====================
  var _announceApp = null;

  function mountAnnounceVue() {
    if (_announceApp) return;
    var section = document.getElementById('page-toolbox-announce');
    var mount = document.getElementById('page-toolbox-announce-vue');
    if (!section || !mount) return;
    mount.classList.remove('hidden');
    _announceApp = Vue.createApp(AnnouncePage);
    _announceApp.mount(mount);
  }

  function unmountAnnounceVue() {
    if (!_announceApp) return;
    _announceApp.unmount();
    _announceApp = null;
    var mount = document.getElementById('page-toolbox-announce-vue');
    if (mount) { mount.classList.add('hidden'); mount.innerHTML = ''; }
  }

  function installAnnounceHook() {
    var section = document.getElementById('page-toolbox-announce');
    if (!section) return;
    if (!section.classList.contains('hidden')) mountAnnounceVue();
    var observer = new MutationObserver(function () {
      if (!section.classList.contains('hidden')) mountAnnounceVue();
      else unmountAnnounceVue();
    });
    observer.observe(section, { attributes: true, attributeFilter: ['class'] });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installAnnounceHook);
  } else {
    installAnnounceHook();
  }
})();
