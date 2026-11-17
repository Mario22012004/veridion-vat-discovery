"""End-to-end PoC: company -> website -> crawl -> VAT candidates -> checksum -> HMRC -> entity match.

Outputs (data/):
  poc_results.csv            one row per company
  candidates.csv             every 9-digit span harvested, with checksum + HMRC + match outcome
  crawl_log.jsonl            every HTTP request made while discovering / crawling
  false_positives_log.json   Type 1 / Type 2 false positives with evidence
  experiment_summary.json    metrics, confusion matrices, timing, HTTP failure breakdown, examples
"""
from __future__ import annotations

import base64
import csv
import json
import re
import socket
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from common import BOT_UA, DATA_DIR, RateLimiter, extract_postcode, fetch, make_session, save_json, strip_resp
from hmrc_verifier import GENERIC, HMRCVerifier, core_tokens, match_entity, normalize_name
from vat_extractor import extract, mentions_vat_number_text, to_dict

VENDOR_CTX = re.compile(r"design(ed)? by|website by|site by|developed by|powered by|built by|hosted by|"
                        r"agency|paypal|stripe|worldpay|klarna|opayo|sage ?pay|clearpay|finance provided", re.I)
BING_LIMITER = RateLimiter(2.0)
SPECIFIC = ("jsonld", "gb_prefix", "vat_context")  # UK-VAT-specific extraction tiers
WORKERS = 6
MAX_PAGES = 12
LEGAL_LINK_RE = re.compile(r"contact|about|terms|conditions|privacy|legal|imprint|impressum|company-info|"
                           r"cookie|disclaimer|accessibility|policies|t-c|tandc", re.I)
DEFAULT_PATHS = ["/terms", "/terms-and-conditions", "/legal", "/privacy", "/privacy-policy", "/contact",
                 "/contact-us", "/about", "/about-us"]
# Directories, social networks, registries and media that are never a company's own site. Matched on the
# REGISTRABLE domain (exact) or its first label, never as a substring: an earlier unanchored regex
# blocked victrex.com via 'x\.com', fedex.com, craft.co.uk via 'ft\.com', any domain containing '192'.
DIRECTORY_EXACT = {"x.com", "yell.com", "dnb.com", "192.com", "ft.com", "lse.co.uk", "fool.co.uk", "reed.co.uk",
                   "investing.com", "hl.co.uk", "ii.co.uk", "craft.co", "apollo.io", "clutch.co"}
DIRECTORY_LABELS = {
    "companieshouse", "endole", "companycheck", "opencorporates", "bizdb", "companiesintheuk", "checkcompany",
    "linkedin", "facebook", "twitter", "instagram", "youtube", "tiktok", "wikipedia", "bloomberg", "zoominfo",
    "crunchbase", "glassdoor", "indeed", "trustpilot", "duedil", "northdata", "globaldatabase", "cbinsights",
    "companiesdb", "find-company", "bizstats", "amazon", "ebay", "google", "bing", "microsoft", "yelp", "cylex",
    "freeindex", "thomsonlocal", "checkatrade", "mybuilder", "ratedpeople", "houzz", "pomanda", "ukdata",
    "opengovuk", "companieslist", "reuters", "marketscreener", "londonstockexchange", "thegazette", "bizapedia",
    "corporationwiki", "ukcompany", "kompass", "europages", "companiesmadesimple", "creditsafe", "experian",
    "issuu", "scribd", "totaljobs", "cityam", "yahoo", "morningstar", "ajbell", "owler", "rocketreach",
    "signalhire", "leadiq", "lusha", "f6s", "goodfirms", "designrush", "wikidata", "whois"}


def is_directory(host: str) -> bool:
    dom = registrable(host)
    return dom in DIRECTORY_EXACT or dom.split(".")[0] in DIRECTORY_LABELS or dom.endswith("gov.uk")


# ------------------------------------------------------------------ domain discovery
def registrable(host: str) -> str:
    host = host.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "org", "ltd", "plc", "ac", "gov", "me", "net", "com"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def bing_search(session, query: str, log: list) -> list[str]:
    # NB: Bing returns an empty SERP (HTTP 200, no li.b_algo) for scripted clients when cc=/mkt= are set.
    rec = fetch(session, "https://www.bing.com/search", params={"q": query, "setlang": "en-GB"},
                limiter=BING_LIMITER, timeout=20)
    r = rec.get("_resp")
    urls = []
    if r is not None and r.status_code == 200:
        for a in BeautifulSoup(r.text, "lxml").select("li.b_algo h2 a[href]"):
            href = a["href"]
            u = parse_qs(urlparse(href).query).get("u", [""])[0]
            if u.startswith("a1"):
                u = u[2:] + "=" * (-len(u[2:]) % 4)
                try:
                    href = base64.urlsafe_b64decode(u).decode("utf-8", "ignore")
                except Exception:
                    pass
            urls.append(href)
    log.append({**strip_resp(rec), "stage": "bing", "n_results": len(urls)})
    return urls


