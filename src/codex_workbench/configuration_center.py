"""配置中心的公开投影；沿用目录与凭据引用，不解密、不重复登记。"""
from .configuration_schema import configuration_schema


def configuration_entries(entries, models, other_accounts=()):
    """投影公开配置及其显式其他账户关联，不读取凭据正文。"""
    schema = configuration_schema()
    aliases = {key.casefold(): t['id'] for t in schema['types'] for key in (t['id'], t['label'])}
    labels = {t['id']: t['label'] for t in schema['types']}
    result = []
    account_links = {}
    provider_links = {}
    for account in other_accounts:
        vault_id = account.get('vault_id')
        if not vault_id:
            continue
        account_links.setdefault(vault_id, []).append(account['id'])
        provider_links.setdefault(vault_id, []).append(account['provider_id'])
    model_credentials = {model.get('credential_id') for model in models if model.get('credential_id')}
    for entry in entries:
        # 模型专用配置由模型目录展示，保险库仍是秘密的唯一来源。
        if entry['id'] in model_credentials and entry['id'] not in account_links:
            continue
        item = dict(entry)
        # 标签是用户明确登记的元数据；不依据名称或秘密字段猜测服务类型。
        types = {aliases[tag.casefold()] for tag in item.get('tags', []) if isinstance(tag, str) and tag.casefold() in aliases}
        explicit = item.get('service_type')
        kind = explicit if explicit in labels else next(iter(types)) if len(types) == 1 else None
        item.update(service_type=kind, service_type_label=labels.get(kind),
                    other_account_ids=account_links.get(entry['id'], []),
                    account_provider_ids=list(dict.fromkeys(provider_links.get(entry['id'], []))))
        result.append(item)
    return result
