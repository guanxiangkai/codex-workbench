/* Synthetic public data for the standalone static demonstration. */
(()=>{
  'use strict';
  const stamp='2031-01-15T09:30:00Z',status={snapshot:{state:'ready',updated_at:stamp}};
  const accounts=[{id:'demo-codex',name:'演示 Codex 账户',email:'demo-user@demo.invalid',plan:'plus',login_status:'ready',is_current:true,is_default:true,remaining_percent:72,observed_at:stamp,usage_refresh:{state:'ready'}}];
  const platformAccounts=[{id:'demo-platform',provider_id:'demo-platform',provider_name:'演示其他账户',label:'演示其他账户',login_status:'ready',api_auth:{status:'accepted'},observed_at:stamp,usage_refresh:{state:'ready'}}];
  const models=[{id:'demo-model',name:'Demo Model',provider_id:'demo-platform',provider_name:'演示其他账户',model_type:'reasoning',validation_status:'verified',updated_at:stamp,capabilities:['文本推理']}];
  const views={accounts:{accounts,status},other_accounts:{accounts:platformAccounts,status},models:{models,status}},byId=list=>Object.fromEntries(list.map(item=>[item.id,item]));
  globalThis.DEMO_FIXTURES={modules:[{id:'accounts',name:'Codex 账户',group:'账户与模型'},{id:'other_accounts',name:'其他账户',group:'账户与模型'},{id:'models',name:'模型服务',group:'账户与模型'}],views,details:{models:byId(models)}};
})();
