"""
run_dqc_pipeline.py
-------------------
End-to-end DQC pipeline runner — no subprocess.

Sends a circuit to the QNCP DQC plugin via RPC, receives the simulation
payload, and passes it directly to qnpack's run_from_labeled().

Default: the 3-QPU Grover-4 QASM circuit (grover4_3qpu.qasm) sent as
circuit_content in cisco mode.  Pass a JSON file of pre-partitioned commands
to use a different circuit.

Usage::

    # Noiseless (default), 1 run
    python run_dqc_pipeline.py

    # Noisy, 1000 runs, save CSV
    python run_dqc_pipeline.py --runs 1000 --noisy --csv grover4_3qpu_noisy.csv

    # Custom QASM, noiseless, 500 runs
    python run_dqc_pipeline.py --qasm path/to/circuit.qasm --runs 500 --csv out.csv

    # Pre-partitioned commands (tket mode)
    python run_dqc_pipeline.py commands.json --mode tket --runs 100 --csv out.csv
"""

import argparse
import asyncio
import json
import os
import sys

# Path to the default 3-QPU Grover-4 QASM circuit bundled with qnpack
_DEFAULT_QASM = os.path.join(
    os.path.dirname(__file__),
    "../../../qnpack/qnpack/dqc/qasm/grover4_3qpu.qasm",
)

# Standard noisy params matching the existing experiment CSVs
_NOISY_PARAMS = {
    "qpu": {
        "two_q_depolar_prob": 0.0001,
        "one_q_depolar_prob": 0.00001,
    },
    "memory": {
        "T1": 600_000_000,
        "T2": 60_000_000,
    },
}


# ── RPC client ────────────────────────────────────────────────────────────────


async def send_dqc_request(payload, host, schema_path, timeout=120.0):
    from quantnet_mq.rpcclient import RPCClient
    from quantnet_mq.schema.models import Schema

    Schema.load_schema(schema_path, ns="dqc")

    client = RPCClient("pipeline-dqc-client", host=host)
    client.set_handler("dqcRequest", None, "quantnet_mq.schema.models.dqc.dqcRequest")
    await client.start()
    try:
        raw = await client.call("dqcRequest", payload, timeout=timeout)
        return json.loads(raw)
    finally:
        await client.stop()


# ── Simulation (in-process) ───────────────────────────────────────────────────


def run_simulation(sim_payload, num_runs=1, noise=None, debug=False):
    import importlib.resources as _res
    from qnpack.dqc.sim import DQCSimulation

    _here = os.path.dirname(os.path.abspath(__file__))
    _pkg_params = os.path.join(_here, "../../../qnpack/qnpack/dqc/parameters.yml")
    if not os.path.exists(_pkg_params):
        try:
            _pkg_params = str(_res.files("qnpack.dqc") / "parameters.yml")
        except Exception:
            pass

    fixed_params = dict(noise) if noise else {}
    if debug:
        fixed_params.setdefault("sim", {})["debug"] = True

    sim = DQCSimulation(parameter_file=_pkg_params, fixed_params=fixed_params, varying_params={})
    return sim.start_from_labeled(sim_payload, num_runs=num_runs)


# ── CSV output ────────────────────────────────────────────────────────────────


def save_csv(results, csv_path):
    import pandas as pd

    df = pd.DataFrame(results)
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    df.to_csv(csv_path, index=False)
    print(f"Saved {len(df)} row(s) → {csv_path}")


# ── Main pipeline ─────────────────────────────────────────────────────────────


