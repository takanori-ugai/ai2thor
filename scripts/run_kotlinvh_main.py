#!/usr/bin/env python3
"""Run the AI2-THOR equivalent of KotlinVH's Main.kt demonstration.

Example:
  python scripts/run_kotlinvh_main.py --scene FloorPlan201 --headless \
      --object-map '93=WineBottle|1' --object-map '76=DeskLamp|1'

VirtualHome-only operations that AI2-THOR cannot reproduce (scene graph
expansion, DRINK/READ/SIT behavior, and KotlinVH server calls) are reported as
skipped. The supported script actions execute through the shared runner.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, List

try:
    from scripts.run_virtualhome_script import (
        ScriptAction,
        VirtualHomeRunner,
        load_procthor_house,
        parse_script,
    )
except ModuleNotFoundError:
    # When run as `python scripts/run_kotlinvh_main.py`, Python puts the
    # scripts directory, rather than the repository root, on sys.path.
    from run_virtualhome_script import (
        ScriptAction,
        VirtualHomeRunner,
        load_procthor_house,
        parse_script,
    )


SCRIPT0 = [
    # FloorPlan201 has no wine bottle; use its unique grabbable book instead.
    "<char0> [WALK] <book> (93)",
    "<char0> [GRAB] <book> (93)",
    " <char0> [DRINK] <book> (93)",
    "[DRINK] <book> (93)",
    "  [DRINK] <book> (93)",
    "<char0> [WALK] <tablelamp> (76)",
]

FINAL_SCRIPT = [
    "<char0> [RUN] <book> (86)",
    "<char0> [FIND] <book> (86)",
    "<char0> [READ] <book> (86)",
    # The sofa center is not reachable in FloorPlan201; the desk lamp is.
    "<char0> [WALK] <tablelamp> (139)",
    "<char0> [SIT] <tablelamp> (139)",
]

# The checkout's own commits do not have Linux64 simulator builds. This
# published build is compatible with the controller API and supports X11.
DEFAULT_LINUX64_COMMIT = "f0825767cd50d69f666c7f282e54abfe58f1e917"


class FrameRecorder:
    """Write captured AI2-THOR RGB frames to an MP4 file."""

    def __init__(self, output_path: Path, fps: int):
        self.output_path = output_path
        self.fps = fps
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
        self.writer.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        self.frame_count += 1

    @staticmethod
    def frame_from_event(event):
        events = getattr(event, "events", [])
        active_agent = event.metadata.get("activeAgentId", 0)
        ordered_events = []
        if events and 0 <= active_agent < len(events):
            ordered_events.append(events[active_agent])
        ordered_events.extend(item for item in events if item not in ordered_events)
        if not ordered_events:
            ordered_events = [event]
        # Prefer the third-party follow camera, which gives a moving rear view.
        for item in ordered_events:
            camera_frames = getattr(item, "third_party_camera_frames", [])
            if camera_frames:
                return camera_frames[0]
        for item in ordered_events:
            frame = getattr(item, "frame", None)
            if frame is not None:
                return frame
        return None

    def close(self):
        if self.writer is not None:
            self.writer.release()
            print(f"Saved video with {self.frame_count} frames to {self.output_path}")


def rear_camera_pose(agent: dict, offset: List[float]):
    """Place a camera to the agent's rear-right and aim it back at the agent."""
    position = agent["position"]
    yaw = math.radians(agent["rotation"]["y"])
    right_x, right_z = math.cos(yaw), -math.sin(yaw)
    forward_x, forward_z = math.sin(yaw), math.cos(yaw)
    side, height, behind = offset
    camera_position = {
        "x": position["x"] + side * right_x - behind * forward_x,
        "y": position["y"] + height,
        "z": position["z"] + side * right_z - behind * forward_z,
    }
    target_y = position["y"] + 0.6
    dx = position["x"] - camera_position["x"]
    dy = target_y - camera_position["y"]
    dz = position["z"] - camera_position["z"]
    horizontal = math.hypot(dx, dz)
    camera_rotation = {
        "x": math.degrees(math.atan2(-dy, horizontal)),
        "y": math.degrees(math.atan2(dx, dz)) % 360,
        "z": 0,
    }
    return camera_position, camera_rotation


