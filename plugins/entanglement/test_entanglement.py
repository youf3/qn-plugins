"""
Unit tests for the continuous entanglement generation interface.

Tests the DummyEntanglementSource driver and the EntanglementInterpreter.
"""

import asyncio
import sys
import os
import unittest

# Add agent to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "qn-agent"))

from quantnet_agent.hal.driver.dummy_entanglement import DummyEntanglementSource


def run_async(coro):
    """Helper to run async tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestDummyEntanglementSource(unittest.TestCase):
    """Tests for the DummyEntanglementSource driver."""

    def setUp(self):
        self.source = DummyEntanglementSource(
            {"success_rate": "1.0", "generation_delay": "0", "pair_lifetime": "0"},
            "/dev/null", "localhost", 1883,
        )

    def test_capabilities(self):
        caps = run_async(self.source.capabilities())
        self.assertTrue(caps["continuous_generation"])
        self.assertGreater(caps["max_peers"], 0)
        self.assertGreater(caps["max_pool_size"], 0)

    def test_enable_creates_pool(self):
        resp = run_async(self.source.enable("QPU-B", {"pool_size": 3}))
        self.assertEqual(resp["status"], "ok")

        status = run_async(self.source.status("QPU-B"))
        self.assertTrue(status["available"])
        self.assertEqual(status["pairs"], 3)
        self.assertTrue(status["enabled"])

    def test_status_unknown_peer(self):
        status = run_async(self.source.status("UNKNOWN"))
        self.assertFalse(status["available"])
        self.assertEqual(status["pairs"], 0)
        self.assertFalse(status["enabled"])

    def test_consume_returns_pair(self):
        run_async(self.source.enable("QPU-B", {"pool_size": 2}))
        pair = run_async(self.source.consume("QPU-B"))
        self.assertIsNotNone(pair)
        self.assertIn("comm_qubit_local", pair)
        self.assertIn("comm_qubit_remote", pair)
        self.assertIn("fidelity", pair)
        self.assertIn("generation_time", pair)

        # Pool should have 1 remaining
        status = run_async(self.source.status("QPU-B"))
        self.assertEqual(status["pairs"], 1)

    def test_consume_empty_pool_returns_none(self):
        run_async(self.source.enable("QPU-B", {"pool_size": 1}))
        run_async(self.source.consume("QPU-B"))  # drain it
        pair = run_async(self.source.consume("QPU-B"))
        self.assertIsNone(pair)

    def test_consume_no_enable_returns_none(self):
        pair = run_async(self.source.consume("QPU-B"))
        self.assertIsNone(pair)

    def test_disable_clears_pool(self):
        run_async(self.source.enable("QPU-B", {"pool_size": 3}))
        resp = run_async(self.source.disable("QPU-B"))
        self.assertEqual(resp["status"], "ok")

        status = run_async(self.source.status("QPU-B"))
        self.assertFalse(status["available"])
        self.assertEqual(status["pairs"], 0)
        self.assertFalse(status["enabled"])

    def test_enable_default_pool_size(self):
        """enable() with no pool_size defaults to 1."""
        run_async(self.source.enable("QPU-B", {}))
        status = run_async(self.source.status("QPU-B"))
        self.assertEqual(status["pairs"], 1)

    def test_multiple_peers(self):
        run_async(self.source.enable("QPU-B", {"pool_size": 2}))
        run_async(self.source.enable("QPU-C", {"pool_size": 3}))

        status_b = run_async(self.source.status("QPU-B"))
        status_c = run_async(self.source.status("QPU-C"))
        self.assertEqual(status_b["pairs"], 2)
        self.assertEqual(status_c["pairs"], 3)

        # Consuming from one doesn't affect the other
        run_async(self.source.consume("QPU-B"))
        status_b = run_async(self.source.status("QPU-B"))
        status_c = run_async(self.source.status("QPU-C"))
        self.assertEqual(status_b["pairs"], 1)
        self.assertEqual(status_c["pairs"], 3)

    def test_cleanup(self):
        run_async(self.source.enable("QPU-B", {"pool_size": 2}))
        run_async(self.source.enable("QPU-C", {"pool_size": 3}))
        run_async(self.source.cleanUp())

        status_b = run_async(self.source.status("QPU-B"))
        status_c = run_async(self.source.status("QPU-C"))
        self.assertFalse(status_b["enabled"])
        self.assertFalse(status_c["enabled"])

    def test_pair_lifetime_expiry(self):
        """Pairs expire after pair_lifetime seconds."""
        import time
        source = DummyEntanglementSource(
            {"success_rate": "1.0", "generation_delay": "0",
             "pair_lifetime": "0.01"},  # 10ms lifetime
            "/dev/null", "localhost", 1883,
        )
        run_async(source.enable("QPU-B", {"pool_size": 2}))
        time.sleep(0.02)  # wait for expiry

        status = run_async(source.status("QPU-B"))
        # Pairs should be expired — status still shows pool but consume fails
        pair = run_async(source.consume("QPU-B"))
        self.assertIsNone(pair)


class TestEntanglementInterpreterNoDevice(unittest.TestCase):
    """Tests for EntanglementInterpreter when no device is configured."""

    def setUp(self):
        # Mock HAL with no entanglement_source device
        class MockHAL:
            devs = {}
            _config = type("C", (), {"cid": "test"})()

        sys.path.insert(0, os.path.join(
            os.path.dirname(__file__), "interpreter"))
        from entanglement import EntanglementInterpreter
        self.interp = EntanglementInterpreter(MockHAL())

    def test_capabilities_no_device(self):
        resp = run_async(self.interp.handle_capabilities({}))
        self.assertFalse(resp["continuous_generation"])

    def test_enable_no_device(self):
        req = type("R", (), {"payload": {"peer_id": "X", "config": {}}})()
        resp = run_async(self.interp.handle_enable(req))
        self.assertEqual(resp["status"], "error")

    def test_status_no_device(self):
        req = type("R", (), {"payload": {"peer_id": "X"}})()
        resp = run_async(self.interp.handle_status(req))
        self.assertFalse(resp["available"])

    def test_consume_no_device(self):
        req = type("R", (), {"payload": {"peer_id": "X"}})()
        resp = run_async(self.interp.handle_consume(req))
        self.assertIsNone(resp["pair"])

    def test_disable_no_device(self):
        req = type("R", (), {"payload": {"peer_id": "X"}})()
        resp = run_async(self.interp.handle_disable(req))
        self.assertEqual(resp["status"], "error")


class TestEntanglementInterpreterWithDevice(unittest.TestCase):
    """Tests for EntanglementInterpreter with DummyEntanglementSource."""

    def setUp(self):
        source = DummyEntanglementSource(
            {"success_rate": "1.0", "generation_delay": "0",
             "pair_lifetime": "0"},
            "/dev/null", "localhost", 1883,
        )

        class MockHAL:
            devs = {"entanglement_source": source}
            _config = type("C", (), {"cid": "test"})()

        sys.path.insert(0, os.path.join(
            os.path.dirname(__file__), "interpreter"))
        from entanglement import EntanglementInterpreter
        self.interp = EntanglementInterpreter(MockHAL())

    def test_full_lifecycle(self):
        """enable → status → consume → disable lifecycle."""
        # capabilities
        caps = run_async(self.interp.handle_capabilities({}))
        self.assertTrue(caps["continuous_generation"])

        # enable
        req = type("R", (), {"payload": {"peer_id": "QPU-B", "config": {"pool_size": 2}}})()
        resp = run_async(self.interp.handle_enable(req))
        self.assertEqual(resp["status"], "ok")

        # status
        req = type("R", (), {"payload": {"peer_id": "QPU-B"}})()
        resp = run_async(self.interp.handle_status(req))
        self.assertTrue(resp["available"])
        self.assertEqual(resp["pairs"], 2)

        # consume
        resp = run_async(self.interp.handle_consume(req))
        self.assertIsNotNone(resp["pair"])
        self.assertIn("comm_qubit_local", resp["pair"])

        # disable
        resp = run_async(self.interp.handle_disable(req))
        self.assertEqual(resp["status"], "ok")

        # status after disable
        resp = run_async(self.interp.handle_status(req))
        self.assertFalse(resp["enabled"])


if __name__ == "__main__":
    unittest.main()
