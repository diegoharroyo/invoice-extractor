"""Read invoice PDFs and scans with Claude, check their arithmetic, write the results to CSV and a Google Sheet.
Run: uv run --env-file .env extract.py path/to/invoices   (the demo: extract.py samples --offline --recorded recorded)

Claude only reads what is printed. Python then checks it: line amounts, subtotal, tax, total, dates,
duplicates. Anything that doesn't add up goes to the sheet as needs_review, with the reason.
Each answer is saved in answers/ under the file's hash, so the same file is not paid for twice, even renamed;
--offline uses only saved answers and never calls Claude (tests, CI, and the demo's recorded/)."""

import argparse
import base64
import contextlib
import csv
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, Field

PROMPT_VERSION = "1"
PROMPT = """You read one invoice (a PDF, or a photo or scan of one) and copy its values into the schema.
- Copy numbers exactly as printed, even if they don't add up. Never recalculate or correct anything:
  someone else checks the arithmetic, and a silent fix hides the mistake they are looking for.
- Numbers as plain decimals: "1.234,56 €" is 1234.56, "$1,074.42" is 1074.42. Dates as YYYY-MM-DD.
- currency is the 3-letter ISO code (USD, EUR, GBP...), from the symbol or code on the invoice.
- One line_items entry per row of the item table, on every page, in order. Leave out subtotal/tax/total rows.
- tax is the total tax amount (VAT, sales tax), not the rate. Use null for a value that isn't on the invoice.
- unreadable: names of the fields you couldn't read with confidence (a blurry digit, a cut-off line). Usually empty."""
# What the Anthropic API charges per million tokens (input, output), to put a price on each invoice.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2, 10), "claude-opus-5-5": (4, 20)}
MEDIA = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp"}
MAX_IMAGE = 3_500_000  # the API takes images up to 5 MB and base64 adds a third; bigger photos get scaled down
CENT = 0.006  # amounts must match to the cent (half a cent of room for float rounding)


class Line(BaseModel):
    description: str
    quantity: float
    unit_price: float
    amount: float


class Invoice(BaseModel):
    vendor_name: str
    invoice_number: str
    invoice_date: date
    due_date: date | None
    currency: str = Field(description="3-letter ISO code")
    line_items: list[Line]
    subtotal: float | None
    tax: float | None
    total: float
    unreadable: list[str]


def shrink(raw: bytes) -> bytes:
    """A phone photo is often 3-12 MB. Claude sees images at most 1568 px on the long side anyway."""
    from PIL import Image, ImageOps

    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    if im.mode.startswith("I;16"):  # a 16-bit scan: convert() would clip everything above 255 to white
        im = im.convert("I").point(lambda v: v / 256).convert("L")
    # a transparent background becomes white, not black
    im = Image.alpha_composite(Image.new("RGBA", im.size, "white"), im.convert("RGBA")).convert("RGB")
    im.thumbnail((1568, 1568))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def content(path: Path) -> list[dict]:
    from PIL import Image

    raw, media = path.read_bytes(), "application/pdf"
    if b"%PDF-" not in raw[:1024]:  # the bytes say what it is, not the name: a PNG saved as .jpg is a PNG
        im = Image.open(io.BytesIO(raw))
        media = f"image/{(im.format or '').lower()}"
        # big photos get scaled down, a photo with an EXIF rotation turned upright (0x0112 = orientation),
        # anything over 8000 px a side (the API refuses it) or in another format sent as JPEG
        if (len(raw) > MAX_IMAGE or im.getexif().get(0x0112, 1) != 1 or max(im.size) > 8000
                or media not in MEDIA.values()):
            raw, media = shrink(raw), "image/jpeg"
    kind = "document" if media == "application/pdf" else "image"
    source = {"type": "base64", "media_type": media, "data": base64.standard_b64encode(raw).decode()}
    return [{"type": kind, "source": source}, {"type": "text", "text": "Extract this invoice."}]


def ask_api(path: Path, model: str) -> tuple[dict, dict]:
    """The Anthropic API, with your own key in ANTHROPIC_API_KEY. This is what you'd run in production."""
    import anthropic

    r = anthropic.Anthropic().messages.parse(model=model, max_tokens=16000, system=PROMPT, output_format=Invoice,
                                             messages=[{"role": "user", "content": content(path)}])
    if r.stop_reason != "end_turn" or r.parsed_output is None:
        raise RuntimeError(f"no answer: stop_reason={r.stop_reason}")
    return r.parsed_output.model_dump(mode="json"), {"input_tokens": r.usage.input_tokens,
                                                     "output_tokens": r.usage.output_tokens}


