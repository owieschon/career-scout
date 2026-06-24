"""Shared fetch layer for the public job-board APIs (Greenhouse, Ashby, Lever).

Each `fetch_*` builds the board URL for a slug, performs a JSON GET, and returns
the raw postings list exactly as the API delivers it: the `jobs` array for
Greenhouse and Ashby, and the top-level list for Lever. Callers keep their own
downstream normalization, filtering, and scoring; only the HTTP fetch and the
postings-list extraction are shared here.

A caller may pass its own `get` callable (one that takes a URL and returns the
parsed JSON) to preserve its timeout, decoding, and error-recording behavior.
When omitted, a stdlib default is used.
"""
import json
import ssl
from urllib.request import urlopen, Request

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

USER_AGENT = "job-search-sourcer/2.0 (+personal use)"

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"


def get_json(url, timeout=25):
    """Fetch a URL and return parsed JSON using stdlib urllib."""
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=timeout, context=_SSL) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw)


def fetch_greenhouse(slug, get=None):
    """Return the raw Greenhouse postings list for a board slug."""
    get = get or get_json
    data = get(GREENHOUSE_URL.format(slug=slug))
    return data.get("jobs", [])


def fetch_ashby(slug, get=None):
    """Return the raw Ashby postings list for a board slug."""
    get = get or get_json
    data = get(ASHBY_URL.format(slug=slug))
    return data.get("jobs", [])


def fetch_lever(slug, get=None):
    """Return the raw Lever postings list for a board slug."""
    get = get or get_json
    return get(LEVER_URL.format(slug=slug))
