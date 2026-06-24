"""URL construction and response parsing for the shared ATS fetch layer.

No network: a captured `get` callable records the requested URL and returns a
canned JSON fixture, so each fetch_* is checked for the right URL and the right
postings-list extraction.
"""
import sys
from pathlib import Path

from alice.pipeline import ats_client


class _CapturingGet:
    """A stand-in `get` that records the URL and returns a fixed payload."""

    def __init__(self, payload):
        self.payload = payload
        self.url = None

    def __call__(self, url):
        self.url = url
        return self.payload


def test_greenhouse_url_and_parse():
    fake = _CapturingGet({"jobs": [{"id": 1, "title": "AE"}, {"id": 2, "title": "SE"}]})
    rows = ats_client.fetch_greenhouse("acme", get=fake)
    assert fake.url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    assert [j["id"] for j in rows] == [1, 2]


def test_greenhouse_missing_jobs_key():
    fake = _CapturingGet({})
    assert ats_client.fetch_greenhouse("acme", get=fake) == []


def test_ashby_url_and_parse():
    fake = _CapturingGet({"jobs": [{"id": "a", "title": "RevOps"}]})
    rows = ats_client.fetch_ashby("widgets", get=fake)
    assert fake.url == "https://api.ashbyhq.com/posting-api/job-board/widgets?includeCompensation=true"
    assert rows == [{"id": "a", "title": "RevOps"}]


def test_ashby_missing_jobs_key():
    fake = _CapturingGet({})
    assert ats_client.fetch_ashby("widgets", get=fake) == []


def test_lever_url_and_parse():
    # Lever returns a bare top-level list, not a {"jobs": [...]} object.
    fake = _CapturingGet([{"id": "x", "text": "Founding AE"}])
    rows = ats_client.fetch_lever("startup", get=fake)
    assert fake.url == "https://api.lever.co/v0/postings/startup?mode=json"
    assert rows == [{"id": "x", "text": "Founding AE"}]
