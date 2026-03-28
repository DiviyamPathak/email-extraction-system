from __future__ import annotations

import json
from pathlib import Path
from typing import Any


EVALUATED_FIELDS = [
    "product_line",
    "origin_port_code",
    "origin_port_name",
    "destination_port_code",
    "destination_port_name",
    "incoterm",
    "cargo_weight_kg",
    "cargo_cbm",
    "is_dangerous",
]


def normalize_string(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.strip().lower().split())
    return value


def normalize_float(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 2)
    return value


def values_match(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    left = normalize_float(normalize_string(left))
    right = normalize_float(normalize_string(right))
    return left == right


def load_json(path: str) -> list[dict[str, Any]]:
    return json.loads(Path(path).read_text())


def evaluate(predictions: list[dict[str, Any]], truth: list[dict[str, Any]]) -> dict[str, float]:
    truth_by_id = {row["id"]: row for row in truth}
    total_fields = 0
    total_correct = 0
    metrics: dict[str, float] = {}

    for field in EVALUATED_FIELDS:
        field_total = 0
        field_correct = 0
        for row in predictions:
            expected = truth_by_id[row["id"]]
            field_total += 1
            if values_match(row.get(field), expected.get(field)):
                field_correct += 1
        metrics[field] = field_correct / field_total if field_total else 0.0
        total_fields += field_total
        total_correct += field_correct

    metrics["overall_accuracy"] = total_correct / total_fields if total_fields else 0.0
    return metrics


def main() -> None:
    predictions = load_json("output.json")
    truth = load_json("ground_truth.json")
    metrics = evaluate(predictions, truth)

    print("Accuracy metrics")
    for field in EVALUATED_FIELDS:
        print(f"- {field}: {metrics[field]:.2%}")
    print(f"- overall_accuracy: {metrics['overall_accuracy']:.2%}")


if __name__ == "__main__":
    main()
