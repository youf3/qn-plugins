import logging
from quantnet_agent.hal.interpreter.experiment_interpreter import ExperimentInterpreter
from quantnet_mq.schema.models import experiment, calibration
from bsm.bsm.interpreter.BSM import BSM

log = logging.getLogger(__name__)


class BSMInterpreter(ExperimentInterpreter):

    def __init__(self, hal):
        super().__init__(hal, experiment, "experiment")
        self.parameters = {}  # For Bob role

    async def run_experiment(self, exp_request):
        exp_name = exp_request.expName._value

        if exp_name == "BSM":
            # Pass self._publish as callback to BSM
            self.BSM = BSM(self.hal, self.hal._msgclient, cb=self._publish)
            await self.BSM.init_device()
            await self.BSM.bsm()
        else:
            raise ValueError(f"Unknown experiment name: {exp_name}")

    # Bob role methods (from BSM_b_interpreter)
    async def submit(self, *exp_info, **exp_param):
        log.info(f"Received BSM submit request : {exp_info} {exp_param}")

        # 1. Always store parameters (Common logic)
        for i in exp_info[0].parameters.data:
            for k, v in i.items():
                self.parameters[k] = v

        # 2. Check role: Explicitly check 'bob' role.
        role = self.hal.config.role if self.hal.config.role else self.hal.config.cid
        is_bob = role.lower() == "bob"

        if not is_bob:
            # Alice/Charlie (Executor) role: delegate to parent to run experiment
            log.info(f"Agent acting as {role} (Executor) - running BSM experiment")
            await super().submit(*exp_info, **exp_param)
        else:
            # Bob (Coordinator) role: just stored params, do nothing else
            log.info(f"Agent acting as {role} (Coordinator) - stored parameters, skipping execution")

    async def update_result(self, exp_id):
        log.info(f"Getting BSM result for {exp_id}")
        return {"result": ""}

    def cancel(self, request):
        log.info(f"Received BSM experiment cancel request : {request}")
        pass

    def get_schedulable_commands(self):
        commands = {
            "experiment.submit": [
                self.submit,
                "quantnet_mq.schema.models.experiment.submit",
                calibration.submitResponse,
                None,
            ],
            "experiment.getResult": [
                self.update_result,
                "quantnet_mq.schema.models.experiment.getResult",
                calibration.getResultResponse,
                None,
            ],
            "experiment.cancel": [
                self.cancel,
                "quantnet_mq.schema.models.experiment.cancel",
                calibration.cancelResponse,
                None,
            ],
        }
        return commands
