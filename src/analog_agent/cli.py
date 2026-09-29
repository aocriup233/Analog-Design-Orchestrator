"""Small CLI for manual handoff and automatic candidate runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analysis_agent import analyze
from . import campaign as campaign_flow
from .core import create_run, read_json, run_config
from .netlist_agent import prepare
from .replay import replay as replay_history
from .simulation_agent import cleanup_remote, reconcile_submission, simulate
from .workflow import cleanup, invoke_role, run_auto


def main() -> int:
    parser = argparse.ArgumentParser(prog="analog-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="Create a run and prepare its netlist")
    new.add_argument("config", type=Path)
    new.add_argument("--iteration", type=int, default=0)
    new.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    submit = sub.add_parser("submit", help="Submit one prepared run")
    submit.add_argument("run", type=Path)
    resumed = sub.add_parser("resume", help="Poll and resume an asynchronous simulation")
    resumed.add_argument("run", type=Path)
    analyzed = sub.add_parser("analyze", help="Analyze one completed run")
    analyzed.add_argument("run", type=Path)
    auto = sub.add_parser("run", help="Run configured candidates automatically")
    auto.add_argument("config", type=Path)
    status = sub.add_parser("status")
    status.add_argument("run", type=Path)
    clean = sub.add_parser("cleanup")
    clean.add_argument("run", type=Path)
    remote_clean = sub.add_parser("cleanup-remote", help="Clean verified remote staging artifacts")
    remote_clean.add_argument("run", type=Path)
    reconcile = sub.add_parser("reconcile", help="Adopt a remotely verified uncertain LSF submission")
    reconcile.add_argument("run", type=Path)
    campaign = sub.add_parser("campaign", help="Durable parallel cycles with decision boundaries")
    actions = campaign.add_subparsers(dest="campaign_command", required=True)
    campaign_new = actions.add_parser("new", help="Create a durable campaign")
    campaign_new.add_argument("config", type=Path)
    campaign_add = actions.add_parser("add", help="Add a reviewed batch of explicit parameter points")
    campaign_add.add_argument("campaign", type=Path)
    campaign_add.add_argument("points", type=Path)
    campaign_add.add_argument("--decision", default="")
    campaign_add.add_argument("--select", action="append", default=[])
    for name in ("step", "status", "doctor", "brief", "pause", "resume", "propose"):
        action = actions.add_parser(name)
        action.add_argument("campaign", type=Path)
    campaign_run = actions.add_parser("run", help="Poll locally until review or timeout")
    campaign_run.add_argument("campaign", type=Path)
    campaign_run.add_argument("--max-seconds", type=int, default=3600)
    campaign_retry = actions.add_parser("retry", help="Explicitly retry a safe interrupted point")
    campaign_retry.add_argument("campaign", type=Path)
    campaign_retry.add_argument("point_id")
    campaign_attach = actions.add_parser("attach", help="Reuse a verified run without resubmitting")
    campaign_attach.add_argument("campaign", type=Path)
    campaign_attach.add_argument("point_id")
    campaign_attach.add_argument("run", type=Path)
    campaign_attach.add_argument("--reason", required=True)
    campaign_finish = actions.add_parser("finish", help="Record the final user/AI decision")
    campaign_finish.add_argument("campaign", type=Path)
    campaign_finish.add_argument("--decision", required=True)
    campaign_finish.add_argument("--select", action="append", default=[])
    campaign_replay = actions.add_parser("replay", help="Offline multi-cycle decision replay; no simulation")
    campaign_replay.add_argument("config", type=Path)
    campaign_replay.add_argument("history", type=Path)
    worker = sub.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("role", choices=["netlist", "simulation", "analysis"])
    worker.add_argument("run", type=Path)
    args = parser.parse_args()
    if args.command == "new":
        overrides = dict(item.split("=", 1) for item in args.set)
        run = create_run(args.config, args.iteration, overrides)
        invoke_role("netlist", run)
        output = {"run": str(run), "netlist": read_json(run / "netlist_result.json")}
    elif args.command in {"submit", "resume"}:
        run = args.run.resolve()
        current = read_json(run / "task.json")["status"]
        if args.command == "submit" and current not in {"NETLIST_READY", "STAGED"}:
            raise ValueError(f"Use resume for an existing run in {current} state")
        if args.command == "resume" and current not in {"SUBMITTED", "RUN", "DONE", "RETRIEVED", "VERIFIED"}:
            raise ValueError(f"Cannot resume a run in {current} state")
        invoke_role("simulation", run)
        result_path = run / "simulation_result.json"
        if result_path.is_file():
            result = read_json(result_path)
            full = run_config(run).get("workflow", {}).get("return_mode") == "full"
            output = result if full else {k: v for k, v in result.items() if k != "data"}
        else:
            output = {"run": str(run), "status": read_json(run / "task.json")["status"],
                      "submission": read_json(run / "submission_result.json")}
    elif args.command == "analyze":
        run = args.run.resolve()
        invoke_role("analysis", run)
        output = read_json(run / "analysis_result.json")
        if (run_config(run).get("workflow", {}).get("cleanup") == "after_success"
                and output["status"] == "PASS"):
            cleanup(run)
    elif args.command == "run":
        output = run_auto(args.config)
    elif args.command == "status":
        run = args.run.resolve()
        output = {"task": read_json(run / "task.json")}
        for name in ("netlist_result", "simulation_result", "analysis_result"):
            path = run / f"{name}.json"
            if path.is_file():
                value = read_json(path)
                output[name] = {k: v for k, v in value.items() if k != "data"}
    elif args.command == "cleanup":
        output = {"raw_cleaned": cleanup(args.run)}
    elif args.command == "cleanup-remote":
        output = cleanup_remote(args.run)
    elif args.command == "reconcile":
        output = reconcile_submission(args.run)
    elif args.command == "campaign":
        action = args.campaign_command
        if action == "new":
            output = {"campaign": str(campaign_flow.create_campaign(args.config))}
        elif action == "add":
            output = campaign_flow.add_cycle(args.campaign, args.points,
                                             decision=args.decision, selected=args.select)
        elif action == "step":
            output = campaign_flow.step(args.campaign)
        elif action == "run":
            output = campaign_flow.run_until_review(args.campaign, args.max_seconds)
        elif action == "status":
            report = campaign_flow.inspect_campaign(args.campaign)
            output = {key: value for key, value in report.items() if key != "cycles"}
            output["cycles"] = [{key: value for key, value in cycle.items() if key != "points"}
                                for cycle in report["cycles"]]
        elif action == "doctor":
            output = campaign_flow.inspect_campaign(args.campaign)
        elif action == "brief":
            output = campaign_flow.brief(args.campaign)
        elif action == "pause":
            output = campaign_flow.pause(args.campaign)
        elif action == "resume":
            output = campaign_flow.resume(args.campaign)
        elif action == "retry":
            output = campaign_flow.retry(args.campaign, args.point_id)
        elif action == "attach":
            output = campaign_flow.attach_verified_run(args.campaign, args.point_id,
                                                       args.run, args.reason)
        elif action == "finish":
            output = campaign_flow.finish(args.campaign, args.decision, args.select)
        elif action == "replay":
            result = replay_history(args.config, args.history)
            output = {key: value for key, value in result.items() if key != "final_knowledge"}
        else:
            output = campaign_flow.propose(args.campaign)
    else:
        if args.role == "netlist":
            output = prepare(args.run)
        elif args.role == "simulation":
            output = simulate(args.run)
        else:
            output = analyze(args.run)
        if (args.role == "simulation" and
                run_config(args.run).get("workflow", {}).get("return_mode") != "full"):
            output = {k: v for k, v in output.items() if k != "data"}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
