(function () {
  'use strict';
  var mount = document.getElementById('page-content-studio');
  if (!mount || !window.Vue) return;

  var noteTypes = ['测评','种草','干货','引流','实拍','扣测'];
  var stylePreferences = [
    { name:'幽默打趣型', sub:'段子手｜夸张比喻，笑点落回卖点' },
    { name:'专业话术型', sub:'成分党／参数党｜数据、标准与机制解释' },
    { name:'素人感型', sub:'真实记录｜时间线、犹豫和可验证细节' },
    { name:'情绪共鸣型', sub:'故事先行｜场景痛点与情绪转折' },
    { name:'干货清单型', sub:'收藏向｜编号、公式与避坑清单' },
    { name:'闺蜜私聊型', sub:'亲密安利｜第二人称与使用小技巧' },
    { name:'高级冷淡型', sub:'审美输出｜短句、留白与材质工艺' },
    { name:'反焦虑型', sub:'理性劝退｜谁不该买与单次成本' },
    { name:'沉浸体验型', sub:'感官描写｜声音、触感、气味与温度' },
    { name:'冷静吐槽型', sub:'反向种草｜缺点前置与条件式推荐' }
  ];
  function post(path, payload, method) {
    method=method || 'POST';
    var options={method:method, credentials:'same-origin', headers:{'Content-Type':'application/json'}};
    if (method !== 'GET') options.body=JSON.stringify(payload || {});
    return fetch('/api/content-studio/' + path, options)
      .then(function (response) {
        if (response.status === 401) {
          var authError=new Error('登录已过期，请重新登录'); authError.apiError=true; authError.httpStatus=401; throw authError;
        }
        return response.json().then(function (body) {
          if (!response.ok || !body || body.code !== 0) {
            var apiError=new Error((body && body.msg) || ('请求失败（HTTP '+response.status+'）'));
            apiError.apiError=true; apiError.httpStatus=response.status; throw apiError;
          }
          return body.data;
        });
      })
      .catch(function (error) {
        if (!error.apiError) error.networkError=true;
        throw error;
      });
  }
  function textVal(x) { return Array.isArray(x) ? x.join('\n') : String(x || ''); }
  function inferContentType(raw) {
    var text = String(raw || '').toLowerCase();
    return /视频|口播|镜头|转写|短视频|抖音/.test(text) ? 'video' : 'image_text';
  }
  function contentTypeLabel(type) { return type === 'image_text' ? '图文' : '视频'; }
  function readImageFile(file) {
    return new Promise(function (resolve, reject) {
      if (!file || !/^image\//i.test(file.type || '')) { reject(new Error('只支持图片文件')); return; }
      var reader=new FileReader();
      reader.onerror=function () { reject(new Error('图片读取失败')); };
      reader.onload=function () {
        var img=new Image();
        img.onerror=function () { reject(new Error('图片解析失败')); };
        img.onload=function () {
          var maxSide=1800, scale=Math.min(1,maxSide/Math.max(img.naturalWidth || img.width,img.naturalHeight || img.height));
          var canvas=document.createElement('canvas'); canvas.width=Math.max(1,Math.round((img.naturalWidth || img.width)*scale)); canvas.height=Math.max(1,Math.round((img.naturalHeight || img.height)*scale));
          var ctx=canvas.getContext('2d'); ctx.fillStyle='#fff'; ctx.fillRect(0,0,canvas.width,canvas.height); ctx.drawImage(img,0,0,canvas.width,canvas.height);
          resolve({name:file.name,dataUrl:canvas.toDataURL('image/jpeg',.9),width:canvas.width,height:canvas.height});
        };
        img.src=reader.result;
      };
      reader.readAsDataURL(file);
    });
  }
  function demoImagePrompts(data) {
    var name=String(data.productName || '产品').trim();
    var category=String(data.category || '产品').trim();
    var selling=String(data.sellingPoints || '仅展示已确认的真实卖点').trim();
    var audience=String(data.audience || '目标用户').trim();
    var scene=String(data.scene || '真实日常使用场景').trim();
    var ref=data.reference && data.reference.breakdown ? data.reference.breakdown : {};
    var sourceImageCount=data.contentType === 'image_text' ? Math.max(0, Math.min(6, Number(ref.sourceImageCount || 0))) : 0;
    var basis=data.imitate ? '母本图片版式未提供；仅依据已拆解主题：'+String(ref.topic || '素材中的核心问题')+'，具体版式待补图核验' : '按“'+String(data.noteType || '种草')+'”模板组织信息型组图';
    var productRef=data.productImageCount ? '参考随附的自家产品图，准确保留外形、颜色、结构、Logo位置和比例' : '自家产品图尚未上传，先保留自家产品参考图槽位';
    var negative='不要单品详情图、商品主图、洗手台摆拍、刷头微距轮播、假榜单、虚构竞品、乱码中文、错误 Logo、水印、夸大效果';
    var common='竖版社交媒体测评种草信息图，统一浅色底、清楚的卡片网格和醒目信息层级，标题和短评留白给后期排版，不要求模型直接生成中文';
    var cards = [
      {slot:'第1张·核心结论信息卡',purpose:'先给读者一个可核验的核心结论或选择问题',layoutType:'核心结论信息卡',prompt:common+'。制作第1张图文信息卡：上方留出核心结论区，下方用两到三块等宽信息卡承载问题、已知依据和待核验项；不要默认做封面，不要做单品详情主图。自家产品只放在相关槽位。'+productRef,negativePrompt:negative,composition:'结论区25%，信息卡区65%，页脚核验备注区10%',productPlacement:'自家产品参考图放在与正文任务对应的产品槽位，不占满整页',aspectRatio:'3:4',textOverlay:'核心问题：'+category+'怎么选；自家产品卡：'+name+'；其他对象：待补资料；选择标准：按实际证据填写',copyBlocks:'核心问题、选择标准、产品槽位标题和待核验备注',basis:basis},
      {slot:'对比·信息卡',purpose:'用相同维度比较已知卖点与其他待核验方案',layoutType:'多产品对比卡片',prompt:common+'。纵向排列三张等高对比卡，每卡左侧预留产品抠图位置，右侧预留一句定位、卖点标签与两行短评位置。第一卡使用自家产品参考图，其余卡只保留灰色占位，不生成未知竞品外观。比较维度围绕“'+selling.slice(0,100)+'”，未给证据的结论留空。'+productRef,negativePrompt:negative,composition:'左侧产品图35%，右侧标题和短评65%，三卡连续阅读',productPlacement:'自家产品图仅放在第一张自家产品卡，保持原图外观比例',aspectRatio:'3:4',textOverlay:'自家产品卡：'+name+'；已核实卖点：'+selling.slice(0,90)+'；体验短评：待实测；其他产品信息：待补资料',copyBlocks:'产品名称、卖点标签、两行用户化短评和待实测标记',basis:basis},
      {slot:'收束·适用条件',purpose:'帮助读者判断是否适合自己并核对限制',layoutType:'适用边界双栏卡',prompt:common+'。制作一页“适合谁/还需核对什么”的双栏清单，左栏是'+name+'在'+scene+'中的场景示意并使用自家产品参考图，右栏是适用条件和待核验项文字区；不画虚假的使用效果或用户证言。'+productRef,negativePrompt:negative,composition:'左图右文，图片约40%，清单约60%',productPlacement:'自家产品参考图放左栏场景示意，保持原图外观比例',aspectRatio:'3:4',textOverlay:'适用对象：'+audience+'；已核实卖点：'+selling.slice(0,90)+'；仍需核验：适配、效果证据、竞品对照条件',copyBlocks:'适用人群、已核实卖点、待核验条件',basis:basis}
    ];
    if (sourceImageCount) {
      while (cards.length < sourceImageCount) {
        var extra=Object.assign({}, cards[(cards.length-1) % cards.length]);
        extra.slot='第'+(cards.length+1)+'张·补充信息卡';
        extra.purpose='承接母本第'+(cards.length+1)+'张作品图的信息任务，补充可核验细节';
        cards.push(extra);
      }
      return cards.slice(0, sourceImageCount);
    }
    return cards;
  }
  function demoAnalysis(raw, hasImages) {
    var title = raw.match(/(?:标题|题目)[:：]\s*([^\n]+)/);
    return { title:(title && title[1] ? title[1].trim() : '一套脚垫用了三个月，我终于找到清洁不返味的方法'), sourceUrl:'', contentType:inferContentType(raw), evidence:'演示拆解：未连接服务端 AI', breakdown:{
      topic:'围绕“长期使用后的真实变化”切入，面向正在比较材质、清洁成本和耐用性的车主。',
      structure:'前 3 秒抛出结果或反常识问题 → 展示使用场景 → 给出 2 至 3 个对比证据 → 总结适用人群并引导评论。',
      title:'使用时间 + 明确结果 + 具体场景；标题承诺可验证的体验，避免泛泛而谈。',
      shots:'近景展示污渍或细节 → 手部实拍安装/清洁 → 俯拍整体效果 → 对比镜头收尾。未提供视频时不判断真实镜头。',
      imageLayout:hasImages ? '已上传图文截图，但当前处于演示降级，未完成视觉识别；请重试服务端分析。' : '未提供图文截图，无法判断是榜单/梯队、对比卡片还是单品详情版式。',
      comments:'“你们更在意耐脏还是好清洁？”\n“想看哪种材质的对比，我补一条实拍。”',
      learn:'把抽象卖点换成可观察的使用结果；先给结论，再用连续的小证据降低理解成本。',
      risks:'不能把单次体验写成普遍结论；测试条件要说清；不要复制原文独特句式、画面编排或评论引导。'
    }};
  }
  function demoGeneration(data) {
    var n=data.productName, c=data.category, t=data.noteType || (data.contentType === 'image_text' ? '图文' : '视频'), s=data.sellingPoints, brand=data.brand, style=data.stylePreference;
    var common = { contentType:data.contentType || 'video', imagePromptMode:data.imitate ? 'imitate' : 'template', sourceImageCount:data.contentType === 'image_text' && data.reference && data.reference.breakdown ? Number(data.reference.breakdown.sourceImageCount || 0) : 0, topics:[n+'真实使用一周后的 3 个变化',c+'怎么选：材质、适配和清洁成本',t+'视角拆解 '+n+' 的一个关键卖点', '预算有限时，先看 '+n+' 的这项细节', n+'适合哪些人，哪些人不必买'],
      matrix:[{angle:'真实体验',format:'实拍',hook:'先展示使用后的结果，再回放关键细节'},{angle:'避坑对比',format:'测评',hook:'同一场景只比较一个变量'},{angle:'场景解决方案',format:'干货',hook:'从车主常见痛点给出选择顺序'}],
      titles:['用了 7 天，我终于知道 '+n+' 这个细节值不值','别只看价格：'+c+'先看这 3 个地方','真实体验｜'+n+'适合谁，哪些人可以跳过'],
      body:'最近在整理'+(brand ? '「'+brand+'」' : '')+' '+n+' 的使用体验，想把真实感受说清楚。先说结论：'+s+'。\n\n这次会用「'+style+'」的表达方式，按使用场景、清洁维护和适配细节逐项展示；文中只写已确认的信息，具体效果以实际测试为准。',
      comments:'1. 这个细节我之前还真没注意到，尤其是你提到的使用场景。\n2. 看完先记下了，回头我也想按这个标准对比一下。\n3. 这类产品最怕只说优点，能把限制讲清楚反而更有参考价值。\n4. 我是因为 '+(brand || '这个品牌')+' 的'+c+'进来的，想再看看长期使用后的变化。\n5. 如果后面有实拍或测试记录，想继续蹲一下。',
      checks:'发布前核对车型适配、价格和测试条件；删除无法证明的“全网最好”“绝对不返味”等表述；补齐实拍画面与必要的对比依据。'};
    if (data.contentType === 'image_text') {
      common.imagePlan='第 1 张｜结果/痛点对比，叠加一句结论\n第 2 张｜产品整体与使用场景，标注适用对象\n第 3 张｜材质、结构或细节特写，配合真实参数\n第 4 张｜前后变化或选择清单，写清待核验项';
      common.shooting=''; common.script='';
    } else {
      common.imagePlan='';
      common.shooting='0-3s：结果特写和一句结论；3-8s：展示安装或使用场景；8-15s：用近景呈现卖点“'+s.slice(0,40)+'”；15-22s：补充适用人群与注意事项。';
      common.script='【镜头 1｜0-3s】结果特写，口播：先看用了一段时间后的真实状态。\n【镜头 2｜3-8s】展示安装/清洁过程，口播：这里重点看 '+s.slice(0,32)+'。\n【镜头 3｜8-15s】拍细节和局部对比，口播：只描述看得见、测得到的变化。\n【镜头 4｜15-22s】正面总结，口播：适合……；如果你更在意……，请先核验。';
    }
    common.imagePrompts=demoImagePrompts(data);
    return common;
  }

  var FLOW_META = [
    ['投喂爆文素材','抖音链接、口播文案或镜头摘要'],
    ['爆文拆解','8 维结构化分析并落卡'],
    ['选择参考母本','可选：只借鉴方法，不照抄原句'],
    ['生成配置','品类 · 内容形态 · 品牌 · 风格 · 真实卖点'],
    ['AI 生成内容','选题 → 正文 → 生图提示词'],
    ['人工核验发布','核对适配、价格与测试条件']
  ];
  var BREAKDOWN_MAP = [
    ['选题与受众','topic'],['内容结构','structure'],['标题策略','title'],['镜头与节奏','shots'],['图文版式与信息任务','imageLayout'],
    ['评论区互动模板','comments'],['可借鉴点','learn'],['风险与验证','risks']
  ];

  var studioApp = Vue.createApp({
    components: { 'content-violation-page': window.ContentViolationPage },
    data: function () { return {
      workspaceTab:'creative',
      activeTab:'breakdown', input:'', analysisFocus:'', referenceImages:[], productImages:[], status:'', statusError:false, busy:false, cards:[], selectedId:null,
      category:'', categoryOptions:[], categoryOpen:false, noteType:'测评', brand:'', stylePreference:'素人感型', styleOpen:false, imitate:false, referenceId:null, productName:'', sellingPoints:'', audience:'', scene:'',
      output:null, productionBusy:false, productionStatus:'', productionError:false, aiDegraded:false, imitationOpen:false,
      productTerms:[], libraryOpen:false, libraryBusy:false, librarySync:{status:'idle',message:'',done:0,total:0}, newProductTerm:'',
      titleInput:'', titleOptimizeBusy:false, titleOptimizeStatus:'', titleOptimizeError:false, optimizedTitles:[], optimizedUsedWords:[],
      profileDone:false, cardsDone:false, categoriesDone:false,
      // 页面只挂载一次；账号切换时必须丢弃上个账号的内存状态。
      sessionAccount:sessionStorage.getItem('admin_current_account') || '', sessionEpoch:0,
      noteTypes:noteTypes, stylePreferences:stylePreferences,
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
          !!(this.category.trim() && this.productName.trim() && this.sellingPoints.trim()),
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
      self.sessionAccount=sessionStorage.getItem('admin_current_account') || '';
      self.loadProfile();
      self.loadKeywordLibrary();
      // 融合拆解区：右半是「成果详情」，必须有一张选中卡片才有内容。
      // 本地有历史卡片但 selectedId 为空（刷新/换页回来）时，默认选中最新一张，
      // 否则左列有卡、右边却是空态，看起来像坏了。
      if (!self.selectedId && self.cards.length) self.selectedId = self.cards[0].id;
      window.addEventListener('content-studio-tab', function (e) {
        if (e && e.detail === 'violation') self.workspaceTab = 'violation';
      });
      window.addEventListener('admin-session-changed', function () {
        self.ensureSessionContext();
        if (!mount.classList.contains('hidden')) {
          self.loadProfile(); self.loadCards(); self.loadCategories(); self.loadKeywordLibrary();
        }
      });
      // 本页在「登录之前」就已挂载（脚本首屏执行），那一刻 /api/profile/me 还是 401，
      // 之后再不会自动补取 → 用户卡会一直停在兜底文案「当前用户 / 团队成员」。
      // 所以每次页面被切到前台时补取一次（成功后不再重复请求）。
      try {
        var obs = new MutationObserver(function () {
           if (!mount.classList.contains('hidden')) {
             self.ensureSessionContext();
             if (!self.profileDone) self.loadProfile();
             if (!self.cardsDone) self.loadCards();
             if (!self.categoriesDone) self.loadCategories();
             if (!self.productTerms.length) self.loadKeywordLibrary();
           }
        });
        obs.observe(mount, { attributes:true, attributeFilter:['class', 'style'] });
      } catch (e) {}
      window.addEventListener('hashchange', function () { self.ensureSessionContext(); if (!self.profileDone) self.loadProfile(); if (!self.cardsDone) self.loadCards(); if (!self.categoriesDone) self.loadCategories(); });
      window.addEventListener('click', function () { self.styleOpen=false; self.categoryOpen=false; });
    },
    methods: {
      // 登录/退出不会销毁本页实例，因此以当前账号作为数据状态边界。
      ensureSessionContext:function () {
        var account=sessionStorage.getItem('admin_current_account') || '';
        if (account === this.sessionAccount) return false;
        this.sessionAccount=account;
        this.sessionEpoch += 1;
        this.profileDone=false; this.cardsDone=false; this.categoriesDone=false;
        this.cards=[]; this.selectedId=null; this.referenceId=null; this.referenceImages=[]; this.productImages=[];
        this.categoryOptions=[]; this.output=null; this.avatar='';
        this.status=''; this.statusError=false; this.aiDegraded=false;
        this.userName=sessionStorage.getItem('admin_current_user') || '当前用户';
        this.userRole=sessionStorage.getItem('admin_current_role') || '团队成员';
        return true;
      },
      loadProfile:function () {
        var self=this;
        self.ensureSessionContext();
        if (self.profileDone) return;
        var account=self.sessionAccount, epoch=self.sessionEpoch;
        fetch('/api/profile/me', {credentials:'same-origin'}).then(function (r) { return r.json(); }).then(function (r) {
          if (account !== self.sessionAccount || epoch !== self.sessionEpoch) return;
          if (!r || r.code !== 0 || !r.data) return;
          self.userName=r.data.name || self.userName;
          self.userRole=r.data.role || self.userRole;
          self.avatar=r.data.avatar || '';
           self.profileDone=true;
           self.loadCards(); self.loadCategories();
        }).catch(function () {});
      },
      navigate:function (page) { window.location.hash=page; if (window.App && App.navigateTo) App.navigateTo(page); },
      loadCards:async function () {
        this.ensureSessionContext();
        if (this.cardsDone) return;
        var account=this.sessionAccount, epoch=this.sessionEpoch;
        try {
          var cards=await post('cards', null, 'GET');
          if (account !== this.sessionAccount || epoch !== this.sessionEpoch) return;
          this.cards=Array.isArray(cards) ? cards : [];
          // 从旧版本浏览器缓存做一次性迁移；迁移成功后不再把 localStorage 当作正式数据源。
          if (!this.cards.length) {
            var legacyKey='content_studio_cards_' + (sessionStorage.getItem('admin_current_account') || 'local');
            var legacy=[];
            try { var rawLegacy=JSON.parse(localStorage.getItem(legacyKey) || '[]'); legacy=Array.isArray(rawLegacy) ? rawLegacy.slice(0,50) : []; } catch (ignore) {}
            if (legacy.length) {
              var migrated=[];
              for (var i=legacy.length-1;i>=0;i--) {
                try { migrated.unshift(await post('cards', legacy[i])); } catch (migrationError) { migrated=[]; break; }
              }
              if (migrated.length === legacy.length) {
                this.cards=migrated; try { localStorage.removeItem(legacyKey); } catch (ignoreRemove) {}
              }
            }
          }
          if (account !== this.sessionAccount || epoch !== this.sessionEpoch) return;
          if (!this.selectedId && this.cards.length) this.selectedId=this.cards[0].id;
          this.cardsDone=true;
        } catch (e) {
          if (e.httpStatus !== 401) { this.status=e.message || '拆解卡片读取失败'; this.statusError=true; }
        }
      },
      loadCategories:async function () {
        this.ensureSessionContext();
        if (this.categoriesDone) return;
        var account=this.sessionAccount, epoch=this.sessionEpoch;
        try {
          var categories=await post('categories', null, 'GET');
          if (account !== this.sessionAccount || epoch !== this.sessionEpoch) return;
          this.categoryOptions=Array.isArray(categories) ? categories : [];
          this.categoriesDone=true;
        } catch (e) { if (e.httpStatus !== 401) this.categoryOptions=[]; }
      },
      loadKeywordLibrary:async function () {
        try {
          var d=await post('keyword-library', null, 'GET');
          this.productTerms=Array.isArray(d && d.terms) ? d.terms : [];
          this.librarySync=(d && d.sync) || this.librarySync;
        } catch (e) { if (e.httpStatus !== 401) this.librarySync={status:'error',message:e.message || '词库读取失败'}; }
      },
      toggleKeywordLibrary:function () { this.libraryOpen=!this.libraryOpen; if (this.libraryOpen) this.loadKeywordLibrary(); },
      addProductTerm:async function () {
        var term=this.newProductTerm.trim();
        if (!term) return;
        this.libraryBusy=true;
        try { var d=await post('keyword-library',{term:term}); this.productTerms=(d && d.term) || this.productTerms; this.newProductTerm=''; this.librarySync={status:'idle',message:'已加入词库'}; }
        catch (e) { this.librarySync={status:'error',message:e.message || '添加失败'}; }
        finally { this.libraryBusy=false; }
      },
      removeProductTerm:async function (item) {
        if (!item || !item.id) return;
        try { this.productTerms=await post('keyword-library/'+encodeURIComponent(item.id),null,'DELETE'); }
        catch (e) { this.librarySync={status:'error',message:e.message || '删除失败'}; }
      },
      syncKeywordLibrary:async function () {
        this.libraryBusy=true;
        try { await post('keyword-library/sync',{}); this.librarySync={status:'running',message:'正在采集爱搜下拉词'}; }
        catch (e) { this.librarySync={status:'error',message:e.message || '采集启动失败'}; }
        finally { this.libraryBusy=false; }
      },
      optimizeTitle:async function () {
        var title=this.titleInput.trim();
        if (!title) { this.titleOptimizeStatus='请先输入需要优化的标题'; this.titleOptimizeError=true; return; }
        var product=(this.productName || this.category || '').trim();
        if (!product) { this.titleOptimizeStatus='请先在爆文生产配置中填写产品名称或品类'; this.titleOptimizeError=true; return; }
        this.titleOptimizeBusy=true; this.titleOptimizeError=false; this.titleOptimizeStatus='正在匹配词库并优化标题…';
        try {
          var d=await post('optimize-title',{title:title,productName:product,category:this.category,noteType:this.noteType});
          this.optimizedTitles=(d && d.titles) || []; this.optimizedUsedWords=(d && d.usedWords) || [];
          this.titleOptimizeStatus=(d && d.knowledgeReady) ? '已融合爱搜高热词，请人工核验语义与事实' : '词库暂未有匹配数据，已生成保守版本';
        } catch (e) { this.titleOptimizeStatus=e.message || '标题优化失败'; this.titleOptimizeError=true; }
        finally { this.titleOptimizeBusy=false; }
      },
      selectCategory:function (category) { this.category=category; this.categoryOpen=false; },
      toggleCategory:function () { this.categoryOpen=!this.categoryOpen; },
      selectCard:function (id) { this.selectedId=id; },
      selectStyle:function (name) { this.stylePreference=name; this.styleOpen=false; },
      typeLabel:function (card) { return contentTypeLabel(card && card.contentType); },
      typeIcon:function (card) { return card && card.contentType === 'image_text' ? 'fa-images' : 'fa-video'; },
      draftBadge:function (card) {
        var keys = (card && card.breakdown) ? Object.keys(card.breakdown) : [];
        if (!keys.length) return { cls:'amber', text:'待核验' };
        if (this.referenceId === card.id) return { cls:'pink', text:'参考中' };
        return { cls:'mint', text:'已拆解' };
      },
      goProduction:function (card) {
        if (!card) return;
        this.referenceId=card.id; this.imitate=true; this.imitationOpen=true; this.productionStatus=''; this.productionError=false;
      },
      closeImitation:function () { if (!this.productionBusy) this.imitationOpen=false; },
      clearInput:function () { this.input=''; this.analysisFocus=''; this.referenceImages=[]; },
      addReferenceImages:async function (files) {
        var self=this, incoming=Array.prototype.slice.call(files || []).filter(function (file) { return /^image\//i.test(file.type || ''); }).slice(0,6-self.referenceImages.length);
        for (var i=0;i<incoming.length;i++) { try { self.referenceImages.push(await readImageFile(incoming[i])); } catch (e) { self.status=e.message || '图文截图读取失败'; self.statusError=true; } }
      },
      removeReferenceImage:function (index) { this.referenceImages.splice(index,1); },
      addProductImages:async function (files) {
        var self=this, incoming=Array.prototype.slice.call(files || []).filter(function (file) { return /^image\//i.test(file.type || ''); }).slice(0,4-self.productImages.length);
        for (var i=0;i<incoming.length;i++) { try { self.productImages.push(await readImageFile(incoming[i])); } catch (e) { self.productionStatus=e.message || '产品图读取失败'; self.productionError=true; } }
      },
      removeProductImage:function (index) { this.productImages.splice(index,1); },
      removeCard:async function (card) {
        var target=card || this.selectedCard;
        if (!target || !target.id) return;
        try {
          await post('cards/' + encodeURIComponent(target.id), null, 'DELETE');
          this.cards=this.cards.filter(function (x) { return x.id !== target.id; });
          if (this.referenceId === target.id) this.referenceId=null;
          this.selectedId=this.cards.length ? this.cards[0].id : null;
          this.status='拆解卡片已删除'; this.statusError=false;
        } catch (e) { this.status=e.message || '拆解卡片删除失败'; this.statusError=true; }
      },
      analyze:async function () {
        var raw=this.input.trim();
        var focus=this.analysisFocus.trim();
        if (!raw && !this.referenceImages.length) { this.status='请粘贴素材或上传母本图文截图'; this.statusError=true; return; }
        this.busy=true; this.status='正在读取素材并生成拆解卡…'; this.statusError=false;
        try {
          var d;
          try { d=await post('analyze',{input:raw,focus:focus,images:this.referenceImages.map(function (item) { return item.dataUrl; })}); }
          catch (apiError) {
            if (apiError.apiError) throw apiError;
            d=demoAnalysis(raw, this.referenceImages.length > 0); this.aiDegraded=true;
            this.status='服务端暂时无法连接，已生成演示拆解卡；请检查后端服务或网络后重试';
          }
          var detectedImageCount=Number(d.sourceImageCount || (d.breakdown && d.breakdown.sourceImageCount) || this.referenceImages.length || 0);
          var cardBreakdown=Object.assign({}, d.breakdown || {}, {sourceImageCount:detectedImageCount});
          var card={input:raw || ('用户上传图文截图（'+this.referenceImages.length+'张）'),
            title:d.title || '未命名爆文', sourceUrl:d.sourceUrl || '', contentType:d.contentType || inferContentType(raw), evidence:d.evidence || '', breakdown:cardBreakdown};
          var savedCard=await post('cards', card);
          this.cards.unshift(savedCard); this.cards=this.cards.slice(0,50); this.selectedId=savedCard.id;
          var knowledgeStatus=savedCard.knowledgeStatus || d.knowledgeStatus;
          if (!this.status || this.status.indexOf('演示') < 0) {
            this.status=knowledgeStatus === 'saved' ? '拆解完成，个人卡片已保存，方法已纳入共享知识库' :
              knowledgeStatus === 'exists' ? '拆解完成，个人卡片已保存；该素材已在共享知识库中' :
              '拆解完成，个人卡片已保存；共享知识库写入失败，请稍后重试';
          }
          this.statusError=false; this.input=''; this.analysisFocus=''; this.referenceImages=[];
        } catch (e) { this.status=e.message || '拆解失败'; this.statusError=true; }
        finally { this.busy=false; }
      },
      generate:async function () {
        if (!this.category.trim()) {
          this.productionStatus='请填写产品品类，便于 AI 准确理解内容方向'; this.productionError=true; return;
        }
        if (!this.productName.trim() || !this.sellingPoints.trim()) {
          this.productionStatus='请填写产品名称与真实卖点，避免 AI 编造产品信息'; this.productionError=true; return;
        }
        if (this.imitate && !this.brand.trim()) {
          this.productionStatus='一键仿写需要填写品牌，便于 AI 区分产品资料'; this.productionError=true; return;
        }
        if (this.imitate && !this.referenceCard) { this.productionStatus='开启仿写前，请选择一张已拆解的爆文卡片'; this.productionError=true; return; }
        var contentType = this.imitate && this.referenceCard ? (this.referenceCard.contentType || 'video') : '';
        if (this.imitate) { this.imitationOpen=false; this.activeTab='production'; }
        this.productionBusy=true; this.productionStatus=this.imitate ? '正在按参考母本仿写，请稍候…' : '正在根据笔记类型与风格偏好原创，请稍候…'; this.productionError=false;
        try {
          var payload={category:this.category.trim(),noteType:this.noteType,contentType:contentType,brand:this.brand.trim(),stylePreference:this.stylePreference,productName:this.productName.trim(),sellingPoints:this.sellingPoints.trim(),audience:this.audience.trim(),scene:this.scene.trim(),imitate:this.imitate,productImageCount:this.productImages.length,productImageNames:this.productImages.map(function (item) { return item.name; }),reference:this.imitate && this.referenceCard ? {title:this.referenceCard.title,contentType:contentType,breakdown:this.referenceCard.breakdown} : null};
          try { this.output=await post('generate',payload); this.productionStatus='已生成，可逐段复制并进行人工审核'; }
          catch (apiError) {
            if (apiError.apiError) throw apiError;
            this.output=demoGeneration(payload); this.aiDegraded=true;
            this.productionStatus='服务端暂时无法连接，已生成演示内容；请检查后端服务或网络后重试';
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
      imagePromptText:function (item) {
        if (!item) return '';
        return ['【'+(item.slot || '配图')+'】', '用途：'+(item.purpose || ''), '版式：'+(item.layoutType || '信息型图文卡片'), '提示词：'+(item.prompt || ''), '自家产品图位置：'+(item.productPlacement || '按提示词指定槽位放置'), '负面提示词：'+(item.negativePrompt || ''), '构图：'+(item.composition || ''), '比例：'+(item.aspectRatio || '3:4'), '后期文字：'+(item.textOverlay || ''), '文案区块：'+(item.copyBlocks || ''), '依据：'+(item.basis || '')].join('\n');
      },
      allImagePromptText:function () {
        var list=this.output && this.output.imagePrompts || [];
        var note=this.productImages.length ? '【参考图附件】请将本页已上传的 '+this.productImages.length+' 张自家产品图与提示词一并提交；保持外观、颜色、结构、Logo位置和比例一致。' : '【参考图附件】请先上传自家产品图，再与提示词一并提交；当前提示词只保留产品图槽位。';
        return note+'\n\n'+list.map(this.imagePromptText).join('\n\n');
      },
      copyImagePrompt:function (item) {
        var note=this.productImages.length ? '【参考图附件】请将本页已上传的 '+this.productImages.length+' 张自家产品图与提示词一并提交；保持产品外观、颜色、结构、Logo位置和比例一致。' : '【参考图附件】请先上传自家产品图，再与提示词一并提交。';
        this.copy(note+'\n\n'+this.imagePromptText(item));
      },
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
                  <div class="cs-user-tag"><span class="cs-badge mint"><i class="fa-solid fa-cloud"></i> 草稿账号同步</span></div>
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
                <div class="grow"><div class="cs-title">拆解卡片</div><div class="cs-sub">当前账号服务端保留最近 50 张</div></div>
                <span class="cs-badge">{{ cards.length }}</span>
              </div>
              <div v-if="cards.length" class="cs-list scroll">
                <div v-for="card in cards" :key="card.id" class="cs-draft" :class="{on:selectedId===card.id}" role="button" tabindex="0" @click="selectCard(card.id)">
                   <span class="copy"><span class="nm">{{ card.title }}</span><span class="mt">{{ card.createdAt }}</span></span>
                   <span class="cs-badge" :class="draftBadge(card).cls" style="flex:none">{{ draftBadge(card).text }}</span>
                   <button type="button" class="cs-card-delete" title="删除拆解卡片" aria-label="删除拆解卡片" @click.stop="removeCard(card)"><i class="fa-solid fa-trash-can"></i></button>
                </div>
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
                  <div class="cs-stat"><b>8</b><span>拆解维度</span></div>
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
                      <textarea id="csInput" v-model="input" class="cs-ta" maxlength="20000" placeholder="粘贴抖音链接、文案或镜头摘要；图文链接会自动按作品图片顺序识别，链接无法读取图片时再上传母本截图。"></textarea>
                      <div class="cs-upload-row">
                        <label class="cs-upload-btn"><i class="fa-solid fa-images"></i> 上传母本图文截图 <input type="file" accept="image/*" multiple hidden @change="addReferenceImages($event.target.files); $event.target.value=''" /></label>
                        <span class="cs-upload-hint">链接图片无法读取时使用，最多 6 张，按上传顺序对应作品页</span>
                      </div>
                      <div v-if="referenceImages.length" class="cs-upload-previews">
                        <div v-for="(item,i) in referenceImages" :key="item.name+i" class="cs-upload-preview"><img :src="item.dataUrl" :alt="item.name"><button type="button" @click="removeReferenceImage(i)" aria-label="移除截图"><i class="fa-solid fa-xmark"></i></button></div>
                      </div>
                      <div class="cs-count"><span class="cache"><i class="fa-solid fa-cloud"></i> 当前账号自动保存</span><span>{{ input.length }} / 20000</span></div>
                    </div>
                    <div class="cs-field">
                      <label class="cs-label" for="csFocus">分析重点 <em>可选，留空则全维度拆解</em></label>
                      <textarea id="csFocus" v-model="analysisFocus" class="cs-ta" maxlength="1000" placeholder="例如：重点分析标题钩子、内容结构与镜头节奏；忽略价格与无法验证的效果承诺。"></textarea>
                      <div class="cs-count"><span>建议只写真正关心的维度，避免拆解跑偏</span><span>{{ analysisFocus.length }} / 1000</span></div>
                    </div>
                  </div>
                  <div class="cs-row-actions">
                    <span class="cs-hint">图文链接会按第 1 页到第 N 页生成对应提示词；文字可补充标题、卖点和评论信息</span>
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
                    <small>左列选卡 → 右侧即时查看 8 维分析；把图文版式、选题、结构、镜头和评论话术转成可复用的方法</small>
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
                       <div v-for="card in cards" :key="card.id" class="cs-pick" :class="{on:selectedId===card.id}" role="button" tabindex="0" @click="selectCard(card.id)">
                         <span class="tile"><i class="fa-solid" :class="typeIcon(card)"></i></span>
                         <span class="body">
                           <span class="h">{{ card.title }}</span>
                           <span class="m"><b class="cs-type-tag" :class="card.contentType === 'image_text' ? 'image' : 'video'">{{ typeLabel(card) }}</b>{{ card.createdAt }} · {{ card.evidence || '用户提供素材' }}</span>
                         </span>
                         <button type="button" class="cs-card-delete" title="删除拆解卡片" aria-label="删除拆解卡片" @click.stop="removeCard(card)"><i class="fa-solid fa-trash-can"></i></button>
                       </div>
                    </div>
                    <div v-else class="cs-empty"><i class="fa-solid fa-inbox"></i>还没有拆解卡。<br>先在上方投喂一条素材开始。</div>
                  </div>

                  <!-- 右半：成果详情 -->
                  <div class="cs-fuse-result">
                    <template v-if="selectedCard">
                      <div class="cs-fuse-result-head">
                        <div class="cs-chip mint" style="flex:none"><i class="fa-solid fa-bullseye"></i></div>
                        <div class="grow">
                          <div class="ttl">拆解成果 · 8 维</div>
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
                        <div class="cs-blk"><h4><i class="fa-solid fa-table-cells-large"></i> 图文版式与信息任务</h4><p>{{ display(selectedCard.breakdown.imageLayout) }}</p></div>
                        <div class="cs-blk"><h4><i class="fa-solid fa-comments"></i> 评论区互动模板</h4><p>{{ display(selectedCard.breakdown.comments) }}</p></div>
                        <div class="cs-grid2">
                          <div class="cs-blk mint"><h4><i class="fa-solid fa-lightbulb"></i> 可借鉴点</h4><p>{{ display(selectedCard.breakdown.learn) }}</p></div>
                          <div class="cs-blk pink"><h4><i class="fa-solid fa-triangle-exclamation"></i> 风险与验证</h4><p>{{ display(selectedCard.breakdown.risks) }}</p></div>
                        </div>
                        <div class="cs-blk"><h4><i class="fa-solid fa-circle-info"></i> 分析依据</h4><p>{{ selectedCard.evidence || '用户提供素材' }}</p></div>
                      </div>
                      <div class="cs-fuse-foot">
                        <button type="button" class="cs-btn mint wide" @click="goProduction(selectedCard)"><i class="fa-solid fa-wand-magic-sparkles"></i> 一键仿写</button>
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
                    <p>{{ imitate ? '已识别原爆文的图文/视频形态，补充产品资料后生成对应仿写结果。' : '配置品类、笔记类型与产品信息，需要时挂上一张拆解卡片做结构参考，一次生成可直接使用的产出。' }}</p>
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
                  <div class="cs-block-label">01 · 输入品类 <em>必填</em></div>
                    <div class="cs-category-picker" :class="{open:categoryOpen,filled:!!category.trim()}" @click.stop>
                      <button type="button" class="cs-category-trigger" :aria-expanded="categoryOpen" aria-haspopup="listbox" @click="toggleCategory">
                        <span class="cs-category-icon"><i class="fa-solid fa-shapes"></i></span>
                        <span class="cs-category-body"><small>产品所属品类</small><b>{{ category || '请选择品类' }}</b></span>
                        <i class="cs-category-chevron fa-solid fa-chevron-down"></i>
                      </button>
                      <div v-if="categoryOpen" class="cs-category-menu" role="listbox" aria-label="产品所属品类">
                        <button v-for="item in categoryOptions" :key="item" type="button" role="option" class="cs-category-option" :class="{on:category===item}" :aria-selected="category===item" @click="selectCategory(item)"><span>{{ item }}</span><i v-if="category===item" class="fa-solid fa-check"></i></button>
                        <div v-if="!categoryOptions.length" class="cs-category-empty">暂无品类数据，请先在品类营销数据中完成品类映射。</div>
                      </div>
                      <button v-if="category" type="button" class="cs-category-clear" aria-label="清空品类" title="清空品类" @click="category=''">
                        <i class="fa-solid fa-xmark"></i>
                      </button>
                    </div>
                    <div class="cs-category-hint"><i class="fa-solid fa-circle-info"></i> 品类来自“品类营销数据”的统一品类列表，避免同一品类多种写法。</div>
                  </div>
                  <div v-if="!imitate" class="cs-divider">
                    <div class="cs-block-label">02 · 笔记类型</div>
                    <div class="cs-chips">
                      <button v-for="type in noteTypes" :key="type" type="button" class="cs-ch" :class="{on:noteType===type}" @click="noteType=type">{{ type }}</button>
                    </div>
                    <div class="cs-hint" style="margin-top:9px">「扣测」按对比测试处理，仍需提供真实测试依据。</div>
                  </div>
                  <div v-else class="cs-divider cs-recognized-type">
                    <div class="cs-block-label">02 · 已识别内容形态</div>
                    <div class="cs-recognized-type-value"><i class="fa-solid" :class="referenceCard && referenceCard.contentType === 'image_text' ? 'fa-images' : 'fa-video'"></i><b>{{ referenceCard && referenceCard.contentType === 'image_text' ? '图文' : '视频' }}</b><span>仿写将按原爆文的内容形态生成对应结果</span></div>
                  </div>
                  <div class="cs-divider">
                    <div class="cs-block-label">03 · 产品信息 <em>必填项用 * 标记</em></div>
                    <div class="cs-grid2">
                      <div class="cs-field">
                        <input v-model="productName" class="cs-ta line" maxlength="120" placeholder="产品名称 *">
                        <input v-model="brand" class="cs-ta line" style="margin-top:8px" maxlength="80" placeholder="品牌（可选）">
                        <textarea v-model="sellingPoints" class="cs-ta" style="min-height:122px;margin-top:8px" maxlength="3000" placeholder="真实卖点 / 参数 / 测试结论 *"></textarea>
                        <div class="cs-product-upload">
                          <div class="cs-product-upload-head"><span><i class="fa-solid fa-camera"></i> 自家产品图 <em>建议上传</em></span><small>会作为参考图随提示词使用</small></div>
                          <label class="cs-upload-btn"><i class="fa-solid fa-plus"></i> 添加产品图 <input type="file" accept="image/*" multiple hidden @change="addProductImages($event.target.files); $event.target.value=''" /></label>
                          <div v-if="productImages.length" class="cs-upload-previews"><div v-for="(item,i) in productImages" :key="item.name+i" class="cs-upload-preview"><img :src="item.dataUrl" :alt="item.name"><button type="button" @click="removeProductImage(i)" aria-label="移除产品图"><i class="fa-solid fa-xmark"></i></button></div></div>
                        </div>
                      </div>
                      <div class="cs-field">
                        <div class="cs-style-select" :class="{open:styleOpen}" @click.stop>
                          <button type="button" class="cs-style-trigger" :aria-expanded="styleOpen" aria-haspopup="listbox" @click="styleOpen=!styleOpen">
                            <span class="cs-style-trigger-icon"><i class="fa-solid fa-wand-magic-sparkles"></i></span>
                            <span class="cs-style-trigger-copy"><small>风格偏好</small><b>{{ stylePreference }}</b></span>
                            <i class="cs-style-chevron fa-solid fa-chevron-down"></i>
                          </button>
                          <div v-if="styleOpen" class="cs-style-menu" role="listbox" aria-label="风格偏好">
                            <button v-for="(style,i) in stylePreferences" :key="style.name" type="button" role="option" class="cs-style-option" :class="{on:stylePreference===style.name}" :aria-selected="stylePreference===style.name" @click="selectStyle(style.name)">
                              <span class="cs-style-index">{{ i + 1 }}</span><span class="cs-style-option-copy"><b>{{ style.name }}</b><small>{{ style.sub }}</small></span><i v-if="stylePreference===style.name" class="fa-solid fa-check"></i>
                            </button>
                          </div>
                        </div>
                        <input v-model="audience" class="cs-ta line" maxlength="300" placeholder="目标人群（可选）">
                        <input v-model="scene" class="cs-ta line" style="margin-top:8px" maxlength="300" placeholder="使用场景（可选）">
                      </div>
                    </div>
                  </div>
                  <div class="cs-status" :class="{show:!!productionStatus, error:productionError}">{{ productionStatus }}</div>
                </div>
              </div>

              <div class="cs-card">
                <div class="cs-head">
                  <div class="cs-chip mint"><i class="fa-solid fa-sparkles"></i></div>
                  <div class="grow"><div class="cs-title">AI 生成结果</div><div class="cs-sub">{{ output && output.contentType === 'image_text' ? '图文配图与正文一套交付' : '从选题到拍摄与评论区，一套内容完整交付' }}</div></div>
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
                   <div v-if="output.contentType === 'image_text'" class="cs-out-sec">
                     <div class="cs-out-head"><span class="n"><i class="fa-solid fa-images"></i> 配图方案</span><button type="button" class="cs-copy" @click="copy(output.imagePlan)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                     <div class="cs-out-body">{{ display(output.imagePlan) }}</div>
                   </div>
                   <div v-if="output.contentType !== 'image_text'" class="cs-out-sec">
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
                    <div class="cs-out-head"><span class="n"><i class="fa-solid fa-comments"></i> 原作品评论文案</span><button type="button" class="cs-copy" @click="copy(output.comments)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-out-body">{{ display(output.comments) }}</div>
                  </div>
                   <div v-if="output.contentType !== 'image_text'" class="cs-out-sec">
                     <div class="cs-out-head"><span class="n"><i class="fa-solid fa-film"></i> 短视频脚本</span><button type="button" class="cs-copy" @click="copy(output.script)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-out-body">{{ display(output.script) }}</div>
                  </div>
                  <div class="cs-out-sec">
                    <div class="cs-out-head"><span class="n"><i class="fa-solid fa-triangle-exclamation"></i> 发布前核验</span><button type="button" class="cs-copy" @click="copy(output.checks)"><i class="fa-solid fa-copy"></i> 复制</button></div>
                    <div class="cs-out-body">{{ display(output.checks) }}</div>
                  </div>
                </div>
                <div v-else class="cs-empty"><i class="fa-solid fa-wand-magic-sparkles"></i>填好配置后点击「AI 生成内容」，这里会依次展示内容产出和生图提示词。</div>
              </div>
            </template>
          </main>

          <!-- ==================== 右栏：AI 服务状态 ==================== -->
          <aside class="cs-rail cs-rail-right">
            <div class="cs-card cs-title-optimizer-card">
              <div class="cs-head">
                <div class="cs-chip"><i class="fa-solid fa-heading"></i></div>
                <div class="grow"><div class="cs-title">标题优化</div><div class="cs-sub">匹配爱搜热词，生成可审核标题</div></div>
                <button type="button" class="cs-btn cs-library-btn" @click="toggleKeywordLibrary"><i class="fa-solid fa-book-open"></i> 词库 <span>{{ productTerms.length }}</span></button>
              </div>
              <div class="cs-pad">
                <textarea v-model="titleInput" class="cs-title-input" maxlength="300" placeholder="粘贴已有标题，系统会按当前产品模糊匹配高热词"></textarea>
                <div class="cs-title-actions"><span class="cs-hint">{{ productName || category || '先填写产品名称' }}</span><button type="button" class="cs-btn primary" :disabled="titleOptimizeBusy" @click="optimizeTitle"><i class="fa-solid" :class="titleOptimizeBusy?'fa-spinner':'fa-wand-magic-sparkles'"></i> {{ titleOptimizeBusy ? '优化中' : '优化标题' }}</button></div>
                <div v-if="titleOptimizeStatus" class="cs-title-status" :class="{error:titleOptimizeError}">{{ titleOptimizeStatus }}</div>
                <div v-if="optimizedTitles.length" class="cs-title-results">
                  <div class="cs-title-result" v-for="(item,i) in optimizedTitles" :key="i"><span>{{ item }}</span><button type="button" class="cs-copy" @click="copy(item)"><i class="fa-solid fa-copy"></i></button></div>
                  <div v-if="optimizedUsedWords.length" class="cs-hot-word-row"><i class="fa-solid fa-fire"></i><span v-for="word in optimizedUsedWords" :key="word">{{ word }}</span></div>
                </div>
              </div>
            </div>

            <div class="cs-card cs-image-prompts-card">
              <div class="cs-head">
                <div class="cs-chip pink"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
                <div class="grow"><div class="cs-title">AI 生图提示词</div><div class="cs-sub">随内容产出生成，可直接复制到生图模型</div></div>
                <span class="cs-badge" :class="output && output.imagePrompts && output.imagePrompts.length ? 'pink' : 'gray'">{{ output && output.imagePrompts && output.imagePrompts.length ? '已生成' : '等待内容' }}</span>
              </div>
              <div v-if="output && output.imagePrompts && output.imagePrompts.length" class="cs-pad cs-image-prompts">
                <div class="cs-image-prompt-meta">
                  <span><i class="fa-solid fa-route"></i>{{ output.imagePromptMode === 'imitate' ? '仿写母本：按原作品图序对应' : '原创模板：按笔记类型组织图文版式' }}<small v-if="output.sourceImageCount"> · 按母本 {{ output.sourceImageCount }} 张作品图生成</small><small v-if="productImages.length"> · 已附 {{ productImages.length }} 张自家产品图</small></span>
                  <button type="button" class="cs-copy" @click="copy(allImagePromptText())"><i class="fa-solid fa-copy"></i> 全部复制</button>
                </div>
                <div class="cs-image-prompt-list">
                  <div v-for="(item,i) in output.imagePrompts" :key="i" class="cs-image-prompt-item">
                    <div class="cs-image-prompt-head">
                      <div><b>{{ item.slot || ('配图 '+(i+1)) }}</b><small>{{ item.aspectRatio || '3:4' }} · {{ item.purpose || '补充正文信息' }}</small></div>
                      <button type="button" class="cs-copy" @click="copyImagePrompt(item)"><i class="fa-solid fa-copy"></i> 复制</button>
                    </div>
                    <div class="cs-image-prompt-text">{{ item.prompt }}</div>
                    <details class="cs-image-prompt-details">
                      <summary>查看构图、负面词与后期文字</summary>
                      <div class="cs-image-prompt-detail"><b>版式</b><span>{{ item.layoutType || '信息型图文卡片' }}</span></div>
                      <div class="cs-image-prompt-detail"><b>构图</b><span>{{ item.composition || '按主体任务安排构图' }}</span></div>
                      <div class="cs-image-prompt-detail"><b>产品图</b><span>{{ item.productPlacement || '按提示词指定槽位放置' }}</span></div>
                      <div class="cs-image-prompt-detail"><b>负面提示词</b><span>{{ item.negativePrompt || '无' }}</span></div>
                      <div class="cs-image-prompt-detail"><b>后期文字</b><span>{{ item.textOverlay || '后期排版，不要求模型生成文字' }}</span></div>
                      <div class="cs-image-prompt-detail"><b>文案区块</b><span>{{ item.copyBlocks || '标题、标签和短评后期排版' }}</span></div>
                      <div v-if="item.basis" class="cs-image-prompt-detail"><b>生成依据</b><span>{{ item.basis }}</span></div>
                    </details>
                  </div>
                </div>
                <div class="cs-image-prompt-hint"><i class="fa-solid fa-circle-info"></i> 生成时请把已上传的自家产品图一并作为参考图；准确中文、排名和短评建议后期排版，并核对所有测评依据。</div>
              </div>
              <div v-else class="cs-empty cs-image-prompts-empty"><i class="fa-solid fa-image"></i>先完成一次内容生成，这里会按爆文版式生成榜单、对比卡或信息页提示词。</div>
            </div>

            <div class="cs-card">
              <div class="cs-head">
                <div class="cs-chip mint"><i class="fa-solid fa-circle-info"></i></div>
                <div class="grow"><div class="cs-title">状态与错误提示</div><div class="cs-sub">降级、限流与失败原因集中在此</div></div>
                <span class="cs-badge" :class="statusClass">{{ statusText }}</span>
              </div>
              <div class="cs-state" :class="aiDegraded?'':'mint'">
                <div class="cs-state-line"><i class="fa-solid fa-check"></i> {{ cards.length ? '已拆解 ' + cards.length + ' 张卡片，当前账号保留最近 50 张' : '还没有拆解卡片，先从左侧投喂素材' }}</div>
                <div class="cs-state-line"><i class="fa-solid fa-check"></i> {{ output ? '已生成内容与生图提示词，可逐段复制并人工核验' : '生成结果会逐段展示，可单独复制' }}</div>
              </div>
              <div class="cs-state">
                <div class="cs-state-line amber"><i class="fa-solid fa-triangle-exclamation"></i> 服务端未配置模型密钥时自动降级为演示结果，页面以黄色提示条标出。</div>
                <div class="cs-state-line amber"><i class="fa-solid fa-triangle-exclamation"></i> 登录过期（401）时提示重新登录，不会静默失败。</div>
              </div>
              <ul class="cs-tips">
                <li><i class="fa-solid fa-circle-check"></i> 只根据你提供的素材分析，不臆造视频画面与真实评论。</li>
                <li><i class="fa-solid fa-circle-check"></i> 参考爆文只借鉴选题与结构，不复制原句。</li>
                <li><i class="fa-solid fa-circle-check"></i> 图文版式识别依赖可读图模型；没有截图或视觉模型时会明确标记“无法判断”。</li>
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

      <div v-if="libraryOpen" class="cs-library-backdrop" @click.self="libraryOpen=false">
        <section class="cs-library-popover" role="dialog" aria-modal="true" aria-label="产品词库">
          <div class="cs-library-head">
            <div><div class="cs-kicker">AISOU KEYWORD LIBRARY</div><h2>产品词库</h2><p>词库词会作为变量交给爱搜采集器，周一 12:00 自动更新下拉词知识库。</p></div>
            <button type="button" class="cs-modal-close" @click="libraryOpen=false" aria-label="关闭词库"><i class="fa-solid fa-xmark"></i></button>
          </div>
          <div class="cs-library-toolbar"><span><b>{{ productTerms.length }}</b> 个产品词</span><button type="button" class="cs-btn ghost" :disabled="libraryBusy || librarySync.status==='running'" @click="syncKeywordLibrary"><i class="fa-solid" :class="librarySync.status==='running'?'fa-spinner':'fa-rotate'" ></i> {{ librarySync.status==='running' ? '采集中' : '立即更新' }}</button></div>
          <div class="cs-library-grid">
            <div v-for="item in productTerms" :key="item.id" class="cs-library-term"><span>{{ item.term }}</span><button type="button" title="移除产品词" @click="removeProductTerm(item)"><i class="fa-solid fa-xmark"></i></button></div>
            <form class="cs-library-add" @submit.prevent="addProductTerm"><input v-model="newProductTerm" maxlength="120" placeholder="添加产品词"><button type="submit" :disabled="libraryBusy || !newProductTerm.trim()" title="添加产品词"><i class="fa-solid fa-plus"></i></button></form>
          </div>
          <div v-if="librarySync.message" class="cs-library-foot" :class="{error:librarySync.status==='error'}"><i class="fa-solid" :class="librarySync.status==='error'?'fa-triangle-exclamation':'fa-circle-info'"></i>{{ librarySync.message }}</div>
        </section>
      </div>

      <div v-if="imitationOpen" class="cs-modal-backdrop" @click.self="closeImitation">
        <form class="cs-modal" @submit.prevent="generate">
          <div class="cs-modal-head">
            <div class="cs-chip mint"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
            <div class="grow"><div class="cs-title">一键仿写</div><div class="cs-sub">已根据拆解素材识别内容形态，补充产品资料后生成对应 AI 结果</div></div>
            <button type="button" class="cs-modal-close" aria-label="关闭" @click="closeImitation"><i class="fa-solid fa-xmark"></i></button>
          </div>
          <div class="cs-modal-source" v-if="referenceCard">
            <i class="fa-solid" :class="referenceCard.contentType === 'image_text' ? 'fa-images' : 'fa-video'"></i>
            <span><b>{{ referenceCard.contentType === 'image_text' ? '图文仿写' : '视频仿写' }}</b><small>{{ referenceCard.title }}</small></span>
          </div>
          <div class="cs-modal-grid">
            <label class="cs-modal-field"><span>产品名称 <em>*</em></span><input v-model="productName" maxlength="120" required placeholder="例如：三层防水汽车脚垫"></label>
            <div class="cs-modal-field cs-modal-category"><span>产品品类 <em>*</em></span>
              <div class="cs-category-picker" :class="{open:categoryOpen,filled:!!category.trim()}" @click.stop>
                <button type="button" class="cs-category-trigger" :aria-expanded="categoryOpen" aria-haspopup="listbox" @click="toggleCategory"><span class="cs-category-icon"><i class="fa-solid fa-shapes"></i></span><span class="cs-category-body"><b>{{ category || '请选择品类' }}</b></span><i class="cs-category-chevron fa-solid fa-chevron-down"></i></button>
                <div v-if="categoryOpen" class="cs-category-menu" role="listbox" aria-label="产品品类">
                  <button v-for="item in categoryOptions" :key="item" type="button" role="option" class="cs-category-option" :class="{on:category===item}" :aria-selected="category===item" @click="selectCategory(item)"><span>{{ item }}</span><i v-if="category===item" class="fa-solid fa-check"></i></button>
                  <div v-if="!categoryOptions.length" class="cs-category-empty">暂无品类数据</div>
                </div>
                <button v-if="category" type="button" class="cs-category-clear" aria-label="清空品类" title="清空品类" @click="category=''"><i class="fa-solid fa-xmark"></i></button>
              </div>
            </div>
            <label class="cs-modal-field"><span>品牌 <em>*</em></span><input v-model="brand" maxlength="80" required placeholder="请输入品牌名称"></label>
            <div class="cs-modal-field cs-modal-style"><span>风格偏好 <em>*</em></span>
              <div class="cs-style-select" :class="{open:styleOpen}" @click.stop>
                <button type="button" class="cs-style-trigger" :aria-expanded="styleOpen" aria-haspopup="listbox" @click="styleOpen=!styleOpen">
                  <span class="cs-style-trigger-icon"><i class="fa-solid fa-wand-magic-sparkles"></i></span><span class="cs-style-trigger-copy"><b>{{ stylePreference }}</b></span><i class="cs-style-chevron fa-solid fa-chevron-down"></i>
                </button>
                <div v-if="styleOpen" class="cs-style-menu" role="listbox" aria-label="风格偏好">
                  <button v-for="(style,i) in stylePreferences" :key="style.name" type="button" role="option" class="cs-style-option" :class="{on:stylePreference===style.name}" :aria-selected="stylePreference===style.name" @click="selectStyle(style.name)"><span class="cs-style-index">{{ i + 1 }}</span><span class="cs-style-option-copy"><b>{{ style.name }}</b><small>{{ style.sub }}</small></span><i v-if="stylePreference===style.name" class="fa-solid fa-check"></i></button>
                </div>
              </div>
            </div>
            <label class="cs-modal-field cs-modal-selling"><span>真实卖点 / 参数 / 测试结论 <em>*</em></span><textarea v-model="sellingPoints" maxlength="3000" required placeholder="请填写已确认的卖点、参数或测试结论，AI 不会替你编造事实"></textarea></label>
          </div>
          <div v-if="productionStatus" class="cs-status show" :class="{error:productionError}">{{ productionStatus }}</div>
          <div class="cs-modal-actions"><button type="button" class="cs-btn ghost" @click="closeImitation">取消</button><button type="submit" class="cs-btn mint" :disabled="productionBusy"><i class="fa-solid" :class="productionBusy?'fa-spinner':'fa-wand-magic-sparkles'"></i>{{ productionBusy ? '正在仿写' : '开始一键仿写' }}</button></div>
        </form>
      </div>
    </div>`
  });
  studioApp.mount(mount);
})();
