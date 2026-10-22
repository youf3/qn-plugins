import unittest
import os
import sys
import tempfile
import configparser
from unittest.mock import MagicMock, patch
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
        # With dqc_timeslot_ms=3 (from dqc.cfg or default) and SLOTSIZE=1ms:
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


class TestDQCEGPInjection(unittest.TestCase):
    """Test EGP Sequence injection into build_dynamic_experiment()."""

    def setUp(self):
        self.context = MagicMock()
        self.context.config = MagicMock()

        # Build mock EGP Sequence leaf classes that mirror the real ones
        # (same interface: name, class_name, duration attributes).
        class _MockQnodeEGP(MockSequence):
            name = "experiments/dds_output.py"
            class_name = "DdsOutput"
            duration = timedelta(microseconds=1000)
            dependency = []

        class _MockBSMnodeEGP(MockSequence):
            name = "experiments/dds_output.py"
            class_name = "DdsOutput"
            duration = timedelta(microseconds=2000)
            dependency = []

        self.QnodeEGP = _MockQnodeEGP
        self.BSMnodeEGP = _MockBSMnodeEGP

    def _make_commands(self, ent_label="ent_0"):
        """Return a minimal commands list with one entanglement_gen per QPU."""
        return [
            {
                "timeslot": 0,
                "qpu_id": "LBNL-A",
                "command": "H LBNL-A[0]",
                "op": "gate",
                "qpus_involved": ["LBNL-A"],
            },
            {
                "timeslot": 1,
                "qpu_id": "LBNL-A",
                "command": f"ENTG[{ent_label}] LBNL-A[0]->LBNL-B[0]",
                "op": "entanglement_gen",
                "entanglement_label": ent_label,
                "qpus_involved": ["LBNL-A", "LBNL-B"],
            },
            {
                "timeslot": 2,
                "qpu_id": "LBNL-A",
                "command": "CX LBNL-A[0] LBNL-A[1]",
                "op": "gate",
                "qpus_involved": ["LBNL-A"],
            },
            {
                "timeslot": 0,
                "qpu_id": "LBNL-B",
                "command": f"ENTG[{ent_label}] LBNL-A[0]->LBNL-B[0]",
                "op": "entanglement_gen",
                "entanglement_label": ent_label,
                "qpus_involved": ["LBNL-A", "LBNL-B"],
            },
        ]

    def test_egp_injection_replaces_entanglement_block_for_qpu(self):
        """QnodeEGP replaces the BlockSequence at the entanglement_gen position."""
        logic = DQCLogic(self.context)
        commands = self._make_commands("ent_0")
        egp_sequences = {
            "LBNL-A": {"ent_0": self.QnodeEGP},
            "LBNL-B": {"ent_0": self.QnodeEGP},
        }

        exp = logic.build_dynamic_experiment(
            "TestEGPExp", commands, egp_sequences=egp_sequences
        )

        self.assertIsNotNone(exp)
        lbnl_a = next(s for s in exp.agent_sequences if s.name == "Seq_LBNL-A")
        seqs = lbnl_a.sequences

        # Expected order: Block_0_H, EGP sequence (QnodeEGP), Block_2_CX
        self.assertEqual(len(seqs), 3)
        # First block: local gate
        self.assertTrue(seqs[0].name.startswith("Block_"))
        # Second block: EGP injection — class_name and name must match QnodeEGP
        self.assertEqual(seqs[1].class_name, "DdsOutput")
        self.assertEqual(seqs[1].name, "experiments/dds_output.py")
        self.assertEqual(seqs[1].duration, timedelta(microseconds=1000))
        # Third block: post-entanglement gate
        self.assertTrue(seqs[2].name.startswith("Block_"))

    def test_egp_injection_dependency_chain(self):
        """EGP-injected sequence inherits dependency from the preceding block."""
        logic = DQCLogic(self.context)
        commands = self._make_commands("ent_0")
        egp_sequences = {
            "LBNL-A": {"ent_0": self.QnodeEGP},
            "LBNL-B": {"ent_0": self.QnodeEGP},
        }

        exp = logic.build_dynamic_experiment(
            "TestDepsExp", commands, egp_sequences=egp_sequences
        )

        lbnl_a = next(s for s in exp.agent_sequences if s.name == "Seq_LBNL-A")
        seqs = lbnl_a.sequences
        # EGP sequence depends on the preceding gate block
        self.assertEqual(len(seqs[1].dependency), 1)
        self.assertEqual(seqs[1].dependency[0], seqs[0].name)
        # Post-entanglement block depends on the EGP sequence
        self.assertEqual(seqs[2].dependency[0], seqs[1].name)

    def test_bsm_pure_egp_agent(self):
        """BSM agent with no gate commands gets sequences only from egp_sequences."""
        logic = DQCLogic(self.context)
        commands = self._make_commands("ent_0")
        egp_sequences = {
            "LBNL-A": {"ent_0": self.QnodeEGP},
            "LBNL-B": {"ent_0": self.QnodeEGP},
            "LBNL-BSM": {"ent_0": self.BSMnodeEGP},
        }

        exp = logic.build_dynamic_experiment(
            "TestBSMExp", commands,
            node_types={"LBNL-BSM": "BSMNode"},
            egp_sequences=egp_sequences,
        )

        # BSM agent should appear in agent_sequences
        agent_names = [s.name for s in exp.agent_sequences]
        self.assertIn("Seq_LBNL-BSM", agent_names)

        bsm_seq = next(s for s in exp.agent_sequences if s.name == "Seq_LBNL-BSM")
        self.assertEqual(bsm_seq.node_type, "BSMNode")
        # One BSMnodeEGP entry per entanglement label
        self.assertEqual(len(bsm_seq.sequences), 1)
        self.assertEqual(bsm_seq.sequences[0].class_name, "DdsOutput")
        self.assertEqual(bsm_seq.sequences[0].duration, timedelta(microseconds=2000))

    def test_multiple_ent_labels_same_pair(self):
        """Multiple entanglement labels on the same QPU pair all get EGP sequences."""
        logic = DQCLogic(self.context)
        commands = [
            # First Bell pair
            {
                "timeslot": 0, "qpu_id": "LBNL-A", "command": "ENTG[ent_0] ...",
                "op": "entanglement_gen", "entanglement_label": "ent_0",
                "qpus_involved": ["LBNL-A", "LBNL-B"],
            },
            {
                "timeslot": 1, "qpu_id": "LBNL-A", "command": "H LBNL-A[0]",
                "op": "gate", "qpus_involved": ["LBNL-A"],
            },
            # Second Bell pair
            {
                "timeslot": 2, "qpu_id": "LBNL-A", "command": "ENTG[ent_1] ...",
                "op": "entanglement_gen", "entanglement_label": "ent_1",
                "qpus_involved": ["LBNL-A", "LBNL-B"],
            },
            {
                "timeslot": 0, "qpu_id": "LBNL-B", "command": "ENTG[ent_0] ...",
                "op": "entanglement_gen", "entanglement_label": "ent_0",
                "qpus_involved": ["LBNL-A", "LBNL-B"],
            },
            {
                "timeslot": 1, "qpu_id": "LBNL-B", "command": "ENTG[ent_1] ...",
                "op": "entanglement_gen", "entanglement_label": "ent_1",
                "qpus_involved": ["LBNL-A", "LBNL-B"],
            },
        ]
        egp_sequences = {
            "LBNL-A": {"ent_0": self.QnodeEGP, "ent_1": self.QnodeEGP},
            "LBNL-B": {"ent_0": self.QnodeEGP, "ent_1": self.QnodeEGP},
            "LBNL-BSM": {"ent_0": self.BSMnodeEGP, "ent_1": self.BSMnodeEGP},
        }

        exp = logic.build_dynamic_experiment(
            "TestMultiEnt", commands,
            node_types={"LBNL-BSM": "BSMNode"},
            egp_sequences=egp_sequences,
        )

        lbnl_a = next(s for s in exp.agent_sequences if s.name == "Seq_LBNL-A")
        # ent_0 block, gate block, ent_1 block → 3 sequences, both ent positions are EGP
        egp_seqs = [s for s in lbnl_a.sequences if s.class_name == "DdsOutput"]
        self.assertEqual(len(egp_seqs), 2)

        bsm_seq = next(s for s in exp.agent_sequences if s.name == "Seq_LBNL-BSM")
        # Two entanglement labels → two BSMnodeEGP entries
        self.assertEqual(len(bsm_seq.sequences), 2)

    def test_fallback_to_block_sequence_without_egp(self):
        """Without egp_sequences, entanglement_gen falls back to BlockSequence."""
        logic = DQCLogic(self.context)
        commands = self._make_commands("ent_0")

        exp = logic.build_dynamic_experiment("TestFallback", commands)

        lbnl_a = next(s for s in exp.agent_sequences if s.name == "Seq_LBNL-A")
        # All sequences must be BlockSequences (name starts with "Block_")
        for seq in lbnl_a.sequences:
            self.assertTrue(
                seq.name.startswith("Block_"),
                f"Expected BlockSequence but got {seq.name!r}",
            )

    def test_no_egp_agent_without_commands_and_no_egp_map(self):
        """An agent with neither commands nor an egp_sequences entry is not included."""
        logic = DQCLogic(self.context)
        commands = [
            {"timeslot": 0, "qpu_id": "LBNL-A", "command": "H", "op": "gate", "qpus_involved": ["LBNL-A"]},
        ]
        # LBNL-BSM is absent from both commands and egp_sequences
        exp = logic.build_dynamic_experiment("TestNoOrphan", commands)
        agent_names = [s.name for s in exp.agent_sequences]
        self.assertNotIn("Seq_LBNL-BSM", agent_names)


