/* Build-time replacement for the workbench bridge: local, synthetic reads only. */
const demoClone=value=>JSON.parse(JSON.stringify(value));
const DemoAdapter={
  tool(name,args={}){
    const fixtures=globalThis.DEMO_FIXTURES;
    if(name==='workbench_sync'){
      const data=fixtures.views[args.view];
      if(!data)throw Error('静态演示没有此页面');
      const result=demoClone(data);
      if(args.view==='knowledge'){
        const scope=args.scope&&result.scopes.some(item=>item.id===args.scope)?args.scope:'global';
        const query=String(args.query||'').trim().toLocaleLowerCase();
        result.selected_scope=scope;
        result.knowledge=result.knowledge.filter(item=>item.scope_key===scope&&(!query||`${item.title} ${item.summary} ${item.content}`.toLocaleLowerCase().includes(query)));
      }
      return Promise.resolve({context:'static-demo-v1',revision:`demo-${args.view}`,reset:true,data:result});
    }
    if(name==='skill_detail')return Promise.resolve({skill:demoClone(fixtures.details.skills[args.id])});
    if(name==='model_detail')return Promise.resolve({model:demoClone(fixtures.details.models[args.id]),credential:{id:'demo-public-metadata',label:'公开演示元数据',revision:'static-demo'}});
    if(name==='knowledge_detail')return Promise.resolve({knowledge:demoClone(fixtures.details.knowledge[args.key])});
    if(name==='project_detail')return Promise.resolve({project:{id:args.id,name:'演示项目'},sessions:[]});
    if(name==='credential_list')return Promise.resolve({entries:demoClone(fixtures.views.config.entries),folders:demoClone(fixtures.views.config.folders)});
    throw Error('静态演示不提供此操作');
  }
};
globalThis.DEMO_STATIC_ADAPTER=DemoAdapter;
class Bridge{
  constructor(){this.embedded=false;}
  async initialize(){}
  async tool(name,args={}){return DemoAdapter.tool(name,args);}
  async openDocumentation(){throw Error('静态演示不打开外部链接');}
  async open(){throw Error('静态演示不打开外部链接');}
  async openLogin(){throw Error('静态演示不提供登录');}
}
