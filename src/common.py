"""Shared HTTP, logging and Companies House helpers."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# Wikimedia's User-Agent policy rejects browser-like/generic UAs on query.wikidata.org (HTTP 403);
# API clients must identify themselves descriptively.
BOT_UA = "veridion-vat-discovery-research/0.2 (research prototype; python-requests)"
CH_WEB = "https://find-and-update.company-information.service.gov.uk"
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    return s


class RateLimiter:
    """Thread-safe minimum interval between calls to the same service."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delta = time.time() - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.time()


def fetch(session: requests.Session, url: str, method: str = "GET", timeout: float = 15,
          limiter: RateLimiter | None = None, **kw) -> dict[str, Any]:
    """HTTP call that never raises; returns a compact log record + response object."""
    if limiter:
        limiter.wait()
    t0 = time.time()
    rec: dict[str, Any] = {"url": url, "method": method}
    try:
        r = session.request(method, url, timeout=timeout, **kw)
        rec.update(status=r.status_code, final_url=r.url, bytes=len(r.content),
                   content_type=r.headers.get("content-type", "")[:60],
                   redirects=[h.status_code for h in r.history])
        rec["_resp"] = r
    except requests.exceptions.SSLError as e:
        rec.update(status=None, error="SSLError", detail=str(e)[:160])
    except requests.exceptions.ConnectTimeout:
        rec.update(status=None, error="ConnectTimeout")
    except requests.exceptions.ReadTimeout:
        rec.update(status=None, error="ReadTimeout")
    except requests.exceptions.ConnectionError as e:
        msg = str(e)
        kind = "DNSFailure" if ("NameResolution" in msg or "getaddrinfo" in msg) else "ConnectionError"
        rec.update(status=None, error=kind, detail=msg[:160])
    except requests.exceptions.RequestException as e:
        rec.update(status=None, error=type(e).__name__, detail=str(e)[:160])
    rec["elapsed_s"] = round(time.time() - t0, 3)
    return rec


def safe_json(r, default=None):
    """JSON body of a 200 response, or `default`. VIES, for one, serves an HTML 'Server temporarily
    unavailable' page with HTTP 200 during outages, so status alone does not guarantee JSON."""
    if r is None or r.status_code != 200:
        return default
    try:
        return r.json()
    except ValueError:
        return default


def body_unavailable(r) -> bool:
    """True when a 200 response is an HTML outage page instead of the expected API payload."""
    return r is not None and r.status_code == 200 and "text/html" in r.headers.get("content-type", "") \
        and "unavailable" in r.text[:2000].lower()


VIES_REST = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{cc}/vat/{num}"
# VIES reports capacity/outage problems as isValid=false + one of these codes; they are NOT verdicts.
VIES_TRANSIENT = {"MS_UNAVAILABLE", "MS_MAX_CONCURRENT_REQ", "GLOBAL_MAX_CONCURRENT_REQ",
                  "SERVICE_UNAVAILABLE", "TIMEOUT", "SERVER_BUSY"}


def vies_lookup(session, cc: str, num: str, attempts: int = 4, wait: float = 30) -> tuple[dict, dict]:
    """VIES REST check with outage handling. Returns (http_record, payload). If every attempt hit an
    outage (HTML 'temporarily unavailable' page, timeout, or a transient userError such as
    MS_MAX_CONCURRENT_REQ), payload is {} and record['vies_unavailable'] is True with the last reason."""
    rec: dict = {}
    reason = None
    for i in range(attempts):
        rec = fetch(session, VIES_REST.format(cc=cc, num=num), timeout=30)
        r = rec.get("_resp")
        j = safe_json(r)
        if isinstance(j, dict) and j.get("userError") not in VIES_TRANSIENT:
            rec["attempts"] = i + 1
            return rec, j
        if isinstance(j, dict):
            reason = j.get("userError")
        elif body_unavailable(r):
            reason = "html_outage_page"
        elif r is None:
            reason = rec.get("error")
        else:
            reason = f"non_json_{r.status_code}"
            break  # a real non-JSON answer; retrying won't help
        if i < attempts - 1:
            time.sleep(wait)
    rec.update(vies_unavailable=True, vies_unavailable_reason=reason, attempts=attempts)
    return rec, {}


