import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

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
                       (invoice(currency="$"), "not a currency code"), (invoice(unreadable=["total"]), "hard to read")]:
        assert any(words in w for w in extract.check(inv, set())), words


def test_duplicate_in_batch():
    seen = set()
    assert extract.check(invoice(), seen) == []
    assert "duplicate" in extract.check(invoice(vendor_name="ACME"), seen)[0]
    assert "duplicate" in extract.check(invoice(invoice_number="a-1 "), seen)[0]
    assert "duplicate" in extract.check(invoice(vendor_name="Acme Inc."), seen)[0]  # same number and total
    assert extract.check(invoice(invoice_number="A-2"), seen) == []
    assert extract.check(invoice(vendor_name="Gamma", invoice_number="", total=33.0), seen) == []
    assert extract.check(invoice(vendor_name="Delta", invoice_number="", total=33.0), seen) == []  # no number: no match


def test_formulas_stay_text(tmp_path):
    r = {"file": "=x.pdf", "invoice": invoice(vendor_name=" =HYPERLINK(\"http://evil\")", invoice_number="0012"),
         "reasons": []}
    r["invoice"]["line_items"][0]["description"] = "-5% discount\ud800"  # and a broken character
    extract.to_csv([r], tmp_path)
    inv = (tmp_path / "invoices.csv").read_text(encoding="utf-8-sig").splitlines()[1]
    assert inv.startswith("'=x.pdf,") and ',"\' =HYPERLINK(' in inv
    assert ",'-5% discount?," in (tmp_path / "line_items.csv").read_text(encoding="utf-8-sig")
    sheet = extract.sheet_cells(extract.rows([r]), extract.HEADER)[0]
    assert sheet[4] == "'0012" and sheet[5] == "2026-03-01" and sheet[8] == 30.0  # text stays text, dates are dates


def test_sheet_resets_last_runs_orange(monkeypatch):
    calls = []

    class Ws:
        id, row_count = 0, 1000

        def __getattr__(self, name):
            return lambda *a, **kw: calls.append((name, a))

    book = SimpleNamespace(worksheet=lambda name: Ws(), batch_update=lambda body: None)
    monkeypatch.setitem(sys.modules, "gspread", SimpleNamespace(
        service_account=lambda filename: SimpleNamespace(open_by_key=lambda key: book), WorksheetNotFound=KeyError))
    monkeypatch.setenv("SHEET_ID", "x")
    monkeypatch.setenv("GOOGLE_CREDENTIALS", "x.json")
    extract.to_sheet([{"file": "a.pdf", "invoice": invoice(), "reasons": ["total says"]}])
    formats = [a for name, a in calls if name == "format"]
    assert ("A2:L1000", {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}) in formats
    assert [f["range"] for name, a in calls if name == "batch_format" for f in a[0]] == ["A2:L2"]


def test_check_flags_exactly_the_planted_mistakes():
    # The true printed values, no Claude involved: the rules alone must find every planted mistake and nothing else.
    seen = set()
    flagged = {t["file"] for t in TRUTH if extract.check(t | {"unreadable": []}, seen)}
    assert flagged == {t["file"] for t in TRUTH if t["error"]}


def test_scoring_tolerates_case_and_cents_only():
    assert ev.same("vendor_name", " acme ", "Acme") and ev.same("total", 10.004, 10.0)
    assert not ev.same("total", 10.01, 10.0) and not ev.same("total", None, 10.0)


def test_offline_run_writes_csv(tmp_path):
    out = subprocess.run([sys.executable, "extract.py", "samples", "--offline", "--recorded", "recorded",
                          "--out", str(tmp_path / "new")], capture_output=True, text=True, check=True, env={"PATH": ""})
    summary = json.loads(out.stdout)
    assert summary == summary | {"files": len(TRUTH), "failed": 0, "skipped": 1, "spent_usd": 0}
    assert "truth.json" in out.stderr  # anything that isn't a PDF or image is named, not silently left out
    assert summary["needs_review"] == 18  # the 14 planted mistakes and 4 more, as in the README
    assert len((tmp_path / "new" / "invoices.csv").read_text().splitlines()) == len(TRUTH) + 1


def test_saved_answers_go_by_content(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "renamed.pdf").write_bytes(Path("samples/invoice_02.pdf").read_bytes())
    (tmp_path / "in" / "new.pdf").write_bytes(b"%PDF-1.4 never read")
    run = subprocess.run([sys.executable, "extract.py", str(tmp_path / "in"), "--offline", "--recorded", "recorded",
                          "--out", str(tmp_path)], check=False, capture_output=True, text=True, env={"PATH": ""})
    assert json.loads(run.stdout) | {"spent_usd": 0} == {"files": 2, "needs_review": 0, "failed": 1, "skipped": 0,
                                                       "spent_usd": 0}
    assert "new.pdf" in run.stderr and "renamed.pdf" not in run.stderr and run.returncode == 1


