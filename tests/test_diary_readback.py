from unittest.mock import Mock

import pytest

from app import fatsecret

AUTH = dict(consumer_key='key', consumer_secret='secret',
            access_token='token', access_token_secret='token-secret')


@pytest.mark.parametrize('body', [
    {'food_entry_id': {'value': '123'}}, {'food_entry_id': '123'},
    {'food_entries': {'food_entry': [{'food_entry_id': '123'}]}},
    {'food_entries': {'food_entry': {'food_entry_id': '123'}}},
])
def test_post_accepts_both_id_and_full_entry_responses(monkeypatch, body):
    request = Mock(return_value=Mock(ok=True, json=Mock(return_value=body)))
    monkeypatch.setattr(fatsecret, 'signed_request', request)
    result = fatsecret.post_diary_entry(**AUTH, food_id='10', serving_id='20',
        food_entry_name='SunChips [CB:test]', meal='other', date_int=20000)
    assert result == '123'
    assert request.call_count == 1
    assert request.call_args.kwargs['method'] == 'POST'
    assert request.call_args.kwargs['request_parameters']['number_of_units'] == 1


@pytest.mark.parametrize('body,expected', [
    ({'food_entries': {'food_entry': {'food_entry_id': '1'}}}, [{'food_entry_id': '1'}]),
    ({'food_entries': {'food_entry': [{'food_entry_id': '1'}]}}, [{'food_entry_id': '1'}]),
    ({'food_entries': {}}, []), ({'food_entries': ''}, []),
])
def test_diary_read_formats(monkeypatch, body, expected):
    request = Mock(return_value=Mock(ok=True, json=Mock(return_value=body)))
    monkeypatch.setattr(fatsecret, 'signed_request', request)
    assert fatsecret.read_diary_entries(**AUTH, date_int=20000) == expected
    kwargs = request.call_args.kwargs
    assert kwargs['method'] == 'GET'
    assert kwargs['url'].endswith('/food-entries/v2')
    assert kwargs['token'] == 'token'
    assert kwargs['request_parameters']['date'] == 20000


def test_http_rate_limit_is_explicit_rejection(monkeypatch):
    monkeypatch.setattr(fatsecret, 'signed_request', Mock(return_value=Mock(ok=False, status_code=429)))
    with pytest.raises(fatsecret.FatSecretAPIError) as exc:
        fatsecret.post_diary_entry(**AUTH, food_id='10', serving_id='20',
            food_entry_name='test', meal='other', date_int=20000)
    assert exc.value.code == 'HTTP429'
