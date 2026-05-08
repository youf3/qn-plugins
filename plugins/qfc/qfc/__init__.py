import logging
from quantnet_controller.common.plugin import ProtocolPlugin, PluginType, Path
from quantnet_controller.common.request import RequestManager, RequestType
from quantnet_mq import Code
from quantnet_mq.schema.models import qfc, Status as responseStatus, QNode
from quantnet_controller.core import AbstractDatabase as DB

logger = logging.getLogger(__name__)


class QFC(ProtocolPlugin):
    def __init__(self, context):
        super().__init__("qfc", PluginType.PROTOCOL, context)
        self._client_commands = []
        self._server_commands = [
            ("qfcRequest", self.handle_qfc_request, "quantnet_mq.schema.models.qfc.qfcRequest"),
            ("qfcQuery", self.handle_qfc_query, "quantnet_mq.schema.models.qfc.qfcQuery"),
        ]
        self._msg_commands = list()
        self.ctx = context
        self._db = DB().handler("qfc")

        self.request_manager = RequestManager(
            context, plugin_schema=qfc.qfcRequest, request_type=RequestType.EXPERIMENT
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
            if n.systemSettings.type not in [QNode.__title__, QNode.__title__]:
                raise Exception(f"Node {n.systemSettings.ID} is not a QNode")
        return nodes

    async def handle_qfc_request(self, request):
        payload = qfc.qfcRequest(**request)
        logger.info(f"Received qfc request: {payload.serialize()}")

        rc = Code.OK
        try:
            nodes = self.validate_request(payload)
        except Exception as e:
            logger.error(f"Invalid argument in request: {e}")
            rc = Code.INVALID_ARGUMENT
            return qfc.qfcResponse(status=responseStatus(code=rc.value, value=Code(rc).name, message=f"{e}"))

        try:
            p = Path(nodes)
            path = [str(x.systemSettings.ID) for x in p.hops]
            logger.info(f"Found valid resources: {path}")
        except Exception as e:
            logger.error(f"Could not find valid resources: {e}")
            rc = Code.INVALID_ARGUMENT
            return qfc.qfcResponse(status=responseStatus(code=rc.value, value=Code(rc).name, message=f"{e}"))

        parameters = {
            "exp_name": "QFC",
            "path": p,
            # Any additional experiment execution parameters would go here
        }

        # Create Request object through RequestManager
        # Payload encapsulates the plugin request (nodes, rate, duration are already in payload)
        req = self.request_manager.new_request(payload=payload, parameters=parameters)

        # Schedule the request
        rc = await self.request_manager.schedule(req, blocking=True)

        return qfc.qfcResponse(
            status=responseStatus(code=rc.value, value=Code(rc).name, message=f"{path}"),
            rtype=str(payload.cmd),
            rid=req.id,
        )

    async def handle_qfc_query(self, request):
        payload = qfc.qfcQuery(**request)
        logger.info(f"Received qfc query: {payload.serialize()}")

        rc = Code.OK

        try:
            rid = str(payload.payload.rid)
            # Get request with experiment result
            req = await self.request_manager.get_request(rid, include_result=True)

            return qfc.qfcResponse(
                status=responseStatus(
                    code=req.status_code.value, value=req.status_code.name, message=req.status_message
                ),
                rid=rid,
                data=getattr(req, "experiment_data", None),
            )

        except Exception as e:
            logger.error(f"Failed to get experiment status: {e}")
            return qfc.qfcResponse(
                status=responseStatus(
                    code=Code.INVALID_ARGUMENT.value, value=Code.INVALID_ARGUMENT.name, message=f"{e}"
                ),
                rid=rid if "rid" in locals() else None,
            )