def test_new_prompt_reads_again(tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    old = extract.saved_answer(Path("samples/invoice_01.pdf"), "claude-sonnet-5-5", Path("recorded"))
    (rec / "claude-sonnet-5-5").mkdir(parents=True)
    saved = json.loads(old.read_text())
    saved["provenance"]["prompt_version"] = "0"
    saved["invoice"]["total"] = str(saved["invoice"]["total"])  # hand-edited: a number written as text
    (rec / "claude-sonnet-5-5" / old.name).write_text(json.dumps(saved))
    saved["provenance"]["prompt_version"] = extract.PROMPT_VERSION
    (rec / "lax").mkdir()
    (rec / "lax" / old.name).write_text(json.dumps(saved))
    lax = extract.extract(Path("samples/invoice_01.pdf"), "lax", "api", rec, offline=True)["invoice"]
    assert isinstance(lax["total"], float) and extract.check(lax, set()) == []
    monkeypatch.setattr(extract, "ask_api", lambda path, model: (invoice(), {"input_tokens": 1, "output_tokens": 1}))
    with pytest.raises(FileNotFoundError):  # offline never pays, it fails instead
        extract.extract(Path("samples/invoice_01.pdf"), "claude-sonnet-5-5", "api", rec, offline=True)
    fresh = extract.extract(Path("samples/invoice_01.pdf"), "claude-sonnet-5-5", "api", rec, offline=False)
    assert fresh["fresh"] and fresh["provenance"]["prompt_version"] == extract.PROMPT_VERSION
    assert extract.extract(Path("samples/invoice_01.pdf"), "claude-sonnet-5-5", "api", rec, offline=True)["invoice"]
    (tmp_path / "not_a_folder").write_text("")  # saving fails: the paid-for answer is still used
    assert extract.extract(Path("samples/invoice_01.pdf"), "claude-sonnet-5-5", "api", tmp_path / "not_a_folder",
                           offline=False)["fresh"]


def test_eval_counts_unflagged_misreads():
    s = {r["model"]: ev.review_numbers(r) for r in
         [ev.score(Path("samples"), Path("recorded") / m, TRUTH) for m in extract.PRICES]}
    assert [(n["silent"], n["misread"]) for n in s.values()] == [(1, 6), (2, 5), (2, 4)]


def test_ask_api_sends_the_file_and_schema(monkeypatch):
    # No key, no network: a stand-in for anthropic.Anthropic that records the request.
    calls, stop = [], "end_turn"

    def parse(**kw):
        calls.append(kw)
        return SimpleNamespace(stop_reason=stop, parsed_output=extract.Invoice(**invoice()),
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=300))

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(
        Anthropic=lambda: SimpleNamespace(messages=SimpleNamespace(parse=parse))))
    inv, usage = extract.ask_api(Path("samples/invoice_01.pdf"), "claude-sonnet-5-5")
    assert inv["total"] == 33.0 and usage == {"input_tokens": 1500, "output_tokens": 300}
    kw = calls[0]
    assert kw["output_format"] is extract.Invoice and kw["system"] == extract.PROMPT
    assert kw["messages"][0]["content"][0]["source"]["media_type"] == "application/pdf"
    stop = "max_tokens"
    with pytest.raises(RuntimeError, match="max_tokens"):
        extract.ask_api(Path("samples/invoice_01.pdf"), "claude-sonnet-5-5")


def test_big_photos_are_scaled_down(tmp_path):
    big = tmp_path / "photo.png"
    Image.frombytes("RGB", (2400, 1600), os.urandom(2400 * 1600 * 3)).save(big)  # noise doesn't compress
    assert big.stat().st_size > extract.MAX_IMAGE
    source = extract.content(big)[0]["source"]
    raw = base64.b64decode(source["data"])
    assert source["media_type"] == "image/jpeg" and len(raw) < extract.MAX_IMAGE
    assert max(Image.open(io.BytesIO(raw)).size) == 1568
    turned = tmp_path / "turned.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # the phone held sideways: "rotate 90° to show"
    Image.new("RGB", (400, 300)).save(turned, exif=exif)
    raw = base64.b64decode(extract.content(turned)[0]["source"]["data"])
    assert Image.open(io.BytesIO(raw)).size == (300, 400)
    named_wrong = tmp_path / "really_png.jpg"
    Image.new("RGB", (100, 100)).save(named_wrong, "PNG")
    assert extract.content(named_wrong)[0]["source"]["media_type"] == "image/png"
    receipt = tmp_path / "receipt.png"
    Image.new("L", (1200, 9000), 255).save(receipt)  # small file, but too tall for the API
    raw = base64.b64decode(extract.content(receipt)[0]["source"]["data"])
    assert max(Image.open(io.BytesIO(raw)).size) == 1568
    small = extract.content(Path("samples/invoice_51.jpg"))[0]["source"]  # the samples go as they are
    assert base64.b64decode(small["data"]) == Path("samples/invoice_51.jpg").read_bytes()
