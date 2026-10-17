from scripts.run_kotlinvh_main import KotlinVHMainPort


def test_main_translation_keeps_supported_actions_and_original_line_numbers(capsys):
    actions = KotlinVHMainPort.translate_script(
        [
            "<char0> [WALK] <wine> (93)",
            "[DRINK] <wine> (93)",
            "<char0> [FIND] <book> (86)",
        ]
    )

    assert [action.verb for action in actions] == ["WALK", "FIND"]
    assert [action.line_number for action in actions] == [1, 3]
    assert "skip source line 2" in capsys.readouterr().err
