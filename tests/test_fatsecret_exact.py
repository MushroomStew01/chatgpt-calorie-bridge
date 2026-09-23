from unittest.mock import Mock

import pytest
import requests

from app import fatsecret


AUTH = dict(consumer_key="key", consumer_secret="secret",
            access_token="user-token", access_token_secret="user-secret")


def response(body):
    return Mock(ok=True, json=Mock(return_value=body))


def diary(calories="540", **extra):
    return {"food_entries": {"food_entry": {
        "food_entry_id": "123", "calories": calories, **extra,
    }}}


def write_diary(expected=540):
    return fatsecret.create_diary_entry(
        **AUTH, food_id="10", serving_id="20", number_of_units=1,
        food_entry_name="Pizza - 3 slices", meal="other", date_int=20719,
        expected_calories=expected,
    )


@pytest.mark.parametrize("calories", [0, 1, 540, 540.25])
def test_exact_custom_food_preserves_supplied_nutrition(monkeypatch, calories):
    request = Mock(side_effect=[
        response({"food_id": {"value": "10"}}),
        response({"food": {"servings": {"serving": [
            {"serving_id": "0", "calories": str(calories), "number_of_units": "1"},
            {"serving_id": "20", "calories": str(calories), "number_of_units": "1"},
        ]}}}),
    ])
    monkeypatch.setattr(fatsecret, "signed_request", request)
    assert fatsecret.create_exact_food(
        **AUTH, name="Pizza - 3 slices", calories=calories, protein=24,
        carbs=66, fat=20, fiber=4, sugar=6,
    ) == ("10", "20")
    payload = request.call_args_list[0].kwargs["request_parameters"]
    assert payload["calories"] == str(calories)
    assert payload["protein"] == 24
    assert payload["carbohydrate"] == 66
    assert payload["fat"] == 20
    assert payload["fiber"] == 4
    assert payload["sugar"] == 6
    assert all(call.kwargs["token"] == "user-token" for call in request.call_args_list)


@pytest.mark.parametrize("calories", [0, 1, 540, 540.25])
def test_exact_diary_success(monkeypatch, calories):
    request = Mock(return_value=response(diary(str(calories))))
    monkeypatch.setattr(fatsecret, "signed_request", request)
    assert write_diary(calories) == "123"
    assert request.call_count == 1


@pytest.mark.parametrize("actual", ["539", "541", "0", None, "garbage", "NaN", "Infinity"])
def test_even_small_mismatch_or_unverifiable_calories_are_removed(monkeypatch, actual):
    request = Mock(side_effect=[response(diary(actual)), response({"success": {"value": "1"}})])
    monkeypatch.setattr(fatsecret, "signed_request", request)
    with pytest.raises(fatsecret.FatSecretError, match="incorrect FatSecret entry was removed"):
        write_diary()
    assert request.call_count == 2
    assert request.call_args.kwargs["method"] == "DELETE"
    assert request.call_args.kwargs["request_parameters"]["food_entry_id"] == "123"


@pytest.mark.parametrize("body", [{"error": {"code": 9, "message": "Invalid access token"}}, {}, []])
def test_api_errors_and_unknown_write_results_never_report_success(monkeypatch, body):
    request = Mock(return_value=response(body))
    monkeypatch.setattr(fatsecret, "signed_request", request)
    with pytest.raises(fatsecret.FatSecretError):
        write_diary()
    assert request.call_count == 1


def test_cleanup_api_error_is_not_reported_as_removed(monkeypatch):
    request = Mock(side_effect=[response(diary("500")), response({"error": {"code": 9}})])
    monkeypatch.setattr(fatsecret, "signed_request", request)
    with pytest.raises(fatsecret.FatSecretError, match="entry 123.*cleanup unconfirmed"):
        write_diary()
    assert request.call_count == 2


def test_timeout_is_not_retried(monkeypatch):
    request = Mock(side_effect=requests.Timeout())
    monkeypatch.setattr(fatsecret, "signed_request", request)
    with pytest.raises(requests.Timeout):
        write_diary()
    assert request.call_count == 1


def test_custom_food_permission_failure_is_explicit(monkeypatch):
    request = Mock(return_value=response({"error": {"code": 13, "message": "Permission denied"}}))
    monkeypatch.setattr(fatsecret, "signed_request", request)
    with pytest.raises(fatsecret.FatSecretError, match="food.create.v2 access required.*Permission denied"):
        fatsecret.create_exact_food(**AUTH, name="Pizza", calories=540,
                                   protein=24, carbs=66, fat=20, fiber=4, sugar=6)
    assert request.call_count == 1


@pytest.mark.parametrize("serving", [
    {"serving_id": "0", "calories": "540", "number_of_units": "1"},
    {"serving_id": "20", "calories": "539", "number_of_units": "1"},
    {"serving_id": "20", "calories": "540", "number_of_units": "2"},
])
def test_custom_food_cannot_use_derived_or_inexact_serving(monkeypatch, serving):
    request = Mock(side_effect=[response({"food_id": {"value": "10"}}),
                               response({"food": {"servings": {"serving": serving}}})])
    monkeypatch.setattr(fatsecret, "signed_request", request)
    with pytest.raises(fatsecret.FatSecretError, match="no one-meal serving"):
        fatsecret.create_exact_food(**AUTH, name="Pizza", calories=540,
                                   protein=24, carbs=66, fat=20, fiber=4, sugar=6)
    assert request.call_count == 2
