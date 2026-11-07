"""reqreview CLI.

    reqreview tui [--demo] [--requirements PATH] [--data-dir PATH] [--speed 1.0] [--autoplay]
    reqreview doctor [--provider openrouter|openai]
    reqreview logs [--errors] [-n 60] [--demo]
    reqreview sim add|edit|rm ID [--text T | --file F] [--demo]     (fake Jira: edits requirements.json only)
    reqreview replay PATH
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except Exception:
        pass


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reqreview", description="Loop Engineering Studio")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tui", help="launch the terminal UI")
    t.add_argument("--demo", action="store_true", help="scripted replay, no API key or network needed")
    t.add_argument("--requirements", default="requirements.json")
    t.add_argument("--data-dir", default="data")
    t.add_argument("--speed", type=float, default=1.0, help="demo replay speed multiplier")
    t.add_argument("--autoplay", action="store_true", help="demo: fire a simulated external webhook after a few seconds")
    t.add_argument("--rubric-iterations", type=int, default=3)
    t.add_argument("--provider", choices=["openrouter", "openai"], help="LLM provider (default: from env / keys)")

    d = sub.add_parser("doctor", help="check provider key, models, tool calling and quota")
    d.add_argument("--provider", choices=["openrouter", "openai"])

    lg = sub.add_parser("logs", help="show the readable log (data/logs/studio.log)")
    lg.add_argument("--errors", action="store_true", help="only warnings and errors")
    lg.add_argument("-n", type=int, default=60)
    lg.add_argument("--demo", action="store_true")
    lg.add_argument("--data-dir", default="data")

    s = sub.add_parser("sim", help="simulate Jira: add/edit/remove a requirement in requirements.json")
    s.add_argument("action", choices=["add", "edit", "rm"])
    s.add_argument("ticket_id")
    s.add_argument("--text")
    s.add_argument("--file")
    s.add_argument("--force", action="store_true")
    s.add_argument("--demo", action="store_true", help="target the demo data dir (data/demo)")
    s.add_argument("--requirements", default="requirements.json")
    s.add_argument("--data-dir", default="data")

    r = sub.add_parser("replay", help="print a stored run")
    r.add_argument("path")
    return p


def cmd_tui(args: argparse.Namespace) -> int:
    from .context import build_context

    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider
    if not args.demo:
        from . import llm
        if not llm.has_key():
            print(f"{llm.key_env_name()} is not set (provider: {llm.provider()}; checked environment and .env).\n"
                  "Add it to .env, set LLM_PROVIDER=openai|openrouter, or try the offline experience:  "
                  "reqreview tui --demo", file=sys.stderr)
            return 2
    try:
        ctx = build_context(args.requirements, args.data_dir, demo=args.demo, speed=args.speed,
                            rubric_iterations=args.rubric_iterations)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2
    from .tui.app import StudioApp

    StudioApp(ctx, autoplay=args.autoplay).run()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import llm
    from .diag import explain

    if getattr(args, "provider", None):
        os.environ["LLM_PROVIDER"] = args.provider
    prov = llm.provider()
    print(f"Provider: {prov}   base url: {llm.base_url()}")
    if not llm.has_key():
        print(f"{llm.key_env_name()}: MISSING")
        print("  Fix: copy .env.example to .env and set it (or LLM_PROVIDER=openai|openrouter). No key? use --demo.")
        return 1
    print(f"{llm.key_env_name()}: found")
    if prov == "openrouter":
        found = llm.discover_free_models(force=True)
        print(f"Free tool-capable models available: {len(found) if found is not None else 'unknown (offline?)'}")
    for role in ("reviewer", "grader", "analyst"):
        models = llm.pick_models(role)
        print(f"  {role:<9} -> {models[0]}   (others: {', '.join(models[1:3]) or 'none'})")
    print("Probing tool calling, one request per candidate (a 403 means that model is blocked for you)...")
    any_ok = False
    for m in llm.pick_models("reviewer")[:5]:
        res = llm.probe(m)
        any_ok |= res["ok"]
        print(f"  {'OK  ' if res['ok'] else 'FAIL'} {m}  {res['seconds']}s")
        if not res["ok"]:
            exp = explain(res["detail"])
            print(f"       what happened: {exp.title}")
            print(f"       likely cause : {exp.cause}")
            print(f"       how to fix   : {exp.fix}")
            print(f"       raw          : {res['detail']}")
    print(f"Quota: {llm.quota_text()}")
    return 0 if any_ok else 1


def cmd_logs(args: argparse.Namespace) -> int:
    from .diag import DiagLog

    d = Path(args.data_dir) / ("demo" if args.demo else "") / "logs"
    log = DiagLog(d)
    lines = log.tail(args.n, errors_only=args.errors)
    if not lines:
        print(f"No log lines yet in {log.studio}")
        return 0
    print(f"# {log.studio}  (full tracebacks: {log.errors})")
    print("\n".join(lines))
    return 0


def cmd_sim(args: argparse.Namespace) -> int:
    from .store import Store

    data_dir, req = Path(args.data_dir), Path(args.requirements)
    if args.demo:
        data_dir = data_dir / "demo"
        req = data_dir / "requirements.json"
    store = Store(req, data_dir)
    store.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        reqs = store.load_requirements() if req.exists() else {}
    except Exception as exc:
        print(f"cannot read {req}: {exc}", file=sys.stderr)
        return 2
    tid = args.ticket_id
    if args.action == "rm":
        if tid not in reqs:
            print(f"{tid} not found", file=sys.stderr)
            return 1
        del reqs[tid]
    else:
        if args.file:
            text = Path(args.file).read_text(encoding="utf-8")
        elif args.text:
            text = args.text.replace("\\n", "\n")
        else:
            print("provide --text or --file", file=sys.stderr)
            return 2
        if not text.endswith("\n"):
            text += "\n"
        if args.action == "add" and tid in reqs and not args.force:
            print(f"{tid} already exists (use 'edit', or --force)", file=sys.stderr)
            return 1
        if args.action == "edit" and tid not in reqs:
            print(f"{tid} not found (use 'add')", file=sys.stderr)
            return 1
        reqs[tid] = text
    store.write_requirements(reqs, via="cli")
    print(f"{args.action} {tid} -> {req}  (the watcher in the TUI turns this into an event)")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .agent import VerificationRun, load_run_with_meta

    run, meta = load_run_with_meta(args.path)
    print(f"{meta.get('ticket_id', '?')} run {meta.get('run_n', '?')}  status={getattr(run, 'status', '?')}  turns={run.turns}")
    for s in run.trace:
        print(f"[{s.step:>2}] {s.actor:<16} {s.action}" + (f"\n     {s.detail}" if s.detail else ""))
    if isinstance(run, VerificationRun):
        for g in run.grading:
            print(f"grading pass {g.iteration}: {g.result}" + (f" (failed: {', '.join(g.failed)})" if g.failed else ""))
    print("\n" + run.analysis_markdown)
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = _parser().parse_args(argv)
    return {"tui": cmd_tui, "doctor": cmd_doctor, "logs": cmd_logs, "sim": cmd_sim, "replay": cmd_replay}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
