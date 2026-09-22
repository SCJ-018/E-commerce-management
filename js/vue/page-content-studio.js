(function () {
  'use strict';
  var mount = document.getElementById('page-content-studio');
  if (!mount || !window.Vue) return;

  var categories = [
    { name:'汽车脚垫', icon:'fa-car-side', sub:'座舱场景与使用体验' },
    { name:'汽车座垫', icon:'fa-chair', sub:'舒适度与材质对比' },
    { name:'后备箱垫', icon:'fa-box-open', sub:'收纳、防护与适配' },
    { name:'车载配件', icon:'fa-gauge-high', sub:'实用功能与细节' }
  ];
  var noteTypes = ['测评','种草','干货','引流','实拍','扣测'];
  function accountKey() { return 'content_studio_cards_' + (sessionStorage.getItem('admin_current_account') || 'local'); }
  function loadCards() {
    try { var x = JSON.parse(localStorage.getItem(accountKey()) || '[]'); return Array.isArray(x) ? x.slice(0,50) : []; }
    catch (e) { return []; }
  }
  function saveCards(cards) { try { localStorage.setItem(accountKey(), JSON.stringify(cards.slice(0,50))); } catch (e) {} }
  function post(path, payload) {
    return fetch('/api/content-studio/' + path, { method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) })
      .then(function (r) { if (r.status === 401) throw new Error('登录已过期，请重新登录'); return r.json(); })
      .then(function (r) { if (!r || r.code !== 0) throw new Error((r && r.msg) || '请求失败'); return r.data; });
  }
  function textVal(x) { return Array.isArray(x) ? x.join('\n') : String(x || ''); }
  function demoAnalysis(raw) {
    var title = raw.match(/(?:标题|题目)[:：]\s*([^\n]+)/);
    return { title:(title && title[1] ? title[1].trim() : '一套脚垫用了三个月，我终于找到清洁不返味的方法'), sourceUrl:'', evidence:'演示拆解：未连接服务端 AI', breakdown:{
      topic:'围绕“长期使用后的真实变化”切入，面向正在比较材质、清洁成本和耐用性的车主。',
      structure:'前 3 秒抛出结果或反常识问题 → 展示使用场景 → 给出 2 至 3 个对比证据 → 总结适用人群并引导评论。',
      title:'使用时间 + 明确结果 + 具体场景；标题承诺可验证的体验，避免泛泛而谈。',
      shots:'近景展示污渍或细节 → 手部实拍安装/清洁 → 俯拍整体效果 → 对比镜头收尾。未提供视频时不判断真实镜头。',
      comments:'“你们更在意耐脏还是好清洁？”\n“想看哪种材质的对比，我补一条实拍。”',
      learn:'把抽象卖点换成可观察的使用结果；先给结论，再用连续的小证据降低理解成本。',
      risks:'不能把单次体验写成普遍结论；测试条件要说清；不要复制原文独特句式、画面编排或评论引导。'
    }};
  }
  function demoGeneration(data) {
    var n=data.productName, c=data.category, t=data.noteType, s=data.sellingPoints;
    return { topics:[n+'真实使用一周后的 3 个变化',c+'怎么选：材质、适配和清洁成本',t+'视角拆解 '+n+' 的一个关键卖点', '预算有限时，先看 '+n+' 的这项细节', n+'适合哪些人，哪些人不必买'],
      matrix:[{angle:'真实体验',format:'实拍',hook:'先展示使用后的结果，再回放关键细节'},{angle:'避坑对比',format:'测评',hook:'同一场景只比较一个变量'},{angle:'场景解决方案',format:'干货',hook:'从车主常见痛点给出选择顺序'}],
      shooting:'0-3s：结果特写和一句结论；3-8s：展示安装或使用场景；8-15s：用近景呈现卖点“'+s.slice(0,40)+'”；15-22s：补充适用人群与注意事项。',
      titles:['用了 7 天，我终于知道 '+n+' 这个细节值不值','别只看价格：'+c+'先看这 3 个地方','真实体验｜'+n+'适合谁，哪些人可以跳过'],
      body:'最近在整理车内使用体验，想把 '+n+' 的真实感受说清楚。先说结论：'+s+'。\n\n我会按使用场景、清洁维护和适配细节逐项展示，大家可以根据自己的车型和需求判断。文中只写已确认的信息，具体效果以实际测试为准。\n\n如果你也在选 '+c+'，留言告诉我你的车型和最在意的点。',
      comments:'1. 你更在意耐脏、好清洁还是贴合度？\n2. 想看哪种车型的适配实拍，可以留言。\n3. 如果你的使用场景不同，建议先按自己的条件核验。',
      script:'【镜头 1｜0-3s】结果特写，口播：先看用了一段时间后的真实状态。\n【镜头 2｜3-8s】展示安装/清洁过程，口播：这里重点看 '+s.slice(0,32)+'。\n【镜头 3｜8-15s】拍细节和局部对比，口播：只描述看得见、测得到的变化。\n【镜头 4｜15-22s】正面总结，口播：适合……；如果你更在意……，请先核验。',
      checks:'发布前核对车型适配、价格和测试条件；删除无法证明的“全网最好”“绝对不返味”等表述；补齐实拍画面与必要的对比依据。'};
  }

  Vue.createApp({
    data: function () { return {
      activeTab:'breakdown', input:'', status:'', statusError:false, busy:false, cards:loadCards(), selectedId:null,
      category:'汽车脚垫', noteType:'测评', imitate:false, referenceId:null, productName:'', sellingPoints:'', audience:'', scene:'',
      output:null, productionBusy:false, productionStatus:'', productionError:false, categories:categories, noteTypes:noteTypes,
      userName:sessionStorage.getItem('admin_current_user') || '当前用户', userRole:sessionStorage.getItem('admin_current_role') || '团队成员', avatar:''
    }; },
    computed: {
      selectedCard:function () { var id=this.selectedId; return this.cards.find(function (x) { return x.id === id; }) || null; },
      referenceCard:function () { var id=this.referenceId; return this.cards.find(function (x) { return x.id === id; }) || null; }
    },
    mounted:function () { this.loadProfile(); },
    methods: {
      loadProfile:function () {
        var self=this;
        fetch('/api/profile/me', {credentials:'same-origin'}).then(function (r) { return r.json(); }).then(function (r) {
          if (!r || r.code !== 0 || !r.data) return;
          self.userName=r.data.name || self.userName;
          self.userRole=r.data.role || self.userRole;
          self.avatar=r.data.avatar || '';
        }).catch(function () {});
      },
      navigate:function (page) { window.location.hash=page; if (window.App && App.navigateTo) App.navigateTo(page); },
      selectCard:function (id) { this.selectedId=id; },
      removeCard:function () {
        if (!this.selectedCard) return;
        this.cards=this.cards.filter(function (x) { return x.id !== this.selectedId; },this);
        if (this.referenceId === this.selectedId) this.referenceId=null;
        this.selectedId=this.cards.length ? this.cards[0].id : null;
        saveCards(this.cards);
      },
      analyze:async function () {
        var raw=this.input.trim();
        if (!raw) { this.status='请粘贴抖音链接、文案或镜头摘要'; this.statusError=true; return; }
        this.busy=true; this.status='正在读取素材并生成拆解卡…'; this.statusError=false;
        try {
          var d;
          try { d=await post('analyze',{input:raw}); }
          catch (apiError) {
            if ((apiError.message || '').indexOf('登录已过期') >= 0) throw apiError;
            d=demoAnalysis(raw); this.status='服务端 AI 暂不可用，已生成演示拆解卡；配置 CONTENT_STUDIO_API_KEY 后可切换真实结果';
          }
          var card={id:Date.now(), createdAt:new Date().toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}), input:raw,
            title:d.title || '未命名爆文', sourceUrl:d.sourceUrl || '', evidence:d.evidence || '', breakdown:d.breakdown || {}};
          this.cards.unshift(card); this.cards=this.cards.slice(0,50); saveCards(this.cards); this.selectedId=card.id;
          if (!this.status || this.status.indexOf('演示') < 0) this.status='拆解完成，已保存到本机爆文卡片'; this.statusError=false; this.input='';
        } catch (e) { this.status=e.message || '拆解失败'; this.statusError=true; }
        finally { this.busy=false; }
      },
      generate:async function () {
        if (!this.productName.trim() || !this.sellingPoints.trim()) {
          this.productionStatus='请填写产品名称与真实卖点，避免 AI 编造产品信息'; this.productionError=true; return;
        }
        if (this.imitate && !this.referenceCard) { this.productionStatus='开启仿写前，请选择一张已拆解的爆文卡片'; this.productionError=true; return; }
        this.productionBusy=true; this.productionStatus='正在生成原创内容，请稍候…'; this.productionError=false;
        try {
          var payload={category:this.category,noteType:this.noteType,productName:this.productName.trim(),sellingPoints:this.sellingPoints.trim(),audience:this.audience.trim(),scene:this.scene.trim(),imitate:this.imitate,reference:this.imitate && this.referenceCard ? {title:this.referenceCard.title,breakdown:this.referenceCard.breakdown} : null};
          try { this.output=await post('generate',payload); this.productionStatus='已生成，可逐段复制并进行人工审核'; }
          catch (apiError) {
            if ((apiError.message || '').indexOf('登录已过期') >= 0) throw apiError;
            this.output=demoGeneration(payload); this.productionStatus='服务端 AI 暂不可用，已生成演示内容；配置 CONTENT_STUDIO_API_KEY 后可切换真实结果';
          }
          this.productionError=false;
        } catch (e) { this.productionStatus=e.message || '生成失败'; this.productionError=true; }
        finally { this.productionBusy=false; }
      },
      copy:function (value) {
        var self=this; navigator.clipboard.writeText(textVal(value)).then(function () { self.productionStatus='已复制到剪贴板'; self.productionError=false; })
          .catch(function () { self.productionStatus='复制失败，请手动选择文本'; self.productionError=true; });
      },
      asText:textVal,
      display:function (value) { return textVal(value) || '暂无内容'; }
    },
    template:`<div class="cs-shell">
      <header class="cs-topbar">
        <div class="cs-brand"><div class="cs-brand-mark"><i class="fa-solid fa-water"></i></div><div><div class="cs-brand-name">聚浪内容工坊</div><span class="cs-brand-sub">JULANG CONTENT STUDIO</span></div></div>
        <nav class="cs-nav" aria-label="后台导航">
          <button type="button" class="cs-nav-btn" @click="navigate('marketing-overview')">数据概览</button>
          <button type="button" class="cs-nav-btn" @click="navigate('operation-performance')">运营管理</button>
          <button type="button" class="cs-nav-btn active">内容创作中心</button>
          <button type="button" class="cs-nav-btn" @click="navigate('finance')">财务中心</button>
          <button type="button" class="cs-nav-btn" @click="navigate('profile')">个人中心</button>
        </nav>
        <div class="cs-user" :title="userName+' · '+userRole"><img v-if="avatar" :src="avatar" alt="当前用户头像" class="cs-user-avatar" style="object-fit:cover"><div v-else class="cs-user-avatar">{{ userName.charAt(0) }}</div><div class="cs-user-copy"><div class="cs-user-name">{{ userName }}</div><div class="cs-user-role">{{ userRole }}</div></div></div>
      </header>
      <div class="cs-page">
        <div class="cs-hero"><div><div class="cs-kicker">CONTENT WORKSPACE</div><h1 class="cs-title">聚浪内容工坊</h1><p class="cs-desc">从爆文洞察，到原创内容。投喂抖音素材提炼方法，再结合真实产品信息，生成可审核、可拍摄的内容方案。</p></div><div class="cs-hero-meta"><span class="cs-badge mint"><i class="fa-solid fa-circle-check"></i> 爆文卡片 {{ cards.length }} 张</span><span class="cs-badge"><i class="fa-solid fa-bolt"></i> 拆解与生产一站完成</span></div></div>
        <div class="cs-workspace">
          <div class="cs-workspace-head"><div class="cs-tabs" role="tablist"><button type="button" role="tab" class="cs-tab" :class="{active:activeTab==='breakdown'}" :aria-selected="activeTab==='breakdown'" @click="activeTab='breakdown'"><i class="fa-solid fa-magnifying-glass-chart"></i> 爆文拆解</button><button type="button" role="tab" class="cs-tab" :class="{active:activeTab==='production'}" :aria-selected="activeTab==='production'" @click="activeTab='production'"><i class="fa-solid fa-wand-magic-sparkles"></i> 爆文生产</button></div><div class="cs-head-note"><i class="fa-solid fa-circle"></i> 基于提供的素材分析，发布前请人工核验</div></div>
          <div class="cs-panel" v-show="activeTab==='breakdown'">
            <div class="cs-grid"><div>
              <div class="cs-card" style="margin-bottom:16px"><div class="cs-card-head"><div><div class="cs-card-title">投喂爆文素材</div><div class="cs-card-sub">支持抖音视频链接、分享文案，也可补充口播文本或镜头摘要</div></div><span class="cs-badge">01 / INPUT</span></div><div class="cs-card-body"><label class="cs-label" for="csInput">素材内容 <span>公开链接可能无法提取视频正文</span></label><textarea id="csInput" v-model="input" class="cs-textarea" maxlength="20000" placeholder="粘贴抖音视频链接；若希望分析结构和镜头，请一并粘贴口播文案、字幕或镜头摘要。"></textarea><div class="cs-row-actions"><span class="cs-hint">建议包含标题、开头钩子、主要画面和评论信息</span><button type="button" class="cs-btn primary" :disabled="busy" @click="analyze"><i class="fa-solid fa-sparkles"></i> {{ busy ? '正在拆解' : '生成拆解卡' }}</button></div><div class="cs-status" :class="{show:!!status,error:statusError}">{{ status }}</div></div></div>
              <div class="cs-card soft"><div class="cs-card-head"><div><div class="cs-card-title">已拆解的爆文卡片</div><div class="cs-card-sub">点击卡片查看完整分析；本机当前账号保留最近 50 张</div></div><span class="cs-badge mint">{{ cards.length }} 张</span></div><div v-if="cards.length" class="cs-list"><button v-for="card in cards" :key="card.id" type="button" class="cs-source" :class="{selected:selectedId===card.id}" @click="selectCard(card.id)"><span class="cs-source-icon"><i class="fa-brands fa-tiktok"></i></span><span class="cs-source-copy"><span class="cs-source-title">{{ card.title }}</span><span class="cs-source-meta">{{ card.createdAt }} · {{ card.evidence || '用户提供素材' }}</span></span><i class="fa-solid fa-chevron-right" style="font-size:10px;color:#b9b3cb"></i></button></div><div v-else class="cs-result-empty" style="min-height:120px;margin:0 18px 18px">还没有拆解卡。先投喂一条素材开始。</div></div>
            </div><div class="cs-card"><div class="cs-card-head"><div><div class="cs-card-title">拆解成果 <span v-if="selectedCard">· {{ selectedCard.title }}</span></div><div class="cs-card-sub">将选题、结构、标题、镜头和评论话术转成可复用的方法</div></div><button v-if="selectedCard" type="button" class="cs-btn ghost" @click="removeCard">删除卡片</button></div><div class="cs-card-body"><template v-if="selectedCard"><div class="cs-result-grid"><div style="display:flex;flex-direction:column;gap:9px"><div class="cs-result-block"><h4><i class="fa-solid fa-bullseye"></i> 选题与受众</h4><p>{{ display(selectedCard.breakdown.topic) }}</p></div><div class="cs-result-block"><h4><i class="fa-solid fa-list-ol"></i> 内容结构</h4><p>{{ display(selectedCard.breakdown.structure) }}</p></div><div class="cs-result-block"><h4><i class="fa-solid fa-heading"></i> 标题策略</h4><p>{{ display(selectedCard.breakdown.title) }}</p></div><div class="cs-result-block"><h4><i class="fa-solid fa-video"></i> 镜头与节奏</h4><p>{{ display(selectedCard.breakdown.shots) }}</p></div><div class="cs-result-block"><h4><i class="fa-solid fa-comments"></i> 评论区互动模板</h4><p>{{ display(selectedCard.breakdown.comments) }}</p></div></div><div style="display:flex;flex-direction:column;gap:9px"><div class="cs-result-block good"><h4><i class="fa-solid fa-lightbulb"></i> 可借鉴点</h4><p>{{ display(selectedCard.breakdown.learn) }}</p></div><div class="cs-result-block risk"><h4><i class="fa-solid fa-triangle-exclamation"></i> 风险与验证</h4><p>{{ display(selectedCard.breakdown.risks) }}</p></div><div class="cs-result-block"><h4><i class="fa-solid fa-circle-info"></i> 分析依据</h4><p>{{ selectedCard.evidence || '用户提供素材' }}</p></div><button type="button" class="cs-btn mint" style="width:100%" @click="referenceId=selectedCard.id;imitate=true;activeTab='production'"><i class="fa-solid fa-arrow-right"></i> 参考这张卡片去生产</button></div></div></template><div v-else class="cs-result-empty"><div><i class="fa-solid fa-layer-group"></i>拆解完成后，这里会展示每条爆文的分析结果。</div></div></div></div></div>
          </div>
          <div class="cs-panel" v-show="activeTab==='production'"><div class="cs-production-grid"><div class="cs-card"><div class="cs-card-head"><div><div class="cs-card-title">生成配置</div><div class="cs-card-sub">真实产品信息越完整，内容越可用</div></div></div><div class="cs-card-body cs-config">
            <div class="cs-config-section"><div class="cs-label">01 · 选择品类</div><div class="cs-option-grid"><button v-for="item in categories" :key="item.name" type="button" class="cs-option" :class="{selected:category===item.name}" @click="category=item.name"><span class="cs-option-icon"><i class="fa-solid" :class="item.icon"></i></span><span><b>{{ item.name }}</b><small>{{ item.sub }}</small></span></button></div></div>
            <div class="cs-config-section"><div class="cs-label">02 · 笔记类型</div><div class="cs-chips"><button v-for="type in noteTypes" :key="type" type="button" class="cs-chip" :class="{active:noteType===type}" @click="noteType=type">{{ type }}</button></div><div class="cs-hint" style="margin-top:8px">“扣测”按对比测试处理，仍需提供真实测试依据。</div></div>
            <div class="cs-config-section"><div class="cs-label">03 · 产品信息 <span>必填项用 * 标记</span></div><input v-model="productName" class="cs-textarea" style="min-height:36px;height:36px;padding:8px 10px;margin-bottom:7px" placeholder="产品名称 *"><textarea v-model="sellingPoints" class="cs-textarea" style="min-height:69px;margin-bottom:7px" placeholder="真实卖点 / 参数 / 测试结论 *"></textarea><input v-model="audience" class="cs-textarea" style="min-height:36px;height:36px;padding:8px 10px;margin-bottom:7px" placeholder="目标人群（可选）"><input v-model="scene" class="cs-textarea" style="min-height:36px;height:36px;padding:8px 10px" placeholder="使用场景（可选）"></div>
            <div class="cs-config-section"><div class="cs-switch-row"><div class="cs-switch-label">参考爆文结构<small>只借鉴方法，不照抄原句</small></div><button type="button" class="cs-switch" :class="{on:imitate}" :aria-pressed="imitate" @click="imitate=!imitate"><span></span></button></div><div v-if="imitate" class="cs-reference" style="margin-top:11px"><label v-for="card in cards" :key="card.id" class="cs-ref"><input type="radio" :value="card.id" v-model="referenceId"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ card.title }}</span></label><div v-if="!cards.length" class="cs-hint">暂无可选卡片，请先完成爆文拆解。</div></div></div>
            <button type="button" class="cs-btn primary cs-generate" :disabled="productionBusy" @click="generate"><i class="fa-solid fa-wand-magic-sparkles"></i> {{ productionBusy ? '正在生成内容' : 'AI 生成内容' }}</button><div class="cs-status" :class="{show:!!productionStatus,error:productionError}">{{ productionStatus }}</div>
          </div></div><div class="cs-card cs-output"><div class="cs-output-head"><div><div class="cs-card-title">AI 生成结果</div><div class="cs-card-sub">从选题到拍摄与评论区，一套内容完整交付</div></div><div class="cs-output-state"><strong>●</strong> {{ output ? '可编辑参考稿' : '等待生成' }}</div></div><div class="cs-output-card"><template v-if="output"><div class="cs-output-section"><div class="cs-output-title">选题池 <button @click="copy(output.topics)">复制</button></div><div class="cs-pill-list"><span class="cs-pill" v-for="(x,i) in output.topics || []" :key="i">{{ x }}</span></div></div><div class="cs-output-section"><div class="cs-output-title">内容矩阵 <button @click="copy((output.matrix || []).map(x=>x.angle+'：'+x.format+' / '+x.hook).join('；'))">复制</button></div><div class="cs-matrix"><div v-for="(x,i) in output.matrix || []" :key="i" class="cs-matrix-item"><b>{{ x.angle }}</b><span>{{ x.format }} · {{ x.hook }}</span></div></div></div><div class="cs-output-section"><div class="cs-output-title">拍摄建议 <button @click="copy(output.shooting)">复制</button></div><div class="cs-output-copy">{{ display(output.shooting) }}</div></div><div class="cs-output-section"><div class="cs-output-title">多版本标题 <button @click="copy(output.titles)">复制</button></div><div class="cs-output-copy">{{ display(output.titles) }}</div></div><div class="cs-output-section"><div class="cs-output-title">正文 <button @click="copy(output.body)">复制</button></div><div class="cs-output-copy">{{ display(output.body) }}</div></div><div class="cs-output-section"><div class="cs-output-title">评论区话术 <button @click="copy(output.comments)">复制</button></div><div class="cs-output-copy">{{ display(output.comments) }}</div></div><div class="cs-output-section"><div class="cs-output-title">短视频脚本 <button @click="copy(output.script)">复制</button></div><div class="cs-output-copy">{{ display(output.script) }}</div></div><div class="cs-output-section"><div class="cs-output-title">发布前核验</div><div class="cs-output-copy">{{ display(output.checks) }}</div></div></template><div v-else class="cs-result-empty"><div><i class="fa-solid fa-pen-nib"></i>选择品类、填写真实卖点，点击「AI 生成内容」开始。</div></div></div></div></div></div>
        </div>
      </div>
    </div>`
  }).mount(mount);
})();
