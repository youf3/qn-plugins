import logging
from quantnet_controller.common.plugin import ProtocolPlugin, PluginType, Path
from quantnet_controller.common.request import (
    RequestManager,
    RequestType,
    RequestParameter,
)
from quantnet_mq import Code
from quantnet_mq.schema.models import (
    bsm,
    Status as responseStatus,
    QNode,
    BSMNode,
)

logger = logging.getLogger(__name__)


class BSM(ProtocolPlugin):
    def __init__(self, context):
        super().__init__("bsm", PluginType.PROTOCOL, context)
        self._client_commands = [
            (
                "experiment.submit",
                None,
                "quantnet_mq.schema.models.experiment.submit",
            ),
            (
                "experiment.getResult",
                None,
                "quantnet_mq.schema.models.experiment.getResult",
            ),
            (
                "experiment.cancel",
                None,
                "quantnet_mq.schema.models.experiment.cancel",
            ),
        ]
        self._server_commands = [
            (
                "bsmRequest",
                self.handle_bsm_request,
                "quantnet_mq.schema.models.bsm.bsmRequest",
            ),
            (
                "bsmQuery",
                self.handle_bsm_query,
                "quantnet_mq.schema.models.bsm.bsmQuery",
            ),
        ]
        self._msg_commands = list()
        self.ctx = context

        self.request_manager = RequestManager(
            context,
            plugin_schema=bsm.bsmRequest,
            request_type=RequestType.EXPERIMENT,
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
            if n.systemSettings.type not in [
                QNode.__title__,
                BSMNode.__title__,
            ]:
                raise Exception(f"Node {n.systemSettings.ID} is not a BSMNode")
        return nodes

    async def handle_bsm_request(self, request):
        payload = bsm.bsmRequest(**request)
        logger.info(f"Received BSM request: {payload.serialize()}")

        rc = Code.OK
        try:
            nodes = self.validate_request(payload)
        except Exception as e:
            logger.error(f"Invalid argument in request: {e}")
            return bsm.bsmResponse(
                status=responseStatus(
                    code=rc.value,
                    value=Code(rc).name,
                    message=f"{e}",
                )
            )

        try:
            p = Path(nodes)
            path = [str(x.systemSettings.ID) for x in p.hops]
            logger.info(f"Found valid resources: {path}")
        except Exception as e:
            logger.error(f"Could not find valid resources: {e}")
            rc = Code.INVALID_ARGUMENT
            return bsm.bsmResponse(
                status=responseStatus(
                    code=rc.value,
                    value=Code(rc).name,
                    message=f"{e}",
                )
            )

        parameters = RequestParameter(exp_name="BSM", path=p.to_node_ids())

        # Create Request object through RequestManager
        # Payload encapsulates the plugin request
        req = self.request_manager.new_request(
            payload=payload, parameters=parameters
        )

        # Schedule the request
        fut = self.request_manager.noSchedule(req, blocking=True)
        rc = await fut

        return bsm.bsmResponse(
            status=responseStatus(
                code=rc.value, value=rc.name, message=f"{path}"
            ),
            rtype=str(payload.cmd),
            rid=req.id,
        )

    async def handle_bsm_query(self, request):
        payload = bsm.bsmQuery(**request)
        logger.info(f"Received BSM query: {payload.serialize()}")

        rc = Code.OK

        try:
            rid = str(payload.payload.rid)
            # Get request with experiment result
            req = await self.request_manager.get_request(
                rid, include_result=True
            )

            if req is None:
                raise Exception("Request ID not found")

            return bsm.bsmResponse(
                status=req.status,
                rid=rid,
                data=getattr(req, "experiment_data", None),
            )

        except Exception as e:
            logger.error(f"Failed to get experiment status: {e}")
            return bsm.bsmResponse(
                status=responseStatus(
                    code=Code.INVALID_ARGUMENT.value,
                    value=Code.INVALID_ARGUMENT.name,
                    message=f"{e}"
                ),
                rid=rid if "rid" in locals() else None,
            )
