"""UK VAT candidate extraction + HMRC Modulus 97 / 9755 checksum validation.

Three extraction tiers, from most to least specific:
  jsonld      - schema.org "vatID"/"taxID" values in page markup
  gb_prefix   - "GB" followed by 9 (or 12, branch) digits
  vat_context - 9 digits within 40 non-digit chars after the word "VAT"
  naive       - any free-standing 9-digit run (3-4-2 / 3-3-3 / contiguous), used to measure how
                many non-VAT numbers a naive regex would harvest
Each candidate gets a context class (vat / phone / crn / bank / fca / charity / eori / none)
from the 60 characters preceding it, and checksum flags.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from bs4 import BeautifulSoup

SEP = r"[\s\-. ]?"
DIGIT_RUN9 = rf"(?:\d{SEP}){{8}}\d"  # 9 digits with optional single separators (3-4-2, 3-3-3, contiguous)

GB_RE = re.compile(rf"\bGB{SEP}({DIGIT_RUN9})(?:{SEP}(\d{{3}}))?(?!\d)")
VAT_CTX_RE = re.compile(rf"\bVAT\b[^0-9\n]{{0,40}}?(?:GB{SEP})?({DIGIT_RUN9})(?!\d)", re.I)
NAIVE_RE = re.compile(rf"(?<![\d£$€.,/])({DIGIT_RUN9})(?![\d])")
JSONLD_RE = re.compile(r'"(?:vatID|taxID)"\s*:\s*"([^"]{5,30})"', re.I)
FOREIGN_PREFIX_RE = re.compile(r"\b(AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|"
                               r"SE|SI|SK|CH|NO)[\s\-.]?$")

CONTEXT_RULES = [
    ("vat", re.compile(r"\bvat\b|value added tax|tax id|vat ?id", re.I)),
    ("eori", re.compile(r"\beori\b", re.I)),
    ("phone", re.compile(r"\btel\b|phone|\bfax\b|\bcall\b|mobile|whatsapp|\bt:\s*$|\+44", re.I)),
    ("crn", re.compile(r"company\s*(?:reg|no|number|registration)|registered\s*(?:in|number|no)|\bcrn\b|reg\.?\s*no", re.I)),
    ("bank", re.compile(r"sort\s*code|account|\biban\b|\bbic\b|swift", re.I)),
    ("fca", re.compile(r"\bfca\b|\bfrn\b|financial conduct", re.I)),
    ("charity", re.compile(r"charity", re.I)),
    ("reference", re.compile(r"order|invoice|\bref\b|reference|product|sku|part\s*no", re.I)),
]


def digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def weighted_sum(d9: str) -> int:
    return sum(w * int(c) for w, c in zip((8, 7, 6, 5, 4, 3, 2), d9[:7])) + int(d9[7:9])


def checksum(vrn: str) -> dict:
    """HMRC VRN check-digit rules on the first 9 digits.

    mod97   : (8d1+7d2+...+2d7 + d8d9) % 97 == 0
    mod9755 : (8d1+7d2+...+2d7 + d8d9 + 55) % 97 == 0   (series issued from 2010; prefix >= 100)
    """
    d9 = vrn[:9]
    if len(d9) != 9 or not d9.isdigit():
        return {"mod97": False, "mod9755": False, "valid": False, "rule": None}
    total = weighted_sum(d9)
    m97 = total % 97 == 0
    m9755 = (total + 55) % 97 == 0 and int(d9[:3]) >= 100
    return {"mod97": m97, "mod9755": m9755, "valid": m97 or m9755,
            "rule": "mod97" if m97 else ("mod9755" if m9755 else None)}


def is_valid_vrn(vrn: str) -> bool:
    return checksum(digits(vrn))["valid"]


@dataclass
class Candidate:
    vrn: str              # 9-digit core
    branch: str           # 3-digit branch suffix if present
    raw: str
    pattern: str          # jsonld | gb_prefix | vat_context | naive
    context: str          # vat | phone | crn | bank | ... | none
    mod97: bool
    mod9755: bool
    checksum_valid: bool
    page_url: str
    snippet: str


def classify_context(text: str, start: int) -> str:
    """Label from the keyword nearest to (i.e. last before) the number within 60 chars."""
    window = text[max(0, start - 60):start]
    best, best_pos = "none", -1
    for label, rx in CONTEXT_RULES:
        for m in rx.finditer(window):
            if m.end() > best_pos:
                best, best_pos = label, m.end()
    return best


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    txt = soup.get_text(" ")
    return re.sub(r"[ \t\r\f\v   ]+", " ", txt)


def extract(html: str, page_url: str) -> tuple[list[Candidate], str]:
    """Return (candidates, visible_text). Each 9-digit span is reported once, at its most specific tier."""
    text = page_text(html)
    out: list[Candidate] = []
    claimed: list[tuple[int, int]] = []

    def add(raw, d, branch, pattern, start, end, ctx=None):
        if any(s <= start < e or s < end <= e for s, e in claimed):
            return
        claimed.append((start, end))
        c = checksum(d)
        out.append(Candidate(d[:9], branch or "", raw.strip(), pattern,
                             ctx or classify_context(text, start), c["mod97"], c["mod9755"], c["valid"],
                             page_url, re.sub(r"\s+", " ", text[max(0, start - 60):end + 40]).strip()))

    for m in JSONLD_RE.finditer(html):
        d = digits(m.group(1))
        if len(d) >= 9:
            c = checksum(d)
            out.append(Candidate(d[:9], d[9:12], m.group(1), "jsonld", "vat", c["mod97"], c["mod9755"],
                                 c["valid"], page_url, m.group(0)[:120]))
    for m in GB_RE.finditer(text):
        add(m.group(0), digits(m.group(1)), m.group(2), "gb_prefix", m.start(), m.end(), "vat")
    for m in VAT_CTX_RE.finditer(text):
        if FOREIGN_PREFIX_RE.search(text[max(0, m.start(1) - 4):m.start(1)]):
            # 'VAT ID No. DE81 4963 072' - an EU VAT id whose digits happen to form a 9-digit run
            add(m.group(0), digits(m.group(1)), "", "foreign_vat", m.start(1), m.end(1), "foreign_vat")
        else:
            add(m.group(0), digits(m.group(1)), "", "vat_context", m.start(1), m.end(1), "vat")
    for m in NAIVE_RE.finditer(text):
        add(m.group(0), digits(m.group(1)), "", "naive", m.start(1), m.end(1))
    return out, text


def mentions_vat_number_text(text: str) -> bool:
    return bool(re.search(r"VAT\s*(?:reg(?:istration|istered)?|no|number|id)", text, re.I))


def summarise(cands: list[Candidate]) -> dict:
    """Counts used for the checksum-vs-context experiment."""
    by: dict = {}
    for c in cands:
        k = f"{c.pattern}|{c.context}"
        b = by.setdefault(k, {"n": 0, "checksum_pass": 0})
        b["n"] += 1
        b["checksum_pass"] += int(c.checksum_valid)
    return by


def to_dict(c: Candidate) -> dict:
    return asdict(c)


if __name__ == "__main__":  # self-test with known values
    assert checksum("220430231")["mod97"], "Tesco VRN should pass mod97"
    sample = ("<footer>Registered in England No. 00445790. VAT Reg No: GB 220 4302 31. "
              "Tel 0800 505555. Order ref 123456789. VAT number 987 6543 21</footer>")
    cs, _ = extract(sample, "test")
    for c in cs:
        print(c.pattern, c.context, c.vrn, c.checksum_valid)
    print(json.dumps({"random_9digit_expected_pass_rate": round(2 / 97, 4)}))
