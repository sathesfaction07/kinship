"""Regenerates data/cases.jsonl and data/truth.jsonl.

Kept in the repo so the corpus is reproducible and its planted hazards are auditable rather
than mysterious. Every group below exists to exercise a specific failure named in the docs.
"""

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

rng = random.Random(4242)
base = datetime(2026, 3, 2, 9, 0, 0)
cases: list[dict] = []
truth: dict[str, str] = {}
counter = [1000]


def add(entity: str, name: str, region: str, *, cid: str | None = None) -> str:
    counter[0] += 1
    cid = cid or f"C{counter[0]}"
    cases.append({
        "case_id": cid, "region": region, "merchant_name": name,
        "observed_at": base.isoformat(), "page": None,
        "declared_total": None, "status": "open",
    })
    truth[f"{cid}|{region}"] = entity
    return cid


# 1. The same merchant across regions, under local legal forms and local spellings. Each row
#    is defeated by omitting one specific normalisation step -- see normalise.fold.
for entity, members in [
    ("e-northwind", [("Northwind Trading Ltd", "na"), ("Northwind Trading GmbH", "eu"),
                     ("Northwind Trading K.K.", "fe")]),
    ("e-acme", [("ACME Foods Inc", "na"), ("ACME Foods B.V.", "eu"), ("Foods ACME", "fe")]),
    ("e-weissbrau", [("Weissbr\u00e4u Ltd", "na"), ("Wei\u00dfbr\u00e4u GmbH", "eu")]),
    ("e-cafenoir", [("Cafe\u0301 Noir SARL", "eu"), ("Caf\u00e9 Noir Ltd", "na")]),
    ("e-fullwidth", [("\uff21\uff23\uff2d\uff25 Electronics", "fe"),
                     ("ACME Electronics Pte Ltd", "na")]),
    ("e-nbsp", [("Sunrise\u00a0Imports LLC", "na"), ("Sunrise Imports", "eu")]),
    ("e-article", [("The Bluebell Group", "na"), ("Bluebell Group Ltd", "eu")]),
    ("e-punct", [("O'Hara & Sons, Inc.", "na"), ("OHara and Sons", "eu")]),
    ("e-typo", [("Kestrel Logistics", "na"), ("Kestrel Logistcs Ltd", "eu"),
                ("Kestrel Logistics AB", "fe")]),
]:
    for name, region in members:
        add(entity, name, region)

# 2. The chaining hazard. Five spellings, each one edit from the next, spanning two genuinely
#    different merchants. Every adjacent pair clears a 0.85 threshold and the ends do not, so
#    single linkage merges all five while the matcher itself rejects the extremes.
chain = ["Palisade Holdings", "Palisadr Holdings", "Palisadr Holdimgs",
         "Palisadr Holdimgz", "Palisade Holdingz"]
chain_regions = ["na", "eu", "fe", "na", "eu"]
for index, name in enumerate(chain):
    add("e-palisade-a" if index < 3 else "e-palisade-b", name, chain_regions[index])

# 3. A pair that a first-four-characters block would never compare: they diverge at
#    character 4 and share no token once the legal form is stripped.
add("e-nord", "Nordwind AB", "eu")
add("e-nord", "Norwind AB", "na")

# 4. One case_id issued independently in two regions. These are DIFFERENT cases and must not
#    be merged -- the reason identity is composite rather than the id alone.
add("e-shared-na", "Vermillion Retail", "na", cid="C9001")
add("e-shared-eu", "Halcyon Supplies", "eu", cid="C9001")

# 5. Singletons. A merchant seen once is a real entity, so dropping singletons would change
#    the denominator of every metric.
for name, region in [("Ironbark Timber", "na"), ("Zephyr Marine SA", "eu"),
                     ("Quillon Press", "fe"), ("Marigold Textiles", "na"),
                     ("Basalt Mining Pty", "fe"), ("Verdant Farms", "eu")]:
    add(f"e-solo-{name.split()[0].lower()}", name, region)

