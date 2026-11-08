"""`python -m app.cli realtime ...` commands (registered by app.cli; owner: realtime).

    realtime start [--streams a,b] [--consumers a,b] [--param stream.key=value ...] [--no-sentiment] [--duration S]
    realtime status [--json]
    realtime compact                      hot store -> live_* lake tables now (also reconcile with nightly)
    realtime dlq [--group G] [--requeue ID ...]
    realtime replay --group G --topic T --from-id N     rewind a consumer group (re-processing is idempotent)
    realtime quotes [--duration S] [--once] [--param key=value ...]   delayed quotes service (supervisor child `quotes`)
    realtime quotes-status [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def add_parser(sub: Any) -> None:
    r = sub.add_parser("realtime", help="real-time layer (R1)").add_subparsers(dest="realtime_cmd", required=True)
    p = r.add_parser("start", help="run the realtime supervisor in the foreground (Ctrl+C stops)")
    p.add_argument("--streams", help="comma list (default all): edgar,fca_nsm,tdnet,hkex,dart,rss_official,"
                                     "macro_capture,gdelt,polygon_news")
    p.add_argument("--consumers", help="comma list (default enrich,insider,changepoint,alerts)")
    p.add_argument("--param", action="append", help="stream.key=value (e.g. edgar.interval_s=20)")
    p.add_argument("--no-sentiment", action="store_true", help="skip FinBERT in the enrich consumer")
    p.add_argument("--duration", type=float, help="stop after N seconds")
    p.set_defaults(fn=cmd_start)
    p = r.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = r.add_parser("compact")
    p.set_defaults(fn=cmd_compact)
    p = r.add_parser("dlq")
    p.add_argument("--group")
    p.add_argument("--requeue", nargs="+", type=int)
    p.set_defaults(fn=cmd_dlq)
    p = r.add_parser("replay")
    p.add_argument("--group", required=True)
    p.add_argument("--topic", required=True)
    p.add_argument("--from-id", type=int, required=True, help="messages with id > N are delivered again")
    p.set_defaults(fn=cmd_replay)
    p = r.add_parser("quotes", help="delayed quotes service (workstream B; supervisor child `quotes`)")
    p.add_argument("--duration", type=float, help="stop after N seconds")
    p.add_argument("--once", action="store_true", help="one tick (universe + due fetches), print the report, exit")
    p.add_argument("--param", action="append", help="key=value (open_every_s=60, h24_every_s=120, yahoo_per_hour=400)")
    p.set_defaults(fn=cmd_quotes)
    p = r.add_parser("quotes-status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_quotes_status)


def _csv(x: str | None) -> list[str] | None:
    return [s.strip() for s in x.split(",") if s.strip()] if x else None


#: realtime child: hard exit at the latest this long after the stop request (the supervisor's grace is 60 s)
REALTIME_EXIT_DEADLINE_S = 45.0
#: quotes child: same (supervisor grace 30 s; one Yahoo request can take its 20 s timeout)
QUOTES_EXIT_DEADLINE_S = 22.0


def cmd_start(args: argparse.Namespace) -> int:
    """Supervisor child `realtime` (or a foreground run). Ends with a hard exit after the cleanup
    (app.ops.shutdown.finish_child; BIG360_CHILD_HARD_EXIT=0 returns instead)."""
    import logging  # noqa: PLC0415

    from app.core.config import get_settings  # noqa: PLC0415
    from app.core.logsetup import setup_logging  # noqa: PLC0415
    from app.ops.shutdown import finish_child  # noqa: PLC0415
    from app.realtime.supervisor import RealtimeAlreadyRunning, Supervisor, parse_kv_params  # noqa: PLC0415

    s = get_settings()
    s.ensure_dirs()
    setup_logging(log_file="realtime.log", service="realtime")
    params = parse_kv_params(args.param)
    cparams: dict[str, dict[str, Any]] = {}
    if args.no_sentiment:
        cparams["enrich"] = {"sentiment": False}
    sup = Supervisor(s, streams=_csv(args.streams), consumers=_csv(args.consumers), stream_params=params,
                     consumer_params=cparams, exit_deadline_s=REALTIME_EXIT_DEADLINE_S)
    try:
        rc = sup.run(duration_s=args.duration)
    except RealtimeAlreadyRunning as e:
        print(str(e), file=sys.stderr)
        return 2
    except BaseException:  # noqa: BLE001 - crash: log it, then still exit deterministically
        logging.getLogger("app.realtime.supervisor").exception("realtime supervisor crashed")
        rc = 1
    return finish_child(rc, "realtime")


def _age(iso: str | None) -> str:
    if not iso:
        return "-"
    from datetime import UTC, datetime  # noqa: PLC0415

    s = (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds()
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.0f}m" if s < 7200 else f"{s / 3600:.1f}h"


def cmd_status(args: argparse.Namespace) -> int:
    from app.realtime.supervisor import read_status  # noqa: PLC0415

    st = read_status()
    if args.json:
        print(json.dumps(st, indent=1, default=str))
        return 0
    if not st:
        print("realtime: never started (no data/state/realtime_status.json)")
        return 1
    print(f"realtime supervisor: {'RUNNING' if st.get('alive') else 'STOPPED'} pid={st.get('pid')} "
          f"started={st.get('started_at')} heartbeat {_age(st.get('heartbeat_at'))} ago; queue "
          f"{st.get('queue', {}).get('size')}/{st.get('queue', {}).get('max')}")
    print(f"{'stream':16} {'status':9} {'events':>7} {'1h':>6} {'lag p50':>8} {'p95':>7} {'last event':>11} {'errors':>6}  detail")
    for name, x in {**st.get("streams", {}), **st.get("consumers", {})}.items():
        print(f"{name:16} {x.get('status', ''):9} {x.get('events_total', 0):>7} {x.get('events_1h', 0):>6} "
              f"{x.get('lag_s_p50') if x.get('lag_s_p50') is not None else '-':>8} "
              f"{x.get('lag_s_p95') if x.get('lag_s_p95') is not None else '-':>7} {_age(x.get('last_event_at')):>11} "
              f"{x.get('errors', 0):>6}  {(x.get('detail') or x.get('last_error') or '')[:70]}")
    bus = st.get("bus") or {}
    print(f"bus: head_id={bus.get('head_id')} dead_letters={bus.get('dead_letters')} bytes={bus.get('bytes')}")
    for t, v in sorted((bus.get("topics") or {}).items()):
        print(f"  {t:12} {v.get('messages'):>8} msgs, {v.get('messages_1h'):>6} in 1h, last {v.get('last_ingest')}")
    for g, v in sorted((bus.get("groups") or {}).items()):
        print(f"  group {g:12} lag {v.get('lag_total')}")
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    from app.core.logsetup import setup_logging  # noqa: PLC0415
    from app.realtime.bus import get_bus  # noqa: PLC0415
    from app.realtime.compact import compact, reconcile  # noqa: PLC0415

    setup_logging(log_file="realtime.log", service="realtime")
    bus = get_bus()
    print(json.dumps({"compaction": compact(bus), "reconcile": reconcile()}, indent=1, default=str))
    return 0


def cmd_dlq(args: argparse.Namespace) -> int:
    from app.realtime.bus import get_bus  # noqa: PLC0415

    bus = get_bus()
    if args.requeue:
        print(f"requeued {bus.requeue_dead(args.requeue)}")
        return 0
    for d in bus.dead_letters(args.group, 200):
        print(f"{d['id']:>6} {d['group']:12} {d['topic']:12} {d['key'][:40]:40} attempts={d['attempts']} {d['error'][:80]}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from app.realtime.bus import get_bus  # noqa: PLC0415

    get_bus().seek(args.group, args.topic, args.from_id)
    print(f"group {args.group} on {args.topic} rewound to id {args.from_id}")
    return 0


def cmd_quotes(args: argparse.Namespace) -> int:
    from app.core.config import get_settings  # noqa: PLC0415
    from app.core.logsetup import setup_logging  # noqa: PLC0415
    from app.realtime.quotes.service import QuotesService  # noqa: PLC0415

    s = get_settings()
    s.ensure_dirs()
    setup_logging(log_file="quotes.log", service="quotes")
    params = {k: v for d in parse_params(args.param).values() for k, v in d.items()}
    svc = QuotesService(s, use_lock=not args.once, **params)
    if args.once:
        svc.refresh_universe(svc.clock(), force=True)
        rep = svc.tick()
        svc.write_status()
        print(json.dumps({"tick": rep, "universe": svc.universe.summary(),
                          "markets": {k: v.to_dict() for k, v in svc.market_states.items()}}, indent=1, default=str))
        return 0
    import logging  # noqa: PLC0415

    from app.ops.shutdown import finish_child  # noqa: PLC0415

    try:
        rc = svc.run(duration_s=args.duration, exit_deadline_s=QUOTES_EXIT_DEADLINE_S)
    except BaseException:  # noqa: BLE001
        logging.getLogger("app.realtime.quotes").exception("quotes service crashed")
        rc = 1
    return finish_child(rc, "quotes")


def parse_params(items: list[str] | None) -> dict[str, dict[str, Any]]:
    from app.realtime.supervisor import parse_kv_params  # noqa: PLC0415

    return parse_kv_params([f"q.{x}" for x in items or []])


def cmd_quotes_status(args: argparse.Namespace) -> int:
    from app.realtime.quotes.service import read_status  # noqa: PLC0415

    st = read_status()
    if args.json:
        print(json.dumps(st, indent=1, default=str))
        return 0
    if not st:
        print("quotes: never started (no data/state/quotes_status.json)")
        return 1
    print(f"quotes service: {'RUNNING' if st.get('alive') else 'STOPPED'} pid={st.get('pid')} rss={st.get('rss_mb')} MB "
          f"heartbeat {_age(st.get('heartbeat_at'))} ago; state {st.get('state')} {st.get('reason') or ''}")
    u = st.get("universe") or {}
    print(f"universe: {u.get('n')} securities {u.get('groups')} unresolved {u.get('unresolved')}")
    for name, f in (st.get("feeds") or {}).items():
        print(f"  feed {name:8} {f.get('status'):8} req/1h {f.get('requests_1h'):>4} items/1h {f.get('items_1h'):>5} "
              f"lat p50 {f.get('latency_ms_p50')} ms age p50 {f.get('quote_age_s_p50')} s errors {f.get('errors')} "
              f"{f.get('reason') or f.get('last_error') or ''}")
    for mk, m in (st.get("markets") or {}).items():
        print(f"  {mk:7} {'OPEN ' if m.get('open') else 'closed'} {m.get('reason_text'):40} n={m.get('n_targets'):>3} "
              f"next open {m.get('opens_at') or '-'}")
    return 0
