# Invoice extractor

[![ci](https://github.com/diegoharroyo/invoice-extractor/actions/workflows/ci.yml/badge.svg)](https://github.com/diegoharroyo/invoice-extractor/actions/workflows/ci.yml)

Reads invoices (PDFs, scans and phone photos) with Claude and writes one row per invoice to a Google Sheet and CSV.
Plain code then checks every line, subtotal, tax and total, and marks the invoices that don't add up for review,
with the reason. Claude copies what is printed; it never fixes the numbers, so a wrong total stays visible.

![Demo: 70 invoices land in a Google Sheet, the ones to review in orange](docs/demo.gif)

*The 70 invoices are synthetic, made by `make_invoices.py`: 4 layouts, USD and EUR, 2-page invoices, scans,
phone photos (10 of them taken from further away and shaky), and 14 with a planted mistake. Claude's answers are
saved in `recorded/`, so you can rerun everything below without an API key.*

## How well it reads

| Model | Fields read right | Planted mistakes caught | False alarms | Misreads that got through | Cost per 1,000 invoices | Median time |
|---|---|---|---|---|---|---|
| Haiku 5.5 | 98.1% | 14/14 | 3/52 | 0 of 6 misread | US$ 0.87 | 4 s |
| Sonnet 5.5 (default) | 99.0% | 14/14 | 1/53 | 0 of 5 misread | US$ 14.08 | 4 s |
| Opus 5.5 | 99.4% | 14/14 | 2/54 | 1 of 4 misread | US$ 28.50 | 6 s |

What the columns mean:

- **Fields read right:** vendor, invoice number, both dates, currency, subtotal, tax, total and every line item,
  compared with what is actually printed on each invoice (`samples/truth.json`).
- **Planted mistakes caught:** invoices made wrong on purpose (a total that doesn't add up, a line where
  quantity × price is off, a due date before the invoice date, a duplicate) that ended up marked for review.
- **False alarms:** invoices with nothing wrong (no planted mistake, every field read right) marked for review
  anyway.
- **Misreads that got through:** invoices Claude read wrong that were *not* marked for review. This is the number
  that matters: anything marked gets a human look, anything else goes straight into your books.
- **Cost:** Anthropic API list prices for the tokens each invoice used.

Every misread was on the 10 rough photos. The other 60 files (PDFs, scans, ordinary phone photos, 2-page
invoices) were read perfectly by all three models. On the rough photos: Haiku 86.7% of fields, Sonnet 93.3%,
Opus 95.6%.

- **Haiku** costs a sixteenth of Sonnet and let no misread through. It says "hard to read" more often instead:
  its 3 false alarms are two rough photos and invoice_02, whose date 07/02/2026 can be July 2 or February 7.
  Good for volume, if your team doesn't mind a few more orange rows.
- **Sonnet** (the default) let no misread through and raised 1 false alarm.
- **Opus** read the most fields right but let one through: on invoice_62, a rough photo, it read the due date
  08/12/2026 as 09/12/2026 without saying it was unsure. The wrong date is still plausible, so no check can
  catch it. See [Limits](#limits).
- With 70 invoices, a difference of one or two invoices between models is noise. Run `eval.py` on a sample of
  your own before choosing.

Times were measured through the `claude` CLI and include its start-up.

Rerun the tables with `uv run eval.py` (it only reads `recorded/`, it never calls Claude).

## What it does

- Reads every `.pdf`, `.png` and `.jpg` in a folder. PDFs go to Claude as documents, scans and photos as images.
- Claude fills a fixed schema (Pydantic): vendor, invoice number, dates, currency, line items, subtotal, tax,
  total, plus the fields it couldn't read with confidence.
- Code checks each invoice: quantity × unit price = amount on every line, lines add up to the subtotal,
  subtotal + tax = total, due date not before the invoice date, a real currency code, no duplicate in the batch.
- Writes an **Invoices** tab (one row each, status, reason, the ones to review in orange) and a **Line items**
  tab to your Google Sheet, and the same as `invoices.csv` and `line_items.csv`.
- Saves each answer in `recorded/` with the file's hash, the model, the prompt version, tokens, cost and time,
  so a file is never paid for twice and every number in the sheet can be traced back.
- One bad file doesn't stop the batch: it shows up as `failed` with the error.

## Run it

You need [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run extract.py samples --offline        # the recorded answers: no key, no cost
uv run eval.py                             # the tables above
```

To read your own invoices, copy `.env.example` to `.env`, add an
[Anthropic API key](https://console.anthropic.com/), and run:

```sh
uv run --env-file .env extract.py path/to/invoices
```

It prints what it did, e.g. `{"files": 70, "needs_review": 18, "failed": 0, "spent_usd": 0.99}`.
For the Google Sheet, also set `GOOGLE_CREDENTIALS` (a service-account JSON file) and `SHEET_ID`
(the long id in the sheet's URL), and share the sheet with the service account's email.

`make_invoices.py` rebuilds `samples/` byte for byte (it needs `pdftoppm` and ImageMagick for the scans and photos).

## Adapt it to your invoices

- **Other fields** (PO number, VAT id, bank details): add them to `Invoice` in `extract.py` and to the sheet
  columns. Claude fills whatever the schema asks for.
- **Your own rules** (approved vendors, amount limits, matching against purchase orders): add a line to `check()`.
  It returns the reasons an invoice needs a look; the rest of the pipeline already handles them.
- **Where the invoices come from:** the script reads a folder. A Gmail label, a Drive folder or an upload form
  can drop files there.
- **Which model:** `--model claude-haiku-5-5` for the cheapest run. Measure on a sample of your own invoices
  first: `eval.py` works on any folder with a `truth.json` you filled in by hand.

## Limits

- Handwritten invoices are not in this test set. Bad photos are, and that's where every misread happened: ask
  for a closer, steadier photo when a row comes back "hard to read".
- The checks catch numbers that don't add up. A misread that still adds up (the right digits in the wrong field,
  or a vendor name read wrong) is only caught if Claude says it couldn't read it, so keep a person reviewing
  the orange rows and spot-checking the rest.
- One currency per invoice; no conversion.
- Invoices are sent to Anthropic's API to be read. Check that this fits your data policy.

## Tests

```sh
uv run pytest
uv run ruff check .
```

The checks run on hand-made invoices with one mistake each, on the planted mistakes in `samples/`, and on a full
offline run of all 70 files. Nothing calls Claude, so CI runs on every push.

---

Built with AI coding tools; reviewed and tested by me. MIT license.
