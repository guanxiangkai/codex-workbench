/* Synthetic public data for the standalone static demonstration. */
(()=>{
  'use strict';
  const stamp='2031-01-15T09:30:00Z';
  const accounts=Array.from({length:8},(_,index)=>({
    id:`demo-codex-${index+1}`,name:`演示 Codex 账户 ${index+1}`,
    email:`demo-user-${index+1}@demo.invalid`,plan:index<3?'pro':'plus',
    login_status:'ready',is_current:index===0,is_default:index===0,
    remaining_percent:92-index*7,resets_at:'2031-01-16T00:00:00Z',reset_cards:index%3,
    observed_at:stamp,usage_refresh:{state:'ready'}
  }));
  const providers=['OpenAI 兼容服务','内部模型网关','研究模型目录'];
  const otherAccounts=Array.from({length:6},(_,index)=>({
    id:`demo-provider-account-${index+1}`,provider_id:`demo-provider-${index%3+1}`,
    provider_name:providers[index%3],label:`${providers[index%3]} · 演示 ${index+1}`,
    login_status:'ready',is_current:index===0,is_used:index%2===0,
    api_auth:{status:'accepted',source:'manual_audit',observed_at:stamp},
    usage:{used:14+index*3,limit:100,unit:'请求'},
    usage_windows:[
      {label:'近 5 小时',usage:{used:8+index,limit:50,unit:'请求'},resets_at:'2031-01-15T12:00:00Z'},
      {label:'近 7 天',usage:{used:14+index*3,limit:100,unit:'请求'},resets_at:'2031-01-20T00:00:00Z'}
    ],
    observed_at:stamp,profile_observed_at:stamp,usage_refresh:{state:'ready'}
  }));
  const models=Array.from({length:24},(_,index)=>({
    id:`demo-model-${index+1}`,name:`Demo Model ${String(index+1).padStart(2,'0')}`,
    provider_id:`demo-provider-${index%3+1}`,provider_name:providers[index%3],
    model_type:index%4===0?'embedding':'chat',network_scope:index%2?'external':'internal',
    validation_status:'verified',base_url:'https://demo.invalid/v1',updated_at:stamp,
    description:`用于静态演示的合成模型 ${index+1}`
  }));
  const skills=Array.from({length:18},(_,index)=>({
    id:`demo-skill-${index+1}`,name:`demo_skill_${index+1}`,display_name:`演示技能 ${index+1}`,
    short_description:`合成技能说明 ${index+1}`,description:`这是静态演示中的合成技能 ${index+1}，不连接任何外部服务。`,
    scope:index%3===0?'repo':'user',enabled:true,plugin_id:'static-demo',updated_at:stamp
  }));
  const entries=Array.from({length:24},(_,index)=>({
    id:`demo-config-${index+1}`,name:`demo-config-${index+1}`,label:`公开演示配置 ${index+1}`,
    folder_id:index%2?'demo-folder-services':'demo-folder-models',service_type:'http',
    service_type_label:'HTTP 服务',tags:['演示','公开字段'],account_provider_ids:[],
    mapping_status:'ready',updated_at:stamp,description:'仅展示合成的公开配置元数据。'
  }));
  const knowledge=Array.from({length:40},(_,index)=>({
    knowledge_key:`demo-knowledge-${index+1}`,scope_key:index%2?'global':'team-demo',
    title:`演示知识 ${index+1}`,summary:`用于静态工作台演示的合成知识摘要 ${index+1}`,
    content:`演示知识正文 ${index+1}。其中不包含真实账户、服务地址或凭据。`,
    source:'static-demo',updated_at:stamp
  }));
  const status={snapshot:{state:'ready',updated_at:stamp}};
  const views={
    accounts:{accounts,reset_analysis:{signal:'present',confidence:0.82,predicted_reset_window:{start:'2031-01-16T00:00:00Z',end:'2031-01-16T01:00:00Z'},summary:'此处展示合成分析结果，不代表实际账户。',observed_at:stamp},status},
    other_accounts:{accounts:otherAccounts,other_account_providers:providers.map((name,index)=>({id:`demo-provider-${index+1}`,name,count:2})),facets:{providers:providers.map((name,index)=>({id:`demo-provider-${index+1}`,name,count:2}))},status},
    models:{models,facets:{providers:providers.map((name,index)=>({id:`demo-provider-${index+1}`,name,count:8}))},status},
    agents:{skills,status},
    config:{folders:[{id:'demo-folder-services',name:'演示服务',parent_id:null},{id:'demo-folder-models',name:'演示模型',parent_id:null}],entries,facets:{kinds:[{id:'http',name:'HTTP 服务',count:24}]},other_account_providers:[],status},
    knowledge:{knowledge,scopes:[{id:'global',name:'全局演示',count:20},{id:'team-demo',name:'团队演示',count:20}],selected_scope:'global',status}
  };
  const byId=list=>Object.fromEntries(list.map(item=>[item.id||item.knowledge_key,item]));
  globalThis.DEMO_FIXTURES={
    modules:[
      {id:'accounts',name:'Codex 账户',group:'账户与配置'},
      {id:'other_accounts',name:'其他账户',group:'账户与配置'},
      {id:'config',name:'配置中心',group:'账户与配置'},
      {id:'agents',name:'技能助手',group:'能力与知识'},
      {id:'knowledge',name:'知识中心',group:'能力与知识'},
      {id:'models',name:'模型目录',group:'能力与知识'}
    ],
    views,details:{skills:byId(skills),models:byId(models),knowledge:byId(knowledge),projects:{}}
  };
})();
