"""
Controller-side orchestration of continuous entanglement generation.

The ``EntanglementManager`` queries agent capabilities, enables/disables
continuous generation for QPU pairs needed by a DQC circuit, and waits
for pair readiness before the circuit is submitted.

It communicates with agents on the ``rpc/entanglement/<agent_id>`` MQTT
topic, which is separate from the main ``rpc/<agent_id>`` channel used
by the scheduler and experiment framework.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class EntanglementManager:
    """Manages continuous entanglement generation for a DQC circuit.

    Queries agent capabilities, enables generation for needed QPU pairs,
    waits for readiness, and signals whether continuous or on-demand
    entanglement should be used.

    Parameters
    ----------
    ctx : ControllerContextManager
        Controller context providing access to the RPC client and
        resource manager.
    """

    DEFAULT_READINESS_TIMEOUT_S = 30.0
    DEFAULT_POLL_INTERVAL_S = 0.5

    def __init__(self, ctx):
        self.ctx = ctx

    # -- internal helpers -----------------------------------------------------

    def _entanglement_topic(self, agent_id: str) -> str:
        return f"rpc/entanglement/{agent_id}"

    async def _rpc_call(self, agent_id: str, cmd: str, payload: dict,
                        timeout: float = 10.0):
        """Send an RPC call on the entanglement topic.

        Returns the parsed response dict, or ``None`` on error/timeout.
        """
        topic = self._entanglement_topic(agent_id)
        try:
            resp = await self.ctx.rpc_client.call(
                cmd, payload, topic=topic, timeout=timeout,
            )
            if isinstance(resp, str):
                import json
                return json.loads(resp)
            return resp
        except Exception as e:
            logger.warning(
                "[EntanglementManager] RPC %s to %s failed: %s",
                cmd, agent_id, e,
            )
            return None

    # -- public API -----------------------------------------------------------

    async def query_capabilities(self, agent_ids: list[str]) -> dict:
        """Query entanglement capabilities of all agents.

        Returns ``{agent_id: capabilities_dict}``.  Agents that time out
        or return an error get ``{"continuous_generation": False}``.
        """
        results = {}
        tasks = {
            aid: self._rpc_call(aid, "entanglement.capabilities", {})
            for aid in agent_ids
        }
        responses = await asyncio.gather(
            *tasks.values(), return_exceptions=True,
        )
        for aid, resp in zip(tasks.keys(), responses):
            if isinstance(resp, Exception) or resp is None:
                results[aid] = {"continuous_generation": False}
            else:
                results[aid] = resp
        logger.info(
            "[EntanglementManager] Capabilities: %s",
            {k: v.get("continuous_generation", False) for k, v in results.items()},
        )
        return results

    async def enable_for_circuit(
        self,
        qpu_pairs: list[tuple[str, str]],
        config: dict | None = None,
    ) -> bool:
        """Enable continuous generation for all QPU pairs in the circuit.

        Sends ``entanglement.enable`` to each QPU in the pair set.  If
        any agent reports an error, disables all and returns ``False``.

        Returns ``True`` if all pairs were enabled successfully.
        """
        if config is None:
            config = {}

        # Collect unique QPU IDs and their peers
        enable_tasks: dict[str, list[str]] = {}  # {qpu_id: [peer_ids]}
        for src, dst in qpu_pairs:
            enable_tasks.setdefault(src, []).append(dst)
            enable_tasks.setdefault(dst, []).append(src)

        all_ok = True
        for qpu_id, peers in enable_tasks.items():
            for peer_id in peers:
                resp = await self._rpc_call(
                    qpu_id,
                    "entanglement.enable",
                    {"peer_id": peer_id, "config": config},
                )
                if resp is None or resp.get("status") != "ok":
                    logger.error(
                        "[EntanglementManager] enable failed for %s -> %s: %s",
                        qpu_id, peer_id, resp,
                    )
                    all_ok = False
                    break
            if not all_ok:
                break

        if not all_ok:
            all_qpus = set()
            for src, dst in qpu_pairs:
                all_qpus.add(src)
                all_qpus.add(dst)
            await self.disable_all(list(all_qpus))

        return all_ok

    async def wait_for_readiness(
        self,
        qpu_pairs: list[tuple[str, str]],
        timeout_s: float = DEFAULT_READINESS_TIMEOUT_S,
    ) -> bool:
        """Poll until all QPU pairs report at least one available pair.

        Returns ``True`` if all pairs become available within
        *timeout_s*, ``False`` otherwise.
        """
        deadline = asyncio.get_event_loop().time() + timeout_s

        # Build the set of (qpu_id, peer_id) checks needed
        checks = set()
        for src, dst in qpu_pairs:
            checks.add((src, dst))
            checks.add((dst, src))

        while asyncio.get_event_loop().time() < deadline:
            all_ready = True
            for qpu_id, peer_id in checks:
                resp = await self._rpc_call(
                    qpu_id,
                    "entanglement.status",
                    {"peer_id": peer_id},
                )
                if resp is None or not resp.get("available", False):
                    all_ready = False
                    break

            if all_ready:
                logger.info(
                    "[EntanglementManager] All %d QPU pairs ready",
                    len(qpu_pairs),
                )
                return True

            await asyncio.sleep(self.DEFAULT_POLL_INTERVAL_S)

        logger.warning(
            "[EntanglementManager] Readiness timeout after %.1fs", timeout_s,
        )
        return False

    async def disable_all(self, agent_ids: list[str]) -> None:
        """Send ``entanglement.disable`` to all agents.

        Fire-and-forget: errors are logged but not raised.  Intended for
        use in a ``finally`` block after circuit completion.
        """
        for aid in agent_ids:
            try:
                await self._rpc_call(
                    aid,
                    "entanglement.disable",
                    {"peer_id": "__all__"},
                    timeout=5.0,
                )
            except Exception as e:
                logger.warning(
                    "[EntanglementManager] disable_all failed for %s: %s",
                    aid, e,
                )
