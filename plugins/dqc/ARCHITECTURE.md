# DQC Plugin Architecture

## Overview

The DQC plugin is a **control-plane request handler** that orchestrates distributed quantum circuit execution across multiple QPUs in the Quant-Net platform. It bridges circuit-level specifications (tket command files or QASM 3) with scheduler slot allocation and hardware entanglement sequences. The plugin:

1. **Parses and validates** circuits via qnpack's frontend and labeling pipeline
2. **Computes dynamic prefill** reservations based on circuit entanglement demand
3. **Discovers network routes** for cross-QPU entanglement pairs
4. **Injects real EGP sequences** for hardware-managed entanglement
5. **Allocates scheduler slots** and produces a simulation payload

## Relationship to qnpack Simulator

This plugin is the **control plane** layer; the qnpack DQC simulator is the **simulator** layer. They are **complementary, not redundant**.

**Control plane (this plugin) handles:**
- Circuit parsing and scheduling request flow
- Agent capability discovery and prefill slot reservation
- Route discovery for entanglement paths
- EGP sequence injection for unified experiment submission

**Simulator (qnpack/dqc) handles:**
- Bell-pair pool management (`pool_size_per_pair`, storage allocation)
- Pool-only mode and drain timeout (`pool_only`, `drain_timeout_ns`)
- Fidelity-aware eviction and pair staleness tracking
- BSM leasing and contention resolution (one round per BSM at a time)
- Slot identity coordination (controller-chosen slot bindings)
- On-demand fallback and continuous refill during execution

**The connection:** Our `prefill_map` reserves **TDMA scheduler time slots** at the beginning of each agent's schedule. The simulator reads this reservation as a hint to pre-fill pools ahead of time. However, the simulator's actual pool behavior is configured via `parameters.yml` (`epr_factory`, `pool_size_per_pair`, `comm_qubits_reserved`, `pool_only`, `drain_timeout_ns`, `min_fidelity`). A future enhancement would pass these parameters through the experiment payload to drive the simulator's pool configuration dynamically.

## Request Flow

```
DQC Request (dqcRequest)
  ↓
Deserialize payload to plain Python objects
  ↓
Parse circuit (pre-partitioned or via qnpack frontend)
  ↓
Validate & label via qnpack (tket or cisco mode)
  ↓
Pre-schedule entanglement (move ent_gen earlier if enabled)
  ↓
Get QPU info from live topology (MongoDB)
  ↓
Convert labeled IR → flat timeslot schedule
  ↓
Extract QPU agent IDs and cross-QPU pairs
  ↓
Find network routes for each cross-QPU pair (via router)
  ↓
Extract BSM node IDs from paths (first IN_SPEC BSM per route)
  ↓
Build EGP sequence class map { agent_id: { label: SequenceClass } }
  ↓
Read entanglement capabilities from agent registration
  ↓
Determine mode: continuous if any QPU has continuous_entanglement=true
  ↓
Compute dynamic prefill_map: count ent_gen per agent × prefill_cost_per_pair
  ↓
Build DynamicExperiment with sequences and egp_sequences map
  ↓
Set prefill_map attribute on experiment
  ↓
Get slot allocations from scheduler
  ↓
Return allocations + routes + simulation payload
```

## Entanglement Capability Model

### Capability Registration

When agents register with the controller, they report entanglement capabilities in MongoDB:

```json
{
  "capabilities": {
    "continuous_entanglement": true/false,
    "prefill_cost_per_pair": <int>  // scheduler time slots per pre-filled pair
  }
}
```

These are **static hardware properties**, set once at agent registration time. Example configurations are provided in:
- `/qn-mq/quantnet_mq/schema/examples/topology/conf_lbnl-q.json`
- `/qn-mq/quantnet_mq/schema/examples/topology/conf_ucb-q.json`

**Note:** `prefill_cost_per_pair` is a **control-plane parameter** (scheduler time slots) — it is distinct from the simulator's `pool_size_per_pair` and related pool config parameters, which are set in `parameters.yml` and control the simulator's internal Bell-pair pool behavior.

### Dynamic Prefill Computation

The DQC plugin:

1. **Reads capabilities** from all QPU agents via `ctx.rm.get_nodes()` (MongoDB access)
2. **Determines mode**: If **any** QPU supports continuous entanglement, `use_continuous = True`
3. **Counts entanglement operations**: For each `entanglement_gen` command, counts occurrences per involved QPU
4. **Computes prefill reservation** (in scheduler time slots):
   ```
   prefill_map[agent_id] = count_of_ent_gen_for_agent * prefill_cost_per_pair
   ```
5. **Sets on experiment**: `DynamicExperiment.prefill_map = prefill_map` (empty dict if on-demand)

### Scheduler Handling

The scheduler (`RequestTranslator.translate_request()` in qn-server) reads `prefill_map` from the experiment and:

1. **Expands slot search pattern** to include prefill slots: `total_length = seq_length + prefill_map.get(agent_id, 0)`
2. **Inserts prefill allocation block** at the beginning of each agent's allocations:
   - `expName`: `"entanglement_prefill"`
   - `className`: `"entanglement_prefill"`
   - `timeSlot`: first `prefill_slots_count` indices from the agent's slot list
3. **Consumes prefill slots** from the timeslot mask before assigning sequence slots

This reserves **dead time** at the start of execution during which the control plane signals the hardware to pre-fill entanglement pools. The actual pool configuration (size, draining behavior, etc.) is the simulator's responsibility.

## Sequence Structure

### DynamicExperiment Class

