"""OpenAI 兼容模型能力请求的唯一 endpoint 解析规则。"""
from urllib.parse import urlsplit

_PATHS={"reasoning":lambda protocol:"/responses" if protocol=="openai-responses" else "/chat/completions","multimodal":lambda protocol:"/responses" if protocol=="openai-responses" else "/chat/completions","rerank":lambda _:"/rerank","embedding":lambda _:"/embeddings","speech_to_text":lambda _:"/audio/transcriptions","text_to_speech":lambda _:"/audio/speech"}
_KNOWN={"/chat/completions","/responses","/rerank","/embeddings","/audio/transcriptions","/audio/speech"}
def resolve_endpoint(base_url,model_type,protocol):
    """返回规范化实际 POST endpoint；已给出不匹配能力路径时明确拒绝。"""
    if model_type not in _PATHS: raise ValueError("模型类型无有效能力路径")
    parsed=urlsplit(base_url)
    if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError("接口地址无效")
    required=_PATHS[model_type](protocol); path=parsed.path.rstrip("/")
    suffix=next((item for item in _KNOWN if path.endswith(item)),None)
    if suffix and suffix!=required: raise ValueError("接口路径与模型能力不匹配")
    if not suffix: path+=required
    host=parsed.hostname.lower(); host=f"[{host}]" if ':' in host and not host.startswith('[') else host
    port=parsed.port
    port_text='' if port is None or (parsed.scheme=='http' and port==80) or (parsed.scheme=='https' and port==443) else f":{port}"
    return f"{parsed.scheme.lower()}://{host}{port_text}{path}"
