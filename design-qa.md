# 种草监测中台设计 QA

- reference: `C:\Users\Administrator\.codex\generated_images\01a0c707-f128-78b3-bb98-209ddea9f752\exec-aeaa7059-b1f5-4678-895b-d6cd6a9d1d64.png`
- prototype: `http://127.0.0.1:8766/index.html?design=20260922a#seeding-monitor`
- viewport: `1440 x 1024`
- state: 全部部门、2026-09 月份、演示数据

## Visual checks

- [x] 页面背景、左侧系统导航和主内容区层级与参考图一致
- [x] 标题、月份范围、同步状态和部门页签位于首屏上方
- [x] 四张指标卡显示本月值、上周期值和环比方向；下降使用红色
- [x] 工具条包含月份范围、人员选择、品类绑定、奖金标准、导出和新增记录
- [x] 记录表使用两层分组表头，互动数据和流量分析字段完整
- [x] 记录标题可打开作品链接，分析图按钮、编辑和删除按钮保持可见
- [x] 表格在窄视口保持横向滚动，卡片和表单在移动尺寸下折叠

## Interaction checks

- [x] 部门页签切换后指标、记录数量和表格内容同步变化
- [x] 进入一部后可以打开品类绑定浮窗，并看到默认绑定品类
- [x] 人员选择器和奖金标准下拉入口可用
- [x] 新增记录打开完整表单，包含链接、笔记类型、作品 ID、互动数据和流量分析图上传
- [x] 超级管理员可以编辑和删除记录；普通账号继续按本人维护名单限制

## Verification notes

- 语法检查：`node --check js/vue/page-seeding.js`、`node --check js/app.js`
- 差异检查：`git diff --check`
- 浏览器实测：本地桌面浏览器打开并检查了首屏、部门切换、品类绑定和新增记录弹窗
- 静态预览期间 `/api/*` 会返回 404，页面按本地演示数据回退；这不影响本次前端布局和交互验证

final result: passed
