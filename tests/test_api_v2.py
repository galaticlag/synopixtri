from __future__ import annotations

import io

from fastapi.testclient import TestClient

from synopixtri import config, notify
from synopixtri.api import create_app

from conftest import photo, ts

NOW = ts(2026, 3, 20)
OFFICE = {"lat": 48.8584, "lon": 2.2945, "radius_m": 300}


def client(env, password=None):
    boot = config.Bootstrap(photos_root=env.root, data_dir=env.boot.data_dir, password=password)
    return TestClient(create_app(boot, start_scheduler=False))


def test_zone_crud_and_validation(env):
    with client(env) as c:
        zone = c.post("/api/zones", json={"name": "Bureau", "type": "work", "geometry": OFFICE, "weekdays": [0, 1]}).json()
        assert zone["id"] and c.get("/api/zones").json()[0]["name"] == "Bureau"
        assert c.put(f"/api/zones/{zone['id']}", json={**zone, "name": "Atelier"}).json()["name"] == "Atelier"
        assert c.post("/api/zones", json={"name": "x", "type": "work", "geometry": {"lat": 1}}).status_code == 422
        assert c.delete(f"/api/zones/{zone['id']}").status_code == 200
        assert c.delete(f"/api/zones/{zone['id']}").status_code == 404


def test_rules_crud_preview_templates_and_versions(env):
    env.add("Screenshot_1.PNG", photo(2026, 3, 1))
    env.run(NOW)
    with client(env) as c:
        templates = c.get("/api/rules/templates").json()
        assert {t["id"] for t in templates} >= {"screenshots", "sidecars", "blurry"}
        body = next(t for t in templates if t["id"] == "messaging")
        rule = c.post("/api/rules", json=body).json()
        assert c.get("/api/rules").json()[0]["description"]
        preview = c.post("/api/rules/preview", json={"conditions": [{"type": "filename", "patterns": ["screenshot*"]}]}).json()
        assert preview["count"] == 1 and preview["total"] == 1
        assert c.post("/api/rules/preview", json={"conditions": [{"type": "nope"}]}).status_code == 422
        assert c.post("/api/rules", json={"name": "bad", "conditions": [], "action": "aside"}).status_code == 422
        c.put(f"/api/rules/{rule['id']}", json={**rule, "enabled": False})
        versions = c.get("/api/rules/versions").json()
        assert len(versions) == 2
        oldest = versions[-1]["id"]
        assert c.post(f"/api/rules/versions/{oldest}/restore").status_code == 200
        assert c.get("/api/rules").json()[0]["enabled"] is True
        assert c.post("/api/rules/reorder", json={"ids": [rule["id"]]}).status_code == 200
        assert c.delete(f"/api/rules/{rule['id']}").status_code == 200


def test_naming_preview(env):
    with client(env) as c:
        ok = c.post("/api/naming/preview", json={"event_template": "{range} - {label} ({place})"}).json()
        assert ok["examples"]["event"] == "2026.03.14 - Anniversaire (Lyon)" and ok["recognised"]
        assert c.post("/api/naming/preview", json={"event_template": "{label}"}).status_code == 422


def test_settings_hide_the_smtp_password(env):
    with client(env) as c:
        c.put("/api/settings", json={"smtp_password": "hunter2", "notify_kind": "email"})
        assert c.get("/api/settings").json()["smtp_password"] == "********"
        c.put("/api/settings", json={"smtp_password": "********", "smtp_host": "mail.example.org"})
    conn = env.conn()
    assert config.load(conn)["smtp_password"] == "hunter2"  # the mask never overwrites the real value
    conn.close()


def test_proposals_and_approvals_flow(env):
    env.settings(validation_mode=True)
    for i in range(4):
        env.add(f"E{i}.JPG", photo(2026, 3, 14, 10 + i))
    env.settle(NOW)
    with client(env) as c:
        assert c.get("/api/status").json()["proposals"] == 1
        proposal = c.get("/api/proposals").json()[0]
        assert proposal["count"] == 4 and len(proposal["samples"]) == 4
        saved = c.post("/api/approvals", json={"items": [{"start": "2026-03-14", "end": "2026-03-14", "label": "Mariage", "place": "Lyon"}]})
        assert saved.json() == {"saved": 1}
        assert c.post("/api/approvals", json={"items": [{"start": "2026-03-16", "end": "2026-03-14"}]}).status_code == 422
        # a newer decision on the same days replaces the old one
        c.post("/api/approvals", json={"items": [{"start": "2026-03-14", "end": "2026-03-14", "label": "Noces"}]})
        assert [a["label"] for a in c.get("/api/approvals").json()] == ["Noces"]
    env.run(NOW + 60)
    assert any(p.name.startswith("2026.03.14 Noces") for p in (env.library / "2026").iterdir())


