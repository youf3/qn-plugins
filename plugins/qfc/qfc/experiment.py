from quantnet_controller.common.experimentdefinitions import Sequence, AgentSequences, Experiment
from datetime import timedelta


class QnodeLQFC(Sequence):
    name = "QFC"
    class_name = "QFC_full_workflow"
    duration = timedelta(seconds=240)
    dependency = []


class QFCQnodeLBNLSequence(AgentSequences):
    name = "QFC for Qnode@LBNL"
    node_type = "QNode"
    sequences = [QnodeLQFC]


class QFCExperiment(Experiment):
    name = "QFC"
    agent_sequences = [QFCQnodeLBNLSequence]

    def get_sequence(self, agent_index):
        return self.agent_sequences[agent_index]
