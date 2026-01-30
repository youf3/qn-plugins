from quantnet_controller.common.experimentdefinitions import Sequence, AgentSequences, Experiment
from quantnet_controller.common.constants import ExperimentType
from datetime import timedelta


class Init(Sequence):
    name = "PSO_Initialization"
    class_name = "PSO_Initialization"
    duration = timedelta(seconds=1)
    dependency = []


class Bob_H1_Stabilization(Sequence):
    name = "Bob_H1_Stabilization"
    class_name = "Bob_H1_Stabilization"
    duration = timedelta(seconds=120)
    dependency = []


class Bob_D2_Stabilization(Sequence):
    name = "Bob_D2_Stabilization"
    class_name = "Bob_D2_Stabilization"
    duration = timedelta(seconds=120)
    dependency = []


class Alice_H1D2_Stabilization(Sequence):
    name = "Alice_H1D2_Stabilization"
    class_name = "Alice_H1D2_Stabilization"
    duration = timedelta(seconds=240)
    dependency = []


class Bob_H2_Stabilization(Sequence):
    name = "Bob_H2_Stabilization"
    class_name = "Bob_H2_Stabilization"
    duration = timedelta(seconds=120)
    dependency = []


class LinkCalQnodeLBNLSequence(AgentSequences):
    name = "LinkCal for Qnode@LBNL"
    node_type = "QNode"
    sequences = [Init]  # , Bob_H1_Stabilization, Bob_D2_Stabilization, Alice_H1D2_Stabilization, Bob_H2_Stabilization]


class LinkCalQnodeUCBSequence(AgentSequences):
    name = "LinkCal for Qnode@UCB"
    node_type = "QNode"
    sequences = [Init]  # , Bob_H1_Stabilization, Bob_D2_Stabilization, Alice_H1D2_Stabilization, Bob_H2_Stabilization]


class LinkCalibration(Experiment):
    name = "Link Calibration"
    agent_sequences = [LinkCalQnodeLBNLSequence, LinkCalQnodeUCBSequence]
    type = ExperimentType.CALIBRATION

    def get_sequence(self, agent_index):
        return self.agent_sequences[agent_index]
