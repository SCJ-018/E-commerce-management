/*!
 * no-autofill.js —— 全局关闭「浏览器自动填充 / 历史输入记忆 / 密码保存弹窗」
 *
 * 覆盖范围：全站所有 input、textarea
 *   - index.html 里的静态输入框
 *   - Vue 动态渲染出来的输入框（v-if / 切页面 / 弹窗里新建的）
 *   - 以后新增的页面和输入框（无需再逐个补属性）
 *
 * 加载位置：index.html 的 <head> 里，必须早于任何输入框被浏览器解析，
 *          这样浏览器还没机会去填，readonly 兜底就已经就位。
 *
 * 为什么不能只写 autocomplete="off"：
 *   Chrome / Edge 对 type="password" 的输入框**会无视** autocomplete="off"，
 *   只要用户本地存过该站点的账号密码就会自动填。业界通用解法是
 *   「先 readonly，用户一交互就解锁」——浏览器在只读期间不会写入任何值。
 */
(function () {
  'use strict';

  if (window.__noAutofillInstalled) return;
  window.__noAutofillInstalled = true;

  /* 这些类型不是「可填值」的输入，浏览器也不会记历史，直接跳过 */
  var SKIP_TYPES = {
    checkbox: 1, radio: 1, file: 1, hidden: 1, submit: 1,
    button: 1, reset: 1, image: 1, range: 1, color: 1
  };

  /* 各浏览器 / 密码管理器认的「别填我」标记 */
  var FLAGS = {
    autocomplete: 'off',
    autocorrect: 'off',
    autocapitalize: 'off',
    'data-lpignore': 'true',          /* LastPass */
    'data-1p-ignore': 'true',         /* 1Password */
    'data-bwignore': 'true',          /* Bitwarden */
    'data-protonpass-ignore': 'true', /* Proton Pass */
    'data-form-type': 'other'         /* Dashlane / 通用反识别 */
  };
  var FLAG_KEYS = Object.keys(FLAGS);

  /* 触发解锁的事件：鼠标、触摸、指针、键盘聚焦 */
  var UNLOCK_EVENTS = ['pointerdown', 'mousedown', 'touchstart', 'focus', 'keydown'];

  function isField(el) {
    if (!el || el.nodeType !== 1) return false;
    var t = el.tagName;
    return t === 'INPUT' || t === 'TEXTAREA';
  }

  /* 解锁：去掉 readonly 并保持焦点，让用户接着就能打字 */
  function unlock(el) {
    if (el.__nafEvents) {
      for (var i = 0; i < el.__nafEvents.length; i++) {
        el.removeEventListener(el.__nafEvents[i], el.__nafUnlock, true);
      }
      el.__nafEvents = null;
      el.__nafUnlock = null;
    }
    if (!el.hasAttribute('readonly')) return;
    el.__nafUnlocked = true;   /* 记住"已解锁"，防止 focusin 兜底又把它锁回去 */
    el.removeAttribute('readonly');
    try {
      el.focus({ preventScroll: true });
    } catch (e) {
      try { el.focus(); } catch (e2) { /* 忽略 */ }
    }
  }

  function harden(el) {
    if (!isField(el)) return;

    var type = (el.getAttribute('type') || '').toLowerCase();

    if (SKIP_TYPES[type]) {
      if (type === 'checkbox' || type === 'radio' || type === 'file') {
        el.setAttribute('autocomplete', 'off');
      }
      return;
    }

    for (var i = 0; i < FLAG_KEYS.length; i++) {
      el.setAttribute(FLAG_KEYS[i], FLAGS[FLAG_KEYS[i]]);
    }

    if (type === 'password') {
      /*
       * 密码框专项：Chrome / Edge 会无视 autocomplete=off，
       * 用 readonly 兜底。只在「字段还是空的、且还没解锁过、且当前没被聚焦」
       * 的时候上锁 —— 否则会打断用户已经输入的内容，
       * 或者在 unlock() 触发 focus 时被 focusin 兜底重新锁上，导致打不了字。
       */
      if (!el.hasAttribute('readonly') && !el.value &&
          !el.__nafUnlocked && el !== document.activeElement) {
        el.setAttribute('readonly', 'readonly');
        el.__nafUnlock = function () { unlock(el); };
        el.__nafEvents = UNLOCK_EVENTS.slice();
        for (var j = 0; j < el.__nafEvents.length; j++) {
          el.addEventListener(el.__nafEvents[j], el.__nafUnlock, true);
        }
      }
      return;
    }

    el.__nafDone = true;
  }

  function sweep(root) {
    if (!root || root.nodeType !== 1) return;
    if (isField(root)) harden(root);
    if (!root.querySelectorAll) return;
    var list = root.querySelectorAll('input, textarea');
    for (var i = 0; i < list.length; i++) harden(list[i]);
  }

  /* 兜底 1：输入框一获得焦点就再补一次（覆盖观察器漏掉的极端情况） */
  document.addEventListener('focusin', function (e) {
    if (isField(e.target)) harden(e.target);
  }, true);

  /* 兜底 2：观察整个文档，Vue 渲染 / 切页面 / 弹窗新建的输入框都逃不掉 */
  function startObserver() {
    try {
      var mo = new MutationObserver(function (records) {
        for (var i = 0; i < records.length; i++) {
          var added = records[i].addedNodes;
          for (var j = 0; j < added.length; j++) {
            if (added[j].nodeType === 1) sweep(added[j]);
          }
        }
      });
      mo.observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) { /* 忽略 */ }
  }

  if (document.documentElement) {
    startObserver();          /* head 里就能装上，直接盯住 body 的解析过程 */
  } else {
    document.addEventListener('DOMContentLoaded', startObserver);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      sweep(document.documentElement);
    });
  } else {
    sweep(document.documentElement);
  }

  /* 暴露出来，方便排查 */
  window.NoAutofill = {
    sweep: sweep,
    harden: harden,
    /* 手动全站重扫一遍（极少用得上） */
    rescan: function () { sweep(document.documentElement); }
  };
})();
