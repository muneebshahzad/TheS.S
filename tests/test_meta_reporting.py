from unittest.mock import Mock

import pytest

import analytics_integrations as integration


@pytest.fixture
def meta_env(monkeypatch):
    monkeypatch.setenv('META_ACCESS_TOKEN', 'test-only-secret')
    monkeypatch.setenv('META_API_VERSION', 'v26.0')
    monkeypatch.setenv('META_AD_ACCOUNT_ID', '356232020087034')
    monkeypatch.setenv('ANALYTICS_CURRENCY', 'PKR')


def test_meta_pagination_keeps_token_off_urls(meta_env, monkeypatch):
    request = Mock(side_effect=[{'data': [{'id': '1'}], 'paging': {
        'next': 'https://untrusted.example/?access_token=test-only-secret', 'cursors': {'after': 'next-page'}}},
        {'data': [{'id': '2'}]}])
    monkeypatch.setattr(integration, 'request_json', request)
    assert len(integration.meta_pages('act_356232020087034/ads', {'limit': 1})) == 2
    for call in request.call_args_list:
        assert call.args == ('GET', 'https://graph.facebook.com/v26.0/act_356232020087034/ads')
        assert call.kwargs['headers'] == {'Authorization': 'Bearer test-only-secret'}
        assert call.kwargs['allow_redirects'] is False
        assert 'access_token' not in call.kwargs['params']


@pytest.mark.parametrize('response', [
    {'error': {'message': 'test-only-secret'}}, {}, {'data': None},
    {'data': [], 'paging': {'next': 'unused'}},
])
def test_meta_invalid_responses_never_become_zero_spend(meta_env, monkeypatch, response):
    monkeypatch.setattr(integration, 'request_json', Mock(return_value=response))
    save = Mock()
    monkeypatch.setattr(integration, 'save_report', save)
    with pytest.raises(RuntimeError) as error:
        integration.sync_meta('2026-09-13')
    assert 'test-only-secret' not in str(error.value)
    save.assert_not_called()


def test_meta_repeated_cursor_stops(meta_env, monkeypatch):
    request = Mock(return_value={'data': [], 'paging': {'next': 'unused', 'cursors': {'after': 'same'}}})
    monkeypatch.setattr(integration, 'request_json', request)
    with pytest.raises(RuntimeError, match='pagination'):
        integration.meta_pages('act_356232020087034/ads', {})
    assert request.call_count == 2


def test_meta_wrong_account_never_called(meta_env, monkeypatch):
    request = Mock()
    monkeypatch.setattr(integration, 'request_json', request)
    with pytest.raises(RuntimeError, match='outside'):
        integration.meta_get('act_781154257971392/insights', {})
    request.assert_not_called()


def test_meta_check_verifies_account_identity(meta_env, monkeypatch):
    monkeypatch.setattr(integration, 'meta_get', Mock(return_value={
        'id': 'act_356232020087034', 'account_id': '356232020087034',
        'currency': 'PKR', 'timezone_name': 'Asia/Karachi'}))
    result = integration.meta_check()
    assert result['currency_matches'] and result['timezone_matches']
    monkeypatch.setattr(integration, 'meta_get', Mock(return_value={'account_id': 'wrong'}))
    with pytest.raises(RuntimeError, match='identity'):
        integration.meta_check()


@pytest.mark.parametrize('rows', [
    [{'ad_id': '1', 'account_currency': 'USD'}],
    [dict(ad_id='1', account_currency='PKR', campaign_id='2', campaign_name='C',
          adset_id='3', adset_name='G', ad_name='A')] * 2,
])
def test_meta_currency_or_duplicate_rows_not_saved(meta_env, monkeypatch, rows):
    monkeypatch.setattr(integration, 'meta_pages', Mock(return_value=rows))
    save = Mock()
    monkeypatch.setattr(integration, 'save_report', save)
    with pytest.raises(RuntimeError):
        integration.sync_meta('2026-09-13')
    save.assert_not_called()
