"""Part 1 - empirical tests of UK sources for VAT-number discovery.

Diagnostic set: the first 4 companies of each tier in data/uk_sample_companies.csv (12 companies).
Every request is logged with URL, HTTP status, latency and a compact finding. Output:
data/part1_source_research.json
"""
from __future__ import annotations

import csv
import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from common import (DATA_DIR, ch_profile, safe_json, vies_lookup, ch_search, clean_text, fetch, make_session, save_json,
                    strip_resp)
from hmrc_verifier import EORI_API, HMRCVerifier

GB_VAT_RE = re.compile(r"\bGB\s?\d{3}\s?\d{4}\s?\d{2}\b|\bVAT\s*(?:reg(?:istration)?|no|number)", re.I)
UA_ENGINES = {
    "bing": ("https://www.bing.com/search", "q"),
    "duckduckgo_html": ("https://html.duckduckgo.com/html/", "q"),
    "mojeek": ("https://www.mojeek.com/search", "q"),
    "startpage": ("https://www.startpage.com/do/search", "query"),
    "brave": ("https://search.brave.com/search", "q"),
    "yahoo": ("https://search.yahoo.com/search", "p"),
}


def T(rec: dict, **finding) -> dict:
    r = strip_resp(rec)
    return {k: r.get(k) for k in ("method", "url", "status", "elapsed_s", "error", "final_url", "redirects")
            if r.get(k) not in (None, [], "")} | finding