def ask_cli(path: Path, model: str) -> tuple[dict, dict]:
    """The same request through the Claude Code CLI (`claude -p`), logged in with a Claude subscription.
    Handy for trying it out without an API key. No tools, no settings, no memory: just the invoice and the prompt."""
    msg = {"type": "user", "message": {"role": "user", "content": content(path)}}
    cmd = ["claude", "-p", "--model", model, "--system-prompt", PROMPT, "--json-schema",
           json.dumps(Invoice.model_json_schema()), "--tools", "", "--setting-sources", "", "--strict-mcp-config",
           "--disable-slash-commands", "--no-session-persistence",
           "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    out = subprocess.run(cmd, check=False, input=json.dumps(msg), capture_output=True, text=True, timeout=600,
                         env=os.environ | {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    result = next((json.loads(x) for x in out.stdout.splitlines() if '"type":"result"' in x), None)
    if not result or result.get("is_error") or not result.get("structured_output"):
        raise RuntimeError(f"no answer: {(result or {}).get('result') or out.stderr[-500:]}")
    u = result["usage"]
    inp = u["input_tokens"] + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    return Invoice(**result["structured_output"]).model_dump(mode="json"), {"input_tokens": inp,
                                                                            "output_tokens": u["output_tokens"]}


def saved_answer(path: Path, model: str, recorded: Path, sha: str = "") -> Path:
    """Found by the file's content, not its name: a renamed copy is not read (or paid for) again."""
    return recorded / model / f"{sha or hashlib.sha256(path.read_bytes()).hexdigest()}.json"


def load(rec: Path, sha: str = "") -> dict | None:
    """A saved answer for this file and prompt, with its types checked ("33.0" is used as 33.0); None if missing,
    broken or from another prompt, so the file is read again."""
    try:
        saved = json.loads(rec.read_text())
        p = saved["provenance"]
        if p["sha256"] != (sha or rec.stem) or p["prompt_version"] != PROMPT_VERSION:
            return None
        return saved | {"invoice": Invoice.model_validate(saved["invoice"]).model_dump(mode="json")}
    except (OSError, ValueError, KeyError, TypeError):  # pydantic's ValidationError is a ValueError
        return None


def extract(path: Path, model: str, backend: str, recorded: Path, offline: bool) -> dict:
    """Claude's reading of one file, from the saved answers when this exact file was already read with this prompt."""
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    rec = saved_answer(path, model, recorded, sha)
    if saved := load(rec, sha):
        return saved
    if offline:
        raise FileNotFoundError(f"no saved answer in {recorded}/ for this file and prompt (drop --offline to read it)")
    t = time.monotonic()
    invoice, usage = (ask_api if backend == "api" else ask_cli)(path, model)
    pin, pout = PRICES[model]
    saved = {"invoice": invoice, "provenance": {
        "file": path.name, "sha256": sha, "model": model, "backend": backend, "prompt_version": PROMPT_VERSION,
        "recorded_at": datetime.now().isoformat(timespec="seconds"), "latency_s": round(time.monotonic() - t, 2),
        **usage, "cost_usd": round((usage["input_tokens"] * pin + usage["output_tokens"] * pout) / 1e6, 5)}}
    tmp = rec.with_suffix(".tmp")
    try:
        rec.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(saved, indent=1) + "\n")
        tmp.replace(rec)  # all or nothing: a half-written answer would look like a saved one
    except OSError as e:  # a full disk must not throw away an answer that was already paid for
        with contextlib.suppress(OSError):  # don't leave a half-written .tmp behind
            tmp.unlink()
        print(f"warning: {path.name} read but not saved ({e}), it will be paid for again next run", file=sys.stderr)
    return saved | {"fresh": True}


def money(v: float) -> str:
    return f"{v:,.2f}" if math.isclose(v, round(v, 2), rel_tol=1e-14, abs_tol=1e-9) else f"{round(v, 6):,}"  # 0.125 stays 0.125


def check(inv: dict, seen: set) -> list[str]:
    """Why this invoice needs a human look. Empty list = everything adds up."""
    why = []
    for i, x in enumerate(inv["line_items"], 1):
        if abs(x["quantity"] * x["unit_price"] - x["amount"]) > CENT:
            why.append(f"line {i}: {x['quantity']:,.10g} × {money(x['unit_price'])} is not {money(x['amount'])}")
    lines = sum(x["amount"] for x in inv["line_items"])
    sub, tax, total = inv["subtotal"], inv["tax"] or 0, inv["total"]
    if inv["line_items"] and sub is not None and abs(lines - sub) > CENT:
        why.append(f"lines add up to {money(lines)}, subtotal says {money(sub)}")
    if abs((lines if sub is None else sub) + tax - total) > CENT:
        why.append(f"subtotal + tax is {money((lines if sub is None else sub) + tax)}, total says {money(total)}")
    if inv["due_date"] and inv["due_date"] < inv["invoice_date"]:
        why.append(f"due {inv['due_date']} is before the invoice date {inv['invoice_date']}")
    if not re.fullmatch(r"[A-Z]{3}", inv["currency"]):
        why.append(f"not a currency code: {inv['currency']!r}")
    # "INV-7" and "inv-7 " are the same number; a vendor read as "Acme" once and "Acme Inc." once still matches
    # on number + total. No number, no duplicate check: two invoices without one aren't the same invoice.
    num = re.sub(r"\s", "", inv["invoice_number"]).casefold()
    keys = {("vendor", re.sub(r"\W", "", inv["vendor_name"]).casefold(), num), ("total", num, round(total, 2))}
    keys = keys if num else set()
    if keys & seen:
        why.append(f"duplicate: {inv['vendor_name']} {inv['invoice_number']} was already in this batch")
    seen.update(keys)
    if inv["unreadable"]:
        # line_items[1].amount -> line 2 amount, numbered like the other reasons
        why.append("hard to read: " + ", ".join(
            re.sub(r"line_items\[(\d+)\]\.", lambda m: f"line {int(m[1]) + 1} ", f).replace("_", " ")
            for f in inv["unreadable"]))
    return why


HEADER = ["file", "status", "reason", "vendor", "invoice_number", "invoice_date", "due_date", "currency",
          "subtotal", "tax", "total", "lines"]


def cell(v):
    """Text that starts like a formula ("=HYPERLINK(...)", "-5% discount") stays text in Excel."""
    return "'" + v if isinstance(v, str) and (v.lstrip()[:1] in ("=", "+", "-", "@") or v[:1] in ("\t", "\r", "\n")) else v


def rows(results: list[dict]) -> list[list]:
    out = []
    for r in results:
        if "error" in r:
            out.append([r["file"], "failed", r["error"]] + [""] * (len(HEADER) - 3))
            continue
        i = r["invoice"]
        out.append([r["file"], "needs_review" if r["reasons"] else "ok", "; ".join(r["reasons"]), i["vendor_name"],
                    i["invoice_number"], i["invoice_date"], i["due_date"] or "", i["currency"],
                    i["subtotal"] if i["subtotal"] is not None else "", i["tax"] if i["tax"] is not None else "",
                    i["total"], len(i["line_items"])])
    return out


def line_rows(results: list[dict]) -> list[list]:
    return [[r["file"], r["invoice"]["invoice_number"], x["description"], x["quantity"], x["unit_price"], x["amount"]]
            for r in results if "invoice" in r for x in r["invoice"]["line_items"]]


LINE_HEADER = ["file", "invoice_number", "description", "quantity", "unit_price", "amount"]


def to_csv(results: list[dict], out: Path) -> None:
    for name, header, data in [("invoices.csv", HEADER, rows(results)), ("line_items.csv", LINE_HEADER,
                                                                          line_rows(results))]:
        # the BOM makes Excel read € right; a stray broken character becomes "?" instead of losing the file
        with open(out / name, "w", newline="", encoding="utf-8-sig", errors="replace") as f:
            csv.writer(f).writerows([header] + [[cell(v) for v in r] for r in data])


def sheet_cells(data: list[list], header: list[str]) -> list[list]:
    """The Sheet parses what it's sent: "0012" would become 12, "1/2" a date, "=..." a formula. A leading ' keeps
    any text exactly as printed (Sheets doesn't show it); only the dates are left for the Sheet to read as dates."""
    dates = {header.index(h) for h in ("invoice_date", "due_date") if h in header}
    return [["'" + v if isinstance(v, str) and v and n not in dates else v for n, v in enumerate(r)] for r in data]


def open_sheet():
    """The Google Sheet in SHEET_ID, opened before any invoice is read: a bad key or a sheet that isn't shared
    shows up before anything is paid for. None without SHEET_ID."""
    if not os.environ.get("SHEET_ID"):
        return None
    import gspread

    return gspread.service_account(filename=os.environ["GOOGLE_CREDENTIALS"]).open_by_key(os.environ["SHEET_ID"])


def to_sheet(results: list[dict], book) -> None:
    """'Invoices' tab (one row each, rows to review in orange) and 'Line items' tab, rewritten every run."""
    import gspread


    def tab(name, header, data):
        try:
            ws = book.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(name, rows=1000, cols=len(header))
        ws.clear()
        ws.append_rows([header] + sheet_cells(data, header), value_input_option="USER_ENTERED")
        ws.format("1:1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
        ws.columns_auto_resize(0, len(header))
        return ws

    data, last = rows(results), chr(ord("A") + len(HEADER) - 1)
    ws = tab("Invoices", HEADER, data)
    # clear() keeps formatting: reset every row to white, or last run's orange stays behind
    ws.format(f"A2:{last}{max(ws.row_count, len(data) + 1)}", {"backgroundColor": {"red": 1, "green": 1, "blue": 1}})
    book.batch_update({"requests": [{"updateDimensionProperties": {  # reason: wide enough, long ones clip
        "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
        "properties": {"pixelSize": 380}, "fields": "pixelSize"}}]})
    flagged = [f"A{n}:{last}{n}" for n, r in enumerate(data, 2) if r[1] != "ok"]
    if flagged:
        ws.batch_format([{"range": a, "format": {"backgroundColor": {"red": 1, "green": 0.85, "blue": 0.6}}}
                         for a in flagged])
    tab("Line items", LINE_HEADER, line_rows(results))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder", type=Path, help=f"folder with the invoices ({', '.join(MEDIA)})")
    ap.add_argument("--model", default="claude-sonnet-5-5", choices=PRICES,
                    help="claude-haiku-5-5 is the cheapest; see the README for how they compare")
    ap.add_argument("--backend", choices=["api", "cli"], default="api",
                    help="api: Anthropic API key in ANTHROPIC_API_KEY (use this for real invoices). "
                         "cli: the `claude` command logged in with a subscription, for trying it out")
    ap.add_argument("--recorded", type=Path, default=Path("answers"),
                    help="where Claude's answers are saved (answers/ is git-ignored; the demo's are in recorded/)")
    ap.add_argument("--offline", action="store_true", help="only use recorded answers, never call Claude")
    ap.add_argument("--out", type=Path, default=Path("."), help="where invoices.csv and line_items.csv go")
    args = ap.parse_args()
    if not args.folder.is_dir():
        ap.error(f"{args.folder} is not a folder")
    if os.environ.get("SHEET_ID") and not Path(os.environ.get("GOOGLE_CREDENTIALS", "")).is_file():
        ap.error("SHEET_ID is set but GOOGLE_CREDENTIALS is not the path of the service-account JSON file")
    try:
        book = open_sheet()
    except Exception as e:  # noqa: BLE001 -- broken key file, sheet not shared with the service account, ...
        ap.error(f"can't open the Google Sheet: {type(e).__name__}: {e}")
    args.out.mkdir(parents=True, exist_ok=True)

    found = sorted(p for p in args.folder.iterdir() if not p.name.startswith("."))
    files = [p for p in found if p.is_file() and p.suffix.lower() in MEDIA]
    skipped = [p.name for p in found if p not in files]
    if skipped:  # .heic, .tiff, ... and subfolders are not read
        print(f"skipped, not {'/'.join(MEDIA)}: {', '.join(skipped)}", file=sys.stderr)
    results, seen = [], set()
    for path in files:
        try:
            saved = extract(path, args.model, args.backend, args.recorded, args.offline)
            reasons = check(saved["invoice"], seen)
        except Exception as e:  # noqa: BLE001 -- one unreadable file must not stop the batch
            print(f"failed: {path.name}: {e}", file=sys.stderr)
            results.append({"file": path.name, "error": f"{type(e).__name__}: {e}"})
            continue
        results.append({"file": path.name, "invoice": saved["invoice"], "reasons": reasons,
                        "provenance": saved["provenance"], "fresh": saved.get("fresh", False)})
    written = True
    for where, write in [("CSVs", lambda: to_csv(results, args.out)), ("sheet", lambda: book and to_sheet(results, book))]:
        try:
            write()
        except Exception as e:  # noqa: BLE001 -- e.g. invoices.csv open in Excel: still try the other, and report
            print(f"{where} not written: {type(e).__name__}: {e}", file=sys.stderr)
            written = False
    ok = [r for r in results if "invoice" in r]
    print(json.dumps({"files": len(files), "needs_review": sum(1 for r in ok if r["reasons"]),
                      "failed": len(files) - len(ok), "skipped": len(skipped),
                      "spent_usd": round(sum(r["provenance"]["cost_usd"] for r in ok if r["fresh"]), 4)}))
    sys.exit(0 if len(ok) == len(files) and written else 1)  # so a scheduled run or CI notices


if __name__ == "__main__":
    main()
