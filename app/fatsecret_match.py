from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable, Optional


FILLER_WORDS = {
    "a",
    "an",
    "and",
    "bowl",
    "dish",
    "food",
    "homemade",
    "meal",
    "of",
    "plate",
    "the",
    "with",
}


@dataclass(frozen=True)
class FatSecretMatch:
    food_id: str
    serving_id: str
    number_of_units: float
    food_name: str
    brand_name: str
    food_type: str
    serving_description: str
    score: float
    predicted_calories: float
    predicted_protein: float
    predicted_carbs: float
    predicted_fat: float

    @property
    def display_name(self) -> str:
        if self.brand_name:
            return f"{self.brand_name} {self.food_name}".strip()
        return self.food_name


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tokens(value: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", (value or "").lower())
    return [token for token in tokens if token not in FILLER_WORDS]


def name_similarity(query: str, food_name: str, brand_name: str = "") -> float:
    query_tokens = _tokens(query)
    candidate_tokens = _tokens(f"{brand_name} {food_name}")
    if not query_tokens or not candidate_tokens:
        return 0.0

    query_set = set(query_tokens)
    candidate_set = set(candidate_tokens)
    overlap = len(query_set & candidate_set)

    coverage = overlap / max(len(query_set), 1)
    precision = overlap / max(len(candidate_set), 1)
    f1 = (
        2 * coverage * precision / (coverage + precision)
        if coverage + precision
        else 0.0
    )
    sequence = SequenceMatcher(
        None,
        " ".join(query_tokens),
        " ".join(candidate_tokens),
    ).ratio()

    return max(0.0, min(1.0, 0.55 * coverage + 0.25 * f1 + 0.20 * sequence))


def _relative_similarity(predicted: float, target: float, floor: float) -> float:
    if target <= 0:
        return 1.0
    relative_error = abs(predicted - target) / max(abs(target), floor)
    return math.exp(-1.8 * relative_error)


def nutrition_similarity(
    predicted_calories: float,
    predicted_protein: float,
    predicted_carbs: float,
    predicted_fat: float,
    target_calories: float,
    target_protein: float,
    target_carbs: float,
    target_fat: float,
) -> float:
    scores: list[tuple[float, float]] = []

    if target_calories > 0:
        scores.append(
            (
                0.45,
                _relative_similarity(predicted_calories, target_calories, 80.0),
            )
        )
    if target_protein > 0:
        scores.append((0.20, _relative_similarity(predicted_protein, target_protein, 8.0)))
    if target_carbs > 0:
        scores.append((0.20, _relative_similarity(predicted_carbs, target_carbs, 12.0)))
    if target_fat > 0:
        scores.append((0.15, _relative_similarity(predicted_fat, target_fat, 6.0)))

    if not scores:
        return 0.5

    total_weight = sum(weight for weight, _ in scores)
    return sum(weight * score for weight, score in scores) / total_weight


def _servings_from_food(food: dict[str, Any]) -> list[dict[str, Any]]:
    servings = food.get("servings", {}) if isinstance(food, dict) else {}
    if not isinstance(servings, dict):
        return []
    return [serving for serving in as_list(servings.get("serving")) if isinstance(serving, dict)]


def _best_serving_for_food(
    food: dict[str, Any],
    target_calories: float,
    target_protein: float,
    target_carbs: float,
    target_fat: float,
) -> Optional[tuple[dict[str, Any], float, float, float, float, float, float]]:
    food_type = str(food.get("food_type") or "")
    best: Optional[tuple[dict[str, Any], float, float, float, float, float, float]] = None

    for serving in _servings_from_food(food):
        serving_id = str(serving.get("serving_id") or "")
        if not serving_id or serving_id == "0":
            # FatSecret documents serving_id=0 as a derived serving that cannot be
            # used by food_entry.create.
            continue

        base_calories = safe_float(serving.get("calories"))
        if base_calories <= 0:
            continue

        base_units = safe_float(serving.get("number_of_units"), 1.0)
        if base_units <= 0:
            base_units = 1.0

        is_generic = food_type.lower() == "generic"
        if is_generic and target_calories > 0:
            scale = target_calories / base_calories
            number_of_units = base_units * scale
        else:
            # FatSecret documents Brand foods as number_of_units=1. Choose the
            # closest available serving rather than scaling a brand serving.
            scale = 1.0
            number_of_units = 1.0

        if number_of_units <= 0:
            continue

        predicted_calories = base_calories * scale
        predicted_protein = safe_float(serving.get("protein")) * scale
        predicted_carbs = safe_float(serving.get("carbohydrate")) * scale
        predicted_fat = safe_float(serving.get("fat")) * scale

        nutrition_score = nutrition_similarity(
            predicted_calories,
            predicted_protein,
            predicted_carbs,
            predicted_fat,
            target_calories,
            target_protein,
            target_carbs,
            target_fat,
        )

        # Prefer intuitive portions when nutrition fit is otherwise similar.
        closeness_to_one = math.exp(-0.18 * abs(math.log(max(scale, 0.01))))
        metric_bonus = 1.0 if str(serving.get("metric_serving_unit") or "").lower() in {"g", "ml", "oz"} else 0.0
        serving_score = 0.86 * nutrition_score + 0.10 * closeness_to_one + 0.04 * metric_bonus

        candidate = (
            serving,
            number_of_units,
            predicted_calories,
            predicted_protein,
            predicted_carbs,
            predicted_fat,
            serving_score,
        )
        if best is None or candidate[-1] > best[-1]:
            best = candidate

    return best


def search_foods_from_response(body: dict[str, Any]) -> list[dict[str, Any]]:
    foods = body.get("foods", {}) if isinstance(body, dict) else {}
    if not isinstance(foods, dict):
        return []
    return [food for food in as_list(foods.get("food")) if isinstance(food, dict)]


def detailed_food_from_response(body: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(body, dict):
        return None
    food = body.get("food")
    return food if isinstance(food, dict) else None


def choose_best_match(
    query: str,
    search_results: Iterable[dict[str, Any]],
    detail_fetcher: Callable[[str], Optional[dict[str, Any]]],
    *,
    target_calories: float,
    target_protein: float = 0.0,
    target_carbs: float = 0.0,
    target_fat: float = 0.0,
    max_detail_candidates: int = 6,
) -> Optional[FatSecretMatch]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for result in search_results:
        food_id = str(result.get("food_id") or "")
        if not food_id:
            continue
        food_name = str(result.get("food_name") or "")
        brand_name = str(result.get("brand_name") or "")
        similarity = name_similarity(query, food_name, brand_name)
        ranked.append((similarity, result))

    ranked.sort(key=lambda item: item[0], reverse=True)
    best: Optional[FatSecretMatch] = None

    for name_score, result in ranked[:max_detail_candidates]:
        food_id = str(result.get("food_id") or "")
        detailed = detail_fetcher(food_id)
        if not detailed:
            continue

        serving_match = _best_serving_for_food(
            detailed,
            target_calories,
            target_protein,
            target_carbs,
            target_fat,
        )
        if not serving_match:
            continue

        (
            serving,
            number_of_units,
            predicted_calories,
            predicted_protein,
            predicted_carbs,
            predicted_fat,
            serving_score,
        ) = serving_match

        food_type = str(detailed.get("food_type") or result.get("food_type") or "")
        generic_bonus = 0.04 if food_type.lower() == "generic" else 0.0
        total_score = min(1.0, 0.58 * name_score + 0.38 * serving_score + generic_bonus)

        candidate = FatSecretMatch(
            food_id=str(detailed.get("food_id") or food_id),
            serving_id=str(serving.get("serving_id") or ""),
            number_of_units=round(number_of_units, 4),
            food_name=str(detailed.get("food_name") or result.get("food_name") or query),
            brand_name=str(detailed.get("brand_name") or result.get("brand_name") or ""),
            food_type=food_type,
            serving_description=str(serving.get("serving_description") or "serving"),
            score=round(total_score, 4),
            predicted_calories=round(predicted_calories, 1),
            predicted_protein=round(predicted_protein, 1),
            predicted_carbs=round(predicted_carbs, 1),
            predicted_fat=round(predicted_fat, 1),
        )
        if best is None or candidate.score > best.score:
            best = candidate

    return best
