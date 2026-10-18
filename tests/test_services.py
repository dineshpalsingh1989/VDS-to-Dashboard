import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import app


class OpcUaTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_lifecycle_and_stable_readonly_nodes(self):
        devices = {
            "GW1": {"rpm": 1200, "channels": {"1": {"metrics": {"Velocity": 2.5}}}}
        }

        async def finish_cycle(delay):
            self.assertTrue(app.opcua_running)
            rpm = app.opcua_node_map["GW1"]["rpm_node"]
            self.assertEqual(rpm.nodeid.Identifier, "Device_GW1.RPM")
            self.assertEqual(await rpm.read_value(), 1200.0)
            metric = app.opcua_node_map["GW1"]["channels"]["1"]["metrics"]["Velocity"]
            self.assertEqual(metric.nodeid.Identifier, "Device_GW1.Channel_1.Velocity")
            self.assertNotIn(
                app.ua.AccessLevel.CurrentWrite, await rpm.get_access_level()
            )
            app.opcua_enabled_flag = False

        with (
            patch.object(app, "data_storage", devices),
            patch.object(app, "opcua_enabled_flag", True),
            patch.object(app.Server, "start", new_callable=AsyncMock) as start,
            patch.object(app.Server, "stop", new_callable=AsyncMock) as stop,
            patch.object(app.asyncio, "sleep", side_effect=finish_cycle),
        ):
            await app.run_opcua_server()
            start.assert_awaited_once()
            stop.assert_awaited_once()
            self.assertFalse(app.opcua_running)
            self.assertEqual(app.opcua_node_map, {})


if __name__ == "__main__":
    unittest.main()
