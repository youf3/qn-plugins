# DQC Plugin Architecture

## Overview

The DQC plugin handles distributed quantum circuit execution across multiple QPUs in the Quant-Net platform. It orchestrates:

1. **Circuit parsing and labeling** via qnpack's frontend and labeling pipeline
2. **Command scheduling** and slot allocation across QPU agents
3. **Entanglement mode selection** and dynamic prefill computation
4. **Route discovery** for cross-QPU pairs
5. **EGP sequence injection** for real hardware entanglement sequences

## Request Flow

```
DQC Request (dqcRequest)
  ↓
Parse circuit (partitioned_commands or circuit_file/circuit_content)
  ↓
Validate & label via qnpack
  ↓
Pre-schedule entanglement (move ent_gen earlier in schedule)
  ↓
Convert labeled IR → flat timeslot schedule
  ↓
Extract QPU agent IDs and cross-QPU pairs
  ↓
Find network routes for each cross-QPU pair
  ↓
Collect EGP sequence classes for entanglement blocks
  ↓
Read entanglement capabilities from agent registration (MongoDB)
  ↓
Determine entanglement mode (continuous vs on-demand)
  ↓
Compute dynamic prefill_map from circuit entanglement operations
  ↓
Build DynamicExperiment with sequences and prefill_map
  ↓
Allocate slots via scheduler
  ↓
Return allocations + simulation payload
```

## Entanglement Capability Model

### Capability Registration

When agents register with the controller, they report entanglement capabilities in MongoDB:

```json
{
  "capabilities": {
    "entanglement": {
      "continuous_entanglement": true/false,
      "prefill_cost_per_pair": <int>  // time slots needed to prefill one pair
    }
  }
}
```

These are **static hardware properties**, set once at registration time.

### Dynamic Prefill Computation

The DQC plugin:

1. **Reads capabilities** from all QPU agents via `ctx.rm.get_nodes()` (MongoDB access)
2. **Determines mode**: If any QPU supports continuous entanglement, mode = continuous
3. **Counts entanglement operations**: For each `entanglement_gen` command, count per involved QPU
4. **Computes prefill map**:
   ```
   prefill_map[agent_id] = count_of_ent_gen_for_agent * prefill_cost_per_pair
   ```
5. **Sets on experiment**: `DynamicExperiment.prefill_map = prefill_map`

### Scheduler Handling

The scheduler reads `prefill_map` from the experiment definition and:

- **If prefill_map is non-empty**: Reserve slots at the beginning of each agent's schedule
- **If prefill_map is empty** (on-demand mode): No reservation needed

This is a scheduler-level concern; the DQC plugin only computes and provides the map.

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

## Implementation Details

### Constants

- `PRE_SCHEDULE_ENTANGLEMENT = True`: Enable/disable pre-scheduling of ent_gen
- `EXPECTED_ENT_LATENCY_NS = 1_000_000`: Expected Bell-pair generation latency
- `ONE_Q_GATE_DURATION_NS = 5000`: Estimated single-qubit gate duration
- `TWO_Q_GATE_DURATION_NS = 10700`: Estimated two-qubit gate duration

### Methods

#### `_read_entanglement_capabilities(agent_ids)`

**Sync method** — reads from MongoDB via ResourceManager, no RPC.

Returns:
```python
{
    agent_id: {
        "continuous_entanglement": bool,
        "prefill_cost_per_pair": int
    }
}
```

Gracefully handles missing registration data (defaults to on-demand only).

#### `_compute_prefill_map(commands, qpu_agent_ids, capabilities, use_continuous)`

Counts `entanglement_gen` operations per agent and multiplies by `prefill_cost_per_pair`.

Returns empty dict if `use_continuous` is False.

#### `_pre_schedule(labeled)`

Moves `entanglement_gen` commands earlier in the schedule to overlap latency with gate execution.

Mutations: `labeled` dict in place.

#### `_label_commands(partitioned)`

Validate and label via qnpack, producing:
- `labeled`: Command dicts with per-operation labels
- `process_maps`: Metadata for IR conversion

#### `labeled_ir_to_timeslot_schedule(labeled, process_maps, qpu_id_to_label)`

Converts labeled IR to a flat timeslot schedule, adding:
- `qpu_id`: Integer agent identifier
- `op`: Operation type (gate, entanglement_gen, etc.)
- `qpus_involved`: List of agent IDs for cross-QPU ops

## Testing

### Test Classes

1. **TestDQCLogic**: Core experiment building and QPU pair extraction
2. **TestDQCPreScheduling**: Pre-entanglement scheduling behavior
3. **TestDQCEGPInjection**: EGP sequence injection and fallback
4. **TestDynamicPrefillComputation**: Prefill map calculation logic (6 scenarios)
5. **TestPrefillMapOnExperiment**: Prefill_map attribute on experiments

### Test Coverage

- Single and multiple entanglement_gen operations
- Asymmetric agent participation in pairs
- Zero entanglement circuits
- Missing/zero capability costs
- Continuous vs on-demand modes
- Pure-EGP agents (BSM nodes)
- Dependency chain ordering
- Carry-forward slot packing

## Known Limitations

1. **Single BSM per pair**: `extract_bsm_nodes_from_path()` returns only the first IN_SPEC BSM. More complex multi-hop routes require custom routing logic.

2. **No pool deallocation**: Continuous entanglement pools prefilled before circuit execution are not explicitly deallocated after. Future: add cleanup RPC calls.

3. **Static capabilities**: Entanglement capabilities are read once at plugin load time per circuit, not refreshed per request. For dynamic capability changes, agent re-registration is required.

## Future Work

- Make constants configurable via plugin config file
- Support agent-level entanglement pool size limits
- Implement fidelity constraints on Bell-pair selection
- Add speculative pre-fetch for multi-circuit workloads
