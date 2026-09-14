from unittest.mock import Mock

import pytest

from analytics_integrations import google_ads_query


@pytest.fixture
def google_environment(monkeypatch):
    import os
    for name in list(os.environ):
        if name.startswith('GOOGLE_ADS_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('GOOGLE_ADS_CUSTOMER_ID', '377-647-9482')
    monkeypatch.delenv('GOOGLE_REPORTING_SERVICE_ACCOUNT_JSON', raising=False)
    monkeypatch.setenv('GOOGLE_ADS_USE_PROTO_PLUS', 'true')
    monkeypatch.setenv('GOOGLE_ADS_JSON_KEY_FILE_PATH', '/test-only/reporting.json')


def test_google_sdk_accepts_service_account_without_developer_token(google_environment, monkeypatch):
    from google.ads.googleads import oauth2
    from google.ads.googleads.client import GoogleAdsClient
    from google.auth.credentials import AnonymousCredentials
    monkeypatch.setattr(oauth2, 'get_credentials', lambda settings: AnonymousCredentials())
    client = GoogleAdsClient.load_from_env()
    assert client.developer_token is None
    assert client.use_proto_plus is True


def test_google_query_omits_retired_token(google_environment, monkeypatch):
    from google.ads.googleads.client import GoogleAdsClient
    monkeypatch.setenv('GOOGLE_ADS_DEVELOPER_TOKEN', 'obsolete-test-only')
    factory = Mock()
    factory.return_value.get_service.return_value.search.return_value = ['row']
    monkeypatch.setattr(GoogleAdsClient, 'load_from_dict', factory)
    assert google_ads_query('SELECT campaign.id FROM campaign') == ['row']
    settings = factory.call_args.args[0]
    assert 'developer_token' not in settings
    assert settings['json_key_file_path'] == '/test-only/reporting.json'
    factory.return_value.get_service.return_value.search.assert_called_once_with(
        customer_id='3776479482', query='SELECT campaign.id FROM campaign')


def test_google_configuration_errors_do_not_expose_credentials(google_environment, monkeypatch):
    from google.ads.googleads.client import GoogleAdsClient
    monkeypatch.setattr(GoogleAdsClient, 'load_from_dict', Mock(side_effect=ValueError('secret-test-only')))
    with pytest.raises(RuntimeError) as error:
        google_ads_query('SELECT campaign.id FROM campaign')
    assert 'secret-test-only' not in str(error.value)
    assert error.value.__suppress_context__


def test_google_invalid_customer_does_not_call_api(google_environment, monkeypatch):
    from google.ads.googleads.client import GoogleAdsClient
    factory = Mock()
    monkeypatch.setattr(GoogleAdsClient, 'load_from_dict', factory)
    monkeypatch.setenv('GOOGLE_ADS_CUSTOMER_ID', 'invalid')
    with pytest.raises(RuntimeError):
        google_ads_query('SELECT campaign.id FROM campaign')
    factory.assert_not_called()


def test_google_json_credentials_use_only_requested_scopes(monkeypatch):
    import json
    from analytics_integrations import google_reporting_credentials
    from google.oauth2.service_account import Credentials
    info = {'type': 'service_account', 'token_uri': 'https://oauth2.googleapis.com/token'}
    monkeypatch.setenv('GOOGLE_REPORTING_SERVICE_ACCOUNT_JSON', json.dumps(info))
    factory = Mock(return_value='credentials')
    monkeypatch.setattr(Credentials, 'from_service_account_info', factory)
    scopes = ['https://www.googleapis.com/auth/analytics.readonly']
    assert google_reporting_credentials(scopes) == 'credentials'
    factory.assert_called_once_with(info, scopes=scopes)


def test_google_json_rejects_untrusted_token_endpoint(monkeypatch):
    import json
    from analytics_integrations import google_reporting_credentials
    monkeypatch.setenv('GOOGLE_REPORTING_SERVICE_ACCOUNT_JSON', json.dumps({
        'type': 'service_account', 'token_uri': 'https://invalid.example/token', 'private_key': 'secret-test-only'}))
    with pytest.raises(RuntimeError) as error:
        google_reporting_credentials([])
    assert 'secret-test-only' not in str(error.value)
