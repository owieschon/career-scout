"""Offline unit tests for _remote_us_ok body extension.

Covers both directions:

  1. RESCUE: genuinely-remote-in-body role (location = city / "Eastern Time Zone",
     body says "fully remote, US") PASSES _remote_us_ok.

  2. ONSITE-WITH-BOILERPLATE: onsite/hybrid role whose body contains remote
     BOILERPLATE ("remote-first culture" but "onsite 3x/week") is still DROPPED.
     This is the catch that proves the false-positive guard works.

  3. CLEARLY-ONSITE: a role with no remote language anywhere is DROPPED.

  4. TITLE-PASSES: a role with "Remote" in the title/location passes without
     needing a body.

  5. HUB-CITY without remote stays DROPPED even with body boilerplate.

  6. BODY-ONLY-BOILERPLATE is NOT rescued ("remote-first culture" alone).

  7. STRONG-SIGNAL-BUT-HYBRID-BODY: body says "fully remote" but also
     "in-office 2 days/week" — rejection wins.

  8. NONUS-IN-BODY: body strong remote signal but non-US region in body → DROPPED.

No network, no LLM calls.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

from alice.pipeline.daily_delta import _remote_us_ok  # noqa: E402


# ---------------------------------------------------------------------------
# Shared synthetic fixtures
# ---------------------------------------------------------------------------

# A Cobalt Automation-style listing: location = "Eastern Time Zone", no remote in title
GEO_CITY_ONLY      = "CPM Manufacturing Automation | Eastern Time Zone"
GEO_CITY_ONLY2     = "Senior Solutions Engineer | Columbus, OH"

BODY_FULLY_REMOTE  = (
    "Cobalt Automation is hiring a CPM to grow our manufacturing robotics business. "
    "This role is fully remote, US only. You will work from home every day. "
    "We have a remote-first culture and provide a remote work stipend."
)

BODY_HYBRID_BOILERPLATE = (
    "We are a remote-first company. We support remote work and offer a "
    "remote work stipend. However, this role is onsite 3 days per week at "
    "our Chicago headquarters. Candidates must be local to Chicago."
)

BODY_NO_REMOTE = (
    "This is an in-office position at our Detroit facility. "
    "You will work alongside the manufacturing engineering team on-site. "
    "Relocation assistance is available."
)

BODY_BOILERPLATE_ONLY = (
    "We embrace a remote-first culture and offer remote work stipends. "
    "Our team is distributed and we support asynchronous work. "
    "Remote work equipment is provided."
)

BODY_FULLY_REMOTE_BUT_HYBRID = (
    "This role is fully remote — however, we ask team members to come "
    "into the office 2 days per week when in the same city. "
    "We are hybrid-flexible."
)

BODY_FULLY_REMOTE_NONUS = (
    "This position is fully remote. Open to candidates in Canada, the UK, "
    "or Europe. We are not able to hire in the United States at this time."
)

BODY_REMOTE_US_EXPLICIT = (
    "Position is remote (US). You must be authorized to work in the "
    "United States. We do not offer visa sponsorship."
)


# ---------------------------------------------------------------------------
# 1. RESCUE: genuinely-remote-in-body role
# ---------------------------------------------------------------------------

def test_rescue_fully_remote_in_body():
    """Location = 'Eastern Time Zone' (no 'remote' in geo), body says
    'This role is fully remote, US only' → must PASS."""
    assert _remote_us_ok(GEO_CITY_ONLY, remote_flag=False, body=BODY_FULLY_REMOTE), (
        "A role with 'fully remote' declared in the JD body should be rescued "
        "when title/location carry no remote signal."
    )


def test_rescue_remote_us_explicit_in_body():
    """Location = city, body says 'Position is remote (US)' → must PASS."""
    assert _remote_us_ok(GEO_CITY_ONLY2, remote_flag=False, body=BODY_REMOTE_US_EXPLICIT), (
        "Body 'remote (US)' is a strong positional remote signal and should rescue."
    )


# ---------------------------------------------------------------------------
# 2. ONSITE-WITH-REMOTE-BOILERPLATE must still be DROPPED (the guard)
# ---------------------------------------------------------------------------

def test_onsite_with_remote_boilerplate_dropped():
    """Body says 'remote-first culture' AND 'onsite 3 days/week' →
    rejection WINS; role must be DROPPED."""
    assert not _remote_us_ok(GEO_CITY_ONLY, remote_flag=False, body=BODY_HYBRID_BOILERPLATE), (
        "GUARD FAILURE: a body with hybrid/onsite language must drop the role even "
        "if the same body mentions remote culture. Rejection wins over remote mention."
    )


# ---------------------------------------------------------------------------
# 3. CLEARLY-ONSITE regression (no remote language anywhere)
# ---------------------------------------------------------------------------

def test_clearly_onsite_no_remote_language_dropped():
    """Body has no remote language; title/location have no remote signal →
    must be DROPPED (regression: original behavior unchanged)."""
    assert not _remote_us_ok(GEO_CITY_ONLY, remote_flag=False, body=BODY_NO_REMOTE), (
        "A clearly-onsite role with no remote language anywhere must be dropped."
    )


def test_clearly_onsite_no_body_dropped():
    """No remote in geo, no body at all → DROPPED."""
    assert not _remote_us_ok(GEO_CITY_ONLY, remote_flag=False, body=None), (
        "Without a body and without remote in title/location, the role must drop."
    )


# ---------------------------------------------------------------------------
# 4. TITLE-PASSES regression: original path unchanged
# ---------------------------------------------------------------------------

def test_title_remote_passes_without_body():
    """'Remote US' in title/location → PASSES even without body."""
    assert _remote_us_ok("Senior CSM | Remote, US", remote_flag=False, body=None)


def test_remote_flag_passes_without_body():
    """remote_flag=True with a non-hub-city location → PASSES (original
    remote_flag path unchanged). Note: hub-city locations (SF, NYC, Chicago,
    Boston, etc.) still drop even with remote_flag=True — the hub-city check
    runs before the remote_flag check and is not overridden by remote_flag."""
    assert _remote_us_ok("Solutions Engineer | Denver, CO", remote_flag=True, body=None)


def test_hybrid_in_geo_dropped_regardless_of_body():
    """'hybrid' in geo → DROPPED even when body declares fully remote."""
    assert not _remote_us_ok("Solutions Engineer | Hybrid - Chicago", remote_flag=False,
                              body=BODY_FULLY_REMOTE)


# ---------------------------------------------------------------------------
# 5. HUB-CITY without remote stays DROPPED even with body boilerplate
# ---------------------------------------------------------------------------

def test_hub_city_title_no_body_dropped():
    """Hub-city in title/location, no 'remote' in geo, no body → DROPPED."""
    assert not _remote_us_ok("FDE - SF | San Francisco", remote_flag=False, body=None)


def test_hub_city_title_boilerplate_body_dropped():
    """Hub-city in title, body only has remote boilerplate → DROPPED.
    Boilerplate body is not a strong enough rescue signal."""
    assert not _remote_us_ok("FDE - SF | San Francisco", remote_flag=False,
                              body=BODY_BOILERPLATE_ONLY)


# ---------------------------------------------------------------------------
# 6. BODY-ONLY-BOILERPLATE is NOT rescued
# ---------------------------------------------------------------------------

def test_boilerplate_only_body_not_rescued():
    """Body has only 'remote-first culture' / benefits copy, no positional
    remote declaration → must NOT rescue; role is DROPPED."""
    assert not _remote_us_ok(GEO_CITY_ONLY, remote_flag=False, body=BODY_BOILERPLATE_ONLY), (
        "Remote-culture boilerplate is not a strong signal and must not rescue."
    )


# ---------------------------------------------------------------------------
# 7. STRONG-SIGNAL-BUT-HYBRID-BODY: rejection wins
# ---------------------------------------------------------------------------

def test_strong_remote_signal_but_hybrid_body_dropped():
    """Body says 'this role is fully remote' but also mentions 'in-office
    2 days per week' and 'hybrid-flexible' → DROPPED (rejection wins)."""
    assert not _remote_us_ok(GEO_CITY_ONLY, remote_flag=False,
                              body=BODY_FULLY_REMOTE_BUT_HYBRID), (
        "GUARD: hybrid mention in body must win over a 'fully remote' declaration "
        "in the same body."
    )


# ---------------------------------------------------------------------------
# 8. NONUS-IN-BODY: strong remote signal but non-US region
# ---------------------------------------------------------------------------

def test_strong_remote_signal_nonus_body_dropped():
    """Body says 'fully remote' but explicitly names Canada/UK/Europe and says
    'not able to hire in the United States' → DROPPED."""
    assert not _remote_us_ok(GEO_CITY_ONLY, remote_flag=False,
                              body=BODY_FULLY_REMOTE_NONUS), (
        "Non-US region in body must drop a strong-remote-signal role."
    )
