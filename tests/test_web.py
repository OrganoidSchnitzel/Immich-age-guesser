import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from immich_age_guesser.web.app import create_app


@pytest.fixture
def client(guesser):
    with TestClient(create_app(guesser), headers={"X-Age-Guesser": "1"}) as c:
        yield c


def wait(client, job):
    for _ in range(200):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] in {"done", "failed"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.fixture
def batch(fake):
    anna = fake.add_person("Anna", date(1980, 3, 1))
    oma = fake.add_person("Oma", None)
    ids = [
        fake.add_asset("b.jpg", date(1986, 8, 1), [anna], album="scans"),
        fake.add_asset("a.jpg", date(1986, 8, 1), [anna, oma], album="scans"),
        fake.add_asset("c.jpg", date(1975, 1, 1), [], album="scans"),
    ]
    return {"anna": anna, "oma": oma, "ids": ids}


def test_pages_render(client, batch):
    r = client.get("/")
    assert r.status_code == 200 and "Album scans" in r.text and "not calibrated yet" in r.text
    assert client.get("/album/scans").status_code == 200
    assert client.get("/people").status_code == 200
    assert client.get("/album/missing").status_code == 404
    assert client.get(f"/thumb/{batch['ids'][0]}").headers["content-type"] == "image/png"


def test_album_assets_sorted_with_people(client, batch):
    data = client.get("/api/albums/scans/assets").json()
    assert [a["file_name"] for a in data["assets"]] == ["a.jpg", "b.jpg", "c.jpg"]
    people = {p["name"]: p for p in data["people"]}
    assert people["Anna"]["photos"] == 2 and people["Oma"]["birth_date"] is None


def test_parse_date(client):
    assert client.get("/api/parse-date", params={"text": "06.1987"}).json()["written"] == "1987-06-15"
    assert client.get("/api/parse-date", params={"text": "soon"}).json()["ok"] is False


def test_known_date_flow(client, batch, fake):
    job = client.post("/api/known-date", json={"asset_ids": batch["ids"][:2], "date": "1986"}).json()
    assert wait(client, job)["status"] == "done"
    assets = {a["id"]: a for a in client.get("/api/albums/scans/assets").json()["assets"]}
    assert assets[batch["ids"][0]]["dated"]["label"] == "1986"
    assert assets[batch["ids"][0]]["date"].startswith("1986-07-02")
    assert client.post("/api/known-date", json={"asset_ids": batch["ids"], "date": "nope"}).status_code == 400


def test_estimate_and_apply_flow(client, batch, fake):
    job = client.post("/api/estimate", json={"asset_ids": batch["ids"], "not_after": "1990"}).json()
    job = wait(client, job)
    assert job["status"] == "done"
    assets = {a["id"]: a for a in client.get("/api/albums/scans/assets").json()["assets"]}
    first = assets[batch["ids"][1]]["estimate"]
    assert first["status"] == "ok" and "Oma has no birthday" in first["notes"]
    assert assets[batch["ids"][2]]["estimate"]["status"] == "no_estimate"

    job = wait(client, client.post("/api/apply-estimates", json={"asset_ids": batch["ids"]}).json())
    assert job["result"] == {"written": 2, "skipped": 1}
    assets = {a["id"]: a for a in client.get("/api/albums/scans/assets").json()["assets"]}
    assert assets[batch["ids"][0]]["dated"]["source"] == "estimate"


def test_discard(client, batch):
    wait(client, client.post("/api/estimate", json={"asset_ids": batch["ids"][:1]}).json())
    client.post("/api/discard-estimates", json={"asset_ids": batch["ids"][:1]})
    assert client.get("/api/albums/scans/assets").json()["assets"][1]["estimate"] is None


def test_birthdays(client, batch, fake):
    people = client.get("/api/people").json()
    assert [p["name"] for p in people] == ["Anna", "Oma"]
    r = client.put(f"/api/people/{batch['oma']}/birthdate", json={"birth_date": "1930-02-01"})
    assert r.json()["birth_date"] == "1930-02-01" and fake.people[batch["oma"]]["birthDate"] == "1930-02-01"
    assert client.put(f"/api/people/{batch['oma']}/birthdate", json={"birth_date": "2999-01-01"}).status_code == 400


def test_failed_job_reports_error(client, batch, guesser):
    def boom(*a, **k):
        raise RuntimeError("model exploded")
    guesser.estimate = boom
    job = wait(client, client.post("/api/estimate", json={"asset_ids": batch["ids"]}).json())
    assert job["status"] == "failed" and "model exploded" in job["error"]


def test_write_and_remove_from_album(client, batch, fake):
    ids = batch["ids"]
    job = client.post("/api/known-date", json={"asset_ids": ids[:2], "date": "1986", "album_id": "scans",
                                               "remove_from_album": True}).json()
    result = wait(client, job)["result"]
    assert result["written"] == 2 and result["removed"] == 2
    assert fake.albums["scans"]["assets"] == [ids[2]]
    assert fake.assets[ids[0]].exif_date.startswith("1986")  # the photo itself is untouched otherwise


def test_estimates_removed_only_when_written(client, batch, fake):
    ids = batch["ids"]
    wait(client, client.post("/api/estimate", json={"asset_ids": ids}).json())
    result = wait(client, client.post("/api/apply-estimates", json={
        "asset_ids": ids, "album_id": "scans", "remove_from_album": True}).json())["result"]
    assert result == {"written": 2, "skipped": 1, "removed": 2}
    assert fake.albums["scans"]["assets"] == [ids[2]]  # the photo without a suggestion stays


def test_no_removal_by_default(client, batch, fake):
    job = client.post("/api/known-date", json={"asset_ids": batch["ids"], "date": "1986", "album_id": "scans"})
    assert "removed" not in wait(client, job.json())["result"]
    assert len(fake.albums["scans"]["assets"]) == 3


def test_state_changes_need_the_csrf_header(guesser, batch):
    with TestClient(create_app(guesser)) as c:
        r = c.post("/api/known-date", json={"asset_ids": batch["ids"], "date": "1986"})
        assert r.status_code == 403
        assert c.get("/api/albums/scans/assets").status_code == 200


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True, "configured": True}