def clearbit_suggest(session, name: str, log: list) -> list[tuple[str, str]]:
    rec = fetch(session, "https://autocomplete.clearbit.com/v1/companies/suggest", params={"query": name}, timeout=10)
    log.append({**strip_resp(rec), "stage": "clearbit"})
    r = rec.get("_resp")
    try:
        return [(x["name"], x["domain"]) for x in r.json()] if r is not None and r.status_code == 200 else []
    except ValueError:
        return []


WIKIDATA_LIMITER = RateLimiter(1.0)


def wikidata_by_crn(session, crn: str, log: list) -> list[str]:
    """Entity-linked lookup: Wikidata item with Companies House ID (P2622) -> official website (P856)."""
    q = f'SELECT ?w WHERE {{ ?i wdt:P2622 "{crn}" . ?i wdt:P856 ?w }}'
    for attempt in range(3):  # WDQS answers bursts with 429 + Retry-After, and occasionally times out
        rec = fetch(session, "https://query.wikidata.org/sparql", params={"query": q, "format": "json"},
                    headers={"Accept": "application/sparql-results+json", "User-Agent": BOT_UA},
                    limiter=WIKIDATA_LIMITER, timeout=30)
        r = rec.get("_resp")
        if r is not None and r.status_code not in (429, 500, 502, 503, 504):
            break
        retry_after = r.headers.get("Retry-After", "") if r is not None else ""
        time.sleep(min(int(retry_after), 60) if retry_after.isdigit() else 5 * (attempt + 1))
    rec["attempts"] = attempt + 1
    urls = []
    if r is not None and r.status_code == 200:
        try:
            urls = [b["w"]["value"] for b in r.json()["results"]["bindings"]]
        except (ValueError, KeyError):
            pass
    log.append({**strip_resp(rec), "stage": "wikidata", "n_results": len(urls)})
    return urls


def label_similarity(compact: str, label: str) -> float:
    """Similarity of a domain label to the compacted company name. partial_ratio is only allowed when the
    label is not much shorter than the name, so 'london' does not match 'londonelitetraining'."""
    if not compact or len(label) < 2:
        return 0.0
    r = fuzz.ratio(compact, label)
    if len(label) >= 0.6 * len(compact):
        r = max(r, fuzz.partial_ratio(compact, label))
    return round(r, 1)


def score_domain(company: dict, domain: str, html: str | None, final_url: str | None = None) -> dict:
    core = core_tokens(company["company_name"])
    compact = "".join(core)
    final_dom = registrable(urlparse(final_url).netloc) if final_url else domain
    label = final_dom.split(".")[0].replace("-", "")  # score where the request actually landed
    label_sim = label_similarity(compact, label)
    acronym = "".join(t[0] for t in core) == label and len(label) >= 2
    ev = {"domain": domain, "final_domain": final_dom, "label_sim": label_sim, "acronym": acronym}
    if html:
        text = BeautifulSoup(html, "lxml").get_text(" ")
        num = company["company_number"]
        crn_patterns = {num, num.lstrip("0")} if num.isdigit() else {num}
        ev["crn_on_page"] = any(re.search(rf"(?<!\d){re.escape(p)}(?!\d)", text) for p in crn_patterns if len(p) >= 5)
        pc = company.get("postcode") or ""
        ev["postcode_on_page"] = bool(pc) and pc.replace(" ", "").lower() in text.replace(" ", "").lower()
        title = BeautifulSoup(html, "lxml").title
        ev["title_score"] = fuzz.token_set_ratio(" ".join(core), " ".join(core_tokens(title.get_text()))) if title else 0
        ev["name_in_text"] = " ".join(core) in re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower()))
        ev["uk_signal"] = bool(final_dom.endswith(".uk") or extract_postcode(text) or re.search(
            r"\+44|registered in (england|scotland|wales|northern ireland)|united kingdom", text, re.I))
    strong = ev.get("crn_on_page") or ev.get("postcode_on_page")
    named = ev.get("name_in_text")     # full core name phrase on the page (title similarity alone is too lenient)
    if (strong and label_sim >= 40) or (acronym and strong):
        ev["confidence"] = "high"      # registry identifier / registered postcode printed on the site
    elif len(compact) <= 4:
        ev["confidence"] = None        # 'CDM', 'TFCM': too ambiguous without a registry identifier on the page
    elif label_sim >= 80 and named and (ev.get("uk_signal") or (label_sim >= 90 and tld_rank(final_dom) <= 1)):
        ev["confidence"] = "medium"    # name-matching domain + name on page + (UK signal or exact .uk/.com)
    elif label_sim >= 90 and ev.get("uk_signal"):
        ev["confidence"] = "low"       # name-matching UK domain, no on-page name evidence
    else:
        ev["confidence"] = None
    ev["accept"] = ev["confidence"] is not None
    return ev


