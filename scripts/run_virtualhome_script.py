#!/usr/bin/env python3
"""Execute a small VirtualHome-style action script in an AI2-THOR scene.

Examples:
  python scripts/run_virtualhome_script.py script.txt --scene FloorPlan1 --headless
  python scripts/run_virtualhome_script.py script.txt --procthor-split train --procthor-index 0

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


class FrameRecorder:
    """Write captured AI2-THOR RGB frames to an MP4 file."""

    def __init__(self, output_path: Path, fps: float, show_window: bool = False):
        self.output_path = output_path
        self.fps = fps
        self.preview = X11Preview() if show_window else None
        self.writer = None
        self.frame_count = 0

    def append(self, frame):
        if frame is None:
            return
        import cv2

        height, width = frame.shape[:2]
        if self.writer is None:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self.writer = cv2.VideoWriter(str(self.output_path), fourcc, self.fps, (width, height))
            if not self.writer.isOpened():
                raise RuntimeError(f"could not create video file: {self.output_path}")
        bgr_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        self.writer.write(bgr_frame)
        if self.preview is not None:
            self.preview.show(frame)
        self.frame_count += 1

    @staticmethod
    def frame_from_event(event, camera_view="follow"):
        events = getattr(event, "events", [])
        active_agent = event.metadata.get("activeAgentId", 0)
        ordered_events = []
        if events and 0 <= active_agent < len(events):
            ordered_events.append(events[active_agent])
        ordered_events.extend(item for item in events if item not in ordered_events)
        if not ordered_events:
            ordered_events = [event]
        if camera_view == "agent":
            for item in ordered_events:
                frame = getattr(item, "frame", None)
                if frame is not None:
                    return frame
        else:
            for item in ordered_events:
                frames = getattr(item, "third_party_camera_frames", [])
                if frames:
                    return frames[0]
            for item in ordered_events:
                frame = getattr(item, "frame", None)
                if frame is not None:
                    return frame
        return None

    def close(self):
        if self.preview is not None:
            self.preview.close()
        if self.writer is not None:
            self.writer.release()
            print(f"Saved video with {self.frame_count} frames to {self.output_path}")


class X11Preview:
    """Display live camera frames in an X11 window when OpenCV has no GUI support."""

    def __init__(self):
        from Xlib import X, display

        self.X = X
        self.display = display.Display()
        screen = self.display.screen()
        self.width, self.height = 512, 384
        self.depth = screen.root_depth
        self.window = screen.root.create_window(
            0,
            0,
            self.width,
            self.height,
            0,
            self.depth,
            X.InputOutput,
            X.CopyFromParent,
            background_pixel=screen.black_pixel,
        )
        self.window.set_wm_name("AI2-THOR Follow Camera")
        self.window.set_wm_class("AI2ThorFollow", "AI2ThorFollow")
        self.window.map()
        self.gc = self.window.create_gc()
        self.display.flush()

    def show(self, frame):
        from PIL import Image

        image = Image.fromarray(frame).convert("RGB").resize((self.width, self.height))
        data = image.tobytes("raw", "BGRX")
        rows_per_request = 64
        bytes_per_row = self.width * 4
        for y in range(0, self.height, rows_per_request):
            row_count = min(rows_per_request, self.height - y)
            start = y * bytes_per_row
            end = (y + row_count) * bytes_per_row
            self.window.put_image(
                self.gc,
                0,
                y,
                self.width,
                row_count,
                self.X.ZPixmap,
                self.depth,
                0,
                data[start:end],
            )
        self.display.flush()

    def close(self):
        self.window.destroy()
        self.display.close()


def rear_camera_pose(agent: dict, offset: List[float], target: dict | None = None):
    position = agent["position"]
    yaw = math.radians(agent["rotation"]["y"])
    right_x, right_z = math.cos(yaw), -math.sin(yaw)
    forward_x, forward_z = math.sin(yaw), math.cos(yaw)
    side, height, behind = offset
    target = target or {
        "x": position["x"],
        "y": position["y"] + 0.6,
        "z": position["z"],
    }
    camera_position = {
        "x": target["x"] + side * right_x - behind * forward_x,
        "y": target["y"] + height,
        "z": target["z"] + side * right_z - behind * forward_z,
    }
    dx = target["x"] - camera_position["x"]
    dy = target["y"] - camera_position["y"]
    dz = target["z"] - camera_position["z"]
    horizontal = math.hypot(dx, dz)
    return camera_position, {
        "x": math.degrees(math.atan2(-dy, horizontal)),
        "y": math.degrees(math.atan2(dx, dz)) % 360,
        "z": 0,
    }


ACTION_RE = re.compile(r"^\s*<(?P<agent>[^>]+)>\s+\[(?P<action>[A-Za-z_]+)\]\s+(?P<args>.+?)\s*$")
ARG_RE = re.compile(r"<(?P<name>[^>]+)>\s*\((?P<id>[^)]+)\)")
SUPPORTED = {
    "WALK",
    "GRAB",
    "OPEN",
    "CLOSE",
    "PUTIN",
    "FIND",
    "TURNTO",
    "LOOKAT_LONG",
    "TOUCH",
    "PLUGIN",
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
        verb = match.group("action").upper()
        if verb not in SUPPORTED:
            raise ValueError(f"line {line_number}: unsupported action [{verb}]")
        objects = [(m.group("name"), m.group("id")) for m in ARG_RE.finditer(match.group("args"))]
        if not objects:
            raise ValueError(
                f"line {line_number}: invalid VirtualHome object argument syntax: {raw}"
            )
        expected = 2 if verb == "PUTIN" else 1
        if len(objects) != expected:
            raise ValueError(
                f"line {line_number}: [{verb}] expects {expected} object argument(s), got {len(objects)}"
            )
        # Reject trailing/unparsed tokens instead of silently executing a partial line.
        residue = ARG_RE.sub("", match.group("args")).strip()
        if residue:
            raise ValueError(f"line {line_number}: malformed object argument(s): {residue}")
        actions.append(ScriptAction(line_number, raw, match.group("agent"), verb, objects))
    return actions


def normalize_type(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def load_procthor_house(split: str, index: int) -> dict:
    """Load one ProcTHOR-10K house specification using the optional ``prior`` package."""
    try:
        import prior
    except ImportError as exc:
        raise RuntimeError(
            "ProcTHOR-10K requires the optional 'prior' package; install it with "
            "'$HOME/venv/bin/pip install prior'"
        ) from exc

    try:
        dataset = prior.load_dataset("procthor-10k")
        houses = dataset[split]
    except Exception as exc:
        raise RuntimeError(f"could not load ProcTHOR-10K split {split!r}: {exc}") from exc

    if index < 0 or index >= len(houses):
        raise ValueError(
            f"ProcTHOR-10K {split!r} index must be between 0 and {len(houses) - 1}; " f"got {index}"
        )
    house = houses[index]
    # Some prior versions expose a House object, while the dataset's current
    # LazyJsonDataset returns the serialized house specification as a dict.
    if not isinstance(house, dict) and isinstance(getattr(house, "data", None), dict):
        house = house.data
    if not isinstance(house, dict):
        raise TypeError(
            f"ProcTHOR-10K returned {type(house).__name__} for {split}[{index}], "
            "expected a house specification dictionary"
        )
    return house


class VirtualHomeRunner:
    def __init__(
        self,
        controller,
        variables: Dict[str, str],
        object_mappings: Dict[str, str],
        continue_on_error: bool,
    ):
        self.controller = controller
        self.variables = variables
        self.object_mappings = object_mappings
        self.continue_on_error = continue_on_error

    def resolve(self, name: str, identifier: str) -> dict:
        if identifier.startswith("$"):
            if identifier not in self.variables:
                raise RuntimeError(f"undefined variable {identifier}")
            identifier = self.variables[identifier]
        objects = self.controller.last_event.metadata.get("objects", [])
        if identifier in self.object_mappings:
            mapped_id = self.object_mappings[identifier]
            for obj in objects:
                if obj.get("objectId") == mapped_id:
                    return obj
            raise RuntimeError(
                f"mapped objectId {mapped_id!r} for VirtualHome id {identifier!r} is not in the scene"
            )
        # VirtualHome may use 0 as an unresolved ID; resolve it from the class name.
        # Otherwise prefer an explicit AI2-THOR objectId before matching objectType.
        by_id = [] if identifier == "0" else [o for o in objects if o.get("objectId") == identifier]
        if by_id:
            return by_id[0]
        accepted_types = TYPE_ALIASES.get(normalize_type(name), {normalize_type(name)})
        matches = [o for o in objects if normalize_type(o.get("objectType", "")) in accepted_types]
        # VirtualHome IDs are scene-local and must not be guessed as AI2-THOR IDs.
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            agent_position = self.controller.last_event.metadata.get("agent", {}).get("position")
            positioned_matches = [
                obj
                for obj in matches
                if isinstance(obj.get("position"), dict)
                and "x" in obj["position"]
                and "z" in obj["position"]
            ]
            if agent_position and positioned_matches:
                return min(
                    positioned_matches,
                    key=lambda obj: math.hypot(
                        obj["position"]["x"] - agent_position["x"],
                        obj["position"]["z"] - agent_position["z"],
                    ),
                )
        raise RuntimeError(
            f"expected at least one {name!r} object (VirtualHome id {identifier!r}), "
            f"found {len(matches)}; map the VirtualHome ID to an AI2-THOR objectId "
            "with --object-map ID=OBJECT_ID"
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
        start = min(
            by_key,
            key=lambda k: math.hypot(
                by_key[k]["x"] - agent_pos["x"], by_key[k]["z"] - agent_pos["z"]
            ),
        )
        goals = {
            k
            for k, p in by_key.items()
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
            return {
                "objectId": target["objectId"],
                "objectType": target["objectType"],
                "visible": target.get("visible"),
            }
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
            self.step(
                "OpenObject" if verb == "OPEN" else "CloseObject",
                objectId=target["objectId"],
                forceAction=True,
            )
        elif verb == "PUTIN":
            destination = self.resolve(*action.objects[1])
            item = target
            target = destination
            inventory = self.controller.last_event.metadata.get("inventoryObjects", [])
            if not inventory:
                raise RuntimeError(f"[{verb}] requires a held object")
            held = inventory[0]
            if held["objectId"] != item["objectId"]:
                raise RuntimeError(
                    f"held object {held['objectId']} does not match script item {item['objectId']}"
                )
            if not target.get("receptacle", False):
                raise RuntimeError(f"{target['objectType']} is not a receptacle")
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
            return {
                "objectId": target["objectId"],
                "objectType": target["objectType"],
                "contact": "approximated by close approach",
            }
        elif verb == "PLUGIN":
            # iTHOR residential scenes do not expose a plug/socket relationship.
            # For toggleable lamps, emulate the useful end state (powered on).
            if "lamp" not in normalize_type(target.get("objectType", "")) or not target.get(
                "toggleable"
            ):
                raise RuntimeError(
                    "PLUGIN target is not a toggleable lamp; this scene has no plug/socket model"
                )
            if not target.get("isToggled", False):
                self.step("ToggleObjectOn", objectId=target["objectId"], forceAction=True)
            return {
                "objectId": target["objectId"],
                "objectType": target["objectType"],
                "plugin": "emulated by turning lamp on (iTHOR has no outlet connection state)",
            }
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
    scene_group = parser.add_mutually_exclusive_group()
    scene_group.add_argument(
        "--scene",
        help="iTHOR scene name (e.g. FloorPlan1 or FloorPlan201); defaults to FloorPlan1",
    )
    scene_group.add_argument(
        "--procthor-split",
        choices=("train", "val", "test"),
        help="load a house from the ProcTHOR-10K split (requires: pip install prior)",
    )
    parser.add_argument(
        "--procthor-index",
        type=int,
        default=0,
        help="zero-based house index within --procthor-split (default: 0)",
    )
    parser.add_argument("--headless", action="store_true", help="run Unity without a display")
    parser.add_argument(
        "--commit-id",
        default="f0825767cd50d69f666c7f282e54abfe58f1e917",
        help="AI2-THOR simulator build commit",
    )
    parser.add_argument(
        "--continue-on-error", action="store_true", help="continue after a failed line"
    )
    parser.add_argument(
        "--variable",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="resolve script variables, e.g. WINE_ID=0",
    )
    parser.add_argument(
        "--object-map",
        action="append",
        default=[],
        metavar="VH_ID=OBJECT_ID",
        help="map a VirtualHome numeric ID to an AI2-THOR objectId",
    )
    parser.add_argument("--json-out", type=Path, help="write per-line results as JSON")
    parser.add_argument("--video-out", type=Path, help="record an MP4 video")
    parser.add_argument("--video-fps", type=float, default=66.666, help="output video frame rate")
    parser.add_argument(
        "--camera-view",
        choices=("agent", "follow"),
        default="agent",
        help="record the simulator agent view or the added follow camera",
    )
    parser.add_argument(
        "--show-camera",
        action="store_true",
        help="show the selected camera in a live preview window while recording",
    )
    parser.add_argument("--width", type=int, default=1024, help="simulator frame width")
    parser.add_argument("--height", type=int, default=768, help="simulator frame height")
    parser.add_argument(
        "--camera-position",
        default="-0.45,0.25,0.45",
        help="follow camera offset as right,up,behind meters (from held item when carrying)",
    )
    args = parser.parse_args()
    if args.procthor_split is None and args.procthor_index != 0:
        parser.error("--procthor-index requires --procthor-split")
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

    try:
        offset = [float(value) for value in args.camera_position.split(",")]
        if len(offset) != 3:
            raise ValueError("expected x,y,z")
    except ValueError as exc:
        parser.error(f"invalid --camera-position: {exc}")

    import ai2thor.controller
    import ai2thor.platform

    scene = args.scene or "FloorPlan1"
    if args.procthor_split is not None:
        try:
            scene = load_procthor_house(args.procthor_split, args.procthor_index)
        except (RuntimeError, TypeError, ValueError) as exc:
            parser.error(str(exc))
        print(f"Loaded ProcTHOR-10K {args.procthor_split}[{args.procthor_index}]")

    controller = ai2thor.controller.Controller(
        scene=scene,
        headless=args.headless,
        width=args.width,
        height=args.height,
        commit_id=args.commit_id,
        platform=ai2thor.platform.Linux64,
    )
    recorder = None
    try:
        recorder = (
            FrameRecorder(args.video_out, args.video_fps, show_window=args.show_camera)
            if args.video_out
            else None
        )
        controller_step = controller.step

        if recorder is not None:

            def update_follow_camera(event):
                inventory = event.metadata.get("inventoryObjects", [])
                held_pose = event.metadata.get("heldObjectPose", {})
                focus = held_pose.get("position") if inventory else None
                camera_position, camera_rotation = rear_camera_pose(
                    event.metadata["agent"], offset, focus
                )
                cameras = event.metadata.get("thirdPartyCameras", [])
                if cameras:
                    camera_event = controller_step(
                        action="UpdateThirdPartyCamera",
                        thirdPartyCameraId=0,
                        position=camera_position,
                        rotation=camera_rotation,
                    )
                else:
                    camera_event = controller_step(
                        action="AddThirdPartyCamera",
                        position=camera_position,
                        rotation=camera_rotation,
                    )
                recorder.append(FrameRecorder.frame_from_event(camera_event, "follow"))

            if not args.headless:
                if args.camera_view == "follow":
                    update_follow_camera(controller.last_event)
                else:
                    recorder.append(FrameRecorder.frame_from_event(controller.last_event, "agent"))

                def recording_step(*step_args, **step_kwargs):
                    event = controller_step(*step_args, **step_kwargs)
                    if args.camera_view == "follow":
                        update_follow_camera(event)
                    else:
                        recorder.append(FrameRecorder.frame_from_event(event, "agent"))
                    return event

                controller.step = recording_step

        results = VirtualHomeRunner(
            controller, variables, object_mappings, args.continue_on_error
        ).run(actions)
    finally:
        controller.stop()
        if recorder is not None:
            recorder.close()
    if args.json_out:
        import json

        args.json_out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(r["success"] for r in results) and len(results) == len(actions) else 1


if __name__ == "__main__":
    raise SystemExit(main())
