from pathlib import Path
from alice.persistence import reformat_ledger as rl


def test_hyperlink_formula():
    f = rl.hyperlink_formula("https://x.co/job/5", "Senior AE")
    assert f == '=HYPERLINK("https://x.co/job/5","Senior AE")'


def test_hyperlink_softens_quotes():
    f = rl.hyperlink_formula("https://x.co", 'AE "Strategic"')
    assert '"AE \'Strategic\'"' in f and f.startswith("=HYPERLINK(")


def test_no_url_returns_plain_role():
    assert rl.hyperlink_formula("", "Senior AE") == "Senior AE"
    assert rl.hyperlink_formula("not-a-url", "Senior AE") == "Senior AE"
