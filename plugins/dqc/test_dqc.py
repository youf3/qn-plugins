import unittest
import os
import sys
from unittest.mock import MagicMock
from datetime import timedelta

# --- MOCKING MODULES BEFORE ANY DQC IMPORTS ---
# dqc/__init__.py imports quantnet_controller and quantnet_mq at module level.
# All mocks must be installed before dqc (or dqc.logic) is first imported.

mock_exp_defs = MagicMock()


class MockExperiment:
    pass


class MockAgentSequences:
    pass


class MockSequence:
    pass


mock_exp_defs.Experiment = MockExperiment
mock_exp_defs.AgentSequences = MockAgentSequences
mock_exp_defs.Sequence = MockSequence
mock_exp_defs.Sequence.duration = timedelta(seconds=0)
mock_exp_defs.get_num_timeslot = MagicMock(return_value=1)

sys.modules["quantnet_controller"] = MagicMock()
sys.modules["quantnet_controller.common"] = MagicMock()
sys.modules["quantnet_controller.common.experimentdefinitions"] = mock_exp_defs
mock_constants = MagicMock()
mock_constants.Constants.SLOTSIZE = timedelta(milliseconds=1)
sys.modules["quantnet_controller.common.constants"] = mock_constants

mock_request = MagicMock()
mock_request.RequestManager = MagicMock()
mock_request.RequestType = MagicMock()
mock_request.RequestParameter = MagicMock()
sys.modules["quantnet_controller.common.request"] = mock_request

mock_translator_mod = MagicMock()
mock_translator_class = MagicMock()
mock_translator_mod.RequestTranslator = mock_translator_class
sys.modules["quantnet_controller.common.request_translator"] = mock_translator_mod

class _NoOpProtocolPlugin:
    """Minimal stand-in for ProtocolPlugin that accepts (name, type, context)."""
    def __init__(self, name, plugin_type, context):
        pass

mock_plugin = MagicMock()
mock_plugin.ProtocolPlugin = _NoOpProtocolPlugin
mock_plugin.PluginType = MagicMock()
sys.modules["quantnet_controller.common.plugin"] = mock_plugin

mock_mq = MagicMock()
mock_mq.Code = MagicMock()
mock_schema = MagicMock()
mock_mq.schema = mock_schema
sys.modules["quantnet_mq"] = mock_mq
sys.modules["quantnet_mq.schema"] = mock_schema
sys.modules["quantnet_mq.schema.models"] = MagicMock()

# Make `from logic import DQCLogic` and sibling imports inside dqc/__init__.py
# resolve correctly. The plugin runtime adds the plugin folder to sys.path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "dqc"))

# Now it is safe to import from the dqc package.
from dqc.logic import DQCLogic


