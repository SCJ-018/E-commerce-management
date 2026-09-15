/**
 * 全站页面渲染回归检查（线上/本地通用）
 * ------------------------------------------------------------------
 * 用途：用真实浏览器逐个打开所有导航页，检查每页是否真的渲染出内容、
 *       并收集 JS 报错。用于排查「页面空白」类问题，或在改前端后做回归。
 *
 * 为什么需要它：Vue 3 的渲染函数一旦抛错，异常会中断整个组件的渲染，
 *       表现就是「整页空白」，而服务端日志完全正常（接口全是 200）。
 *       光看后端日志无法定位，必须在真实浏览器里跑。
 *
 * 用法：
 *   set NODE_PATH=C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules
 *   node .deploy/render_check.js [host] [账号] [密码]
 *   例：node .deploy/render_check.js https://julangkeji.site admin ******
 *
 * 说明：需要一个可登录的账号。若临时借用账号，测完请及时删除，
 *       不要长期把一个诊断账号留在生产库里。
 *       依赖 puppeteer-core + 系统已安装的 Chrome，无需下载 Chromium。
 * ------------------------------------------------------------------
 */
const puppeteer = require('puppeteer-core');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const HOST = process.argv[2] || 'https://julangkeji.site';
const USER = process.argv[3];
const PASS = process.argv[4];

if (!USER || !PASS) {
  console.log('用法: node .deploy/render_check.js <host> <账号> <密码>');
  process.exit(1);
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1000 });

  const errs = [];
  page.on('pageerror', (e) => errs.push('[pageerror] ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error' && !/favicon|404|net::ERR/.test(m.text())) errs.push('[console.error] ' + m.text());
  });

  await page.goto(HOST, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await new Promise((r) => setTimeout(r, 1500));

  const login = await page.evaluate(async (u, p) => {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, password: p }),
      credentials: 'include',
    });
    return await r.text();
  }, USER, PASS);
  const lj = JSON.parse(login);
  if (lj.code !== 0) { console.log('登录失败:', login); await browser.close(); return; }

  await page.evaluate((acct, name) => {
    sessionStorage.setItem('admin_logged_in', 'true');
    sessionStorage.setItem('admin_account', acct);
    sessionStorage.setItem('admin_name', name);
  }, lj.data.account, lj.data.name);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await new Promise((r) => setTimeout(r, 2500));

  const pages = await page.evaluate(() =>
    Array.from(document.querySelectorAll('.nav-item[data-page]')).map((a) => a.dataset.page));

  console.log('站点:', HOST, '| 账号:', USER, '| 角色:', lj.data.role);
  console.log('待测页面:', pages.length, '个\n');
  const results = [];

  for (const p of pages) {
    const before = errs.length;
    await page.evaluate((t) => {
      const a = document.querySelector('.nav-item[data-page="' + t + '"]');
      if (a) a.click();
    }, p);
    await new Promise((r) => setTimeout(r, 3500));

    const info = await page.evaluate((t) => {
      const vueEl = document.getElementById('page-' + t + '-vue');
      const secEl = document.getElementById('page-' + t);
      const pick = (el) => {
        if (!el || el.classList.contains('hidden')) return null;
        const r = el.getBoundingClientRect();
        return { id: el.id, h: Math.round(r.height), len: el.innerHTML.length };
      };
      return pick(vueEl) || pick(secEl) || { id: '(未渲染)', h: 0, len: 0 };
    }, p);

    results.push({ page: p, ...info, newErrs: errs.length - before });
  }

  console.log('页面'.padEnd(28) + '容器'.padEnd(36) + '高'.padEnd(8) + '内容长度'.padEnd(11) + '新增错误');
  console.log('-'.repeat(100));
  for (const r of results) {
    const bad = r.h === 0 || r.newErrs > 0;
    console.log(
      (bad ? 'X  ' : 'OK ') + r.page.padEnd(25) + r.id.padEnd(36) +
      String(r.h).padEnd(8) + String(r.len).padEnd(11) + r.newErrs
    );
  }

  console.log('\n=== 全部 JS 错误 ===');
  console.log(errs.length ? [...new Set(errs)].join('\n') : '(无)');

  const bad = results.filter((r) => r.h === 0 || r.newErrs > 0);
  console.log('\n结论:', bad.length === 0
    ? '全部 ' + results.length + ' 个页面渲染正常'
    : bad.length + ' 个页面异常：' + bad.map((b) => b.page).join(', '));

  await browser.close();
})().catch((e) => { console.error('SCRIPT FAIL:', e.message); process.exit(1); });