def guess_domains(name: str) -> list[str]:
    """Name-derived domain guesses (validated later by CRN / postcode / name on the homepage)."""
    toks = [t for t in core_tokens(name) if t.isalnum()]
    if not toks:
        return []
    stems = ["".join(toks), "-".join(toks), "".join(toks) + "ltd"]
    if len(toks) > 1 and len(toks[0]) >= 5 and toks[0] not in GENERIC:
        stems.append(toks[0])
    if len(toks) > 2:
        stems.append("".join(toks[:2]))
    out = []
    for st in dict.fromkeys(stems):
        if 3 <= len(st) <= 40:
            out += [f"{st}.co.uk", f"{st}.com", f"{st}.uk"]
    return out


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def tld_rank(dom: str) -> int:
    if dom.endswith(".uk"):
        return 0
    if dom.endswith(".com"):
        return 1
    if re.search(r"\.(net|org|io|co|tech|global|group|biz|info|ltd|online|site)$", dom):
        return 2
    return 3  # foreign ccTLD (Bing localises results to the client IP)


def query_name(name: str) -> str:
    toks = core_tokens(name)
    suffix = "plc" if re.search(r"\bplc\b|public limited", name, re.I) or "plc" in normalize_name(name).split() else "ltd"
    return " ".join(toks + [suffix])


def discover_domain(session, company: dict, log: list) -> tuple[str | None, str, dict]:
    """Candidate order (most to least entity-specific): Wikidata by CRN -> Clearbit (strict name match)
    -> Bing (label-filtered; scripted SERPs are degraded) -> DNS-resolving name guesses."""
    name = company["company_name"]
    compact = "".join(core_tokens(name))
    cands: list[tuple[str, str]] = []
    for u in wikidata_by_crn(session, company["company_number"], log):
        cands.append((registrable(urlparse(u).netloc), "wikidata_crn"))
    for cname, dom in clearbit_suggest(session, " ".join(core_tokens(name))[:60], log)[:5]:
        if fuzz.ratio("".join(core_tokens(cname)), compact) >= 85:
            cands.append((registrable(dom), "clearbit"))
    bing = []
    for u in bing_search(session, query_name(name), log)[:10]:
        host = urlparse(u).netloc
        if host and not is_directory(host):
            dom = registrable(host)
            if label_similarity(compact, dom.split(".")[0].replace("-", "")) >= 50:
                bing.append((dom, "bing"))
    for dom, src in sorted(bing, key=lambda x: tld_rank(x[0])):  # stable: keeps Bing order within a rank
        if tld_rank(dom) == 3:  # foreign ccTLD: try the UK / .com twin of the same brand label first
            label = dom.split(".")[0]
            cands += [(f"{label}.co.uk", "bing_cctld_swap"), (f"{label}.com", "bing_cctld_swap")]
        cands.append((dom, src))
    guesses = [g for g in guess_domains(name) if resolves(g)]
    log.append({"stage": "domain_guess_dns", "url": name, "status": "resolved" if guesses else "none_resolved",
                "n_results": len(guesses)})
    cands += [(g, "name_guess") for g in guesses]
    seen, tried = set(), []
    for dom, src in cands:
        if dom in seen or is_directory(dom):
            continue
        seen.add(dom)
        if len(tried) >= 8:
            break
        rec = fetch(session, f"https://{dom}/", timeout=10)
        log.append({**strip_resp(rec), "stage": "domain_probe"})
        if rec.get("status") is None and rec.get("error") in ("SSLError", "ConnectionError", "ConnectTimeout"):
            rec = fetch(session, f"http://{dom}/", timeout=10)  # legacy SME sites without valid TLS
            log.append({**strip_resp(rec), "stage": "domain_probe_http"})
        r = rec.get("_resp")
        html = r.text if r is not None and r.status_code == 200 and "html" in rec.get("content_type", "") else None
        ev = {**score_domain(company, dom, html, r.url if r is not None else None), "source": src,
              "http": rec.get("status") or rec.get("error")}
        if r is not None:
            ev["final_url"] = r.url
        if src == "wikidata_crn":  # linked to the exact CRN: trust the entity even if the site blocks bots
            ev["confidence"], ev["accept"] = ev.get("confidence") or "high_registry_link", True
        elif src == "name_guess" and ev.get("confidence") == "low":  # guesses need on-page corroboration
            ev["confidence"], ev["accept"] = None, False
        tried.append(ev)
        if ev["accept"]:
            return dom, src, {"chosen": ev, "tried": tried}
    return None, "", {"tried": tried}


