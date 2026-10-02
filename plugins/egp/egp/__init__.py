import logging
from quantnet_controller.common.plugin import ProtocolPlugin, PluginType
from quantnet_controller.common.request import RequestManager, RequestType, RequestParameter
from quantnet_mq import Code
from quantnet_mq.schema.models import egp, Status as responseStatus, QNode

logger = logging.getLogger(__name__)


class EGP(ProtocolPlugin):
    def __init__(self, context):
        super().__init__("egp", PluginType.PROTOCOL, context)
        self._client_commands = []
        self._server_commands = [
            ("egpRequest", self.handle_egp_request, "quantnet_mq.schema.models.egp.egpRequest"),
            ("egpQuery", self.handle_egp_query, "quantnet_mq.schema.models.egp.egpQuery"),
        ]
        self._msg_commands = list()
        self.ctx = context

        # Initialize RequestManager with EGP schema
        self.request_manager = RequestManager(
            context, plugin_schema=egp.egpRequest, request_type=RequestType.EXPERIMENT
        )

    def initialize(self):
        pass

    def destroy(self):
        pass

    def reset(self):
        pass

    def validate_request(self, req):
        """Validate that source and destination are QNodes."""
        nodes = self.ctx.rm.get_nodes(req.payload.source, req.payload.destination)
        for n in nodes:
            if n.systemSettings.type != QNode.__title__:
                raise Exception(f"Node {n.systemSettings.ID} is not a QNode")
        return nodes

    async def handle_egp_request(self, request):
        """Handle incoming EGP request."""
        # Create plugin-specific payload object
        egpreq = egp.egpRequest(**request)
        logger.info(f"Received EGP request: {egpreq.serialize()}")

        # Validate request
        try:
            self.validate_request(egpreq)
        except Exception as e:
            logger.error(f"Invalid argument in request: {e}")
            return egp.egpResponse(
                status=responseStatus(
                    code=Code.INVALID_ARGUMENT.value, value=Code.INVALID_ARGUMENT.name, message=f"{e}"
                )
            )

        # Find path between source and destination
        try:
            p = await self.ctx.router.find_path(egpreq.payload.source, egpreq.payload.destination)
            path = [str(x.systemSettings.ID) for x in p.hops]
            logger.info(f"Found valid resources: {path}")
        except Exception as e:
            logger.error(f"Could not find valid resources: {e}")
            return egp.egpResponse(
                status=responseStatus(
                    code=Code.INVALID_ARGUMENT.value, value=Code.INVALID_ARGUMENT.name, message=f"{e}"
                )
            )

        # Create experiment execution parameters
        parameters = RequestParameter(exp_name="Entanglement Generation", path=p.to_node_ids())

        # Create Request object through RequestManager
        # Payload encapsulates the plugin request (source, destination, pairs, bellState, fidelity)
        owner = getattr(egpreq.payload, 'owner', None)
        request = self.request_manager.new_request(payload=egpreq, parameters=parameters, owner=owner)

        # Schedule the request for execution
        fut = self.request_manager.noSchedule(request, blocking=True)
        rc = await fut

        return egp.egpResponse(
            status=responseStatus(code=rc.value, value=rc.name, message=f"Path: {path}"),
            rtype=str(egpreq.cmd),
            rid=request.id,
        )

    async def handle_egp_query(self, request):
        """Handle EGP query for request status/results."""
        payload = egp.egpQuery(**request)
        logger.info(f"Received EGP query: {payload.serialize()}")

        try:
            rid = str(payload.payload.rid)

            # Get request with experiment result
            req = await self.request_manager.get_request(rid, include_result=True)

            if req is None:
                raise Exception("Request ID not found")

            return egp.egpResponse(
                status=req.status,
                rid=rid,
                data=getattr(req, "experiment_data", None),
            )

        except Exception as e:
            logger.error(f"Failed to get experiment status: {e}")
            return egp.egpResponse(
                status=responseStatus(
                    code=Code.INVALID_ARGUMENT.value, value=Code.INVALID_ARGUMENT.name, message=f"{e}"
                ),
                rid=rid if "rid" in locals() else None,
            )
