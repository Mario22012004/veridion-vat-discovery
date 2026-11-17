"""Build a stratified sample of 60 active UK companies from the live Companies House register.

Tier A (15): well-known mid/large UK suppliers & manufacturers, resolved by name.
Tier B (25): established SMEs (inc. 2000-2016) drawn at random from CH advanced search across
             5 B2B sectors x 5 regions; dormant / micro-entity filers excluded.
Tier C (20): newly incorporated (Jun 2025 - Sep 2026) active Ltd companies drawn at random;
             most have no accounts filed yet and many are likely below the VAT threshold.
"""
from __future__ import annotations

import csv
import random
import re
import sys

from bs4 import BeautifulSoup

from common import (CH_LIMITER, CH_WEB, DATA_DIR, ch_latest_accounts, ch_profile, ch_search,
                    fetch, make_session)

SEED = 20261117

TIER_A = [
    "Renishaw plc", "Rotork plc", "Halma plc", "J.C. Bamford Excavators Limited", "Marshalls plc",
    "Ibstock plc", "Travis Perkins plc", "Howden Joinery Group plc", "Screwfix Direct Limited",
    "RS Components Limited", "Greggs plc", "Costain Group plc", "Morgan Sindall Group plc",
    "Victrex plc", "Bunzl plc",
]

TIER_B_STRATA = {  # sector -> SIC codes
    "construction": ["43210", "43220", "41201", "43390"],
    "it_services": ["62020", "62012", "62090"],
    "wholesale": ["46690", "46900", "46730", "46440"],
    "engineering": ["25620", "28290", "71129", "33120"],
    "business_services": ["82990", "70229", "81210", "74909"],
}
REGIONS = ["Leeds", "Birmingham", "Bristol", "Manchester", "Glasgow", "Nottingham", "Sheffield",
           "Cardiff", "Newcastle", "Leicester", "Norwich", "Southampton"]
TIER_C_SICS = ["62020", "43210", "46900", "70229", "82990", "47910", "74909", "41201", "96090", "73110"]
NON_TRADING = re.compile(r"HOLDING|PROPERT|INVEST|NOMINEE|DORMANT|ESTATES?\b|MANAGEMENT COMPANY|RTM", re.I)


def compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def advanced_search(session, params: dict, page: int = 1) -> list[tuple[str, str]]:
    q = {"status": "active", "type": "ltd", **params}
    if page > 1:
        q["page"] = page
    rec = fetch(session, f"{CH_WEB}/advanced-search/get-results", params=q, limiter=CH_LIMITER, timeout=30)
    r = rec.get("_resp")
    if r is None or r.status_code != 200:
        return []
    main = BeautifulSoup(r.text, "lxml").find("main")
    out, seen = [], set()
    for a in main.find_all("a", href=re.compile(r"^/company/[A-Z0-9]{8}$")):
        n = a["href"].split("/")[-1]
        if n not in seen:
            seen.add(n)
            name = re.sub(r"\(link opens a new window\)", "", a.get_text())
            out.append((n, re.sub(r"\s+", " ", name).strip()))
    return out


def date_params(prefix: str, d: int, m: int, y: int) -> dict:
    return {f"{prefix}Day": d, f"{prefix}Month": m, f"{prefix}Year": y}


def enrich(session, number: str, tier: str, method: str, sector: str = "") -> dict | None:
    _, f = ch_profile(session, number)
    if not f.get("company_name") or f.get("company_status", "").lower() != "active":
        return None
    return {
        "tier": tier, "company_number": number, "company_name": f["company_name"],
        "company_status": f["company_status"], "company_type": f["company_type"],
        "incorporation_date": f["incorporation_date"], "sic_codes": "; ".join(f["sic_codes"]),
        "sector_stratum": sector, "registered_address": f["registered_address"],
        "postcode": f["postcode"] or "", "latest_accounts_type": ch_latest_accounts(session, number),
        "sampling_method": method,
    }


def build() -> list[dict]:
    rng = random.Random(SEED)
    s = make_session()
    rows: list[dict] = []

    # ---- Tier A: resolve by name
    for name in TIER_A:
        _, res = ch_search(s, name)
        pick = next((x for x in res if compact(x["company_name"]) == compact(name)), None)
        pick = pick or next((x for x in res if compact(name)[:6] in compact(x["company_name"])), None)
        row = enrich(s, pick["company_number"], "A", f"CH name search: '{name}'", "large_known") if pick else None
        if row:
            rows.append(row)
        print(f"A  {name[:35]:35} -> {row['company_number'] + ' ' + row['company_name'] if row else 'MISS'}")

    # ---- Tier B: 5 per sector, random region / SIC / result page
    for sector, sics in TIER_B_STRATA.items():
        got, tries = 0, 0
        while got < 5 and tries < 25:
            tries += 1
            sic, region = rng.choice(sics), rng.choice(REGIONS)
            params = {"sicCodes": sic, "registeredOfficeAddress": region,
                      **date_params("incorporationFrom", 1, 1, 2000), **date_params("incorporationTo", 31, 12, 2016)}
            hits = advanced_search(s, params, page=rng.randint(1, 3)) or advanced_search(s, params)
            hits = [h for h in hits if not NON_TRADING.search(h[1]) and h[0] not in {r["company_number"] for r in rows}]
            if not hits:
                continue
            n, _ = rng.choice(hits)
            row = enrich(s, n, "B", f"CH advanced search sic={sic} address~{region} inc=2000-2016", sector)
            if row and row["latest_accounts_type"] not in ("dormant", "micro", "none_filed", "unknown"):
                rows.append(row)
                got += 1
        print(f"B  {sector:18} -> {got}")

    # ---- Tier C: newly incorporated
    got, tries = 0, 0
    while got < 20 and tries < 60:
        tries += 1
        sic = TIER_C_SICS[got % len(TIER_C_SICS)] if tries <= 20 else rng.choice(TIER_C_SICS)
        params = {"sicCodes": sic, **date_params("incorporationFrom", 1, 6, 2025),
                  **date_params("incorporationTo", 30, 9, 2026)}
        hits = advanced_search(s, params, page=rng.randint(1, 20))
        hits = [h for h in hits if not NON_TRADING.search(h[1]) and h[0] not in {r["company_number"] for r in rows}]
        if not hits:
            continue
        n, _ = rng.choice(hits)
        row = enrich(s, n, "C", f"CH advanced search sic={sic} inc=2025-06..2026-09", "new_micro")
        if row and row["latest_accounts_type"] != "dormant":
            rows.append(row)
            got += 1
    print(f"C  newly incorporated -> {got}")
    return rows


def refresh_accounts() -> None:
    """Recompute latest_accounts_type for the existing sample (no redraw) after the AA-parser fix."""
    out = DATA_DIR / "uk_sample_companies.csv"
    with out.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    s = make_session()
    changed = []
    for r in rows:
        new = ch_latest_accounts(s, r["company_number"])
        if new != r["latest_accounts_type"]:
            changed.append((r["tier"], r["company_number"], r["latest_accounts_type"], new))
            r["latest_accounts_type"] = new
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"refreshed accounts type: {len(changed)} changed -> {changed}")


def main() -> None:
    if "--refresh-accounts" in sys.argv:
        refresh_accounts()
        return
    rows = build()
    out = DATA_DIR / "uk_sample_companies.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    tiers = {t: sum(r["tier"] == t for r in rows) for t in "ABC"}
    print(f"wrote {out.name}: {len(rows)} companies {tiers}")
    if len(rows) != 60:
        print("WARNING: sample size != 60", file=sys.stderr)


if __name__ == "__main__":
    main()