# ------------------------------------------------------------------ crawl + extract
def crawl(session, start_url: str, log: list) -> tuple[list[dict], list]:
    """Crawl homepage + legal/contact pages. start_url is the post-redirect homepage seen during
    discovery, so a domain that forwards to a canonical site (screwfix.eu -> screwfix.com) is followed."""
    pages, cands = [], []
    queue, seen = ["/"], set()
    p0 = urlparse(start_url)
    base = f"{p0.scheme}://{p0.netloc}"
    domain = registrable(p0.netloc)
    while queue and len(pages) < MAX_PAGES:
        path = queue.pop(0)
        url = urljoin(base, path)
        key = urlparse(url).path.rstrip("/").lower() or "/"
        if key in seen:
            continue
        seen.add(key)
        rec = fetch(session, url, timeout=12)
        r = rec.get("_resp")
        if r is not None and r.status_code == 200 and registrable(urlparse(r.url).netloc) != registrable(domain):
            rec["note"] = "offsite_redirect"
        log.append({**strip_resp(rec), "stage": "crawl"})
        pg = {"url": url, "status": rec.get("status"), "error": rec.get("error"), "chars": 0}
        pages.append(pg)
        if r is None or r.status_code != 200 or "html" not in rec.get("content_type", ""):
            continue
        if rec.get("note") == "offsite_redirect":
            pg["error"] = "offsite_redirect"
            continue
        found, text = extract(r.text, r.url)
        pg["chars"] = len(text.strip())
        if pg["chars"] < 200:
            pg["error"] = "thin_or_js_rendered"   # HTTP 200 but no server-side text (SPA / bot interstitial)
        pg["vat_text"] = mentions_vat_number_text(text) or any(c.pattern in SPECIFIC for c in found)
        cands.extend(found)
        if path == "/":
            base = f"{urlparse(r.url).scheme}://{urlparse(r.url).netloc}"
            links = []
            for a in BeautifulSoup(r.text, "lxml").find_all("a", href=True):
                full = urljoin(r.url, a["href"])
                p = urlparse(full)
                hay = p.path + " " + a.get_text()
                if registrable(p.netloc) == registrable(domain) and LEGAL_LINK_RE.search(hay):
                    links.append((link_priority(hay), p.path or "/"))
            ranked = [p for _, p in sorted(links, key=lambda x: x[0])]  # stable: legal first, about last
            queue = list(dict.fromkeys(ranked[:14] + DEFAULT_PATHS))
    return pages, cands


def link_priority(hay: str) -> int:
    h = hay.lower()
    if re.search(r"terms|conditions|legal|imprint|impressum|company-info|t-c|tandc|disclaimer", h):
        return 0
    if re.search(r"privacy|cookie|policies", h):
        return 1
    if "contact" in h:
        return 2
    return 3


# ------------------------------------------------------------------ per-company pipeline
def process(company: dict, verifier: HMRCVerifier) -> dict:
    t0 = time.time()
    s = make_session()
    log: list = []
    res = {k: company[k] for k in ("tier", "company_number", "company_name", "sector_stratum",
                                   "postcode", "latest_accounts_type")}
    dom, src, dom_ev = discover_domain(s, company, log)
    t_disc = time.time() - t0
    res.update(domain=dom or "", domain_source=src,
               domain_confidence=(dom_ev.get("chosen") or {}).get("confidence") or "",
               domain_evidence=json.dumps(dom_ev.get("chosen") or dom_ev.get("tried"))[:600])
    start = (dom_ev.get("chosen") or {}).get("final_url") or f"https://{dom}/"
    pages, cands = (crawl(s, start, log) if dom else ([], []))
    res["crawl_start_url"] = start if dom else ""
    t_crawl = time.time() - t0 - t_disc
    ok_pages = [p for p in pages if p["status"] == 200 and not p.get("error")]
    res.update(pages_attempted=len(pages), pages_ok=len(ok_pages),
               text_chars=sum(p["chars"] for p in ok_pages),
               vat_text_mention=any(p.get("vat_text") for p in ok_pages),
               http_failures=";".join(f"{k}x{v}" for k, v in Counter(
                   str(p["status"] or p["error"]) for p in pages if p not in ok_pages).items()))

    # ---- choose which candidates to verify
    cdicts = [to_dict(c) for c in cands]
    specific = [c for c in cdicts if c["pattern"] in SPECIFIC]
    naive = [c for c in cdicts if c["pattern"] == "naive"]
    to_check = list(dict.fromkeys(c["vrn"] for c in specific))[:8]
    to_check += [v for v in dict.fromkeys(c["vrn"] for c in naive if c["checksum_valid"]) if v not in to_check][:3]
    # one checksum-failing control per company: measures whether HMRC ever accepts what Mod-97 rejects
    to_check += [v for v in dict.fromkeys(c["vrn"] for c in naive if not c["checksum_valid"]) if v not in to_check][:1]
    for v in to_check:
        verifier.check(v)  # network stage only; classification happens in finalize()
    t_ver = time.time() - t0 - t_disc - t_crawl
    res.update(t_discovery_s=round(t_disc, 2), t_crawl_s=round(t_crawl, 2), t_verify_s=round(t_ver, 2),
               elapsed_s=round(time.time() - t0, 2))
    return {"row": res, "candidates": cdicts, "log": [{**x, "company_number": company["company_number"]} for x in log],
            "to_check": to_check, "pages": pages, "company": company, "has_domain": bool(dom),
            "ok_pages": len(ok_pages)}