# 6. Filler with shared second tokens, so token blocking produces realistically sized blocks
#    rather than a block per merchant.
firsts = ["Amber", "Crimson", "Dovetail", "Elmwood", "Fjord", "Granite", "Harrow", "Indigo",
          "Juniper", "Kingfisher", "Lantern", "Meridian", "Nutmeg", "Opal", "Pewter",
          "Quarry", "Rosewood", "Saffron", "Thistle", "Umber", "Vellum", "Willow"]
seconds = ["Trading", "Supplies", "Holdings", "Imports", "Exports", "Logistics", "Foods",
           "Retail"]
for index, first in enumerate(firsts):
    add(f"e-fill-{first.lower()}", f"{first} {seconds[index % len(seconds)]}",
        ["na", "eu", "fe"][index % 3])

# 7. Re-observations: the same (case_id, region) seen again 48 hours later with a new status.
#    Only the later one should survive, and it is the one carrying 'closed'.
for original in [c for c in cases if c["region"] == "na"][:3]:
    cases.append({**original,
                  "observed_at": (base + timedelta(hours=48)).isoformat(),
                  "status": "closed"})

# 8. Pagination per region, then a page dropped from FE.
by_region: dict[str, list[dict]] = {}
for case in cases:
    by_region.setdefault(case["region"], []).append(case)

# Page size differs per region so that FE spans three pages: dropping its MIDDLE page leaves
# a non-contiguous sequence, which is the only completeness signal available without a
# declared total.
page_size = {"na": 8, "eu": 8, "fe": 5}
for region, group in by_region.items():
    for index, case in enumerate(group):
        case["page"] = (index // page_size[region]) + 1
    # The declared total counts DISTINCT cases, not rows -- it is what the platform says it
    # holds, and the platform does not double-count a case just because we fetched it twice.
    distinct = len({(c["case_id"], c["region"]) for c in group})
    if region == "na":
        for case in group:
            case["declared_total"] = distinct
    elif region == "fe":
        # Declared BEFORE the page drop, so FE ends up short by exactly the dropped page.
        for case in group:
            case["declared_total"] = distinct
    # EU declares no total at all, so its completeness is UNVERIFIABLE.

dropped = [c for c in cases if c["region"] == "fe" and c["page"] == 2]
cases = [c for c in cases if not (c["region"] == "fe" and c["page"] == 2)]
for case in dropped:
    truth.pop(f"{case['case_id']}|{case['region']}", None)

rng.shuffle(cases)

root = Path(__file__).parent
with (root / "cases.jsonl").open("w", encoding="utf-8") as handle:
    for case in cases:
        handle.write(json.dumps(case, ensure_ascii=False) + "\n")
with (root / "truth.jsonl").open("w", encoding="utf-8") as handle:
    for compound, entity in sorted(truth.items()):
        case_id, region = compound.split("|")
        handle.write(json.dumps(
            {"case_id": case_id, "region": region, "entity": entity}, ensure_ascii=False
        ) + "\n")

entity_sizes: dict[str, int] = {}
for entity in truth.values():
    entity_sizes[entity] = entity_sizes.get(entity, 0) + 1

print(f"cases.jsonl  {len(cases)} rows ({len(dropped)} withheld with FE page 2)")
print(f"truth.jsonl  {len(truth)} keys -> {len(entity_sizes)} entities")
print(f"  multi-member: {sum(1 for n in entity_sizes.values() if n > 1)}")
print(f"  singletons:   {sum(1 for n in entity_sizes.values() if n == 1)}")
for region in ("na", "eu", "fe"):
    group = [c for c in cases if c["region"] == region]
    pages = sorted({c["page"] for c in group})
    print(f"  {region}: {len(group):>3} cases  pages {pages}  declared {group[0]['declared_total']}")
