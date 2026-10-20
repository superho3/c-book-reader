import csv
import json
import re
import pathlib
from collections import Counter

root = pathlib.Path(r"c:/Users/HO3/VS_GTDM/SW_CODE/C-BOOK/BX726/HMYJ2_BX726E_6MX65_125PS")
src = root / "HMYJ2.c"
modules = {p.name.upper() for p in root.iterdir() if p.is_dir()}

fn_re = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_\*\s]*\b([A-Za-z0-9_]+)\s*\(")
call_re = re.compile(r"\b([A-Za-z0-9]+)_[A-Za-z0-9_]+\s*\(")
current = None
edges = Counter()

with src.open("r", errors="ignore") as f:
    for line in f:
        m = fn_re.match(line)
        if m:
            fn = m.group(1)
            mm = re.match(r"^([A-Za-z0-9]+)_", fn)
            if mm and mm.group(1).upper() in modules:
                current = mm.group(1).upper()
        if current:
            for cm in call_re.finditer(line):
                dst = cm.group(1).upper()
                if dst in modules and dst != current:
                    edges[(current, dst)] += 1

out = sorted(edges.items(), key=lambda x: x[1], reverse=True)
outdeg = Counter()
indeg = Counter()
for (a, b), c in out:
    outdeg[a] += c
    indeg[b] += c

print("TOTAL_MODULES", len(modules))
print("TOTAL_RELATIONS", len(out))
print("TOP_OUT")
for m, c in outdeg.most_common(20):
    print(m, c)

print("TOP_IN")
for m, c in indeg.most_common(20):
    print(m, c)

print("TOP_PAIRS")
for (a, b), c in out[:120]:
    print(f"{a}->{b} {c}")

edge_csv = pathlib.Path("c:/Users/HO3/VS_GTDM/SW_CODE/Result-MD/HMYJ2_module_edges.csv")
node_csv = pathlib.Path("c:/Users/HO3/VS_GTDM/SW_CODE/Result-MD/HMYJ2_module_nodes.csv")
graph_json = pathlib.Path("c:/Users/HO3/VS_GTDM/SW_CODE/Result-MD/HMYJ2_module_graph.json")

with edge_csv.open("w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["from_module", "to_module", "call_count"])
    for (a, b), c in out:
        writer.writerow([a, b, c])

all_modules = sorted(modules)
with node_csv.open("w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["module", "out_calls", "in_calls"])
    for m in all_modules:
        writer.writerow([m, outdeg.get(m, 0), indeg.get(m, 0)])

payload = {
    "total_modules": len(modules),
    "total_relations": len(out),
    "edges": [{"from": a, "to": b, "count": c} for (a, b), c in out],
    "nodes": [
        {"module": m, "out_calls": outdeg.get(m, 0), "in_calls": indeg.get(m, 0)}
        for m in all_modules
    ],
}

with graph_json.open("w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
