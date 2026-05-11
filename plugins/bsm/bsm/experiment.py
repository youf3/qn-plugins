from quantnet_controller.common.experimentdefinitions import Sequence, AgentSequences, Experiment
from datetime import timedelta


class QnodeLBNLBSM(Sequence):
    name = "BSM"
    class_name = "BSM"
    duration = timedelta(seconds=60)
    dependency = []


class BSMnodeLBNLSequence(AgentSequences):
    name = "BSM for BSMNode@LBNL"
    node_type = "BSMNode"
    sequences = [QnodeLBNLBSM]


class QnodeLBNLSequence(AgentSequences):
    name = "BSM for Qnode@LBNL"
    node_type = "QNode"
    sequences = [QnodeLBNLBSM]


class QnodeUCBSequence(AgentSequences):
    name = "BSM for Qnode@UCB"
    node_type = "QNode"
    sequences = [QnodeLBNLBSM]


class BSMExperiment(Experiment):
    name = "BSM"
    agent_sequences = [QnodeLBNLSequence, QnodeUCBSequence, BSMnodeLBNLSequence]

    def get_sequence(self, agent_index):
        return self.agent_sequences[agent_index]
