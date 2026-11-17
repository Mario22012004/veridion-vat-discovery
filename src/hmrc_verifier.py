"""Verify UK VRNs against HMRC's public 'Check a UK VAT number' service + entity matching.

Observed behaviour (Nov 2026):
  * HMRC REST API v1 (api.service.hmrc.gov.uk/organisations/vat/check-vat-number/lookup/{vrn})
    is retired -> 404 MATCHING_RESOURCE_NOT_FOUND; v2 requires OAuth app credentials -> 401.
  * Public web service: GET /check-vat-number/enter-vat-details (sets mdtp/mdtpdi cookies and a
    hidden csrfToken) then POST csrfToken+target -> 303 to /known (valid; shows name + address)
    or /unknown (not found). Input must be a VRN; free text returns the form with a validation error.
  * Throttling: after a few dozen lookups from one IP the /known and /unknown result pages return
    HTTP 429 (no Retry-After), while the POST still returns the 303 verdict. So validity stays
    observable, but the registered name (needed for entity matching) does not -> back-off retry pass.
  * Fallbacks: VIES REST (country XI) answers only for Northern-Ireland-registered traders;
    HMRC EORI open API confirms GB<VRN>000 EORIs (validity only, no name).
"""
from __future__ import annotations

import json
import re
import threading
import time

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from common import (DATA_DIR, RateLimiter, clean_text, extract_postcode, fetch, make_session, safe_json,
                    strip_resp, vies_lookup)

HMRC_FORM = "https://www.tax.service.gov.uk/check-vat-number/enter-vat-details"
EORI_API = "https://api.service.hmrc.gov.uk/customs/eori/lookup/check-multiple-eori"
CACHE_FILE = DATA_DIR / "hmrc_cache.json"