def strip_resp(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def save_json(name: str, obj: Any) -> Path:
    p = DATA_DIR / name
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return p


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def extract_postcode(s: str) -> str | None:
    m = POSTCODE_RE.search(s or "")
    return f"{m.group(1).upper()} {m.group(2).upper()}" if m else None


# ---------------------------------------------------------------- Companies House (public web)
CH_LIMITER = RateLimiter(0.6)


def ch_search(session, query: str) -> tuple[dict, list[dict]]:
    rec = fetch(session, f"{CH_WEB}/search/companies", params={"q": query}, limiter=CH_LIMITER)
    out = []
    r = rec.get("_resp")
    if r is not None and r.status_code == 200:
        soup = BeautifulSoup(r.text, "lxml")
        for li in soup.select("ul#results li.type-company"):
            a = li.select_one("h3 a")
            if not a:
                continue
            meta = clean_text(li.select_one("p.meta").get_text(" ")) if li.select_one("p.meta") else ""
            addr = li.find_all("p")
            out.append({
                "company_number": a["href"].rstrip("/").split("/")[-1],
                "company_name": clean_text(a.get_text()),
                "meta": meta,
                "address": clean_text(addr[-1].get_text(" ")) if addr else "",
            })
    return strip_resp(rec), out


def ch_profile(session, number: str) -> tuple[dict, dict]:
    """Parse the public company overview page. Returns (http_log, fields)."""
    rec = fetch(session, f"{CH_WEB}/company/{number}", limiter=CH_LIMITER)
    r = rec.get("_resp")
    f: dict[str, Any] = {"company_number": number}
    if r is None or r.status_code != 200:
        return strip_resp(rec), f
    soup = BeautifulSoup(r.text, "lxml")
    h1 = soup.select_one("p.heading-xlarge") or soup.find("h1")
    f["company_name"] = clean_text(h1.get_text()) if h1 else ""

    def dd(elem_id):
        e = soup.find(id=elem_id)
        return clean_text(e.get_text(" ")) if e else ""
    f["registered_address"] = dd("roa-address")
    f["company_status"] = dd("company-status")
    f["company_type"] = dd("company-type")
    f["incorporation_date"] = dd("company-creation-date")
    f["sic_codes"] = [clean_text(s.get_text(" ")) for s in soup.select("span[id^=sic]")]
    f["postcode"] = extract_postcode(f["registered_address"])
    main = soup.find("main") or soup
    text = main.get_text(" ")
    f["_page_mentions_vat"] = bool(re.search(r"\bVAT\b", text))
    ext = [a["href"] for a in main.find_all("a", href=True)
           if a["href"].startswith("http") and "gov.uk" not in a["href"] and "companieshouse" not in a["href"]]
    f["_external_links"] = ext
    f["_fields_shown"] = sorted({clean_text(dt.get_text()) for dt in main.find_all(["dt", "h2"])})[:40]
    return strip_resp(rec), f


def ch_latest_accounts(session, number: str, max_pages: int = 6) -> str:
    """Latest annual-accounts filing (form type AA) as a size proxy: micro / small / full / dormant / group.

    The filing-history `category` filter is applied client-side, so the server returns all filings;
    busy plcs push accounts past page 1 behind share filings. Page through and match the AA form type
    exactly (AA01 = change of accounting reference date, not accounts)."""
    for page in range(1, max_pages + 1):
        rec = fetch(session, f"{CH_WEB}/company/{number}/filing-history", params={"page": page},
                    limiter=CH_LIMITER)
        r = rec.get("_resp")
        if r is None or r.status_code != 200:
            return "unknown"
        rows = BeautifulSoup(r.text, "lxml").select("table#fhTable tr")[1:]
        if not rows:
            break
        for tr in rows:
            cells = [clean_text(td.get_text(" ")) for td in tr.find_all("td")]
            if len(cells) >= 3 and cells[1] == "AA":
                t = cells[2].lower()
                if t.startswith(("interim", "initial")):  # dividend-purpose filings, not annual accounts
                    continue
                for key in ("micro", "dormant", "total exemption", "small", "abridged", "group", "full", "medium"):
                    if key in t:
                        return key
                return "other"
    return "none_filed"
