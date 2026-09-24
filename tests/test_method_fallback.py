from unittest.mock import Mock

import pytest
import requests

from app import fatsecret


def response(body):
    return Mock(ok=True, json=Mock(return_value=body))


@pytest.mark.parametrize('method,http,url', [
    ('food.create.v2', 'POST', fatsecret.FOOD_CREATE_URL),
    ('food.get.v5', 'GET', fatsecret.FOOD_GET_URL),
    ('food_entry.create', 'POST', fatsecret.DIARY_URL),
    ('food_entries.get.v2', 'GET', 'https://platform.fatsecret.com/rest/food-entries/v2'),
])
def test_unknown_method_uses_fresh_signed_documented_format(monkeypatch, method, http, url):
    request = Mock(side_effect=[response({'error': {'code': 10}}), response({'result': 'ok'})])
    monkeypatch.setattr(fatsecret, 'signed_request', request)
    params = {'format': 'json', 'calories': 550}
    result = fatsecret.platform_json(api_method=method, consumer_key='key',
        consumer_secret='secret', token='token', token_secret='token-secret',
        method=http, url=url, request_parameters=params)
    assert result == {'result': 'ok'}
    first, second = [call.kwargs for call in request.call_args_list]
    assert first['url'] == url
    assert second['url'] == 'https://platform.fatsecret.com/rest/server.api'
    assert second['request_parameters'] == {**params, 'method': method}
    assert second['method'] == http
    assert second['token'] == first['token']
    assert params == {'format': 'json', 'calories': 550}


@pytest.mark.parametrize('code', [1, 9, 11, 12, 13, 14, 20, 21, 24, 101])
def test_other_errors_never_trigger_alternate_write(monkeypatch, code):
    request = Mock(return_value=response({'error': {'code': code, 'message': 'secret token'}}))
    monkeypatch.setattr(fatsecret, 'signed_request', request)
    with pytest.raises(fatsecret.FatSecretAPIError) as caught:
        fatsecret.platform_json(api_method='food.create.v2', method='POST', url=fatsecret.FOOD_CREATE_URL)
    assert request.call_count == 1
    assert caught.value.operation == 'food.create.v2'
    assert 'secret token' not in str(caught.value)


@pytest.mark.parametrize('error', [requests.Timeout(), requests.ConnectionError(),
                                  fatsecret.FatSecretError('invalid JSON')])
def test_uncertain_request_is_never_replayed(monkeypatch, error):
    request = Mock(side_effect=error)
    monkeypatch.setattr(fatsecret, 'signed_request', request)
    with pytest.raises(type(error)):
        fatsecret.platform_json(api_method='food_entry.create', method='POST', url=fatsecret.DIARY_URL)
    assert request.call_count == 1


def test_second_unknown_method_stops_after_two_attempts(monkeypatch):
    request = Mock(return_value=response({'error': {'code': 10}}))
    monkeypatch.setattr(fatsecret, 'signed_request', request)
    with pytest.raises(fatsecret.FatSecretAPIError) as caught:
        fatsecret.platform_json(api_method='food.create.v2', method='POST', url=fatsecret.FOOD_CREATE_URL)
    assert request.call_count == 2
    assert caught.value.operation == 'food.create.v2'
