"""Read invoice PDFs and scans with Claude, check their arithmetic, write the results to CSV and a Google Sheet.
Run: uv run --env-file .env extract.py samples/

Claude only reads what is printed. Plain code then checks it: line amounts, subtotal, tax, total, dates,
duplicates. Anything that doesn't add up goes to the sheet as needs_review, with the reason.
Each answer is saved in recorded/ next to the file's hash, so a file is never paid for twice;
--offline uses only those saved answers and never calls Claude (tests, CI, demos)."""

import argparse
import base64
import csv
import hashlib
import json
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
MEDIA = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
CENT = 0.011  # amounts are compared to the cent, with room for float rounding


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


def content(path: Path) -> list[dict]:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    kind = "document" if path.suffix.lower() == ".pdf" else "image"
    source = {"type": "base64", "media_type": MEDIA[path.suffix.lower()], "data": data}
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


def extract(path: Path, model: str, backend: str, recorded: Path, offline: bool) -> dict:
    """Claude's reading of one file, from recorded/ when this exact file was already read with this prompt."""
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    rec = recorded / model / (path.stem + ".json")
    if rec.exists():
        saved = json.loads(rec.read_text())
        if saved["provenance"]["sha256"] == sha and saved["provenance"]["prompt_version"] == PROMPT_VERSION:
            return saved
    if offline:
        raise FileNotFoundError(f"{path.name}: no recorded answer for this file and prompt (drop --offline)")
    t = time.monotonic()
    invoice, usage = (ask_api if backend == "api" else ask_cli)(path, model)
    pin, pout = PRICES.get(model, (0, 0))
    saved = {"invoice": invoice, "provenance": {
        "file": path.name, "sha256": sha, "model": model, "backend": backend, "prompt_version": PROMPT_VERSION,
        "recorded_at": datetime.now().isoformat(timespec="seconds"), "latency_s": round(time.monotonic() - t, 2),
        **usage, "cost_usd": round((usage["input_tokens"] * pin + usage["output_tokens"] * pout) / 1e6, 5)}}
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(saved, indent=1) + "\n")
    return saved | {"fresh": True}


def check(inv: dict, seen: set) -> list[str]:
    """Why this invoice needs a human look. Empty list = everything adds up."""
    why = []
    for i, x in enumerate(inv["line_items"], 1):
        if abs(x["quantity"] * x["unit_price"] - x["amount"]) > CENT:
            why.append(f"line {i}: {x['quantity']:g} × {x['unit_price']:.2f} is not {x['amount']:.2f}")
    lines = sum(x["amount"] for x in inv["line_items"])
    sub, tax, total = inv["subtotal"], inv["tax"] or 0, inv["total"]
    if sub is not None and abs(lines - sub) > CENT:
        why.append(f"lines add up to {lines:.2f}, subtotal says {sub:.2f}")
    if abs((lines if sub is None else sub) + tax - total) > CENT:
        why.append(f"subtotal + tax is {(lines if sub is None else sub) + tax:.2f}, total says {total:.2f}")
    if inv["due_date"] and inv["due_date"] < inv["invoice_date"]:
        why.append(f"due {inv['due_date']} is before the invoice date {inv['invoice_date']}")
    if not (len(inv["currency"]) == 3 and inv["currency"].isalpha() and inv["currency"].isupper()):
        why.append(f"unknown currency {inv['currency']!r}")
    key = (inv["vendor_name"].casefold(), inv["invoice_number"])
    if key in seen:
        why.append(f"duplicate: {inv['vendor_name']} {inv['invoice_number']} was already in this batch")
    seen.add(key)
    if inv["unreadable"]:
        # line_items[1].amount -> line 2 amount, numbered like the other reasons
        why.append("hard to read: " + ", ".join(
            re.sub(r"line_items\[(\d+)\]\.", lambda m: f"line {int(m[1]) + 1} ", f).replace("_", " ")
            for f in inv["unreadable"]))
    return why


HEADER = ["file", "status", "reason", "vendor", "invoice_number", "invoice_date", "due_date", "currency",
          "subtotal", "tax", "total", "lines"]


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
        with open(out / name, "w", newline="") as f:
            csv.writer(f).writerows([header] + data)


def to_sheet(results: list[dict]) -> None:
    """Optional: 'Invoices' tab (one row each, rows to review in orange) and 'Line items' tab, rewritten every run."""
    if not os.environ.get("SHEET_ID"):
        return
    import gspread

    book = gspread.service_account(filename=os.environ["GOOGLE_CREDENTIALS"]).open_by_key(os.environ["SHEET_ID"])

    def tab(name, header, data):
        try:
            ws = book.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(name, rows=1000, cols=len(header))
        ws.clear()
        ws.append_rows([header] + data, value_input_option="USER_ENTERED")
        ws.format("1:1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
        ws.columns_auto_resize(0, len(header))
        return ws

    data = rows(results)
    ws = tab("Invoices", HEADER, data)
    ws.format(f"A2:L{len(data) + 1}", {"backgroundColor": {"red": 1, "green": 1, "blue": 1}})
    book.batch_update({"requests": [{"updateDimensionProperties": {  # reason: wide enough, long ones clip
        "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
        "properties": {"pixelSize": 380}, "fields": "pixelSize"}}]})
    flagged = [f"A{n}:L{n}" for n, r in enumerate(data, 2) if r[1] != "ok"]
    if flagged:
        ws.batch_format([{"range": a, "format": {"backgroundColor": {"red": 1, "green": 0.85, "blue": 0.6}}}
                         for a in flagged])
    tab("Line items", LINE_HEADER, line_rows(results))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder", type=Path, help="folder with the invoices (.pdf, .png, .jpg)")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--backend", choices=["api", "cli"], default="api",
                    help="api: Anthropic API key in ANTHROPIC_API_KEY. cli: the `claude` command, logged in")
    ap.add_argument("--recorded", type=Path, default=Path("recorded"))
    ap.add_argument("--offline", action="store_true", help="only use recorded answers, never call Claude")
    ap.add_argument("--out", type=Path, default=Path("."), help="where invoices.csv and line_items.csv go")
    args = ap.parse_args()

    files = sorted(p for p in args.folder.iterdir() if p.suffix.lower() in MEDIA)
    results, seen = [], set()
    for path in files:
        try:
            saved = extract(path, args.model, args.backend, args.recorded, args.offline)
        except Exception as e:  # noqa: BLE001 -- one unreadable file must not stop the batch
            print(f"failed: {path.name}: {e}", file=sys.stderr)
            results.append({"file": path.name, "error": f"{type(e).__name__}: {e}"})
            continue
        results.append({"file": path.name, "invoice": saved["invoice"], "reasons": check(saved["invoice"], seen),
                        "provenance": saved["provenance"], "fresh": saved.get("fresh", False)})
    to_csv(results, args.out)
    to_sheet(results)
    ok = [r for r in results if "invoice" in r]
    print(json.dumps({"files": len(files), "needs_review": sum(1 for r in ok if r["reasons"]),
                      "failed": len(files) - len(ok),
                      "spent_usd": round(sum(r["provenance"]["cost_usd"] for r in ok if r["fresh"]), 4)}))


if __name__ == "__main__":
    main()
