"""Supervisor app mode: --serve-ui wiring of the API child, --no-schedule, credential env for children (never in status)."""

from __future__ import annotations

import json

from app.ops import supervisor as SUP


class FakeProc:
    pid = 777

    def poll(self):
        return None


def test_app_mode_children_and_env(tmp_data, tmp_path, monkeypatch):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html></html>")
    monkeypatch.setenv("FRED_APIKEY", "explicit-wins")
    sup = SUP.Supervisor(tmp_data, api_port=8095, filler=False, serve_ui=True, ui_dir=ui, schedule=False,
                         extra_env={"EIA_APIKEY": "pytest-secret-1", "FRED_APIKEY": "pytest-secret-2"}, app_mode=True)
    api, worker = sup.children
    assert api.env == {"BIG360_SERVE_UI": "1", "BIG360_UI_DIR": str(ui)}
    assert "--no-schedule" in worker.command
    captured = {}

    def fake_popen(cmd, **kw):
        captured[cmd[2]] = kw["env"]
        return FakeProc()

    monkeypatch.setattr(SUP.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(SUP, "port_open", lambda *a, **k: False)
    for c in sup.children:
        sup.start_child(c)
    env_api, env_worker = captured["app.cli"], captured["app.scheduler.worker"]
    assert env_api["BIG360_SERVE_UI"] == "1" and "BIG360_SERVE_UI" not in env_worker
    assert env_api["EIA_APIKEY"] == "pytest-secret-1" == env_worker["EIA_APIKEY"]
    assert env_api["FRED_APIKEY"] == "explicit-wins"                  # an explicit variable beats the store
    st = sup.status()
    assert st["app_mode"] is True and st["ui"] == "http://127.0.0.1:8095/" and st["credentials_loaded"] == 2
    assert "pytest-secret" not in json.dumps(st)


def test_no_ui_build_means_api_only(tmp_data, monkeypatch):
    monkeypatch.setattr("app.api.static_ui.resolve_ui_dir", lambda s=None: None)
    sup = SUP.Supervisor(tmp_data, worker=False, filler=False, serve_ui=True)
    assert sup.ui_dir is None and sup.children[0].env == {}
    sup2 = SUP.Supervisor(tmp_data, worker=False, filler=False)        # dev default: no UI serving
    assert sup2.children[0].env == {} and sup2.status()["ui"] is None