class TestDQCLogic(unittest.TestCase):
    def setUp(self):
        self.context = MagicMock()
        self.context.config = MagicMock()

    def test_build_dynamic_experiment(self):
        logic = DQCLogic(self.context)

        # Flat array structure with qpus_involved
        commands_list = [
            {"timeslot": 0, "qpu_id": "LBNL-A", "command": "waiting", "qpus_involved": []},
            {
                "timeslot": 1,
                "qpu_id": "LBNL-A",
                "command": "ENTG server_1_link_register[0], server_0_link_register[0]",
                "qpus_involved": ["LBNL-A", "LBNL-B"],
            },
            {
                "timeslot": 2,
                "qpu_id": "LBNL-A",
                "command": "CU1(1) server_0_link_register[1], server_0_link_register[0]",
                "qpus_involved": ["LBNL-A"],
            },
            {"timeslot": 3, "qpu_id": "LBNL-A", "command": "H server_1[0]", "qpus_involved": ["LBNL-A"]},
            {"timeslot": 0, "qpu_id": "LBNL-B", "command": "waiting", "qpus_involved": []},
            {
                "timeslot": 1,
                "qpu_id": "LBNL-B",
                "command": "ENTG server_1_link_register[0], server_0_link_register[0]",
                "qpus_involved": ["LBNL-A", "LBNL-B"],
            },
        ]

        dynamic_experiment = logic.build_dynamic_experiment("TestExp", commands_list)

        self.assertEqual(dynamic_experiment.name, "TestExp")
        # agent_ids = ["LBNL-A", "LBNL-B"]

        # Check Agent LBNL-A Sequences
        # Note: build_dynamic_experiment doesn't return agent_ids as a separate list anymore,
        # but they are in DynamicExperiment.agent_sequences named Seq_<agent_id>
        agent_seq_names = [s.name for s in dynamic_experiment.agent_sequences]
        self.assertIn("Seq_LBNL-A", agent_seq_names)
        self.assertIn("Seq_LBNL-B", agent_seq_names)

        agent1_seq = [s for s in dynamic_experiment.agent_sequences if s.name == "Seq_LBNL-A"][0]
        seqs = agent1_seq.sequences

        self.assertEqual(len(seqs), 3)
        self.assertEqual(seqs[0].name, "Block_0_waiting")
        # With DQC_TIMESLOT_MS=3 and SLOTSIZE=1ms:
        # block_0: 1 cmd → 3ms DQC → ceil(3ms/1ms)=3 slots → 3ms duration
        self.assertEqual(seqs[0].duration, timedelta(milliseconds=3))

        self.assertEqual(seqs[1].name, "Block_1_ENTG")
        # block_1: 1 cmd → 6ms DQC total → ceil(6ms/1ms)=6, delta=6-3=3 slots → 3ms
        self.assertEqual(seqs[1].duration, timedelta(milliseconds=3))

        self.assertEqual(seqs[2].name, "Block_2_CU1(1)")
        # block_2: 2 cmds → 12ms DQC total → ceil(12ms/1ms)=12, delta=12-6=6 slots → 6ms
        self.assertEqual(seqs[2].duration, timedelta(milliseconds=6))

        # Check dependencies within LBNL-A
        self.assertEqual(seqs[0].dependency, [])
        self.assertEqual(seqs[1].dependency, ["Block_0_waiting"])
        self.assertEqual(seqs[2].dependency, ["Block_1_ENTG"])

        # Check Agent LBNL-B Sequences
        agent2_seq = [s for s in dynamic_experiment.agent_sequences if s.name == "Seq_LBNL-B"][0]
        seqs2 = agent2_seq.sequences

        self.assertEqual(len(seqs2), 2)
        self.assertEqual(seqs2[0].name, "Block_0_waiting")
        self.assertEqual(seqs2[1].name, "Block_1_ENTG")

    def test_extract_qpu_pairs_basic(self):
        """Cross-QPU commands produce canonical, deduplicated pairs."""
        logic = DQCLogic(self.context)
        commands = [
            {"timeslot": 0, "qpu_id": "LBNL-A", "command": "waiting", "qpus_involved": []},
            {"timeslot": 1, "qpu_id": "LBNL-A", "command": "ENTG ...", "qpus_involved": ["LBNL-A", "LBNL-B"]},
            {"timeslot": 1, "qpu_id": "LBNL-B", "command": "ENTG ...", "qpus_involved": ["LBNL-A", "LBNL-B"]},
            {"timeslot": 2, "qpu_id": "LBNL-A", "command": "H ...", "qpus_involved": ["LBNL-A"]},
        ]
        pairs = logic.extract_qpu_pairs(commands)
        # Duplicate cross-QPU entry should appear only once
        self.assertEqual(len(pairs), 1)
        self.assertIn(("LBNL-A", "LBNL-B"), pairs)

    def test_extract_qpu_pairs_canonical_order(self):
        """Pairs are stored in sorted order regardless of which QPU appears first."""
        logic = DQCLogic(self.context)
        commands = [
            {"timeslot": 0, "qpu_id": "LBNL-B", "command": "ENTG ...", "qpus_involved": ["LBNL-B", "LBNL-A"]},
        ]
        pairs = logic.extract_qpu_pairs(commands)
        self.assertEqual(pairs, [("LBNL-A", "LBNL-B")])

    def test_extract_qpu_pairs_empty(self):
        """Commands with no cross-QPU involvement return an empty list."""
        logic = DQCLogic(self.context)
        commands = [
            {"timeslot": 0, "qpu_id": "LBNL-A", "command": "H ...", "qpus_involved": ["LBNL-A"]},
            {"timeslot": 1, "qpu_id": "LBNL-A", "command": "waiting", "qpus_involved": None},
            {"timeslot": 2, "qpu_id": "LBNL-A", "command": "waiting", "qpus_involved": []},
        ]
        pairs = logic.extract_qpu_pairs(commands)
        self.assertEqual(pairs, [])

    def test_extract_qpu_pairs_multiple(self):
        """Multiple distinct QPU pairs are all returned."""
        logic = DQCLogic(self.context)
        commands = [
            {"timeslot": 0, "qpu_id": "LBNL-A", "command": "ENTG ...", "qpus_involved": ["LBNL-A", "LBNL-B"]},
            {"timeslot": 1, "qpu_id": "LBNL-B", "command": "ENTG ...", "qpus_involved": ["LBNL-B", "LBNL-C"]},
        ]
        pairs = logic.extract_qpu_pairs(commands)
        self.assertEqual(len(pairs), 2)
        self.assertIn(("LBNL-A", "LBNL-B"), pairs)
        self.assertIn(("LBNL-B", "LBNL-C"), pairs)


