import math
import logging
from datetime import timedelta
from quantnet_controller.common.experimentdefinitions import Experiment, AgentSequences, Sequence, get_num_timeslot
from quantnet_controller.common.constants import Constants
from collections import defaultdict

# Duration of one DQC timeslot from the partitioner (timeslot_schedule.json).
# Multiple DQC timeslots may fit inside a single agent scheduler slot (SLOTSIZE).
DQC_TIMESLOT_MS = 3

logger = logging.getLogger(__name__)


class DQCLogic:
    def __init__(self, context):
        self.context = context

    @staticmethod
    def extract_qpu_pairs(commands_list):
        """Return unique canonical (src, dst) QPU pairs from cross-QPU commands.

        A cross-QPU command is one whose ``qpus_involved`` list contains two or
        more QPU IDs (e.g. an entanglement-generation operation).  Pairs are
        returned in sorted order so that (A, B) and (B, A) map to the same key.

        :param commands_list: Flat list of command dicts from the DQC request.
        :returns: Deduplicated list of ``(qpu_a, qpu_b)`` string tuples.
        :rtype: list[tuple[str, str]]
        """
        pairs = set()
        for cmd in commands_list:
            qpus = cmd.get("qpus_involved") or []
            if len(qpus) >= 2:
                src, dst = str(qpus[0]), str(qpus[1])
                pairs.add(tuple(sorted([src, dst])))
        return list(pairs)

    def extract_bsm_nodes_from_path(self, path):
        """Return the first available BSMNode ID from a router Path object.

        Iterates over *path.hops* in order and returns a single-element list
        containing the first BSM node whose latest agent state is ``IN_SPEC``.
        If no BSM node is available, returns an empty list.

        :param path: Path object returned by ``router.find_path``.
        :type path: quantnet_controller.common.plugin.Path
        :returns: List with at most one BSMNode ID string.
        :rtype: list[str]
        """
        if path is None or path.hops is None:
            return []
        for hop in path.hops:
            if not (hasattr(hop, "systemSettings") and hop.systemSettings.type == "BSMNode"):
                continue
            bsm_id = str(hop.systemSettings.ID)
            state = self.context.rm.get_node_state(bsm_id)
            if state and state.get("value") == "IN_SPEC":
                logger.debug(f"Selected BSM node {bsm_id} (IN_SPEC)")
                return [bsm_id]
            logger.debug(f"Skipping BSM node {bsm_id} (state={state.get('value') if state else 'unknown'})")
        return []

    def build_dynamic_experiment(self, exp_name, commands_list, node_types=None, egp_sequences=None):
        """Build a dynamic Experiment class from the commands list.

        Each generated sequence block will store its original commands in a
        ``command_list`` class attribute.

        For cross-QPU entanglement commands, callers may supply *egp_sequences*
        to inject real EGP ``Sequence`` leaf classes (e.g. ``QnodeEGP``,
        ``BSMnodeEGP``) at the correct position in each agent's sequence list
        rather than generating an opaque ``BlockSequence`` placeholder.  This
        enables the unified experiment submission to carry both local gate
        blocks and real hardware entanglement sequences in a single
        ``experiment.submit`` call per agent.

        :param exp_name: Unique name for the generated experiment class.
        :param commands_list: Flat list of command dicts (gate + entanglement).
        :param node_types: Optional dict mapping agent/QPU IDs to their node
            type string (e.g. ``{"LBNL-BSM": "BSMNode"}``).
        :param egp_sequences: Optional nested dict
            ``{ agent_id: { entanglement_label: Sequence class } }``.
            When a command block's entanglement label appears here for the
            current agent, the corresponding EGP ``Sequence`` class is inserted
            directly instead of a ``BlockSequence``.  Pure-EGP agents (e.g. BSM
            nodes) that have no gate blocks in *commands_list* are also built
            from this dict.
        :returns: The generated Experiment class, or ``None`` if there is
            nothing to schedule.
        """
        if egp_sequences is None:
            egp_sequences = {}

        # Collect all agent IDs: those with gate commands plus any pure-EGP
        # agents (e.g. BSM nodes) that appear only in egp_sequences.
        commands_by_agent = defaultdict(list)
        for cmd in commands_list:
            qpu_id = str(cmd.get("qpu_id"))
            commands_by_agent[qpu_id].append(cmd)

        if node_types is None:
            node_types = {}

        all_agent_ids = sorted(
            set(commands_by_agent.keys()) | set(egp_sequences.keys())
        )

        if not all_agent_ids:
            return None

        agent_ids = all_agent_ids

        class DynamicExperiment(Experiment):
            name = exp_name
            agent_sequences = []

            @classmethod
            def get_allocations(cls, slots_to_allocate, slot_size_sec):
                """Transform flat allocated slots into enriched command blocks.

                EGP-injected sequences (``QnodeEGP``, ``BSMnodeEGP``) have no
                ``command_list`` attribute; they produce an entry with an empty
                ``"commands"`` list so the slot offset is still visible to the
                simulation caller without crashing.
                """
                allocations = {}
                # Match agent sequences to allocated slots in order
                for agent_id, agent_seq in zip(agent_ids, cls.agent_sequences):
                    agent_slots = slots_to_allocate[agent_id]
                    slot_ptr = 0
                    blocks = []
                    for seq in agent_seq.sequences:
                        num = get_num_timeslot(seq)
                        block_slots = agent_slots[slot_ptr: slot_ptr + num]
                        if block_slots:
                            blocks.append(
                                {
                                    "offset": round(block_slots[0] * slot_size_sec, 6),
                                    "commands": [
                                        {"slot": s, "message": c}
                                        for s, c in zip(block_slots, getattr(seq, "command_list", []))
                                    ],
                                }
                            )
                        slot_ptr += num
                    allocations[agent_id] = blocks
                return allocations

        for agent_id in agent_ids:
            cmds = sorted(commands_by_agent.get(agent_id, []), key=lambda x: x.get("timeslot", 0))

            _agent_node_type = node_types.get(agent_id, "QNode")
            _agent_egp_map = egp_sequences.get(agent_id, {})  # { ent_label: Sequence class }

            class DynamicAgentSeq(AgentSequences):
                name = f"Seq_{agent_id}"
                node_type = _agent_node_type
                sequences = []

            # ── Pure-EGP agent (e.g. BSM node with no gate blocks) ────────────
            # Build its sequences entirely from egp_sequences in label order.
            if not cmds and _agent_egp_map:
                pure_egp_seqs = []
                for label, egp_seq_cls in sorted(_agent_egp_map.items()):
                    # Give the EGP sequence a unique dependency chain so the
                    # scheduler can order them correctly on the agent.
                    egp_deps = [pure_egp_seqs[-1].name] if pure_egp_seqs else []
                    # We create a thin subclass so we can attach the dependency
                    # list without mutating the shared EGP class definition.
                    seq_name = f"EGP_{agent_id}_{label}"
                    EGPSeq = type(seq_name, (egp_seq_cls,), {
                        "name": egp_seq_cls.name,
                        "class_name": egp_seq_cls.class_name,
                        "duration": egp_seq_cls.duration,
                        "dependency": egp_deps,
                    })
                    pure_egp_seqs.append(EGPSeq)
                DynamicAgentSeq.sequences = pure_egp_seqs
                DynamicExperiment.agent_sequences.append(DynamicAgentSeq)
                continue

            # ── Gate-bearing agent (QPU) ──────────────────────────────────────
            # First pass: collect raw blocks (list of command dicts).
            # Boundaries are detected by changes in qpus_involved — a cross-QPU
            # entanglement block differs from a local-gate block.
            raw_blocks = []
            current_block = []
            prev_qpus_involved = None
            for cmd in cmds:
                qpus_inv = cmd.get("qpus_involved") or []
                current_qpus_involved = set(str(q) for q in qpus_inv)
                if prev_qpus_involved is not None and current_qpus_involved != prev_qpus_involved:
                    if current_block:
                        raw_blocks.append(current_block)
                    current_block = []
                current_block.append(cmd)
                prev_qpus_involved = current_qpus_involved
            if current_block:
                raw_blocks.append(current_block)

            # Second pass: assign agent-slot durations carrying the fractional
            # remainder forward so leftover time from one block is absorbed by
            # the next block rather than wasting a full agent slot on every
            # block boundary.
            #
            # EGP-injected sequences are inserted verbatim at their block
            # position and do NOT participate in the carry-forward accumulator
            # (they have their own fixed hardware duration).
            #
            # e.g. SLOTSIZE=100ms, DQC_TIMESLOT=3ms:
            #   block 0: 19 cmds → 57ms total,  ceil(57/100)=1 slot  allocated so far=1
            #   block 1:  1 cmd  → 60ms total,  ceil(60/100)=1 slot  delta=0 → use 1 (min)
            #   block 2:  7 cmds → 81ms total,  ceil(81/100)=1 slot  delta=0 → use 1 (min)
            #   block 3:  1 cmd  → 84ms total,  ceil(84/100)=1 slot  delta=0 → use 1 (min)
            #   block 4:  5 cmds → 99ms total,  ceil(99/100)=1 slot  delta=0 → use 1 (min)
            #   block 5:  2 cmds → 105ms total, ceil(105/100)=2 slots delta=1 → use 1
            #   ...total = 6 agent slots for 6 blocks (vs 6 without packing)
            # When commands pack tightly, multiple blocks share a slot until the
            # ceiling increments — only then is a new slot consumed.
            slotsize_ms = Constants.SLOTSIZE.total_seconds() * 1000
            total_dqc_ms = 0.0
            slots_allocated = 0
            agent_sequences_list = []
            for b_idx, block in enumerate(raw_blocks):
                first_cmd_dict = block[0]
                first_cmd_str = str(first_cmd_dict.get("command") or "")
                first_op = first_cmd_str.split(" ")[0] if first_cmd_str else "Unknown"

                # ── EGP injection check ───────────────────────────────────────
                # If this block contains an entanglement_gen command and the
                # caller provided an EGP Sequence class for its label, use that
                # real hardware sequence instead of a BlockSequence placeholder.
                ent_label = first_cmd_dict.get("entanglement_label")
                egp_cls = _agent_egp_map.get(ent_label) if ent_label else None

                if egp_cls is not None:
                    # EGP sequence: fixed hardware duration, no command_list.
                    # Create a thin subclass with a unique name and dependency.
                    egp_deps = [agent_sequences_list[-1].name] if agent_sequences_list else []
                    seq_name = f"EGP_{agent_id}_{ent_label}"
                    EGPSeq = type(seq_name, (egp_cls,), {
                        "name": egp_cls.name,
                        "class_name": egp_cls.class_name,
                        "duration": egp_cls.duration,
                        "dependency": egp_deps,
                    })
                    agent_sequences_list.append(EGPSeq)
                    # Do NOT update total_dqc_ms / slots_allocated — EGP
                    # sequences have their own fixed duration managed by the
                    # scheduler, not by the DQC carry-forward accumulator.
                    logger.debug(
                        f"[DQC] Injected EGP sequence {egp_cls.__name__} "
                        f"for agent {agent_id} label {ent_label}"
                    )
                    continue

                # ── Standard DQC gate block ───────────────────────────────────
                seq_name = f"Block_{b_idx}_{first_op}"

                total_dqc_ms += len(block) * DQC_TIMESLOT_MS
                new_total_slots = math.ceil(total_dqc_ms / slotsize_ms)
                agent_slots = max(new_total_slots - slots_allocated, 1)
                slots_allocated += agent_slots

                deps = [agent_sequences_list[-1].name] if agent_sequences_list else []
                total_duration = Constants.SLOTSIZE * agent_slots
                cmd_list = [c.get("command") for c in block]

                BlockSequence = type(seq_name, (Sequence,), {
                    "name": seq_name,
                    "class_name": seq_name,
                    "duration": total_duration,
                    "dependency": deps,
                    "command_list": cmd_list,
                })

                agent_sequences_list.append(BlockSequence)

            DynamicAgentSeq.sequences = agent_sequences_list
            DynamicExperiment.agent_sequences.append(DynamicAgentSeq)

        return DynamicExperiment