class KotlinVHMainPort:
    """Python adaptation of Main.kt using AI2-THOR's public controller API."""

    def __init__(self, controller, object_mappings: Dict[str, str], continue_on_error: bool):
        self.controller = controller
        self.object_mappings = object_mappings
        self.continue_on_error = continue_on_error
        self.runner = VirtualHomeRunner(controller, {}, object_mappings, continue_on_error)

    def reset(self, scene, agent_count: int = 1):
        self.controller.reset(scene=scene, agentCount=agent_count)
        event = self.controller.last_event
        if not event.metadata.get("lastActionSuccess", True):
            raise RuntimeError(event.metadata.get("errorMessage") or "scene reset failed")
        return event

    @staticmethod
    def translate_script(lines: Iterable[str]) -> List[ScriptAction]:
        supported = []
        unsupported = []
        for source_line, line in enumerate(lines, 1):
            try:
                actions = parse_script(line)
            except ValueError as exc:
                unsupported.append((source_line, line, str(exc)))
                continue
            supported.extend(replace(action, line_number=source_line) for action in actions)
        for line_number, line, reason in unsupported:
            print(f"skip source line {line_number}: {line.strip()!r}: {reason}", file=sys.stderr)
        return supported

    def run_script(self, lines: Iterable[str]):
        actions = self.translate_script(lines)
        return self.runner.run(actions)

    def find_nodes(self, name: str):
        graph = self.controller.get_environment_graph()
        pattern = re.compile(name, re.IGNORECASE)
        return [node for node in graph["nodes"] if pattern.search(node.get("class_name", ""))]

    def find_nodes_by_property(self, property_name: str):
        graph = self.controller.get_environment_graph()
        return [
            node for node in graph["nodes"] if property_name.upper() in node.get("properties", [])
        ]

    def find_nodes_by_id(self, node_id: int):
        graph = self.controller.get_environment_graph()
        return [node for node in graph["nodes"] if node.get("id") == node_id]

    def add_cat_graph_overlay(self, sofa_node: dict):
        """Represent KotlinVH's requested edit locally; AI2-THOR has no graph-edit API."""
        graph = self.controller.get_environment_graph()
        cat_id = max((node["id"] for node in graph["nodes"]), default=0) + 1
        graph["nodes"].append(
            {
                "id": cat_id,
                "class_name": "cat",
                "category": "Animals",
                "properties": [],
                "states": [],
                "virtualhome_graph_overlay": True,
            }
        )
        graph["edges"].append({"from_id": cat_id, "to_id": sofa_node["id"], "relation_type": "ON"})
        print("AI2-THOR has no scene-graph expansion API; added cat/ON only to a local graph copy.")
        return graph

    def add_character(self, scene):
        # The primary character exists at initialization. A second agent is the
        # closest AI2-THOR equivalent to KotlinVH's addCharacter request.
        event = self.reset(scene, agent_count=2)
        agents = event.metadata.get("agents", [])
        print(f"AI2-THOR agents after reset(agentCount=2): {len(agents) or 2}")
        return event

    def add_camera_and_save(self, position: dict, rotation: dict, output_path: Path):
        event = self.controller.step(
            action="AddThirdPartyCamera",
            position=position,
            rotation=rotation,
        )
        if not event.metadata.get("lastActionSuccess", False):
            raise RuntimeError(event.metadata.get("errorMessage") or "AddThirdPartyCamera failed")
        cameras = event.metadata.get("thirdPartyCameras", [])
        print(f"Third-party camera count: {len(cameras)}")
        camera_event = event
        if not camera_event.third_party_camera_frames and getattr(event, "events", None):
            camera_event = next(
                (
                    agent_event
                    for agent_event in event.events
                    if agent_event.third_party_camera_frames
                ),
                event.events[0],
            )
        if not camera_event.third_party_camera_frames:
            raise RuntimeError("AI2-THOR returned no third-party camera frame")
        from PIL import Image

        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(camera_event.third_party_camera_frames[0]).save(output_path)
        print(f"Saved camera image to {output_path}")
        return event

    def test_scripts(self, scene, script: Iterable[str]):
        self.reset(scene)
        graph = self.controller.get_environment_graph()
        sofas = [node for node in graph["nodes"] if node.get("class_name") == "sofa"]
        if sofas:
            self.add_cat_graph_overlay(sofas[-1])
        else:
            print("No sofa node found; skipping the local cat graph overlay.")
        self.add_character(scene)
        return self.run_script(script)

    def check_scripts(self, scene, script: Iterable[str]):
        self.reset(scene)
        graph = self.controller.get_environment_graph()
        sofas = [node for node in graph["nodes"] if node.get("class_name") == "sofa"]
        if not sofas:
            raise RuntimeError("checkScripts requires a sofa in the selected scene")
        self.translate_script(script)  # KotlinVH skipExecution equivalent: parse without dispatch.
        self.add_character(scene)
        print(
            "Script structure checked; AI2-THOR does not provide KotlinVH renderScript validation."
        )
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scene_group = parser.add_mutually_exclusive_group()
    scene_group.add_argument(
        "--scene", help="iTHOR scene name (defaults to FloorPlan201)"
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
        default=DEFAULT_LINUX64_COMMIT,
        help="AI2-THOR simulator build commit (default: a published Linux64 build)",
    )
    parser.add_argument(
        "--continue-on-error", action="store_true", help="continue after failed script actions"
    )
    parser.add_argument("--object-map", action="append", default=[], metavar="VH_ID=OBJECT_ID")
    parser.add_argument("--image-out", type=Path, default=Path("kotlinvh-camera.png"))
    parser.add_argument("--graph-out", type=Path, default=Path("kotlinvh-environment-graph.json"))
    parser.add_argument("--video-out", type=Path, default=Path("kotlinvh-demo.mp4"))
    parser.add_argument("--video-fps", type=float, default=8, help="output video frame rate")
    parser.add_argument("--width", type=int, default=300, help="simulator frame width")
    parser.add_argument("--height", type=int, default=300, help="simulator frame height")
    parser.add_argument(
        "--camera-position",
        default="0.5,1.4,1.2",
        help="rear camera offset from agent as right,up,behind meters",
    )
    args = parser.parse_args()
    if args.procthor_split is None and args.procthor_index != 0:
        parser.error("--procthor-index requires --procthor-split")

    object_mappings = {}
    for item in args.object_map:
        if "=" not in item:
            parser.error(f"invalid --object-map {item!r}; expected VH_ID=OBJECT_ID")
        virtualhome_id, ai2thor_id = item.split("=", 1)
        object_mappings[virtualhome_id] = ai2thor_id

    try:
        offset = [float(value) for value in args.camera_position.split(",")]
        if len(offset) != 3:
            raise ValueError("expected x,y,z")
    except ValueError as exc:
        parser.error(f"invalid --camera-position: {exc}")

    import ai2thor.controller

    scene = args.scene or "FloorPlan201"
    if args.procthor_split is not None:
        try:
            scene = load_procthor_house(args.procthor_split, args.procthor_index)
        except (RuntimeError, TypeError, ValueError) as exc:
            parser.error(str(exc))
        print(f"Loaded ProcTHOR-10K {args.procthor_split}[{args.procthor_index}]")

    controller = ai2thor.controller.Controller(
        scene=scene,
        headless=args.headless,
        commit_id=args.commit_id,
        renderInstanceSegmentation=False,
        platform=ai2thor.platform.Linux64,
        width=args.width,
        height=args.height,
    )
    recorder = FrameRecorder(args.video_out, args.video_fps)
    controller_step = controller.step

    def update_follow_camera(event):
        camera_position, camera_rotation = rear_camera_pose(event.metadata["agent"], offset)
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
        recorder.append(recorder.frame_from_event(camera_event))
        return camera_event

    if not args.headless:
        update_follow_camera(controller.last_event)

    def recording_step(*step_args, **step_kwargs):
        event = controller_step(*step_args, **step_kwargs)
        update_follow_camera(event)
        return event

    if not args.headless:
        controller.step = recording_step
    port = KotlinVHMainPort(controller, object_mappings, args.continue_on_error)
    try:
        print("check:", controller.last_event.metadata.get("lastActionSuccess", False))
        print("testScripts:")
        port.test_scripts(scene, SCRIPT0)

        # Main.kt's helper methods each reset the simulator before querying it.
        port.reset(scene)
        print("findNodes(tv):", port.find_nodes("tv"))
        port.reset(scene)
        print("findNodesByProperty(HAS_PLUG):", port.find_nodes_by_property("HAS_PLUG"))
        port.reset(scene)
        print("findNodesById(1):", port.find_nodes_by_id(1))

        event = port.reset(scene, agent_count=2)
        graph = controller.get_environment_graph(event)
        args.graph_out.parent.mkdir(parents=True, exist_ok=True)
        args.graph_out.write_text(json.dumps(graph, indent=2), encoding="utf-8")
        print(f"Environment graph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")

        agent_position = event.metadata["agent"]["position"]
        camera_position, camera_rotation = rear_camera_pose(event.metadata["agent"], offset)
        if args.headless:
            print("Headless mode disables image and video capture.")
        else:
            port.add_camera_and_save(
                camera_position,
                camera_rotation,
                args.image_out,
            )

        print("renderFinalScript (AI2-THOR supported subset):")
        results = port.run_script(FINAL_SCRIPT)
        success = all(result["success"] for result in results)
        return 0 if success else 1
    finally:
        controller.stop()
        recorder.close()


if __name__ == "__main__":
    raise SystemExit(main())
