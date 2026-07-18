from unittest.mock import MagicMock

import questionary
from prompt_toolkit.keys import Keys

from omm.cli import _add_escape_to_cancel


def test_escape_binding_triggers_keyboard_interrupt_style_exit():
    question = questionary.select(
        "Pick one:",
        choices=[questionary.Choice(title="a", value="a")],
    )

    _add_escape_to_cancel(question)

    escape_bindings = [
        b for b in question.application.key_bindings.bindings if b.keys == (Keys.Escape,)
    ]
    assert escape_bindings, "expected an Escape key binding to be registered"

    fake_event = MagicMock()
    escape_bindings[-1].handler(fake_event)

    fake_event.app.exit.assert_called_once_with(
        exception=KeyboardInterrupt, style="class:aborting"
    )