def finalize(out: dict, verifier: HMRCVerifier) -> dict:
    """Classify a processed company from cached HMRC results (re-run after the name back-off pass)."""
    company, cdicts, pages, res = out["company"], out["candidates"], out["pages"], out["row"]
    specific = [c for c in cdicts if c["pattern"] in SPECIFIC]
    naive = [c for c in cdicts if c["pattern"] == "naive"]
    hmrc = {}
    num = company["company_number"]
    crn_rx = re.compile(rf"(?<!\d)0*{re.escape(num.lstrip('0'))}(?!\d)") if num.isdigit() else re.compile(re.escape(num))
    for v in out["to_check"]:
        h = verifier.cache.get(v, {"outcome": "unverified"})
        m = match_entity(company["company_name"], company.get("postcode"), h.get("name"), h.get("address"))
        snips = " || ".join(c["snippet"] for c in cdicts if c["vrn"] == v)
        m["type2_subtype"] = None
        if h.get("outcome") == "valid" and m["match_class"] != "match":
            if m["match_class"] == "third_party" and crn_rx.search(snips):
                m["match_class"], m["reason"] = "related_entity", "declared_alongside_target_crn"
            if m["match_class"] == "related_entity":
                m["type2_subtype"] = "vat_group_or_parent"
            elif VENDOR_CTX.search(snips):
                m["type2_subtype"] = "third_party_vendor"
            else:
                m["type2_subtype"] = "third_party_unrelated"
        hmrc[v] = {**h, **m}
    for c in cdicts:
        c.update(company_number=company["company_number"], tier=company["tier"])
        h = hmrc.get(c["vrn"])
        c["hmrc_outcome"] = h["outcome"] if h else "not_checked"
        c["hmrc_name"] = h.get("name") if h else None
        c["match_class"] = h.get("match_class") if h else None
        c["type2_subtype"] = h.get("type2_subtype") if h else None
        c["name_score"] = h.get("name_score") if h else None

    valid = {v: h for v, h in hmrc.items() if h["outcome"] in ("valid",)}
    matched = sorted([(h["name_score"], v) for v, h in valid.items() if h["match_class"] == "match"], reverse=True)
    related = [v for v, h in valid.items() if h["match_class"] == "related_entity"]
    third = [v for v, h in valid.items() if h["match_class"] == "third_party"]
    nameless = [v for v, h in valid.items() if h["match_class"] == "unknown"]
    best = matched[0][1] if matched else None
    if not out["has_domain"]:
        outcome = "no_domain_found"
    elif not out["ok_pages"]:
        walled = any(p.get("error") == "thin_or_js_rendered" or p["status"] in (401, 403, 429, 503) for p in pages)
        outcome = "site_js_or_bot_wall" if walled else "site_unreachable"
    elif matched:
        outcome = "verified_match"
    elif related:
        outcome = "related_entity_only"
    elif third:
        outcome = "third_party_only"
    elif nameless:
        outcome = "hmrc_valid_name_throttled"   # validity known from the 303, entity match impossible
    elif specific:
        outcome = "vat_candidates_invalid"
    else:
        outcome = "no_vat_on_site"
    # naive baseline: first VAT-context candidate on the site, taken without checks
    first = specific[0] if specific else None
    if first:
        fh = hmrc.get(first["vrn"], {})
        naive_verdict = ("checksum_fail" if not first["checksum_valid"] else
                         {"valid": f"valid_{fh.get('match_class')}", "not_found": "hmrc_not_found"}.get(fh.get("outcome"), "unverified"))
    else:
        naive_verdict = "none"
    res.update(
        n_spans_total=len(cdicts), n_naive_9digit=len(naive), n_vat_specific=len(specific),
        n_checksum_pass=sum(c["checksum_valid"] for c in cdicts), n_hmrc_checked=len(hmrc),
        n_hmrc_valid=len(valid), verified_vat=f"GB{best}" if best else "",
        hmrc_name=valid[best]["name"] if best else "", hmrc_address=valid[best]["address"] if best else "",
        name_score=valid[best]["name_score"] if best else "", match_class=valid[best]["match_class"] if best else "",
        related_entity_vrns=";".join(f"GB{v} ({valid[v]['name']})" for v in related),
        third_party_vrns=";".join(f"GB{v} ({valid[v]['name']})" for v in third), outcome=outcome,
        naive_baseline_first_vat=f"GB{first['vrn']}" if first else "", naive_baseline_verdict=naive_verdict,
        hmrc_name_throttled_vrns=";".join(f"GB{v}" for v in nameless),
        # evidence only, NOT a match: Screwfix prints its parent VAT group's VRN next to its own CRN
        throttled_vrn_printed_next_to_own_crn=any(
            crn_rx.search(" ".join(c["snippet"] for c in cdicts if c["vrn"] == v)) for v in nameless) if nameless else "")
    return res