class HMRCVerifier:
    def __init__(self, min_interval: float = 1.0):
        self.s = make_session()
        self.limiter = RateLimiter(min_interval)
        self.lock = threading.Lock()
        self.submit_lock = threading.Lock()
        self.cache: dict = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
        self.http_log: list[dict] = []

    def _log(self, rec: dict, step: str) -> dict:
        r = strip_resp(rec)
        r["step"] = step
        self.http_log.append(r)
        return r

    def submit(self, target: str) -> dict:
        """Raw form submission (any string). Serialised: CSRF token is bound to the shared session."""
        with self.submit_lock:
            return self._submit(target)

    def _submit(self, target: str) -> dict:
        g = fetch(self.s, HMRC_FORM, limiter=self.limiter, timeout=20)
        self._log(g, "hmrc_get_form")
        resp = g.get("_resp")
        if resp is None or resp.status_code != 200:
            return {"outcome": "error", "http_status": g.get("status"), "error": g.get("error", "form_get_failed")}
        tok = BeautifulSoup(resp.text, "lxml").find("input", {"name": "csrfToken"})
        if not tok:
            return {"outcome": "error", "http_status": resp.status_code, "error": "no_csrf_token"}
        # POST without auto-redirect: the 303 Location carries HMRC's verdict even when the result page
        # itself is throttled (observed: POST 303 -> GET /known returns HTTP 429, no Retry-After header).
        p = fetch(self.s, HMRC_FORM, method="POST", timeout=20, allow_redirects=False,
                  data={"csrfToken": tok["value"], "target": target, "requester": ""})
        rec = self._log(p, "hmrc_post")
        r = p.get("_resp")
        out = {"http_status": rec.get("status"), "elapsed_s": rec.get("elapsed_s")}
        if r is None:
            return {**out, "outcome": "error", "error": rec.get("error")}
        loc = r.headers.get("Location", "")
        out["redirect_location"] = loc
        if r.status_code == 303 and loc.endswith(("/known", "/unknown")):
            verdict = "valid" if loc.endswith("/known") else "not_found"
            res = fetch(self.s, "https://www.tax.service.gov.uk" + loc, timeout=20)
            rr = self._log(res, "hmrc_result_page")
            out["result_page_status"] = rr.get("status")
            if verdict == "not_found":
                return {**out, "outcome": "not_found"}
            page = res.get("_resp")
            if page is None or page.status_code != 200:
                return {**out, "outcome": "valid", "name": None, "address": None, "name_status": "result_page_throttled"}
            main = BeautifulSoup(page.text, "lxml").find("main")
            text = clean_text(main.get_text(" ")) if main else ""
            m = re.search(r"Registered business name (.*?) Registered business address (.*?) Search completed", text)
            return {**out, "outcome": "valid", "name": m.group(1) if m else None,
                    "address": m.group(2) if m else None, "name_status": "ok" if m else "unparsed"}
        main = BeautifulSoup(r.text, "lxml").find("main")
        text = clean_text(main.get_text(" ")) if main else ""
        if r.status_code == 429 or "too many" in text.lower():
            return {**out, "outcome": "rate_limited"}
        err = re.search(r"There is a problem (.{0,160})", text)
        return {**out, "outcome": "form_error" if err else "unexpected",
                "message": err.group(1) if err else text[:200]}

    def retry_names(self, vrns: list[str], waits=(120, 300, 600)) -> dict:
        """Back-off pass for VRNs whose verdict is known but whose result page was throttled."""
        log = {"attempts": []}
        pending = [v for v in vrns if self.cache.get(v, {}).get("outcome") == "valid" and not self.cache[v].get("name")]
        for w in waits:
            if not pending:
                break
            time.sleep(w)
            for v in list(pending):
                res = self.submit(v)
                log["attempts"].append({"vrn": v, "after_wait_s": w, "result_page_status": res.get("result_page_status"),
                                        "name_status": res.get("name_status")})
                if res.get("name"):
                    self.cache[v] = {**res, "source": "hmrc_web_backoff_retry"}
                    pending.remove(v)
                elif res.get("result_page_status") == 429:
                    break  # still throttled: wait for the next back-off step
        log["still_pending"] = pending
        return log

    def check(self, vrn: str) -> dict:
        """Verify a 9-digit VRN with caching and fallbacks."""
        with self.lock:
            if vrn in self.cache:
                return {**self.cache[vrn], "cached": True}
        res = self.submit(vrn)
        res["source"] = "hmrc_web"
        if res["outcome"] in ("error", "rate_limited", "unexpected"):
            time.sleep(5)
            retry = self.submit(vrn)
            if retry["outcome"] in ("valid", "not_found", "form_error"):
                res = {**retry, "source": "hmrc_web_retry"}
            else:
                res = {**self.fallback(vrn), "hmrc_error": res}
        with self.lock:
            self.cache[vrn] = res
        return res

    def fallback(self, vrn: str) -> dict:
        v, j = vies_lookup(self.s, "XI", vrn, attempts=1)
        self._log(v, "vies_xi")
        if j.get("isValid"):
            return {"outcome": "valid", "name": j.get("name"), "address": j.get("address"), "source": "vies_xi"}
        e = fetch(self.s, EORI_API, method="POST", timeout=20, json={"eoris": [f"GB{vrn}000"]},
                  headers={"Accept": "application/vnd.hmrc.1.0+json"})
        self._log(e, "eori_api")
        res = safe_json(e.get("_resp"), []) or []
        if res and res[0].get("valid"):
            return {"outcome": "valid_eori_only", "name": None, "address": None, "source": "eori_api"}
        return {"outcome": "unverified", "source": "fallbacks_exhausted"}

    def save_cache(self) -> None:
        CACHE_FILE.write_text(json.dumps(self.cache, indent=1), encoding="utf-8")


# ------------------------------------------------------------------------ entity matching
SUFFIXES = {"ltd", "limited", "plc", "llp", "lp", "company", "co", "the", "uk", "gb", "group", "holdings",
            "international", "and", "of", "t", "a", "ta"}
GENERIC = {"services", "solutions", "systems", "engineering", "construction", "consulting", "consultancy",
           "trading", "supplies", "building", "technology", "technologies", "management", "global",
           "electrical", "developments", "contractors", "enterprises", "industries", "europe", "british"}
