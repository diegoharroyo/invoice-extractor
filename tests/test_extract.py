import json
import subprocess
import sys
from pathlib import Path

import eval as ev
import extract

TRUTH = json.loads(Path("samples/truth.json").read_text())


def invoice(**kw):
    inv = {"vendor_name": "Acme", "invoice_number": "A-1", "invoice_date": "2026-03-01", "due_date": "2026-03-31",
           "currency": "USD", "subtotal": 30.0, "tax": 3.0, "total": 33.0, "unreadable": [],
           "line_items": [{"description": "x", "quantity": 2, "unit_price": 10.0, "amount": 20.0},
                          {"description": "y", "quantity": 1, "unit_price": 10.0, "amount": 10.0}]}
    return inv | kw


def test_clean_invoice_passes():
    assert extract.check(invoice(), set()) == []


def test_each_mistake_is_flagged():
    bad_line = invoice()["line_items"][:1] + [{"description": "y", "quantity": 1, "unit_price": 10.0, "amount": 11.0}]
    for inv, words in [(invoice(total=34.0), "total says"), (invoice(subtotal=31.0), "subtotal says"),
                       (invoice(line_items=bad_line, subtotal=31.0, total=34.0), "line 2"),
                       (invoice(due_date="2026-02-01"), "before the invoice date"),
                       (invoice(currency="$"), "unknown currency"), (invoice(unreadable=["total"]), "hard to read")]:
        assert any(words in w for w in extract.check(inv, set())), words


def test_duplicate_in_batch():
    seen = set()
    assert extract.check(invoice(), seen) == []
    assert "duplicate" in extract.check(invoice(vendor_name="ACME"), seen)[0]


def test_check_flags_exactly_the_planted_mistakes():
    # The true printed values, no Claude involved: the rules alone must find every planted mistake and nothing else.
    seen = set()
    flagged = {t["file"] for t in TRUTH if extract.check(t | {"unreadable": []}, seen)}
    assert flagged == {t["file"] for t in TRUTH if t["error"]}


def test_scoring_tolerates_case_and_cents_only():
    assert ev.same("vendor_name", " acme ", "Acme") and ev.same("total", 10.004, 10.0)
    assert not ev.same("total", 10.01, 10.0) and not ev.same("total", None, 10.0)


def test_offline_run_writes_csv(tmp_path):
    out = subprocess.run([sys.executable, "extract.py", "samples", "--offline", "--out", str(tmp_path)],
                         capture_output=True, text=True, check=True, env={"PATH": ""})
    summary = json.loads(out.stdout)
    assert summary == summary | {"files": len(TRUTH), "failed": 0, "spent_usd": 0}
    assert summary["needs_review"] >= sum(1 for t in TRUTH if t["error"])
    assert len((tmp_path / "invoices.csv").read_text().splitlines()) == len(TRUTH) + 1