# ------------------------------------------------------------------ metrics
def confusion(pairs: list[tuple[bool, bool]]) -> dict:
    tp = sum(p and t for p, t in pairs); fp = sum(p and not t for p, t in pairs)
    fn = sum(not p and t for p, t in pairs); tn = sum(not p and not t for p, t in pairs)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    return {"TP": tp, "FP": fp, "FN": fn, "TN": tn, "precision": round(prec, 3) if prec is not None else None,
            "recall": round(rec, 3) if rec is not None else None, "n": len(pairs)}


def summarise(rows, cands, logs, verifier, wall):
    by_tier = {}
    for t in "ABC":
        tr = [r for r in rows if r["tier"] == t]
        n = len(tr)
        by_tier[t] = {
            "n": n,
            "domain_found": sum(bool(r["domain"]) for r in tr),
            "domain_confidence": dict(Counter(r["domain_confidence"] or "none" for r in tr)),
            "domain_source": dict(Counter(r["domain_source"] or "none" for r in tr)),
            "site_reachable": sum(r["pages_ok"] > 0 for r in tr),
            "vat_text_on_site": sum(bool(r["vat_text_mention"]) for r in tr),
            "verified_match": sum(r["outcome"] == "verified_match" for r in tr),
            "verified_match_rate": round(sum(r["outcome"] == "verified_match" for r in tr) / n, 3) if n else None,
            "outcomes": dict(Counter(r["outcome"] for r in tr)),
        }
    checked = [c for c in {(c["company_number"], c["vrn"]): c for c in cands}.values() if c["hmrc_outcome"] != "not_checked"]
    truth = lambda c: c["hmrc_outcome"] == "valid" and c["match_class"] == "match"
    ladder = []
    stages = [("all_9digit_spans (naive regex)", lambda c: True),
              ("vat_specific_pattern (GB/VAT-context/JSON-LD)", lambda c: c["pattern"] in SPECIFIC),
              ("vat_specific + checksum_valid", lambda c: c["pattern"] in SPECIFIC and c["checksum_valid"]),
              ("+ HMRC valid", lambda c: c["pattern"] in SPECIFIC and c["checksum_valid"] and c["hmrc_outcome"] == "valid"),
              ("+ entity match (pipeline output)", lambda c: c["pattern"] in SPECIFIC and truth(c))]
    uniq = list({(c["company_number"], c["vrn"], c["pattern"]): c for c in cands}.values())
    for label, f in stages:
        kept = [c for c in uniq if f(c)]
        ladder.append({"stage": label, "kept_spans": len(kept), "target_verified": sum(truth(c) for c in kept),
                       "precision": round(sum(truth(c) for c in kept) / len(kept), 3) if kept else None})
    ctx = defaultdict(lambda: {"n": 0, "checksum_pass": 0, "hmrc_valid": 0, "hmrc_checked": 0})
    for c in cands:
        k = ctx[f"{c['pattern']}|{c['context']}"]
        k["n"] += 1; k["checksum_pass"] += c["checksum_valid"]
        k["hmrc_checked"] += c["hmrc_outcome"] != "not_checked"; k["hmrc_valid"] += c["hmrc_outcome"] == "valid"
    naive_all = [c for c in cands if c["pattern"] == "naive"]
    http = Counter()
    for x in logs:
        http[f"{x['stage']}:{x.get('status') or x.get('error')}"] += 1
    hm = Counter(f"{x['step']}:{x.get('status') or x.get('error')}" for x in verifier.http_log)
    return {
        "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_companies": len(rows), "wall_clock_s": round(wall, 1),
        "coverage_by_tier": by_tier,
        "overall": {
            "verified_match": sum(r["outcome"] == "verified_match" for r in rows),
            "verified_match_rate": round(sum(r["outcome"] == "verified_match" for r in rows) / len(rows), 3),
            "outcomes": dict(Counter(r["outcome"] for r in rows)),
            "match_classes_of_assigned": dict(Counter(r["match_class"] for r in rows if r["verified_vat"])),
        },
        "candidate_funnel": {
            "total_9digit_spans": len(cands),
            "naive_spans": len(naive_all),
            "naive_checksum_fail": sum(not c["checksum_valid"] for c in naive_all),
            "naive_checksum_pass": sum(c["checksum_valid"] for c in naive_all),
            "vat_specific_spans": sum(c["pattern"] in SPECIFIC for c in cands),
            "foreign_vat_spans": sum(c["pattern"] == "foreign_vat" for c in cands),
            "unique_vrns_hmrc_checked": len({c["vrn"] for c in checked}),
            "by_pattern_and_context": dict(ctx),
            "theoretical_random_checksum_pass_rate": round(2 / 97, 4),
        },
        "precision_ladder": ladder,
        "confusion_matrices": {
            "checksum_vs_hmrc (unique company-VRN pairs sent to HMRC; truth = HMRC valid)":
                confusion([(c["checksum_valid"], c["hmrc_outcome"] == "valid") for c in checked]),
            "vat_specific_regex_vs_verified_target (truth = HMRC valid AND entity match)":
                confusion([(c["pattern"] in SPECIFIC, truth(c)) for c in checked]),
            "full_pipeline_vs_verified_target":
                confusion([(c["pattern"] in SPECIFIC and c["checksum_valid"] and truth(c), truth(c)) for c in checked]),
            "naive_baseline_first_vat_number_per_company": dict(Counter(r["naive_baseline_verdict"] for r in rows)),
        },
        "timing_s": {k: {"mean": round(sum(r[k] for r in rows) / len(rows), 2), "max": max(r[k] for r in rows)}
                     for k in ("t_discovery_s", "t_crawl_s", "t_verify_s", "elapsed_s")},
        "http_breakdown": {"discovery_and_crawl": dict(http.most_common()), "hmrc_and_fallbacks": dict(hm)},
        "hmrc_outcomes": dict(Counter(h.get("outcome") for h in verifier.cache.values())),
    }


