import logging
from quantnet_controller.common.plugin import ProtocolPlugin, PluginType, Path
from quantnet_controller.common.request import (
    RequestManager,
    RequestType,
    RequestParameter,
)
from quantnet_mq import Code
from quantnet_mq.schema.models import (
    linkCalibration,
    Status as responseStatus,
    QNode,
)

logger = logging.getLogger(__name__)


class LinkCalibration(ProtocolPlugin):
    def __init__(self, context):
        super().__init__("linkCal", PluginType.PROTOCOL, context)
        self._client_commands = [
            (
                "calibration.submit",
                None,
                "quantnet_mq.schema.models.calibration.submit",
            ),
            (
                "calibration.getResult",
                None,
                "quantnet_mq.schema.models.calibration.getResult",
            ),
            (
                "calibration.cancel",
                None,
                "quantnet_mq.schema.models.calibration.cancel",
            ),
        ]
        self._server_commands = [
            (
                "linkCalRequest",
                self.handle_linkCal_request,
                "quantnet_mq.schema.models.linkCalibration.linkCalRequest",
            ),
            (
                "linkCalQuery",
                self.handle_linkCal_query,
                "quantnet_mq.schema.models.linkCalibration.linkCalQuery",
            ),
        ]
        self._msg_commands = list()
        self.ctx = context

        # Initialize RequestManager with calibration type
        self.request_manager = RequestManager(
            context,
            plugin_schema=linkCalibration.linkCalRequest,
            request_type=RequestType.CALIBRATION,
        )

    def initialize(self):
        pass

    def destroy(self):
        pass

    def reset(self):
        pass

    def validate_request(self, req):
        """Validate that all requested nodes are QNodes."""
        nodes = self.ctx.rm.get_nodes(*req.payload.nodes)
        for n in nodes:
            if n.systemSettings.type != QNode.__title__:
                raise Exception(f"Node {n.systemSettings.ID} is not a QNode")
        return nodes

    async def handle_linkCal_request(self, request):
        """Handle incoming Link Calibration request."""
        # Create plugin-specific payload object
        payload = linkCalibration.linkCalRequest(**request)
        logger.info(f"Received linkCal request: {payload.serialize()}")

        # Validate request
        try:
            nodes = self.validate_request(payload)
        except Exception as e:
            logger.error(f"Invalid argument in request: {e}")
            return linkCalibration.linkCalResponse(
                status=responseStatus(
                    code=Code.INVALID_ARGUMENT.value, value=Code.INVALID_ARGUMENT.name, message=f"{e}"
                )
            )

        # Find valid resources
        try:
            p = Path(nodes)
            path = [str(x.systemSettings.ID) for x in p.hops]
            logger.info(f"Found valid resources: {path}")
        except Exception as e:
            logger.error(f"Could not find valid resources: {e}")
            return linkCalibration.linkCalResponse(
                status=responseStatus(
                    code=Code.INVALID_ARGUMENT.value, value=Code.INVALID_ARGUMENT.name, message=f"{e}"
                )
            )

        # Create calibration execution parameters
        parameters = RequestParameter(
            exp_name="Link Calibration", path=p.to_node_ids()
        )

        # Create Request object through RequestManager
        # Payload encapsulates the plugin request (nodes)
        req = self.request_manager.new_request(
            payload=payload, parameters=parameters
        )

        # Schedule the request for execution
        fut = self.request_manager.noSchedule(req, blocking=True)
        rc = await fut

        return linkCalibration.linkCalResponse(
            status=responseStatus(
                code=rc.value,
                value=rc.name,
                message=f"Path: {path}",
            ),
            rtype=str(payload.cmd),
            rid=req.id,
        )

    async def handle_linkCal_query(self, request):
        """Handle Link Calibration query for request status/results."""
        payload = linkCalibration.linkCalQuery(**request)
        logger.info(f"Received Link Calibration query: {payload.serialize()}")

        try:
            rid = str(payload.payload.rid)

            # Get Request object from RequestManager
            req = await self.request_manager.get_request(
                rid, include_result=True
            )
            if not req:
                raise Exception("Request ID not found")

            return linkCalibration.linkCalResponse(
                status=req.status,
                rid=rid,
                data=getattr(req, "experiment_data", None),
            )

        except Exception as e:
            logger.error(f"Failed to get calibration status: {e}")
            return linkCalibration.linkCalResponse(
                status=responseStatus(
                    code=Code.INVALID_ARGUMENT.value,
                    value=Code.INVALID_ARGUMENT.name,
                    message=f"{e}",
                ),
                rid=rid if "rid" in locals() else None,
            )