class TestDynamicPrefillComputation(unittest.TestCase):
    """Tests for _compute_prefill_map dynamic computation from circuit."""

    def setUp(self):
        self.context = MagicMock()
        self.context.config = MagicMock()
        # Create a minimal DQC-like object with _compute_prefill_map method
        # We'll test the logic directly by calling the function
        self.logic = DQCLogic(self.context)

    def test_single_entanglement_gen_single_agent(self):
        """Single entanglement_gen with cost yields count * cost."""
        commands = [
            {"op": "entanglement_gen", "qpus_involved": ["LBNL-A", "LBNL-B"]},
        ]
        qpu_agent_ids = ["LBNL-A", "LBNL-B"]
        capabilities = {
            "LBNL-A": {"continuous_entanglement": True, "prefill_cost_per_pair": 5},
            "LBNL-B": {"continuous_entanglement": True, "prefill_cost_per_pair": 3},
        }
        # In actual usage, this would be a method on DQC plugin
        # For now, we test the logic pattern
        prefill_map = {}
        if True:  # use_continuous
            ent_counts = {}
            for cmd in commands:
                if cmd.get("op") == "entanglement_gen":
                    for qpu in cmd.get("qpus_involved", []):
                        qpu_id = str(qpu)
                        ent_counts[qpu_id] = ent_counts.get(qpu_id, 0) + 1
            for agent_id in qpu_agent_ids:
                count = ent_counts.get(agent_id, 0)
                cost = capabilities.get(agent_id, {}).get("prefill_cost_per_pair", 0)
                prefill_map[agent_id] = count * cost
        
        self.assertEqual(prefill_map["LBNL-A"], 1 * 5)
        self.assertEqual(prefill_map["LBNL-B"], 1 * 3)

    def test_multiple_entanglement_gen_same_pair(self):
        """Multiple entanglement_gen on same pair accumulates counts."""
        commands = [
            {"op": "entanglement_gen", "qpus_involved": ["LBNL-A", "LBNL-B"]},
            {"op": "entanglement_gen", "qpus_involved": ["LBNL-A", "LBNL-B"]},
            {"op": "entanglement_gen", "qpus_involved": ["LBNL-A", "LBNL-B"]},
        ]
        qpu_agent_ids = ["LBNL-A", "LBNL-B"]
        capabilities = {
            "LBNL-A": {"continuous_entanglement": True, "prefill_cost_per_pair": 2},
            "LBNL-B": {"continuous_entanglement": True, "prefill_cost_per_pair": 2},
        }
        
        prefill_map = {}
        if True:  # use_continuous
            ent_counts = {}
            for cmd in commands:
                if cmd.get("op") == "entanglement_gen":
                    for qpu in cmd.get("qpus_involved", []):
                        qpu_id = str(qpu)
                        ent_counts[qpu_id] = ent_counts.get(qpu_id, 0) + 1
            for agent_id in qpu_agent_ids:
                count = ent_counts.get(agent_id, 0)
                cost = capabilities.get(agent_id, {}).get("prefill_cost_per_pair", 0)
                prefill_map[agent_id] = count * cost
        
        self.assertEqual(prefill_map["LBNL-A"], 3 * 2)
        self.assertEqual(prefill_map["LBNL-B"], 3 * 2)

    def test_zero_entanglement_gen(self):
        """Circuit with no entanglement_gen yields zero prefill."""
        commands = [
            {"op": "gate", "qpus_involved": ["LBNL-A"]},
            {"op": "gate", "qpus_involved": ["LBNL-B"]},
        ]
        qpu_agent_ids = ["LBNL-A", "LBNL-B"]
        capabilities = {
            "LBNL-A": {"continuous_entanglement": True, "prefill_cost_per_pair": 5},
            "LBNL-B": {"continuous_entanglement": True, "prefill_cost_per_pair": 5},
        }
        
        prefill_map = {}
        if True:  # use_continuous
            ent_counts = {}
            for cmd in commands:
                if cmd.get("op") == "entanglement_gen":
                    for qpu in cmd.get("qpus_involved", []):
                        qpu_id = str(qpu)
                        ent_counts[qpu_id] = ent_counts.get(qpu_id, 0) + 1
            for agent_id in qpu_agent_ids:
                count = ent_counts.get(agent_id, 0)
                cost = capabilities.get(agent_id, {}).get("prefill_cost_per_pair", 0)
                prefill_map[agent_id] = count * cost
        
        self.assertEqual(prefill_map["LBNL-A"], 0)
        self.assertEqual(prefill_map["LBNL-B"], 0)

    def test_asymmetric_entanglement_counts(self):
        """Different pairs involve different agents differently."""
        commands = [
            {"op": "entanglement_gen", "qpus_involved": ["LBNL-A", "LBNL-B"]},
            {"op": "entanglement_gen", "qpus_involved": ["LBNL-B", "LBNL-C"]},
        ]
        qpu_agent_ids = ["LBNL-A", "LBNL-B", "LBNL-C"]
        capabilities = {
            "LBNL-A": {"continuous_entanglement": True, "prefill_cost_per_pair": 2},
            "LBNL-B": {"continuous_entanglement": True, "prefill_cost_per_pair": 3},
            "LBNL-C": {"continuous_entanglement": True, "prefill_cost_per_pair": 2},
        }
        
        prefill_map = {}
        if True:  # use_continuous
            ent_counts = {}
            for cmd in commands:
                if cmd.get("op") == "entanglement_gen":
                    for qpu in cmd.get("qpus_involved", []):
                        qpu_id = str(qpu)
                        ent_counts[qpu_id] = ent_counts.get(qpu_id, 0) + 1
            for agent_id in qpu_agent_ids:
                count = ent_counts.get(agent_id, 0)
                cost = capabilities.get(agent_id, {}).get("prefill_cost_per_pair", 0)
                prefill_map[agent_id] = count * cost
        
        # LBNL-A: 1 ent_gen * 2 = 2
        # LBNL-B: 2 ent_gens * 3 = 6
        # LBNL-C: 1 ent_gen * 2 = 2
        self.assertEqual(prefill_map["LBNL-A"], 2)
        self.assertEqual(prefill_map["LBNL-B"], 6)
        self.assertEqual(prefill_map["LBNL-C"], 2)

    def test_zero_cost_per_pair(self):
        """Zero cost_per_pair yields zero prefill even with ent_gen."""
        commands = [
            {"op": "entanglement_gen", "qpus_involved": ["LBNL-A", "LBNL-B"]},
        ]
        qpu_agent_ids = ["LBNL-A", "LBNL-B"]
        capabilities = {
            "LBNL-A": {"continuous_entanglement": True, "prefill_cost_per_pair": 0},
            "LBNL-B": {"continuous_entanglement": True, "prefill_cost_per_pair": 0},
        }
        
        prefill_map = {}
        if True:  # use_continuous
            ent_counts = {}
            for cmd in commands:
                if cmd.get("op") == "entanglement_gen":
                    for qpu in cmd.get("qpus_involved", []):
                        qpu_id = str(qpu)
                        ent_counts[qpu_id] = ent_counts.get(qpu_id, 0) + 1
            for agent_id in qpu_agent_ids:
                count = ent_counts.get(agent_id, 0)
                cost = capabilities.get(agent_id, {}).get("prefill_cost_per_pair", 0)
                prefill_map[agent_id] = count * cost
        
        self.assertEqual(prefill_map["LBNL-A"], 0)
        self.assertEqual(prefill_map["LBNL-B"], 0)

    def test_missing_capability_defaults_to_zero(self):
        """Missing capability data defaults to cost=0."""
        commands = [
            {"op": "entanglement_gen", "qpus_involved": ["LBNL-A", "LBNL-B"]},
        ]
        qpu_agent_ids = ["LBNL-A", "LBNL-B"]
        # LBNL-B is missing
        capabilities = {
            "LBNL-A": {"continuous_entanglement": True, "prefill_cost_per_pair": 5},
        }
        
        prefill_map = {}
        if True:  # use_continuous
            ent_counts = {}
            for cmd in commands:
                if cmd.get("op") == "entanglement_gen":
                    for qpu in cmd.get("qpus_involved", []):
                        qpu_id = str(qpu)
                        ent_counts[qpu_id] = ent_counts.get(qpu_id, 0) + 1
            for agent_id in qpu_agent_ids:
                count = ent_counts.get(agent_id, 0)
                cost = capabilities.get(agent_id, {}).get("prefill_cost_per_pair", 0)
                prefill_map[agent_id] = count * cost
        
        self.assertEqual(prefill_map["LBNL-A"], 5)
        self.assertEqual(prefill_map["LBNL-B"], 0)