```python
class DynamicExperiment(Experiment):
    name = exp_name
    agent_sequences = [...]  # AgentSequences per agent
    prefill_map = {}         # {agent_id: prefill_slots}
```

### Sequence Types per Block

Each command block is wrapped in a Sequence subclass:

1. **BlockSequence**: Local gate operations
   - `duration`: Computed from command count (carry-forward slot packing)
   - `command_list`: The actual gate commands
   - `dependency`: Previous sequence (ensures order)

2. **EGPSequence** (if available): Cross-QPU entanglement
   - `duration`: Fixed hardware duration from EGP class
   - No `command_list` (hardware-managed)
   - `dependency`: Previous sequence

### Agent Sequences

Each agent gets an `AgentSequences` object:

```python
class DynamicAgentSeq(AgentSequences):
    name = f"Seq_{agent_id}"
    node_type = "QNode" or "BSMNode"
    sequences = [...]  # Ordered BlockSequence + EGPSequence
```

**Pure-EGP agents** (BSM nodes with no gate commands) have only EGPSequences.

## EGP Sequence Injection

If the EGP plugin is available (`egp.experiment`), the DQC plugin:

1. Extracts entanglement labels for each cross-QPU pair
2. Maps labels to EGP sequence classes (`QnodeEGP`, `BSMnodeEGP`)
3. Passes the map to `build_dynamic_experiment(..., egp_sequences=map)`
4. The builder injects real EGP classes instead of BlockSequence placeholders

This enables a single unified experiment submission with both gate and entanglement operations.

### Fallback

If EGP plugin is unavailable, entanglement blocks default to opaque `BlockSequence` placeholders.

## Slot Allocation

### Carry-Forward Packing

DQC uses a "carry-forward" algorithm to pack multiple command blocks into agent scheduler slots:

```
DQC_TIMESLOT_MS = 3
SLOTSIZE = 100ms (per Constants)

total_dqc_ms = 0
for block in blocks:
    total_dqc_ms += len(block) * DQC_TIMESLOT_MS
    new_total_slots = ceil(total_dqc_ms / SLOTSIZE_ms)
    agent_slots = max(new_total_slots - allocated, 1)
    allocate(agent_slots)
```

This accumulates fractional time and only allocates new slots when the cumulative total crosses a slot boundary.

### Allocation Output

```python
allocations = {
    agent_id: [
        {
            "offset": start_time_sec,
            "commands": [
                {"slot": slot_idx, "message": cmd_str},
                ...
            ]
        },
        ...
    ]
}
```

## Known Limitations

1. **Single BSM per pair**: `extract_bsm_nodes_from_path()` returns only the first IN_SPEC BSM found on a route. Multi-hop routes with redundant BSMs are not leveraged; all entanglement for a pair routes through one BSM. More complex multi-hop topologies would require custom routing logic.

2. **Static capabilities per request**: Entanglement capabilities are read once per DQC request from MongoDB. For rapid capability changes (e.g., agent hardware reconfiguration), agent re-registration is required; the plugin does not poll for updates during execution.

3. **No simulator pool configuration passthrough**: The control plane computes `prefill_map` (scheduler time slots) but does not pass simulator pool parameters (`pool_size_per_pair`, `pool_only`, `drain_timeout_ns`, `comm_qubits_reserved`, `min_fidelity`) through the experiment payload. These remain configured via `parameters.yml`, limiting dynamic pool tuning.

4. **No fidelity-aware selection**: Control plane has no awareness of Bell-pair fidelity or staleness. Fidelity-aware eviction and pair age thresholding (§4.2 of qnpack ARCHITECTURE) are handled entirely by the simulator.

5. **Carry-forward packing is greedy**: The slot accumulation algorithm does not optimize for load balance across agents; it packs sequentially. An agent with many small blocks may fit differently than one with few large blocks.

6. **Pre-scheduling assumes fixed latency**: The `EXPECTED_ENT_LATENCY_NS` constant is a single value applied to all pairs. Actual latency varies with BSM availability, network congestion, and fidelity constraints; no feedback loop adjusts the value.

## Future Work

- **Make constants configurable**: Move `PRE_SCHEDULE_ENTANGLEMENT`, `EXPECTED_ENT_LATENCY_NS`, `ONE_Q_GATE_DURATION_NS`, `TWO_Q_GATE_DURATION_NS` to a plugin config file instead of hard-coded class constants.

- **Pass simulator pool parameters**: Extend experiment payload to include `pool_size_per_pair`, `pool_only`, `drain_timeout_ns`, `comm_qubits_reserved`, `min_fidelity` and pass them to qnpack's configuration layer so the control plane can drive pool behavior dynamically.

- **Support `pool_only` mode selection**: Allow the control plane to specify whether the simulator should use pool-only (no on-demand fallback) vs. pooled-with-fallback mode.

- **Adaptive latency estimation**: Implement feedback from actual simulation results to refine `EXPECTED_ENT_LATENCY_NS` for future circuits.

- **Multi-BSM route selection**: Support routes with redundant BSMs and load-balance entanglement across them instead of always picking the first IN_SPEC BSM.

- **Fidelity constraints from control plane**: Expose simulator fidelity thresholds to the control plane so pre-fill reservation accounts for pair age and decoherence.

- **Carry-forward optimization**: Replace greedy slot packing with a balanced allocation that minimizes makespan and load skew across agents.

- **Pool deallocation tracking**: Implement post-circuit cleanup and pool resource accounting (though pools are simulator-managed, tracking their lifecycle in the control plane could improve observability).
