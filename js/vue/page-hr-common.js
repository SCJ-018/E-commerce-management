/**
 * 人事数据中心 — 前端公共层
 * classic script（与 app.js 共用全局词法作用域，符合既有 Vue 迁移方案）
 * 提供：
 *   HR.CardSelector  表格切换的卡片选择器组件
 *   HR.makePage(cfg) 通用「单表独立页」工厂（KPI 卡片 + 图表 + 表格 + 新增/编辑/删除 + 导出 CSV）
 *   HR.util          数值/百分比/CSV/月份等工具
 * 依赖：Vue 3 全局构建、EcomUI（components.js）、App.showToast、ApiService
 * XSS：所有数据一律走 {{ }} 插值，不使用 v-html
 */
var HR = (function () {
  'use strict';

  // ==================== 工具函数 ====================
  var util = {
    /** 从任意字符串里取出数字（"8,000元" → 8000） */
    num: function (v) {
      if (v === null || v === undefined) return 0;
      var n = parseFloat(String(v).replace(/[^0-9.\-]/g, ''));
      return isNaN(n) ? 0 : n;
    },
    /** 百分比：pct(3, 10) → "30.0%" */
    pct: function (a, b) {
      if (!b) return '0%';
      return (a / b * 100).toFixed(1) + '%';
    },
    /** 千分位整数 */
    fmt: function (n) {
      n = Math.round(util.num(n));
      return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    },
    /** 取 YYYY-MM */
    month: function (v) {
      var s = String(v || '').trim();
      if (!s) return '';
      var m = s.match(/^(\d{4})-(\d{2})/);
      return m ? m[1] + '-' + m[2] : '';
    },
    /** 当前月 YYYY-MM */
    curMonth: function () {
      var d = new Date();
      return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
    },
    /** 近 n 个月（含当前月） */
    lastMonths: function (n) {
      var out = [], d = new Date();
      for (var i = n - 1; i >= 0; i--) {
        var t = new Date(d.getFullYear(), d.getMonth() - i, 1);
        out.push(t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0'));
      }
      return out;
    },
    /** 按字段分组计数 → [{name, value}]，按数量降序 */
    group: function (rows, field, limit) {
      var map = {}, order = [];
      (rows || []).forEach(function (r) {
        var k = String(r[field] || '').trim() || '未填写';
        if (!(k in map)) { map[k] = 0; order.push(k); }
        map[k]++;
      });
      var out = order.map(function (k) { return { name: k, value: map[k] }; });
      out.sort(function (a, b) { return b.value - a.value; });
      return limit ? out.slice(0, limit) : out;
    },
    /** 按字段求和（数值字段） */
    sum: function (rows, field) {
      var s = 0;
      (rows || []).forEach(function (r) { s += util.num(r[field]); });
      return s;
    },
    /** 按字段求平均 */
    avg: function (rows, field) {
      if (!rows || !rows.length) return 0;
      return util.sum(rows, field) / rows.length;
    },
    /** 统计满足某值的行数 */
    countIf: function (rows, field, values) {
      var vs = Array.isArray(values) ? values : [values];
      return (rows || []).filter(function (r) {
        return vs.indexOf(String(r[field] || '').trim()) >= 0;
      }).length;
    },
    /** 导出 CSV（带 BOM，Excel 中文不乱码） */
    csv: function (filename, columns, rows) {
      var esc = function (v) {
        var s = (v === null || v === undefined) ? '' : String(v);
        return '"' + s.replace(/"/g, '""') + '"';
      };
      var lines = [columns.map(esc).join(',')];
      (rows || []).forEach(function (r) {
        lines.push(columns.map(function (c) { return esc(r[c]); }).join(','));
      });
      var blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 1500);
    },
  };

  var PALETTE = ['#4f46e5', '#0891b2', '#059669', '#d97706', '#e11d48', '#7c3aed',
                 '#0284c7', '#ca8a04', '#db2777', '#16a34a', '#ea580c', '#6366f1'];

  // ==================== 卡片选择器（表格切换控件） ====================
  var CardSelector = {
    props: {
      cards: { type: Array, default: function () { return []; } },
      active: { type: String, default: '' },
    },
    emits: ['select'],
    methods: {
      pick: function (c) {
        if (c.key !== this.active) this.$emit('select', c.key);
      },
      styleFor: function (c, on) {
        return on
          ? 'border-color:' + c.color + ';box-shadow:0 6px 18px ' + c.color + '22;background:' + c.color + '0d'
          : '';
      },
    },
    template: `
<div class="hr-card-selector">
  <button v-for="c in cards" :key="c.key" type="button" class="hr-card-item"
          :class="{ active: c.key === active }" :style="styleFor(c, c.key === active)" @click="pick(c)">
    <span class="hr-card-glow" :style="'background:' + c.color"></span>
    <span class="hr-card-icon" :style="'background:' + c.color + '18;color:' + c.color"><i :class="c.icon"></i></span>
    <span class="hr-card-main">
      <span class="hr-card-title">{{ c.label }}</span>
      <span class="hr-card-desc">{{ c.desc }}</span>
    </span>
    <span class="hr-card-count" :style="'color:' + c.color">{{ c.count === null || c.count === undefined ? '—' : c.count }}<em>条</em></span>
  </button>
</div>
    `,
  };

  // ==================== 通用单表页面工厂 ====================
  /**
   * cfg = {
   *   key, label, perm, icon, color, desc,
   *   fields: [{name, type, required, options}],
   *   pk: [列名],
   *   chips: function(rows) → [{label, value}],
   *   kpis:  function(rows) → [{label, value, sub, icon, tone}],   tone: emerald/indigo/cyan/amber/rose/violet
   *   charts: [{title, subtitle, span:1|2, option: function(rows) → echarts option}],
   *   badgeClass: function(row, key) → css class（可选）
   * }
   */
  function makePage(cfg) {
    return {
      components: {
        'ecom-pagination': EcomUI.Pagination,
        'ecom-modal': EcomUI.Modal,
      },
      data: function () {
        return {
          cfg: cfg,
          rows: [],
          loading: true,
          search: '',
          searchLocked: true,
          page: 1,
          pageSize: 15,
          showForm: false,
          editing: false,
          editingPk: null,
          form: {},
          delTarget: null,
        };
      },
      computed: {
        columns: function () { return cfg.fields; },
        chips: function () { return cfg.chips ? cfg.chips(this.rows) : []; },
        kpis: function () { return cfg.kpis ? cfg.kpis(this.rows) : []; },
        charts: function () { return cfg.charts || []; },
        filtered: function () {
          var kw = this.search.trim().toLowerCase();
          if (!kw) return this.rows;
          var self = this;
          return this.rows.filter(function (r) {
            return cfg.fields.some(function (f) {
              return String(r[f.name] || '').toLowerCase().indexOf(kw) >= 0;
            });
          });
        },
        totalPages: function () {
          return Math.max(1, Math.ceil(this.filtered.length / this.pageSize));
        },
        pageRows: function () {
          var start = (this.page - 1) * this.pageSize;
          return this.filtered.slice(start, start + this.pageSize);
        },
      },
      watch: {
        search: function () { this.page = 1; },
        totalPages: function (tp) { if (this.page > tp) this.page = tp; },
      },
      methods: {
        unlockSearch: function () { this.searchLocked = false; },
        goPage: function (n) { this.page = n; },
        cellStyle: function (f) {
          if (f.type === 'number') return 'text-align:right;font-variant-numeric:tabular-nums';
          return '';
        },
        badgeClass: function (row, key) {
          if (typeof cfg.badgeClass === 'function') {
            var c = cfg.badgeClass(row, key);
            if (c) return c;
          }
          return 'badge-gray';
        },
        isBadge: function (f) { return !!(f.options && f.options.length); },
        pkOf: function (row) {
          var pk = {};
          cfg.pk.forEach(function (c) { pk[c] = row[c]; });
          return pk;
        },
        load: function () {
          var self = this;
          this.loading = true;
          ApiService.getHrRows(cfg.key).then(function (rows) {
            self.rows = rows || [];
            self.loading = false;
            self.$nextTick(function () { self.renderCharts(); });
          });
        },
        emptyForm: function () {
          var f = {};
          cfg.fields.forEach(function (x) { f[x.name] = ''; });
          return f;
        },
        openAdd: function () {
          this.editing = false;
          this.editingPk = null;
          this.form = this.emptyForm();
          this.showForm = true;
        },
        openEdit: function (row) {
          this.editing = true;
          this.editingPk = this.pkOf(row);
          var f = {};
          cfg.fields.forEach(function (x) { f[x.name] = row[x.name] === null || row[x.name] === undefined ? '' : String(row[x.name]); });
          this.form = f;
          this.showForm = true;
        },
        closeForm: function () { this.showForm = false; },
        save: function () {
          var self = this;
          var payload = {};
          cfg.fields.forEach(function (x) { payload[x.name] = (self.form[x.name] || '').trim(); });
          for (var i = 0; i < cfg.fields.length; i++) {
            var fd = cfg.fields[i];
            if (fd.required && !payload[fd.name]) {
              App.showToast(fd.name + ' 为必填项', 'error');
              return;
            }
          }
          var req;
          if (this.editing) {
            payload._pk = this.editingPk;
            req = ApiService.updateHrRow(cfg.key, payload);
          } else {
            req = ApiService.createHrRow(cfg.key, payload);
          }
          req.then(function (res) {
            if (res === null || res === undefined) {
              App.showToast('保存失败，请检查后端服务或数据是否重复', 'error');
              return;
            }
            self.showForm = false;
            App.showToast(self.editing ? '已保存' : '已新增', 'success');
            if (typeof HR.onDataChange === 'function') HR.onDataChange();
            self.load();
          });
        },
        askDelete: function (row) { this.delTarget = row; },
        cancelDelete: function () { this.delTarget = null; },
        doDelete: function () {
          var self = this;
          if (!this.delTarget) return;
          ApiService.deleteHrRow(cfg.key, { _pk: this.pkOf(this.delTarget) }).then(function (res) {
            if (res === null || res === undefined) { App.showToast('删除失败，请刷新后重试', 'error'); return; }
            App.showToast('已删除', 'success');
            self.delTarget = null;
            if (typeof HR.onDataChange === 'function') HR.onDataChange();
            self.load();
          });
        },
        exportCsv: function () {
          var cols = cfg.fields.map(function (f) { return f.name; });
          var stamp = new Date().toISOString().slice(0, 10);
          util.csv('人事数据-' + cfg.label + '-' + stamp + '.csv', cols, this.filtered);
          App.showToast('已导出 ' + this.filtered.length + ' 条', 'success');
        },
        // ---------------- 图表 ----------------
        renderCharts: function () {
          this.disposeCharts();
          if (typeof echarts === 'undefined' || !echarts.init) return;
          if (!this.rows.length) return;
          var self = this;
          this._charts = [];
          this.charts.forEach(function (c, i) {
            var dom = self.$refs['chartBox' + i];
            if (Array.isArray(dom)) dom = dom[0];   // 兼容 Vue 把 v-for 内的 ref 收集成数组
            if (!dom) return;
            var inst = echarts.init(dom);
            inst.setOption(c.option(self.rows, PALETTE));
            self._charts.push(inst);
          });
        },
        disposeCharts: function () {
          (this._charts || []).forEach(function (c) { try { c.dispose(); } catch (e) {} });
          this._charts = [];
        },
        onResize: function () {
          (this._charts || []).forEach(function (c) { try { c.resize(); } catch (e) {} });
        },
      },
      mounted: function () {
        this.load();
        window.addEventListener('resize', this.onResize);
      },
      beforeUnmount: function () {
        this.disposeCharts();
        window.removeEventListener('resize', this.onResize);
      },
      template: `
<div class="hr-page">
  <div class="dashboard-header">
    <div class="dh-left">
      <div class="dh-icon" :style="'background:linear-gradient(135deg,' + cfg.color + ',' + cfg.color + 'aa);box-shadow:0 2px 8px ' + cfg.color + '33'">
        <i :class="cfg.icon" style="color:#fff;font-size:18px"></i>
      </div>
      <div class="dh-title-group">
        <h2 class="dh-title">{{ cfg.label }}</h2>
        <span class="dh-subtitle">{{ cfg.desc }}</span>
        <span class="dh-status"><i class="fa-solid fa-circle" style="font-size:6px;color:#10b981;margin-right:4px"></i>实时数据 · 共 <span>{{ rows.length }}</span> 条记录</span>
      </div>
    </div>
    <div class="dh-chips">
      <div class="dh-chip" v-for="(c, i) in chips" :key="i">
        <span class="dh-chip-label">{{ c.label }}</span>
        <span class="dh-chip-value" :style="c.color ? 'color:' + c.color : ''">{{ c.value }}</span>
      </div>
    </div>
  </div>

  <div class="mkt-cards-grid">
    <div v-for="(k, i) in kpis" :key="i" class="mkt-card" :class="'mkt-card--' + (k.tone || 'indigo')">
      <div class="mkt-card-header">
        <div class="mkt-card-icon"><i :class="k.icon"></i></div>
        <span class="mkt-card-label">{{ k.label }}</span>
      </div>
      <div class="mkt-card-body">
        <div class="mkt-card-value">{{ k.value }}</div>
        <div class="mkt-card-trend neutral"><span>{{ k.sub }}</span></div>
      </div>
    </div>
  </div>

  <div class="hr-charts-row">
    <div v-for="(c, i) in charts" :key="i" class="chart-module hr-chart-card" :style="c.span === 2 ? 'grid-column:span 2' : ''">
      <div class="hr-chart-head">
        <span class="hr-chart-title">{{ c.title }}</span>
        <span class="hr-chart-sub">{{ c.subtitle }}</span>
      </div>
      <div :ref="'chartBox' + i" class="chart-module-body" style="min-height:280px"></div>
    </div>
  </div>

  <div class="ps-table-card">
    <div class="ps-table-header">
      <div class="ps-table-title-group">
        <h3>{{ cfg.label }}</h3>
        <span class="ps-table-badge" :style="'background:' + cfg.color + '14;color:' + cfg.color">共 {{ filtered.length }} 条</span>
      </div>
      <div class="ps-table-tools">
        <div class="ps-search-wrap"><i class="fa-solid fa-search"></i>
          <input type="text" class="ps-search-input" v-model="search" placeholder="全字段搜索..." autocomplete="off" :readonly="searchLocked" @focus="unlockSearch">
        </div>
        <button class="ap-btn-primary hr-btn-ghost" @click="exportCsv"><i class="fa-solid fa-file-export"></i> 导出CSV</button>
        <button class="ap-btn-primary" @click="openAdd"><i class="fa-solid fa-plus"></i> 新增数据</button>
      </div>
    </div>
    <div class="ps-table-wrap">
      <table class="ps-store-table">
        <thead>
          <tr>
            <th v-for="f in columns" :key="f.name">{{ f.label || f.name }}</th>
            <th style="width:120px;text-align:center">操作</th>
          </tr>
        </thead>
        <tbody v-if="pageRows.length">
          <tr v-for="(r, ri) in pageRows" :key="ri">
            <td v-for="f in columns" :key="f.name" :style="cellStyle(f)">
              <span v-if="isBadge(f) && r[f.name]" class="badge" :class="badgeClass(r, f.name)">{{ r[f.name] }}</span>
              <template v-else>{{ r[f.name] }}</template>
            </td>
            <td style="text-align:center;white-space:nowrap">
              <button class="hr-row-btn" @click="openEdit(r)"><i class="fa-solid fa-pen"></i></button>
              <button class="hr-row-btn danger" @click="askDelete(r)"><i class="fa-solid fa-trash"></i></button>
            </td>
          </tr>
        </tbody>
        <tbody v-else>
          <tr><td :colspan="columns.length + 1" style="text-align:center;padding:36px;color:#94a3b8">
            {{ loading ? '加载中...' : '暂无数据，点击右上角「新增数据」录入第一条' }}
          </td></tr>
        </tbody>
      </table>
    </div>
    <ecom-pagination :page="page" :total-pages="totalPages" :total="filtered.length" @change="goPage"></ecom-pagination>
  </div>

  <ecom-modal :visible="showForm" :title="(editing ? '编辑 - ' : '新增 - ') + cfg.label" width="820px" @close="closeForm" @save="save">
    <div class="hr-form-grid">
      <div v-for="f in columns" :key="f.name" class="hr-form-item" :class="{ 'hr-form-item--full': f.type === 'textarea' }">
        <label>{{ f.name }}<em v-if="f.required" class="hr-req">*</em></label>
        <input v-if="f.type !== 'textarea'" class="form-input" :type="f.type === 'date' ? 'date' : (f.type === 'time' ? 'time' : (f.type === 'number' ? 'number' : 'text'))"
               v-model="form[f.name]" :list="'hrdl-' + cfg.key + '-' + f.name" :placeholder="f.placeholder || ''"
               :disabled="editing && cfg.pk.indexOf(f.name) >= 0" :title="editing && cfg.pk.indexOf(f.name) >= 0 ? '主键字段不可修改' : ''">
        <textarea v-else class="form-input" rows="2" v-model="form[f.name]" :placeholder="f.placeholder || ''"></textarea>
        <datalist v-if="f.options && f.options.length" :id="'hrdl-' + cfg.key + '-' + f.name">
          <option v-for="o in f.options" :key="o" :value="o"></option>
        </datalist>
      </div>
    </div>
  </ecom-modal>

  <ecom-modal :visible="!!delTarget" title="删除确认" width="420px" save-text="删除" :danger="true" @close="cancelDelete" @save="doDelete">
    <p style="color:#475569;line-height:1.8">确定要删除这条记录吗？<br><span style="color:#94a3b8;font-size:12px">删除后不可恢复，请谨慎操作。</span></p>
  </ecom-modal>
</div>
      `,
    };
  }

  return {
    util: util,
    palette: PALETTE,
    CardSelector: CardSelector,
    makePage: makePage,
  };
})();
window.HR = HR;
