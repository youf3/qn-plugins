"""
DQC Plugin — Distributed Quantum Computing request handler.

Accepts a partitioned circuit (per-QPU command dicts), labels it using
qnpack's labeling pipeline, schedules execution on hardware agents, and
sends the labeled commands to qnpack for NetSquid simulation.

Input circuit can be provided in two ways:
  1. ``partitioned_commands`` — already parsed per-QPU command dicts
     (as produced by a qnpack frontend).  The plugin skips frontend.parse()
     and goes straight to validate + label.
  2. ``circuit_file`` or ``circuit_content`` — raw circuit source.
     The plugin calls the appropriate qnpack frontend to parse it first.

After scheduling, the labeled commands are serialized to JSON and sent to
qnpack's ``run_from_labeled()`` receiver for simulation.
"""

import json
import logging
import os
import tempfile

from munch import Munch

from quantnet_controller.common.plugin import ProtocolPlugin, PluginType
from quantnet_controller.common.request import RequestManager, RequestType, RequestParameter
from quantnet_controller.common.constants import Constants
from quantnet_mq import Code
from quantnet_mq.schema.models import dqc, Status as responseStatus

from logic import DQCLogic
from topology_adapter import get_qpu_info_from_topology
from ir_converter import labeled_ir_to_timeslot_schedule

# qnpack imports — pure Python, no NetSquid dependency
from qnpack.dqc.frontends import load_frontend
from qnpack.dqc.labeling import label_and_build_maps
from qnpack.dqc.models.validation import validate_commands
from qnpack.dqc.protocols.controller import insert_pre_entanglement_commands

logger = logging.getLogger(__name__)


def _to_plain(obj):
    """Recursively convert schema proxy objects (Munch, etc.) to plain Python.

    The incoming partitioned_commands come through the schema layer as proxy
    objects; their field values may be proxy types that don't have .lower(),
    list concatenation, etc.  This ensures validate_commands() and the labeler
    receive plain str/int/list/dict/bool/None values.
    """
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(i) for i in obj]
    # Munch and similar proxy types expose items() like a dict
    if hasattr(obj, "items") and not isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    return obj


