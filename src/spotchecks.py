"""Spot checks used by README sections 2 and 6 (no HMRC traffic).

1. VIES REST: does each member state return the registered name/address for a valid VAT id?
2. Wikidata SPARQL (P2622 Companies House ID -> P856 website): the pipeline got HTTP 403 on all 60
   lookups with a browser-like User-Agent; test whether a descriptive UA (Wikimedia UA policy) works.
Output: data/spotchecks.json
"""
from __future__ import annotations

from common import BOT_UA, fetch, make_session, safe_json, save_json, strip_resp, vies_lookup

VIES_IDS = [  # (country, number, why it is in the list)
    ("FR", "03552081317", "control: SIREN-derived key, seen in part1"),
    ("DE", "814963072", "USt-IdNr printed on cdm.com (CDM Smith SE), seen during PoC development"),
    ("ES", "A28015865", "Telefonica S.A. CIF"),
    ("IT", "00488410010", "Telecom Italia S.p.A. Partita IVA"),
    ("RO", "1590082", "OMV Petrom S.A. CUI"),
    ("XI", "220430231", "Tesco PLC, Northern Ireland protocol registration"),
]
SPARQL = "https://query.wikidata.org/sparql"
Q = 'SELECT ?w WHERE { ?i wdt:P2622 "%s" . ?i wdt:P856 ?w }'


def main() -> None:
    s = make_session()
    vies = []
    for cc, num, why in VIES_IDS:
        rec, j = vies_lookup(s, cc, num)
        vies.append({**strip_resp(rec), "country": cc, "vat": f"{cc}{num}", "why": why,
                     "isValid": j.get("isValid"), "userError": j.get("userError"),
                     "name": j.get("name"), "address": j.get("address")})
    wiki = []
    for label, ua in (("browser_like_ua", s.headers["User-Agent"]), ("descriptive_ua", BOT_UA)):
        for crn in ("01106260", "00445790"):
            rec = fetch(s, SPARQL, params={"query": Q % crn, "format": "json"}, timeout=30,
                        headers={"User-Agent": ua, "Accept": "application/sparql-results+json"})
            j = safe_json(rec.get("_resp"), {}) or {}
            sites = [b["w"]["value"] for b in j.get("results", {}).get("bindings", [])]
            wiki.append({**strip_resp(rec), "ua_variant": label, "crn": crn, "websites": sites})
    save_json("spotchecks.json", {"vies_name_disclosure": vies, "wikidata_ua_test": wiki})
    for v in vies:
        print(f"VIES {v['vat']:14} valid={v['isValid']} name={str(v['name'])[:40]!r}")
    for w in wiki:
        print(f"WIKIDATA {w['ua_variant']:16} crn={w['crn']} status={w.get('status')} sites={w['websites'][:2]}")


if __name__ == "__main__":
    main()
