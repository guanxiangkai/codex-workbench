"""根据真实调用结果区分付费限制、凭据问题和临时故障。"""

def availability(status, code=None):
    """返回面向用户的状态；未能验证不等于模型永久不可用。"""
    if status == 'verified': return 'available'
    if status == 'paused': return 'paused'
    if status == 'validating': return 'validating'
    if status == 'pending': return 'unverified'
    if code == 'insufficient_balance': return 'insufficient_balance'
    if code == 'payment_required': return 'payment_required'
    if code in {'credential_rejected', 'unauthorized', 'forbidden', 'credential_format', 'credential_target'}: return 'access_denied'
    if code == 'credential_missing': return 'missing_credential'
    if code in {'endpoint_or_model_unsupported', 'not_found', 'task_missing'}: return 'unavailable'
    if code in {'timeout', 'network_error', 'connection_error', 'rate_limited', 'provider_server_error', 'server_error'}: return 'temporary_error'
    if status == 'skipped' or code in {'unsupported', 'capability_unsupported', 'unregistered_https_endpoint', 'validation_fixture_missing'}: return 'unsupported_validation'
    return 'verification_failed'
