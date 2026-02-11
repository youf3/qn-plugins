import logging
from quantnet_agent.hal.interpreter.experiment_interpreter import ExperimentInterpreter
from quantnet_mq.schema.models import calibration
from .PSO.PSO import PSO

log = logging.getLogger(__name__)


class PSOInterpreter(ExperimentInterpreter):

    def __init__(self, hal):
        super().__init__(hal, calibration, "calibration")
        self.PSO = None
        self.parameters = {}  # For Bob role

    async def run_experiment(self, exp_request):
        log.info("Received Link Calibration submit request")
        log.debug(f"{exp_request}")

        self.PSO = PSO(self.hal, cb=self._publish)
        await self.PSO.init()
        await self.PSO.run_Bob_H1_Stabilization()
        await self.PSO.run_Bob_D2_Stabilization()
        await self.PSO.run_Alice_H1D2_Stabilization()
        await self.PSO.run_Bob_H2_Stabilization()

    def get_workflow_state(self):
        """
        Get current PSO workflow state for integration with QFC workflow.

        Returns:
            dict: Current stabilization state and visibilities
        """
        if self.PSO is None:
            return {"initialized": False, "message": "PSO not initialized"}

        return {
            "initialized": True,
            "step1_success": self.PSO.step1_success,
            "step2_success": self.PSO.step2_success,
            "step3_success": self.PSO.step3_success,
            "step4_success": self.PSO.step4_success,
            "visibilities": {
                "bob_h1": self.PSO.step1_visibility,
                "bob_d2": self.PSO.step2_visibility,
                "bob_h2": self.PSO.step4_visibility,
                "alice_h1": self.PSO.H1_visibility,
                "alice_d2": self.PSO.D2_visibility,
                "alice_h2": self.PSO.H2_visibility,
            },
        }

    def verify_pso_stabilization(self):
        """
        Verify PSO stabilization meets thresholds for QFC operation.

        Returns:
            bool: True if all stabilization steps are successful
        """
        if self.PSO is None:
            return False

        return self.PSO.step1_success and self.PSO.step2_success and self.PSO.step3_success and self.PSO.step4_success

    # Bob role methods (from PSO_b_interpreter)
    async def submit(self, *exp_info, **exp_param):
        log.info(f"Received Link Calibration submit request : {exp_info} {exp_param}")

        # 1. Always store parameters (Common logic)
        for i in exp_info[0].parameters.data:
            for k, v in i.items():
                self.parameters[k] = v

        # 2. Check role: Explicitly check 'bob' role.
        role = self.hal.config.role if self.hal.config.role else self.hal.config.cid
        is_bob = role.lower() == "bob"

        if not is_bob:
            # Alice/Charlie (Executor) role: delegate to parent to run experiment
            log.info(f"Agent acting as {role} (Executor) - running Link Calibration")
            await super().submit(*exp_info, **exp_param)
        else:
            # Bob (Coordinator) role: just stored params, do nothing else
            log.info(f"Agent acting as {role} (Coordinator) - stored parameters, skipping execution")

    async def update_result(self, exp_id):
        log.info(f"Getting calibration result for {exp_id}")
        return {"result": ""}

    def cancel(self, request):
        log.info(f"Received calibration cancel request : {request}")
        pass

    def get_schedulable_commands(self):
        commands = {
            "calibration.submit": [
                self.submit,
                "quantnet_mq.schema.models.calibration.submit",
                calibration.submitResponse,
                None,
            ],
            "calibration.getResult": [
                self.update_result,
                "quantnet_mq.schema.models.calibration.getResult",
                calibration.getResultResponse,
                None,
            ],
            "calibration.cancel": [
                self.cancel,
                "quantnet_mq.schema.models.calibration.cancel",
                calibration.cancelResponse,
                None,
            ],
        }
        return commands
