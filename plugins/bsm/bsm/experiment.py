from quantnet_controller.common.experimentdefinitions import Sequence, AgentSequences, Experiment
from datetime import timedelta


class QnodeLBSM(Sequence):
    name = "BSM"
    class_name = "BSM"
    duration = timedelta(seconds=240)
    dependency = []


class BSMQnodeLBNLSequence(AgentSequences):
    name = "BSM for Qnode@LBNL"
    node_type = "QNode"
    sequences = [QnodeLBSM]


class BSMExperiment(Experiment):
    name = "BSM"
    agent_sequences = [BSMQnodeLBNLSequence, BSMQnodeLBNLSequence]

    def get_sequence(self, agent_index):
        return self.agent_sequences[agent_index]
