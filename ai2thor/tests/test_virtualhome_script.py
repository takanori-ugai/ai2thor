from types import SimpleNamespace

import pytest

from scripts.run_virtualhome_script import (
    VirtualHomeRunner,
    parse_script,
)


def test_parse_virtualhome_actions_and_keep_source_lines():
    actions = parse_script(
        """# setup
<char0> [WALK] <wine> ($WINE_ID)

// put the wine in the washer
<char0> [PUTIN] <wine> ($WINE_ID) <washingmachine> (365)
"""
    )

    assert [action.line_number for action in actions] == [2, 5]
    assert actions[0].verb == "WALK"
    assert actions[0].objects == [("wine", "$WINE_ID")]
    assert actions[1].objects == [("wine", "$WINE_ID"), ("washingmachine", "365")]


@pytest.mark.parametrize(
    "script, message",
    [
        ("<char0> [FLY] <sofa> (1)", "unsupported action"),
        ("<char0> [WALK] sofa (1)", "invalid VirtualHome object argument syntax"),
        ("<char0> [PUTIN] <washer> (1)", "expects 2 object argument"),
        ("<char0> [GRAB] <wine> (1) unexpected", "malformed object argument"),
    ],
)
def test_parser_reports_line_and_reason(script, message):
    with pytest.raises(ValueError, match=message):
        parse_script(script)


class FakeController:
    def __init__(self, objects):
        self.last_event = SimpleNamespace(
            metadata={"objects": objects, "inventoryObjects": [], "lastActionSuccess": True}
        )
        self.calls = []

    def step(self, action, **kwargs):
        self.calls.append((action, kwargs))
        metadata = dict(self.last_event.metadata)
        metadata["lastActionSuccess"] = True
        self.last_event = SimpleNamespace(metadata=metadata)
        return self.last_event


def test_resolve_virtualhome_type_alias_and_explicit_object_mapping():
    wine = {"objectId": "WineBottle|1", "objectType": "WineBottle"}
    controller = FakeController([wine])
    runner = VirtualHomeRunner(controller, {}, {"365": "WineBottle|1"}, False)

    assert runner.resolve("wine", "365") == wine


def test_grab_and_putin_map_to_ai2thor_actions():
    washer = {"objectId": "Washer|1", "objectType": "WashingMachine", "receptacle": True}
    wine = {"objectId": "WineBottle|1", "objectType": "WineBottle"}
    controller = FakeController([washer, wine])
    controller.last_event.metadata["inventoryObjects"] = [wine]
    runner = VirtualHomeRunner(controller, {"$WINE_ID": "WineBottle|1"}, {}, False)

    grab = parse_script("<char0> [GRAB] <wine> ($WINE_ID)")[0]
    putin = parse_script("<char0> [PUTIN] <wine> ($WINE_ID) <washingmachine> (365)")[0]
    runner.execute(grab)
    runner.execute(putin)

    assert controller.calls == [
        ("PickupObject", {"objectId": "WineBottle|1"}),
        ("PutObject", {"objectId": "Washer|1"}),
    ]


def test_run_records_failure_and_stops_by_default():
    controller = FakeController([])
    runner = VirtualHomeRunner(controller, {}, {}, False)
    actions = parse_script(
        "<char0> [FIND] <unknown> (1)\n<char0> [FIND] <unknown> (2)"
    )

    results = runner.run(actions)

    assert len(results) == 1
    assert results[0]["line"] == 1
    assert results[0]["success"] is False
    assert "found 0" in results[0]["error"]