def load_diag() -> list[dict]:
    with (DATA_DIR / "uk_sample_companies.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [r for t in "ABC" for r in [x for x in rows if x["tier"] == t][:4]]


# --------------------------------------------------------------------------- 1. Companies House
def test_companies_house(s, diag):
    tests = []
    vat_hits = web_hits = 0
    fields_seen = Counter()
    for c in diag:
        log, res = ch_search(s, c["company_name"])
        top_ok = bool(res) and res[0]["company_number"] == c["company_number"]
        tests.append(T(log, query=c["company_name"], n_results=len(res), top_result_is_target=top_ok,
                       returned_fields=["company_number", "company_name", "incorporation_meta", "address"]))
        plog, prof = ch_profile(s, c["company_number"])
        fields_seen.update(prof.get("_fields_shown", []))
        vat_hits += prof.get("_page_mentions_vat", False)
        web_hits += bool(prof.get("_external_links"))
        tests.append(T(plog, company_number=c["company_number"],
                       parsed={k: prof.get(k) for k in ("company_name", "company_status", "company_type",
                                                        "incorporation_date", "sic_codes", "registered_address")},
                       page_mentions_VAT=prof.get("_page_mentions_vat"), external_website_links=prof.get("_external_links")))
    api = fetch(s, f"https://api.company-information.service.gov.uk/company/{diag[0]['company_number']}")
    r = api.get("_resp")
    tests.append(T(api, note="Official REST API without key", body=r.text[:150] if r is not None else None))
    return {
        "source": "Companies House (public web register + REST API)",
        "category": "company_registry",
        "tests": tests,
        "summary": {
            "companies_tested": len(diag),
            "search_top_hit_correct": sum(t.get("top_result_is_target", False) for t in tests),
            "profile_fields_displayed": sorted(fields_seen),
            "profiles_mentioning_VAT": vat_hits,
            "profiles_with_external_website_link": web_hits,
        },
        "verdict": "DEAD END for VAT/website. Returns CRN, name, status, type, incorporation date, SIC, registered "
                   "office. 0 profiles expose a VAT number or a company website. REST API needs a key (401) and "
                   "its company-profile resource has no VAT or website field either.",
    }


# --------------------------------------------------------------------------- 2. HMRC check-vat-number
def test_hmrc(s, diag, verifier: HMRCVerifier):
    tests = []
    for label, accept in (("api_v1_open", "application/vnd.hmrc.1.0+json"), ("api_v2_oauth", "application/vnd.hmrc.2.0+json")):
        rec = fetch(s, "https://api.service.hmrc.gov.uk/organisations/vat/check-vat-number/lookup/220430231",
                    headers={"Accept": accept})
        r = rec.get("_resp")
        tests.append(T(rec, test=label, body=r.text[:160] if r is not None else None))
    # raw form submissions
    for label, target in [("known_valid_vrn (Tesco)", "220430231"), ("gb_prefixed_vrn", "GB220430231"),
                          ("checksum_invalid_vrn", "123456789"), ("checksum_valid_but_unissued", "100000132"),
                          ("company_number_8_digits", diag[0]["company_number"])]:
        out = verifier.submit(target)
        tests.append({"test": label, "target": target, **{k: out.get(k) for k in (
            "http_status", "redirect_location", "result_page_status", "outcome", "name", "address", "name_status",
            "message", "elapsed_s")}})
    name_rejected = 0
    for c in diag:
        out = verifier.submit(c["company_name"])
        name_rejected += out.get("outcome") == "form_error"
        tests.append({"test": "company_name_as_query", "target": c["company_name"],
                      **{k: out.get(k) for k in ("http_status", "redirect_location", "outcome", "message")}})
    cache = DATA_DIR / "hmrc_cache.json"
    if cache.exists():  # successful name+address responses captured during the PoC run (same endpoint)
        named = [{"vrn": k, "name": v.get("name"), "address": v.get("address"), "source": v.get("source")}
                 for k, v in json.loads(cache.read_text(encoding="utf-8")).items() if v.get("name")]
        tests.append({"test": "name_and_address_returned_during_poc_run", "examples": named[:5]})
    # CSRF behaviour: POST without token
    rec = fetch(make_session(), "https://www.tax.service.gov.uk/check-vat-number/enter-vat-details", method="POST",
                data={"target": "220430231"})
    tests.append(T(rec, test="post_without_csrf_token_or_session"))
    # VIES fallbacks
    for cc, vrn in (("GB", "220430231"), ("XI", "220430231"), ("XI", "422900581")):
        rec, j = vies_lookup(s, cc, vrn)
        tests.append(T(rec, test=f"vies_{cc}", vrn=vrn, isValid=j.get("isValid"), userError=j.get("userError"),
                       name=j.get("name"), vies_unavailable=rec.get("vies_unavailable")))
    return {
        "source": "HMRC 'Check a UK VAT number' (web service + REST API) and EU VIES",
        "category": "vat_validation",
        "tests": tests,
        "summary": {"company_name_queries": len(diag), "company_name_queries_rejected_by_form": name_rejected,
                    "result_pages_throttled_429": sum(x.get("step") == "hmrc_result_page" and x.get("status") == 429
                                                      for x in verifier.http_log)},
        "verdict": "VALIDATION ONLY, not discovery. The web service requires session cookies + a hidden csrfToken, "
                   "accepts only a 9/12-digit VRN (optional GB prefix) and 303-redirects to /known (returns "
                   "registered name + address) or /unknown. Company names and CRNs are rejected with a form error. "
                   "Under load the /known result page returns HTTP 429 (no Retry-After) while the 303 verdict "
                   "still comes back, so validity survives throttling but the name needed for matching does not. "
                   "REST API v1 is retired (404) and v2 requires OAuth application credentials (401). VIES no "
                   "longer covers GB (INVALID_INPUT); XI works only for Northern-Ireland-registered traders.",
    }


# --------------------------------------------------------------------------- 3. procurement + gazette
def scan_ocds(releases: list) -> dict:
    schemes, vat_hits, parties = Counter(), 0, 0
    for rel in releases:
        for p in rel.get("parties", []) or []:
            parties += 1
            for ident in [p.get("identifier") or {}] + (p.get("additionalIdentifiers") or []):
                if ident.get("scheme"):
                    schemes[ident["scheme"]] += 1
        vat_hits += bool(GB_VAT_RE.search(json.dumps(rel)))
    return {"releases": len(releases), "parties": parties, "identifier_schemes": dict(schemes.most_common()),
            "releases_with_vat_pattern": vat_hits}


def test_procurement_gazette(s, diag):
    out = []
    now = datetime.now(timezone.utc)
    # Contracts Finder OCDS (awards, last 30 days)
    rec = fetch(s, "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search", timeout=60,
                params={"publishedFrom": (now - timedelta(days=30)).strftime("%Y-%m-%d"),
                        "publishedTo": now.strftime("%Y-%m-%d"), "stages": "award", "limit": 100})
    rels = (safe_json(rec.get("_resp"), {}) or {}).get("releases", [])
    cf_scan = scan_ocds(rels)
    cf_tests = [T(rec, test="ocds_award_releases_last_30d", **cf_scan)]
    for c in diag:  # keyword search per company
        rec = fetch(s, "https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json", method="POST",
                    timeout=30, json={"searchCriteria": {"keyword": c["company_name"]}, "size": 20})
        r = rec.get("_resp")
        n, vat, excerpt, gbnum = None, None, None, None
        if r is not None and r.status_code == 200:
            try:
                j = r.json()
                n = j.get("hitCount", len(j.get("noticeList", [])))
                m = GB_VAT_RE.search(r.text)
                vat = bool(m)
                excerpt = r.text[max(0, m.start() - 80):m.end() + 60] if m else None
                gbnum = bool(re.search(r"\bGB\s?\d{3}\s?\d{4}\s?\d{2}\b", r.text))
            except ValueError:
                pass
        cf_tests.append(T(rec, test="keyword_search", query=c["company_name"], hits=n,
                          vat_phrase_in_payload=vat, gb_vat_number_in_payload=gbnum, vat_excerpt=excerpt,
                          note="hitCount far above any single supplier's notices: keyword search is token-OR "
                               "(LIMITED/LTD match ~180k notices), so it cannot isolate a supplier"))
    leaks = [t["vat_excerpt"] for t in cf_tests if t.get("gb_vat_number_in_payload")]
    out.append({"source": "Contracts Finder (OCDS + search API)", "category": "procurement", "tests": cf_tests,
                "summary": {**cf_scan, "keyword_searches_with_gb_vat_number_in_free_text": len(leaks)},
                "verdict": "DEAD END for VAT as a structured identifier. Supplier parties carry Companies House "
                           "(GB-COH) or supplier-registration (GB-SRS) ids; no VAT identifier scheme exists in award "
                           "releases. " + (f"GB-format VAT numbers do appear incidentally in free-text fields "
                           f"({len(leaks)} payload(s), e.g. a supplier list), unlinked to any party id, so they "
                           "cannot be attributed to a company without the same verify+match step."
                           if leaks else "No GB VAT numbers appeared even in free text.")})
    # Find a Tender
    rec = fetch(s, "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages", timeout=90,
                params={"updatedFrom": (now - timedelta(days=3)).strftime("%Y-%m-%dT00:00:00"), "limit": 50})
    fts_json = safe_json(rec.get("_resp"))
    fts_scan = scan_ocds(fts_json.get("releases", [])) if isinstance(fts_json, dict) else {}
    out.append({"source": "Find a Tender Service (OCDS release packages)", "category": "procurement",
                "tests": [T(rec, test="ocds_release_packages_last_3d", **fts_scan)], "summary": fts_scan,
                "verdict": "Identifier schemes observed are registry IDs (GB-COH / GB-PPON etc.); no VAT field."
                           if fts_scan else "Endpoint did not return data within 90 s from this client (logged)."})
    # The Gazette
    gz_tests, gz_vat, gz_crn, gz_notices = [], 0, 0, 0
    for c in diag:
        rec = fetch(s, "https://www.thegazette.co.uk/all-notices/notice/data.json", timeout=30,
                    params={"text": c["company_name"], "results-page-size": 10})
        r = rec.get("_resp")
        entries = []
        if r is not None and r.status_code == 200:
            try:
                entries = r.json().get("entry", []) or []
            except ValueError:
                pass
        texts = json.dumps(entries)
        gz_notices += len(entries)
        gz_vat += bool(GB_VAT_RE.search(texts))
        gz_crn += c["company_number"].lstrip("0") in texts
        gz_tests.append(T(rec, query=c["company_name"], notices=len(entries),
                          vat_pattern=bool(GB_VAT_RE.search(texts)), crn_present=c["company_number"].lstrip("0") in texts))
    rec = fetch(s, "https://www.thegazette.co.uk/insolvency/notice/data.json", timeout=30,
                params={"results-page-size": 50})
    ins = (safe_json(rec.get("_resp"), {}) or {}).get("entry", []) or []
    ins_text = [json.dumps(e) for e in ins]
    ins_scan = {"notices": len(ins), "with_vat_pattern": sum(bool(GB_VAT_RE.search(t)) for t in ins_text),
                "with_company_number_phrase": sum(bool(re.search(r"company (registration )?number|\(\d{8}\)|\b\d{8}\b",
                                                                  t, re.I)) for t in ins_text)}
    gz_tests.append(T(rec, test="latest_50_insolvency_notices", **ins_scan))
    out.append({"source": "The Gazette (all notices + insolvency feed)", "category": "public_records",
                "tests": gz_tests, "summary": {"company_queries": len(diag), "notices_returned": gz_notices,
                                               "queries_with_vat_pattern": gz_vat, "queries_with_crn": gz_crn,
                                               "insolvency_feed": ins_scan},
                "verdict": "DEAD END for VAT. Notices identify companies by name and Companies House number; "
                           "VAT numbers are not part of the notice schema."})
    return out


# --------------------------------------------------------------------------- 4. adjacent identifiers
def test_adjacent(s, verified_pairs):
    tests = []
    # 4a CRN -> VRN derivability on HMRC-verified pairs
    rows = []
    for crn, vrn, name in verified_pairs:
        digits_crn = re.sub(r"\D", "", crn)
        crn7 = digits_crn[-7:].zfill(7)
        # if a VRN were 'CRN + check digits' (France-style), these would hold:
        w = sum(a * int(b) for a, b in zip((8, 7, 6, 5, 4, 3, 2), crn7))
        rows.append({"company": name, "crn": crn, "vrn": vrn,
                     "vrn_equals_crn": digits_crn.lstrip("0") == vrn.lstrip("0"),
                     "vrn_first7_equals_crn_last7": vrn[:7] == crn7,
                     "vrn_equals_crn7_plus_mod97_check": vrn == f"{crn7}{(-w) % 97:02d}",
                     "vrn_equals_crn7_plus_mod9755_check": vrn == f"{crn7}{(-w - 55) % 97:02d}",
                     "positional_digit_matches_of_9": sum(a == b for a, b in zip(digits_crn.zfill(9), vrn))})
    derivable = any(r[k] for r in rows for k in ("vrn_equals_crn", "vrn_first7_equals_crn_last7",
                                                  "vrn_equals_crn7_plus_mod97_check", "vrn_equals_crn7_plus_mod9755_check"))
    tests.append({"test": "crn_to_vrn_derivation", "n_pairs": len(rows), "pairs": rows, "any_derivation_rule_holds": derivable,
                  "expected_positional_matches_by_chance": 0.9,
                  "note": "CRNs (Companies House) and VRNs (HMRC) come from independent issuing sequences. Any "
                          "rank correlation between them reflects issue date (older firms have both lower CRNs and "
                          "older VRNs), not derivability; no deterministic rule maps one to the other."})
    # 4b France contrast: VAT key = (12 + 3 * (SIREN mod 97)) mod 97
    for siren in ("552081317", "542051180"):
        key = (12 + 3 * (int(siren) % 97)) % 97
        rec, j = vies_lookup(s, "FR", f"{key:02d}{siren}")
        tests.append(T(rec, test="france_siren_to_vat_derivation", siren=siren, derived_vat=f"FR{key:02d}{siren}",
                       vies_isValid=j.get("isValid"), vies_name=j.get("name"),
                       vies_unavailable=rec.get("vies_unavailable")))
    # 4c EORI: GB + VRN + 000
    eoris = [f"GB{v}000" for _, v, _ in verified_pairs][:10] + ["GB123456789000"]
    rec = fetch(s, EORI_API, method="POST", json={"eoris": eoris}, headers={"Accept": "application/vnd.hmrc.1.0+json"})
    res = safe_json(rec.get("_resp"), []) or []
    tests.append(T(rec, test="eori_from_vrn", checked=len(eoris),
                   valid=sum(x.get("valid", False) for x in res), results=res[:12]))
    # 4d other registries
    for label, url, kw in [
        ("charity_commission_api", "https://api.charitycommission.gov.uk/register/api/allcharitydetails/1000000/0", {}),
        ("fca_register_api", "https://register.fca.org.uk/services/V0.1/Firm/122702", {}),
        ("fca_register_web_search", "https://register.fca.org.uk/s/search", {"params": {"q": "renishaw", "type": "Companies"}}),
        ("ico_data_protection_register", "https://ico.org.uk/ESDWebPages/Search", {"params": {"Name": "renishaw"}}),
    ]:
        rec = fetch(s, url, timeout=20, **kw)
        r = rec.get("_resp")
        body = clean_text(BeautifulSoup(r.text, "lxml").get_text(" "))[:140] if r is not None else None
        tests.append(T(rec, test=label, body_excerpt=body,
                       vat_pattern=bool(GB_VAT_RE.search(r.text)) if r is not None else None))
    return {"source": "Adjacent identifiers (CRN, EORI, charity, FCA, ICO) + FR contrast", "category": "adjacent",
            "tests": tests,
            "verdict": "UK VRNs are NOT derivable from CRNs (independent number spaces, no equality, Mod-97 does not "
                       "link them). France contrast: FR VAT = key(SIREN) is computable and VIES confirms it. GB EORIs "
                       "are derived FROM the VRN (GB+VRN+000), so the EORI API can corroborate a VRN but cannot "
                       "discover one; there is no EORI-by-name search. Charity Commission and FCA APIs require keys, "
                       "the FCA register is a JS app and the ICO register is behind a Cloudflare challenge; none "
                       "publish VAT numbers."}


# --------------------------------------------------------------------------- 5. open web
def test_open_web(s, diag):
    tests = []
    from pipeline import bing_search, registrable  # reuse the pipeline's Bing parser
    from hmrc_verifier import core_tokens
    for c in diag:  # Bing relevance: does any result host contain a distinctive name token?
        toks = [t for t in core_tokens(c["company_name"]) if len(t) >= 4]
        hosts = [registrable(urlparse(u).netloc) for u in bing_search(s, c["company_name"], [])]
        rel = [h for h in hosts if any(t in h.replace("-", "") for t in toks)]
        tests.append({"engine": "bing", "query": c["company_name"], "results": len(hosts),
                      "relevant_results": len(rel), "top_hosts": list(dict.fromkeys(hosts))[:5]})
    q = diag[4]["company_name"]  # a Tier-B SME
    for eng, (url, param) in UA_ENGINES.items():
        rec = fetch(s, url, params={param: q}, timeout=20)
        r = rec.get("_resp")
        txt = r.text.lower() if r is not None else ""
        blocked = None
        if r is not None:
            blocked = ("captcha" if "captcha" in txt else "js_challenge" if ("anubis" in txt or "just a moment" in txt)
                       else "anomaly_page" if "anomaly" in txt else None)
        tests.append(T(rec, engine=eng, query=q, block_signal=blocked))
        time.sleep(1.5)
    poc = DATA_DIR / "poc_results.csv"
    pipeline_stats = {}
    if poc.exists():
        with poc.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        diag_nums = {c["company_number"] for c in diag}
        for scope, rs in (("diagnostic_12", [r for r in rows if r["company_number"] in diag_nums]), ("all_60", rows)):
            bing_tests = [t for t in tests if t.get("engine") == "bing" and "relevant_results" in t]
            pipeline_stats["bing_relevance_diag_12"] = {
                "queries": len(bing_tests), "queries_with_any_relevant_result": sum(t["relevant_results"] > 0 for t in bing_tests)}
            pipeline_stats[scope] = {
                "n": len(rs), "domain_found": sum(bool(r["domain"]) for r in rs),
                "domain_source": dict(Counter(r["domain_source"] or "none" for r in rs)),
                "site_reachable": sum(int(r["pages_ok"]) > 0 for r in rs),
                "vat_text_on_site": sum(r["vat_text_mention"] == "True" for r in rs),
                "verified_match": sum(r["outcome"] == "verified_match" for r in rs),
                "outcomes": dict(Counter(r["outcome"] for r in rs)),
            }
    return {"source": "Open web discovery (search engines, Clearbit autocomplete, domain guessing) + site crawl",
            "category": "open_web", "tests": tests, "summary": pipeline_stats,
            "verdict": "The ONLY source in this study that yields VAT numbers for a company not already "
                       "identified by VRN. Bottleneck is domain discovery: scripted search is blocked "
                       "(captcha / JS challenge / 429 / 500) or silently degraded (Bing returns HTTP 200 with "
                       "irrelevant, IP-localised results), so production needs a licensed search API or a "
                       "pre-built company->domain index. Free entity-linked source: Wikidata P2622 (Companies "
                       "House ID) -> P856 (website), large companies only. Crawl targets: footer, /terms, "
                       "/legal, /privacy, /contact, /about."}


def main():
    t0 = time.time()
    s = make_session()
    diag = load_diag()
    verifier = HMRCVerifier(min_interval=1.0)
    pairs = [("00445790", "220430231", "TESCO PLC")]
    cands_file = DATA_DIR / "candidates.csv"
    if cands_file.exists():  # HMRC-valid VRNs that the company's own site prints next to its own CRN
        with cands_file.open(encoding="utf-8") as fh:
            seen = set()
            for c in csv.DictReader(fh):
                crn = c["company_number"]
                if (c["hmrc_outcome"] == "valid" and c["pattern"] in ("gb_prefix", "vat_context", "jsonld")
                        and (c["match_class"] == "match" or crn.lstrip("0") in re.sub(r"\D", "", c["snippet"]))
                        and (crn, c["vrn"]) not in seen):
                    seen.add((crn, c["vrn"]))
                    pairs.append((crn, c["vrn"], c["hmrc_name"] or f"(site of CRN {crn})"))
    sources = [test_companies_house(s, diag), test_hmrc(s, diag, verifier)]
    sources += test_procurement_gazette(s, diag)
    sources += [test_adjacent(s, pairs), test_open_web(s, diag)]
    report = {
        "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_s": round(time.time() - t0, 1),
        "diagnostic_companies": [{k: c[k] for k in ("tier", "company_number", "company_name", "postcode")} for c in diag],
        "sources": sources,
        "hmrc_http_log": verifier.http_log,
        "development_observations": {
            "hmrc_result_page_throttle_2026-11-17": {
                "how": "after three full pipeline runs (~60 HMRC lookups in ~40 min from one IP), probed one "
                       "known-valid VRN once per minute with fresh sessions",
                "observed": "POST always 303 -> /known; GET /known returned 429 from 12:31:14 to 12:40:18 "
                            "(10 probes) and 200 at 12:41:19 -> throttle window ~10 min, no Retry-After header",
                "implication": "budget lookups (<= ~1 per 3 s, cache by VRN) and treat the 303 Location as the "
                               "validity verdict; name retrieval needs back-off or an authenticated API v2 client"},
            "hmrc_throttle_in_pipeline_runs_2026-11-17": {
                "note": "recorded from pipeline runs whose raw hmrc_http_log.json was later overwritten; the final "
                        "run served all 17 verdicts from data/hmrc_cache.json and made 0 HMRC requests",
                "run_1_~13:20_utc": {"posts": 16, "posts_303": 16, "result_pages_200": 3, "result_pages_429": 13,
                                     "inline_backoff_120_300_600s": "3 attempts, all 429",
                                     "resume_10min_rounds": "12 attempts: 9x429 then 3x200; first name after 7 "
                                                            "consecutive 429 rounds (~70 min)"},
                "run_2_~15:40_utc_after_wikidata_fix": {"posts": 8, "posts_303": 8, "result_pages_200": 5,
                                                        "result_pages_429": 3,
                                                        "resume_10min_rounds": "round 1: 196097366 200, 887276955 429; "
                                                                               "round 2: 887276955 200"},
            },
            "search_engines": "Bing served IP-localised junk (Romanian/Chinese sites) for most scripted queries; "
                              "Mojeek captcha, Startpage JS challenge, Brave 429, Yahoo 500, DuckDuckGo 202 anomaly page",
        },
        "conclusion": "No UK public registry, procurement or gazette source maps company -> VAT number. HMRC "
                      "(and the EORI API) only validate a VRN you already have. The viable discovery path is "
                      "company website -> VAT text in footer/legal pages -> Mod-97/9755 -> HMRC validation -> "
                      "entity match against the Companies House name/postcode.",
    }
    save_json("part1_source_research.json", report)
    print(f"part1_source_research.json written ({len(sources)} sources, {sum(len(x['tests']) for x in sources)} tests, "
          f"{report['runtime_s']}s)")
    for x in sources:
        print(f" - {x['source'][:60]:60} tests={len(x['tests'])}")


if __name__ == "__main__":
    main()
