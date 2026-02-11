import logging
import json
from datetime import datetime, timezone
from quantnet_agent.hal.HAL import ScheduleableInterpreter
from quantnet_mq.schema.models import experiment
from qfc.qfc.interpreter.QFC import QFCWorkflow

log = logging.getLogger(__name__)


class QFCInterpreter(ScheduleableInterpreter):
    """
    QFC Interpreter supporting the full experimental workflow:
    1. Polarization stabilization
    2. HOM measurement (with stabilization light)
    3. BSM measurement (with stabilization light)
    4. QFC initialization (IM optimization + mean photon number)
    5. HOM with QFC (EOM-based, 20ns pulses)
    6. BSM with QFC
    """

    def __init__(self, hal):
        super().__init__(hal)
        self.parameters = {}
        self.set_agent_commands()
        self.msgtopic = "experiment_data"
        self.expid = ""
        self.workflow = None

    async def submit(self, *exp_info, **exp_param):
        log.info(f"Received QFC submit request : {exp_info} {exp_param}")
        exp_name = exp_info[0].expName._value
        agent_comms_topic = exp_info[0]["topic"]._value
        self.expid = exp_param["exp_id"]._value

        # Extract parameters
        for i in exp_info[0].parameters.data:
            for k, v in i.items():
                self.parameters[k] = v

        # Initialize QFC workflow
        self.workflow = QFCWorkflow(self.hal, self.hal._msgclient, agent_comms_topic, cb=self.__publish)
        # await self.workflow.run_full_workflow()

        # Determine which workflow step to execute
        if exp_name == "QFC_init":
            # Step 4: QFC initialization only
            await self.workflow.qfc_initialization()

        elif exp_name == "HOM_EOM":
            # Step 5: HOM with QFC (EOM-based)
            await self.workflow.hom_eom_scan()

        elif exp_name == "QFC_full_workflow":
            # Complete workflow: Steps 1-7
            await self.workflow.run_full_workflow()

        elif exp_name == "QFC_check_stabilization":
            # Check polarization, HOM, BSM before QFC
            await self.workflow.check_stabilization_parameters()

        else:
            raise ValueError(f"Unknown experiment name: {exp_name}")

    async def update_result(self, exp_id):
        log.info(f"Getting QFC result for {exp_id}")
        return {"result": ""}

    def set_agent_commands(self):
        commands = [("agentSubmit", None, "quantnet_mq.schema.models.agentSubmit")]
        for cmd in commands:
            self.hal._rpcclient.set_handler(*cmd)

    def get_commands(self):
        commands = {}
        return commands

    async def __publish(self, payload):
        msg = json.dumps(
            {
                "expid": self.expid,
                "ts": datetime.now(timezone.utc).timestamp(),
                "data": payload,
            }
        )
        await self.hal._msgclient.publish(self.msgtopic, msg)

    def cancel(self, request):
        log.info(f"Received QFC cancel request : {request}")
        if self.workflow:
            self.workflow.cancel()

    def get_schedulable_commands(self):
        commands = {
            "experiment.submit": [
                self.submit,
                "quantnet_mq.schema.models.experiment.submit",
                experiment.submitResponse,
                None,
            ],
            "experiment.getResult": [
                self.update_result,
                "quantnet_mq.schema.models.experiment.getResult",
                experiment.getResultResponse,
                None,
            ],
            "experiment.cancel": [
                self.cancel,
                "quantnet_mq.schema.models.experiment.cancel",
                experiment.cancelResponse,
                None,
            ],
        }
        return commands
