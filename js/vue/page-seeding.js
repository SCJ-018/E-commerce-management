/**
 * 种草监测中台
 *
 * 这是一个独立的 Vue 页面，保留 app.js 的挂载入口，数据接口可按现有
 * ApiService 约定接入。页面状态集中在本文件，避免旧版账号抓取页面的状态
 * 与监测台互相污染。
 */
(function () {
  var departments = ['一部', '二部', '三部', '四部', '五部'];
  var noteTypes = ['测评', '种草', '干货', '引流', '实拍', '扣测'];
  var categoryOptions = [
    '汽车脚垫', '汽车坐垫', '汽车香薰', '去油膜', '汽车头枕', '汽车腰靠',
    '汽车车衣', '剃须刀', '爬楼机', '电动牙刷', '护腰坐垫', '西西猫店铺',
    '御车宝店铺', '所有店铺'
  ];
  var defaultBindings = {
    '一部': ['汽车脚垫', '汽车坐垫', '汽车香薰', '去油膜', '汽车头枕', '汽车腰靠', '西西猫店铺'],
    '二部': ['汽车脚垫', '汽车坐垫', '汽车香薰', '去油膜', '汽车头枕', '汽车腰靠', '西西猫店铺'],
    '三部': ['汽车脚垫', '汽车坐垫', '汽车车衣', '御车宝店铺'],
    '四部': ['剃须刀', '爬楼机', '所有店铺'],
    '五部': ['电动牙刷', '护腰坐垫', '所有店铺']
  };
  var bonusRules = [
    { type: '扣测', levels: '1万 / 5万 / 10万 / 20万 / 50万 / 100万', bonus: '20 / 30 / 50 / 100 / 200 / 500元' },
    { type: '种草', levels: '1万 / 5万 / 10万 / 20万 / 50万 / 100万', bonus: '10 / 20 / 30 / 50 / 100 / 200元' },
    { type: '引流', levels: '5万 / 10万 / 20万 / 50万 / 100万 / 200万', bonus: '10 / 15 / 20 / 30 / 50 / 100元' },
    { type: '干货', levels: '5万 / 10万 / 20万 / 50万 / 100万 / 200万', bonus: '10 / 15 / 20 / 30 / 50 / 100元' },
    { type: '测评、实拍', levels: '1万 / 5万 / 10万 / 20万 / 50万 / 100万', bonus: '20 / 30 / 50 / 100 / 200 / 500元' }
  ];
  var hotThreshold = { 扣测: 10000, 种草: 10000, 引流: 50000, 干货: 50000, 测评: 10000, 实拍: 10000 };

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function monthKey(date) {
    return String(date || '').slice(0, 7);
  }

  function currentMonth() {
    var now = new Date();
    return now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0');
  }

  function monthRange() {
    var now = new Date();
    var year = now.getFullYear();
    var month = now.getMonth();
    var lastDay = new Date(year, month + 1, 0).getDate();
    return {
      start: year + '-' + String(month + 1).padStart(2, '0') + '-01',
      end: year + '-' + String(month + 1).padStart(2, '0') + '-' + String(lastDay).padStart(2, '0')
    };
  }

  function safeLocal(key, fallback) {
    try {
      var parsed = JSON.parse(localStorage.getItem(key) || 'null');
      return parsed || fallback;
    } catch (e) {
      return fallback;
    }
  }

  var sampleRows = [
    { id: 1, dept: '五部', product: '电动牙刷', platform: '抖音', source: '代发', type: '干货', title: '尽我所能 让他成为世界上最幸福的人', link: 'https://v.douyin.com/UnUi-298Icw/', author: '王二喵', workId: '79435586085', likes: 1509, collects: 35, comments: 955, views: 100000, date: '2026-09-18', trafficImage: '' },
    { id: 2, dept: '五部', product: '电动牙刷', platform: '抖音', source: '代发', type: '实拍', title: '每天都要用的东西，真的值得买好一点', link: 'https://v.douyin.com/cdmWo1t3UGM/', author: '雪碧加冰', workId: 'xuebijiabing16', likes: 2, collects: 1, comments: 0, views: 1306, date: '2026-09-17', trafficImage: '' },
    { id: 3, dept: '五部', product: '电动牙刷', platform: '抖音', source: '代发', type: '实拍', title: '亲测好用！电动牙刷别乱买', link: 'https://v.douyin.com/-XyfdbOKRko/', author: '好好爱自己', workId: '35954318537', likes: 1, collects: 2, comments: 0, views: 265, date: '2026-09-16', trafficImage: '' },
    { id: 4, dept: '五部', product: '电动牙刷', platform: '抖音', source: '代发', type: '种草', title: '节日的礼物是礼物 日常的礼物是爱', link: 'https://v.douyin.com/IzmaTvIxHeA/', author: '1136', workId: '46529866626', likes: 2, collects: 1, comments: 0, views: 0, date: '2026-09-15', trafficImage: '' },
    { id: 5, dept: '五部', product: '电动牙刷', platform: '抖音', source: '代发', type: '干货', title: '拥有是一件幸福的事 永远别亏待了自己', link: 'https://v.douyin.com/QNudrzTTUSQ/', author: 'no cae', workId: '32705565727', likes: 321, collects: 25, comments: 57, views: 10000, date: '2026-09-14', trafficImage: '' },
    { id: 6, dept: '五部', product: '电动牙刷', platform: '抖音', source: '代发', type: '干货', title: '尽我所能 让他成为世界上最幸福的人', link: 'https://v.douyin.com/oKhrT6DGU04/', author: '王二喵', workId: '79435586085', likes: 18, collects: 5, comments: 3, views: 521, date: '2026-09-13', trafficImage: '' },
    { id: 7, dept: '五部', product: '电动牙刷', platform: '抖音', source: '代发', type: '实拍', title: '还在纠结电动牙刷怎么挑', link: 'https://v.douyin.com/r7LCIWMDZwM/', author: '好好爱自己', workId: '35954318537', likes: 73, collects: 1, comments: 0, views: 1166, date: '2026-09-12', trafficImage: '' },
    { id: 8, dept: '五部', product: '电动牙刷', platform: '抖音', source: '代发', type: '干货', title: '打工人租房好物清单(牛马省钱版)', link: 'https://v.douyin.com/MJoCguvDYlU/', author: '雨天', workId: '63546573071', likes: 1, collects: 2, comments: 0, views: 100, date: '2026-09-11', trafficImage: '' },
    { id: 9, dept: '一部', product: '汽车脚垫', platform: '抖音', source: '代发', type: '种草', title: '汽车脚垫这样选，耐脏又好打理', link: 'https://v.douyin.com/demo-one/', author: '小橙', workId: 'A1001', likes: 820, collects: 90, comments: 21, views: 68000, date: '2026-09-10', trafficImage: '' },
    { id: 10, dept: '二部', product: '汽车香薰', platform: '抖音', source: '代发', type: '测评', title: '车内香薰实测：三种味道对比', link: 'https://v.douyin.com/demo-two/', author: '林林', workId: 'B1002', likes: 420, collects: 36, comments: 14, views: 21000, date: '2026-09-09', trafficImage: '' },
    { id: 11, dept: '三部', product: '汽车车衣', platform: '抖音', source: '代发', type: '实拍', title: '车衣贴膜全过程记录', link: 'https://v.douyin.com/demo-three/', author: '阿杰', workId: 'C1003', likes: 1600, collects: 118, comments: 32, views: 120000, date: '2026-09-08', trafficImage: '' },
    { id: 12, dept: '四部', product: '剃须刀', platform: '抖音', source: '代发', type: '引流', title: '剃须刀选购避坑清单', link: 'https://v.douyin.com/demo-four/', author: '小周', workId: 'D1004', likes: 230, collects: 31, comments: 9, views: 56000, date: '2026-09-07', trafficImage: '' }
  ];

  var app = {
    setup: function () {
      var state = Vue.reactive({
        selectedDept: '全部',
        selectedPerson: '全部',
        rows: safeLocal('seeding-monitor-rows', clone(sampleRows)),
        people: {
          '一部': ['小橙', '苏苏'],
          '二部': ['林林', '小北'],
          '三部': ['阿杰', '小雨'],
          '四部': ['小周', '阿文'],
          '五部': ['王二喵', '雪碧加冰', '好好爱自己', '1136', 'no cae', '雨天']
        },
        categoryBindings: Object.assign(clone(defaultBindings), safeLocal('seeding-monitor-bindings', {})),
        dashboardViews: { all: 0, departments: {} },
        categoryPopup: false,
        peoplePopup: false,
        recordModal: false,
        trafficModal: false,
        trafficRow: null,
        editingId: null,
        syncState: '本地演示数据',
        lastSync: new Date().toLocaleString('zh-CN', { hour12: false }),
        form: {
          product: '', platform: '抖音', source: '代发', type: '种草', title: '', link: '',
          author: '', workId: '', likes: 0, collects: 0, comments: 0, shares: 0, views: 0, date: currentMonth() + '-22', trafficImage: ''
        }
      });

      var isSupervisor = Vue.computed(function () {
        var role = String(window.sessionStorage.getItem('admin_current_role') || window.sessionStorage.getItem('currentRole') || '');
        return !role || /主管|管理员|开发|supervisor|admin/i.test(role);
      });
      var currentUser = Vue.computed(function () {
        return window.sessionStorage.getItem('admin_current_user') || window.sessionStorage.getItem('currentUser') || '';
      });
      var selectedDeptPeople = Vue.computed(function () {
        var source = state.selectedDept === '全部'
          ? departments.reduce(function (all, dept) { return all.concat(state.people[dept] || []); }, [])
          : (state.people[state.selectedDept] || []);
        return Array.from(new Set(source));
      });
      var filteredRows = Vue.computed(function () {
        return state.rows.filter(function (row) {
          var deptOk = state.selectedDept === '全部' || row.dept === state.selectedDept;
          var personOk = state.selectedPerson === '全部' || row.author === state.selectedPerson;
          return deptOk && personOk;
        });
      });
      var monthRows = Vue.computed(function () {
        var key = currentMonth();
        return filteredRows.value.filter(function (row) { return monthKey(row.date) === key; });
      });
      var period = Vue.computed(monthRange);
      var stats = Vue.computed(function () {
        var rows = monthRows.value;
        var views = rows.reduce(function (sum, row) { return sum + Number(row.views || 0); }, 0);
        var hot = rows.filter(function (row) { return Number(row.views || 0) >= (hotThreshold[row.type] || 10000); }).length;
        var fallbackDayViews = state.selectedDept === '全部'
          ? departments.reduce(function (sum, dept) { return sum + Number((state.categoryBindings[dept] || []).length * 12800); }, 0)
          : Number((state.categoryBindings[state.selectedDept] || []).length * 12800);
        var dayViews = state.selectedDept === '全部'
          ? (state.dashboardViews.all || fallbackDayViews)
          : (state.dashboardViews.departments[state.selectedDept] || fallbackDayViews);
        function previous(value, comp) {
          return Math.max(0, Math.round(Number(value || 0) / (1 + (Number(comp || 0) / 100))));
        }
        return [
          { label: '本月笔记总量', value: rows.length, previous: previous(rows.length, 12.8), unit: '篇', comp: 12.8, icon: 'fa-note-sticky', tone: 'blue' },
          { label: '本月浏览量', value: views, previous: previous(views, 8.6), unit: '', comp: 8.6, icon: 'fa-chart-line', tone: 'violet' },
          { label: '本月爆文量', value: hot, previous: previous(hot, -3.2), unit: '篇', comp: -3.2, icon: 'fa-fire', tone: 'orange' },
          { label: '店铺日浏览量', value: dayViews, previous: previous(dayViews, 5.4), unit: '', comp: 5.4, icon: 'fa-store', tone: 'green' }
        ];
      });

      function formatNumber(value) {
        return Number(value || 0).toLocaleString('zh-CN');
      }
      function formatCompact(value) {
        value = Number(value || 0);
        if (value >= 100000000) return (value / 100000000).toFixed(1).replace('.0', '') + '亿';
        if (value >= 10000) return (value / 10000).toFixed(1).replace('.0', '') + '万';
        return formatNumber(value);
      }
      function rowShares(row) {
        return Number(row.shares || 0);
      }
      function rowExposure(row) {
        return Math.round(Number(row.views || 0) * 1.6);
      }
      function rowEngagementRate(row) {
        var views = Number(row.views || 0);
        if (!views) return '0.0%';
        return ((Number(row.likes || 0) + Number(row.collects || 0) + Number(row.comments || 0) + rowShares(row)) / views * 100).toFixed(1) + '%';
      }
      function isHot(row) {
        return Number(row.views || 0) >= (hotThreshold[row.type] || 10000);
      }
      function selectDept(dept) {
        state.selectedDept = dept;
        state.selectedPerson = '全部';
        state.categoryPopup = false;
        state.peoplePopup = false;
      }
      function choosePerson(person) {
        state.selectedPerson = person;
        state.peoplePopup = false;
      }
      function toggleCategory(category) {
        if (state.selectedDept === '全部') return;
        var selected = state.categoryBindings[state.selectedDept] || [];
        var index = selected.indexOf(category);
        if (index >= 0) selected.splice(index, 1);
        else selected.push(category);
        state.categoryBindings[state.selectedDept] = selected;
        localStorage.setItem('seeding-monitor-bindings', JSON.stringify(state.categoryBindings));
      }
      function resetForm() {
        state.form = {
          product: state.selectedDept !== '全部' ? (state.categoryBindings[state.selectedDept] || [])[0] || '' : '',
          platform: '抖音', source: '代发', type: '种草', title: '', link: '',
          author: currentUser.value || (selectedDeptPeople.value[0] || ''), workId: '',
          likes: 0, collects: 0, comments: 0, shares: 0, views: 0, date: currentMonth() + '-22', trafficImage: ''
        };
      }
      function openCreate() {
        state.editingId = null;
        resetForm();
        state.recordModal = true;
      }
      function openEdit(row) {
        if (!canEdit(row)) return;
        state.editingId = row.id;
        state.form = clone(row);
        state.recordModal = true;
      }
      function canEdit(row) {
        return isSupervisor.value || !currentUser.value || row.author === currentUser.value;
      }
      function saveRecord() {
        var form = state.form;
        if (!form.link || !form.type || !form.title || !form.workId) {
          window.alert('请填写作品链接、笔记类型、标题和作品ID');
          return;
        }
        if (state.editingId) {
          var target = state.rows.find(function (row) { return row.id === state.editingId; });
          if (target) Object.assign(target, clone(form));
        } else {
          state.rows.unshift(Object.assign({ id: Date.now(), dept: state.selectedDept === '全部' ? '五部' : state.selectedDept }, clone(form)));
        }
        localStorage.setItem('seeding-monitor-rows', JSON.stringify(state.rows));
        state.recordModal = false;
      }
      function removeRow(row) {
        if (!canEdit(row) || !window.confirm('确定删除这条记录吗？')) return;
        state.rows = state.rows.filter(function (item) { return item.id !== row.id; });
        localStorage.setItem('seeding-monitor-rows', JSON.stringify(state.rows));
      }
      function onImageChange(event) {
        var file = event.target.files && event.target.files[0];
        if (!file) return;
        var reader = new FileReader();
        reader.onload = function () { state.form.trafficImage = reader.result; };
        reader.readAsDataURL(file);
      }
      function previewTraffic(row) {
        if (!row.trafficImage) return;
        state.trafficRow = row;
        state.trafficModal = true;
      }
      function syncPeople() {
        var getter = window.ApiService && window.ApiService.getAnnounceOptions;
        if (typeof getter !== 'function') return;
        Promise.resolve(getter.call(window.ApiService)).then(function (result) {
          var accounts = Array.isArray(result) ? result : (result && (result.data || result.list || result.accounts)) || [];
          var grouped = {};
          accounts.forEach(function (account) {
            var dept = account.department || account.dept || account.departmentName;
            var name = account.name || account.nickname || account.username;
            if (!departments.includes(dept) || !name) return;
            if (!grouped[dept]) grouped[dept] = [];
            grouped[dept].push(name);
          });
          Object.keys(grouped).forEach(function (dept) {
            state.people[dept] = Array.from(new Set(grouped[dept]));
          });
          state.syncState = '已同步网站部门账号';
          state.lastSync = new Date().toLocaleString('zh-CN', { hour12: false });
        }).catch(function () {});
      }
      function syncDashboard() {
        var getter = window.ApiService && (window.ApiService.getDashboardSummary || window.ApiService.getDataDashboard);
        if (typeof getter === 'function') {
          Promise.resolve(getter.call(window.ApiService)).then(function (result) {
            var data = result && (result.data || result.summary || result);
            var all = Number(data && (data.storeDailyViews || data.dailyViews || data.totalDailyViews || data.overallDailyViews) || 0);
            var byDept = data && (data.departments || data.departmentViews || data.byDepartment);
            if (all) state.dashboardViews.all = all;
            if (byDept && typeof byDept === 'object') {
              Object.keys(byDept).forEach(function (dept) {
                var value = byDept[dept];
                state.dashboardViews.departments[dept] = Number(value && (value.dailyViews || value.views || value.value) || value || 0);
              });
            }
            state.syncState = '已连接数据看板';
            state.lastSync = new Date().toLocaleString('zh-CN', { hour12: false });
          }).catch(function () {});
        }
      }
      function exportRows() {
        var headers = ['部门', '品类', '平台', '来源', '笔记类型', '标题', '作品链接', '人员', '作品ID', '点赞', '收藏', '评论', '分享', '阅读量'];
        var body = filteredRows.value.map(function (row) {
          return [row.dept, row.product, row.platform, row.source, row.type, row.title, row.link, row.author, row.workId, row.likes, row.collects, row.comments, rowShares(row), row.views];
        });
        var csv = [headers].concat(body).map(function (line) { return line.map(function (item) { return '"' + String(item == null ? '' : item).replace(/"/g, '""') + '"'; }).join(','); }).join('\n');
        var blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' });
        var url = URL.createObjectURL(blob);
        var anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = '种草监测-' + currentMonth() + '.csv';
        anchor.click();
        URL.revokeObjectURL(url);
      }

      Vue.onMounted(function () {
        syncPeople();
        syncDashboard();
        var mount = document.getElementById('page-seeding-monitor-vue');
        if (mount) mount.classList.remove('hidden');
      });

      return {
        state: state,
        departments: departments,
        noteTypes: noteTypes,
        categoryOptions: categoryOptions,
        bonusRules: bonusRules,
         selectedDeptPeople: selectedDeptPeople,
        period: period,
         filteredRows: filteredRows,
        stats: stats,
        isSupervisor: isSupervisor,
        currentUser: currentUser,
         formatNumber: formatNumber,
         formatCompact: formatCompact,
        rowShares: rowShares,
        rowExposure: rowExposure,
        rowEngagementRate: rowEngagementRate,
        isHot: isHot,
        selectDept: selectDept,
        choosePerson: choosePerson,
        toggleCategory: toggleCategory,
        openCreate: openCreate,
        openEdit: openEdit,
        canEdit: canEdit,
        saveRecord: saveRecord,
        removeRow: removeRow,
        onImageChange: onImageChange,
        previewTraffic: previewTraffic,
        exportRows: exportRows
      };
    },
    template: `
      <div class="srm-shell">
        <header class="srm-top">
          <div>
            <div class="srm-title">种草监测中台</div>
            <div class="srm-subtitle">按部门、人员和品类查看内容产出与店铺流量</div>
          </div>
          <div class="srm-top-actions">
            <div class="srm-pop-wrap">
              <button class="srm-btn srm-btn-lg" @click="state.peoplePopup = !state.peoplePopup"><i class="fa-solid fa-user-check"></i>人员选择<i class="fa-solid fa-chevron-down srm-btn-chevron"></i></button>
              <div v-if="state.peoplePopup" class="srm-pop">
                <div class="srm-pop-title">{{ state.selectedDept === '全部' ? '全部部门' : state.selectedDept }} · 选择维护人员</div>
                <button class="srm-btn" style="width:100%;justify-content:flex-start;margin-bottom:8px" @click="choosePerson('全部')">全部人员</button>
                <button v-for="person in selectedDeptPeople" :key="person" class="srm-btn" style="width:100%;justify-content:flex-start;margin-bottom:6px" @click="choosePerson(person)">{{ person }}</button>
              </div>
            </div>
            <div class="srm-pop-wrap">
              <button class="srm-btn srm-btn-lg" :disabled="state.selectedDept === '全部'" @click="state.categoryPopup = !state.categoryPopup"><i class="fa-solid fa-link"></i>品类绑定<i class="fa-solid fa-chevron-down srm-btn-chevron"></i></button>
              <div v-if="state.categoryPopup && state.selectedDept !== '全部'" class="srm-pop">
                <div class="srm-pop-title">选择 {{ state.selectedDept }} 的店铺品类</div>
                <div class="srm-check-grid">
                  <label v-for="category in categoryOptions" :key="category" class="srm-check"><input type="checkbox" :checked="(state.categoryBindings[state.selectedDept] || []).includes(category)" @change="toggleCategory(category)"><span>{{ category }}</span></label>
                </div>
              </div>
            </div>
            <button class="srm-btn primary srm-btn-lg" @click="openCreate"><i class="fa-solid fa-plus"></i>新增记录</button>
          </div>
        </header>

        <nav class="srm-tabs-row" aria-label="部门范围">
          <span class="srm-range"><i class="fa-regular fa-calendar"></i><span class="srm-current-range">{{ period.start.slice(0, 7) }}</span><i class="fa-solid fa-chevron-down srm-range-chevron"></i></span>
          <div class="srm-tabs">
            <button class="srm-tab" :class="{active: state.selectedDept === '全部'}" @click="selectDept('全部')">全部</button>
            <button v-for="dept in departments" :key="dept" class="srm-tab" :class="{active: state.selectedDept === dept}" @click="selectDept(dept)">{{ dept }}</button>
          </div>
          <div class="srm-tabs-meta"><span class="srm-sync"><span class="srm-dot"></span>{{ state.syncState }}</span></div>
        </nav>

        <section class="srm-stat-grid">
          <article v-for="card in stats" :key="card.label" class="srm-stat">
            <div class="srm-stat-head"><span>{{ card.label }}</span><span class="srm-stat-icon" :class="'srm-tone-' + card.tone"><i class="fa-solid" :class="card.icon"></i></span></div>
            <div class="srm-stat-value">{{ formatCompact(card.value) }}<small>{{ card.unit }}</small></div>
            <div class="srm-stat-foot"><span class="srm-previous">上周期 {{ formatCompact(card.previous) }}</span><span class="srm-trend" :class="card.comp >= 0 ? 'srm-up' : 'srm-down'"><i class="fa-solid" :class="card.comp >= 0 ? 'fa-arrow-trend-up' : 'fa-arrow-trend-down'"></i>{{ Math.abs(card.comp).toFixed(1) }}%</span></div>
          </article>
        </section>

        <section class="srm-toolbar">
          <div class="srm-toolbar-left">
            <span class="srm-filter-context"><i class="fa-solid fa-filter"></i>{{ state.selectedDept === '全部' ? '全部部门' : state.selectedDept }} · {{ state.selectedPerson === '全部' ? '全部人员' : state.selectedPerson }}</span>
          </div>
          <div class="srm-toolbar-right">
            <span class="srm-filter-label">{{ filteredRows.length }} 条记录</span>
            <span v-if="!isSupervisor" class="srm-filter-label"><i class="fa-solid fa-lock"></i> 仅可编辑本人数据</span>
            <details class="srm-standard"><summary class="srm-btn"><i class="fa-solid fa-award"></i>奖金标准</summary><table><thead><tr><th>笔记类型</th><th>浏览量档位</th><th>奖金</th></tr></thead><tbody><tr v-for="rule in bonusRules" :key="rule.type"><td>{{ rule.type }}</td><td>{{ rule.levels }}</td><td>{{ rule.bonus }}</td></tr></tbody></table></details>
            <button class="srm-btn" @click="exportRows"><i class="fa-solid fa-download"></i>导出</button>
            <button class="srm-btn primary" @click="openCreate"><i class="fa-solid fa-plus"></i>新增记录</button>
          </div>
        </section>

        <div class="srm-table-wrap">
          <table class="srm-table">
            <thead>
              <tr><th rowspan="2">品类</th><th rowspan="2">平台</th><th rowspan="2">类型</th><th rowspan="2">作品标题</th><th rowspan="2">维护人员</th><th rowspan="2" class="srm-metric">阅读量</th><th colspan="4" class="group">互动数据</th><th colspan="4" class="group">流量分析</th><th rowspan="2">操作</th></tr>
              <tr><th class="srm-metric">点赞</th><th class="srm-metric">评论</th><th class="srm-metric">收藏</th><th class="srm-metric">分享</th><th class="srm-metric">预估曝光</th><th class="srm-metric">互动率</th><th>是否爆文</th><th>分析图</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in filteredRows" :key="row.id">
                <td>{{ row.product || '-' }}</td>
                <td>{{ row.platform }}<span class="srm-sub-cell">{{ row.source || '自然发布' }}</span></td>
                <td><span class="srm-type">{{ row.type }}</span></td>
                <td class="srm-title-cell" :title="row.title"><a class="srm-title-link" :href="row.link" target="_blank" rel="noopener">{{ row.title }}</a><span class="srm-sub-cell">ID {{ row.workId || '-' }}</span></td>
                <td>{{ row.author || '-' }}</td>
                <td class="srm-metric">{{ formatNumber(row.views) }}</td>
                <td class="srm-metric">{{ formatNumber(row.likes) }}</td>
                <td class="srm-metric">{{ formatNumber(row.comments) }}</td>
                <td class="srm-metric">{{ formatNumber(row.collects) }}</td>
                <td class="srm-metric">{{ formatNumber(rowShares(row)) }}</td>
                <td class="srm-metric">{{ formatCompact(rowExposure(row)) }}</td>
                <td class="srm-metric srm-rate">{{ rowEngagementRate(row) }}</td>
                <td><span v-if="isHot(row)" class="srm-hot"><i class="fa-solid fa-fire"></i>爆文</span><span v-else class="srm-cold">未达标</span></td>
                <td><button class="srm-icon-btn" :disabled="!row.trafficImage" :title="row.trafficImage ? '查看流量分析图' : '暂无流量分析图'" @click="previewTraffic(row)"><i class="fa-regular fa-image"></i></button></td>
                <td><div class="srm-row-actions"><button class="srm-icon-btn" :disabled="!canEdit(row)" title="编辑" @click="openEdit(row)"><i class="fa-solid fa-pen"></i></button><button class="srm-icon-btn danger" :disabled="!canEdit(row)" title="删除" @click="removeRow(row)"><i class="fa-solid fa-trash"></i></button></div></td>
              </tr>
              <tr v-if="!filteredRows.length"><td class="srm-empty" colspan="15">当前筛选范围暂无记录</td></tr>
            </tbody>
          </table>
        </div>

        <div v-if="state.recordModal" class="srm-mask" @click.self="state.recordModal = false">
          <div class="srm-modal">
            <div class="srm-modal-head"><div class="srm-modal-title">{{ state.editingId ? '编辑监测记录' : '新增监测记录' }}</div><button class="srm-btn icon" @click="state.recordModal = false"><i class="fa-solid fa-xmark"></i></button></div>
            <div class="srm-modal-body">
              <div class="srm-form-grid">
                <div class="srm-field"><label>品类</label><input v-model="state.form.product" placeholder="例如：电动牙刷"></div>
                <div class="srm-field"><label>平台</label><select v-model="state.form.platform"><option>抖音</option><option>小红书</option><option>视频号</option></select></div>
                <div class="srm-field"><label>来源</label><input v-model="state.form.source" placeholder="例如：代发"></div>
                <div class="srm-field"><label>笔记类型 *</label><select v-model="state.form.type"><option v-for="type in noteTypes" :key="type">{{ type }}</option></select></div>
                <div class="srm-field full"><label>标题 *</label><input v-model="state.form.title" placeholder="填写作品标题"></div>
                <div class="srm-field full"><label>作品链接 *</label><input v-model="state.form.link" placeholder="https://"></div>
                <div class="srm-field"><label>维护人员</label><input v-model="state.form.author" :disabled="!isSupervisor && !!currentUser" placeholder="姓名"></div>
                <div class="srm-field"><label>作品ID *</label><input v-model="state.form.workId" placeholder="平台作品ID"></div>
                <div class="srm-field"><label>点赞</label><input v-model.number="state.form.likes" type="number" min="0"></div>
                <div class="srm-field"><label>评论</label><input v-model.number="state.form.comments" type="number" min="0"></div>
                <div class="srm-field"><label>收藏</label><input v-model.number="state.form.collects" type="number" min="0"></div>
                <div class="srm-field"><label>分享</label><input v-model.number="state.form.shares" type="number" min="0"></div>
                <div class="srm-field"><label>阅读量</label><input v-model.number="state.form.views" type="number" min="0"></div>
                <div class="srm-field"><label>发布日期</label><input v-model="state.form.date" type="date"></div>
                <div class="srm-field"><label>流量分析图</label><label class="srm-upload"><input type="file" accept="image/*" style="display:none" @change="onImageChange"><img v-if="state.form.trafficImage" :src="state.form.trafficImage" alt="流量分析图"><span v-else><i class="fa-regular fa-image"></i> 点击上传</span></label></div>
              </div>
              <div class="srm-note">带 * 字段为必填；阅读量达到对应类型的最低档位后计入爆文量。</div>
            </div>
            <div class="srm-modal-foot"><button class="srm-btn" @click="state.recordModal = false">取消</button><button class="srm-btn primary" @click="saveRecord">保存记录</button></div>
          </div>
        </div>
        <div v-if="state.trafficModal" class="srm-mask" @click.self="state.trafficModal = false"><div class="srm-modal" style="width:min(900px,100%)"><div class="srm-modal-head"><div class="srm-modal-title">流量分析图 · {{ state.trafficRow && state.trafficRow.title }}</div><button class="srm-btn icon" @click="state.trafficModal = false"><i class="fa-solid fa-xmark"></i></button></div><div class="srm-modal-body"><img class="srm-traffic-preview" :src="state.trafficRow && state.trafficRow.trafficImage" alt="流量分析图"></div></div></div>
      </div>
    `
  };

  window.SeedingPage = app;
  window.mountSeedingVue = function () {
    var mount = document.getElementById('page-seeding-monitor-vue');
    if (!mount || mount.__vue_app__) return;
    Vue.createApp(app).mount(mount);
  };
})();