class TestDQCPreScheduling(unittest.TestCase):
    """Test that the DQC plugin applies pre-entanglement scheduling."""

    def setUp(self):
        # The module-level mocks (quantnet_controller, quantnet_mq) are already
        # installed before this class is loaded, so DQC can be imported here.
        from dqc import DQC
        self.context = MagicMock()
        self.context.config = MagicMock()
        self.dqc = DQC(self.context)

    def test_pre_schedule_moves_entanglement_gen_earlier(self):
        """_pre_schedule() moves entanglement_gen earlier when enabled."""
        labeled = {
            1: [
                {"op": "gate", "gate": "h", "qubits": [20], "original_qubits": ["q[0]"]},
                {"op": "gate", "gate": "h", "qubits": [21], "original_qubits": ["q[1]"]},
                {
                    "op": "entanglement_gen",
                    "qubits": [0],
                    "role": "emitter",
                    "entanglement_label": "ent_0",
                    "start_label": "sl_0",
                    "target_start_label": "sl_0",
                },
                {"op": "starting_process", "start_label": "sl_0", "l_local": 0},
            ],
            2: [
                {"op": "gate", "gate": "x", "qubits": [20], "original_qubits": ["q[0]"]},
                {
                    "op": "entanglement_gen",
                    "qubits": [0],
                    "role": "peer",
                    "entanglement_label": "ent_0",
                    "start_label": "sl_0",
                    "target_start_label": "sl_0",
                },
                {"op": "starting_process_link", "start_label": "sl_0", "qubits": [0]},
            ],
        }
        stats = self.dqc._pre_schedule(labeled)

        self.assertIsNotNone(stats)
        self.assertEqual(stats["total_pairs"], 1)
        self.assertEqual(stats["moved_pairs"], 1)
        # entanglement_gen on QPU 2 should have been pulled back to position 0
        # (min feasible pullback = 1, the single local gate on QPU 2)
        self.assertEqual(labeled[2][0]["op"], "entanglement_gen")

    def test_pre_schedule_disabled(self):
        """_pre_schedule() returns None when PRE_SCHEDULE_ENTANGLEMENT is False."""
        self.dqc.PRE_SCHEDULE_ENTANGLEMENT = False
        labeled = {1: [], 2: []}
        stats = self.dqc._pre_schedule(labeled)
        self.assertIsNone(stats)

    def test_pre_schedule_no_entanglement_commands(self):
        """_pre_schedule() handles a circuit with no entanglement_gen commands."""
        labeled = {
            1: [
                {"op": "gate", "gate": "h", "qubits": [20], "original_qubits": ["q[0]"]},
            ],
        }
        stats = self.dqc._pre_schedule(labeled)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["total_pairs"], 0)
        self.assertEqual(stats["moved_pairs"], 0)

    def test_build_sim_payload_sets_pre_scheduled_flag(self):
        """_build_sim_payload() includes pre_scheduled=True when requested."""
        labeled = {1: [{"op": "gate", "gate": "h", "qubits": [0], "original_qubits": []}]}
        process_maps = {
            "start_qpus": {},
            "end_qpus": {},
            "entanglement_gen_labels": set(),
        }
        topology = []

        payload_with = self.dqc._build_sim_payload(
            labeled, process_maps, topology, pre_scheduled=True
        )
        self.assertTrue(payload_with.get("pre_scheduled"))

        payload_without = self.dqc._build_sim_payload(
            labeled, process_maps, topology, pre_scheduled=False
        )
        self.assertNotIn("pre_scheduled", payload_without)


if __name__ == "__main__":
    unittest.main()
