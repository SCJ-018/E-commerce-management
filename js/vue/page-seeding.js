/** 种草监测中台：服务端数据 + 部门店铺品类绑定。 */
(function () {
  var departments = ['一部', '二部', '三部', '四部', '五部'];
  var platforms = ['抖音', '小红书'];
  var sources = ['代发', '自发'];
  var noteTypes = ['测评', '种草', '干货', '引流', '实拍', '扣测'];
  var defaultSheets = ['全部数据', '马楠', '李世友', '李鑫雨', '刘昱岑', '黄新茹', '李喆'];
  var fallbackCategories = ['汽车脚垫', '汽车坐垫', '汽车香薰', '去油膜', '汽车头枕', '汽车腰靠', '汽车车衣', '剃须刀', '爬楼机', '电动牙刷', '护腰坐垫'];
  function clone(v) { return JSON.parse(JSON.stringify(v)); }
  function currentDate() { return new Date().toISOString().slice(0, 10); }
  function parseDate(value) { var text = String(value || ''); var match = text.match(/(\d{4}-\d{2}-\d{2})/); if (match) return match[1]; var parsed = Date.parse(text); return isNaN(parsed) ? (text ? text.slice(0, 10) : currentDate()) : new Date(parsed).toISOString().slice(0, 10); }
  function normalize(row) {
    row = row || {};
    return { id: row.id || Date.now(), date: parseDate(row.date || row.发布时间), department: row.department || row.dept || row.部门 || '五部', product: row.product || row.产品 || '', platform: row.platform || row.发布平台 || '抖音', source: row.source || row.发布渠道 || '代发', noteType: row.noteType || row.type || row.笔记类型 || '种草', title: row.title || row.标题 || '', publishLink: row.publishLink || row.link || row.发布链接 || '', accountName: row.accountName || row.author || row.发布账号名称 || '', accountId: row.accountId || row.workId || row.发布账号ID || '', responsible: row.responsible || row.负责人 || '', likes: Number(row.likes || row.点赞 || 0), collects: Number(row.collects || row.收藏 || 0), comments: Number(row.comments || row.评论 || 0), views: Number(row.views || row.阅读量 || 0), trafficImage: row.trafficImage || row.流量分析 || '', remark: row.remark || row.备注 || '' };
  }
  var app = {
    setup: function () {
      var state = Vue.reactive({ selectedDept: '全部', selectedSheet: '全部数据', sheetsByDept: {}, rows: [], datePages: [], datePageIndex: 0, virtualStart: 0, people: {}, stores: [], bindings: [], bindingPopup: false, categoryViews: {}, categoryRules: {}, summary: { current: { count: 0, views: 0, hot: 0 }, previous: { count: 0, views: 0, hot: 0 }, categoryCurrent: 0, categoryPrevious: 0 }, recordModal: false, trafficModal: false, trafficRow: null, selectedIds: [], batchMode: false, openDropdown: null, dropdownRect: null, trafficTarget: null, editingId: null, inlineEdit: null, apiReady: false, syncState: '正在连接腾讯云数据库', loadingMore: false, hasMoreRows: false, form: {} });
      var api = window.ApiService;
      var currentUser = Vue.computed(function () { return window.sessionStorage.getItem('admin_current_user') || window.sessionStorage.getItem('currentUser') || ''; });
      var isSupervisor = Vue.computed(function () { var role = String(window.sessionStorage.getItem('admin_current_role') || ''); return !role || /主管|管理员|开发|supervisor|admin/i.test(role); });
      var sheetNames = Vue.computed(function () { return state.selectedDept === '全部' ? [] : ['全部数据'].concat(state.sheetsByDept[state.selectedDept] || []); });
      var allFilteredRows = Vue.computed(function () { return state.rows.filter(function (r) { return (state.selectedDept === '全部' || r.department === state.selectedDept) && (state.selectedSheet === '全部数据' || r.responsible === state.selectedSheet); }).sort(function (a, b) { return String(b.date).localeCompare(String(a.date)) || Number(b.id) - Number(a.id); }); });
      var datePages = Vue.computed(function () { var seen = {}; var list = []; allFilteredRows.value.forEach(function (r) { if (!seen[r.date]) { seen[r.date] = true; list.push(r.date); } }); return list; });
      var filteredRows = Vue.computed(function () { var dates = state.datePages.length ? state.datePages : datePages.value; var selected = dates[state.datePageIndex] || dates[0]; return selected ? allFilteredRows.value.filter(function (r) { return r.date === selected; }) : []; });
      var visibleRows = Vue.computed(function () { var rows = filteredRows.value, start = Math.max(0, Math.min(state.virtualStart, Math.max(0, rows.length - 80))); return rows.slice(start, start + 80); });
      var virtualTopSpace = Vue.computed(function () { return Math.max(0, Math.min(state.virtualStart, Math.max(0, filteredRows.value.length - 80))) * 48; });
      var virtualBottomSpace = Vue.computed(function () { var start = Math.max(0, Math.min(state.virtualStart, Math.max(0, filteredRows.value.length - 80))); return Math.max(0, filteredRows.value.length - start - visibleRows.value.length) * 48; });
      var currentBindings = Vue.computed(function () { return state.bindings.filter(function (b) { return b.department === state.selectedDept; }); });
      var productOptions = Vue.computed(function () { var list = currentBindings.value.map(function (b) { return b.category; }); if (!list.length && state.selectedDept !== '全部') list = state.stores.reduce(function (a, s) { return a.concat(s.categories || []); }, []); return Array.from(new Set(list.length ? list : fallbackCategories)); });
      var categoryViewValue = Vue.computed(function () { return state.selectedDept === '全部' ? departments.reduce(function (n, d) { return n + Number(state.categoryViews[d] || 0); }, 0) : Number(state.categoryViews[state.selectedDept] || 0); });
      var categoryViewTooltip = Vue.computed(function () { var list = state.selectedDept === '全部' ? departments : [state.selectedDept]; return '品类浏览量口径：抖店取商品点击人数，京东和千牛取商品访客数；' + list.map(function (d) { var r = state.categoryRules[d]; return r ? d + '：' + (r.store ? '店铺名含“' + r.store + '”，' : '') + '标题含' + r.keywords.join('、') : ''; }).filter(Boolean).join('；'); });
      function trend(current, previous) { current = Number(current || 0); previous = Number(previous || 0); if (!previous && !current) return { text: '—', className: 'srm-trend' }; if (!previous) return { text: '新增', className: 'srm-up' }; var pct = Math.round((current - previous) / previous * 100); return { text: (pct >= 0 ? '↑ ' : '↓ ') + Math.abs(pct) + '%', className: pct >= 0 ? 'srm-up' : 'srm-down' }; }
      var stats = Vue.computed(function () { var current = state.summary.current || {}, previous = state.summary.previous || {}; return [{ label: '本月笔记总量', value: current.count, unit: '篇', icon: 'fa-note-sticky', tone: 'blue', subIcon: 'fa-heart', subLabel: '较上月', subValue: trend(current.count, previous.count).text, trendClass: trend(current.count, previous.count).className, tag: '本月' }, { label: '本月浏览量', value: current.views, unit: '', icon: 'fa-chart-line', tone: 'violet', subIcon: 'fa-comments', subLabel: '较上月', subValue: trend(current.views, previous.views).text, trendClass: trend(current.views, previous.views).className, tag: '本月' }, { label: '本月爆文量', value: current.hot, unit: '篇', icon: 'fa-fire', tone: 'rose', subIcon: 'fa-percent', subLabel: '较上月', subValue: trend(current.hot, previous.hot).text, trendClass: trend(current.hot, previous.hot).className, tag: '≥1万阅读' }, { label: '昨日品类浏览量', value: state.summary.categoryCurrent, unit: '', icon: 'fa-link', tone: 'green', subIcon: 'fa-store', subLabel: '较前一日', subValue: trend(state.summary.categoryCurrent, state.summary.categoryPrevious).text, trendClass: trend(state.summary.categoryCurrent, state.summary.categoryPrevious).className, tag: '悬浮查看口径' }]; });
      function formatNumber(v) { return Number(v || 0).toLocaleString('zh-CN'); }
      function formatCompact(v) { v = Number(v || 0); return v >= 100000000 ? (v / 100000000).toFixed(1).replace('.0', '') + '亿' : v >= 10000 ? (v / 10000).toFixed(1).replace('.0', '') + '万' : formatNumber(v); }
      /* ---- 收录表 V2：彩色语义胶囊 + 热度分级（纯展示层，不动数据） ---- */
      var PRODUCT_TONE = { '汽车脚垫': 'p-jiaodian', '汽车坐垫': 'p-zuodian', '汽车香薰': 'p-xiangxun', '去油膜': 'p-quyoumo', '汽车头枕': 'p-toushen', '汽车腰靠': 'p-yaokao', '汽车车衣': 'p-cheyi', '剃须刀': 'p-tidao', '爬楼机': 'p-palouji', '电动牙刷': 'p-yaoshuang', '护腰坐垫': 'p-huyao' };
      var NOTE_TONE = { '测评': 'p-ceping', '种草': 'p-zhongcao', '干货': 'p-ganhuo', '引流': 'p-yinliu', '实拍': 'p-shipai', '扣测': 'p-kouce' };
      var HOT_VIEWS = 100000, WARM_VIEWS = 10000;
      function productTone(row) { return PRODUCT_TONE[String(row.product || '').trim()] || 'p-default'; }
      function platformTone(row) { var v = String(row.platform || ''); return v.indexOf('小红书') >= 0 ? 'p-xhs' : (v.indexOf('抖音') >= 0 ? 'p-douyin' : 'p-default'); }
      function sourceTone(row) { return String(row.source || '').indexOf('自发') >= 0 ? 'p-zifa' : 'p-daifa'; }
      function noteTone(row) { return NOTE_TONE[String(row.noteType || '').trim()] || 'p-default'; }
      function likesTone(row) { return Number(row.likes || 0) >= 1000 ? 'srm-m-warm' : ''; }
      function collectsTone(row) { return Number(row.collects || 0) >= 50 ? 'srm-m-amber' : ''; }
      function commentsTone(row) { return Number(row.comments || 0) >= 100 ? 'srm-m-blue' : ''; }
      function viewTier(row) { var v = Number(row.views || 0); return v >= HOT_VIEWS ? 'hot' : (v >= WARM_VIEWS ? 'warm' : 'normal'); }
      var maxViews = Vue.computed(function () { return filteredRows.value.reduce(function (m, r) { return Math.max(m, Number(r.views || 0)); }, 0) || 1; });
      function viewBar(row) { var v = Number(row.views || 0); if (!v) return '0%'; return Math.max(3, Math.round(v / maxViews.value * 100)) + '%'; }
      function dateGroupLabel(row, index) { var n = dateSpan(row, index), sum = 0; for (var i = index; i < index + n; i++) { var r = filteredRows.value[i]; if (r) sum += Number(r.views || 0); } return n + ' 条 · 阅读 ' + formatCompact(sum); }
      var totals = Vue.computed(function () { var t = { count: 0, likes: 0, collects: 0, comments: 0, views: 0, hot: 0, rate: 0 }; filteredRows.value.forEach(function (r) { t.count++; t.likes += Number(r.likes || 0); t.collects += Number(r.collects || 0); t.comments += Number(r.comments || 0); var v = Number(r.views || 0); t.views += v; if (v >= HOT_VIEWS) t.hot++; }); t.rate = t.count ? Math.round(t.hot / t.count * 100) : 0; return t; });
      /* ---- V6 自定义下拉面板（原生 option 列表无法美化，改自绘面板） ---- */
      function dropdownKey(row, field) { return String(row.id) + ':' + field; }
      function isDropdown(row, field) { return state.openDropdown === dropdownKey(row, field); }
      function toggleDropdown(row, field, event) {
        if (!canEdit(row)) return;
        var key = dropdownKey(row, field);
        if (state.openDropdown === key) { closeDropdown(); return; }
        var el = event && event.currentTarget ? event.currentTarget : null;
        var rect = el ? el.getBoundingClientRect() : null;
        state.dropdownRect = rect ? { left: rect.left, top: rect.bottom + 6, width: Math.max(rect.width, 150),
                                      bottom: rect.bottom, spaceBelow: window.innerHeight - rect.bottom } : null;
        state.openDropdown = key;
      }
      function closeDropdown() { state.openDropdown = null; state.dropdownRect = null; }
      function pickDropdown(row, field, value) { row[field] = value; closeDropdown(); finishEdit(row); }
      function dropdownList(field) {
        if (field === 'product') return productOptions.value;
        if (field === 'platform') return platforms;
        if (field === 'source') return sources;
        if (field === 'noteType') return noteTypes;
        return [];
      }
      function dropdownTone(field, value) {
        if (field === 'product') return productTone({ product: value });
        if (field === 'platform') return platformTone({ platform: value });
        if (field === 'source') return sourceTone({ source: value });
        if (field === 'noteType') return noteTone({ noteType: value });
        return '';
      }
      function dropdownField() { return state.openDropdown ? state.openDropdown.split(':')[1] : ''; }
      function dropdownRow() {
        if (!state.openDropdown) return null;
        var id = state.openDropdown.split(':')[0];
        return filteredRows.value.find(function (r) { return String(r.id) === id; }) || null;
      }
      function dropdownItems() { return dropdownList(dropdownField()); }
      function dropdownPick(opt) { var row = dropdownRow(); if (row) pickDropdown(row, dropdownField(), opt); }
      function selectDept(dept) { state.selectedDept = dept; state.selectedSheet = '全部数据'; state.datePageIndex = 0; state.datePages = []; state.virtualStart = 0; state.bindingPopup = false; state.selectedIds = []; rowCache = {}; }
      function selectSheet(sheet) { state.selectedSheet = sheet; state.datePageIndex = 0; state.datePages = []; state.virtualStart = 0; state.selectedIds = []; rowCache = {}; }
      function addSheet() {
        if (state.selectedDept === '全部' || !api || !api.createSeedingSheet) return;
        var name = window.prompt('请输入该部门的新 Sheet 名称');
        if (!name || !String(name).trim()) return;
        api.createSeedingSheet({ department: state.selectedDept, name: String(name).trim() }).then(function () {
          return loadOptions().then(function () { state.selectedSheet = String(name).trim(); rowCache = {}; loadRows(); });
        }).catch(function (error) { window.alert(error && error.message ? error.message : '新增 Sheet 失败'); });
      }
      function renameSheet(sheet) {
        if (state.selectedDept === '全部' || !sheet || sheet === '全部数据' || !api || !api.renameSeedingSheet) return;
        var name = window.prompt('修改 Sheet 名称', sheet);
        if (!name || !String(name).trim() || String(name).trim() === sheet) return;
        api.renameSeedingSheet({ department: state.selectedDept, oldName: sheet, name: String(name).trim() }).then(function () {
          state.selectedSheet = String(name).trim(); rowCache = {}; return loadOptions().then(loadRows);
        }).catch(function (error) { window.alert(error && error.message ? error.message : '修改 Sheet 名称失败'); });
      }
      function formatDateInput(target) { var value = String(target && target.date || '').replace(/\D/g, '').slice(0, 8); if (value.length > 6) value = value.slice(0, 4) + '-' + value.slice(4, 6) + '-' + value.slice(6); else if (value.length > 4) value = value.slice(0, 4) + '-' + value.slice(4); if (target) target.date = value; }
      var dateToneCacheKey = '', dateToneCache = {};
      function dateTone(row) { var list = filteredRows.value; var key = state.selectedDept + '|' + state.selectedSheet + '|' + list.length + '|' + (list[0] ? list[0].id : '') + '|' + (list[list.length - 1] ? list[list.length - 1].id : ''); if (key !== dateToneCacheKey) { dateToneCacheKey = key; dateToneCache = {}; var tone = 0; list.forEach(function (item) { if (dateToneCache[item.date] === undefined) dateToneCache[item.date] = tone++; }); } return dateToneCache[row.date] % 2 ? 'srm-date-tone-b' : 'srm-date-tone-a'; }
      function canEdit(row) { return isSupervisor.value || !currentUser.value || row.accountName === currentUser.value; }
      function startEdit(row, key) { if (canEdit(row)) state.inlineEdit = { id: row.id, key: key }; }
      function isEditing(row, key) { return !!state.inlineEdit && state.inlineEdit.id === row.id && state.inlineEdit.key === key; }
      function finishEdit(row) { if (state.inlineEdit && state.inlineEdit.id === row.id) state.inlineEdit = null; dateToneCacheKey = ''; persistRow(row); }
      function showDate(row, index) { return index === 0 || filteredRows.value[index - 1].date !== row.date; }
      function dateSpan(row, index) { if (!showDate(row, index)) return 0; var n = 1; while (filteredRows.value[index + n] && filteredRows.value[index + n].date === row.date) n++; return n; }
      function linkLabel(row) { return row.publishLink ? '已输入' : '输入链接'; }
      function openLink(row) { if (!row.publishLink) return; if (!isUrl(row.publishLink)) return; var url = /^https?:\/\//i.test(row.publishLink) ? row.publishLink : 'https://' + row.publishLink; window.open(url, '_blank', 'noopener'); }
      /* ★ 库里 publishLink 可能是「不回复」这类非 URL 文本 → 只有看起来像链接才给 href，
         否则标题会给成 '#'，避免浏览器把中文当相对路径去请求（线上 404 的根因）。 */
      function isUrl(v) {
        v = String(v || '').trim();
        if (!v) return false;
        if (/^https?:\/\/\S+$/i.test(v)) return true;
        if (/^\/\/\S+$/.test(v)) return true;
        return /^[a-z0-9-]+(\.[a-z0-9-]+)+(\/\S*)?$/i.test(v);   // 形如 v.douyin.com/xxx
      }
      function linkHref(row) { return (row.publishLink && isUrl(row.publishLink)) ? row.publishLink : null; }
      function retryTraffic(row) { state.trafficTarget = row; var input = document.getElementById('traffic-re-' + row.id); if (input) input.click(); }
      function onRetryImage(event, row) { var file = event.target.files && event.target.files[0]; if (!file || !canEdit(row)) return; var reader = new FileReader(); reader.onload = function () { row.trafficImage = reader.result; persistRow(row); }; reader.readAsDataURL(file); event.target.value = ''; }
      function openTraffic(row) {
        if (!row.trafficImage) return;
        if (row.trafficImage === '__present__' && api && api.getSeedingTrafficImage) {
          state.syncState = '正在读取流量分析图片';
          api.getSeedingTrafficImage(row.id).then(function (data) {
            row.trafficImage = data && data.trafficImage || '';
            if (row.trafficImage) { state.trafficRow = row; state.trafficModal = true; }
            state.syncState = '已连接腾讯云数据库';
          }).catch(function () { state.syncState = '图片读取失败，请重试'; });
          return;
        }
        state.trafficRow = row; state.trafficModal = true;
      }
      function triggerTraffic(row) { var input = document.getElementById('traffic-' + row.id); if (input) input.click(); }
      function onImageChange(event, row) { var file = event.target.files && event.target.files[0]; if (!file || !canEdit(row)) return; var reader = new FileReader(); reader.onload = function () { row.trafficImage = reader.result; if (row.id) persistRow(row); }; reader.readAsDataURL(file); }
      function newForm() { return { date: currentDate(), department: state.selectedDept === '全部' ? departments[0] : state.selectedDept, product: productOptions.value[0] || '', platform: '抖音', source: '代发', noteType: '种草', title: '', publishLink: '', accountName: currentUser.value || '', accountId: '', responsible: state.selectedSheet === '全部数据' ? '' : state.selectedSheet, likes: 0, collects: 0, comments: 0, views: 0, trafficImage: '', remark: '' }; }
      function openCreate() { state.editingId = null; state.form = newForm(); state.recordModal = true; }
      function openEdit(row) {
        if (!canEdit(row)) return;
        state.editingId = row.id; state.form = clone(row); state.recordModal = true;
        if (row.trafficImage === '__present__' && api && api.getSeedingTrafficImage) {
          api.getSeedingTrafficImage(row.id).then(function (data) {
            if (state.editingId === row.id) state.form.trafficImage = data && data.trafficImage || '';
          });
        }
      }
      function toPayload(row) { return { date: row.date, department: row.department, product: row.product, platform: row.platform, source: row.source, noteType: row.noteType, title: row.title, publishLink: row.publishLink, accountName: row.accountName, accountId: row.accountId, responsible: row.responsible, likes: row.likes, collects: row.collects, comments: row.comments, views: row.views, trafficImage: row.trafficImage, remark: row.remark }; }
      function persistRow(row) { if (state.apiReady && api && api.updateSeedingRecord) api.updateSeedingRecord(row.id, toPayload(row)).catch(function () { state.syncState = '保存失败，请重试'; }); }
      function saveRecord() { var row = normalize(state.form); if (!row.date || !row.department || !row.product || !row.title) { window.alert('请填写发布时间、部门、产品和标题'); return; } if (!api || !state.apiReady) { window.alert('腾讯云数据库尚未连接'); return; } var request = state.editingId ? api.updateSeedingRecord(state.editingId, toPayload(row)) : api.createSeedingRecord(toPayload(row)); request.then(function (result) { if (!state.editingId && result && result.id) row.id = result.id; if (state.editingId) { var old = state.rows.find(function (r) { return r.id === state.editingId; }); if (old) Object.assign(old, row, { id: state.editingId }); } else state.rows.unshift(row); rowCache = {}; state.recordModal = false; state.syncState = '已保存到腾讯云数据库'; }).catch(function () { state.syncState = '保存失败，请重试'; }); }
      function removeRow(row) { if (!canEdit(row) || !window.confirm('确定删除这条记录吗？')) return; if (!api || !state.apiReady) return; api.deleteSeedingRecord(row.id).then(function () { state.rows = state.rows.filter(function (r) { return r.id !== row.id; }); rowCache = {}; }).catch(function () { state.syncState = '删除失败，请重试'; }); }
      /* ---- V6 批量删除 ---- */
      function toggleSelect(row, checked) {
        var id = row.id;
        var idx = state.selectedIds.indexOf(id);
        if (checked && idx < 0) state.selectedIds.push(id);
        else if (!checked && idx >= 0) state.selectedIds.splice(idx, 1);
      }
      function isSelected(row) { return state.selectedIds.indexOf(row.id) >= 0; }
      function allSelected() {
        var rows = filteredRows.value;
        if (!rows.length) return false;
        return rows.every(function (r) { return state.selectedIds.indexOf(r.id) >= 0; });
      }
      function toggleSelectAll(checked) {
        var editable = filteredRows.value.filter(canEdit);
        if (checked) {
          editable.forEach(function (r) { if (state.selectedIds.indexOf(r.id) < 0) state.selectedIds.push(r.id); });
        } else {
          var drop = editable.map(function (r) { return r.id; });
          state.selectedIds = state.selectedIds.filter(function (id) { return drop.indexOf(id) < 0; });
        }
      }
      function clearSelection() { state.selectedIds = []; }
      function startBatchMode() { state.batchMode = true; state.selectedIds = []; }
      function exitBatchMode() { state.batchMode = false; state.selectedIds = []; }
      function batchRemove() {
        var targets = state.rows.filter(function (r) { return state.selectedIds.indexOf(r.id) >= 0 && canEdit(r); });
        if (!targets.length) { window.alert('请先勾选要删除的记录'); return; }
        if (!window.confirm('确定删除选中的 ' + targets.length + ' 条记录吗？此操作不可撤销。')) return;
        var ids = targets.map(function (r) { return r.id; });
        state.rows = state.rows.filter(function (r) { return ids.indexOf(r.id) < 0; });
        ids.forEach(function (id) { if (state.apiReady && api) api.deleteSeedingRecord(id); });
        rowCache = {};
        exitBatchMode();
      }
      function toggleBinding(category, store) { var idx = state.bindings.findIndex(function (b) { return b.department === state.selectedDept && b.store === store.store && b.category === category; }); if (idx >= 0) state.bindings.splice(idx, 1); else state.bindings.push({ department: state.selectedDept, store: store.store, brand: store.brand || '', category: category }); if (state.apiReady && api) api.saveSeedingBindings({ department: state.selectedDept, bindings: state.bindings.filter(function (b) { return b.department === state.selectedDept; }) }); }
      function isBound(store, category) { return state.bindings.some(function (b) { return b.department === state.selectedDept && b.store === store.store && b.category === category; }); }
      function loadOptions() { if (!api || !api.getSeedingOptions) { state.syncState = '腾讯云数据库连接失败'; return Promise.resolve(); } return api.getSeedingOptions().then(function (data) { if (!data) throw new Error('options unavailable'); state.apiReady = true; state.syncState = '已连接腾讯云数据库'; state.stores = data.stores || []; state.bindings = data.bindings || []; state.people = data.people || {}; state.sheetsByDept = data.sheets || {}; }).catch(function () { state.apiReady = false; state.syncState = '腾讯云数据库连接失败'; }); }
      function loadCategoryViews() { if (!api || !api.getSeedingCategoryViews) return Promise.resolve(); return api.getSeedingCategoryViews().then(function (data) { if (data) { state.categoryViews = data.departments || {}; state.categoryRules = data.rules || {}; } }); }
      function loadSummary() { if (!api || !api.getSeedingSummary) return Promise.resolve(); return api.getSeedingSummary(state.selectedDept).then(function (data) { if (data) state.summary = data; }); }
      function loadDatePages() {
        if (!api || !api.getSeedingRecordDates) return Promise.resolve();
        return api.getSeedingRecordDates(state.selectedDept, '全部', state.selectedSheet === '全部数据' ? '全部' : state.selectedSheet).then(function (dates) {
          state.datePages = Array.isArray(dates) ? dates : [];
          if (state.datePageIndex >= state.datePages.length) state.datePageIndex = 0;
          state.virtualStart = 0;
          loadRows();
        }).catch(function () { state.datePages = []; state.rows = []; state.syncState = '日期目录加载失败'; });
      }
      var rowRequest = 0, rowTimer = null, rowCache = {}, rowOffsets = {}, rowHasMore = {};
      function loadRows() {
        if (!api || !api.getSeedingRecords) return;
        var selectedDate = state.datePages[state.datePageIndex] || '';
        if (!selectedDate) { state.rows = []; state.hasMoreRows = false; return; }
        var key = state.selectedDept + '|' + state.selectedSheet + '|' + selectedDate;
        if (Object.prototype.hasOwnProperty.call(rowCache, key)) { state.rows = rowCache[key].slice(); state.hasMoreRows = !!rowHasMore[key]; return; }
        var token = ++rowRequest, pageSize = 100;
        if (rowTimer) clearTimeout(rowTimer);
        state.rows = [];
        rowTimer = setTimeout(function () {
          api.getSeedingRecords(state.selectedDept, '全部', state.selectedSheet === '全部数据' ? '全部' : state.selectedSheet, pageSize, 0, selectedDate).then(function (rows) {
            if (token !== rowRequest || !Array.isArray(rows)) return;
            state.rows = rows.map(normalize);
            rowCache[key] = state.rows.slice();
            rowOffsets[key] = state.rows.length;
            rowHasMore[key] = rows.length === pageSize;
            state.hasMoreRows = rowHasMore[key];
            state.apiReady = true;
            state.syncState = state.hasMoreRows ? '已连接腾讯云数据库 · 可继续加载' : '已连接腾讯云数据库';
            // 只预取一批，避免后台自动把整张大表灌进 DOM；后续按需加载。
            if (state.hasMoreRows) setTimeout(function () { if (token === rowRequest) loadMoreRows(); }, 1000);
          }).catch(function () {
            if (token === rowRequest) {
              if (!state.rows.length) state.rows = [];
              state.syncState = state.rows.length ? '首屏已加载，更多数据加载失败' : '腾讯云数据库连接失败';
            }
          });
        }, 60);
      }
      function loadMoreRows() {
        var selectedDate = state.datePages[state.datePageIndex] || '';
        var key = state.selectedDept + '|' + state.selectedSheet + '|' + selectedDate;
        if (state.loadingMore || !rowHasMore[key] || state.rows.length >= 1000 || !api || !api.getSeedingRecords) return;
        var token = rowRequest, offset = rowOffsets[key] || state.rows.length, pageSize = 100;
        state.loadingMore = true;
        api.getSeedingRecords(state.selectedDept, '全部', state.selectedSheet === '全部数据' ? '全部' : state.selectedSheet, pageSize, offset, selectedDate).then(function (rows) {
          if (token !== rowRequest || !Array.isArray(rows)) return;
          var page = rows.map(normalize);
          state.rows = state.rows.concat(page);
          rowCache[key] = state.rows.slice();
          rowOffsets[key] = offset + page.length;
          // 按滚动位置逐页加载，最多保留 1000 行，避免大表持续膨胀导致浏览器无响应。
          rowHasMore[key] = page.length === pageSize && state.rows.length < 1000;
          state.hasMoreRows = false;
          state.syncState = rowHasMore[key] ? '已加载前 ' + state.rows.length + ' 条，继续下滚加载' : '已加载前 ' + state.rows.length + ' 条';
        }).catch(function () {
          if (token === rowRequest) state.syncState = '首屏已加载，更多数据加载失败';
        }).finally(function () { state.loadingMore = false; });
      }
      function exportRows() { var headers = ['发布时间', '部门', '产品', '发布平台', '发布渠道', '笔记类型', '标题', '发布链接', '发布账号名称', '发布账号ID', '点赞', '收藏', '评论', '阅读量', '备注']; var body = filteredRows.value.map(function (r) { return [r.date, r.department, r.product, r.platform, r.source, r.noteType, r.title, r.publishLink, r.accountName, r.accountId, r.likes, r.collects, r.comments, r.views, r.remark]; }); var csv = [headers].concat(body).map(function (line) { return line.map(function (v) { return '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"'; }).join(','); }).join('\n'); var a = document.createElement('a'); a.href = URL.createObjectURL(new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' })); a.download = '种草收录-' + currentDate() + '.csv'; a.click(); }
      var scrollLoadHandler = null, tableScrollHandler = null;
      Vue.onMounted(function () {
        loadOptions().then(function () { loadDatePages(); }); loadCategoryViews(); loadSummary();
        scrollLoadHandler = function () {
          var tableElement = document.querySelector('#page-seeding-monitor-vue .srm-table-wrap');
          if (tableElement) {
            var tableTop = tableElement.getBoundingClientRect().top + window.scrollY;
            state.virtualStart = Math.max(0, Math.floor(Math.max(0, window.scrollY - tableTop) / 48) - 20);
          }
          if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 500) loadMoreRows();
        };
        window.addEventListener('scroll', scrollLoadHandler, { passive: true });
        var tableWrap = document.querySelector('#page-seeding-monitor-vue .srm-table-wrap');
        if (tableWrap) {
          tableScrollHandler = function () {
            state.virtualStart = Math.max(0, Math.floor(tableWrap.scrollTop / 48) - 20);
            if (tableWrap.scrollTop + tableWrap.clientHeight >= tableWrap.scrollHeight - 500) loadMoreRows();
          };
          tableWrap.addEventListener('scroll', tableScrollHandler, { passive: true });
        }
      });
      Vue.onBeforeUnmount(function () { if (rowTimer) clearTimeout(rowTimer); if (scrollLoadHandler) window.removeEventListener('scroll', scrollLoadHandler); var tableWrap = document.querySelector('#page-seeding-monitor-vue .srm-table-wrap'); if (tableWrap && tableScrollHandler) tableWrap.removeEventListener('scroll', tableScrollHandler); rowRequest++; });
      Vue.watch(function () { return [state.selectedDept, state.selectedSheet]; }, loadDatePages);
      Vue.watch(function () { return state.datePageIndex; }, function () { state.virtualStart = 0; loadRows(); });
      Vue.watch(function () { return state.selectedDept; }, function () { loadSummary(); });
      return { state: state, departments: departments, platforms: platforms, sources: sources, noteTypes: noteTypes, sheetNames: sheetNames, datePages: datePages, filteredRows: filteredRows, visibleRows: visibleRows, virtualTopSpace: virtualTopSpace, virtualBottomSpace: virtualBottomSpace, productOptions: productOptions, stats: stats, categoryViewTooltip: categoryViewTooltip, formatNumber: formatNumber, formatCompact: formatCompact, selectDept: selectDept, selectSheet: selectSheet, addSheet: addSheet, renameSheet: renameSheet, formatDateInput: formatDateInput, dateTone: dateTone, canEdit: canEdit, isDropdown: isDropdown, toggleDropdown: toggleDropdown, closeDropdown: closeDropdown, pickDropdown: pickDropdown, dropdownList: dropdownList, dropdownTone: dropdownTone, dropdownField: dropdownField, dropdownRow: dropdownRow, dropdownItems: dropdownItems, dropdownPick: dropdownPick, toggleSelect: toggleSelect, isSelected: isSelected, allSelected: allSelected, toggleSelectAll: toggleSelectAll, clearSelection: clearSelection, startBatchMode: startBatchMode, exitBatchMode: exitBatchMode, batchRemove: batchRemove, retryTraffic: retryTraffic, onRetryImage: onRetryImage, startEdit: startEdit, isEditing: isEditing, finishEdit: finishEdit, showDate: showDate, dateSpan: dateSpan, productTone: productTone, platformTone: platformTone, sourceTone: sourceTone, noteTone: noteTone, likesTone: likesTone, collectsTone: collectsTone, commentsTone: commentsTone, viewTier: viewTier, viewBar: viewBar, dateGroupLabel: dateGroupLabel, totals: totals, linkLabel: linkLabel, openLink: openLink, isUrl: isUrl, linkHref: linkHref, openTraffic: openTraffic, triggerTraffic: triggerTraffic, onImageChange: onImageChange, openCreate: openCreate, openEdit: openEdit, saveRecord: saveRecord, removeRow: removeRow, exportRows: exportRows };
    },
    template: `
      <div class="srm-shell"><div class="srm-console"><header class="srm-top"><div class="srm-head"><span class="srm-mark"><i class="fa-solid fa-seedling"></i></span><div><div class="srm-title">种草监测中台<span class="srm-live" :class="{warn:state.syncState!=='已连接腾讯云数据库'}"><i></i>{{state.syncState}}</span></div><div class="srm-subtitle"><b>按部门</b><em></em><b>发布账号</b><span>管理内容收录</span></div></div></div><div class="srm-top-actions"><button class="srm-btn primary srm-btn-lg" @click="openCreate"><i class="fa-solid fa-plus"></i>新增记录</button></div></header>
      <section class="srm-stat-grid"><article v-for="card in stats" :key="card.label" class="srm-stat" :class="'srm-ct-'+card.tone" :title="card.label.indexOf('品类浏览量')>=0?categoryViewTooltip:''"><div class="srm-stat-head"><span class="srm-stat-icon"><i class="fa-solid" :class="card.icon"></i></span><span>{{card.label}}</span></div><div class="srm-stat-value">{{formatCompact(card.value)}}<small>{{card.unit}}</small></div><div class="srm-stat-foot"><span class="srm-stat-sub"><i class="fa-solid" :class="card.subIcon"></i>{{card.subLabel}} <b :class="card.trendClass">{{card.subValue}}</b></span><span class="srm-stat-tag">{{card.tag}}</span></div></article></section>
      </div>
      <div class="srm-table-box">
<nav class="srm-tabs-row"><span class="srm-range"><i class="fa-regular fa-calendar"></i><b>当前收录</b></span><div class="srm-tabs"><button class="srm-tab" :class="{active:state.selectedDept==='全部'}" @click="selectDept('全部')">全部</button><button v-for="dept in departments" :key="dept" class="srm-tab" :class="{active:state.selectedDept===dept}" @click="selectDept(dept)">{{dept}}</button></div><div v-if="state.selectedDept!=='全部'" class="srm-sheet-tabs" aria-label="当前部门 Sheet 页"><button v-for="sheet in sheetNames" :key="sheet" class="srm-sheet-tab" :class="{active:state.selectedSheet===sheet}" @click="selectSheet(sheet)"><span>{{sheet}}</span><i v-if="sheet!=='全部数据'" class="fa-solid fa-pen srm-sheet-edit" title="修改名称" @click.stop="renameSheet(sheet)"></i></button><button class="srm-sheet-tab srm-sheet-add" title="新增 Sheet" @click="addSheet"><i class="fa-solid fa-plus"></i>新增</button></div></nav>
      <section class="srm-toolbar"><span class="srm-filter-context"><i class="fa-solid fa-filter"></i>{{state.selectedDept==='全部'?'全部部门':state.selectedDept}} · {{state.selectedSheet}} <em>{{filteredRows.length}}</em></span><div class="srm-toolbar-right"><span class="srm-toolbar-tip"><i class="fa-solid fa-hand-pointer"></i>双击单元格可编辑</span><button class="srm-btn" @click="exportRows"><i class="fa-solid fa-download"></i>导出</button><template v-if="!state.batchMode"><button class="srm-btn danger" @click="startBatchMode"><i class="fa-solid fa-trash-can"></i>批量删除</button></template><template v-else><button class="srm-btn danger" :disabled="!state.selectedIds.length" @click="batchRemove"><i class="fa-solid fa-trash-can"></i>删除已选<span v-if="state.selectedIds.length" class="srm-batch-num">{{state.selectedIds.length}}</span></button><button class="srm-btn" @click="exitBatchMode">取消</button></template><button class="srm-btn primary" @click="openCreate"><i class="fa-solid fa-plus"></i>新增记录</button></div></section>
      <div class="srm-table-wrap"><table class="srm-table srm-entry-table"><thead><tr class="srm-g"><th class="srm-g-plain srm-g-check" rowspan="2"><label v-if="state.batchMode" class="srm-check-all" title="全选本页"><input type="checkbox" :checked="allSelected()" @change="toggleSelectAll($event.target.checked)"><span></span></label><span v-else>序号</span></th><th class="srm-g-plain srm-g-div" rowspan="2">发布时间</th><th class="srm-g-content" colspan="8">内 容 信 息</th><th class="srm-g-metric srm-g-div" colspan="4">互 动 数 据</th><th class="srm-g-plain srm-g-div" rowspan="2">流量分析</th><th class="srm-g-plain" rowspan="2">备注</th><th class="srm-g-plain" rowspan="2">操作</th></tr><tr class="srm-h"><th>产品</th><th>发布平台</th><th>发布渠道</th><th>笔记类型</th><th>标题</th><th>发布链接</th><th>发布账号 / 账号ID</th><th>负责人</th><th class="srm-g-div srm-metric">点赞</th><th class="srm-metric">收藏</th><th class="srm-metric">评论</th><th class="srm-metric">阅读量</th></tr></thead><tbody><tr v-for="(row,index) in filteredRows" :key="row.id" :class="{ 'srm-row-locked': !canEdit(row) }"><td class="srm-check-cell"><label v-if="state.batchMode" class="srm-check-row" @click.stop><input type="checkbox" :checked="isSelected(row)" :disabled="!canEdit(row)" @change="toggleSelect(row,$event.target.checked)"><span></span></label><span v-else>{{index+1}}</span></td><td class="srm-date-cell" :class="dateTone(row)" @dblclick="startEdit(row,'date')"><input v-if="isEditing(row,'date')" v-model="row.date" type="text" inputmode="numeric" maxlength="10" @input="formatDateInput(row)" @blur="finishEdit(row)"><template v-else><b>{{row.date}}</b><small>{{row.department}}</small></template></td><td><button type="button" class="srm-chip srm-chip-prod srm-dd-trigger" :class="[productTone(row),{open:isDropdown(row,'product')}]" :title="'产品：'+(row.product||'')" :disabled="!canEdit(row)" @click.stop="toggleDropdown(row,'product',$event)"><span class="srm-dd-text">{{row.product}}</span><i class="fa-solid fa-chevron-down srm-dd-caret"></i></button></td><td><button type="button" class="srm-chip srm-chip-plat srm-dd-trigger" :class="[platformTone(row),{open:isDropdown(row,'platform')}]" :title="'发布平台：'+(row.platform||'')" :disabled="!canEdit(row)" @click.stop="toggleDropdown(row,'platform',$event)"><i></i><span class="srm-dd-text">{{row.platform}}</span><i class="fa-solid fa-chevron-down srm-dd-caret"></i></button></td><td><button type="button" class="srm-chip srm-chip-src srm-dd-trigger" :class="[sourceTone(row),{open:isDropdown(row,'source')}]" :title="'发布渠道：'+(row.source||'')" :disabled="!canEdit(row)" @click.stop="toggleDropdown(row,'source',$event)"><span class="srm-dd-text">{{row.source}}</span><i class="fa-solid fa-chevron-down srm-dd-caret"></i></button></td><td><button type="button" class="srm-chip srm-chip-type srm-dd-trigger" :class="[noteTone(row),{open:isDropdown(row,'noteType')}]" :title="'笔记类型：'+(row.noteType||'')" :disabled="!canEdit(row)" @click.stop="toggleDropdown(row,'noteType',$event)"><span class="srm-dd-text">{{row.noteType}}</span><i class="fa-solid fa-chevron-down srm-dd-caret"></i></button></td><td class="srm-title-cell" @dblclick="startEdit(row,'title')"><input v-if="isEditing(row,'title')" v-model="row.title" @blur="finishEdit(row)" @keyup.enter="finishEdit(row)"><a v-else class="srm-title-link" :class="{nolink:!linkHref(row)}" :href="linkHref(row)||'#'" :target="linkHref(row)?'_blank':null" :title="linkHref(row)?('打开作品页：'+row.publishLink):'未填写有效发布链接'" @click="!linkHref(row)&&$event.preventDefault()">{{row.title||'未填写标题'}}</a></td><td><button class="srm-link-state" :class="linkHref(row)?'done':'todo'" :disabled="!linkHref(row)" :title="linkHref(row)?('打开作品页：'+row.publishLink):(row.publishLink?'该值不是有效链接：'+row.publishLink:'未填写发布链接')" @click.stop="openLink(row)"><i class="fa-solid" :class="linkHref(row)?'fa-arrow-up-right-from-square':'fa-minus'"></i>{{linkHref(row)?'已输入':(row.publishLink?'链接无效':'输入链接')}}</button></td><td class="srm-acct"><input v-if="isEditing(row,'accountName')" v-model="row.accountName" @blur="finishEdit(row)" @keyup.enter="finishEdit(row)"><b v-else @dblclick="startEdit(row,'accountName')">{{row.accountName||'—'}}</b><input v-if="isEditing(row,'accountId')" v-model="row.accountId" @blur="finishEdit(row)" @keyup.enter="finishEdit(row)"><small v-else @dblclick="startEdit(row,'accountId')">{{row.accountId||'—'}}</small></td><td class="srm-responsible-cell">{{row.responsible||'—'}}</td><td class="srm-metric" :class="likesTone(row)" @dblclick="startEdit(row,'likes')"><input v-if="isEditing(row,'likes')" v-model.number="row.likes" type="number" @blur="finishEdit(row)"><span v-else class="srm-iv"><i class="fa-solid fa-heart"></i>{{formatNumber(row.likes)}}</span></td><td class="srm-metric" :class="collectsTone(row)" @dblclick="startEdit(row,'collects')"><input v-if="isEditing(row,'collects')" v-model.number="row.collects" type="number" @blur="finishEdit(row)"><span v-else class="srm-iv"><i class="fa-solid fa-bookmark"></i>{{formatNumber(row.collects)}}</span></td><td class="srm-metric" :class="commentsTone(row)" @dblclick="startEdit(row,'comments')"><input v-if="isEditing(row,'comments')" v-model.number="row.comments" type="number" @blur="finishEdit(row)"><span v-else class="srm-iv"><i class="fa-solid fa-comment"></i>{{formatNumber(row.comments)}}</span></td><td class="srm-metric srm-views" :class="'srm-v-'+viewTier(row)" @dblclick="startEdit(row,'views')"><input v-if="isEditing(row,'views')" v-model.number="row.views" type="number" @blur="finishEdit(row)"><div v-else class="srm-view"><span class="srm-view-val">{{formatNumber(row.views)}}<span v-if="viewTier(row)!=='normal'" class="srm-heat" :class="'srm-heat-'+viewTier(row)"><i class="fa-solid" :class="viewTier(row)==='hot'?'fa-fire':'fa-bolt'"></i>{{viewTier(row)==='hot'?'爆':'热'}}</span></span><span class="srm-view-bar"><span :style="{width:viewBar(row)}"></span></span></div></td><td class="srm-center-cell"><input :id="'traffic-'+row.id" type="file" accept="image/*" hidden @change="onImageChange($event,row)"><button class="srm-icon-btn srm-traffic-btn" :class="row.trafficImage?'has':'no'" :title="row.trafficImage?'查看已插入图片':'插入流量分析图片'" @click="row.trafficImage?openTraffic(row):triggerTraffic(row)"><i class="fa-solid" :class="row.trafficImage?'fa-eye':'fa-plus'"></i></button></td><td class="srm-remark-cell" @dblclick="startEdit(row,'remark')"><input v-if="isEditing(row,'remark')" v-model="row.remark" @blur="finishEdit(row)" @keyup.enter="finishEdit(row)"><span v-else>{{row.remark||'—'}}</span></td><td><div class="srm-row-actions"><button class="srm-icon-btn" @click="openEdit(row)" :disabled="!canEdit(row)" title="编辑"><i class="fa-solid fa-pen"></i></button><button class="srm-icon-btn danger" @click="removeRow(row)" :disabled="!canEdit(row)" title="删除"><i class="fa-solid fa-trash"></i></button></div></td></tr><tr v-if="!filteredRows.length"><td colspan="17" class="srm-empty">暂无收录记录，点击右上角新增记录</td></tr></tbody><tfoot v-if="filteredRows.length"><tr><td colspan="17"><div class="srm-sum"><span>本页合计</span><span class="srm-s1">笔记 <b>{{totals.count}}</b> 篇</span><span class="srm-s2">点赞 <b>{{formatNumber(totals.likes)}}</b></span><span class="srm-s3">收藏 <b>{{formatNumber(totals.collects)}}</b></span><span class="srm-s4">阅读量 <b>{{formatCompact(totals.views)}}</b></span><span class="srm-dim">爆文 {{totals.hot}} 篇 · 占比 {{totals.rate}}%</span></div></td></tr></tfoot></table></div>
      </div>
      <teleport to="body"><div v-if="state.openDropdown&&state.dropdownRect" class="srm-dd-mask" @click="closeDropdown"></div><div v-if="state.openDropdown&&state.dropdownRect" class="srm-dd-panel" :class="{'srm-dd-up': state.dropdownRect.spaceBelow < 260}" :style="{left:state.dropdownRect.left+'px',top:state.dropdownRect.top+'px',width:state.dropdownRect.width+'px'}"><div class="srm-dd-head"><i class="fa-solid fa-list-ul"></i>{{({product:'选择产品',platform:'选择发布平台',source:'选择发布渠道',noteType:'选择笔记类型'})[dropdownField()]||'请选择'}}</div><div class="srm-dd-list"><button v-for="opt in dropdownItems()" :key="opt" type="button" class="srm-dd-item" :class="[dropdownTone(dropdownField(),opt),{on:dropdownRow()&&dropdownRow()[dropdownField()]===opt}]" @click="dropdownPick(opt)"><span class="srm-dd-dot"></span><span class="srm-dd-label">{{opt}}</span><i class="fa-solid fa-check srm-dd-check"></i></button></div></div></teleport>
      <div v-if="state.recordModal" class="srm-mask" @click.self="state.recordModal=false"><div class="srm-modal"><div class="srm-modal-head"><div class="srm-modal-title">{{state.editingId?'编辑收录记录':'新增收录记录'}}</div><button class="srm-btn icon" @click="state.recordModal=false"><i class="fa-solid fa-xmark"></i></button></div><div class="srm-modal-body"><div class="srm-form-grid"><div class="srm-field"><label>发布时间 *</label><input v-model="state.form.date" type="text" inputmode="numeric" maxlength="10" placeholder="YYYYMMDD" @input="formatDateInput(state.form)"></div><div class="srm-field"><label>部门 *</label><select v-model="state.form.department"><option v-for="dept in departments" :key="dept">{{dept}}</option></select></div><div class="srm-field"><label>产品 *</label><select v-model="state.form.product"><option v-for="p in productOptions" :key="p">{{p}}</option></select></div><div class="srm-field"><label>发布平台 *</label><select v-model="state.form.platform"><option v-for="p in platforms" :key="p">{{p}}</option></select></div><div class="srm-field"><label>发布渠道 *</label><select v-model="state.form.source"><option v-for="s in sources" :key="s">{{s}}</option></select></div><div class="srm-field"><label>笔记类型 *</label><select v-model="state.form.noteType"><option v-for="t in noteTypes" :key="t">{{t}}</option></select></div><div class="srm-field full"><label>标题 *</label><input v-model="state.form.title" placeholder="填写作品标题"></div><div class="srm-field full"><label>发布链接</label><input v-model="state.form.publishLink" placeholder="https://"></div><div class="srm-field"><label>发布账号名称</label><input v-model="state.form.accountName"></div><div class="srm-field"><label>发布账号ID</label><input v-model="state.form.accountId"></div><div class="srm-field"><label>负责人</label><input v-model="state.form.responsible" readonly></div><div class="srm-field"><label>点赞</label><input v-model.number="state.form.likes" type="number" min="0"></div><div class="srm-field"><label>收藏</label><input v-model.number="state.form.collects" type="number" min="0"></div><div class="srm-field"><label>评论</label><input v-model.number="state.form.comments" type="number" min="0"></div><div class="srm-field"><label>阅读量</label><input v-model.number="state.form.views" type="number" min="0"></div><div class="srm-field full"><label>备注</label><input v-model="state.form.remark"></div><div class="srm-field full"><label>流量分析图片</label><label class="srm-upload"><input type="file" accept="image/*" hidden @change="onImageChange($event,state.form)"><img v-if="state.form.trafficImage" :src="state.form.trafficImage"><span v-else><i class="fa-solid fa-plus"></i> 点击插入图片</span></label></div></div></div><div class="srm-modal-foot"><button class="srm-btn" @click="state.recordModal=false">取消</button><button class="srm-btn primary" @click="saveRecord">保存记录</button></div></div></div>
      <div v-if="state.trafficModal" class="srm-mask" @click.self="state.trafficModal=false"><div class="srm-modal" style="width:min(900px,100%)"><div class="srm-modal-head"><div class="srm-modal-title">流量分析 · {{state.trafficRow&&state.trafficRow.title}}</div><button class="srm-btn icon" @click="state.trafficModal=false"><i class="fa-solid fa-xmark"></i></button></div><div class="srm-modal-body srm-traffic-body"><img class="srm-traffic-preview" :src="state.trafficRow&&state.trafficRow.trafficImage"><input :id="'traffic-re-'+(state.trafficRow&&state.trafficRow.id)" type="file" accept="image/*" hidden @change="onRetryImage($event,state.trafficRow)"></div><div class="srm-modal-foot srm-traffic-foot"><span class="srm-traffic-hint"><i class="fa-solid fa-circle-info"></i>如需更换，请选择新的流量分析图片</span><button class="srm-btn primary" @click="retryTraffic(state.trafficRow)"><i class="fa-solid fa-cloud-arrow-up"></i>重新上传图片</button></div></div></div></div>`
  };
  window.SeedingPage = app;
  window.mountSeedingVue = function () {
    var mount = document.getElementById('page-seeding-monitor-vue');
    if (!mount || mount.__vue_app__) return;
    // 日期翻页和虚拟窗口注入到现有模板，复用已有按钮样式，不改 CSS 文件。
    app.template = app.template
      .replace('<div class="srm-table-wrap">', '<div class="srm-toolbar-right"><button class="srm-btn" :disabled="state.datePageIndex<=0" @click="state.datePageIndex=Math.max(0,state.datePageIndex-1)"><i class="fa-solid fa-chevron-left"></i>上一日</button><select class="srm-btn" v-model.number="state.datePageIndex"><option v-for="(day,index) in datePages" :key="day" :value="index">{{day}} · 第 {{index+1}} / {{datePages.length}} 日</option></select><button class="srm-btn" :disabled="state.datePageIndex>=datePages.length-1" @click="state.datePageIndex=Math.min(datePages.length-1,state.datePageIndex+1)">下一日<i class="fa-solid fa-chevron-right"></i></button></div><div class="srm-table-wrap">')
      .replace('<tbody>', '<tbody><tr v-if="virtualTopSpace"><td colspan="17" :height="virtualTopSpace"></td></tr>')
      .replace('v-for="(row,index) in filteredRows"', 'v-for="(row,index) in visibleRows"')
      .replace('{{index+1}}', '{{state.virtualStart+index+1}}')
      .replace('<tr v-if="!filteredRows.length">', '<tr v-if="virtualBottomSpace"><td colspan="17" :height="virtualBottomSpace"></td></tr><tr v-if="!filteredRows.length">');
    Vue.createApp(app).mount(mount);
  };
})();
