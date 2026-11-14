"""Score Claude's saved answers against truth.json, the values actually printed on each invoice.
Run: uv run eval.py   (prints the markdown tables that are in the README; never calls Claude)
On your own invoices: write a truth.json (the file name and the FIELDS below for each invoice), run extract.py on the folder with each model,
then: uv run eval.py path/to/invoices --recorded answers"""

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

import extract

FIELDS = ["vendor_name", "invoice_number", "invoice_date", "due_date", "currency", "subtotal", "tax", "total",
          "line_items"]


def same(field: str, got, want) -> bool:
    if field == "line_items":
        return len(got) == len(want) and all(
            g["description"].strip().casefold() == w["description"].strip().casefold()
            and all(abs(g[k] - w[k]) < 0.005 for k in ("quantity", "unit_price", "amount")) for g, w in zip(got, want))
    if field in ("subtotal", "tax", "total"):
        return got is not None and want is not None and abs(got - want) < 0.005
    if field == "vendor_name":
        return got.strip().casefold() == want.strip().casefold()
    return str(got).strip() == str(want).strip()  # a hand-written truth.json may say 1234 for "1234"


def answer(folder: Path, model_dir: Path, t: dict) -> Path:
    return extract.saved_answer(folder / t["file"], model_dir.name, model_dir.parent)


def current(rec: Path) -> bool:
    return extract.load(rec) is not None  # missing, broken or from an older prompt: that model is left out


def kind(reason: str) -> str:
    """Which check a reason comes from, without the values in it: "line 2: 3 × 10.00 is not 31.00" -> "line 2"."""
    return re.match(r"line \d+|lines|subtotal|due|not a|duplicate|hard", reason)[0]


def score(folder: Path, model_dir: Path, truth: list[dict]) -> dict:
    rows, seen, seen_printed = [], set(), set()
    for t in truth:
        saved = extract.load(answer(folder, model_dir, t))
        inv = saved["invoice"]
        wrong = [f for f in FIELDS if not same(f, inv[f], t[f])]
        reasons = extract.check(inv, seen)
        printed = extract.check({**t, "unreadable": []}, seen_printed)
        unsure = {re.split(r"[.\[]", u)[0] for u in inv["unreadable"]}
        # A misread is caught only if the flag points at it: Claude said it couldn't read every wrong field,
        # or a check fails on what Claude read that doesn't fail on what is printed.
        caught = set(wrong) <= unsure or bool({kind(r) for r in reasons} - {kind(r) for r in printed} - {"hard"})
        rows.append({"t": t, "wrong": wrong, "flagged": bool(reasons), "caught": caught, **saved["provenance"]})
    return {"model": model_dir.name, "rows": rows}


def groups(rows):
    yield "all", rows
    if "layout" not in rows[0]["t"]:  # your own truth.json only needs the file name and the fields
        return
    for lay in sorted({r["t"]["layout"] for r in rows}):
        yield lay, [r for r in rows if r["t"]["layout"] == lay]
    for name, rs in [("scanned (PNG)", [r for r in rows if r["t"]["scanned"]]),
                     ("phone photos (JPG)", [r for r in rows if r["t"]["photo"] and not r["t"]["rough"]]),
                     ("rough photos: far, shaky (JPG)", [r for r in rows if r["t"]["rough"]]),
                     ("2 pages, 30-45 lines", [r for r in rows if r["t"]["pages"] > 1])]:
        if rs:  # a smaller truth.json may have none of them
            yield name, rs


def accuracy_table(s: dict) -> str:
    out = ["| Invoices | Field accuracy | Invoices read perfectly |", "|---|---|---|"]
    for name, rs in groups(s["rows"]):
        fields = sum(len(FIELDS) - len(r["wrong"]) for r in rs)
        out.append(f"| {name} ({len(rs)}) | {fields}/{len(rs) * len(FIELDS)} ({fields / len(rs) / len(FIELDS):.1%}) "
                   f"| {sum(not r['wrong'] for r in rs)}/{len(rs)} |")
    return "\n".join(out)


def review_numbers(s: dict) -> dict:
    rs = s["rows"]
    planted = [r for r in rs if r["t"].get("error")]
    clean = [r for r in rs if not r["t"].get("error") and not r["wrong"]]  # nothing to catch: any flag is a false alarm
    return {
        "caught": sum(r["flagged"] for r in planted), "planted": len(planted),
        "false_alarms": sum(r["flagged"] for r in clean), "clean": len(clean),
        "silent": sum(1 for r in rs if r["wrong"] and not r["caught"]),
        "perfect": sum(not r["wrong"] for r in rs),
        "misread": sum(1 for r in rs if r["wrong"]),
        "cost": statistics.mean(r["cost_usd"] for r in rs),
        "latency": statistics.median(r["latency_s"] for r in rs),
    }


def comparison_table(scores: list[dict]) -> str:
    out = [("| Model | Invoices read perfectly | Field accuracy | Planted mistakes caught | False alarms "
            "| Misreads not flagged | Cost per 1,000 invoices | Median time |"), "|---|---|---|---|---|---|---|---|"]
    for s in scores:
        n = review_numbers(s)
        fields = sum(len(FIELDS) - len(r["wrong"]) for r in s["rows"])
        out.append(f"| {s['model']} | {n['perfect']}/{len(s['rows'])} | {fields / len(s['rows']) / len(FIELDS):.1%} "
                   f"| {n['caught']}/{n['planted']} | {n['false_alarms']}/{n['clean']} | {n['silent']} of {n['misread']} misread "
                   f"| US$ {n['cost'] * 1000:.2f} | {n['latency']:.0f} s |")
    return "\n".join(out)


def misses(s: dict) -> str:
    return "\n".join(f"- {r['t']['file']}: "
                     f"{', '.join(r['wrong'])}{'' if r['caught'] else ' -- NOT flagged'}"
                     for r in s["rows"] if r["wrong"]) or "- none"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder", type=Path, nargs="?", default=Path("samples"), help="invoices and their truth.json")
    ap.add_argument("--recorded", type=Path, default=Path("recorded"), help="the saved answers, one folder per model")
    args = ap.parse_args()
    if not (args.folder / "truth.json").is_file() or not args.recorded.is_dir():
        ap.error(f"needs {args.folder}/truth.json and a {args.recorded}/ folder of saved answers")
    truth = json.loads((args.folder / "truth.json").read_text())
    if not truth:
        ap.error(f"{args.folder}/truth.json lists no invoices")
    if missing := [t["file"] for t in truth if not (args.folder / t["file"]).is_file()]:
        ap.error(f"truth.json names files that aren't in {args.folder}: {', '.join(missing)}")
    order = list(extract.PRICES)
    dirs = sorted((d for d in args.recorded.iterdir() if d.is_dir()),
                  key=lambda d: order.index(d.name) if d.name in order else 99)
    scores = []
    for d in dirs:
        if lacking := [t["file"] for t in truth if not current(answer(args.folder, d, t))]:
            print(f"left out {d.name}: no answer with the current prompt for {len(lacking)} file(s), e.g. {lacking[0]}",
                  file=sys.stderr)
        else:
            scores.append(score(args.folder, d, truth))
    if not scores:
        sys.exit("no model has a saved answer for every file in truth.json")
    print(comparison_table(scores))
    for s in scores:
        print(f"\n### {s['model']}\n\n{accuracy_table(s)}\n\nMisread:\n{misses(s)}")


if __name__ == "__main__":
    main()
