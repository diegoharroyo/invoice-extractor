"""Make the sample invoices in samples/ and samples/truth.json, the values printed on each one.
Run: uv run make_invoices.py   (same seed, same bytes; the scans and photos need pdftoppm and ImageMagick)

Everything is fake: vendors on example.com/.net/.org, 555-01xx phone numbers, tax IDs that can't be valid,
and a "SAMPLE - synthetic" stamp on every page. 14 invoices have a planted mistake, listed in truth.json."""

import json
import random
import shutil
import subprocess
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

rl_config.invariant = 1  # no timestamps in the PDFs, so they hash the same every run

OUT = Path("samples")
N = 70
LAYOUTS = ["classic", "modern", "compact", "european"]
MULTIPAGE = {7, 18, 29, 40}         # 30-45 lines, 2 pages
SCANNED = {5, 14, 27, 38}           # printed, then "scanned" to a crooked, noisy PNG
PHOTOS = set(range(51, 71))         # printed, then photographed with a phone: skewed, shadowed, small JPEG
ROUGH = set(range(61, 71))          # ...from further away and shaky: about 72 dpi, motion blur, heavy JPEG
STAMPED = {52, 55, 58}              # a red PAID stamp across the page before the photo
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"  # for the stamp; any bold .ttf works
ERRORS = {3: "total", 11: "subtotal", 16: "line", 22: "date", 25: "total", 31: "subtotal", 36: "line", 44: "total",
          54: "total", 59: "subtotal", 63: "total", 66: "line", 69: "subtotal"}
DUPLICATE = (47, 12)                # invoice 47 repeats vendor + number of invoice 12

VENDORS = [  # name, website, currency (None = either), what they sell
    ("Bluegate Office Supply", "bluegate.example.com", "USD", "office"),
    ("Harbor & Pine Printing", "harborpine.example.net", "USD", "print"),
    ("Kestrel Cleaning Co.", "kestrel-clean.example.org", "USD", "cleaning"),
    ("Brightwell IT Services", "brightwell.example.com", None, "it"),
    ("Quarry Hill Coffee", "quarryhill.example.net", None, "coffee"),
    ("Lumen Parts GmbH", "lumenparts.example.com", "EUR", "parts"),
    ("Atelier Moreau SARL", "atelier-moreau.example.org", "EUR", "print"),
    ("Vantor Freight", "vantor.example.net", None, "freight"),
]
CUSTOMERS = ["Sample Client LLC", "Example Retail Inc.", "Demo Studio Ltd", "Placeholder Foods Co."]
ITEMS = {
    "office": [("Copy paper A4, box of 5 reams", 18, 42), ("Toner cartridge, black", 45, 120), ("Desk chair", 90, 260),
               ("Whiteboard markers, pack of 12", 6, 15), ("Stapler, heavy duty", 12, 30), ("Filing cabinet", 120, 300)],
    "print": [("Business cards, 500 pcs", 25, 60), ("Flyers A5, 1000 pcs", 70, 150), ("Roll-up banner 85x200", 60, 140),
              ("Design proof, per hour", 30, 55), ("Stickers, die cut, 250 pcs", 40, 90)],
    "cleaning": [("Office cleaning, per visit", 80, 160), ("Window cleaning", 60, 140), ("Carpet shampoo, per room", 35, 70),
                 ("Supplies surcharge", 10, 25)],
    "it": [("Support hours", 45, 95), ("Laptop setup and imaging", 60, 120), ("Backup service, monthly", 20, 60),
           ("Network switch, 24 ports", 150, 400), ("Password manager seats", 3, 8)],
    "coffee": [("Espresso beans 1 kg", 18, 32), ("Oat milk, case of 12", 20, 35), ("Paper cups, sleeve of 50", 4, 9),
               ("Machine descaling service", 40, 90)],
    "parts": [("Hex bolts M8, box of 100", 9, 22), ("Bearing 6204-2RS", 3, 9), ("Aluminium profile 40x40, 1 m", 12, 28),
              ("Cable gland M20", 1, 4), ("Stepper motor NEMA 17", 14, 35), ("Shipping and handling", 8, 25)],
    "freight": [("Pallet transport, per pallet", 60, 180), ("Fuel surcharge", 15, 60), ("Waiting time, per hour", 35, 70),
                ("Liftgate delivery", 25, 60)],
}
TAX = {"USD": [Decimal(0), Decimal("0.0625"), Decimal("0.08875")], "EUR": [Decimal("0.19"), Decimal("0.20"), Decimal("0.21")]}
CENT = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(x).quantize(CENT, ROUND_HALF_UP)


