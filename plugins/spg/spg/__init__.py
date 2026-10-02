import logging
from quantnet_controller.common.plugin import ProtocolPlugin, PluginType, Path
from quantnet_controller.common.request import RequestManager, RequestType, RequestParameter
from quantnet_mq import Code
from quantnet_mq.schema.models import spg, Status as responseStatus, QNode

logger = logging.getLogger(__name__)


class SPG(ProtocolPlugin):
    def __init__(self, context):
        super().__init__("spg", PluginType.PROTOCOL, context)
        self._client_commands = []
        self._server_commands = [
            ("spgRequest", self.handle_spg_request, "quantnet_mq.schema.models.spg.spgRequest"),
            ("spgQuery", self.handle_spg_query, "quantnet_mq.schema.models.spg.spgQuery"),
        ]
        self._msg_commands = list()
        self.ctx = context

        self.request_manager = RequestManager(
            context, plugin_schema=spg.spgRequest, request_type=RequestType.EXPERIMENT
        )

    def initialize(self):
        pass

    def destroy(self):
        pass

    def reset(self):
        pass

    def validate_request(self, req):
        nodes = self.ctx.rm.get_nodes(*req.payload.nodes)
        for n in nodes:
            if n.systemSettings.type != QNode.__title__:
                raise Exception(f"Node {n.systemSettings.ID} is not a QNode")
        return nodes

    async def handle_spg_request(self, request):
        # Create plugin-specific payload object
        payload = spg.spgRequest(**request)
        logger.info(f"Received SPG request: {payload.serialize()}")

        rc = Code.OK
        try:
            nodes = self.validate_request(payload)
        except Exception as e:
            logger.error(f"Invalid argument in request: {e}")
            rc = Code.INVALID_ARGUMENT
            return spg.spgResponse(status=responseStatus(code=rc.value, value=Code(rc).name, message=f"{e}"))

        try:
            p = Path(nodes).to_node_ids()
            logger.info(f"Found valid resources: {p}")
        except Exception as e:
            logger.error(f"Could not find valid resources: {e}")
            rc = Code.INVALID_ARGUMENT
            return spg.spgResponse(status=responseStatus(code=rc.value, value=Code(rc).name, message=f"{e}"))

        # Parameters are experiment-specific execution parameters (what was in params)
        parameters = RequestParameter(exp_name="Single Photon Generation", path=p)
        # Any additional experiment execution parameters would go here

        # Create Request object through RequestManager
        # Payload encapsulates the plugin request (nodes, rate, duration are already in payload)
        owner = getattr(payload.payload, 'owner', None)
        req = self.request_manager.new_request(payload=payload, parameters=parameters, owner=owner)

        # Schedule the request
        rc = await self.request_manager.schedule(req, blocking=True)

        return spg.spgResponse(
            status=responseStatus(code=rc.value, value=Code(rc).name, message=f"{p}"),
            rtype=str(payload.cmd),
            rid=req.id,
        )

    async def handle_spg_query(self, request):
        """Handle SPG query for request status/results."""
        payload = spg.spgQuery(**request)
        logger.info(f"Received SPG query: {payload.serialize()}")

        try:
            rid = str(payload.payload.rid)

            # Get request with experiment result
            req = await self.request_manager.get_request(rid, include_result=True)

            if req is None:
                raise Exception("Request ID not found")

            return spg.spgResponse(
                status=req.status,
                rid=rid,
                data=getattr(req, "experiment_data", None),
            )

        except Exception as e:
            logger.error(f"Failed to get experiment status: {e}")
            return spg.spgResponse(
                status=responseStatus(
                    code=Code.INVALID_ARGUMENT.value, value=Code.INVALID_ARGUMENT.name, message=f"{e}"
                ),
                rid=rid if "rid" in locals() else None,
            )
