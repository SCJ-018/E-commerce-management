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

  var FLOW_META = [
    ['投喂爆文素材','抖音链接、口播文案或镜头摘要'],
    ['爆文拆解','7 维结构化分析并落卡'],
    ['选择参考母本','可选：只借鉴方法，不照抄原句'],
    ['生成配置','品类 · 类型 · 真实卖点'],
    ['AI 生成内容','选题 → 脚本，共 8 段产出'],
    ['人工核验发布','核对适配、价格与测试条件']
  ];
  var BREAKDOWN_MAP = [
    ['选题与受众','topic'],['内容结构','structure'],['标题策略','title'],['镜头与节奏','shots'],
    ['评论区互动模板','comments'],['可借鉴点','learn'],['风险与验证','risks']
  ];

  var studioApp = Vue.createApp({
    components: { 'content-violation-page': window.ContentViolationPage },
    data: function () { return {
      workspaceTab:'creative',
      activeTab:'breakdown', input:'', analysisFocus:'', status:'', statusError:false, busy:false, cards:loadCards(), selectedId:null,
      category:'汽车脚垫', noteType:'测评', imitate:false, referenceId:null, productName:'', sellingPoints:'', audience:'', scene:'',
      output:null, productionBusy:false, productionStatus:'', productionError:false, aiDegraded:false,
      profileDone:false,
      categories:categories, noteTypes:noteTypes,
      userName:sessionStorage.getItem('admin_current_user') || '当前用户', userRole:sessionStorage.getItem('admin_current_role') || '团队成员', avatar:''
    }; },
    computed: {
      selectedCard:function () { var id=this.selectedId; return this.cards.find(function (x) { return x.id === id; }) || null; },
      referenceCard:function () { var id=this.referenceId; return this.cards.find(function (x) { return x.id === id; }) || null; },
      flowSteps:function () {
        var hasCards = this.cards.length > 0;
        var flags = [
          hasCards || !!this.input.trim(),
          hasCards,
          !!this.referenceId,
          !!(this.productName.trim() && this.sellingPoints.trim()),
          !!this.output,
          !!this.output
        ];
        var head = -1;
        for (var i=0;i<flags.length;i++) { if (!flags[i]) { head=i; break; } }
        return FLOW_META.map(function (m,i) {
          return { n:i+1, title:m[0], sub:m[1], state: flags[i] ? 'done' : (i === head ? 'now' : 'todo') };
        });
      },
      flowProgress:function () {
        var done = this.flowSteps.filter(function (s) { return s.state === 'done'; }).length;
        return Math.round(done / FLOW_META.length * 100);
      },
      statusText:function () {
        if (this.busy || this.productionBusy) return '处理中';
        if (this.statusError || this.productionError) return '异常';
        if (this.aiDegraded) return '演示降级';
        return '正常';
      },
      statusClass:function () {
        if (this.busy || this.productionBusy) return '';
        if (this.statusError || this.productionError) return 'pink';
        if (this.aiDegraded) return 'amber';
        return 'mint';
      }
    },
    mounted:function () {
      var self = this;
      self.loadProfile();
      // 融合拆解区：右半是「成果详情」，必须有一张选中卡片才有内容。
      // 本地有历史卡片但 selectedId 为空（刷新/换页回来）时，默认选中最新一张，
      // 否则左列有卡、右边却是空态，看起来像坏了。
      if (!self.selectedId && self.cards.length) self.selectedId = self.cards[0].id;
      window.addEventListener('content-studio-tab', function (e) {
        if (e && e.detail === 'violation') self.workspaceTab = 'violation';
      });
      // 本页在「登录之前」就已挂载（脚本首屏执行），那一刻 /api/profile/me 还是 401，
      // 之后再不会自动补取 → 用户卡会一直停在兜底文案「当前用户 / 团队成员」。
      // 所以每次页面被切到前台时补取一次（成功后不再重复请求）。
      try {
        var obs = new MutationObserver(function () {
          if (!mount.classList.contains('hidden') && !self.profileDone) self.loadProfile();
        });
        obs.observe(mount, { attributes:true, attributeFilter:['class', 'style'] });
      } catch (e) {}
      window.addEventListener('hashchange', function () { if (!self.profileDone) self.loadProfile(); });
    },
    methods: {
      loadProfile:function () {
        var self=this;
        if (self.profileDone) return;
        fetch('/api/profile/me', {credentials:'same-origin'}).then(function (r) { return r.json(); }).then(function (r) {
          if (!r || r.code !== 0 || !r.data) return;
          self.userName=r.data.name || self.userName;
          self.userRole=r.data.role || self.userRole;
          self.avatar=r.data.avatar || '';
          self.profileDone=true;
        }).catch(function () {});
      },
      navigate:function (page) { window.location.hash=page; if (window.App && App.navigateTo) App.navigateTo(page); },
      selectCard:function (id) { this.selectedId=id; },
      draftBadge:function (card) {
        var keys = (card && card.breakdown) ? Object.keys(card.breakdown) : [];
        if (!keys.length) return { cls:'amber', text:'待核验' };
        if (this.referenceId === card.id) return { cls:'pink', text:'参考中' };
        return { cls:'mint', text:'已拆解' };
      },
      goProduction:function (card) {
        if (!card) return;
        this.referenceId=card.id; this.imitate=true; this.activeTab='production';
      },
      clearInput:function () { this.input=''; this.analysisFocus=''; },
      removeCard:function () {
        var id = this.selectedId;
        if (!id) return;
        this.cards=this.cards.filter(function (x) { return x.id !== id; });
        if (this.referenceId === id) this.referenceId=null;
        this.selectedId=this.cards.length ? this.cards[0].id : null;
        saveCards(this.cards);
      },
      analyze:async function () {
        var raw=this.input.trim();
        var focus=this.analysisFocus.trim();
        if (!raw) { this.status='请粘贴抖音链接、文案或镜头摘要'; this.statusError=true; return; }
        this.busy=true; this.status='正在读取素材并生成拆解卡…'; this.statusError=false;
        try {
          var d;
          var payload = focus ? (raw + '\n\n【本次分析重点】\n' + focus) : raw;
          try { d=await post('analyze',{input:payload}); }
          catch (apiError) {
            if ((apiError.message || '').indexOf('登录已过期') >= 0) throw apiError;
            d=demoAnalysis(raw); this.aiDegraded=true;
            this.status='服务端 AI 暂不可用，已生成演示拆解卡；配置 CONTENT_STUDIO_API_KEY 后可切换真实结果';
          }
          var card={id:Date.now(), createdAt:new Date().toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}), input:raw,
            title:d.title || '未命名爆文', sourceUrl:d.sourceUrl || '', evidence:d.evidence || '', breakdown:d.breakdown || {}};
          this.cards.unshift(card); this.cards=this.cards.slice(0,50); saveCards(this.cards); this.selectedId=card.id;
          if (!this.status || this.status.indexOf('演示') < 0) this.status='拆解完成，已保存到本机爆文卡片'; this.statusError=false; this.input=''; this.analysisFocus='';
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
            this.output=demoGeneration(payload); this.aiDegraded=true;
            this.productionStatus='服务端 AI 暂不可用，已生成演示内容；配置 CONTENT_STUDIO_API_KEY 后可切换真实结果';
          }
          this.productionError=false;
        } catch (e) { this.productionStatus=e.message || '生成失败'; this.productionError=true; }
        finally { this.productionBusy=false; }
      },
      copy:function (value) {
        var self=this; navigator.clipboard.writeText(textVal(value)).then(function () { self.productionStatus='已复制到剪贴板'; self.productionError=false; })
          .catch(function () { self.productionStatus='复制失败，请手动选择文本'; self.productionError=true; });
      },
      copyBreakdown:function () {
        var b=this.selectedCard && this.selectedCard.breakdown;
        if (!b) { this.status='请先选择一张拆解卡片'; this.statusError=true; return; }
        var out=BREAKDOWN_MAP.map(function (x) { return '【'+x[0]+'】\n'+textVal(b[x[1]] || '暂无内容'); }).join('\n\n');
        this.copy(out);
      },
      copyMatrix:function () { return textVal((this.output && this.output.matrix || []).map(function (x) { return x.angle+'：'+x.format+' / '+x.hook; })); },
      asText:textVal,
      display:function (value) { return textVal(value) || '暂无内容'; }
    },
    template:`<div class="cs-shell">
      <nav class="cs-workspace-nav" aria-label="内容创作中心功能导航">
        <div class="cs-workspace-nav-inner">
          <div class="cs-workspace-title"><span class="mark"><i class="fa-solid fa-water"></i></span><span><b>内容创作中心</b><small>CONTENT STUDIO</small></span></div>
          <div class="cs-workspace-links" role="tablist">
            <button type="button" role="tab" :aria-selected="workspaceTab==='creative'" :class="{on:workspaceTab==='creative'}" @click="workspaceTab='creative'"><i class="fa-solid fa-wand-magic-sparkles"></i> 爆文创作</button>
            <button type="button" role="tab" :aria-selected="workspaceTab==='violation'" :class="{on:workspaceTab==='violation'}" @click="workspaceTab='violation'"><i class="fa-solid fa-shield-halved"></i> 违规词检测</button>
          </div>
          <div class="cs-workspace-note"><i class="fa-solid fa-circle-check"></i> 创作与合规一体化工作台</div>
        </div>
      </nav>

      <div v-if="workspaceTab==='creative'" class="cs-page">
        <div class="cs-layout">

          <!-- ==================== 左栏：流程 + 草稿 ==================== -->
          <aside class="cs-rail cs-rail-left">
            <div class="cs-card tint-mint">
              <div class="cs-brand">
                <div class="cs-brand-mark"><i class="fa-solid fa-water"></i></div>
                <div>
                  <div class="cs-brand-name">聚浪内容工坊</div>
                  <div class="cs-brand-sub">JULANG CONTENT STUDIO</div>
                </div>
              </div>
            </div>

            <div class="cs-card tint-pink">
              <div class="cs-user" :title="userName + ' · ' + userRole">
                <img v-if="avatar" :src="avatar" alt="当前用户头像" class="cs-user-avatar" style="object-fit:cover">
                <div v-else class="cs-user-avatar">{{ userName.charAt(0) }}</div>
                <div class="cs-user-copy">
                  <div class="cs-user-name">{{ userName }}</div>
                  <div class="cs-user-role">{{ userRole }}</div>
                  <div class="cs-user-tag"><span class="cs-badge mint"><i class="fa-solid fa-floppy-disk"></i> 草稿本机同步</span></div>
                </div>
              </div>
            </div>

            <div class="cs-card">
              <div class="cs-flow-head"><span class="t">创作流程</span><span class="p">{{ flowProgress }}%</span></div>
              <div class="cs-bar"><i :style="{width: flowProgress + '%'}"></i></div>
              <ul class="cs-steps">
                <li v-for="s in flowSteps" :key="s.n" class="cs-step" :class="s.state">
                  <span class="n">{{ s.n }}</span>
                  <span class="txt"><b>{{ s.title }}</b><small>{{ s.sub }}</small></span>
                </li>
              </ul>
            </div>

            <div class="cs-card">
              <div class="cs-head">
                <div class="grow"><div class="cs-title">拆解卡片</div><div class="cs-sub">本机当前账号保留最近 50 张</div></div>
                <span class="cs-badge">{{ cards.length }}</span>
              </div>
              <div v-if="cards.length" class="cs-list scroll">
                <button v-for="card in cards" :key="card.id" type="button" class="cs-draft" :class="{on:selectedId===card.id}" @click="selectCard(card.id)">
                  <span class="copy"><span class="nm">{{ card.title }}</span><span class="mt">{{ card.createdAt }}</span></span>
                  <span class="cs-badge" :class="draftBadge(card).cls" style="flex:none">{{ draftBadge(card).text }}</span>
                </button>
              </div>
              <div v-else class="cs-empty"><i class="fa-solid fa-layer-group"></i>还没有拆解卡，先投喂一条素材。</div>
            </div>
          </aside>

          <!-- ==================== 中栏：主流程 ==================== -->
          <main class="cs-main">
            <div class="cs-toolbar">
              <div class="cs-tabs" role="tablist">
                <button type="button" role="tab" class="cs-tab" :class="{on:activeTab==='breakdown'}" :aria-selected="activeTab==='breakdown'" @click="activeTab='breakdown'"><i class="fa-solid fa-magnifying-glass-chart"></i> 爆文拆解</button>
                <button type="button" role="tab" class="cs-tab" :class="{on:activeTab==='production'}" :aria-selected="activeTab==='production'" @click="activeTab='production'"><i class="fa-solid fa-wand-magic-sparkles"></i> 爆文生产</button>
              </div>
              <span class="cs-badge gray"><i class="fa-solid fa-circle-info"></i> 基于所提供素材分析，发布前请人工核验</span>
              <div class="right"><span class="cs-badge mint"><i class="fa-solid fa-check"></i> 自动缓存</span></div>
            </div>

            <!-- ---------- 视图 A：爆文拆解 ---------- -->
            <template v-if="activeTab==='breakdown'">
              <div class="cs-card cs-hero">
                <div class="cs-hero-top">
                  <div class="col">
                    <div class="cs-kicker">CONTENT WORKFLOW</div>
                    <h1>从爆文素材到可发布草稿</h1>
                    <p>投喂抖音链接或文案 → 自动拆解选题与结构 → 挑一张卡片做母本 → 结合真实产品信息生成可审核、可拍摄的完整内容方案。</p>
                  </div>
                  <div class="cs-hero-art"><i class="fa-solid fa-pen-nib"></i></div>
                </div>
                <div class="cs-stats">
                  <div class="cs-stat"><b>{{ cards.length }}</b><span>爆文卡片</span></div>
                  <div class="cs-stat"><b>7</b><span>拆解维度</span></div>
                  <div class="cs-stat"><b>6</b><span>笔记类型</span></div>
                  <div class="cs-stat"><b>8</b><span>内容产出段</span></div>
                </div>
              </div>

              <div class="cs-card">
                <div class="cs-head">
                  <div class="cs-chip"><i class="fa-solid fa-note-sticky"></i></div>
                  <div class="grow"><div class="cs-title">投喂爆文素材</div><div class="cs-sub">支持抖音视频链接、分享文案，也可补充口播文本或镜头摘要</div></div>
                  <span class="cs-badge mint"><i class="fa-solid fa-floppy-disk"></i> 自动缓存</span>
                  <button type="button" class="cs-btn primary" :disabled="busy" @click="analyze"><i class="fa-solid" :class="busy?'fa-spinner':'fa-wand-magic-sparkles'"></i> {{ busy ? '正在拆解' : '生成拆解卡' }}</button>
                </div>
                <div class="cs-pad">
                  <div class="cs-grid2">
                    <div class="cs-field">
                      <label class="cs-label" for="csInput">素材内容 <em>公开链接可能无法提取视频正文</em></label>
                      <textarea id="csInput" v-model="input" class="cs-ta" maxlength="20000" placeholder="粘贴抖音视频链接；若希望分析结构和镜头，请一并粘贴口播文案、字幕或镜头摘要。"></textarea>
                      <div class="cs-count"><span class="cache"><i class="fa-solid fa-check"></i> 本机自动保存</span><span>{{ input.length }} / 20000</span></div>
                    </div>
                    <div class="cs-field">
                      <label class="cs-label" for="csFocus">分析重点 <em>可选，留空则全维度拆解</em></label>
                      <textarea id="csFocus" v-model="analysisFocus" class="cs-ta" maxlength="1000" placeholder="例如：重点分析标题钩子、内容结构与镜头节奏；忽略价格与无法验证的效果承诺。"></textarea>
                      <div class="cs-count"><span>建议只写真正关心的维度，避免拆解跑偏</span><span>{{ analysisFocus.length }} / 1000</span></div>
                    </div>
                  </div>
                  <div class="cs-row-actions">
                    <span class="cs-hint">建议包含标题、开头钩子、主要画面和评论信息</span>
                    <button type="button" class="cs-btn ghost" @click="clearInput"><i class="fa-solid fa-eraser"></i> 清空</button>
                  </div>
                  <div class="cs-status" :class="{show:!!status, error:statusError}">{{ status }}</div>
                </div>
              </div>

              <div class="cs-card cs-fuse">
                <div class="cs-fuse-top">
                  <div class="mark"><i class="fa-solid fa-layer-group"></i></div>
                  <div class="ttl">
                    <b>爆文拆解区</b>
                    <small>左列选卡 → 右侧即时查看 7 维分析；把选题、结构、标题、镜头和评论话术转成可复用的方法</small>
                  </div>
                  <div class="tools">
                    <span class="cs-badge pink">{{ cards.length }} 张</span>
                    <button v-if="selectedCard" type="button" class="cs-btn" @click="copyBreakdown"><i class="fa-solid fa-copy"></i> 复制全部</button>
                  </div>
                </div>

                <div class="cs-fuse-body">
                  <!-- 左半：卡片选择列 -->
                  <div class="cs-fuse-items">
                    <div class="cs-fuse-items-head">
                      <span class="t">已拆解卡片</span>
                      <span class="cs-badge gray">共 {{ cards.length }} 张</span>
                    </div>
                    <div v-if="cards.length" class="cs-fuse-items-list">
                      <button v-for="card in cards" :key="card.id" type="button" class="cs-pick" :class="{on:selectedId===card.id}" @click="selectCard(card.id)">
                        <span class="tile"><i class="fa-brands fa-tiktok"></i></span>
                        <span class="body">
                          <span class="h">{{ card.title }}</span>
                          <span class="m">{{ card.createdAt }} · {{ card.evidence || '用户提供素材' }}</span>
                        </span>
                      </button>
                    </div>
                    <div v-else class="cs-empty"><i class="fa-solid fa-inbox"></i>还没有拆解卡。<br>先在上方投喂一条素材开始。</div>
                  </div>

                  <!-- 右半：成果详情 -->
                  <div class="cs-fuse-result">
                    <template v-if="selectedCard">
                      <div class="cs-fuse-result-head">
                        <div class="cs-chip mint" style="flex:none"><i class="fa-solid fa-bullseye"></i></div>
                        <div class="grow">
                          <div class="ttl">拆解成果 · 7 维</div>
                          <div class="sub" :title="selectedCard.title">{{ selectedCard.title }}</div>
                        </div>
                        <span class="cs-badge" :class="draftBadge(selectedCard).cls">{{ draftBadge(selectedCard).text }}</span>
                      </div>
                      <div class="cs-fuse-res">
                        <div class="cs-blk"><h4><i class="fa-solid fa-bullseye"></i> 选题与受众</h4><p>{{ display(selectedCard.breakdown.topic) }}</p></div>
                        <div class="cs-blk"><h4><i class="fa-solid fa-list-ol"></i> 内容结构</h4><p>{{ display(selectedCard.breakdown.structure) }}</p></div>
                        <div class="cs-grid2">
                          <div class="cs-blk"><h4><i class="fa-solid fa-heading"></i> 标题策略</h4><p>{{ display(selectedCard.breakdown.title) }}</p></div>
                          <div class="cs-blk"><h4><i class="fa-solid fa-video"></i> 镜头与节奏</h4><p>{{ display(selectedCard.breakdown.shots) }}</p></div>
                        </div>
                        <div class="cs-blk"><h4><i class="fa-solid fa-comments"></i> 评论区互动模板</h4><p>{{ display(selectedCard.breakdown.comments) }}</p></div>
                        <div class="cs-grid2">
                          <div class="cs-blk mint"><h4><i class="fa-solid fa-lightbulb"></i> 可借鉴点</h4><p>{{ display(selectedCard.breakdown.learn) }}</p></div>
                          <div class="cs-blk pink"><h4><i class="fa-solid fa-triangle-exclamation"></i> 风险与验证</h4><p>{{ display(selectedCard.breakdown.risks) }}</p></div>
                        </div>
                        <div class="cs-blk"><h4><i class="fa-solid fa-circle-info"></i> 分析依据</h4><p>{{ selectedCard.evidence || '用户提供素材' }}</p></div>
                      </div>
                      <div class="cs-fuse-foot">
                        <button type="button" class="cs-btn mint wide" @click="goProduction(selectedCard)"><i class="fa-solid fa-arrow-right"></i> 参考这张卡片去生产</button>
                        <div class="cs-row-actions" style="margin-top:9px">
                          <span class="cs-hint">只借鉴方法，不照抄原句与画面编排</span>
                          <button type="button" class="cs-btn ghost" @click="removeCard"><i class="fa-solid fa-trash-can"></i> 删除卡片</button>
                        </div>
                      </div>
                    </template>
                    <div v-else class="cs-empty"><i class="fa-solid fa-layer-group"></i>拆解完成后，这里会展示每条爆文的 7 维分析结果。</div>
                  </div>
                </div>
              </div>
            </template>

            <!-- ---------- 视图 B：爆文生产 ---------- -->
            <template v-else>
              <div class="cs-card cs-hero">
                <div class="cs-hero-top">
                  <div class="col">
                    <div class="cs-kicker">GENERATION WORKFLOW</div>
                    <h1>从真实卖点到可拍摄脚本</h1>
                    <p>配置品类、笔记类型与产品信息，需要时挂上一张拆解卡片做结构参考，一次生成 8 段可直接使用的产出。</p>
                  </div>
                  <div class="cs-hero-art"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
                </div>
                <div class="cs-stats">
                  <div class="cs-stat"><b>{{ output ? (output.topics || []).length : 0 }}</b><span>选题候选</span></div>
                  <div class="cs-stat"><b>{{ output ? (output.matrix || []).length : 0 }}</b><span>内容矩阵</span></div>
                  <div class="cs-stat"><b>8</b><span>输出段落</span></div>
                  <div class="cs-stat"><b>{{ referenceCard ? 1 : 0 }}</b><span>参考母本</span></div>
                </div>
              </div>

              <div class="cs-card">
                <div class="cs-head">
                  <div class="cs-chip"><i class="fa-solid fa-sliders"></i></div>
                  <div class="grow"><div class="cs-title">生成配置</div><div class="cs-sub">真实产品信息越完整，内容越可用</div></div>
                  <span class="cs-badge mint"><i class="fa-solid fa-floppy-disk"></i> 自动缓存</span>
                  <button type="button" class="cs-btn primary" :disabled="productionBusy" @click="generate"><i class="fa-solid" :class="productionBusy?'fa-spinner':'fa-wand-magic-sparkles'"></i> {{ productionBusy ? '正在生成' : 'AI 生成内容' }}</button>
                </div>
                <div class="cs-pad">
                  <div class="cs-divider">
                    <div class="cs-block-label">01 · 选择品类</div>
                    <div class="cs-opts">
                      <button v-for="item in categories" :key="item.name" type="button" class="cs-opt" :class="{on:category===item.name}" @click="category=item.name">
                        <span class="tile"><i class="fa-solid" :class="item.icon"></i></span>
                        <span><b>{{ item.name }}</b><small>{{ item.sub }}</small></span>
                      </button>
                    </div>
                  </div>
                  <div class="cs-divider">
                    <div class="cs-block-label">02 · 笔记类型</div>
                    <div class="cs-chips">
                      <button v-for="type in noteTypes" :key="type" type="button" class="cs-ch" :class="{on:noteType===type}" @click="noteType=type">{{ type }}</button>
                    </div>
                    <div class="cs-hint" style="margin-top:9px">「扣测」按对比测试处理，仍需提供真实测试依据。</div>
                  </div>
                  <div class="cs-divider">
                    <div class="cs-block-label">03 · 产品信息 <em>必填项用 * 标记</em></div>
                    <div class="cs-grid2">
                      <div class="cs-field">
                        <input v-model="productName" class="cs-ta line" maxlength="120" placeholder="产品名称 *">
                        <textarea v-model="sellingPoints" class="cs-ta" style="min-height:122px;margin-top:8px" maxlength="3000" placeholder="真实卖点 / 参数 / 测试结论 *"></textarea>
                      </div>
                      <div class="cs-field">
                        <input v-model="audience" class="cs-ta line" maxlength="300" placeholder="目标人群（可选）">
                        <input v-model="scene" class="cs-ta line" style="margin-top:8px" maxlength="300" placeholder="使用场景（可选）">
                        <div class="cs-switch-row" style="margin-top:12px">
                          <div class="cs-switch-label">参考爆文结构<small>只借鉴方法，不照抄原句</small></div>
                          <button type="button" class="cs-switch" :class="{on:imitate}" :aria-pressed="imitate" @click="imitate=!imitate"><span></span></button>
                        </div>
                      </div>
                    </div>
                    <div v-if="imitate" class="cs-refs">
                      <label v-for="card in cards" :key="card.id" class="cs-ref"><input type="radio" :value="card.id" v-model="referenceId"><span>{{ card.title }}</span></label>
                      <div v-if="!cards.length" class="cs-hint">暂无可选卡片，请先完成爆文拆解。</div>
                    </div>
                  </div>
                  <div class="cs-status" :class="{show:!!productionStatus, error:productionError}">{{ productionStatus }}</div>
                </div>
              </div>

              <div class="cs-card">
                <div class="cs-head">
                  <div class="cs-chip mint"><i class="fa-solid fa-sparkles"></i></div>
                  <div class="grow"><div class="cs-title">AI 生成结果</div><div class="cs-sub">从选题到拍摄与评论区，一套内容完整交付</div></div>
                  <span class="cs-badge" :class="output?'mint':'gray'">{{ output ? '可编辑参考稿' : '等待生成' }}</span>
                </div>
                <div v-if="output" class="cs-pad">
                  <div class="cs-out-sec">
                    <div class="cs-out-head"><span class="n"><i class="fa-solid fa-layer-group"></i> 选题池</span><button type="button" class="cs-copy" @click="copy(output.topics)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-pills"><span class="cs-pl" v-for="(x,i) in output.topics || []" :key="i">{{ x }}</span></div>
                  </div>
                  <div class="cs-out-sec">
                    <div class="cs-out-head"><span class="n"><i class="fa-solid fa-bullseye"></i> 内容矩阵</span><button type="button" class="cs-copy" @click="copyMatrix"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-mx">
                      <div v-for="(x,i) in output.matrix || []" :key="i" class="cs-mx-item"><b>{{ x.angle }}</b><span>{{ x.format }} · {{ x.hook }}</span></div>
                    </div>
                  </div>
                  <div class="cs-out-sec">
                    <div class="cs-out-head"><span class="n"><i class="fa-solid fa-video"></i> 拍摄建议</span><button type="button" class="cs-copy" @click="copy(output.shooting)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-out-body">{{ display(output.shooting) }}</div>
                  </div>
                  <div class="cs-out-sec">
                    <div class="cs-out-head"><span class="n"><i class="fa-solid fa-heading"></i> 多版本标题</span><button type="button" class="cs-copy" @click="copy(output.titles)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-out-body">{{ display(output.titles) }}</div>
                  </div>
                  <div class="cs-out-sec">
                    <div class="cs-out-head"><span class="n"><i class="fa-solid fa-note-sticky"></i> 正文</span><button type="button" class="cs-copy" @click="copy(output.body)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-out-body">{{ display(output.body) }}</div>
                  </div>
                  <div class="cs-out-sec">
                    <div class="cs-out-head"><span class="n"><i class="fa-solid fa-comments"></i> 评论区话术</span><button type="button" class="cs-copy" @click="copy(output.comments)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-out-body">{{ display(output.comments) }}</div>
                  </div>
                  <div class="cs-out-sec">
                    <div class="cs-out-head"><span class="n"><i class="fa-solid fa-film"></i> 短视频脚本</span><button type="button" class="cs-copy" @click="copy(output.script)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-out-body">{{ display(output.script) }}</div>
                  </div>
                  <div class="cs-out-sec">
                    <div class="cs-out-head"><span class="n"><i class="fa-solid fa-triangle-exclamation"></i> 发布前核验</span><button type="button" class="cs-copy" @click="copy(output.checks)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-out-body">{{ display(output.checks) }}</div>
                  </div>
                </div>
                <div v-else class="cs-empty"><i class="fa-solid fa-wand-magic-sparkles"></i>填好配置后点击「AI 生成内容」，这里会依次展示 8 段产出。</div>
              </div>
            </template>
          </main>

          <!-- ==================== 右栏：AI 服务状态 ==================== -->
          <aside class="cs-rail cs-rail-right">
            <div class="cs-card">
              <div class="cs-head">
                <div class="cs-chip"><i class="fa-solid fa-align-left"></i></div>
                <div class="grow"><div class="cs-title">文案生成</div><div class="cs-sub">模型由服务端统一调度</div></div>
                <span class="cs-badge" :class="aiDegraded?'amber':'mint'">{{ aiDegraded ? '演示降级' : '服务端 AI' }}</span>
              </div>
              <div class="cs-pad">
                <div class="cs-kv">
                  <div><span class="k">调用方式</span><div class="v">服务端 AI（密钥仅存在服务器环境变量，前端不保存）</div></div>
                  <div><span class="k">接口</span><div class="v mute">/api/content-studio/analyze<br>/api/content-studio/generate</div></div>
                </div>
              </div>
            </div>

            <div class="cs-card">
              <div class="cs-head">
                <div class="cs-chip pink"><i class="fa-solid fa-image"></i></div>
                <div class="grow"><div class="cs-title">图片生成</div><div class="cs-sub">封面与配图能力</div></div>
                <span class="cs-badge gray">未接入</span>
              </div>
              <div class="cs-pad">
                <div class="cs-kv">
                  <div><span class="k">当前状态</span><div class="v mute">本页暂不生成图片；封面与配图请走本地流程</div></div>
                </div>
              </div>
            </div>

            <div class="cs-card">
              <div class="cs-head">
                <div class="cs-chip mint"><i class="fa-solid fa-circle-info"></i></div>
                <div class="grow"><div class="cs-title">状态与错误提示</div><div class="cs-sub">降级、限流与失败原因集中在此</div></div>
                <span class="cs-badge" :class="statusClass">{{ statusText }}</span>
              </div>
              <div class="cs-state" :class="aiDegraded?'':'mint'">
                <div class="cs-state-line"><i class="fa-solid fa-check"></i> {{ cards.length ? '已拆解 ' + cards.length + ' 张卡片，本机保留最近 50 张' : '还没有拆解卡片，先从左侧投喂素材' }}</div>
                <div class="cs-state-line"><i class="fa-solid fa-check"></i> {{ output ? '已生成 8 段内容产出，可逐段复制并人工核验' : '生成结果会逐段展示，可单独复制' }}</div>
              </div>
              <div class="cs-state">
                <div class="cs-state-line amber"><i class="fa-solid fa-triangle-exclamation"></i> 服务端未配置模型密钥时自动降级为演示结果，页面以黄色提示条标出。</div>
                <div class="cs-state-line amber"><i class="fa-solid fa-triangle-exclamation"></i> 登录过期（401）时提示重新登录，不会静默失败。</div>
              </div>
              <ul class="cs-tips">
                <li><i class="fa-solid fa-circle-check"></i> 只根据你提供的素材分析，不臆造视频画面与真实评论。</li>
                <li><i class="fa-solid fa-circle-check"></i> 参考爆文只借鉴选题与结构，不复制原句。</li>
              </ul>
            </div>
          </aside>

        </div>
      </div>

      <div v-else class="cs-page cs-violation-wrap">
        <div class="cs-violation-hero">
          <div><div class="cs-kicker">CONTENT COMPLIANCE</div><h1>发布前违规词检测</h1><p>图片由本地 PaddleOCR 提取原文，文本直接匹配启用中的违规词库；低置信度结果请人工复核。</p></div>
          <div class="cs-violation-shield"><i class="fa-solid fa-shield-halved"></i></div>
        </div>
        <content-violation-page></content-violation-page>
      </div>
    </div>`
  });
  studioApp.mount(mount);
})();
