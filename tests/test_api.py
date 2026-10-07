from __future__ import annotations

from fastapi.testclient import TestClient

from synopixtri import config
from synopixtri.api import create_app

from conftest import photo, ts


def client(env, password=None):
    boot = config.Bootstrap(photos_root=env.root, data_dir=env.boot.data_dir, password=password)
    return TestClient(create_app(boot, start_scheduler=False))


def test_status_settings_and_page(env):
    with client(env) as c:
        assert "SynoPixtri" in c.get("/").text
        status = c.get("/api/status").json()
        assert status["inbox_files"] == 0 and status["to_name"] == []
        settings = c.get("/api/settings").json()
        assert settings["scan_interval_min"] == 30
        updated = c.put("/api/settings", json={"scan_interval_min": 45, "active_hours": ["08:00-22:00"]})
        assert updated.status_code == 200 and updated.json()["scan_interval_min"] == 45


def test_settings_validation(env):
    with client(env) as c:
        assert c.put("/api/settings", json={"nope": 1}).status_code == 422
        assert c.put("/api/settings", json={"active_hours": ["25:99"]}).status_code == 422
        assert c.put("/api/settings", json={"inbox_dir": "../outside"}).status_code == 422
        assert c.put("/api/settings", json={"orphan_live_policy": "bogus"}).status_code == 422
        assert c.put("/api/settings", json={"library_dir": "inbox/sub"}).status_code == 422


def test_password_protects_the_api(env):
    with client(env, password="s3cret") as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", auth=("any", "wrong")).status_code == 401
        assert c.get("/api/status", auth=("any", "s3cret")).status_code == 200


def test_jobs_operations_undo_via_api(env):
    env.add("a.JPG", photo(2026, 3, 1))
    job = env.settle(ts(2026, 3, 20))
    with client(env) as c:
        jobs = c.get("/api/jobs").json()
        assert jobs[0]["id"] == job["id"] and jobs[0]["state"] == "done"
        ops = c.get(f"/api/jobs/{job['id']}/operations").json()
        assert any(o["op"] == "move" and o["state"] == "done" for o in ops)
        assert c.post(f"/api/jobs/{job['id']}/undo").json()["state"] == "undone"
        assert c.post("/api/jobs/9999/undo").status_code == 404
        assert c.post(f"/api/jobs/{job['id']}/undo").status_code == 409  # already undone


def test_active_hours_windows():
    from datetime import datetime

    day = datetime(2026, 3, 20, 14, 0)
    assert config.in_active_hours([], day)
    assert config.in_active_hours(["08:00-18:00"], day)
    assert not config.in_active_hours(["20:00-23:00"], day)
    assert config.in_active_hours(["22:00-06:00"], datetime(2026, 3, 20, 2, 0))  # crosses midnight