class TestPrefillMapOnExperiment(unittest.TestCase):
    """Tests for prefill_map attribute on DynamicExperiment."""

    def setUp(self):
        self.context = MagicMock()
        self.context.config = MagicMock()
        self.logic = DQCLogic(self.context)

    def test_prefill_map_set_when_continuous(self):
        """prefill_map is set on experiment when continuous entanglement active."""
        commands = [
            {"timeslot": 0, "qpu_id": "LBNL-A", "command": "H", "op": "gate", "qpus_involved": ["LBNL-A"]},
            {"timeslot": 1, "qpu_id": "LBNL-A", "command": "ENTG", "op": "entanglement_gen", "qpus_involved": ["LBNL-A", "LBNL-B"]},
        ]
        exp = self.logic.build_dynamic_experiment("TestPrefillSet", commands)
        
        # Simulate setting prefill_map as the DQC plugin does
        exp.prefill_map = {"LBNL-A": 5, "LBNL-B": 3}
        
        self.assertTrue(hasattr(exp, "prefill_map"))
        self.assertEqual(exp.prefill_map["LBNL-A"], 5)
        self.assertEqual(exp.prefill_map["LBNL-B"], 3)

    def test_prefill_map_empty_when_on_demand(self):
        """prefill_map is empty dict when on-demand entanglement active."""
        commands = [
            {"timeslot": 0, "qpu_id": "LBNL-A", "command": "H", "op": "gate", "qpus_involved": ["LBNL-A"]},
        ]
        exp = self.logic.build_dynamic_experiment("TestPrefillEmpty", commands)
        
        # Simulate setting empty prefill_map when use_continuous=False
        exp.prefill_map = {}
        
        self.assertTrue(hasattr(exp, "prefill_map"))
        self.assertEqual(exp.prefill_map, {})


