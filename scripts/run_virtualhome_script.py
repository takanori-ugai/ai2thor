#!/usr/bin/env python3
"""Execute a small VirtualHome-style action script in an AI2-THOR scene.

Example:
  python scripts/run_virtualhome_script.py script.txt --scene FloorPlan1 --headless

This is an integration harness for the actions listed in
doc/virtualhome-action-porting-plan.md. VirtualHome object IDs are resolved by
matching object types; they are not assumed to be AI2-THOR object IDs.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

ACTION_RE = re.compile(r"^\s*<(?P<agent>[^>]+)>\s+\[(?P<action>[A-Z_]+)\]\s+(?P<args>.+?)\s*$")
ARG_RE = re.compile(r"<(?P<name>[^>]+)>\s*\((?P<id>[^)]+)\)")
SUPPORTED = {
    "WALK", "GRAB", "OPEN", "CLOSE", "PUTIN", "PUTON", "FIND",
    "TURNTO", "LOOKAT_LONG", "TOUCH", "PLUGIN",
}
TYPE_ALIASES = {
    "wine": {"wine", "winebottle"},
    "tablelamp": {"tablelamp", "desklamp"},
    "washingmachine": {"washingmachine"},
    "sofa": {"sofa", "couch"},
    "tv": {"tv", "television"},
    "book": {"book"},
    "door": {"door"},
}


@dataclass
class ScriptAction:
    line_number: int
    source: str
    agent: str
    verb: str
    objects: List[tuple[str, str]]


def parse_script(source: str) -> List[ScriptAction]:
    actions = []
    for line_number, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        match = ACTION_RE.match(line)
        if not match:
            raise ValueError(f"line {line_number}: invalid VirtualHome action syntax: {raw}")
        verb = match.group("action")
        if verb not in SUPPORTED:
            raise ValueError(f"line {line_number}: unsupported action [{verb}]")
        objects = [(m.group("name"), m.group("id")) for m in ARG_RE.finditer(match.group("args"))]
        if not objects:
            raise ValueError(f"line {line_number}: invalid VirtualHome object argument syntax: {raw}")
        expected = 2 if verb in {"PUTIN", "PUTON"} else 1
        if len(objects) != expected:
            raise ValueError(f"line {line_number}: [{verb}] expects {expected} object argument(s), got {len(objects)}")
        # Reject trailing/unparsed tokens instead of silently executing a partial line.
        residue = ARG_RE.sub("", match.group("args")).strip()
        if residue:
            raise ValueError(f"line {line_number}: malformed object argument(s): {residue}")
        actions.append(ScriptAction(line_number, raw, match.group("agent"), verb, objects))
    return actions


def normalize_type(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class VirtualHomeRunner:
    def __init__(self, controller, variables: Dict[str, str], object_mappings: Dict[str, str], continue_on_error: bool):
        self.controller = controller
        self.variables = variables
        self.object_mappings = object_mappings
        self.continue_on_error = continue_on_error

    def resolve(self, name: str, identifier: str) -> dict:
        if identifier.startswith("$"):
            if identifier not in self.variables:
                raise RuntimeError(f"undefined variable {identifier}")
            identifier = self.variables[identifier]
        mapped_id = self.object_mappings.get(identifier)
        if mapped_id:
            identifier = mapped_id
        objects = self.controller.last_event.metadata.get("objects", [])
        # Prefer explicit AI2-THOR objectId, then map VirtualHome class names to objectType.
        by_id = [o for o in objects if o.get("objectId") == identifier]
        if by_id:
            return by_id[0]
        accepted_types = TYPE_ALIASES.get(normalize_type(name), {normalize_type(name)})
        matches = [o for o in objects if normalize_type(o.get("objectType", "")) in accepted_types]
        # VirtualHome IDs are scene-local and must not be guessed as AI2-THOR IDs.
        if len(matches) == 1:
            return matches[0]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected one {name!r} object (VirtualHome id {identifier!r}), found {len(matches)}; "
                "map the VirtualHome ID to an AI2-THOR objectId with --object-map ID=OBJECT_ID"
            )

    def step(self, action: str, **kwargs):
        event = self.controller.step(action=action, **kwargs)
        if not event.metadata.get("lastActionSuccess", False):
            raise RuntimeError(event.metadata.get("errorMessage") or f"{action} failed")
        return event

    def walk_to(self, obj: dict):
        # Find a route on the reachable grid and execute it with ordinary movement actions.
        reachable = self.step("GetReachablePositions").metadata.get("actionReturn") or []
        target = obj.get("position", {})
        if not reachable:
            raise RuntimeError("GetReachablePositions returned no positions")

        grid = 0.25
        def key(p):
            return (round(p["x"] / grid), round(p["z"] / grid))

        by_key = {key(p): p for p in reachable}
        agent_pos = self.controller.last_event.metadata["agent"]["position"]
        start = min(by_key, key=lambda k: math.hypot(by_key[k]["x"] - agent_pos["x"], by_key[k]["z"] - agent_pos["z"]))
        goals = {
            k for k, p in by_key.items()
            if math.hypot(p["x"] - target["x"], p["z"] - target["z"]) <= 0.75
        }
        if not goals:
            raise RuntimeError("no reachable position within 0.75m of target")

        queue = deque([start])
        previous = {start: None}
        directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        destination = None
        while queue:
            current = queue.popleft()
            if current in goals:
                destination = current
                break
            for dx, dz in directions:
                neighbor = (current[0] + dx, current[1] + dz)
                if neighbor in by_key and neighbor not in previous:
                    previous[neighbor] = current
                    queue.append(neighbor)
        if destination is None:
            raise RuntimeError("target is not reachable from the agent")

        path = []
        cursor = destination
        while cursor != start:
            path.append(cursor)
            cursor = previous[cursor]
        path.reverse()
        yaw_for = {(0, 1): 0, (1, 0): 90, (0, -1): 180, (-1, 0): 270}
        for here, there in zip([start] + path[:-1], path):
            delta = (there[0] - here[0], there[1] - here[1])
            desired_yaw = yaw_for[delta]
            current_yaw = self.controller.last_event.metadata["agent"]["rotation"]["y"]
            delta = (desired_yaw - current_yaw + 180) % 360 - 180
            if abs(delta) > 1:
                self.step("RotateRight" if delta > 0 else "RotateLeft", degrees=abs(delta))
            self.step("MoveAhead", moveMagnitude=grid)

    def execute(self, action: ScriptAction) -> dict:
        if action.agent != "char0":
            raise RuntimeError(f"only the primary iTHOR agent is supported, got <{action.agent}>")
        target = self.resolve(*action.objects[0])
        verb = action.verb
        if verb == "FIND":
            return {"objectId": target["objectId"], "objectType": target["objectType"], "visible": target.get("visible")}
        if verb == "WALK":
            self.walk_to(target)
        elif verb in ("TURNTO", "LOOKAT_LONG"):
            agent = self.controller.last_event.metadata["agent"]
            p, q = agent["position"], target["position"]
            yaw = math.degrees(math.atan2(q["x"] - p["x"], q["z"] - p["z"])) % 360
            current_yaw = agent["rotation"]["y"]
            delta = (yaw - current_yaw + 180) % 360 - 180
            if abs(delta) > 1:
                self.step("RotateRight" if delta > 0 else "RotateLeft", degrees=abs(delta))
            if verb == "LOOKAT_LONG" and not any(
                o.get("objectId") == target["objectId"] and o.get("visible")
                for o in self.controller.last_event.metadata.get("objects", [])
            ):
                raise RuntimeError("target is not visible after turning toward it")
        elif verb == "GRAB":
            agent = self.controller.last_event.metadata["agent"]
            p, q = agent["position"], target["position"]
            yaw = math.degrees(math.atan2(q["x"] - p["x"], q["z"] - p["z"])) % 360
            current_yaw = agent["rotation"]["y"]
            delta = (yaw - current_yaw + 180) % 360 - 180
            if abs(delta) > 1:
                self.step("RotateRight" if delta > 0 else "RotateLeft", degrees=abs(delta))
            self.step("PickupObject", objectId=target["objectId"])
        elif verb in ("OPEN", "CLOSE"):
            self.step("OpenObject" if verb == "OPEN" else "CloseObject", objectId=target["objectId"], forceAction=True)
        elif verb in ("PUTIN", "PUTON"):
            item = self.resolve(*action.objects[1])
            inventory = self.controller.last_event.metadata.get("inventoryObjects", [])
            if not inventory:
                raise RuntimeError(f"[{verb}] requires a held object")
            held = inventory[0]
            if held["objectId"] != item["objectId"]:
                raise RuntimeError(f"held object {held['objectId']} does not match script item {item['objectId']}")
            if verb == "PUTON" and not target.get("receptacle", False):
                raise RuntimeError(f"{target['objectType']} is not a placement surface/receptacle")
            # PutObject.objectId names the destination receptacle; the held item
            # is implicit in AI2-THOR's API.
            self.step("PutObject", objectId=target["objectId"])
        elif verb == "TOUCH":
            # Touch has no generic state-changing equivalent: approach the target
            # and report the contact intent without changing its state.
            self.walk_to(target)
            agent_position = self.controller.last_event.metadata["agent"]["position"]
            target_distance = math.hypot(
                agent_position["x"] - target["position"]["x"],
                agent_position["z"] - target["position"]["z"],
            )
            if target_distance > 1.0:
                raise RuntimeError(f"target remains out of touch range ({target_distance:.2f}m)")
            return {"objectId": target["objectId"], "objectType": target["objectType"], "contact": "approximated by close approach"}
        elif verb == "PLUGIN":
            # iTHOR residential scenes do not expose a plug/socket relationship.
            # For toggleable lamps, emulate the useful end state (powered on).
            if "lamp" not in normalize_type(target.get("objectType", "")) and not target.get("toggleable"):
                raise RuntimeError("PLUGIN target is not a toggleable lamp; this scene has no plug/socket model")
            if not target.get("isToggled", False):
                self.step("ToggleObjectOn", objectId=target["objectId"], forceAction=True)
            return {"objectId": target["objectId"], "objectType": target["objectType"], "plugin": "emulated by turning lamp on (iTHOR has no outlet connection state)"}
        return {"objectId": target["objectId"], "objectType": target["objectType"]}

    def run(self, actions: List[ScriptAction]) -> List[dict]:
        results = []
        for action in actions:
            result = {"line": action.line_number, "action": action.source, "success": False}
            try:
                result["result"] = self.execute(action)
                result["success"] = True
            except Exception as exc:  # report per-line failure for the integration test
                result["error"] = str(exc)
                results.append(result)
                print(f"line {action.line_number}: FAILED: {exc}", file=sys.stderr)
                if not self.continue_on_error:
                    break
            else:
                results.append(result)
                print(f"line {action.line_number}: OK: {action.verb} -> {result['result']}")
        return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", type=Path, help="UTF-8 file with one VirtualHome action per line")
    parser.add_argument("--scene", default="FloorPlan1", help="iTHOR living scene (e.g. FloorPlan1 through FloorPlan30)")
    parser.add_argument("--headless", action="store_true", help="run Unity without a display")
    parser.add_argument("--continue-on-error", action="store_true", help="continue after a failed line")
    parser.add_argument("--variable", action="append", default=[], metavar="NAME=VALUE", help="resolve script variables, e.g. WINE_ID=0")
    parser.add_argument("--object-map", action="append", default=[], metavar="VH_ID=OBJECT_ID", help="map a VirtualHome numeric ID to an AI2-THOR objectId")
    parser.add_argument("--json-out", type=Path, help="write per-line results as JSON")
    args = parser.parse_args()
    try:
        actions = parse_script(args.script.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    variables = {}
    for item in args.variable:
        if "=" not in item:
            parser.error(f"invalid --variable {item!r}; expected NAME=VALUE")
        name, value = item.split("=", 1)
        variables["$" + name.lstrip("$")] = value

    object_mappings = {}
    for item in args.object_map:
        if "=" not in item:
            parser.error(f"invalid --object-map {item!r}; expected VH_ID=OBJECT_ID")
        vh_id, thor_id = item.split("=", 1)
        object_mappings[vh_id] = thor_id

    import ai2thor.controller

    controller = ai2thor.controller.Controller(scene=args.scene, headless=args.headless)
    try:
        results = VirtualHomeRunner(controller, variables, object_mappings, args.continue_on_error).run(actions)
    finally:
        controller.stop()
    if args.json_out:
        import json
        args.json_out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(r["success"] for r in results) and len(results) == len(actions) else 1


if __name__ == "__main__":
    raise SystemExit(main())
