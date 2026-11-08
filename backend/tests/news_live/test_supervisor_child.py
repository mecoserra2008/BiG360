"""Supervisor 4th child `realtime` (live news): toggle flag, RAM governor; tray toggle + news alert toasts; realtime
supervisor STOP file and status metrics."""

from __future__ import annotations

import asyncio
import time

from app.ops import supervisor as SUP


def test_live_news_child_toggle_and_ram_governor(tmp_data, monkeypatch):
    sup = SUP.Supervisor(tmp_data, api=False, worker=True, filler=False, realtime=True, live_news=True)
    names = [c.name for c in sup.children]
    assert names == ["worker", "realtime"]
    assert "--realtime" not in sup.children[0].command          # the child owns the realtime layer
    rt = sup.children[1]
    assert rt.command[-3:] == ["app.cli", "realtime", "start"] and rt.ram_cap_mb == SUP.LIVE_NEWS_RAM_CAP_MB
    started, stopped = [], []
    monkeypatch.setattr(sup, "start_child", lambda c: (started.append(c.name), setattr(c, "state", "running")))
    monkeypatch.setattr(sup, "stop_child", lambda c, restart=False: stopped.append((c.name, restart)))
    SUP.set_live_news(False, tmp_data)
    assert not SUP.live_news_enabled(tmp_data)
    rt.state = "stopped"
    sup.check_child(rt)
    assert rt.state == "disabled" and started == []
    SUP.set_live_news(True, tmp_data)
    sup.check_child(rt)
    assert started == ["realtime"]
    # RAM governor: 3 samples above the cap -> graceful restart
    monkeypatch.setattr(SUP.winsys, "tree_memory_mb", lambda pid: 9999.0)

    class P:
        pid = 4242

        def poll(self):
            return None

    rt.popen, rt.state, rt.started_at = P(), "running", time.monotonic() - SUP.RAM_GRACE_S - 1
    for _ in range(SUP.RAM_STRIKES - 1):
        sup.check_child(rt)
    assert stopped == []
    sup.check_child(rt)
    assert stopped == [("realtime", True)] and rt.history[0]["exit"] == "ram_cap"
    st = sup.status()["children"]["realtime"]
    assert st["ram_cap_mb"] == SUP.LIVE_NEWS_RAM_CAP_MB and st["enabled"] is True


def test_tray_live_news_toggle_and_news_toasts(tmp_data, monkeypatch):
    from app.desktop import autostart
    from app.desktop import tray_actions as TA

    monkeypatch.setattr(autostart, "is_enabled", lambda *a, **k: False)

    class Api:
        alerts = {"items": []}

        def get(self, path):
            if path.startswith("/api/v1/live/alerts"):
                return self.alerts
            if path.endswith("/system/status"):
                return {"data_as_of_overall": "2026-10-20", "scheduler": {}, "ops_alerts": []}
            return {"active": "weekly", "profiles": []}

    notes = []
    api = Api()
    cfg = {"api_host": "127.0.0.1", "api_port": 8095, "serve_ui": True, "filler": False, "realtime": False,
           "start_supervisor": True, "status_poll_s": 60, "notifications": True, "notify_severities": ["error"],
           "update_dir": "", "update_check_h": 24, "live_news": True}
    ctl = TA.TrayController(tmp_data, api=api, open_url=lambda u: None, notify=lambda *n: notes.append(n), cfg=cfg)
    ids = [m.id for m in ctl.menu()]
    assert "livenews" in ids and "news" in ids
    assert next(m for m in ctl.menu() if m.id == "livenews").checked
    assert "--live-news" in ctl.supervisor_command()
    assert "off" in ctl.dispatch("livenews") and not ctl.live_news_on()
    assert "Live news: off" in ctl.status_lines()
    assert "on" in ctl.dispatch("livenews") and ctl.live_news_on()
    ctl.poll()                                      # first poll: remembers what is there, no toast
    api.alerts = {"items": [{"event_id": "e1", "rule_type": "watchlist_news", "message": "news: Acme recalls", "read": False},
                            {"event_id": "e2", "rule_type": "new_filing", "message": "8-K", "read": False}]}
    ctl.poll()
    assert [n[0] for n in notes] == ["BiG360 · News"] and "Acme" in notes[0][1]
    ctl.poll()
    assert len(notes) == 1                          # once per event


def test_realtime_supervisor_stop_file_and_metrics(tmp_data):
    from app.realtime.bus import SqliteBus
    from app.realtime.streams.base import StreamWorker
    from app.realtime.supervisor import Supervisor, read_status, stop_file

    class W(StreamWorker):
        name = "w"

        async def poll(self, ctx):
            return []

        def metrics(self):
            return {"ok": 1}

    bus = SqliteBus(tmp_data.data_dir / "realtime" / "bus.sqlite")
    sup = Supervisor(tmp_data, bus=bus, workers=[W(interval_s=0.2)], status_every_s=0.2, use_lock=False, grace_s=2)

    async def go():
        task = asyncio.create_task(sup.main())
        await asyncio.sleep(0.6)
        stop_file(tmp_data).write_text("x")
        await asyncio.wait_for(task, timeout=20)

    asyncio.run(go())
    st = read_status(tmp_data)
    assert st["metrics"]["w"] == {"ok": 1} and "rss_mb" in st and not stop_file(tmp_data).exists()
    bus.close()