ABBREV = {"svcs": "services", "servs": "services", "svc": "services", "bros": "brothers", "intl": "international",
          "eng": "engineering", "mgmt": "management", "tech": "technology", "elec": "electrical",
          "cons": "construction", "assoc": "associates"}


def normalize_name(name: str) -> str:
    n = (name or "").lower().replace("&", " and ").replace("+", " and ")
    n = re.sub(r"\bt/a\b.*$", "", n)                         # drop "trading as ..." tail
    n = re.sub(r"\bp\s*\.?\s*l\s*\.?\s*c\b\.?", " plc ", n)   # P L C / p.l.c.
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b([a-z])\s(?=[a-z]\b)", r"\1", n)           # "j c b" -> "jcb"
    toks = [ABBREV.get(t, t) for t in n.split()]
    return " ".join(toks)


def core_tokens(name: str) -> list[str]:
    return [t for t in normalize_name(name).split() if t not in SUFFIXES]


def compact_core(name: str) -> str:
    c = re.sub(r"[^a-z0-9]", "", normalize_name(name))
    for _ in range(2):
        c = re.sub(r"(limited|ltd|plc|llp|group|holdings|uk|company|co)$", "", c)
    return re.sub(r"^the", "", c)


def match_entity(target_name: str, target_postcode: str | None, hmrc_name: str | None,
                 hmrc_address: str | None) -> dict:
    if not hmrc_name:
        return {"match_class": "unknown", "name_score": None, "postcode_match": None}
    a, b = " ".join(core_tokens(target_name)), " ".join(core_tokens(hmrc_name))
    # token_sort (not token_set): extra distinctive tokens must cost score, otherwise a subsidiary such as
    # 'MARSHALLS MONO LTD' scores 100 against 'MARSHALLS PLC'. The compact comparison absorbs HMRC's
    # fixed-width line-wrap artefacts ('CONSTR UCTION LIMI TED').
    score = max(fuzz.token_sort_ratio(a, b), fuzz.ratio(compact_core(target_name), compact_core(hmrc_name)))
    hp = extract_postcode(hmrc_address or "")
    pc_full = bool(hp and target_postcode and hp.replace(" ", "") == target_postcode.replace(" ", ""))
    pc_out = bool(hp and target_postcode and hp.split()[0] == target_postcode.split()[0])
    distinctive = {t for t in core_tokens(target_name) if len(t) >= 4 and t not in GENERIC}
    shared = sorted(distinctive & set(core_tokens(hmrc_name)))
    compact_sim = fuzz.ratio(compact_core(target_name), compact_core(hmrc_name))
    # distinctive tokens present on only one side ('mono' in MARSHALLS MONO LTD) signal a different entity
    extra = sorted(t for t in set(core_tokens(hmrc_name)) ^ set(core_tokens(target_name))
                   if len(t) >= 3 and t not in GENERIC)
    if compact_sim >= 95 or (not extra and (score >= 88 or (score >= 70 and pc_out))):
        cls = "match"
    elif pc_full or shared or score >= 60:
        cls = "related_entity"      # VAT-group representative, parent, trading name: needs review
    else:
        cls = "third_party"         # Type 2 false positive if taken as the target's VAT number
    return {"match_class": cls, "name_score": round(score, 1), "compact_sim": round(compact_sim, 1),
            "postcode_match": pc_full, "outward_code_match": pc_out, "shared_tokens": shared,
            "unmatched_distinctive_tokens": extra, "hmrc_postcode": hp}


if __name__ == "__main__":
    print(match_entity("J Smith Building Services Ltd", "LS1 4AP", "J. Smith Building Svcs Limited", "1 X ST LEEDS LS1 4AP"))
    print(match_entity("Acme Widgets Ltd", "LS1 4AP", "PIXEL WEB DESIGN LTD", "BRISTOL BS1 5TR"))
    v = HMRCVerifier()
    print(v.check("220430231"))