class DQC(ProtocolPlugin):
    # ── Pre-entanglement scheduling defaults ──────────────────────────────────
    # These control whether entanglement_gen commands are moved earlier in
    # the QPU schedule to overlap Bell-pair generation latency with local
    # gate execution.  Change the constants here to tune the behaviour;
    # they will be relocated to plugin config in a future change.
    PRE_SCHEDULE_ENTANGLEMENT = True
    EXPECTED_ENT_LATENCY_NS = 1_000_000      # 1 ms — expected Bell-pair gen time
    ONE_Q_GATE_DURATION_NS = 5000            # ns per single-qubit gate
    TWO_Q_GATE_DURATION_NS = 10700           # ns per two-qubit gate

    def __init__(self, context):
        super().__init__("dqc", PluginType.PROTOCOL, context)
        self._server_commands = [
            ("dqcRequest", self.handle_dqc_request, "quantnet_mq.schema.models.dqc.dqcRequest"),
        ]
        self.ctx = context

        # Initialize logic handler
        self.logic = DQCLogic(context)

        # Initialize RequestManager
        self.request_manager = RequestManager(
            context, plugin_schema=dqc.dqcRequest, request_type=RequestType.EXPERIMENT
        )

    def initialize(self):
        pass

    def destroy(self):
        pass

    def reset(self):
        pass

    # ── Public request handler ────────────────────────────────────────────────

    async def handle_dqc_request(self, request):
        """Handle incoming DQC request.

        Parses (if needed), labels, schedules, and simulates the circuit.
        """
        dqc_req = dqc.dqcRequest(**request)
        logger.info(f"Received DQC request: {dqc_req.serialize()}")

        try:
            # Deserialize via the schema model to get plain Python dicts with
            # no proxy objects — request['payload'] contains schema proxies
            # whose field values aren't plain str/int, causing .lower() to fail
            # inside validate_commands.
            payload = json.loads(dqc_req.serialize()).get("payload", {})

            # ── 1. Get per-QPU command dicts (parse if needed) ────────────────
            partitioned, circuit_mode, frontend_meta = self._get_partitioned_commands(payload)

            # ── 2. Validate + Label via qnpack ────────────────────────────────
            labeled, process_maps = self._label_commands(partitioned)

            # ── 2b. Pre-schedule entanglement commands ────────────────────────
            pre_ent_stats = self._pre_schedule(labeled)

            # ── 3. Convert labeled IR → flat timeslot schedule ────────────────
            qpu_info, qpu_id_to_label, label_to_qpu_id, bsm_nodes, raw_topology = get_qpu_info_from_topology(self.ctx)
            commands = labeled_ir_to_timeslot_schedule(labeled, process_maps, qpu_id_to_label)

            agent_ids = sorted(list(set(str(c.get("qpu_id")) for c in commands)))

            # ── 3b. Find network routes between all cross-QPU pairs ───────────
            qpu_pairs = self.logic.extract_qpu_pairs(commands)
            routes = {}
            _path_objects = {}
            if qpu_pairs and hasattr(self.ctx, "router") and self.ctx.router:
                for src, dst in qpu_pairs:
                    route_key = f"{src}->{dst}"
                    try:
                        p = await self.ctx.router.find_path(src, dst)
                        routes[route_key] = p.to_node_ids()
                        _path_objects[route_key] = p
                        logger.info(f"Route found {route_key}: {routes[route_key]}")
                    except Exception as e:
                        logger.warning(f"Could not find route {route_key}: {e}")
                        routes[route_key] = None
                        _path_objects[route_key] = None
            else:
                logger.debug("No router plugin available or no cross-QPU pairs; skipping route discovery")

            # ── 3c. Extract BSM nodes and build synthetic BSM commands ─────────
            node_types = {}
            bsm_commands = []
            _seen_bsm_ids = set()

            for route_key, path_obj in _path_objects.items():
                bsm_ids = self.logic.extract_bsm_nodes_from_path(path_obj)
                if not bsm_ids:
                    continue

                src_qpu, dst_qpu = route_key.split("->") if "->" in route_key else (None, None)
                pair_cmds = [
                    c
                    for c in commands
                    if len(c.get("qpus_involved") or []) >= 2
                    and src_qpu in [str(q) for q in c.get("qpus_involved", [])]
                    and dst_qpu in [str(q) for q in c.get("qpus_involved", [])]
                ]

                for bsm_id in bsm_ids:
                    node_types[bsm_id] = "BSMNode"
                    if bsm_id in _seen_bsm_ids:
                        continue
                    _seen_bsm_ids.add(bsm_id)
                    for cmd in pair_cmds:
                        bsm_cmd = dict(cmd)
                        bsm_cmd["qpu_id"] = bsm_id
                        bsm_cmd["node_type"] = "BSMNode"
                        bsm_commands.append(bsm_cmd)
                    logger.info(f"Injected {len(pair_cmds)} BSM command(s) for " f"node {bsm_id} on route {route_key}")

            if bsm_commands:
                commands = commands + bsm_commands

            all_agent_ids = sorted(list(set(str(c.get("qpu_id")) for c in commands)))
            agent_ids = all_agent_ids

            # ── Resolve rid ───────────────────────────────────────────────────
            rid = getattr(dqc_req.payload, "rid", None) or request.get("id")
            if not rid or rid == "unknown":
                from quantnet_controller.common.utils import generate_uuid

                rid = generate_uuid()

            # ── 4. Build dynamic experiment structure ─────────────────────────
            exp_name = f"DQC_{rid}"
            DynamicExp = self.logic.build_dynamic_experiment(exp_name, commands, node_types=node_types)
            if not DynamicExp:
                raise Exception("No commands to process")

            # ── 5. Register dynamic experiment with translator ─────────────────
            self.request_manager.translator.exp_defs.append(DynamicExp)

            try:
                start_time, slots = await self.request_manager.translator.get_slots_to_allocate(agent_ids, DynamicExp)

                allocations = DynamicExp.get_allocations(slots, Constants.SLOTSIZE.total_seconds())

                parameters = RequestParameter(exp_name=exp_name, path=agent_ids)
                req_obj = self.request_manager.new_request(payload=dqc_req, parameters=parameters, rid=rid)

                await self.request_manager.schedule(req_obj, blocking=True)

                # ── 6. Build simulation payload for caller ────────────────────
                sim_payload = self._build_sim_payload(
                    labeled, process_maps, raw_topology, frontend_meta,
                    pre_scheduled=(pre_ent_stats is not None),
                )

                return dqc.dqcResponse(
                    status=responseStatus(code=Code.OK.value, value=Code.OK.name, message="DQC execution completed"),
                    rid=rid,
                    data={
                        "startTime": start_time,
                        "allocations": allocations,
                        "routes": routes,
                        "simulation_payload": sim_payload,
                    },
                )
            finally:
                if DynamicExp in self.request_manager.translator.exp_defs:
                    self.request_manager.translator.exp_defs.remove(DynamicExp)

        except Exception as e:
            logger.error(f"DQC processing failed: {e}")
            return dqc.dqcResponse(
                status=responseStatus(code=Code.FAILED.value, value=Code.FAILED.name, message=f"{e}"),
                rid=dqc_req.payload.rid if hasattr(dqc_req.payload, "rid") else "unknown",
            )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_partitioned_commands(self, payload):
        """Return per-QPU command dicts and the circuit mode.

        If ``partitioned_commands`` is in the payload it is used directly.
        Otherwise the plugin parses the circuit via the appropriate qnpack
        frontend using ``circuit_file`` or ``circuit_content``.

        :returns: ``(partitioned, mode)`` where *partitioned* is
            ``{int_qpu_id: [cmd_dict, ...]}`` and *mode* is ``"cisco"``
            or ``"tket"``.
        :raises ValueError: If neither source is provided.
        """
        mode = payload.get("circuit_mode")
        if not mode:
            raise ValueError("payload.circuit_mode is required")

        if "partitioned_commands" in payload and payload["partitioned_commands"]:
            # JSON keys are strings; convert to int (qnpack convention).
            # Command lists may come through as schema proxy objects (Munch
            # etc.) — recursively convert everything to plain Python dicts/lists
            # so that validate_commands and the labeler can call .lower(), etc.
            raw = payload["partitioned_commands"]
            partitioned = {int(k): [_to_plain(cmd) for cmd in cmds] for k, cmds in raw.items()}
            logger.info(f"Using pre-partitioned commands for {len(partitioned)} QPU(s)")
            return partitioned, mode, {}

        # No partitioned commands — parse from circuit source
        qpu_info, _, _, _, _ = get_qpu_info_from_topology(self.ctx)
        circuit_file, tmp_file = self._resolve_circuit_source(payload, mode)
        try:
            source_field = "qasm_file" if mode == "cisco" else "dist_commands_file"
            circuit_cfg = Munch({"mode": mode, source_field: circuit_file})
            frontend, _ = load_frontend(circuit_cfg)
            partitioned = frontend.parse(qpu_info)
            frontend_meta = {
                "num_output_bits": getattr(frontend, "_num_output_bits", None)
                or getattr(frontend, "num_output_bits", None),
                "output_reg_name": getattr(frontend, "_output_reg_name", None)
                or getattr(frontend, "output_reg_name", "m"),
            }
            logger.info(
                f"Parsed circuit via {mode} frontend: "
                f"{sum(len(v) for v in partitioned.values())} command(s) "
                f"across {len(partitioned)} QPU(s), "
                f"output_bits={frontend_meta['num_output_bits']}, "
                f"reg={frontend_meta['output_reg_name']}"
            )
            return partitioned, mode, frontend_meta
        finally:
            if tmp_file and os.path.exists(tmp_file):
                os.unlink(tmp_file)

    def _resolve_circuit_source(self, payload, mode):
        """Return ``(circuit_file_path, tmp_file_path_or_None)``.

        Writes ``circuit_content`` to a temp file if needed.
        """
        circuit_file = payload.get("circuit_file")
        tmp_file = None

        if not circuit_file:
            content = payload.get("circuit_content")
            if not content:
                raise ValueError(
                    "Either partitioned_commands, circuit_file, or "
                    "circuit_content must be provided in the DQC request payload."
                )
            suffix = ".qasm" if mode == "cisco" else ".txt"
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, prefix="dqc_circuit_")
            tmp.write(content)
            tmp.close()
            circuit_file = tmp.name
            tmp_file = tmp.name
            logger.debug(f"Wrote circuit_content to temp file {tmp_file}")

        return circuit_file, tmp_file

    def _label_commands(self, partitioned):
        """Validate and label partitioned per-QPU commands via qnpack.

        :param partitioned: ``{int_qpu_id: [cmd_dict, ...]}``
        :returns: ``(labeled_commands, process_maps)``
        :raises ValueError: On hard validation errors.
        """
        errors = validate_commands(partitioned)
        warnings = [e for e in errors if e.severity != "error"]
        hard = [e for e in errors if e.severity == "error"]
        for w in warnings:
            logger.warning(f"Circuit validation warning: {w}")
        if hard:
            for e in hard:
                logger.error(f"Circuit validation error: {e}")
            raise ValueError(f"Circuit validation failed with {len(hard)} error(s). " "See plugin logs for details.")

        labeled, process_maps = label_and_build_maps(partitioned)
        logger.info(
            f"Labeled {sum(len(v) for v in labeled.values())} command(s); "
            f"start_qpus={len(process_maps['start_qpus'])}, "
            f"end_qpus={len(process_maps['end_qpus'])}, "
            f"ent_labels={len(process_maps['entanglement_gen_labels'])}"
        )
        return labeled, process_maps

    def _pre_schedule(self, labeled):
        """Apply pre-entanglement scheduling to labeled commands.

        Moves ``entanglement_gen`` commands earlier in each QPU's command list
        to overlap Bell-pair generation latency with local gate execution.
        The commands dict is mutated in place.

        :param labeled: ``{int_qpu_id: [cmd_dict, ...]}`` — mutated in place.
        :returns: Stats dict from ``insert_pre_entanglement_commands()``,
            or ``None`` if pre-scheduling is disabled.
        :rtype: dict or None
        """
        if not self.PRE_SCHEDULE_ENTANGLEMENT:
            return None

        stats = insert_pre_entanglement_commands(
            labeled,
            expected_ent_latency_ns=self.EXPECTED_ENT_LATENCY_NS,
            one_q_gate_duration_ns=self.ONE_Q_GATE_DURATION_NS,
            two_q_gate_duration_ns=self.TWO_Q_GATE_DURATION_NS,
        )

        total = stats["total_pairs"]
        moved = stats["moved_pairs"]
        latency = stats["expected_latency_ns"]
        logger.info(
            f"[DQC] Pre-entanglement scheduling: {moved}/{total} pairs moved "
            f"(expected latency={latency:,.0f} ns)"
        )
        if stats["moves"]:
            total_gate_time = sum(
                min(m["gate_time_ns"][0], m["gate_time_ns"][1])
                for m in stats["moves"]
            )
            avg_overlap = total_gate_time / len(stats["moves"])
            coverage = (avg_overlap / latency * 100) if latency > 0 else 0
            logger.info(
                f"[DQC] Avg gate-time overlap: {avg_overlap:,.0f} ns "
                f"({coverage:.1f}% of expected latency)"
            )

        return stats

    def _build_sim_payload(self, labeled, process_maps, topology, frontend_meta=None, pre_scheduled=False):
        """Build the JSON-serializable payload for qnpack simulation.

        Returns a dict the caller can write to a file and pass to
        ``dqc-sim-labeled <file>``.  Sets are converted to lists and
        integer QPU-ID keys are stringified for JSON compatibility.

        :param labeled: ``{int_qpu_id: [cmd_dict, ...]}``
        :param process_maps: ``{start_qpus, end_qpus, entanglement_gen_labels}``
        :param topology: Raw topology list from ``get_qpu_info_from_topology``
        :param frontend_meta: Optional dict with ``num_output_bits`` and
            ``output_reg_name`` from the frontend, so ``run_from_labeled``
            knows which register columns to read back as the bitstring.
        :returns: JSON-serializable dict ready for ``dqc-sim-labeled``.
        """
        payload = {
            "labeled_commands": {str(k): v for k, v in labeled.items()},
            "process_maps": {
                "start_qpus": {str(k): [int(x) for x in v] for k, v in process_maps["start_qpus"].items()},
                "end_qpus": {str(k): [int(x) for x in v] for k, v in process_maps["end_qpus"].items()},
                "entanglement_gen_labels": list(process_maps["entanglement_gen_labels"]),
            },
            "topology": topology,
        }
        if frontend_meta:
            if frontend_meta.get("num_output_bits") is not None:
                payload["num_output_bits"] = frontend_meta["num_output_bits"]
            if frontend_meta.get("output_reg_name"):
                payload["output_reg_name"] = frontend_meta["output_reg_name"]
        if pre_scheduled:
            payload["pre_scheduled"] = True
        return payload