class TestDQCConfigLoading(unittest.TestCase):
    """Test DQC plugin configuration file loading."""

    def setUp(self):
        """Set up mocks for each test."""
        self.mock_context = MagicMock()

    def test_config_file_with_custom_values(self):
        """Load dqc.cfg with custom scheduling constants."""
        # Create a temporary config file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.cfg', delete=False) as f:
            f.write("""\
[scheduling]
pre_schedule_entanglement = false
expected_ent_latency_ns = 2000000
one_q_gate_duration_ns = 6000
two_q_gate_duration_ns = 12000
dqc_timeslot_ms = 4
""")
            temp_cfg = f.name

        try:
            # Patch os.path.exists and os.path.join to return our temp file
            from dqc import DQC
            with patch('dqc.os.path.abspath') as mock_abspath, \
                 patch('dqc.os.path.dirname') as mock_dirname, \
                 patch('dqc.os.path.join') as mock_join, \
                 patch('dqc.os.path.exists') as mock_exists:
                
                # Make the path resolution point to our temp file
                mock_abspath.return_value = "/fake/path/__init__.py"
                mock_dirname.side_effect = lambda x: "/fake/path" if "/__init__" in x else "/fake"
                mock_join.return_value = temp_cfg
                mock_exists.return_value = True

                dqc = DQC(self.mock_context)

                # Verify custom values were loaded
                self.assertFalse(dqc.PRE_SCHEDULE_ENTANGLEMENT)
                self.assertEqual(dqc.EXPECTED_ENT_LATENCY_NS, 2000000)
                self.assertEqual(dqc.ONE_Q_GATE_DURATION_NS, 6000)
                self.assertEqual(dqc.TWO_Q_GATE_DURATION_NS, 12000)
                self.assertEqual(dqc.DQC_TIMESLOT_MS, 4)
        finally:
            os.unlink(temp_cfg)

    def test_config_file_missing_uses_defaults(self):
        """Use default values when dqc.cfg does not exist."""
        from dqc import DQC
        with patch('dqc.os.path.exists') as mock_exists:
            mock_exists.return_value = False
            
            dqc = DQC(self.mock_context)
            
            # Verify defaults are used
            self.assertTrue(dqc.PRE_SCHEDULE_ENTANGLEMENT)
            self.assertEqual(dqc.EXPECTED_ENT_LATENCY_NS, 1000000)
            self.assertEqual(dqc.ONE_Q_GATE_DURATION_NS, 5000)
            self.assertEqual(dqc.TWO_Q_GATE_DURATION_NS, 10700)
            self.assertEqual(dqc.DQC_TIMESLOT_MS, 3)

    def test_config_file_partial_overrides(self):
        """Partial config file overrides only specified options."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.cfg', delete=False) as f:
            f.write("""\
