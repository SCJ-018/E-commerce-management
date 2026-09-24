// ==================== 内容创作中心 - 违规词检测 Vue 版 ====================
// 同时支持图片 OCR 检测与直接文本检测，并可嵌入内容创作中心。
// 状态保留在模块级（切走页面不丢），挂载/卸载只控制 DOM。
// 违规词高亮用 Vue 文本插值拆分渲染，XSS 只强不弱（沿用 CSS 类 vd-conf-mark/vd-susp-mark）。
(function () {
  if (typeof Vue === 'undefined' || typeof ApiService === 'undefined' || typeof App === 'undefined') return;

  // ---- 常量（与旧版一致） ----
  var _VD_OCR_MAX_SIDE = 2400;
  var _VD_OCR_JPEG_QUALITY = 0.95;
  var _VD_BATCH_SIZE = 5;

  // ---- 模块级状态（跨挂载/卸载保留，切走页面检测结果不丢） ----
  var _nextImgId = 1;

  var _state = Vue.reactive({
    images: [],
    running: false,
    scanning: false,
    pass: false,
    progressShow: false,
    progressPct: 0,
    progressText: '',
    processingLeft: 0,
    notifications: [],
  });

  // ---- 仅对超大图片缩放；PNG 保持无损，避免小字在 JPEG 转码后丢笔画 ----
  function compressForOcr(dataUrl) {
    return new Promise(function (resolve) {
      var img = new Image();
      img.onload = function () {
        var w = img.width, h = img.height;
        var maxSide = Math.max(w, h);
        var changed = false;
        if (maxSide > _VD_OCR_MAX_SIDE) {
          var scale = _VD_OCR_MAX_SIDE / maxSide;
          w = Math.max(1, Math.round(w * scale));
          h = Math.max(1, Math.round(h * scale));
          changed = true;
        }
        if (!changed) {
          resolve(dataUrl);
          return;
        }
        var canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        var ctx = canvas.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(img, 0, 0, w, h);
        var isPng = dataUrl.indexOf('data:image/png') === 0;
        resolve(canvas.toDataURL(isPng ? 'image/png' : 'image/jpeg', _VD_OCR_JPEG_QUALITY));
      };
      img.onerror = function () { resolve(dataUrl); };
      img.src = dataUrl;
    });
  }

  // ---- 添加图片（文件读取 + 去重 + 异步压缩） ----
  function addFiles(files) {
    if (!files || files.length === 0) return;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      if (!f.type || !f.type.match(/image\/(jpeg|png|webp)/)) continue;
      var dup = _state.images.some(function (img) {
        return img.name === f.name && img.file && img.file.size === f.size;
      });
      if (dup) continue;
      var reader = new FileReader();
      reader.onload = (function (file, name) {
        return function (e) {
          var originalDataUrl = e.target.result;
          var imgObj = Vue.reactive({
            id: _nextImgId++,
            file: Vue.markRaw(file),
            name: name,
            dataUrl: originalDataUrl,
            ocrDataUrl: originalDataUrl,
            ocrText: null,
            ocrLines: 0,
            ocrConfidence: 0,
            ocrFallback: false,
            ocrError: null,
            violations: [],
            suspectedWords: [],
          });
          _state.images.push(imgObj);
          compressForOcr(originalDataUrl).then(function (compressed) {
            if (compressed) imgObj.ocrDataUrl = compressed;
          });
        };
      })(f, f.name);
      reader.readAsDataURL(f);
    }
  }

  // ---- 文件夹递归扫描（拖拽文件夹） ----
  function scanEntries(entries, cb) {
    var pending = entries.length;
    if (pending === 0) { cb([]); return; }
    var results = [];
    for (var i = 0; i < entries.length; i++) {
      (function (entry) {
        if (entry.isFile) {
          entry.file(function (f) { results.push(f); pending--; if (pending === 0) cb(results); });
        } else if (entry.isDirectory) {
          var reader = entry.createReader();
          reader.readEntries(function (subEntries) {
            scanEntries(subEntries, function (sub) {
              results = results.concat(sub);
              pending--;
              if (pending === 0) cb(results);
            });
          });
        } else {
          pending--;
          if (pending === 0) cb(results);
        }
      })(entries[i]);
    }
  }

  function handleDrop(e) {
    if (e && e.preventDefault) e.preventDefault();
    var items = e && e.dataTransfer ? e.dataTransfer.items : null;
    if (items && items.length > 0 && items[0].webkitGetAsEntry) {
      var entries = [];
      for (var i = 0; i < items.length; i++) {
        var entry = items[i].webkitGetAsEntry ? items[i].webkitGetAsEntry() : null;
        if (entry) entries.push(entry);
      }
      if (entries.length > 0) {
        scanEntries(entries, function (fs) { addFiles(fs); });
        return;
      }
    }
    addFiles(e && e.dataTransfer ? e.dataTransfer.files : []);
  }

  // ==================== 组件 ====================
  var ViolationPage = {
    setup: function () {
      var activeMode = Vue.ref('image');
      var dragOver = Vue.ref(false);
      var notifyOpen = Vue.ref(false);
      var textInput = Vue.ref('');
      var textRunning = Vue.ref(false);
      var textResult = Vue.ref(null);
      var textError = Vue.ref('');

      function hasIssue(img) {
        return (img.violations && img.violations.length > 0) || (img.suspectedWords && img.suspectedWords.length > 0);
      }

      function imgState(img) {
        if (img.violations && img.violations.length > 0) return 'hit';
        if (img.ocrText !== null && img.ocrText !== undefined) return 'done';
        return 'pending';
      }

      function thumbBorder(img) {
        var s = imgState(img);
        if (s === 'hit') return '#ef4444';
        if (s === 'done') return '#16a34a';
        return '#e2e8f0';
      }

      function headerInfo(img) {
        var hasError = !!(img.ocrError && !img.ocrText);
        var bad = hasIssue(img) || hasError;
        return {
          color: bad ? '#ef4444' : '#16a34a',
          icon: bad ? 'fa-circle-exclamation' : 'fa-circle-check',
        };
      }

      function resultStatusText(img) {
        var hasV = img.violations && img.violations.length > 0;
        var hasS = img.suspectedWords && img.suspectedWords.length > 0;
        if (hasV || hasS) {
          var parts = [];
          if (hasV) parts.push(img.violations.length + ' 个精确违规');
          if (hasS) parts.push(img.suspectedWords.length + ' 个疑似');
          return '发现 ' + parts.join('，');
        }
        if (img.ocrError && !img.ocrText) return '识别失败';
        return '未发现违规词';
      }

      // 把文本拆成「普通片段 / 命中片段」，命中片段用 mark 高亮，文本走插值自动转义
      function segmentText(text, violations, suspectedWords) {
        var marks = [];
        (violations || []).forEach(function (v) {
          if (v.position !== undefined && v.length) {
            marks.push({ type: 'confirmed', start: v.position, end: v.position + v.length, word: v.word });
          }
        });
        (suspectedWords || []).forEach(function (v) {
          if (v.position !== undefined && v.length) {
            marks.push({ type: 'suspected', start: v.position, end: v.position + v.length, word: v.word });
          }
        });
        marks.sort(function (a, b) { return a.start - b.start; });
        var segs = [];
        var pos = 0;
        marks.forEach(function (m) {
          if (m.start > pos) segs.push({ hit: false, text: text.slice(pos, m.start) });
          if (m.end > m.start) segs.push({ hit: true, type: m.type, word: m.word, text: text.slice(m.start, m.end) });
          pos = Math.max(pos, m.end);
        });
        if (pos < text.length) segs.push({ hit: false, text: text.slice(pos) });
        return segs;
      }

      function buildSegments(img) {
        return segmentText(img.ocrText || '', img.violations, img.suspectedWords);
      }

      function buildTextSegments() {
        if (!textResult.value) return [];
        return segmentText(textInput.value || '', textResult.value.confirmed, textResult.value.suspected);
      }

      async function startTextCheck() {
        var text = textInput.value.trim();
        if (!text) { textError.value = '请先输入需要检测的文案'; return; }
        textRunning.value = true;
        textError.value = '';
        textResult.value = null;
        try {
          var result = await ApiService.violationDetect(text);
          if (!result || !result.success) throw new Error((result && result.error) || '文本检测失败');
          textResult.value = {
            confirmed: result.confirmed || [],
            suspected: result.suspected || [],
          };
        } catch (e) {
          textError.value = e.message || '文本检测失败';
        } finally {
          textRunning.value = false;
        }
      }

      function clearTextCheck() {
        textInput.value = '';
        textResult.value = null;
        textError.value = '';
      }

      Vue.onMounted(function () {
        // 提前触发服务端模型懒加载，用户选择图片期间即可完成 OCR 预热。
        if (ApiService.ocrWarmup) ApiService.ocrWarmup().catch(function () {});
      });

      var allProcessed = Vue.computed(function () {
        return _state.images.length > 0 &&
          _state.images.every(function (img) { return img.ocrText !== null && img.ocrText !== undefined; });
      });

      var hitCount = Vue.computed(function () {
        return _state.images.filter(function (img) { return hasIssue(img); }).length;
      });

      var passRate = Vue.computed(function () {
        if (_state.images.length === 0) return '--';
        if (!allProcessed.value) return '--';
        return Math.round((_state.images.length - hitCount.value) / _state.images.length * 100) + '%';
      });

      var resultSummary = Vue.computed(function () {
        if (_state.images.length === 0) return { text: '等待检测', color: '#94a3b8' };
        if (!allProcessed.value) return { text: _state.images.length + ' 张待检测', color: '#94a3b8' };
        if (hitCount.value > 0) {
          var totalConfirmed = 0, totalSuspected = 0;
          _state.images.forEach(function (img) {
            totalConfirmed += img.violations ? img.violations.length : 0;
            totalSuspected += img.suspectedWords ? img.suspectedWords.length : 0;
          });
          var parts = [];
          if (totalConfirmed > 0) parts.push(totalConfirmed + ' 处精确违规');
          if (totalSuspected > 0) parts.push(totalSuspected + ' 处疑似违规');
          return { text: _state.images.length + ' 张图片 · ' + hitCount.value + ' 张命中 · ' + parts.join(' + '), color: '#ef4444' };
        }
        return { text: _state.images.length + ' 张图片 · 全部合规', color: '#16a34a' };
      });

      // ---- OCR 检测核心 ----
      async function processBatch(batch) {
        var imageDataUrls = batch.map(function (img) { return img.ocrDataUrl || img.dataUrl; });
        var json = await ApiService.ocrDetect(imageDataUrls);
        var data = (json && json.results) ? json.results : [];
        for (var j = 0; j < batch.length; j++) {
          var img = batch[j];
          var result = data[j];
          img.ocrText = (result && result.text) ? result.text : '';
          img.ocrLines = (result && result.lines) ? result.lines : 0;
          img.ocrConfidence = (result && result.confidence) ? result.confidence : 0;
          img.ocrFallback = (result && result.fallback) ? result.fallback : false;
          img.ocrError = (result && result.error) ? result.error : null;
          if (!img.ocrText && !img.ocrError && json && json.error) img.ocrError = json.error;
          img.violations = [];
          img.suspectedWords = [];
          if (img.ocrText && img.ocrText.trim()) {
            var vJson = await ApiService.violationDetect(img.ocrText);
            if (vJson && vJson.success) {
              img.violations = vJson.confirmed || [];
              img.suspectedWords = vJson.suspected || [];
            }
          }
        }
      }

      async function startOcr() {
        if (_state.running) return;
        if (_state.images.length === 0) { App.showToast('请先选择图片', 'warning'); return; }

        _state.running = true;
        _state.scanning = true;
        _state.pass = false;
        _state.progressShow = true;
        _state.processingLeft = _state.images.length;

        var total = _state.images.length;
        var failedCount = 0;
        _state.progressPct = 0;
        _state.progressText = '正在识别 0/' + total;

        for (var start = 0; start < total; start += _VD_BATCH_SIZE) {
          var batch = _state.images.slice(start, start + _VD_BATCH_SIZE);
          try {
            await processBatch(batch);
          } catch (e) {
            console.error('OCR API error:', e);
            failedCount += batch.length;
            batch.forEach(function (img) {
              img.ocrText = '';
              img.ocrError = e.message;
              img.violations = [];
              img.suspectedWords = [];
            });
          }
          var done = Math.min(start + _VD_BATCH_SIZE, total);
          var remaining = Math.max(0, total - done);
          _state.processingLeft = remaining;
          _state.progressPct = Math.round(done / total * 100);
          _state.progressText = '正在识别 ' + done + '/' + total;
        }

        _state.running = false;
        _state.scanning = false;
        _state.progressShow = false;
        _state.processingLeft = 0;

        var hitCnt = _state.images.filter(function (img) { return hasIssue(img); }).length;
        _state.pass = (hitCnt === 0);

        var notifyIcon, notifyColor, notifyTitle;
        if (failedCount > 0) {
          notifyIcon = 'fa-circle-exclamation'; notifyColor = '#ef4444';
          notifyTitle = '检测完成：' + total + ' 张，' + failedCount + ' 张识别失败';
        } else if (hitCnt > 0) {
          notifyIcon = 'fa-triangle-exclamation'; notifyColor = '#f59e0b';
          notifyTitle = '检测完成：' + total + ' 张，' + hitCnt + ' 张含违规词';
        } else {
          notifyIcon = 'fa-circle-check'; notifyColor = '#16a34a';
          notifyTitle = '检测完成：' + total + ' 张，全部合规';
        }
        _state.notifications.unshift({
          id: Date.now(),
          icon: notifyIcon,
          color: notifyColor,
          title: notifyTitle,
          time: new Date().toLocaleString('zh-CN', { hour12: false }),
        });

        if (failedCount > 0) {
          App.showToast('检测完成：' + total + ' 张图片，' + failedCount + ' 张识别失败', 'error');
        } else {
          App.showToast('检测完成：' + total + ' 张图片，' + hitCnt + ' 张含违规词', hitCnt > 0 ? 'warning' : 'success');
        }
      }

      function clearAll() {
        _state.images.splice(0, _state.images.length);
        _nextImgId = 1;
        _state.running = false;
        _state.scanning = false;
        _state.pass = false;
        _state.progressShow = false;
        _state.progressPct = 0;
        _state.progressText = '';
        _state.processingLeft = 0;
      }

      function removeImage(id) {
        var idx = _state.images.findIndex(function (img) { return img.id === id; });
        if (idx >= 0) _state.images.splice(idx, 1);
      }

      function toggleNotifications() { notifyOpen.value = !notifyOpen.value; }
      function clearNotifications() { _state.notifications.splice(0, _state.notifications.length); }

      function onFileChange(e) {
        addFiles(e.target.files);
        e.target.value = '';
      }

      function onDrop(e) {
        handleDrop(e);
        dragOver.value = false;
      }

      return {
        state: _state,
        activeMode, dragOver, notifyOpen,
        textInput, textRunning, textResult, textError,
        hitCount, passRate, allProcessed, resultSummary,
        imgState, thumbBorder, headerInfo, resultStatusText, buildSegments,
        onFileChange, onDrop, removeImage, startOcr, clearAll,
        startTextCheck, clearTextCheck, buildTextSegments,
        toggleNotifications, clearNotifications,
      };
    },

    template: `
<div class="vd-integrated">
  <div class="vd-mode-tabs" role="tablist" aria-label="违规词检测方式">
    <button type="button" :class="{on:activeMode==='image'}" @click="activeMode='image'"><i class="fa-solid fa-image"></i><span>图片检测<small>PaddleOCR 提取文字并匹配词库</small></span></button>
    <button type="button" :class="{on:activeMode==='text'}" @click="activeMode='text'"><i class="fa-solid fa-align-left"></i><span>文本检测<small>直接检测文案与脚本内容</small></span></button>
  </div>

  <template v-if="activeMode==='image'">
  <!-- 顶部统计卡片 -->
  <div class="vd-stats-row">
    <div class="vd-stat-card">
      <div class="vd-stat-icon" style="background:#eff6ff;color:#3b82f6"><i class="fa-solid fa-images"></i></div>
      <div class="vd-stat-body"><span class="vd-stat-num">{{ state.images.length }}</span><span class="vd-stat-label">已上传</span></div>
    </div>
    <div class="vd-stat-card">
      <div class="vd-stat-icon" style="background:#fef3c7;color:#f59e0b"><i class="fa-solid" :class="state.running ? 'fa-spinner fa-spin' : 'fa-spinner'"></i></div>
      <div class="vd-stat-body"><span class="vd-stat-num">{{ state.processingLeft }}</span><span class="vd-stat-label">检测中</span></div>
    </div>
    <div class="vd-stat-card">
      <div class="vd-stat-icon" style="background:#fee2e2;color:#ef4444"><i class="fa-solid fa-triangle-exclamation"></i></div>
      <div class="vd-stat-body"><span class="vd-stat-num">{{ hitCount }}</span><span class="vd-stat-label">违规命中</span></div>
    </div>
    <div class="vd-stat-card">
      <div class="vd-stat-icon" style="background:#dcfce7;color:#16a34a"><i class="fa-solid fa-circle-check"></i></div>
      <div class="vd-stat-body"><span class="vd-stat-num">{{ passRate }}</span><span class="vd-stat-label">通过率</span></div>
    </div>
    <!-- 通知铃铛 -->
    <div class="vd-notify-bell" @click="toggleNotifications" title="检测通知">
      <i class="fa-solid fa-bell"></i>
      <span class="vd-bell-badge" v-if="state.notifications.length > 0">{{ state.notifications.length }}</span>
    </div>
    <!-- 通知下拉 -->
    <div class="vd-notify-dropdown" v-if="notifyOpen">
      <div class="vd-notify-header"><span>检测通知</span><button @click="clearNotifications">清空</button></div>
      <div class="vd-notify-list">
        <div class="vd-notify-empty" v-if="state.notifications.length === 0">暂无通知</div>
        <div class="vd-notify-item" v-for="n in state.notifications" :key="n.id">
          <i class="fa-solid" :class="n.icon" :style="{ color: n.color }"></i>
          <div>
            <div>{{ n.title }}</div>
            <div class="vd-notify-time">{{ n.time }}</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- 主体双栏 -->
  <div class="vd-main-grid">
    <!-- 左侧：上传源区 -->
    <div class="vd-upload-panel">
      <div class="vd-panel-header">
        <span class="vd-panel-title"><i class="fa-solid fa-cloud-arrow-up"></i> 上传源区</span>
        <span class="vd-panel-hint">JPG / PNG / WebP · 多图 · 文件夹</span>
      </div>
      <div class="vd-drop-zone" :class="{ 'vd-drop-active': dragOver }"
           @dragover.prevent="dragOver = true"
           @dragleave="dragOver = false"
           @drop.prevent="onDrop"
           @click="$refs.fileInput.click()">
        <div class="vd-drop-icon"><i class="fa-solid fa-cloud-arrow-up"></i></div>
        <div class="vd-drop-text">拖拽图片到此处，或<span class="vd-drop-link">点击选择</span></div>
        <div class="vd-drop-sub">支持选择多张图片或整个文件夹</div>
        <input ref="fileInput" type="file" accept="image/*" multiple style="display:none" @change="onFileChange">
      </div>
      <div class="vd-thumb-gallery">
        <div class="vd-thumb-empty" v-if="state.images.length === 0">
          <div class="vd-empty-illustration"><i class="fa-solid fa-image" style="font-size:48px;color:#cbd5e1"></i></div>
          <div class="vd-empty-text">暂无图片，请上传</div>
        </div>
        <div class="vd-thumb-list" v-else>
          <div v-for="img in state.images" :key="img.id"
               :style="{ position:'relative', width:'64px', height:'64px', borderRadius:'8px', overflow:'hidden', border:'2px solid ' + thumbBorder(img), flexShrink:'0', cursor:'pointer' }"
               :title="img.name + ' (点击移除)'"
               @click="removeImage(img.id)">
            <img :src="img.dataUrl" style="width:100%;height:100%;object-fit:cover">
            <span v-if="imgState(img)==='hit' || imgState(img)==='done'" style="position:absolute;top:2px;right:2px;font-size:10px;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,0.6)">
              <i class="fa-solid" :class="imgState(img)==='hit' ? 'fa-circle-exclamation' : 'fa-circle-check'"></i>
            </span>
            <span style="position:absolute;bottom:0;left:0;right:0;font-size:9px;color:#fff;background:rgba(0,0,0,0.5);padding:1px 4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ img.name }}</span>
          </div>
        </div>
      </div>
      <div class="vd-actions">
        <button class="vd-btn-detect" :disabled="state.running" @click="startOcr">
          <i class="fa-solid fa-magnifying-glass"></i> 开始检测
        </button>
        <button class="vd-btn-clear" @click="clearAll">
          <i class="fa-solid fa-trash-can"></i> 清空全部
        </button>
      </div>
      <div class="vd-progress-wrap" v-if="state.progressShow">
        <div class="vd-progress-bar"><div class="vd-progress-fill" :style="{ width: state.progressPct + '%' }"></div></div>
        <div class="vd-progress-text"><span>{{ state.progressText }}</span><span>{{ state.progressPct }}%</span></div>
      </div>
    </div>

    <!-- 右侧：检测结果区 -->
    <div class="vd-result-panel">
      <div class="vd-panel-header">
        <span class="vd-panel-title"><i class="fa-solid fa-clipboard-list"></i> 检测结果区</span>
        <span class="vd-panel-hint" :style="{ color: resultSummary.color }">{{ resultSummary.text }}</span>
      </div>
      <div class="vd-result-body">
        <!-- 检测中动画 -->
        <div class="vd-scanning" v-if="state.scanning">
          <div class="vd-scan-box">
            <div class="vd-scan-line"></div>
            <i class="fa-solid fa-spinner fa-spin" style="font-size:28px;color:#f59e0b"></i>
            <div style="margin-top:12px;color:#64748b;font-size:14px">OCR识别 + 违规检测中...</div>
          </div>
        </div>

        <!-- 空状态 -->
        <div class="vd-result-empty" v-else-if="state.images.length === 0 || !allProcessed">
          <div class="vd-empty-illustration"><i class="fa-solid fa-magnifying-glass-chart" style="font-size:56px;color:#cbd5e1"></i></div>
          <div class="vd-empty-title">准备开始检测</div>
          <div class="vd-empty-desc">上传图片后点击「开始检测」，系统将自动提取文字并标记违规内容</div>
          <div class="vd-sample-badge">
            <span class="vd-sample-red">红色 = 精确违规</span>
            <span class="vd-sample-orange">橙色 = 疑似违规</span>
          </div>
        </div>

        <!-- 全部处理完 -->
        <template v-else>
          <div class="vd-pass-anim" v-if="state.pass">
            <div class="vd-pass-check"><i class="fa-solid fa-circle-check"></i></div>
            <div>
              <div class="vd-pass-title">未发现违规，合规通过!</div>
              <div class="vd-pass-desc">所有图片均未命中违规词库</div>
            </div>
          </div>
          <div v-for="img in state.images" :key="'r' + img.id" style="margin-bottom:16px;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden">
            <div style="display:flex;align-items:center;gap:10px;padding:10px 14px;background:#f8fafc;border-bottom:1px solid #e2e8f0">
              <img :src="img.dataUrl" style="width:40px;height:40px;border-radius:6px;object-fit:cover">
              <div style="flex:1;min-width:0">
                <div style="font-size:13px;font-weight:600;color:#1e293b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" :title="img.name">{{ img.name }}</div>
                <div style="font-size:12px" :style="{ color: headerInfo(img).color }">
                  <i class="fa-solid" :class="headerInfo(img).icon"></i> {{ resultStatusText(img) }}
                </div>
              </div>
              <div style="font-size:11px;color:#94a3b8">
                {{ img.ocrConfidence ? '置信度 ' + Math.round(img.ocrConfidence * 100) + '%' : '' }}{{ img.ocrFallback ? ' · 降级识别' : '' }}
              </div>
            </div>
            <div style="padding:10px 14px;font-size:13px;line-height:1.8;max-height:200px;overflow-y:auto;word-break:break-all">
              <template v-if="img.ocrError && !img.ocrText">
                <span style="color:#dc2626;font-style:italic">[识别失败: {{ img.ocrError }}]</span>
              </template>
              <template v-else-if="!img.ocrText">
                <span style="color:#94a3b8;font-style:italic">[未检测到文字]</span>
              </template>
              <template v-else>
                <template v-for="(seg, si) in buildSegments(img)" :key="si">
                  <mark v-if="seg.hit" :class="seg.type === 'confirmed' ? 'vd-conf-mark' : 'vd-susp-mark'" :title="(seg.type === 'confirmed' ? '精确违规: ' : '疑似违规: ') + seg.word">{{ seg.text }}</mark>
                  <span v-else>{{ seg.text }}</span>
                </template>
              </template>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
  </template>

  <template v-else>
    <div class="vd-text-grid">
      <section class="vd-upload-panel vd-text-panel">
        <div class="vd-panel-header">
          <span class="vd-panel-title"><i class="fa-solid fa-pen-to-square"></i> 输入待检测文本</span>
          <span class="vd-panel-hint">标题 / 正文 / 口播 / 评论话术</span>
        </div>
        <textarea v-model="textInput" maxlength="30000" class="vd-textarea" placeholder="粘贴需要检测的文案。系统将标记精确违规词与疑似风险词，不会把内容发送给生成模型。"></textarea>
        <div class="vd-text-meta"><span>本次仅调用本地违规词库</span><span>{{ textInput.length }} / 30000</span></div>
        <div class="vd-actions">
          <button class="vd-btn-detect" :disabled="textRunning" @click="startTextCheck"><i class="fa-solid" :class="textRunning?'fa-spinner fa-spin':'fa-shield-halved'"></i> {{ textRunning ? '检测中' : '开始检测' }}</button>
          <button class="vd-btn-clear" @click="clearTextCheck"><i class="fa-solid fa-eraser"></i> 清空</button>
        </div>
        <div v-if="textError" class="vd-text-error"><i class="fa-solid fa-circle-exclamation"></i>{{ textError }}</div>
      </section>

      <section class="vd-result-panel vd-text-result">
        <div class="vd-panel-header">
          <span class="vd-panel-title"><i class="fa-solid fa-clipboard-check"></i> 文本检测结果</span>
          <span v-if="textResult" class="vd-panel-hint" :style="{color:(textResult.confirmed.length || textResult.suspected.length) ? '#dc5f76' : '#2f9c7d'}">
            {{ textResult.confirmed.length }} 处精确违规 · {{ textResult.suspected.length }} 处疑似风险
          </span>
        </div>
        <div v-if="!textResult" class="vd-result-empty">
          <div class="vd-empty-illustration"><i class="fa-solid fa-file-shield" style="font-size:56px;color:#c9c1f3"></i></div>
          <div class="vd-empty-title">等待检测文案</div>
          <div class="vd-empty-desc">输入文本后开始检测，风险词会在原文中高亮显示</div>
          <div class="vd-sample-badge"><span class="vd-sample-red">红色 = 精确违规</span><span class="vd-sample-orange">橙色 = 疑似风险</span></div>
        </div>
        <div v-else class="vd-text-result-body">
          <div class="vd-pass-anim" v-if="!textResult.confirmed.length && !textResult.suspected.length">
            <div class="vd-pass-check"><i class="fa-solid fa-circle-check"></i></div>
            <div><div class="vd-pass-title">未发现违规，合规通过</div><div class="vd-pass-desc">当前文本未命中启用中的违规词库</div></div>
          </div>
          <div class="vd-highlight-text">
            <template v-for="(seg, si) in buildTextSegments()" :key="si">
              <mark v-if="seg.hit" :class="seg.type === 'confirmed' ? 'vd-conf-mark' : 'vd-susp-mark'" :title="(seg.type === 'confirmed' ? '精确违规: ' : '疑似违规: ') + seg.word">{{ seg.text }}</mark>
              <span v-else>{{ seg.text }}</span>
            </template>
          </div>
        </div>
      </section>
    </div>
  </template>
</div>
    `,
  };

  // ==================== 挂载 / 卸载 / 互斥钩子 ====================
  var _violationApp = null;

  // 暴露组件供内容创作中心复用；保留旧路由挂载作为历史链接兼容。
  window.ContentViolationPage = ViolationPage;

  function mountViolationVue() {
    if (_violationApp) return;
    var oldSection = document.getElementById('page-toolbox-violation-check');
    var mount = document.getElementById('page-toolbox-violation-check-vue');
    if (!oldSection || !mount) return;
    oldSection.style.display = 'none';
    mount.classList.remove('hidden');
    _violationApp = Vue.createApp(ViolationPage);
    _violationApp.mount(mount);
  }

  function unmountViolationVue() {
    if (!_violationApp) return;
    _violationApp.unmount();
    _violationApp = null;
    var mount = document.getElementById('page-toolbox-violation-check-vue');
    if (mount) { mount.classList.add('hidden'); mount.innerHTML = ''; }
    var oldSection = document.getElementById('page-toolbox-violation-check');
    if (oldSection) oldSection.style.display = '';
  }

  function installViolationHook() {
    var oldSection = document.getElementById('page-toolbox-violation-check');
    if (!oldSection) return;
    if (!oldSection.classList.contains('hidden')) mountViolationVue();
    var observer = new MutationObserver(function () {
      if (!oldSection.classList.contains('hidden')) mountViolationVue();
      else unmountViolationVue();
    });
    observer.observe(oldSection, { attributes: true, attributeFilter: ['class'] });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installViolationHook);
  } else {
    installViolationHook();
  }
})();