async def pipeline(rpc_payload, host, schema_path, num_runs, noise, csv_path, json_path):
    # ── Step 1: RPC to controller ─────────────────────────────────────────────
    noise_tag = "noisy" if noise else "noiseless"
    print(f"[1/3] Sending DQC request to {host} ...")
    response = await send_dqc_request(rpc_payload, host, schema_path)

    status = response.get("status", {})
    print(f"      Status: {status.get('value')}  RID: {response.get('rid')}")
    if status.get("value") != "OK":
        print(f"      Error: {status.get('message')}")
        sys.exit(1)

    data = response.get("data", {})
    print(f"      Start time: {data.get('startTime')}")

    sim_payload = data.get("simulation_payload")
    if sim_payload is None:
        print("ERROR: No simulation_payload in response — check plugin logs")
        sys.exit(1)

    labeled_commands = sim_payload["labeled_commands"]
    total_labeled = sum(len(v) for v in labeled_commands.values())
    print(f"      Labeled:    {total_labeled} commands across {len(labeled_commands)} QPU(s)")
    print(
        f"      Output reg: {sim_payload.get('output_reg_name', 'm')}  "
        f"bits: {sim_payload.get('num_output_bits', '?')}"
    )

    # ── Step 2: Simulate in-process ───────────────────────────────────────────
    print(f"\n[2/3] Running {num_runs} {noise_tag} simulation run(s) ...")
    results = run_simulation(sim_payload, num_runs=num_runs, noise=noise)
    print(f"      Completed {len(results)} run(s)")

    if results:
        from collections import Counter

        counts = Counter(r.get("bitstring", "?") for r in results)
        print("      Top bitstrings:")
        for bs, cnt in counts.most_common(5):
            print(f"        {bs}  ({cnt}/{len(results)})")

    # ── Step 3: Output ────────────────────────────────────────────────────────
    print("\n[3/3] Saving results ...")

    if csv_path:
        save_csv(results, csv_path)

    if json_path:
        with open(json_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Saved JSON → {json_path}")

    if not csv_path and not json_path:
        print(json.dumps(results, indent=2))

    return results


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="run_dqc_pipeline",
        description="End-to-end DQC pipeline: RPC → label → schedule → simulate (in-process).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "commands_file",
        nargs="?",
        default=None,
        metavar="FILE",
        help="JSON file with pre-partitioned commands {qpu_id: [cmd, ...]}. "
        "Default: send the 3-QPU Grover-4 QASM circuit.",
    )
    parser.add_argument(
        "--qasm",
        default=None,
        metavar="FILE",
        help="Path to a QASM circuit file (cisco mode). " "Overrides the default grover4_3qpu.qasm.",
    )
    parser.add_argument(
        "--mode",
        "-m",
        default="tket",
        choices=["tket", "cisco"],
        help="Circuit mode when commands_file is provided (default: tket).",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "localhost"),
        help="Controller host (default: $HOST or localhost).",
    )
    parser.add_argument(
        "--runs",
        "-n",
        type=int,
        default=1,
        help="Number of simulation runs (default: 1).",
    )
    parser.add_argument(
        "--noisy",
        action="store_true",
        default=False,
        help="Apply standard noise (two_q=0.0001, one_q=0.00001, T1=600ms, T2=60ms).",
    )
    parser.add_argument(
        "--csv",
        default=None,
        metavar="FILE",
        help="Write results to a CSV file (compatible with result_analysis.ipynb).",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        metavar="FILE",
        help="Write results to a JSON file.",
    )
    args = parser.parse_args()

    schema_path = os.path.join(os.path.dirname(__file__), "../schema/dqc.yaml")
    noise = _NOISY_PARAMS if args.noisy else None

    if args.commands_file:
        with open(args.commands_file) as f:
            partitioned_commands = json.load(f)
        print(f"Loaded partitioned commands from {args.commands_file}")
        rpc_payload = {"circuit_mode": args.mode, "partitioned_commands": partitioned_commands}
    else:
        qasm_path = os.path.normpath(args.qasm or _DEFAULT_QASM)
        with open(qasm_path) as f:
            circuit_content = f.read()
        print(f"Using QASM circuit: {qasm_path}")
        rpc_payload = {"circuit_mode": "cisco", "circuit_content": circuit_content}

    asyncio.run(
        pipeline(
            rpc_payload,
            host=args.host,
            schema_path=schema_path,
            num_runs=args.runs,
            noise=noise,
            csv_path=args.csv,
            json_path=args.output,
        )
    )


if __name__ == "__main__":
    main()