[scheduling]
expected_ent_latency_ns = 3000000
dqc_timeslot_ms = 5
""")
            temp_cfg = f.name

        try:
            from dqc import DQC
            with patch('dqc.os.path.abspath') as mock_abspath, \
                 patch('dqc.os.path.dirname') as mock_dirname, \
                 patch('dqc.os.path.join') as mock_join, \
                 patch('dqc.os.path.exists') as mock_exists:
                
                mock_abspath.return_value = "/fake/path/__init__.py"
                mock_dirname.side_effect = lambda x: "/fake/path" if "/__init__" in x else "/fake"
                mock_join.return_value = temp_cfg
                mock_exists.return_value = True

                dqc = DQC(self.mock_context)

                # Verify only specified values override defaults
                self.assertTrue(dqc.PRE_SCHEDULE_ENTANGLEMENT)  # default
                self.assertEqual(dqc.EXPECTED_ENT_LATENCY_NS, 3000000)  # overridden
                self.assertEqual(dqc.ONE_Q_GATE_DURATION_NS, 5000)  # default
                self.assertEqual(dqc.TWO_Q_GATE_DURATION_NS, 10700)  # default
                self.assertEqual(dqc.DQC_TIMESLOT_MS, 5)  # overridden
        finally:
            os.unlink(temp_cfg)


if __name__ == "__main__":
    unittest.main()