def test_aside_screen_restore_and_lock(env):
    with client(env) as c:
        c.post("/api/rules", json={"name": "png", "conditions": [{"type": "extension", "values": ["png"]}], "action": "aside"})
        env.add("a.PNG", photo(2026, 3, 1))
        env.settle(NOW)
        items = c.get("/api/aside").json()
        assert len(items) == 1 and "Règle « png »" in items[0]["why"]
        assert c.post("/api/aside/restore", json={"ids": [items[0]["id"]]}).json()["restored"] == 1
        assert c.get("/api/aside").json() == [] and (env.inbox / "a.PNG").exists()
        assert c.post("/api/aside/restore", json={"ids": []}).status_code == 422
        env.add("b.JPG", photo(2026, 3, 1))
        env.settle(NOW + 3 * 24 * 3600)
        folder = c.get("/api/folders").json()[0]
        assert c.put(f"/api/folders/{folder['id']}", json={"locked": True}).json()["status"] == "verrouille"
        assert c.put(f"/api/folders/{folder['id']}", json={"locked": False}).json()["status"] == "utilisateur"
        assert c.put("/api/folders/999", json={"locked": True}).status_code == 404


def test_timeline_and_exact_duplicates_screens(env):
    env.add("a.JPG", photo(2026, 3, 1), content=b"same")
    env.add("b.JPG", photo(2026, 3, 1, 11), content=b"same")
    env.settle(NOW)
    with client(env) as c:
        months = c.get("/api/timeline").json()
        assert months[0]["month"] == "2026-03" and months[0]["count"] == 1
        dup = c.get("/api/duplicates").json()
        assert len(dup["exact"]) == 1 and dup["similar"] == [] and dup["analysis_on"] is False


def test_thumbnail_is_served_only_inside_the_photos_root(env):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (10, 90, 160)).save(buf, "JPEG")
    path = env.add("a.JPG", photo(2026, 3, 1), content=buf.getvalue())
    outside = env.root.parent / "secret.jpg"
    outside.write_bytes(buf.getvalue())
    with client(env) as c:
        ok = c.get("/api/thumb", params={"path": str(path)})
        assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
        assert c.get("/api/thumb", params={"path": str(outside)}).status_code == 404
        assert c.get("/api/thumb", params={"path": str(env.root / ".." / "secret.jpg")}).status_code == 404
        env.add("b.JPG", photo(2026, 3, 1), content=b"not an image")
        assert c.get("/api/thumb", params={"path": str(env.inbox / "b.JPG")}).status_code == 415


# -- "to name" reminder ---------------------------------------------------------------------------
def make_event(env):
    for i in range(4):
        env.add(f"E{i}.JPG", photo(2026, 3, 14, 10 + i))
    env.settle(NOW)


def test_reminder_is_sent_after_the_delay_and_not_repeated(env, monkeypatch):
    sent = []
    monkeypatch.setitem(notify.SENDERS, "ntfy", lambda cfg, title, body, items: sent.append((title, body, len(items))))
    env.settings(notify_kind="ntfy", notify_url="http://127.0.0.1:9/x", notify_after_days=3, notify_every_days=7)
    make_event(env)
    conn = env.conn()
    cfg = config.namespace(config.load(conn))
    assert notify.check(conn, cfg, NOW + 3600)["reason"] == "nothing_to_name"  # too fresh
    assert notify.check(conn, cfg, NOW + 4 * 86400)["sent"] is True
    assert "2026.03.14 À nommer" in sent[0][1]
    assert notify.check(conn, cfg, NOW + 5 * 86400)["reason"] == "too_soon"
    assert notify.check(conn, cfg, NOW + 12 * 86400)["sent"] is True
    conn.close()


def test_reminder_failure_is_reported_not_raised(env, monkeypatch):
    def boom(cfg, title, body, items):
        raise OSError("network down")

    monkeypatch.setitem(notify.SENDERS, "webhook", boom)
    env.settings(notify_kind="webhook", notify_url="http://127.0.0.1:9/x", notify_after_days=0)
    make_event(env)
    conn = env.conn()
    result = notify.check(conn, config.namespace(config.load(conn)), NOW + 86400)
    assert result["sent"] is False and "network down" in result["error"]
    conn.close()


def test_reminder_disabled_by_default_and_test_endpoint(env, monkeypatch):
    with client(env) as c:
        assert c.post("/api/notify/test").json()["reason"] == "disabled"
        sent = []
        monkeypatch.setitem(notify.SENDERS, "ntfy", lambda cfg, title, body, items: sent.append(title))
        c.put("/api/settings", json={"notify_kind": "ntfy", "notify_url": "http://127.0.0.1:9/x"})
        assert c.post("/api/notify/test").json()["sent"] is True and sent == ["SynoPixtri: test"]


def test_a_newer_pass_replaces_an_older_one_waiting_at_the_brake(env):
    env.settings(brake_max_files=2)
    for i in range(4):
        env.add(f"E{i}.JPG", photo(2026, 3, 14, 10 + i))
    env.settle(NOW)
    env.run(NOW + 60)
    env.run(NOW + 120)
    with client(env) as c:
        status = c.get("/api/status").json()
        assert len(status["paused_jobs"]) == 1 and status["paused_planned"] >= 3
        states = sorted(j["state"] for j in c.get("/api/jobs").json())
        assert states.count("paused_brake") == 1 and "superseded" in states
