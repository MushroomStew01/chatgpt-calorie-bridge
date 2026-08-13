from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
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
    _require_ok(response, "FatSecret diary cleanup failed")


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
    max_calorie_error_ratio: float = 0.15,
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
    _require_ok(response, "FatSecret diary sync failed")

    try:
        body = response.json()
    except ValueError as exc:
        raise FatSecretError("FatSecret diary sync returned invalid JSON") from exc

    entry = body.get("food_entries", {}).get("food_entry")
    if isinstance(entry, list):
        entry = entry[0] if entry else None
    if not isinstance(entry, dict):
        return None

    entry_id = str(entry.get("food_entry_id") or "") or None
    actual_calories = 0.0
    try:
        actual_calories = float(entry.get("calories") or 0)
    except (TypeError, ValueError):
        actual_calories = 0.0

    # FatSecret returns the calories it actually recorded. Validate that value
    # against the ChatGPT/photo estimate so a bad match can never silently turn
    # a 950 kcal meal into a 260 kcal diary entry again.
    if expected_calories and expected_calories > 0 and actual_calories > 0:
        error_ratio = abs(actual_calories - expected_calories) / expected_calories
        if error_ratio > max_calorie_error_ratio:
            cleanup_error = None
            if entry_id:
                try:
                    delete_diary_entry(
                        consumer_key=consumer_key,
                        consumer_secret=consumer_secret,
                        access_token=access_token,
                        access_token_secret=access_token_secret,
                        food_entry_id=entry_id,
                    )
                except FatSecretError as exc:
                    cleanup_error = str(exc)

            detail = (
                f"FatSecret recorded {actual_calories:.0f} kcal for a "
                f"{expected_calories:.0f} kcal estimate"
            )
            if cleanup_error:
                detail += f"; automatic cleanup also failed: {cleanup_error}"
            else:
                detail += "; incorrect FatSecret entry was removed"
            raise FatSecretError(detail)

    return entry_id
