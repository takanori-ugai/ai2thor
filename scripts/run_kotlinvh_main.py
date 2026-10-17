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
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, List

try:
    from scripts.run_virtualhome_script import ScriptAction, VirtualHomeRunner, parse_script
except ModuleNotFoundError:
    # When run as `python scripts/run_kotlinvh_main.py`, Python puts the
    # scripts directory, rather than the repository root, on sys.path.
    from run_virtualhome_script import ScriptAction, VirtualHomeRunner, parse_script


SCRIPT0 = [
    "<char0> [WALK] <wine> (93)",
    "<char0> [GRAB] <wine> (93)",
    " <char0> [DRINK] <wine> (93)",
    "[DRINK] <wine> (93)",
    "  [DRINK] <wine> (93)",
    "<char0> [WALK] <tablelamp> (76)",
]

FINAL_SCRIPT = [
    "<char0> [RUN] <book> (86)",
    "<char0> [FIND] <book> (86)",
    "<char0> [READ] <book> (86)",
    "<char0> [WALK] <sofa> (139)",
    "<char0> [SIT] <sofa> (139)",
]


class KotlinVHMainPort:
    """Python adaptation of Main.kt using AI2-THOR's public controller API."""

    def __init__(self, controller, object_mappings: Dict[str, str], continue_on_error: bool):
        self.controller = controller
        self.object_mappings = object_mappings
        self.continue_on_error = continue_on_error
        self.runner = VirtualHomeRunner(controller, {}, object_mappings, continue_on_error)

    def reset(self, scene: str, agent_count: int = 1):
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
        return [node for node in graph["nodes"] if property_name.upper() in node.get("properties", [])]

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
        graph["edges"].append(
            {"from_id": cat_id, "to_id": sofa_node["id"], "relation_type": "ON"}
        )
        print("AI2-THOR has no scene-graph expansion API; added cat/ON only to a local graph copy.")
        return graph

    def add_character(self, scene: str):
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
                (agent_event for agent_event in event.events if agent_event.third_party_camera_frames),
                event.events[0],
            )
        if not camera_event.third_party_camera_frames:
            raise RuntimeError("AI2-THOR returned no third-party camera frame")
        from PIL import Image

        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(camera_event.third_party_camera_frames[0]).save(output_path)
        print(f"Saved camera image to {output_path}")
        return event

    def test_scripts(self, scene: str, script: Iterable[str]):
        self.reset(scene)
        graph = self.controller.get_environment_graph()
        sofas = [node for node in graph["nodes"] if node.get("class_name") == "sofa"]
        if sofas:
            self.add_cat_graph_overlay(sofas[-1])
        else:
            print("No sofa node found; skipping the local cat graph overlay.")
        self.add_character(scene)
        return self.run_script(script)

    def check_scripts(self, scene: str, script: Iterable[str]):
        self.reset(scene)
        graph = self.controller.get_environment_graph()
        sofas = [node for node in graph["nodes"] if node.get("class_name") == "sofa"]
        if not sofas:
            raise RuntimeError("checkScripts requires a sofa in the selected scene")
        self.translate_script(script)  # KotlinVH skipExecution equivalent: parse without dispatch.
        self.add_character(scene)
        print("Script structure checked; AI2-THOR does not provide KotlinVH renderScript validation.")
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", default="FloorPlan201", help="iTHOR living-room scene")
    parser.add_argument("--headless", action="store_true", help="run Unity without a display")
    parser.add_argument("--continue-on-error", action="store_true", help="continue after failed script actions")
    parser.add_argument("--object-map", action="append", default=[], metavar="VH_ID=OBJECT_ID")
    parser.add_argument("--image-out", type=Path, default=Path("kotlinvh-camera.png"))
    parser.add_argument("--graph-out", type=Path, default=Path("kotlinvh-environment-graph.json"))
    parser.add_argument("--camera-position", default="0,1.6,0", help="camera offset from agent as x,y,z meters")
    args = parser.parse_args()

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

    controller = ai2thor.controller.Controller(
        scene=args.scene,
        headless=args.headless,
        renderInstanceSegmentation=False,
    )
    port = KotlinVHMainPort(controller, object_mappings, args.continue_on_error)
    try:
        print("check:", controller.last_event.metadata.get("lastActionSuccess", False))
        print("testScripts:")
        port.test_scripts(args.scene, SCRIPT0)

        # Main.kt's helper methods each reset the simulator before querying it.
        port.reset(args.scene)
        print("findNodes(tv):", port.find_nodes("tv"))
        port.reset(args.scene)
        print("findNodesByProperty(HAS_PLUG):", port.find_nodes_by_property("HAS_PLUG"))
        port.reset(args.scene)
        print("findNodesById(1):", port.find_nodes_by_id(1))

        event = port.reset(args.scene, agent_count=2)
        graph = controller.get_environment_graph(event)
        args.graph_out.parent.mkdir(parents=True, exist_ok=True)
        args.graph_out.write_text(json.dumps(graph, indent=2), encoding="utf-8")
        print(f"Environment graph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")

        agent_position = event.metadata["agent"]["position"]
        camera_position = {
            "x": agent_position["x"] + offset[0],
            "y": agent_position["y"] + offset[1],
            "z": agent_position["z"] + offset[2],
        }
        port.add_camera_and_save(
            camera_position,
            {"x": 20, "y": 21, "z": 22},
            args.image_out,
        )

        print("renderFinalScript (AI2-THOR supported subset):")
        results = port.run_script(FINAL_SCRIPT)
        success = all(result["success"] for result in results)
        return 0 if success else 1
    finally:
        controller.stop()


if __name__ == "__main__":
    raise SystemExit(main())