def invoice(i: int, rng: random.Random) -> dict:
    layout = LAYOUTS[i % 4]
    pool = [v for v in VENDORS if (v[2] == "EUR") == (layout == "european") or v[2] is None]
    name, site, cur, kind = rng.choice(pool)
    cur = "EUR" if layout == "european" else cur or rng.choice(["USD", "EUR"])
    issued = date(2026, 1, 5) + timedelta(days=rng.randrange(300))
    lines = []
    for _ in range(rng.randint(30, 45) if i in MULTIPAGE else rng.randint(1, 7)):
        desc, lo, hi = rng.choice(ITEMS[kind])
        qty = Decimal(rng.choice([1, 1, 2, 3, 4, 5, 10, 12, 20])) if "hour" not in desc else Decimal(rng.randint(2, 16)) / 2
        unit = money(rng.uniform(lo, hi))
        lines.append({"description": desc, "quantity": qty, "unit_price": unit, "amount": money(qty * unit)})
    inv = {
        "file": f"invoice_{i:02d}" + (".png" if i in SCANNED else ".jpg" if i in PHOTOS else ".pdf"),
        "layout": layout, "pages": 2 if i in MULTIPAGE else 1, "scanned": i in SCANNED, "photo": i in PHOTOS,
        "rough": i in ROUGH, "error": ERRORS.get(i),
        "vendor_name": name, "website": site, "phone": f"+1 555-01{rng.randint(10, 99)}",
        "tax_id": f"EU VAT XX{rng.randint(10**8, 10**9 - 1)}" if cur == "EUR" else f"EIN 00-{rng.randint(10**6, 10**7 - 1)}",
        "customer": rng.choice(CUSTOMERS),
        "invoice_number": f"{name[:3].upper()}-{issued.year}-{rng.randint(100, 9999):04d}",
        "invoice_date": issued, "due_date": issued + timedelta(days=rng.choice([15, 30, 30, 45])),
        "currency": cur, "line_items": lines,
    }
    inv["subtotal"] = sum((x["amount"] for x in lines), Decimal(0))
    inv["tax_rate"] = rng.choice(TAX[cur])
    inv["tax"] = money(inv["subtotal"] * inv["tax_rate"])
    inv["total"] = inv["subtotal"] + inv["tax"]
    # planted mistakes: the printed numbers are wrong, the way they are on real invoices
    if inv["error"] == "total":
        inv["total"] += rng.choice([Decimal(10), Decimal(100), Decimal("-9.00"), Decimal("0.90")])
    elif inv["error"] == "subtotal":
        inv["subtotal"] += money(rng.uniform(5, 60))
        inv["total"] = inv["subtotal"] + inv["tax"]
    elif inv["error"] == "line":
        lines[0]["amount"] += money(lines[0]["unit_price"])  # one unit too many, and the sums carry it on
        inv["subtotal"] = sum((x["amount"] for x in lines), Decimal(0))
        inv["tax"] = money(inv["subtotal"] * inv["tax_rate"])
        inv["total"] = inv["subtotal"] + inv["tax"]
    elif inv["error"] == "date":
        inv["due_date"] = issued - timedelta(days=rng.choice([3, 10, 30]))
    return inv


# --- how each layout writes numbers and dates ---

def fmt_num(x: Decimal, layout: str) -> str:
    s = f"{x:,.2f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".") if layout == "european" else s


def fmt_money(x: Decimal, cur: str, layout: str) -> str:
    n = fmt_num(x, layout)
    if layout == "european":
        return f"{n} €"
    if layout == "compact":
        return n
    return f"${n}" if cur == "USD" else f"€{n}" if layout == "modern" else f"EUR {n}"


def fmt_qty(q: Decimal, layout: str) -> str:
    s = f"{q.normalize():f}"
    return s.replace(".", ",") if layout == "european" else s


def fmt_date(d: date, layout: str) -> str:
    return {"classic": d.strftime("%b %-d, %Y"), "modern": d.isoformat(), "compact": d.strftime("%m/%d/%Y"),
            "european": d.strftime("%d.%m.%Y")}[layout]


