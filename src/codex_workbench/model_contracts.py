"""显式绑定的协议参考，不代表第三方接口已经验证支持。"""
from copy import deepcopy

PROFILES = {
    'openai_images': {
        'name': 'Images', 'path': '/v1/images/generations',
        'source_url': 'https://developers.openai.com/api/reference/resources/images/methods/generate',
        'inputs': [
            ('prompt', 'string', '必填', '图像生成提示词'),
            ('model', 'string', '可选', '具体模型名称；当前配置不指定'),
            ('n', 'integer', '可选', '生成数量，范围取决于服务支持'),
            ('size', 'string', '可选', '图像尺寸，支持值取决于模型'),
            ('quality', 'string', '可选', '图像质量，支持值取决于模型'),
            ('response_format', 'string', '可选', 'url 或 b64_json；部分模型不支持此参数'),
        ],
        'outputs': [
            ('created', 'integer', '', '生成时间，Unix 秒'),
            ('data[]', 'array<object>', '', '生成的图像列表'),
            ('data[].url', 'string', '条件返回', '图像链接，仅部分模型及格式返回'),
            ('data[].b64_json', 'string', '条件返回', 'Base64 图像，仅相应返回格式提供'),
            ('data[].revised_prompt', 'string', '条件返回', '模型调整后的提示词'),
            ('usage', 'object', '条件返回', 'Token 使用量，字段取决于模型'),
        ],
    },
    'openai_chat': {
        'name': 'Chat Completions', 'path': '/v1/chat/completions',
        'source_url': 'https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create',
        'inputs': [
            ('messages[]', 'array<object>', '必填', '对话消息列表'),
            ('messages[].role', 'string', '必填', '消息角色，例如 user、assistant'),
            ('messages[].content', 'string | array | null', '依角色', '消息内容或多模态内容块'),
            ('model', 'string', '协议必填', '官方协议要求；此接口省略时的行为尚未验证'),
            ('stream', 'boolean', '可选', '启用时返回 SSE 增量事件'),
            ('temperature', 'number', '可选', '采样参数，支持性取决于模型'),
            ('max_completion_tokens', 'integer', '可选', '生成 Token 上限，支持性取决于模型'),
        ],
        'outputs': [
            ('id', 'string', '', '响应标识'),
            ('model', 'string', '', '实际返回的模型名称'),
            ('choices[].message.content', 'string | null', '非流式', '生成的回复内容'),
            ('choices[].finish_reason', 'string | null', '', '生成结束原因'),
            ('choices[].delta.content', 'string', '流式', '增量文本，不是 message 完整内容'),
            ('usage.prompt_tokens', 'integer', '条件返回', '输入 Token'),
            ('usage.completion_tokens', 'integer', '条件返回', '生成 Token'),
            ('usage.total_tokens', 'integer', '条件返回', '总 Token；流式需服务支持用量返回'),
        ],
    },
}

def contract_for(model):
    profile = PROFILES.get(model.get('api_profile'))
    if profile is None:
        return None
    return {**deepcopy(profile), 'method': 'POST', 'status': 'reference',
            'note': '常用字段的协议参考；未验证此服务的实际支持范围。'}
