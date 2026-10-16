import pytest

from app.providers import ProviderQuota, ProviderRegistry, ProviderResult, _sanitize_result


def test_provider_result_rejects_non_http_source_urls():
    result = _sanitize_result(ProviderResult(provider="demo", external_id="1", title="x", source_url="javascript:alert(1)"))
    assert result is not None
    assert result.source_url == ""


def test_provider_quota_is_bounded():
    quota = ProviderQuota(requests=1, period_seconds=60)
    assert quota.allow() is True
    assert quota.allow() is False
