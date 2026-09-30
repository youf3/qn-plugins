"""
Agent-side RPC handler for continuous entanglement generation.

Delegates all operations to the configured ``EntanglementSource`` HAL
device.  If no device is configured, ``capabilities()`` reports
``continuous_generation=False`` and all other commands return an error
response.

Register on the agent via ``agent.cfg``::

    [protocols]
    entanglement=entanglement.py

And configure the HAL device::

    [[entanglement_source]]
    enabled=true
    type=EntanglementSource
    driver=DummyEntanglementSource
"""

import logging

from quantnet_agent.hal.HAL import CMDInterpreter

log = logging.getLogger(__name__)

# Response helpers
_NO_SOURCE = {"status": "error", "reason": "no entanglement source configured"}
_NO_CAPS = {
    "continuous_generation": False,
    "max_peers": 0,
    "max_pool_size": 0,
    "supports_fidelity_tracking": False,
    "supports_prefill": False,
    "prefill_slots": 0,
    "supports_background_refill": False,
}


class EntanglementInterpreter(CMDInterpreter):
    """Expose the ``EntanglementSource`` HAL device as RPC commands.

    Commands are registered on the agent's RPC server under the
    ``entanglement.*`` namespace.  The interpreter looks for a device
    named ``entanglement_source`` in the HAL; if absent, all commands
    degrade gracefully.
    """

    def __init__(self, hal):
        super().__init__(hal)
        self._source = hal.devs.get("entanglement_source")
        if self._source is None:
            log.warning(
                "EntanglementInterpreter: no 'entanglement_source' device "
                "configured — continuous entanglement will be unavailable"
            )

    def get_commands(self):
        return {
            "entanglement.capabilities": [
                self.handle_capabilities,
                "entanglement.capabilitiesRequest",
            ],
            "entanglement.enable": [
                self.handle_enable,
                "entanglement.enableRequest",
            ],
            "entanglement.status": [
                self.handle_status,
                "entanglement.statusRequest",
            ],
            "entanglement.consume": [
                self.handle_consume,
                "entanglement.consumeRequest",
            ],
            "entanglement.disable": [
                self.handle_disable,
                "entanglement.disableRequest",
            ],
        }

    async def handle_capabilities(self, request):
        """Report what the configured entanglement source supports."""
        log.debug("entanglement.capabilities request received")
        if self._source is None:
            return _NO_CAPS
        try:
            return await self._source.capabilities()
        except Exception as e:
            log.error("entanglement.capabilities failed: %s", e)
            return _NO_CAPS

    async def handle_enable(self, request):
        """Enable continuous entanglement generation with a peer."""
        if self._source is None:
            return _NO_SOURCE
        payload = request.payload if hasattr(request, "payload") else request
        peer_id = payload.get("peer_id") if isinstance(payload, dict) else getattr(payload, "peer_id", None)
        config = payload.get("config", {}) if isinstance(payload, dict) else getattr(payload, "config", {})
        if config is None:
            config = {}
        log.info("entanglement.enable: peer=%s config=%s", peer_id, config)
        try:
            return await self._source.enable(peer_id, config)
        except Exception as e:
            log.error("entanglement.enable failed: %s", e)
            return {"status": "error", "reason": str(e)}

    async def handle_status(self, request):
        """Query entanglement status with a peer."""
        if self._source is None:
            return {"available": False, "pairs": 0, "enabled": False}
        payload = request.payload if hasattr(request, "payload") else request
        peer_id = payload.get("peer_id") if isinstance(payload, dict) else getattr(payload, "peer_id", None)
        log.debug("entanglement.status: peer=%s", peer_id)
        try:
            return await self._source.status(peer_id)
        except Exception as e:
            log.error("entanglement.status failed: %s", e)
            return {"available": False, "pairs": 0, "enabled": False}

    async def handle_consume(self, request):
        """Consume one entangled pair with a peer."""
        if self._source is None:
            return {"pair": None}
        payload = request.payload if hasattr(request, "payload") else request
        peer_id = payload.get("peer_id") if isinstance(payload, dict) else getattr(payload, "peer_id", None)
        log.debug("entanglement.consume: peer=%s", peer_id)
        try:
            pair = await self._source.consume(peer_id)
            return {"pair": pair}
        except Exception as e:
            log.error("entanglement.consume failed: %s", e)
            return {"pair": None}

    async def handle_disable(self, request):
        """Disable continuous entanglement generation with a peer."""
        if self._source is None:
            return _NO_SOURCE
        payload = request.payload if hasattr(request, "payload") else request
        peer_id = payload.get("peer_id") if isinstance(payload, dict) else getattr(payload, "peer_id", None)
        log.info("entanglement.disable: peer=%s", peer_id)
        try:
            return await self._source.disable(peer_id)
        except Exception as e:
            log.error("entanglement.disable failed: %s", e)
            return {"status": "error", "reason": str(e)}
