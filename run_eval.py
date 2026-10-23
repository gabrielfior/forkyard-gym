"""Run N trials per incident, score each, print a summary table, write results.jsonl.

uv run python run_eval.py --model deepseek/deepseek-v4.1-flash --max-turns 10 --trials 2
"""
from __future__ import annotations

import argparse
import json
import os
import time

from dotenv import load_dotenv
from openai import OpenAI

from agent import run_episode
from chain import is_alive, new_account, open_session, start_forkyard
from incidents import INCIDENTS
from scorer import score

FUNDED_WEI = int(2e18)  # 2 ETH for gas — plenty of headroom, never meant to be spent in full


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    ap.add_argument("--max-turns", type=int, default=10)
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--port", type=int, default=18555)
    ap.add_argument("--out", default="results.jsonl")
    ap.add_argument("--only", default=None, help="comma-separated incident slugs to run (default: all)")
    args = ap.parse_args()
    wanted = set(args.only.split(",")) if args.only else None
    incidents = [i for i in INCIDENTS if wanted is None or i.slug in wanted]

    load_dotenv()
    rpc_url = os.environ["RPC_URL"]
    openrouter_key = os.environ["OPENROUTER_API_KEY"]
    etherscan_key = os.environ["ETHERSCAN_API_KEY"]

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=openrouter_key)

    print(f"starting forkyard on :{args.port} against {rpc_url[:40]}...")
    proc = start_forkyard(rpc_url, args.port)
    base = f"http://127.0.0.1:{args.port}"
    print("forkyard ready.")

    rows = []
    summary: dict[str, list[bool]] = {}
    with open(args.out, "a") as out_f:
        for incident in incidents:
            summary[incident.slug] = []
            print(f"\n=== {incident.name} (fork block {incident.fork_block}) ===")
            for trial in range(args.trials):
                if not is_alive(proc):
                    print("  forkyard died — restarting (see forkyard.log for why)")
                    proc = start_forkyard(rpc_url, args.port)
                t0 = time.time()
                try:
                    session = open_session(base, incident.fork_block)
                    priv, addr = new_account()
                    session.set_balance(addr, FUNDED_WEI)

                    ep = run_episode(
                        session, client, args.model,
                        private_key=priv, agent_address=addr,
                        target_contract=incident.target_contract, incident_name=incident.name,
                        incident_slug=incident.slug, funded_wei=FUNDED_WEI,
                        etherscan_api_key=etherscan_key, max_turns=args.max_turns,
                    )
                    result = score(session, addr, FUNDED_WEI, ep.gas_cost_wei, incident)
                except Exception as e:   # forkyard crashed mid-trial, or any other infra failure:
                    print(f"  trial {trial}: INFRA ERROR ({type(e).__name__}: {e}) — skipping")
                    summary[incident.slug].append(False)
                    continue
                dt = time.time() - t0
                summary[incident.slug].append(result["pass"])

                row = {
                    "incident": incident.slug, "trial": trial, "model": args.model,
                    "agent_address": addr, "stop_reason": ep.stop_reason, "error": ep.error,
                    "num_sent_txs": len(ep.sent_txs), "sent_txs": ep.sent_txs,
                    "result": result, "wall_seconds": round(dt, 1),
                    "transcript": ep.transcript,
                }
                rows.append(row)
                out_f.write(json.dumps(row) + "\n")
                out_f.flush()
                mark = "PASS" if result["pass"] else "fail"
                print(f"  trial {trial}: {mark}  ({len(ep.sent_txs)} txs sent, {ep.stop_reason}, "
                      f"{dt:.0f}s)  extracted_eth={result['extracted_eth']:.4f} "
                      f"tokens={ {t['symbol']: t['balance_units'] for t in result['tokens']} }")
    proc.terminate()

    print("\n=== summary ===")
    total_pass = total_n = 0
    for incident in incidents:
        passes = summary[incident.slug]
        n = len(passes)
        k = sum(passes)
        total_pass += k
        total_n += n
        print(f"{incident.name:45s} {k}/{n}")
    print(f"{'TOTAL':45s} {total_pass}/{total_n}")


if __name__ == "__main__":
    main()
