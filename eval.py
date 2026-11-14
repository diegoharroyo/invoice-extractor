"""Score the recorded answers against samples/truth.json, the values actually printed on each invoice.
Run: uv run eval.py   (prints the markdown tables that are in the README; never calls Claude)"""

import json
import statistics
from pathlib import Path

import extract

FIELDS = ["vendor_name", "invoice_number", "invoice_date", "due_date", "currency", "subtotal", "tax", "total",
          "line_items"]


def same(field: str, got, want) -> bool:
    if field == "line_items":
        return len(got) == len(want) and all(
            g["description"].strip().casefold() == w["description"].casefold()
            and all(abs(g[k] - w[k]) < 0.005 for k in ("quantity", "unit_price", "amount")) for g, w in zip(got, want))
    if isinstance(want, float) and got is not None:
        return abs(got - want) < 0.005
    if field == "vendor_name":
        return got.strip().casefold() == want.casefold()
    return got == want


def score(model_dir: Path, truth: list[dict]) -> dict:
    rows, seen = [], set()
    for t in truth:
        saved = json.loads((model_dir / (Path(t["file"]).stem + ".json")).read_text())
        inv = saved["invoice"]
        wrong = [f for f in FIELDS if not same(f, inv[f], t[f])]
        rows.append({"t": t, "wrong": wrong, "flagged": bool(extract.check(inv, seen)), **saved["provenance"]})
    return {"model": model_dir.name, "rows": rows}


def groups(rows):
    yield "all", rows
    for lay in sorted({r["t"]["layout"] for r in rows}):
        yield lay, [r for r in rows if r["t"]["layout"] == lay]
    yield "scanned (PNG)", [r for r in rows if r["t"]["scanned"]]
    yield "phone photos (JPG)", [r for r in rows if r["t"]["photo"] and not r["t"]["rough"]]
    yield "rough photos: far, shaky (JPG)", [r for r in rows if r["t"]["rough"]]
    yield "2 pages, 30-45 lines", [r for r in rows if r["t"]["pages"] > 1]


def accuracy_table(s: dict) -> str:
    out = ["| Invoices | Fields read right | Invoices read perfectly |", "|---|---|---|"]
    for name, rs in groups(s["rows"]):
        fields = sum(len(FIELDS) - len(r["wrong"]) for r in rs)
        out.append(f"| {name} ({len(rs)}) | {fields}/{len(rs) * len(FIELDS)} ({fields / len(rs) / len(FIELDS):.1%}) "
                   f"| {sum(not r['wrong'] for r in rs)}/{len(rs)} |")
    return "\n".join(out)


def review_numbers(s: dict) -> dict:
    rs = s["rows"]
    planted = [r for r in rs if r["t"]["error"]]
    clean = [r for r in rs if not r["t"]["error"] and not r["wrong"]]  # nothing to catch: any flag is a false alarm
    return {
        "caught": sum(r["flagged"] for r in planted), "planted": len(planted),
        "false_alarms": sum(r["flagged"] for r in clean), "clean": len(clean),
        "silent": sum(1 for r in rs if r["wrong"] and not r["flagged"]),
        "misread": sum(1 for r in rs if r["wrong"]),
        "cost": statistics.mean(r["cost_usd"] for r in rs),
        "latency": statistics.median(r["latency_s"] for r in rs),
    }


def comparison_table(scores: list[dict]) -> str:
    out = [("| Model | Fields read right | Planted mistakes caught | False alarms | Misreads that got through "
           "| Cost per 1,000 invoices | Median time |"), "|---|---|---|---|---|---|---|"]
    for s in scores:
        n = review_numbers(s)
        fields = sum(len(FIELDS) - len(r["wrong"]) for r in s["rows"])
        out.append(f"| {s['model']} | {fields / len(s['rows']) / len(FIELDS):.1%} | {n['caught']}/{n['planted']} "
                   f"| {n['false_alarms']}/{n['clean']} | {n['silent']} of {n['misread']} misread "
                   f"| US$ {n['cost'] * 1000:.2f} | {n['latency']:.0f} s |")
    return "\n".join(out)


def misses(s: dict) -> str:
    return "\n".join(f"- {r['t']['file']} ({r['t']['layout']}): "
                     f"{', '.join(r['wrong'])}{'' if r['flagged'] else ' -- NOT flagged'}"
                     for r in s["rows"] if r["wrong"]) or "- none"


def main() -> None:
    truth = json.loads(Path("samples/truth.json").read_text())
    order = list(extract.PRICES)
    dirs = sorted((d for d in Path("recorded").iterdir() if d.is_dir()),
                  key=lambda d: order.index(d.name) if d.name in order else 99)
    scores = [score(d, truth) for d in dirs if all((d / (Path(t["file"]).stem + ".json")).exists() for t in truth)]
    print(comparison_table(scores))
    for s in scores:
        print(f"\n### {s['model']}\n\n{accuracy_table(s)}\n\nMisread:\n{misses(s)}")


if __name__ == "__main__":
    main()
