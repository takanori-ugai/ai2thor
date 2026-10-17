"""Convert AI2-THOR event metadata to a VirtualHome-style environment graph."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Tuple


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _node_properties(obj: Mapping[str, Any]) -> List[str]:
    properties = []
    for metadata_key, graph_property in (
        ("pickupable", "GRABBABLE"),
        ("openable", "CAN_OPEN"),
        ("receptacle", "RECIPIENT"),
        ("toggleable", "HAS_SWITCH"),
        ("sliceable", "CUTTABLE"),
        ("moveable", "MOVABLE"),
        ("readable", "READABLE"),
    ):
        if obj.get(metadata_key):
            properties.append(graph_property)

    object_type = str(obj.get("objectType", "")).lower()
    if object_type in {"sofa", "chair", "armchair", "stool", "bench"}:
        properties.append("SITTABLE")
    if object_type in {"bed", "sofa"}:
        properties.append("LIEABLE")
    return properties


def _node_states(obj: Mapping[str, Any]) -> List[str]:
    states = []
    if obj.get("openable"):
        states.append("OPEN" if obj.get("isOpen") else "CLOSED")
    if obj.get("toggleable"):
        states.append("ON" if obj.get("isToggled") else "OFF")
    if obj.get("dirtyable") or "isDirty" in obj:
        states.append("DIRTY" if obj.get("isDirty") else "CLEAN")
    if obj.get("isCooked"):
        states.append("COOKED")
    if obj.get("isSliced"):
        states.append("SLICED")
    if obj.get("isBroken"):
        states.append("BROKEN")
    return states


def _vector3(value: Any) -> Optional[Tuple[float, float, float]]:
    if isinstance(value, Mapping) and all(axis in value for axis in ("x", "y", "z")):
        return (float(value["x"]), float(value["y"]), float(value["z"]))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (float(value[0]), float(value[1]), float(value[2]))
    return None


def _object_distance_to_bounds(obj1: Mapping[str, Any], obj2: Mapping[str, Any]) -> Optional[float]:
    position1 = _vector3(obj1.get("position"))
    bounds2 = obj2.get("axisAlignedBoundingBox") or obj2.get("boundingBox")
    center2 = _vector3(bounds2.get("center")) if isinstance(bounds2, Mapping) else None
    size2 = _vector3(bounds2.get("size")) if isinstance(bounds2, Mapping) else None
    if position1 is None or center2 is None or size2 is None:
        position2 = _vector3(obj2.get("position"))
        if position1 is None or position2 is None:
            return None
        return math.dist(position1, position2)

    delta = [max(abs(position1[i] - center2[i]) - size2[i] / 2.0, 0.0) for i in range(3)]
    return math.sqrt(sum(component * component for component in delta))


def environment_graph(event_or_metadata: Any, include_close_edges: bool = True) -> Dict[str, list]:
    """Build a graph with VirtualHome's ``nodes``/``edges`` structure.

    Node IDs are stable integers within an event snapshot; each node also carries
    its original AI2-THOR ``objectId`` in ``ai2thor_object_id``. Containment and
    support relations are inferred from AI2-THOR receptacle metadata. ``CLOSE``
    uses a 1.5m threshold and is emitted in both directions.
    """
    metadata = getattr(event_or_metadata, "metadata", event_or_metadata)
    objects = _as_list(metadata.get("objects", []))
    ordered_objects = sorted(objects, key=lambda obj: str(obj.get("objectId", "")))
    object_node_ids = {obj.get("objectId"): index + 1 for index, obj in enumerate(ordered_objects)}

    nodes = [
        {
            "id": 0,
            "class_name": "character",
            "category": "Characters",
            "properties": [],
            "states": [],
            "obj_transform": {
                "position": metadata.get("agent", {}).get("position"),
                "rotation": metadata.get("agent", {}).get("rotation"),
            },
        }
    ]
    for obj in ordered_objects:
        object_id = obj.get("objectId")
        bounds = obj.get("axisAlignedBoundingBox") or obj.get("boundingBox")
        node = {
            "id": object_node_ids[object_id],
            "class_name": str(obj.get("objectType", "unknown")).lower(),
            "category": obj.get("category", "Objects"),
            "properties": _node_properties(obj),
            "states": _node_states(obj),
            "ai2thor_object_id": object_id,
        }
        if obj.get("position") is not None or obj.get("rotation") is not None:
            node["obj_transform"] = {
                "position": obj.get("position"),
                "rotation": obj.get("rotation"),
            }
        if bounds:
            node["bounding_box"] = bounds
        nodes.append(node)

    edges = []
    seen_edges = set()

    def add_edge(source_id: int, target_id: int, relation: str) -> None:
        key = (source_id, target_id, relation)
        if key not in seen_edges:
            edges.append({"from_id": source_id, "to_id": target_id, "relation_type": relation})
            seen_edges.add(key)

    # AI2-THOR reports receptacle membership on the parent. An openable parent
    # maps to INSIDE; a non-openable support surface maps to ON.
    for parent in ordered_objects:
        parent_id = parent.get("objectId")
        parent_node_id = object_node_ids.get(parent_id)
        if parent_node_id is None:
            continue
        relation = "INSIDE" if parent.get("openable") else "ON"
        for child_id in _as_list(parent.get("receptacleObjectIds")):
            child_node_id = object_node_ids.get(child_id)
            if child_node_id is not None:
                add_edge(child_node_id, parent_node_id, relation)

    for child in ordered_objects:
        child_node_id = object_node_ids[child.get("objectId")]
        for parent_id in _as_list(child.get("parentReceptacles")):
            parent_node_id = object_node_ids.get(parent_id)
            parent = next((obj for obj in ordered_objects if obj.get("objectId") == parent_id), None)
            if parent_node_id is not None and parent is not None:
                add_edge(child_node_id, parent_node_id, "INSIDE" if parent.get("openable") else "ON")

    inventory = _as_list(metadata.get("inventoryObjects", []))
    for held in inventory:
        held_id = held.get("objectId") if isinstance(held, Mapping) else None
        held_node_id = object_node_ids.get(held_id)
        if held_node_id is not None:
            add_edge(0, held_node_id, "HOLDS_RH")

    if include_close_edges:
        for index, obj1 in enumerate(ordered_objects):
            node1 = object_node_ids[obj1.get("objectId")]
            for obj2 in ordered_objects[index + 1 :]:
                distance = _object_distance_to_bounds(obj1, obj2)
                if distance is not None and distance < 1.5:
                    node2 = object_node_ids[obj2.get("objectId")]
                    add_edge(node1, node2, "CLOSE")
                    add_edge(node2, node1, "CLOSE")

    return {"nodes": nodes, "edges": edges}
