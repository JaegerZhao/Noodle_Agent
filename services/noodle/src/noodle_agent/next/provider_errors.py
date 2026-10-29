"""Provider stops projected consistently for old receipts and new failures."""


def provider_issue(error):
    detail = str(error.get('detail', ''))
    if any(term in detail.casefold() for term in ('accountoverdueerror', 'insufficient balance', 'insufficient_quota')):
        return {'category': 'PROVIDER_BILLING', 'status': 'PROVIDER_BLOCKED',
            'message': '模型服务账户余额不足或欠费，充值后可从检查点继续。', 'retry_after_recovery': True}
    if any(term in detail.casefold() for term in ('invalid_api_key', 'incorrect api key', 'authenticationerror')):
        return {'category': 'PROVIDER_AUTH', 'status': 'PROVIDER_BLOCKED',
            'message': '模型服务凭据不可用，更新配置后可从检查点继续。', 'retry_after_recovery': True}
    return None
