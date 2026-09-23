from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import parse_qs, quote

import requests

from app.fatsecret_match import (
    FatSecretMatch,
    choose_best_match,
    detailed_food_from_response,
    search_foods_from_response,
)

REQUEST_TOKEN_URL = "https://authentication.fatsecret.com/oauth/request_token"
AUTHORIZE_URL = "https://authentication.fatsecret.com/oauth/authorize"
ACCESS_TOKEN_URL = "https://authentication.fatsecret.com/oauth/access_token"
SEARCH_URL = "https://platform.fatsecret.com/rest/foods/search/v1"
FOOD_GET_URL = "https://platform.fatsecret.com/rest/food/v5"
FOOD_CREATE_URL = "https://platform.fatsecret.com/rest/food/v2"
DIARY_URL = "https://platform.fatsecret.com/rest/food-entries/v1"


@dataclass(frozen=True)
class TokenPair:
    token: str
    secret: str


class FatSecretError(RuntimeError):
    pass


def percent(value: object) -> str:
    return quote(str(value), safe="~-._")


def oauth_parameters(
    *,
    consumer_key: str,
    consumer_secret: str,
    method: str,
    url: str,
    request_parameters: Optional[dict[str, object]] = None,
    token: Optional[str] = None,
    token_secret: str = "",
    callback: Optional[str] = None,
    verifier: Optional[str] = None,
    timestamp: Optional[int] = None,
    nonce: Optional[str] = None,
) -> dict[str, str]:
    oauth: dict[str, str] = {
        "oauth_consumer_key": consumer_key,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_version": "1.0",
    }
    if token:
        oauth["oauth_token"] = token
    if callback:
        oauth["oauth_callback"] = callback
    if verifier:
        oauth["oauth_verifier"] = verifier

    signable: list[tuple[str, str]] = []
    for key, value in (request_parameters or {}).items():
        if value is not None:
            signable.append((str(key), str(value)))
    signable.extend(oauth.items())

    encoded_pairs = sorted((percent(k), percent(v)) for k, v in signable)
    normalized = "&".join(f"{key}={value}" for key, value in encoded_pairs)
    base_string = "&".join(
        [method.upper(), percent(url), percent(normalized)]
    )
    signing_key = f"{percent(consumer_secret)}&{percent(token_secret)}"
    digest = hmac.new(
        signing_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode("ascii")
    return oauth


def signed_request(
    *,
    consumer_key: str,
    consumer_secret: str,
    method: str,
    url: str,
    request_parameters: Optional[dict[str, object]] = None,
    token: Optional[str] = None,
    token_secret: str = "",
    callback: Optional[str] = None,
    verifier: Optional[str] = None,
    timeout: int = 20,
) -> requests.Response:
    payload = {
        key: value
        for key, value in (request_parameters or {}).items()
        if value is not None
    }
    oauth = oauth_parameters(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        method=method,
        url=url,
        request_parameters=payload,
        token=token,
        token_secret=token_secret,
        callback=callback,
        verifier=verifier,
    )
    combined = {**payload, **oauth}

    method = method.upper()
    if method == "GET":
        return requests.get(url, params=combined, timeout=timeout)
    if method == "POST":
        return requests.post(
            url,
            data=combined,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )
    if method == "DELETE":
        return requests.delete(url, params=combined, timeout=timeout)
    raise ValueError(f"Unsupported OAuth HTTP method: {method}")


def _response_error(response: requests.Response) -> str:
    body = (response.text or "").strip()
    if len(body) > 300:
        body = body[:300] + "..."
    if body:
        return f"HTTP {response.status_code}: {body}"
    return f"HTTP {response.status_code}"


def _require_ok(response: requests.Response, operation: str) -> None:
    if not response.ok:
        raise FatSecretError(f"{operation}: {_response_error(response)}")


def _json_response(response: requests.Response, operation: str) -> dict[str, Any]:
    _require_ok(response, operation)
    try:
        body = response.json()
    except ValueError as exc:
        raise FatSecretError(f"{operation}: invalid JSON") from exc
    if not isinstance(body, dict):
        raise FatSecretError(f"{operation}: invalid response")
    if "error" in body:
        error = body["error"]
        if isinstance(error, dict):
            raise FatSecretError(
                f"{operation}: API error {error.get('code')}: {error.get('message')}"
            )
        raise FatSecretError(f"{operation}: API error")
    return body


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FatSecretError("FatSecret returned an invalid calorie value") from exc
    if not result.is_finite() or result < 0:
        raise FatSecretError("FatSecret returned an invalid calorie value")
    return result


def create_exact_food(
    *,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    access_token_secret: str,
    name: str,
    calories: float,
    protein: float,
    carbs: float,
    fat: float,
    fiber: float,
    sugar: float,
) -> tuple[str, str]:
    """Create one custom serving containing the entire meal's supplied nutrition."""
    auth = dict(
        consumer_key=consumer_key, consumer_secret=consumer_secret,
        token=access_token, token_secret=access_token_secret,
    )
    body = _json_response(signed_request(
        **auth, method="POST", url=FOOD_CREATE_URL,
        request_parameters={
            "food_name": name, "brand_type": "manufacturer",
            "serving_size": "1 meal", "calories": str(_decimal(calories)),
            "protein": protein, "carbohydrate": carbs, "fat": fat,
            "fiber": fiber, "sugar": sugar, "format": "json",
        },
    ), "FatSecret custom food creation failed (food.create.v2 access required)")
    food_id = body.get("food_id")
    if isinstance(food_id, dict):
        food_id = food_id.get("value")
    if not food_id or not str(food_id).isdigit() or int(food_id) <= 0:
        raise FatSecretError("FatSecret custom food returned no valid food id")
    food_id = str(food_id)
    # User-created foods must be retrieved with the user's OAuth credentials.
    detail = _json_response(signed_request(
        **auth, method="GET", url=FOOD_GET_URL,
        request_parameters={"food_id": food_id, "format": "json"},
    ), "FatSecret custom food lookup failed")
    food = detail.get("food")
    if not isinstance(food, dict) or not isinstance(food.get("servings"), dict):
        raise FatSecretError("FatSecret custom food returned no servings")
    servings = food["servings"].get("serving")
    if isinstance(servings, dict):
        servings = [servings]
    for serving in servings if isinstance(servings, list) else []:
        if not isinstance(serving, dict):
            continue
        serving_id = str(serving.get("serving_id") or "")
        if not serving_id.isdigit() or int(serving_id) <= 0:
            continue
        if (_decimal(serving.get("calories")) == _decimal(calories)
                and _decimal(serving.get("number_of_units")) == 1):
            return food_id, serving_id
    raise FatSecretError("FatSecret custom food has no one-meal serving with exact calories")


def request_token(
    consumer_key: str,
    consumer_secret: str,
    callback_url: str,
) -> TokenPair:
    response = signed_request(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        method="POST",
        url=REQUEST_TOKEN_URL,
        callback=callback_url,
    )
    _require_ok(response, "FatSecret request-token step failed")

    data = parse_qs(response.text)
    token = data.get("oauth_token", [None])[0]
    token_secret = data.get("oauth_token_secret", [None])[0]
    callback_confirmed = data.get("oauth_callback_confirmed", [None])[0]
    if not token or not token_secret:
        raise FatSecretError("FatSecret did not return an OAuth request token and secret")
    if callback_confirmed not in (None, "true", True):
        raise FatSecretError("FatSecret did not confirm the OAuth callback URL")
    return TokenPair(str(token), str(token_secret))


def authorization_url(token: str) -> str:
    return f"{AUTHORIZE_URL}?oauth_token={percent(token)}"


def exchange_access_token(
    consumer_key: str,
    consumer_secret: str,
    request_token_value: str,
    request_token_secret: str,
    verifier: str,
) -> TokenPair:
    response = signed_request(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        method="GET",
        url=ACCESS_TOKEN_URL,
        token=request_token_value,
        token_secret=request_token_secret,
        verifier=verifier,
    )
    _require_ok(response, "FatSecret access-token step failed")

    data = parse_qs(response.text)
    token = data.get("oauth_token", [None])[0]
    token_secret = data.get("oauth_token_secret", [None])[0]
    if not token or not token_secret:
        raise FatSecretError("FatSecret did not return an access token")
    return TokenPair(str(token), str(token_secret))


def find_best_food_match(
    *,
    consumer_key: str,
    consumer_secret: str,
    query: str,
    calories: float,
    protein: float = 0.0,
    carbs: float = 0.0,
    fat: float = 0.0,
    max_search_results: int = 12,
    max_detail_candidates: int = 6,
) -> Optional[FatSecretMatch]:
    query = (query or "").strip()
    if not query:
        return None

    search_response = signed_request(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        method="GET",
        url=SEARCH_URL,
        request_parameters={
            "search_expression": query,
            "max_results": max(1, min(20, int(max_search_results))),
            "page_number": 0,
            "format": "json",
        },
    )
    _require_ok(search_response, "FatSecret food search failed")

    try:
        search_results = search_foods_from_response(search_response.json())
    except (ValueError, TypeError) as exc:
        raise FatSecretError("FatSecret food search returned invalid JSON") from exc
    if not search_results:
        return None

    def detail_fetcher(food_id: str) -> Optional[dict[str, Any]]:
        response = signed_request(
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            method="GET",
            url=FOOD_GET_URL,
            request_parameters={"food_id": food_id, "format": "json"},
        )
        if not response.ok:
            return None
        try:
            return detailed_food_from_response(response.json())
        except (ValueError, TypeError):
            return None

    return choose_best_match(
        query,
        search_results,
        detail_fetcher,
        target_calories=calories,
        target_protein=protein,
        target_carbs=carbs,
        target_fat=fat,
        max_detail_candidates=max(1, min(10, int(max_detail_candidates))),
    )


def delete_diary_entry(
    *,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    access_token_secret: str,
    food_entry_id: str,
) -> None:
    response = signed_request(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        method="DELETE",
        url=DIARY_URL,
        request_parameters={
            "food_entry_id": food_entry_id,
            "format": "json",
        },
        token=access_token,
        token_secret=access_token_secret,
    )
    body = _json_response(response, "FatSecret diary cleanup failed")
    success = body.get("success")
    if isinstance(success, dict):
        success = success.get("value")
    if str(success) != "1":
        raise FatSecretError("FatSecret diary cleanup was not confirmed")


def create_diary_entry(
    *,
    consumer_key: str,
    consumer_secret: str,
    access_token: str,
    access_token_secret: str,
    food_id: str,
    serving_id: str,
    number_of_units: float,
    food_entry_name: str,
    meal: str,
    date_int: int,
    expected_calories: Optional[float] = None,
) -> Optional[str]:
    response = signed_request(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        method="POST",
        url=DIARY_URL,
        request_parameters={
            "food_id": food_id,
            "food_entry_name": food_entry_name,
            "serving_id": serving_id,
            "number_of_units": round(float(number_of_units), 4),
            "meal": meal,
            "date": date_int,
            "format": "json",
        },
        token=access_token,
        token_secret=access_token_secret,
    )
    body = _json_response(response, "FatSecret diary sync failed")
    entries = body.get("food_entries")
    entry = entries.get("food_entry") if isinstance(entries, dict) else None
    if isinstance(entry, list):
        entry = entry[0] if len(entry) == 1 else None
    if not isinstance(entry, dict):
        raise FatSecretError("FatSecret diary write outcome unknown: no unique entry; check diary before retrying")

    entry_id = str(entry.get("food_entry_id") or "") or None
    if not entry_id:
        raise FatSecretError("FatSecret diary write outcome unknown: missing entry id; check diary before retrying")
    try:
        actual_calories = _decimal(entry.get("calories"))
    except FatSecretError:
        actual_calories = None
    if expected_calories is not None and (
        actual_calories is None or actual_calories != _decimal(expected_calories)
    ):
        detail = (
            f"FatSecret entry {entry_id} recorded {actual_calories} kcal; "
            f"expected exactly {expected_calories} kcal"
        )
        try:
            delete_diary_entry(
                consumer_key=consumer_key, consumer_secret=consumer_secret,
                access_token=access_token, access_token_secret=access_token_secret,
                food_entry_id=entry_id,
            )
        except (FatSecretError, requests.RequestException) as exc:
            detail += f"; cleanup unconfirmed ({type(exc).__name__}); check diary before retrying"
        else:
            detail += "; incorrect FatSecret entry was removed"
        raise FatSecretError(detail)

    return entry_id
