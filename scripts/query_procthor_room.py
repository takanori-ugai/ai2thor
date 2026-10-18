#!/usr/bin/env python3
"""Resolve natural language to a room ID in a ProcTHOR-10K house.

Example:
  $HOME/venv/bin/python scripts/query_procthor_room.py \
      --query 'ベッドがある寝室' --split train --index 0

The OpenAI API chooses only among room IDs present in the selected house.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from typing import Any

try:
    from scripts.run_virtualhome_script import load_procthor_house
except ModuleNotFoundError:  # Allow running this file directly from scripts/.
    from run_virtualhome_script import load_procthor_house


def _point_in_polygon(x: float, z: float, polygon: list[dict[str, Any]]) -> bool:
    """Return whether an x/z point is inside a ProcTHOR floor polygon."""
    inside = False
    if len(polygon) < 3:
        return False
    previous = polygon[-1]
    for current in polygon:
        x1, z1 = float(previous.get("x", 0)), float(previous.get("z", 0))
        x2, z2 = float(current.get("x", 0)), float(current.get("z", 0))
        if (z1 > z) != (z2 > z) and x < (x2 - x1) * (z - z1) / ((z2 - z1) or 1e-12) + x1:
            inside = not inside
        previous = current
    return inside


def _room_contains(room: dict[str, Any], position: dict[str, Any]) -> bool:
    try:
        return _point_in_polygon(
            float(position["x"]), float(position["z"]), room.get("floorPolygon", [])
        )
    except (KeyError, TypeError, ValueError):
        return False


def _asset_name(asset_id: str | None) -> str | None:
    if not asset_id:
        return None
    return re.sub(r"_\d+$", "", asset_id).replace("_", " ")


def collect_room_candidates(house: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe each room by its ID, type, and the object types in its floor area."""
    rooms = house.get("rooms", [])
    object_counts: list[Counter[str]] = [Counter() for _ in rooms]

    def visit(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            position = item.get("position")
            object_id = item.get("id")
            if isinstance(position, dict) and isinstance(object_id, str):
                for index, room in enumerate(rooms):
                    if _room_contains(room, position):
                        object_counts[index][object_id.split("|", 1)[0]] += 1
                        break
            visit(item.get("children"))

    visit(house.get("objects", []))
    candidates = []
    for index, room in enumerate(rooms):
        room_id = room.get("id")
        if not isinstance(room_id, str) or not room_id:
            continue
        polygon = room.get("floorPolygon", [])
        center = None
        if polygon:
            center = {
                "x": round(sum(float(p.get("x", 0)) for p in polygon) / len(polygon), 2),
                "z": round(sum(float(p.get("z", 0)) for p in polygon) / len(polygon), 2),
            }
        candidates.append(
            {
                "room_id": room_id,
                "room_type": room.get("roomType"),
                "objects": dict(object_counts[index].most_common()),
                "approximate_center": center,
            }
        )
    return candidates


def choose_room(query: str, candidates: list[dict[str, Any]], model: str) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI SDK is missing. Install dependencies with "
            "'$HOME/venv/bin/pip install -r requirements-procthor-openai.txt'."
        ) from exc
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Set OPENAI_API_KEY in the environment before running this command.")

    response = OpenAI().responses.create(
        model=model,
        instructions=(
            "Select the single room that best matches the user's description. Use only the "
            "supplied room candidates. Consider room type and contained object types to "
            "distinguish rooms with the same type. If none reasonably matches, return "
            "room_id null. Never invent or alter IDs. Explain briefly in Japanese."
        ),
        input=(
            f"ユーザーの指定: {query}\n"
            f"ProcTHORの部屋候補 (JSON): {json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}"
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "procthor_room_selection",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": ["string", "null"]},
                        "reason": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["room_id", "reason", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
    )
    try:
        selection = json.loads(response.output_text)
    except (AttributeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenAI returned no valid structured selection.") from exc

    candidates_by_id = {candidate["room_id"]: candidate for candidate in candidates}
    room_id = selection.get("room_id")
    if room_id is not None and room_id not in candidates_by_id:
        raise RuntimeError(f"Model returned a room ID not present in this house: {room_id!r}")
    return {
        "room_id": room_id,
        "room": candidates_by_id.get(room_id),
        "reason": selection.get("reason", ""),
        "confidence": selection.get("confidence"),
        "candidate_count": len(candidates),
        "model": model,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query", required=True, help="Natural-language room description (Japanese or English)."
    )
    parser.add_argument("--split", default="train", help="ProcTHOR-10K split (default: train).")
    parser.add_argument(
        "--index", type=int, default=0, help="Zero-based house index in the split (default: 0)."
    )
    parser.add_argument("--model", default="gpt-6-luna", help="OpenAI model (default: gpt-6-luna).")
    args = parser.parse_args()

    try:
        house = load_procthor_house(args.split, args.index)
        candidates = collect_room_candidates(house)
        if not candidates:
            raise RuntimeError("No room IDs were found in the selected ProcTHOR house.")
        result = choose_room(args.query, candidates, args.model)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
