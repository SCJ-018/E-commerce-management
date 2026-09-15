/**
 * 人事数据中心 — 5 张表的页面配置（列定义 / KPI / 图表）
 * 每个 key 对应数据库中的一张实表，配置成 HR.makePage 可用的独立页面。
 * 图表指标全部由表内现有字段派生（数据库暂无出勤字段，故不虚构出勤率）。
 */
(function () {
  'use strict';
  var u = HR.util;

  // ==================== 通用图表构造 ====================
  function axisStyle() {
    return {
      axisLine: { lineStyle: { color: '#e2e8f0' } },
      axisLabel: { color: '#64748b', fontSize: 11 },
      splitLine: { lineStyle: { color: '#f1f5f9' } },
    };
  }

  function pieOption(data, colors) {
    return {
      tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
      legend: { bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 11, color: '#64748b' } },
      color: colors,
      series: [{
        type: 'pie', radius: ['46%', '72%'], center: ['50%', '46%'],
        data: data, avoidLabelOverlap: true,
        label: { formatter: '{b}\n{c}人', fontSize: 11, color: '#475569' },
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
      }],
    };
  }

  function barOption(categories, values, opt) {
    opt = opt || {};
    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: function (v) { return opt.unit ? v + opt.unit : v; } },
      grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
      xAxis: Object.assign({ type: 'category', data: categories, axisTick: { show: false } }, axisStyle()),
      yAxis: Object.assign({ type: 'value', name: opt.yName || '' }, axisStyle()),
      color: [opt.color || '#4f46e5'],
      series: [{
        type: 'bar', data: values, barMaxWidth: 34,
        itemStyle: { borderRadius: [6, 6, 0, 0] },
        label: { show: !!opt.showLabel, position: 'top', fontSize: 11, color: '#64748b', formatter: opt.labelFormatter || '{c}' },
      }],
    };
  }

  /** 取数据中出现的月份（升序）最后 n 个月 + 计数 */
  function monthCounts(rows, field, n) {
    var map = {};
    (rows || []).forEach(function (r) {
      var m = u.month(r[field]);
      if (m) map[m] = (map[m] || 0) + 1;
    });
    var months = Object.keys(map).sort();
    if (months.length > n) months = months.slice(months.length - n);
    return { months: months, data: months.map(function (m) { return map[m]; }) };
  }

  function lineOption(mc, opt) {
    opt = opt || {};
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
      xAxis: Object.assign({ type: 'category', data: mc.months, boundaryGap: false, axisTick: { show: false } }, axisStyle()),
      yAxis: Object.assign({ type: 'value', minInterval: 1 }, axisStyle()),
      color: [opt.color || '#4f46e5'],
      series: [{
        type: opt.type || 'line', data: mc.data, smooth: true, symbolSize: 7,
        lineStyle: { width: 2.5 }, areaStyle: { opacity: 0.10 },
        label: { show: true, position: 'top', fontSize: 11, color: '#64748b' },
      }],
    };
  }

  function groupBarOption(items, opt) {
    return barOption(items.map(function (x) { return x.name; }), items.map(function (x) { return x.value; }), opt);
  }

  // ==================== 1. 员工花名册 ====================
  var roster = {
    key: 'roster', label: '员工花名册', perm: 'hr-roster',
    icon: 'fa-solid fa-address-book', color: '#4f46e5', desc: 'Employee Roster',
    pk: ['工号', '姓名', '手机号'],
    fields: [
      { name: '工号', type: 'text', required: true },
      { name: '姓名', type: 'text', required: true },
      { name: '入职时间', type: 'date' },
      { name: '一级部门', type: 'text' },
      { name: '二级部门', type: 'text' },
      { name: '直带人', type: 'text' },
      { name: '岗级', type: 'text' },
      { name: '手机号', type: 'text', required: true },
      { name: '身份证号', type: 'text' },
      { name: '紧急联系人电话', type: 'text' },
      { name: '状态', type: 'text', required: false, options: ['在职', '试用期', '离职', '待入职'] },
      { name: '薪资待遇', type: 'text' },
      { name: '转正日期', type: 'date' },
      { name: '银行卡号', type: 'text' },
      { name: '开户行', type: 'text' },
      { name: '主人事', type: 'text' },
    ],
    badgeClass: function (row, key) {
      if (key !== '状态') return '';
      if (row['状态'] === '在职') return 'badge-success';
      if (row['状态'] === '试用期') return 'badge-warning';
      if (row['状态'] === '离职') return 'badge-danger';
      if (row['状态'] === '待入职') return 'badge-info';
      return 'badge-gray';
    },
    chips: function (rows) {
      var active = u.countIf(rows, '状态', ['在职']);
      return [
        { label: '总人数', value: rows.length },
        { label: '在职率', value: u.pct(active, rows.length), color: '#16a34a' },
        { label: '试用期', value: u.countIf(rows, '状态', ['试用期']), color: '#f59e0b' },
        { label: '本月入职', value: monthNew(rows), color: '#6366f1' },
      ];
    },
    kpis: function (rows) {
      var active = u.countIf(rows, '状态', ['在职']);
      var probation = u.countIf(rows, '状态', ['试用期']);
      var avgMonths = 0, cnt = 0, now = new Date();
      rows.forEach(function (r) {
        var m = u.month(r['入职时间']);
        if (!m) return;
        var d = new Date(m + '-01');
        avgMonths += (now.getFullYear() - d.getFullYear()) * 12 + (now.getMonth() - d.getMonth());
        cnt++;
      });
      return [
        { label: '在职人数', value: active, sub: '占比 ' + u.pct(active, rows.length), icon: 'fa-solid fa-user-check', tone: 'emerald' },
        { label: '试用期人数', value: probation, sub: '占比 ' + u.pct(probation, rows.length), icon: 'fa-solid fa-hourglass-half', tone: 'amber' },
        { label: '平均司龄', value: cnt ? (avgMonths / cnt).toFixed(1) + ' 月' : '—', sub: '按入职时间测算', icon: 'fa-solid fa-calendar-days', tone: 'cyan' },
        { label: '部门数', value: u.group(rows, '一级部门').length, sub: '一级部门统计', icon: 'fa-solid fa-sitemap', tone: 'violet' },
      ];
    },
    charts: [
      {
        title: '部门人员分布', subtitle: '按一级部门统计在职人数', span: 1,
        option: function (rows, palette) { return pieOption(u.group(rows, '一级部门'), palette); },
      },
      {
        title: '在职状态分布', subtitle: '在职 / 试用期 / 离职占比', span: 1,
        option: function (rows, palette) { return pieOption(u.group(rows, '状态'), palette); },
      },
      {
        title: '月度入职趋势', subtitle: '按入职时间统计每月入职人数', span: 2,
        option: function (rows) { return lineOption(monthCounts(rows, '入职时间', 12), { color: '#4f46e5' }); },
      },
    ],
  };

  function monthNew(rows) {
    var cm = u.curMonth();
    return rows.filter(function (r) { return u.month(r['入职时间']) === cm; }).length;
  }

  // ==================== 2. 面试信息登记表 ====================
  var interview = {
    key: 'interview', label: '面试信息登记表', perm: 'hr-interview',
    icon: 'fa-solid fa-comments', color: '#0891b2', desc: 'Interview Record',
    pk: ['姓名', '年龄', '日期'],
    fields: [
      { name: '姓名', type: 'text', required: true },
      { name: '性别', type: 'text', options: ['男', '女'] },
      { name: '年龄', type: 'text', required: true },
      { name: '岗位', type: 'text' },
      { name: '日期', type: 'date', required: true },
      { name: '时间', type: 'time', required: true },
      { name: '邀约人', type: 'text' },
      { name: '结果', type: 'text', options: ['通过', '未通过', '待定', '已入职', '放弃'] },
      { name: '备注', type: 'textarea' },
    ],
    badgeClass: function (row, key) {
      if (key !== '结果') return '';
      var v = row['结果'];
      if (v === '通过' || v === '已入职') return 'badge-success';
      if (v === '未通过' || v === '放弃') return 'badge-danger';
      if (v === '待定') return 'badge-warning';
      return 'badge-gray';
    },
    chips: function (rows) {
      var passed = u.countIf(rows, '结果', ['通过', '已入职']);
      return [
        { label: '面试总数', value: rows.length },
        { label: '通过率', value: u.pct(passed, rows.length), color: '#16a34a' },
        { label: '待定', value: u.countIf(rows, '结果', ['待定']), color: '#f59e0b' },
        { label: '本月面试', value: rows.filter(function (r) { return u.month(r['日期']) === u.curMonth(); }).length, color: '#0891b2' },
      ];
    },
    kpis: function (rows) {
      var passed = u.countIf(rows, '结果', ['通过', '已入职']);
      var failed = u.countIf(rows, '结果', ['未通过']);
      var ages = rows.map(function (r) { return u.num(r['年龄']); }).filter(function (n) { return n > 0; });
      var avgAge = ages.length ? (ages.reduce(function (a, b) { return a + b; }, 0) / ages.length).toFixed(1) : '—';
      return [
        { label: '通过人数', value: passed, sub: '通过率 ' + u.pct(passed, rows.length), icon: 'fa-solid fa-circle-check', tone: 'emerald' },
        { label: '未通过', value: failed, sub: '占比 ' + u.pct(failed, rows.length), icon: 'fa-solid fa-circle-xmark', tone: 'rose' },
        { label: '平均年龄', value: avgAge, sub: '面试候选人', icon: 'fa-solid fa-user-group', tone: 'cyan' },
        { label: '覆盖岗位', value: u.group(rows, '岗位').length, sub: '岗位种类数', icon: 'fa-solid fa-briefcase', tone: 'indigo' },
      ];
    },
    charts: [
      {
        title: '面试结果分布', subtitle: '通过 / 未通过 / 待定占比', span: 1,
        option: function (rows, palette) { return pieOption(u.group(rows, '结果'), palette); },
      },
      {
        title: '岗位面试量 TOP8', subtitle: '各岗位面试人次', span: 1,
        option: function (rows) { return groupBarOption(u.group(rows, '岗位', 8), { color: '#0891b2', showLabel: true }); },
      },
      {
        title: '月度面试量趋势', subtitle: '按面试日期统计每月面试人次', span: 2,
        option: function (rows) { return lineOption(monthCounts(rows, '日期', 12), { color: '#0891b2' }); },
      },
    ],
  };

  // ==================== 3. 入职人员信息统计表 ====================
  var onboarding = {
    key: 'onboarding', label: '入职人员信息统计表', perm: 'hr-onboarding',
    icon: 'fa-solid fa-user-plus', color: '#059669', desc: 'Onboarding Statistics',
    pk: ['姓名', '年龄', '面试日期', '入职日期'],
    fields: [
      { name: '姓名', type: 'text', required: true },
      { name: '性别', type: 'text', options: ['男', '女'] },
      { name: '年龄', type: 'text', required: true },
      { name: '岗位', type: 'text' },
      { name: '面试日期', type: 'date', required: true },
      { name: '入职日期', type: 'date', required: true },
      { name: '入职情况', type: 'text', options: ['已入职', '未入职', '放弃', '待定'] },
      { name: '定岗', type: 'text' },
      { name: '第一天', type: 'text', options: ['正常', '异常', '离职'] },
      { name: '第三天', type: 'text', options: ['正常', '异常', '离职'] },
      { name: '第七天', type: 'text', options: ['正常', '异常', '离职'] },
      { name: '邀约人', type: 'text' },
      { name: '一级部门', type: 'text' },
      { name: '二级部门', type: 'text' },
      { name: '直带人', type: 'text' },
      { name: '备注', type: 'textarea' },
    ],
    badgeClass: function (row, key) {
      var v = row[key];
      if (key === '入职情况') {
        if (v === '已入职') return 'badge-success';
        if (v === '放弃') return 'badge-danger';
        if (v === '未入职' || v === '待定') return 'badge-warning';
      }
      if (key === '第一天' || key === '第三天' || key === '第七天') {
        if (v === '正常') return 'badge-success';
        if (v === '异常') return 'badge-warning';
        if (v === '离职') return 'badge-danger';
      }
      return '';
    },
    chips: function (rows) {
      var joined = u.countIf(rows, '入职情况', ['已入职']);
      return [
        { label: '登记人数', value: rows.length },
        { label: '入职完成率', value: u.pct(joined, rows.length), color: '#16a34a' },
        { label: '7天留存率', value: u.pct(u.countIf(rows, '第七天', ['正常']), rows.length), color: '#059669' },
        { label: '本月入职', value: rows.filter(function (r) { return u.month(r['入职日期']) === u.curMonth(); }).length, color: '#6366f1' },
      ];
    },
    kpis: function (rows) {
      var joined = u.countIf(rows, '入职情况', ['已入职']);
      var days = [];
      rows.forEach(function (r) {
        var a = String(r['面试日期'] || '').trim(), b = String(r['入职日期'] || '').trim();
        if (a && b) {
          var d = (new Date(b) - new Date(a)) / 86400000;
          if (!isNaN(d) && d >= 0) days.push(d);
        }
      });
      var avgDays = days.length ? (days.reduce(function (x, y) { return x + y; }, 0) / days.length).toFixed(1) : '—';
      return [
        { label: '已入职', value: joined, sub: '入职完成率 ' + u.pct(joined, rows.length), icon: 'fa-solid fa-user-check', tone: 'emerald' },
        { label: '第七天在岗', value: u.countIf(rows, '第七天', ['正常']), sub: '7 天留存 ' + u.pct(u.countIf(rows, '第七天', ['正常']), rows.length), icon: 'fa-solid fa-calendar-check', tone: 'cyan' },
        { label: '面试到入职', value: avgDays === '—' ? '—' : avgDays + ' 天', sub: '平均周期', icon: 'fa-solid fa-clock', tone: 'amber' },
        { label: '涉及部门', value: u.group(rows, '一级部门').length, sub: '一级部门数', icon: 'fa-solid fa-sitemap', tone: 'violet' },
      ];
    },
    charts: [
      {
        title: '入职情况分布', subtitle: '已入职 / 未入职 / 放弃占比', span: 1,
        option: function (rows, palette) { return pieOption(u.group(rows, '入职情况'), palette); },
      },
      {
        title: '新人 1 / 3 / 7 天留存', subtitle: '各阶段状态为「正常」的人数', span: 1,
        option: function (rows) {
          return groupBarOption([
            { name: '第一天', value: u.countIf(rows, '第一天', ['正常']) },
            { name: '第三天', value: u.countIf(rows, '第三天', ['正常']) },
            { name: '第七天', value: u.countIf(rows, '第七天', ['正常']) },
          ], { color: '#059669', showLabel: true });
        },
      },
      {
        title: '月度入职趋势', subtitle: '按入职日期统计每月入职人数', span: 2,
        option: function (rows) { return lineOption(monthCounts(rows, '入职日期', 12), { color: '#059669' }); },
      },
    ],
  };

  // ==================== 4. 人员薪资标准（表 a） ====================
  var salaryA = {
    key: 'salary-a', label: '人员薪资标准（表 a）', perm: 'hr-salary-a',
    icon: 'fa-solid fa-money-bill-wave', color: '#d97706', desc: 'Salary Standard A',
    pk: ['部门', '姓名', '身份证号'],
    fields: [
      { name: '部门', type: 'text', required: true },
      { name: '直带人', type: 'text' },
      { name: '姓名', type: 'text', required: true },
      { name: '性别', type: 'text', options: ['男', '女'] },
      { name: '驾驶证', type: 'text', options: ['有', '无'] },
      { name: '门禁', type: 'text', options: ['已开通', '未开通'] },
      { name: '联系方式', type: 'text' },
      { name: '身份证号', type: 'text', required: true },
      { name: '年龄', type: 'text' },
      { name: '入职时间', type: 'date' },
      { name: '人员类型', type: 'text', options: ['正式', '试用', '实习', '兼职', '外包'] },
      { name: '转正月份实际', type: 'date' },
      { name: '开户行', type: 'text' },
      { name: '银行卡号', type: 'text' },
      { name: '主人事', type: 'text' },
      { name: '备注', type: 'textarea' },
    ],
    badgeClass: function (row, key) {
      var v = row[key];
      if (key === '人员类型') {
        if (v === '正式') return 'badge-success';
        if (v === '试用') return 'badge-warning';
        if (v) return 'badge-info';
      }
      if (key === '门禁') {
        if (v === '已开通') return 'badge-success';
        if (v === '未开通') return 'badge-gray';
      }
      return '';
    },
    chips: function (rows) {
      var formal = u.countIf(rows, '人员类型', ['正式']);
      return [
        { label: '登记人数', value: rows.length },
        { label: '正式占比', value: u.pct(formal, rows.length), color: '#16a34a' },
        { label: '部门数', value: u.group(rows, '部门').length, color: '#d97706' },
        { label: '本月转正', value: rows.filter(function (r) { return u.month(r['转正月份实际']) === u.curMonth(); }).length, color: '#6366f1' },
      ];
    },
    kpis: function (rows) {
      var formal = u.countIf(rows, '人员类型', ['正式']);
      var probation = u.countIf(rows, '人员类型', ['试用']);
      var ages = rows.map(function (r) { return u.num(r['年龄']); }).filter(function (n) { return n > 0; });
      return [
        { label: '登记人数', value: rows.length, sub: '档案总数', icon: 'fa-solid fa-users', tone: 'amber' },
        { label: '正式员工', value: formal, sub: '占比 ' + u.pct(formal, rows.length), icon: 'fa-solid fa-user-check', tone: 'emerald' },
        { label: '试用期', value: probation, sub: '占比 ' + u.pct(probation, rows.length), icon: 'fa-solid fa-hourglass-half', tone: 'indigo' },
        { label: '平均年龄', value: ages.length ? (ages.reduce(function (a, b) { return a + b; }, 0) / ages.length).toFixed(1) : '—', sub: '按登记年龄', icon: 'fa-solid fa-cake-candles', tone: 'cyan' },
      ];
    },
    charts: [
      {
        title: '部门人数分布', subtitle: '各部门登记人数占比', span: 1,
        option: function (rows, palette) { return pieOption(u.group(rows, '部门'), palette); },
      },
      {
        title: '人员类型分布', subtitle: '正式 / 试用 / 实习 / 兼职', span: 1,
        option: function (rows, palette) { return pieOption(u.group(rows, '人员类型'), palette); },
      },
      {
        title: '转正月份趋势', subtitle: '按「转正月份实际」统计人数', span: 2,
        option: function (rows) { return lineOption(monthCounts(rows, '转正月份实际', 12), { color: '#d97706' }); },
      },
    ],
  };

  // ==================== 5. 人员薪资标准（表 b） ====================
  var salaryB = {
    key: 'salary-b', label: '人员薪资标准（表 b）', perm: 'hr-salary-b',
    icon: 'fa-solid fa-sack-dollar', color: '#e11d48', desc: 'Salary Standard B',
    pk: ['部门', '姓名', '身份证号'],
    fields: [
      { name: '部门', type: 'text', required: true },
      { name: '姓名', type: 'text', required: true },
      { name: '性别', type: 'text', options: ['男', '女'] },
      { name: '驾驶证', type: 'text', options: ['有', '无'] },
      { name: '门禁', type: 'text', options: ['已开通', '未开通'] },
      { name: '联系方式', type: 'text' },
      { name: '身份证号', type: 'text', required: true },
      { name: '年龄', type: 'text' },
      { name: '职级', type: 'text' },
      { name: '入职时间', type: 'text' },
      { name: '基本薪资', type: 'number' },
      { name: '绩效', type: 'number' },
      { name: '全勤', type: 'number' },
      { name: '综合薪资', type: 'number' },
      { name: '试用期', type: 'text', options: ['是', '否'] },
      { name: '试用薪资', type: 'number' },
      { name: '人员类型', type: 'text', options: ['正式', '试用', '实习', '兼职', '外包'] },
      { name: '转正月份实际', type: 'text' },
      { name: '开户行', type: 'text' },
      { name: '银行卡号', type: 'text' },
      { name: '主人事', type: 'text' },
      { name: '备注', type: 'textarea' },
    ],
    badgeClass: function (row, key) {
      var v = row[key];
      if (key === '试用期') return v === '是' ? 'badge-warning' : (v === '否' ? 'badge-success' : '');
      if (key === '人员类型') {
        if (v === '正式') return 'badge-success';
        if (v === '试用') return 'badge-warning';
        if (v) return 'badge-info';
      }
      return '';
    },
    chips: function (rows) {
      return [
        { label: '登记人数', value: rows.length },
        { label: '平均综合薪资', value: '¥' + u.fmt(u.avg(rows, '综合薪资')), color: '#e11d48' },
        { label: '平均基本薪资', value: '¥' + u.fmt(u.avg(rows, '基本薪资')), color: '#d97706' },
        { label: '试用期人数', value: u.countIf(rows, '试用期', ['是']), color: '#f59e0b' },
      ];
    },
    kpis: function (rows) {
      var probation = u.countIf(rows, '试用期', ['是']);
      return [
        { label: '平均综合薪资', value: '¥' + u.fmt(u.avg(rows, '综合薪资')), sub: '含绩效与全勤', icon: 'fa-solid fa-sack-dollar', tone: 'rose' },
        { label: '平均基本薪资', value: '¥' + u.fmt(u.avg(rows, '基本薪资')), sub: '岗位基本工资', icon: 'fa-solid fa-money-bill', tone: 'amber' },
        { label: '平均绩效', value: '¥' + u.fmt(u.avg(rows, '绩效')), sub: '绩效考核部分', icon: 'fa-solid fa-chart-line', tone: 'cyan' },
        { label: '试用期人数', value: probation, sub: '占比 ' + u.pct(probation, rows.length), icon: 'fa-solid fa-hourglass-half', tone: 'violet' },
      ];
    },
    charts: [
      {
        title: '薪资构成对比', subtitle: '基本 / 绩效 / 全勤 / 综合 人均金额（元）', span: 1,
        option: function (rows) {
          return groupBarOption([
            { name: '基本薪资', value: Math.round(u.avg(rows, '基本薪资')) },
            { name: '绩效', value: Math.round(u.avg(rows, '绩效')) },
            { name: '全勤', value: Math.round(u.avg(rows, '全勤')) },
            { name: '综合薪资', value: Math.round(u.avg(rows, '综合薪资')) },
            { name: '试用薪资', value: Math.round(u.avg(rows, '试用薪资')) },
          ], { color: '#e11d48', showLabel: true, unit: ' 元' });
        },
      },
      {
        title: '部门平均综合薪资', subtitle: '各部门综合薪资人均（元）', span: 1,
        option: function (rows) {
          var groups = u.group(rows, '部门');
          var items = groups.map(function (g) {
            var sub = rows.filter(function (r) { return (r['部门'] || '').trim() === g.name; });
            return { name: g.name, value: Math.round(u.avg(sub, '综合薪资')) };
          });
          return groupBarOption(items, { color: '#d97706', showLabel: true, unit: ' 元' });
        },
      },
      {
        title: '职级分布', subtitle: '各职级人数占比', span: 2,
        option: function (rows, palette) { return pieOption(u.group(rows, '职级'), palette); },
      },
    ],
  };

  window.HR_TABLES = [roster, interview, onboarding, salaryA, salaryB];
})();
