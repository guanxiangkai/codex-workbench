/* Synthetic public data for the standalone static demonstration. */
(()=>{
  'use strict';
  const stamp='2031-01-15T09:30:00Z',status={snapshot:{state:'ready',updated_at:stamp}};
  const accounts=[{id:'demo-codex',name:'演示 Codex 账户',email:'demo-user@demo.invalid',plan:'plus',login_status:'ready',is_current:true,is_default:true,remaining_percent:72,observed_at:stamp,usage_refresh:{state:'ready'}}];
  const otherAccounts=[{id:'demo-provider-account',provider_id:'demo-provider',provider_name:'演示模型服务',label:'演示模型服务',login_status:'ready',api_auth:{status:'accepted'},observed_at:stamp,usage_refresh:{state:'ready'}}];
  const models=[{id:'demo-model',name:'Demo Model',provider_id:'demo-provider',provider_name:'演示模型服务',model_type:'chat',validation_status:'verified',updated_at:stamp}];
  const entries=[{id:'demo-config',label:'演示配置',service_type:'http',service_type_label:'HTTP 服务',updated_at:stamp,description:'仅展示合成的公开配置元数据。'}];
  const connections=[{id:'demo-mcp',name:'演示 MCP',kind:'mcp',transport:'stdio',enabled:true}];
  const views={accounts:{accounts,status},other_accounts:{accounts:otherAccounts,status},models:{models,status},config:{folders:[],entries,status},connections:{connections,status}},byId=list=>Object.fromEntries(list.map(item=>[item.id,item]));
  globalThis.DEMO_FIXTURES={modules:[{id:'accounts',name:'Codex 账户',group:'账户管理'},{id:'other_accounts',name:'其他账户',group:'账户管理'},{id:'models',name:'模型目录',group:'账户管理'},{id:'config',name:'配置中心',group:'账户管理'},{id:'connections',name:'连接管理',group:'账户管理'}],views,details:{models:byId(models),config:byId(entries)}};
})();
