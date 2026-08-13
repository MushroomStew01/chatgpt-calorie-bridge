from app.fatsecret_match import choose_best_match, name_similarity


def test_name_similarity_prefers_matching_food():
    matching = name_similarity("beef macaroni bowl", "Macaroni with Beef", "")
    unrelated = name_similarity("beef macaroni bowl", "Vanilla Ice Cream", "")
    assert matching > unrelated
    assert matching > 0.5


def test_generic_food_scales_serving_to_estimated_calories():
    search_results = [
        {
            "food_id": "100",
            "food_name": "Macaroni with Beef",
            "food_type": "Generic",
        },
        {
            "food_id": "200",
            "food_name": "Macaroni and Cheese",
            "food_type": "Generic",
        },
    ]

    details = {
        "100": {
            "food_id": "100",
            "food_name": "Macaroni with Beef",
            "food_type": "Generic",
            "servings": {
                "serving": [
                    {
                        "serving_id": "10",
                        "serving_description": "1 cup",
                        "number_of_units": "1",
                        "metric_serving_amount": "230",
                        "metric_serving_unit": "g",
                        "calories": "350",
                        "protein": "18",
                        "carbohydrate": "38",
                        "fat": "14",
                    }
                ]
            },
        },
        "200": {
            "food_id": "200",
            "food_name": "Macaroni and Cheese",
            "food_type": "Generic",
            "servings": {
                "serving": [
                    {
                        "serving_id": "20",
                        "serving_description": "1 cup",
                        "number_of_units": "1",
                        "calories": "310",
                        "protein": "10",
                        "carbohydrate": "44",
                        "fat": "11",
                    }
                ]
            },
        },
    }

    match = choose_best_match(
        "beef macaroni bowl",
        search_results,
        lambda food_id: details[food_id],
        target_calories=700,
        target_protein=36,
        target_carbs=76,
        target_fat=28,
    )

    assert match is not None
    assert match.food_id == "100"
    assert match.serving_id == "10"
    assert match.number_of_units == 2.0
    assert match.predicted_calories == 700.0
    assert match.predicted_protein == 36.0


def test_derived_serving_id_zero_is_not_selected():
    search_results = [
        {
            "food_id": "300",
            "food_name": "Chicken Breast",
            "food_type": "Brand",
        }
    ]
    detail = {
        "food_id": "300",
        "food_name": "Chicken Breast",
        "food_type": "Brand",
        "servings": {
            "serving": [
                {
                    "serving_id": "0",
                    "serving_description": "100 g",
                    "number_of_units": "1",
                    "calories": "165",
                    "protein": "31",
                    "carbohydrate": "0",
                    "fat": "3.6",
                },
                {
                    "serving_id": "31",
                    "serving_description": "1 serving",
                    "number_of_units": "1",
                    "calories": "180",
                    "protein": "32",
                    "carbohydrate": "0",
                    "fat": "4",
                },
            ]
        },
    }

    match = choose_best_match(
        "chicken breast",
        search_results,
        lambda _: detail,
        target_calories=180,
        target_protein=32,
        target_carbs=0,
        target_fat=4,
    )

    assert match is not None
    assert match.serving_id == "31"