def fp_log(cands, rows):
    t1, t2, rel = [], [], []
    seen = set()
    for c in cands:
        key = (c["company_number"], c["vrn"])
        if key in seen:
            continue
        seen.add(key)
        base = {k: c[k] for k in ("tier", "company_number", "vrn", "pattern", "context", "checksum_valid",
                                  "hmrc_outcome", "hmrc_name", "name_score", "match_class", "type2_subtype",
                                  "page_url", "snippet")}
        base["target_name"] = next(r["company_name"] for r in rows if r["company_number"] == c["company_number"])
        if not c["checksum_valid"]:
            t1.append({**base, "fp_type": "type1_checksum_fail"})
        elif c["hmrc_outcome"] == "not_found":
            t1.append({**base, "fp_type": "type1_checksum_ok_hmrc_not_found"})
        elif c["hmrc_outcome"] == "valid" and c["match_class"] == "third_party":
            t2.append({**base, "fp_type": f"type2_{c['type2_subtype']}"})
        elif c["hmrc_outcome"] == "valid" and c["match_class"] == "related_entity":
            rel.append({**base, "fp_type": "type2_vat_group_or_parent"})
    return {"definitions": {
                "type1": "9-digit number a naive regex would accept as a VAT number but which fails Mod-97/9755 "
                         "or is not found by HMRC",
                "type2_third_party": "HMRC-valid VRN found on the target's site but registered to an unrelated "
                                     "entity (agency / vendor / unrelated)",
                "type2_vat_group_or_parent": "HMRC-valid VRN registered to a different legal name that is a parent / "
                                             "VAT-group representative (shares name tokens or postcode, or is printed "
                                             "next to the target's own CRN). Correct for invoicing in a VAT group, "
                                             "but NOT the target's own registration."},
            "type1": t1, "type2_third_party": t2, "type2_vat_group_or_parent": rel,
            "counts": {"type1": len(t1), "type1_checksum_fail": sum(x["fp_type"] == "type1_checksum_fail" for x in t1),
                       "type1_hmrc_not_found": sum(x["fp_type"] == "type1_checksum_ok_hmrc_not_found" for x in t1),
                       "type2_third_party": len(t2), "type2_vat_group_or_parent": len(rel)}}


STATE_FILE = DATA_DIR / "pipeline_state.json"


