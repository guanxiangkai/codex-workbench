"""以原有配置引用形成服务目录；不解密或重复登记连接信息。"""
import hashlib

CONNECTION_FIELDS=frozenset({'host','ip','port','endpoint','base_url','connection_uri','brokers','name_servers','endpoints'})


def services(models,entries):
    """只依据明确的模型配置或当前版本字段结构，不从名称猜服务类型。"""
    result=[];model_credentials=set();model_services={}
    for m in models:
        if not m.get('base_url'):continue
        reference=m.get('credential_id')
        if reference:model_credentials.add(reference)
        key=('credential',reference) if reference else ('endpoint',m['base_url'])
        model={'id':m['id'],'name':m['name'],'model_type':m.get('model_type')}
        if key in model_services:
            model_services[key]['models'].append(model);continue
        service={'id':'model:'+m['id'],'name':m.get('service_name') or m['name'],'kind':'模型服务','source':'model_configuration',
                 'reference_kind':'model','reference_id':m['id'],'endpoint':m['base_url'],'tags':[],
                 'models':[model],'credential_id':reference,'updated_at':m.get('updated_at')}
        model_services[key]=service;result.append(service)
    for entry in entries:
        if entry['id'] in model_credentials:continue
        if entry.get('structure_status')!='indexed' or not CONNECTION_FIELDS.intersection(entry.get('has_fields',[])):continue
        result.append({'id':'credential:'+entry['id'],'name':entry.get('label') or entry['id'],'kind':'连接配置',
                       'source':'credential_structure','reference_kind':'credential','reference_id':entry['id'],
                       'endpoint':None,'tags':entry.get('tags',[]),'credential_id':entry['id'],'updated_at':entry.get('updated_at')})
    return result
