import os
import unittest
from unittest.mock import patch

from pymavlink import mavutil

from app import build_home_command, build_mission_items, resolve_fc_connection_string


class MissionPayloadTests(unittest.TestCase):
    def test_build_mission_items_contains_takeoff_waypoint_and_land(self):
        items = build_mission_items(-7.9666, 112.6326, 20.0, takeoff_alt_m=50.0)

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["command"], mavutil.mavlink.MAV_CMD_NAV_TAKEOFF)
        self.assertEqual(items[1]["command"], mavutil.mavlink.MAV_CMD_NAV_WAYPOINT)
        self.assertEqual(items[2]["command"], mavutil.mavlink.MAV_CMD_NAV_LAND)
        self.assertEqual(items[1]["x"], int(-7.9666 * 1e7))
        self.assertEqual(items[1]["y"], int(112.6326 * 1e7))
        self.assertEqual(items[0]["z"], 50.0)
        self.assertEqual(items[1]["z"], 20.0)

    def test_build_home_command_uses_given_coordinates(self):
        command = build_home_command(-7.9666, 112.6326, 10.0)

        self.assertEqual(command["command"], mavutil.mavlink.MAV_CMD_DO_SET_HOME)
        self.assertEqual(command["param1"], 1.0)
        self.assertEqual(command["param5"], -7.9666)
        self.assertEqual(command["param6"], 112.6326)
        self.assertEqual(command["param7"], 10.0)

    def test_resolve_fc_connection_string_prefers_env(self):
        with patch.dict(os.environ, {"FC_CONNECTION": "udp:127.0.0.1:14550"}, clear=False):
            self.assertEqual(resolve_fc_connection_string(), "udp:127.0.0.1:14550")


if __name__ == "__main__":
    unittest.main()
