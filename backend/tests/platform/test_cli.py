"""CLI smoke tests (argparse entry point, no network)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.cli import main
from app.core import config as config_mod

BACKEND = Path(__file__).resolve().parents[2]


def test_jobs_plan_nightly_dry_run(capsys, tmp_data):
    assert main(["jobs", "plan", "nightly", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "PLAN nightly" in out and "critical path" in out and "alerts_eval" in out and "dry run" in out


def test_jobs_plan_json_and_list(capsys, tmp_data):
    assert main(["jobs", "plan", "weekly", "--json", "--universe", "full"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["universe"] == "full" and plan["n_jobs"] > 0
    assert main(["jobs", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert any(r["job_id"] == "dq_checks" and r["available"] for r in rows)


def test_jobs_run_dry_run_and_status(capsys, tmp_data):
    assert main(["jobs", "run", "sec_xbrl_facts", "--with-upstream", "--dry-run", "--tickers", "AAPL,MSFT"]) == 0
    out = capsys.readouterr().out
    assert "sec_filings_index" in out and '"tickers": ["AAPL", "MSFT"]' in out
    assert main(["jobs", "status"]) == 0
    assert "no job runs" in capsys.readouterr().out


def test_jobs_run_real_platform_job_and_status(capsys, tmp_data):
    assert main(["jobs", "run", "alerts_eval", "--as-of", "2026-10-09"]) == 0
    out = capsys.readouterr().out
    assert "success" in out
    assert main(["jobs", "status", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["job_id"] == "alerts_eval" and rows[0]["status"] == "success"


def test_enqueue_and_queue(capsys, tmp_data):
    assert main(["jobs", "enqueue", "dq_checks", "--param", "tables=[\"fx_rates\"]"]) == 0
    assert "queued dq_checks" in capsys.readouterr().out
    assert main(["jobs", "queue", "--json"]) == 0
    q = json.loads(capsys.readouterr().out)
    assert len(q["pending"]) == 1 and q["pending"][0]["params"] == {"tables": ["fx_rates"]}
    assert main(["jobs", "enqueue", "nope"]) == 1


def test_dq_run_no_write(capsys, tmp_data):
    assert main(["dq", "run", "--tables", "fx_rates", "--no-write"]) == 0
    assert "tables without data" in capsys.readouterr().out
    assert main(["dq", "summary", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] is None


def test_doctor_offline_never_prints_key_values(capsys, tmp_data, monkeypatch):
    secret = "ZZTOPSECRETKEY123"
    monkeypatch.setenv("FRED_APIKEY", secret)
    config_mod.reset_settings_cache()
    rc = main(["doctor", "--offline"])
    out = capsys.readouterr().out
    assert secret not in out
    assert "FRED_APIKEY: PRESENT" in out
    assert "network" not in out
    assert rc in (0, 1)


def test_module_entry_point_subprocess(tmp_path):
    env = {**__import__("os").environ, "BIG360_DATA_DIR": str(tmp_path / "data")}
    r = subprocess.run([sys.executable, "-m", "app.cli", "jobs", "plan", "nightly", "--dry-run"], cwd=BACKEND,
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "critical path" in r.stdout