def main() -> None:
    """Full run, or `--resume-names`: re-try HMRC names for VRNs whose verdict is known but whose result
    page was throttled, then re-finalize all outputs from the saved crawl state (no re-crawl)."""
    verifier = HMRCVerifier(min_interval=5.0)  # gentle pacing: HMRC throttles result pages under load
    pending_of = lambda: [v for v, h in verifier.cache.items() if h.get("outcome") == "valid" and not h.get("name")]
    if "--resume-names" in sys.argv:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        results, wall = state["results"], state["network_wall_clock_s"]
        verifier.http_log = state["hmrc_http_log"]
        pending = pending_of()
        # observed: ~3 result pages per window, window 10-70+ min -> one round every 10 minutes.
        # --offline re-finalizes outputs from saved state + cache without touching HMRC.
        offline = "--offline" in sys.argv
        resume = verifier.retry_names(pending, waits=() if offline else (600,) * 9)
        if not offline:  # persist every online resume so later re-finalizations keep the full history
            state.setdefault("resume_history", []).append(
                {"run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **resume})
            state["hmrc_http_log"] = verifier.http_log
            save_json(STATE_FILE.name, state)
        backoff = {"initial_run": state["backoff"], "resumes": state.get("resume_history", [])}
        print(f"HMRC name resume: {len(pending)} pending -> {len(resume['still_pending'])} still pending")
    else:
        t0 = time.time()
        with (DATA_DIR / "uk_sample_companies.csv").open(encoding="utf-8") as fh:
            companies = list(csv.DictReader(fh))
        with ThreadPoolExecutor(WORKERS) as ex:
            results = list(ex.map(lambda c: process(c, verifier), companies))
        # HMRC throttles the result page (429) while still returning the 303 verdict -> back-off for names
        pending = pending_of()
        waits = (0,) if "--no-backoff" in sys.argv else (120, 300, 600)
        backoff = verifier.retry_names(pending, waits) if pending else {"attempts": [], "still_pending": []}
        print(f"HMRC name back-off: {len(pending)} pending -> {len(backoff['still_pending'])} still pending")
        wall = time.time() - t0
        save_json(STATE_FILE.name, {"results": results, "network_wall_clock_s": wall, "backoff": backoff,
                                    "hmrc_http_log": verifier.http_log})
    verifier.save_cache()
    write_outputs(results, verifier, wall, backoff)


def write_outputs(results: list, verifier: HMRCVerifier, wall: float, backoff: dict) -> None:
    rows = [finalize(o, verifier) for o in results]
    for i, r in enumerate(rows, 1):
        print(f"[{i:2}/{len(rows)}] {r['tier']} {r['company_name'][:34]:34} {r['domain'][:26]:26} {r['outcome']:26} {r['verified_vat']}")
    cands = [c for o in results for c in o["candidates"]]
    logs = [x for o in results for x in o["log"]]
    with (DATA_DIR / "poc_results.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    if cands:
        with (DATA_DIR / "candidates.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(cands[0].keys())); w.writeheader(); w.writerows(cands)
    with (DATA_DIR / "crawl_log.jsonl").open("w", encoding="utf-8") as fh:
        for x in logs:
            fh.write(json.dumps(x, default=str) + "\n")
    save_json("hmrc_http_log.json", verifier.http_log)
    fps = fp_log(cands, rows)
    save_json("false_positives_log.json", fps)
    summ = summarise(rows, cands, logs, verifier, wall)
    summ["false_positive_counts"] = fps["counts"]
    summ["hmrc_throttling"] = {
        "result_pages_429": sum(x.get("step") == "hmrc_result_page" and x.get("status") == 429 for x in verifier.http_log),
        "result_pages_200": sum(x.get("step") == "hmrc_result_page" and x.get("status") == 200 for x in verifier.http_log),
        "posts": sum(x.get("step") == "hmrc_post" for x in verifier.http_log),
        "backoff_and_resume_attempts_by_result_status": dict(Counter(
            str(a.get("result_page_status")) for a in
            backoff.get("attempts", []) + backoff.get("initial_run", {}).get("attempts", [])
            + [a for r in backoff.get("resumes", []) for a in r.get("attempts", [])])),
        "name_backoff": backoff,
        "note": "POST /enter-vat-details keeps returning 303 -> /known|/unknown while throttled, so validity is "
                "observable; the result page with the registered name returns 429 without Retry-After."}
    summ["examples"] = {
        "verified": [{k: r[k] for k in ("company_name", "domain", "verified_vat", "hmrc_name", "name_score")}
                     for r in rows if r["outcome"] == "verified_match"][:8],
        "type1": fps["type1"][:5], "type2_third_party": fps["type2_third_party"][:5],
        "type2_vat_group_or_parent": fps["type2_vat_group_or_parent"][:5],
    }
    save_json("experiment_summary.json", summ)
    print(json.dumps({"overall": summ["overall"], "fp": fps["counts"],
                      "by_tier": {t: {k: v for k, v in d.items() if k != "outcomes"} for t, d in summ["coverage_by_tier"].items()}}, indent=1))


if __name__ == "__main__":
    main()
