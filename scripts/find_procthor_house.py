#!/usr/bin/env python3
"""Find a ProcTHOR-10K house matching room and object-location requirements.

Example:
  $HOME/venv/bin/python scripts/find_procthor_house.py --split train --query '...'
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from typing import Any

from query_procthor_room import _room_contains


FRUITS = {
    "apple",
    "banana",
    "orange",
    "lemon",
    "lime",
    "pear",
    "peach",
    "grape",
    "strawberry",
    "tomato",
}


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _house_rooms(house: dict[str, Any]) -> list[dict[str, Any]]:
    rooms = house.get("rooms", [])
    counts = [Counter() for _ in rooms]

    def visit(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            position = item.get("position")
            object_id = item.get("id")
            asset = item.get("assetId", "")
            labels = set()
            if isinstance(object_id, str):
                labels.add(_norm(object_id.split("|", 1)[0]))
            if isinstance(asset, str):
                labels.add(_norm(re.sub(r"_\d+$", "", asset)))
            if isinstance(position, dict):
                for index, room in enumerate(rooms):
                    if _room_contains(room, position):
                        counts[index].update(labels)
                        break
            visit(item.get("children"))

    visit(house.get("objects", []))
    result = []
    for index, room in enumerate(rooms):
        if isinstance(room.get("id"), str):
            result.append(
                {
                    "room_id": room["id"],
                    "room_type": room.get("roomType", ""),
                    "objects": dict(counts[index]),
                }
            )
    return result


def _has_any(objects: dict[str, int], options: set[str]) -> bool:
    def matches_type(name: str, option: str) -> bool:
        return name == option or (name.startswith(option) and name[len(option) :].isdigit())

    return any(
        count > 0 and any(matches_type(name, option) for option in options)
        for name, count in objects.items()
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def evaluate_requirements(rooms: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for room in rooms:
        by_type.setdefault(_norm(room["room_type"]), []).append(room)

    kitchen = [room for name, group in by_type.items() if "kitchen" in name for room in group]
    living = [
        room
        for name, group in by_type.items()
        if "livingroom" in name or name == "living"
        for room in group
    ]
    bedroom = any("bedroom" in name for name in by_type)
    bathroom = any("bathroom" in name for name in by_type)
    if not (kitchen and living and bedroom and bathroom):
        return {"matches": False}

    kitchen_room = next(
        (
            room
            for room in kitchen
            if _has_any(room["objects"], FRUITS)
            and _has_any(room["objects"], {"fridge", "refrigerator"})
        ),
        None,
    )
    kitchen_sink = bool(
        kitchen_room and _has_any(kitchen_room["objects"], {"sink", "sinkbasin", "kitchensink"})
    )
    living_ok = any(
        _has_any(room["objects"], {"diningtable", "table"})
        and _has_any(room["objects"], {"sofa", "couch"})
        for room in living
    )
    if kitchen_room is None or not living_ok:
        return {"matches": False}
    living_room = next(
        room
        for room in living
        if _has_any(room["objects"], {"diningtable", "table"})
        and _has_any(room["objects"], {"sofa", "couch"})
    )
    return {
        "matches": True,
        "has_kitchen_sink": kitchen_sink,
        "room_ids": {
            "kitchen": kitchen_room["room_id"],
            "living_room": living_room["room_id"],
            "bedrooms": [
                room["room_id"] for room in rooms if "bedroom" in _norm(room["room_type"])
            ],
            "bathrooms": [
                room["room_id"] for room in rooms if "bathroom" in _norm(room["room_type"])
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="train", help="ProcTHOR-10K split (default: train).")
    parser.add_argument(
        "--query", required=True, help="Natural-language description of the desired house."
    )
    parser.add_argument(
        "--model", default="gpt-6-luna", help="OpenAI model used to select among matching houses."
    )
    parser.add_argument(
        "--max-candidates",
        type=_positive_int,
        default=30,
        help="Maximum matching houses sent to the model.",
    )
    args = parser.parse_args()

    try:
        import prior
        from openai import OpenAI

        dataset = prior.load_dataset("procthor-10k")
        houses = dataset[args.split]
        matching = []
        for index, house in enumerate(houses):
            if not isinstance(house, dict) and isinstance(getattr(house, "data", None), dict):
                house = house.data
            rooms = _house_rooms(house)
            criteria = evaluate_requirements(rooms)
            if criteria["matches"]:
                matching.append({"house_index": index, "rooms": rooms, "criteria": criteria})
        if not matching:
            raise RuntimeError(
                f"No houses in split {args.split!r} had the requested room layout, kitchen items, "
                "and living-room furniture."
            )

        exact = [item for item in matching if item["criteria"]["has_kitchen_sink"]]
        pool = exact or matching
        candidates = pool[: args.max_candidates]
        model_candidates = [
            {
                "house_index": item["house_index"],
                "rooms": [
                    {
                        "room_id": room["room_id"],
                        "room_type": room["room_type"],
                        "object_counts": room["objects"],
                    }
                    for room in item["rooms"]
                ],
                "has_kitchen_sink": item["criteria"]["has_kitchen_sink"],
            }
            for item in candidates
        ]
        response = OpenAI().responses.create(
            model=args.model,
            instructions=(
                "Choose the ProcTHOR house that best matches the user's natural-language request. "
                "Candidates have the requested room layout, fruit and fridge in a kitchen, and table and sofa in a living room. "
                "Prefer a candidate with a kitchen sink if available. If none has one, choose the best match and do not claim that it does. "
                "Return only one supplied house_index. Explain briefly in Japanese."
            ),
            input=(
                f"ユーザーの指定: {args.query}\n"
                f"条件に合う家候補 (JSON): {json.dumps(model_candidates, ensure_ascii=False, separators=(',', ':'))}"
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "procthor_house_selection",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "house_index": {"type": "integer"},
                            "reason": {"type": "string"},
                        },
                        "required": ["house_index", "reason"],
                        "additionalProperties": False,
                    },
                }
            },
        )
        selection = json.loads(response.output_text)
        chosen = next(
            (item for item in candidates if item["house_index"] == selection["house_index"]), None
        )
        if chosen is None:
            raise RuntimeError("Model selected a house outside the supplied candidates.")
        print(
            json.dumps(
                {
                    "split": args.split,
                    "house_index": chosen["house_index"],
                    "room_ids": chosen["criteria"]["room_ids"],
                    "rooms": [
                        {
                            "room_id": room["room_id"],
                            "room_type": room["room_type"],
                            "matching_objects": {
                                name: count
                                for name, count in room["objects"].items()
                                if any(
                                    token in name
                                    for token in (
                                        "apple",
                                        "banana",
                                        "orange",
                                        "lemon",
                                        "lime",
                                        "pear",
                                        "peach",
                                        "grape",
                                        "strawberry",
                                        "tomato",
                                        "fridge",
                                        "refrigerator",
                                        "sink",
                                        "diningtable",
                                        "table",
                                        "sofa",
                                        "couch",
                                    )
                                )
                            },
                        }
                        for room in chosen["rooms"]
                        if room["room_id"]
                        in set(
                            chosen["criteria"]["room_ids"]["bedrooms"]
                            + chosen["criteria"]["room_ids"]["bathrooms"]
                            + [
                                chosen["criteria"]["room_ids"]["kitchen"],
                                chosen["criteria"]["room_ids"]["living_room"],
                            ]
                        )
                    ],
                    "matching_house_count": len(matching),
                    "exact_match": chosen["criteria"]["has_kitchen_sink"],
                    "criteria": chosen["criteria"],
                    "reason": selection["reason"],
                    "model": args.model,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
