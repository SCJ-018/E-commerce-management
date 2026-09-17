/**
 * Vue 迁移公共组件层（阶段 3）
 * classic script，顶层 const EcomUI（全局词法作用域，不挂 window、不与既有变量冲突）
 * 供 js/vue/ 下各迁移页面复用：分页 Pagination / 弹窗 Modal / 通用表格 DataTable
 * 加载顺序：必须早于任何引用 EcomUI 的页面脚本（page-hr.js 等）
 */
const EcomUI = {};

// ==================== 分页 ====================
// 复刻旧版 .ps-table-footer + .ps-pagination-btns 的省略号分页逻辑
EcomUI.Pagination = {
  props: {
    page: { type: Number, default: 1 },
    totalPages: { type: Number, default: 1 },
    total: { type: Number, default: 0 },
    // 计数单位，默认「条」（不改任何现有页面的显示）；历史选品记录传「次运行」
    unit: { type: String, default: '条' },
  },
  emits: ['change'],
  computed: {
    pages: function () {
      var tp = this.totalPages, p = this.page, out = [];
      for (var i = 1; i <= tp; i++) {
        if (tp <= 7 || i === 1 || i === tp || (i >= p - 1 && i <= p + 1)) {
          out.push({ t: 'page', n: i });
        } else if (i === p - 2 || i === p + 2) {
          out.push({ t: 'gap' });
        }
      }
      return out;
    },
  },
  methods: {
    go: function (n) {
      if (n < 1 || n > this.totalPages || n === this.page) return;
      this.$emit('change', n);
    },
  },
  template: `
<div class="ps-table-footer">
  <span>第 {{ page }} / {{ totalPages }} 页，共 {{ total }} {{ unit }}</span>
  <div class="ps-pagination-btns">
    <button :disabled="page <= 1" @click="go(page - 1)"><i class="fa-solid fa-chevron-left"></i></button>
    <template v-for="(it, idx) in pages" :key="idx">
      <button v-if="it.t === 'page'" :class="{ active: it.n === page }" @click="go(it.n)">{{ it.n }}</button>
      <button v-else disabled>...</button>
    </template>
    <button :disabled="page >= totalPages" @click="go(page + 1)"><i class="fa-solid fa-chevron-right"></i></button>
  </div>
</div>
  `,
};

// ==================== 弹窗 ====================
// 复刻旧版 #formModal（.modal / .modal-content / .modal-header / .modal-body / .modal-footer）
EcomUI.Modal = {
  props: {
    visible: { type: Boolean, default: false },
    title: { type: String, default: '' },
    width: { type: String, default: '' },
    saveText: { type: String, default: '保存' },
    danger: { type: Boolean, default: false },
  },
  emits: ['close', 'save'],
  methods: {
    onClose: function () { this.$emit('close'); },
    onSave: function () { this.$emit('save'); },
  },
  template: `
<div class="modal" :class="{ hidden: !visible }">
  <div class="modal-content" :style="width ? 'max-width:' + width : ''">
    <div class="modal-header">
      <h3>{{ title }}</h3>
      <button type="button" class="modal-close" title="关闭" aria-label="关闭" @click="onClose">&times;</button>
    </div>
    <div class="modal-body"><slot></slot></div>
    <div class="modal-footer">
      <button class="btn btn-outline" @click="onClose">取消</button>
      <button class="btn" :class="danger ? 'btn-danger' : 'btn-primary'" @click="onSave">{{ saveText }}</button>
    </div>
  </div>
</div>
  `,
};

// ==================== 通用表格 ====================
// columns: [{ key, label, type }]，type 支持 text(默认)/strong/mono/badge
// 文本一律走 {{ }} 插值（Vue 自动转义，防 XSS）；badge 通过 badgeClass 函数决定颜色
EcomUI.DataTable = {
  props: {
    columns: { type: Array, required: true },
    rows: { type: Array, default: function () { return []; } },
    badgeClass: { type: Function, default: null },
    emptyText: { type: String, default: '暂无数据' },
  },
  methods: {
    cellBadge: function (row, col) {
      if (typeof this.badgeClass === 'function') return this.badgeClass(row, col.key);
      return 'badge-gray';
    },
  },
  template: `
<table class="ps-store-table">
  <thead><tr><th v-for="c in columns" :key="c.key">{{ c.label }}</th></tr></thead>
  <tbody v-if="rows.length">
    <tr v-for="(r, ri) in rows" :key="ri">
      <td v-for="c in columns" :key="c.key" :style="c.type === 'mono' ? 'font-family:monospace;font-size:12px' : ''">
        <strong v-if="c.type === 'strong'">{{ r[c.key] }}</strong>
        <span v-else-if="c.type === 'badge'" class="badge" :class="cellBadge(r, c)">{{ r[c.key] }}</span>
        <template v-else>{{ r[c.key] }}</template>
      </td>
    </tr>
  </tbody>
  <tbody v-else><tr><td :colspan="columns.length" style="text-align:center;padding:32px;color:#94a3b8">{{ emptyText }}</td></tr></tbody>
</table>
  `,
};
