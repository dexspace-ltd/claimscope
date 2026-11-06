import json

import pytest

from claimscope import api, store


class FakeStore:
    def __init__(self):
        self.items = {}

    def put(self, item):
        self.items[(item["PK"], item["SK"])] = dict(item)

    def get_meta(self, assessment_id):
        return self.items.get((store.asmt_pk(assessment_id), "META"))

    def query_assessment(self, assessment_id):
        pk = store.asmt_pk(assessment_id)
        return [v for (p, _), v in sorted(self.items.items()) if p == pk]


class ConditionFailed(Exception):
    pass


class FakeTable:
    """Just enough of update_item for the counters: ADD n/#c with an upper-limit condition."""

    meta = type("M", (), {"client": type("C", (), {"exceptions": type("E", (), {"ConditionalCheckFailedException": ConditionFailed})})})

    def __init__(self):
        self.counters = {}

    def update_item(self, Key, ExpressionAttributeValues, **kwargs):
        key = (Key["PK"], Key["SK"], kwargs.get("ExpressionAttributeNames", {}).get("#c", "n"))
        if self.counters.get(key, 0) >= ExpressionAttributeValues[":limit"]:
            raise ConditionFailed()
        self.counters[key] = self.counters.get(key, 0) + 1


@pytest.fixture
def fake(monkeypatch):
    fake = FakeStore()
    for name in ("put", "get_meta", "query_assessment"):
        monkeypatch.setattr(store, name, getattr(fake, name))
    table = FakeTable()
    monkeypatch.setattr(store, "table", lambda: table)
    monkeypatch.delenv("ORIGIN_VERIFY", raising=False)
    monkeypatch.delenv("RATE_LIMIT_EXEMPT_IPS", raising=False)
    return fake


def call(method, path, body=None, ip="203.0.113.7", headers=None):
    event = {"requestContext": {"http": {"method": method, "sourceIp": "198.51.100.1"}}, "rawPath": path,
             "headers": {"cloudfront-viewer-address": f"{ip}:51234", **(headers or {})},
             "body": json.dumps(body) if body is not None else None}
    res = api.handler(event, None)
    return res["statusCode"], json.loads(res["body"])


def test_create_room_and_read_back(fake):
    status, asmt = call("POST", "/api/assessments", {"claim_ref": "DEMO-1", "peril": "escape_of_water"})
    assert status == 201 and asmt["status"] == "created"

    status, room = call("POST", f"/api/assessments/{asmt['assessment_id']}/rooms",
                        {"name": "Lounge", "dimensions": {"length_m": 5, "breadth_m": 4, "height_m": 2.4}})
    assert status == 200
    assert room["derived"]["floor_area"] == 20

    status, full = call("GET", f"/api/assessments/{asmt['assessment_id']}")
    assert status == 200 and [r["name"] for r in full["rooms"]] == ["Lounge"]


def test_validation_errors(fake, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_EXEMPT_IPS", "203.0.113.7")
    assert call("POST", "/api/assessments", {"peril": "alien_invasion"})[0] == 400
    _, asmt = call("POST", "/api/assessments", {"peril": "flood"})
    path = f"/api/assessments/{asmt['assessment_id']}/rooms"
    assert call("POST", path, {"name": "Hall", "dimensions": {"length_m": 2}})[0] == 400
    assert call("POST", path, {"name": "Hall", "dimensions": {
        "length_m": 2, "breadth_m": 1, "height_m": 2.4, "openings": [{"kind": "hatch", "width_m": 1, "height_m": 1}]}})[0] == 400


def test_unknown_routes(fake):
    assert call("GET", "/api/nope")[0] == 404
    assert call("GET", "/api/assessments/0123456789ab")[0] == 404


def test_set_status_aliases_reserved_words(monkeypatch):
    calls = []

    class FakeTable:
        meta = type("M", (), {"client": type("C", (), {"exceptions": type("E", (), {"ConditionalCheckFailedException": KeyError})})})

        def update_item(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(store, "table", lambda: FakeTable())
    assert store.set_status("0123456789ab", "failed", error="boom", progress="x")
    expr = calls[0]["UpdateExpression"]
    assert " error " not in f" {expr} " and "#e = :e" in expr and calls[0]["ExpressionAttributeNames"]["#e"] == "error"


def test_two_new_assessments_per_ip_per_day(fake, monkeypatch):
    new = lambda ip: call("POST", "/api/assessments", {"peril": "flood"}, ip=ip)[0]
    assert [new("203.0.113.9"), new("203.0.113.9"), new("203.0.113.9")] == [201, 201, 429]
    assert new("2001:db8::1") == 201  # a different address has its own allowance
    monkeypatch.setenv("RATE_LIMIT_EXEMPT_IPS", "203.0.113.9")
    assert new("203.0.113.9") == 201


def test_exempt_networks(fake, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_EXEMPT_IPS", "203.0.113.9, 2001:db8:1:2::/64")
    assert api.is_exempt("203.0.113.9") and not api.is_exempt("203.0.113.10")
    assert api.is_exempt("2001:db8:1:2:aaaa:bbbb:cccc:dddd") and not api.is_exempt("2001:db8:1:3::1")
    assert not api.is_exempt("not-an-ip")


def test_requests_must_come_through_cloudfront(fake, monkeypatch):
    monkeypatch.setenv("ORIGIN_VERIFY", "s3cret")
    assert call("GET", "/api/nope")[0] == 403
    assert call("GET", "/api/nope", headers={"x-origin-verify": "s3cret"})[0] == 404


def test_matterport_link_parsing(fake, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_EXEMPT_IPS", "203.0.113.7")
    ok = "https://my.matterport.com/show/?m=SxQL3iGyoDo&play=1"
    status, a = call("POST", "/api/assessments", {"peril": "flood", "matterport_url": ok})
    assert status == 201 and a["matterport_model"] == "SxQL3iGyoDo"
    for link in ("https://discover.matterport.com/space/aLnysz475kH", "https://my.matterport.com/models/aLnysz475kH",
                 "aLnysz475kH", "https://my.matterport.com/show?m=aLnysz475kH"):
        assert call("POST", "/api/assessments", {"peril": "flood", "matterport_url": link})[1]["matterport_model"] == "aLnysz475kH"
    for bad in ("https://evil.example/?m=SxQL3iGyoDo", "https://discover.matterport.com.evil.example/space/aLnysz475kH"):
        assert call("POST", "/api/assessments", {"peril": "flood", "matterport_url": bad})[0] == 400


def test_incident_brief_validation():
    assert api._incident({"cause": "  Cistern leak in the flat above  ", "date_noticed": "2026-05-01", "junk": "x"}) == {
        "cause": "Cistern leak in the flat above", "date_noticed": "2026-05-01"}
    with pytest.raises(api.HttpError):
        api._incident({"date_of_incident": "01/05/2026"})
