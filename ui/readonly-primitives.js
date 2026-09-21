/* 无状态的只读列表渲染积木；调用方负责提供展示上下文。 */
(()=>{
 'use strict';

 function createReadonlyPrimitives({escape,icon}){
  if(typeof escape!=='function'||typeof icon!=='function')throw Error('只读渲染依赖无效');
  const values=items=>Array.isArray(items)?items:[];
  return {
   options(items,value,empty){
    return `<option value="">${escape(empty)}</option>`+values(items)
     .map(item=>`<option value="${escape(item.id)}" ${item.id===value?'selected':''}>${escape(item.name||item.title||item.id)}</option>`)
     .join('');
   },
   select(id,label,content){
    return `<label class="select"><span class="sr-only">${escape(label)}</span><select id="${escape(id)}">${content}</select>${icon('chevron-down')}</label>`;
   },
   listCount(items,unit){
    return `<span class="count" role="status">${values(items).length} ${escape(unit)}</span>`;
   },
   search(page,query){
    return `<label class="search">${icon('search')}<span class="sr-only">搜索${escape(page)}</span><input id="query" placeholder="搜索${escape(page)}" value="${escape(query)}" autocomplete="off"></label>`;
   },
   empty(text='暂无数据'){
    return `<div class="empty">${icon('inbox')}<p>${escape(text)}</p></div>`;
   },
   toolbar(...content){
    return `<div class="toolbar">${content.filter(Boolean).join('')}</div>`;
   }
  };
 }

 window.createWorkbenchReadonlyPrimitives=createReadonlyPrimitives;
})();
