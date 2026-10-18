from ai2thor.util.environment_graph import environment_graph


def test_environment_graph_maps_nodes_states_properties_and_relations():
    metadata = {
        "objects": [
            {
                "objectId": "Mug|1",
                "objectType": "Mug",
                "pickupable": True,
                "position": {"x": 0.0, "y": 1.0, "z": 0.0},
                "parentReceptacles": ["Cabinet|1"],
                "axisAlignedBoundingBox": {
                    "center": {"x": 0.0, "y": 1.0, "z": 0.0},
                    "size": {"x": 0.1, "y": 0.1, "z": 0.1},
                },
            },
            {
                "objectId": "Cabinet|1",
                "objectType": "Cabinet",
                "openable": True,
                "isOpen": False,
                "receptacle": True,
                "receptacleObjectIds": ["Mug|1"],
                "position": {"x": 0.0, "y": 1.0, "z": 0.0},
                "axisAlignedBoundingBox": {
                    "center": {"x": 0.0, "y": 1.0, "z": 0.0},
                    "size": {"x": 1.0, "y": 1.0, "z": 1.0},
                },
            },
        ],
        "inventoryObjects": [{"objectId": "Mug|1"}],
    }

    graph = environment_graph(metadata)
    nodes_by_object_id = {
        node["ai2thor_object_id"]: node for node in graph["nodes"] if "ai2thor_object_id" in node
    }
    id_by_object_id = {key: node["id"] for key, node in nodes_by_object_id.items()}
    relations = {(edge["from_id"], edge["to_id"], edge["relation_type"]) for edge in graph["edges"]}

    assert graph["nodes"][0]["class_name"] == "character"
    assert nodes_by_object_id["Mug|1"]["class_name"] == "mug"
    assert "GRABBABLE" in nodes_by_object_id["Mug|1"]["properties"]
    assert "CLOSED" in nodes_by_object_id["Cabinet|1"]["states"]
    assert (
        id_by_object_id["Mug|1"],
        id_by_object_id["Cabinet|1"],
        "INSIDE",
    ) in relations
    assert (0, id_by_object_id["Mug|1"], "HOLDS_RH") in relations
    assert (id_by_object_id["Mug|1"], id_by_object_id["Cabinet|1"], "CLOSE") in relations
    assert (id_by_object_id["Cabinet|1"], id_by_object_id["Mug|1"], "CLOSE") in relations


def test_environment_graph_can_skip_proximity_edges():
    metadata = {
        "objects": [
            {"objectId": "A|1", "objectType": "A", "position": {"x": 0, "y": 0, "z": 0}},
            {"objectId": "B|1", "objectType": "B", "position": {"x": 0.1, "y": 0, "z": 0}},
        ]
    }

    graph = environment_graph(metadata, include_close_edges=False)

    assert graph["edges"] == []