def stamp(canvas, doc):
    canvas.saveState()
    w, h = doc.pagesize
    canvas.setFillColor(colors.Color(0.85, 0.1, 0.1, alpha=0.18))
    canvas.setFont("Helvetica-Bold", 54)
    canvas.translate(w / 2, h / 2)
    canvas.rotate(35)
    canvas.drawCentredString(0, 0, "SAMPLE - synthetic")
    canvas.restoreState()
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.grey)
    canvas.drawString(15 * mm, 10 * mm, "Synthetic invoice made for a software demo. The company and the people on it do not exist.")
    canvas.drawRightString(w - 15 * mm, 10 * mm, f"Page {doc.page}")
    if doc.layout == "modern":
        canvas.setFillColor(colors.HexColor("#1f4e79"))
        canvas.rect(0, h - 18 * mm, w, 18 * mm, stroke=0, fill=1)
    canvas.restoreState()


def render(inv: dict, path: Path) -> None:
    lay, cur = inv["layout"], inv["currency"]
    st = getSampleStyleSheet()
    body = st["Normal"].clone("b", fontName="Courier" if lay == "compact" else "Helvetica", fontSize=9, leading=11)
    big = st["Title"].clone("t", fontName="Courier-Bold" if lay == "compact" else "Helvetica-Bold", alignment=0)
    p = lambda text, style=body: Paragraph(text, style)
    m, d = lambda x: fmt_money(x, cur, lay), lambda x: fmt_date(x, lay)
    labels = {
        "classic": ("INVOICE", "Invoice #", "Date", "Due date", "Bill to", "Subtotal", "Tax", "Total"),
        "modern": ("Invoice", "Invoice no.", "Issued", "Payment due", "Billed to", "Subtotal", "Sales tax", "Amount due"),
        "compact": ("INVOICE", "Ref", "Date", "Due", "Customer", "SUBTOTAL", "TAX", "TOTAL " + cur),
        "european": ("Invoice", "Invoice no.", "Invoice date", "Due date", "Customer", "Net amount",
                     f"VAT {fmt_qty(inv['tax_rate'] * 100, lay)} %", "Total gross"),
    }[lay]
    title, l_no, l_date, l_due, l_to, l_sub, l_tax, l_tot = labels
    vendor = (f"<b>{inv['vendor_name']}</b><br/>{inv['website']}<br/>{inv['phone']}<br/>{inv['tax_id']}")
    meta = Table([[l_no, inv["invoice_number"]], [l_date, d(inv["invoice_date"])], [l_due, d(inv["due_date"])]],
                 style=[("FONT", (0, 0), (-1, -1), body.fontName, 9), ("FONT", (0, 0), (0, -1), body.fontName + "-Bold", 9)])
    if lay == "compact":
        head = [p(vendor), Spacer(1, 4 * mm), p(title, big), meta]
    else:
        head = [Spacer(1, 8 * mm if lay == "modern" else 0),
                Table([[p(vendor), [p(title, big), meta]]], colWidths=["55%", "45%"],
                      style=[("VALIGN", (0, 0), (-1, -1), "TOP")])]
    head += [Spacer(1, 6 * mm), p(f"<b>{l_to}:</b> {inv['customer']}, 123 Example Street, Springfield"), Spacer(1, 6 * mm)]

    cols = ["Description", "Qty", "Unit price", "Amount"] if lay != "european" else ["Item", "Qty", "Unit price", "Net"]
    rows = [cols] + [[p(x["description"]), fmt_qty(x["quantity"], lay), m(x["unit_price"]), m(x["amount"])]
                     for x in inv["line_items"]]
    table = LongTable(rows, colWidths=["52%", "10%", "19%", "19%"], repeatRows=1)
    style = [("FONT", (0, 0), (-1, -1), body.fontName, 9), ("FONT", (0, 0), (-1, 0), body.fontName + "-Bold", 9),
             ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    style += {"classic": [("GRID", (0, 0), (-1, -1), 0.4, colors.grey)],
              "modern": [("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef3f8")]),
                         ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#1f4e79"))],
              "compact": [("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black)],
              "european": [("BOX", (0, 0), (-1, -1), 0.6, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black)]}[lay]
    table.setStyle(TableStyle(style))
    totals = Table([[l_sub, m(inv["subtotal"])], [l_tax, m(inv["tax"])], [l_tot, m(inv["total"])]],
                   colWidths=[40 * mm, 35 * mm], hAlign="RIGHT",
                   style=[("FONT", (0, 0), (-1, -1), body.fontName, 9), ("FONT", (0, -1), (-1, -1), body.fontName + "-Bold", 10),
                          ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black)])
    doc = SimpleDocTemplate(str(path), pagesize=A4 if lay == "european" else letter,
                            leftMargin=18 * mm, rightMargin=18 * mm, topMargin=22 * mm, bottomMargin=20 * mm)
    doc.layout = lay
    doc.build(head + [table, Spacer(1, 5 * mm), totals, Spacer(1, 8 * mm), p("Thank you for your business.")],
              onFirstPage=stamp, onLaterPages=stamp)


def scan(pdf: Path, png: Path, rng: random.Random) -> None:
    """A printed-and-scanned look: grey, a little crooked, noisy, slightly blurred."""
    subprocess.run(["pdftoppm", "-r", "150", "-gray", "-png", "-singlefile", pdf, pdf.with_suffix("")], check=True)
    subprocess.run(["magick", pdf.with_suffix(".png"), "-background", "white", "-rotate", f"{rng.uniform(-2, 2):.2f}",
                    "-blur", "0x0.7", "-seed", str(rng.randrange(10**6)), "-attenuate", "0.25", "+noise", "Gaussian",
                    "-level", "0%,96%", "-colorspace", "Gray", "-strip", png], check=True)
    pdf.unlink()


def photo(pdf: Path, jpg: Path, rng: random.Random, paid: bool, rough: bool) -> None:
    """A phone photo of the printout on a desk: low resolution, in perspective, a shadow across it, JPEG.
    rough: taken from further away with a shaky hand, so some digits are hard to tell apart even for a person."""
    png = pdf.with_suffix(".png")
    subprocess.run(["pdftoppm", "-r", "72" if rough else "100", "-png", "-singlefile", pdf, pdf.with_suffix("")], check=True)
    w, h = map(int, subprocess.run(["magick", "identify", "-format", "%w %h", png], capture_output=True, text=True,
                                   check=True).stdout.split())
    c = [rng.randint(10, 70) for _ in range(8)]  # how far each corner moves in
    stamp = ["-fill", "rgba(200,30,30,0.5)", "-font", FONT, "-pointsize", "150", "-gravity", "center",
             "-annotate", f"{rng.randint(-30, -10)}x{rng.randint(-30, -10)}+0+{rng.randint(-150, 250)}", "PAID"]
    subprocess.run(["magick", png, *(stamp if paid else []), "-virtual-pixel", "background", "-background", "#7d7366",
                    "-distort", "Perspective",
                    f"0,0 {c[0]},{c[1]}  {w},0 {w - c[2]},{c[3]}  {w},{h} {w - c[4]},{h - c[5]}  0,{h} {c[6]},{h - c[7]}",
                    "(", "+clone", "-sparse-color", "Barycentric", f"0,0 white {w},{h} gray{rng.randint(50, 65)}", ")",
                    "-compose", "Multiply", "-composite",
                    *(["-motion-blur", f"0x1.4+{rng.randrange(180)}"] if rough else ["-blur", "0x0.8"]), "-seed", str(rng.randrange(10**6)),
                    "-attenuate", "0.4", "+noise", "Gaussian", "-quality", "35" if rough else "50", jpg], check=True)
    png.unlink()
    pdf.unlink()


def jsonable(x):
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, date):
        return x.isoformat()
    raise TypeError(x)


def main() -> None:
    rng = random.Random(2026)
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir()
    invoices = [invoice(i, rng) for i in range(1, N + 1)]
    dup, orig = invoices[DUPLICATE[0] - 1], invoices[DUPLICATE[1] - 1]
    dup.update(vendor_name=orig["vendor_name"], website=orig["website"], invoice_number=orig["invoice_number"],
               error="duplicate")
    for inv in invoices:
        pdf = OUT / (Path(inv["file"]).stem + ".pdf")
        render(inv, pdf)
        if inv["scanned"]:
            scan(pdf, OUT / inv["file"], rng)
        elif inv["photo"]:
            photo(pdf, OUT / inv["file"], rng, int(inv["file"][8:10]) in STAMPED, inv["rough"])
    truth = [{k: v for k, v in inv.items() if k not in ("website", "phone", "tax_id", "customer", "tax_rate")}
             for inv in invoices]
    (OUT / "truth.json").write_text(json.dumps(truth, indent=1, default=jsonable) + "\n")
    print(f"{len(invoices)} invoices in {OUT}/, {sum(1 for i in invoices if i['error'])} with a planted mistake")


if __name__ == "__main__":
    main()
