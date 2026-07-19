import unittest
from unittest.mock import patch

import relay_controller


class RelayControllerTests(unittest.TestCase):
    def test_open_door_uses_non_blocking_persistent_pulse(self):
        with patch.object(relay_controller._relay_daemon, "pulse", return_value=True) as pulse:
            with patch.object(relay_controller.time, "sleep") as sleep:
                self.assertTrue(relay_controller.open_door("jones", duration=0.75))

        pulse.assert_called_once_with(1, 0.75)
        sleep.assert_not_called()

    def test_harvey_maps_to_second_relay(self):
        with patch.object(relay_controller._relay_daemon, "pulse", return_value=True) as pulse:
            self.assertTrue(relay_controller.open_door("HARVEY"))

        pulse.assert_called_once_with(2, 0.5)

    def test_cli_fallback_preserves_access(self):
        with patch.object(relay_controller._relay_daemon, "pulse", return_value=False):
            with patch.object(relay_controller, "_legacy_pulse", return_value=True) as fallback:
                self.assertTrue(relay_controller.open_door("jones", duration=0.25))

        fallback.assert_called_once_with(1, 0.25)

    def test_unknown_door_never_sends_a_command(self):
        with patch.object(relay_controller._relay_daemon, "pulse") as pulse:
            self.assertFalse(relay_controller.open_door("unknown"))

        pulse.assert_not_called()


if __name__ == "__main__":
    unittest.main()
