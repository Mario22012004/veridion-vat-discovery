# UK VAT Identifier Discovery — Phase 1 findings

Research, experiments and a working proof of concept for attaching UK VAT registration numbers (VRNs)
to Companies House entities. Everything below was produced by the code in `src/` against live sources
on 17 November 2026. Every number cites a file in `data/`.

**Final artifacts:**

| Artifact | Timestamp (UTC) |
|---|---|
| Pipeline run (`experiment_summary.json`) | `2026-11-17T16:40:09` |
| Part 1 run (`part1_source_research.json`) | `2026-11-17T16:45:45` |
| Spot checks (`spotchecks.json`) | same afternoon |

**Short version.** Discovery runs company → website → VAT text → checksum → HMRC → entity match. On a
stratified sample of 60 active UK companies it attached a VAT number, verified at HMRC as the
company's *own* registration, to **6 companies (10.0%, 95% CI 4.7–20.1%)**. Three more companies
publish a real, HMRC-valid number that belongs to a *different* legal entity (parent or VAT group). A
naive "first VAT-looking number on the site" approach would have shipped those 3 as matches.

The binding constraints are:
- **Domain resolution:** 30 of 60 companies had no website the pipeline could confirm.
- **HMRC throughput:** the public checker withholds registered names after a handful of lookups per
  IP.
- **Group VAT registrations.**

The extraction regex and the checksum are not what limits results.

---

## Contents

1. [Problem framing and the core asymmetry](#1-problem-framing-and-the-core-asymmetry)
2. [Part 1 — source evaluation and dead ends](#2-part-1--source-evaluation-and-dead-ends)
3. [Part 2 — proof of concept](#3-part-2--proof-of-concept)
4. [Part 3 — what I would do with real resources](#4-part-3--what-i-would-do-with-real-resources)
5. [Debate topics](#5-debate-topics)
6. [Beyond the UK](#6-beyond-the-uk)
7. [Repository guide and reproducibility](#7-repository-guide-and-reproducibility)

---

## 1. Problem framing and the core asymmetry

The brief gives roughly **4.2M live Companies House entities** against roughly **2.18M UK VAT
registrations**. I did not re-measure either figure.

The VAT figure includes sole traders and partnerships, which have no Companies House number. So
*fewer* than 2.18M of the 4.2M companies can hold a VRN, and at least half of active UK companies have
no VAT number to find. These are companies below the £90,000 taxable-turnover registration threshold,
companies making only exempt supplies, dormant companies and holding vehicles.

"No VAT number" is therefore a correct answer for a large share of the register, not a pipeline
failure. The sample shows how skewed this is by company age and size:
- **Tier C** (20 companies incorporated Jul 2025–Feb 2026): 18 have no accounts filed yet and **0 of
  20** show a VAT number anywhere we could reach.
- **Tier A** (15 large suppliers): 7 of 15 publish one (`data/poc_results.csv`).

The two kinds of error are not symmetric:

| Error | What the customer sees | Cost |
|---|---|---|
| Missing VAT (abstain) | An empty field. The invoice join falls back to name/address matching or a human. | Visible, countable, recoverable. |
| Wrong VAT (false positive) | A valid-looking `GB` number that passes Mod-97 and HMRC, attached to the wrong CRN. | Silent. ERP and invoice joins succeed and attach invoices to the wrong legal entity. Nothing downstream flags it. |

This sample shows how easily the second kind happens. Of the 10 companies whose site showed a
VAT-specific number, taking the first one without checks gives this
(`experiment_summary.json → confusion_matrices.naive_baseline_first_vat_number_per_company`):
- **6 correct**
- **3 HMRC-valid numbers registered to a different legal entity** (Screwfix, Marshalls, Costain)
- **1 number that fails the checksum** (Pure Table Top's own footer)

All three wrong-entity numbers pass a "is it a valid VAT number?" test. The design principle follows
from this: **precision first, abstain freely**. Every attached VRN must be valid at HMRC *and*
registered to the same legal entity as the CRN.

One reframing for the 40,000-invoice use case. A valid UK VAT invoice must show the supplier's VRN.
If the invoices are available, the cheaper problem is the reverse direction: read the VRN off the
invoice, look it up at HMRC to get the registered name and address, and match that to Companies House.
That is exactly the verification-and-match half of this pipeline, with no web discovery. The rest of
this document covers the harder forward direction (CRN → VRN), which the brief asks for.

---

## 2. Part 1 — source evaluation and dead ends

**Diagnostic set:** the first 4 companies of each tier in `data/uk_sample_companies.csv`:
- Tier A: Renishaw, Rotork, Halma, J.C. Bamford Excavators
- Tier B: Stapleford Plumbing Services, Milncroft Construction, Electrodex, Armson Electrical Services
- Tier C: Alligra, RL Electrical Energy Services, Unikraft, Papillon Consulting

All results are in `data/part1_source_research.json` (102 logged tests). The VIES name-disclosure and
Wikidata User-Agent checks are in `data/spotchecks.json`.

### 2.1 Source comparison

| Source | What I sent | What came back | Coverage on test batch | Verdict |
|---|---|---|---|---|
| Companies House web search | Company name, `GET /search/companies?q=` | 1–20 results with CRN, name, incorporation date, address | Top hit was the target for 11/12. Rotork missed: query `ROTORK P.L.C.` | Entity resolution only |
| Companies House profile page | `GET /company/{CRN}` | Status, type, incorporation date, SIC, registered office, accounts / confirmation-statement dates | **0/12 mention VAT; 0/12 link a website** | Dead end for VAT and domain |
| Companies House REST API | `GET /company/{CRN}` without key | `401` | — | Needs a key; the company-profile resource has no VAT or website field |
| HMRC check-vat-number (web) | VRN through form (GET → csrfToken → POST) | `303` to `/known` (name + address) or `/unknown` | 12/12 company-name queries rejected (`400`, "Enter the UK VAT number … in the correct format"). CRN `01106260` also rejected. `GB220430231` → `TESCO PLC`, CIRRUS A SHIRE PARK … AL7 1GA | Validation only. Cannot be queried by name or CRN |
| HMRC VAT API v1 | `GET /organisations/vat/check-vat-number/lookup/220430231` | `404 MATCHING_RESOURCE_NOT_FOUND` | — | Retired |
| HMRC VAT API v2 | Same, `Accept: application/vnd.hmrc.2.0+json` | `401 MISSING_CREDENTIALS` | — | Needs OAuth app credentials. This is the production route |
| EU VIES | `GB/220430231`, `XI/220430231`, `XI/422900581` | GB: `INVALID_INPUT`. XI Tesco: valid, `TESCO PLC`. XI Renishaw: `INVALID` | — | GB is out of VIES since Brexit; XI covers only Northern Ireland traders |
| Contracts Finder OCDS | Award releases, last 30 days, `limit=100` | 100 releases, 218 parties. Identifier schemes: `GB-COH` 45, `GB-SRS` 39, `GB-LAE` 5, `GB-GOR` 1 | 0 releases with a VAT identifier | Dead end (structured) |
| Contracts Finder keyword search | `POST /api/rest/2/search_notices/json`, company name | `hitCount` 25,572–221,981 per name; 3/12 timed out at 30 s | 1 payload contained a VAT number as free text (see 2.2) | Token-OR search; cannot isolate a supplier |
| Find a Tender OCDS | Release packages, last 3 days, `limit=50` | 50 releases, 227 parties. `GB-PPON` 205, `GB-COH` 29, `GB-NHS` 1, `GB-CHC` 1, `GB-MPR` 1 | 0 with a VAT identifier | Dead end |
| The Gazette, all notices | `data.json?text={name}` | 112 notices for 12 queries | CRN present in 10/12 result sets; VAT pattern 0/12 | Dead end |
| The Gazette, insolvency feed | Latest 50 notices | 50 notices | 35 carry a company-number phrase; 0 a VAT number | Dead end |
| HMRC EORI API | `POST check-multiple-eori` with `GB{VRN}000` | `valid` true/false, no name | 6/8 known VRNs have a matching EORI (Footprint Software and Little Fire Digital do not); control `GB123456789000` invalid | Corroboration only, one way |
| Charity Commission API | `allcharitydetails` | `401` missing subscription key | — | Key needed; charities only |
| FCA Register | API `Firm/122702`; web search | API `403` "Missing Headers"; web is a JS app ("Sorry to interrupt CSS Error") | — | Key + JS; no VAT field |
| ICO register | `ESDWebPages/Search?Name=` | `403` Cloudflare "Just a moment…" | — | Bot wall; no VAT field |
| Search engines | Company name | Bing `200`; DuckDuckGo `202` anomaly page; Mojeek `403`; Startpage JS challenge; Brave `429` captcha; Yahoo `500` | Bing: 2/12 queries returned any result whose host contains a name token | Unusable without a licensed API |
| Wikidata (P2622 → P856) | SPARQL by CRN, descriptive User-Agent | Official website URL | 60/60 lookups HTTP 200; website for 5 CRNs, all Tier A; all 5 became the accepted domain. A browser-like UA gets `403` | Precise but brand-only |
| Clearbit autocomplete | Core name tokens | Name + domain | 16 of 30 accepted domains | Works for brands; ambiguous for SMEs |
| Company website crawl | Footer + legal / T&Cs / privacy / contact / about pages | VAT-specific text on 10 of 30 reachable sites | See Part 2 | The only discovery source that worked |

### 2.2 Dead ends, with the evidence

**Companies House gives zero VAT numbers and zero websites.**
- All 12 diagnostic profile pages were parsed. The fields displayed are: Registered office address,
  Company status, Company type, Incorporated on, Accounts, Confirmation statement, Nature of business
  (SIC), Previous company names.
- No profile mentions "VAT" and none links to an external site (`profiles_mentioning_VAT: 0`,
  `profiles_with_external_website_link: 0`).
- The register is the right *spine* (CRN, legal name, registered postcode) and the wrong *source*.

**Procurement data identifies suppliers by registry id, not VAT number.**
- Contracts Finder parties use `GB-COH` (Companies House) and `GB-SRS` (supplier registration).
  Find a Tender has moved almost entirely to `GB-PPON`: 205 of 237 identifiers.
- No release in either feed carried a VAT identifier scheme.
- A Contracts Finder keyword search for "HALMA PUBLIC LIMITED COMPANY" returned a payload with a
  free-text supplier list: `…MCKENNA SECURITY LTD,Constant Security Service,GB753091928,The ExcluSec
  Group Ltd…`. `753091928` passes Mod-97, so it is probably a real VRN, but it sits in a comma-separated
  string with no party id. Attributing it to "Constant Security Service" would need the same
  verify-and-match step as a web scrape, on a far thinner base rate.
- The keyword search itself is token-OR: `hitCount` was 174,441–221,981 for 7 of the 9 names that
  returned, because `LIMITED` matches everything.

**The Gazette uses Companies House numbers.**
- CRNs were present in 10 of 12 query result sets.
- 35 of the latest 50 insolvency notices contain a company-number phrase; 0 of 162 notices contained a
  VAT pattern.
- Insolvency notices are also a *negative* signal (companies leaving the market). They are useful for
  invalidating VAT records, not discovering them.

**A CRN cannot be turned into a VRN.**
- The two numbers come from different issuers with different formats. CRNs are 8-character strings
  issued by Companies House, with jurisdiction prefixes: the sample has 11 `SC…` and 1 `NI…`.
  VRNs are 9-digit numbers issued by HMRC with Mod-97/9755 check digits.
- On all 8 CRN/VRN pairs available, I tested whether the VRN equals the CRN, equals CRN digits 2–8
  plus Mod-97 or Mod-9755 check digits, or shares a 7-digit core. **No rule held for any pair**
  (`crn_to_vrn_derivation.any_derivation_rule_holds: false`):

  | Company | CRN | VRN |
  |---|---|---|
  | Tesco | `00445790` | `220430231` |
  | Renishaw | `01106260` | `422900581` |
  | Travis Perkins | `00824821` | `408556737` |
  | Screwfix | `03006378` | `232555575` (the Kingfisher group number) |
  | RS Components | `01002091` | `243164091` |
  | Victrex | `02793780` | `604782539` |
  | Footprint Software | `05246159` | `887276955` |
  | Little Fire Digital | `09201920` | `196097366` |

- Positional digit agreement was 0–3 of 9, against 0.9 expected by chance.
- An early draft reported a Pearson r between CRN and VRN; I removed it. Both numbers grow with issue
  date, so a correlation measures company age, not derivability.
- France is the counter-example. `FR` + key + SIREN with key = (12 + 3 × (SIREN mod 97)) mod 97
  produced `FR03552081317` and `FR59542051180`. VIES confirmed both as valid: `ELECTRICITE DE FRANCE`,
  `SE TOTALENERGIES SE`.

**Adjacent registries confirm, they don't discover.**
- A GB EORI is `GB` + VRN + `000`, so the EORI API can corroborate a VRN you already have. It
  confirmed 6 of 8 known VRNs.
- The EORI test fails silently for companies that don't trade goods across the border. Footprint
  Software (`887276955`) and Little Fire Digital (`196097366`) are HMRC-valid but have no EORI.
- No EORI service searches by name.
- Charity Commission, FCA and ICO registers sit behind keys, JS apps or Cloudflare, and none publish
  VAT numbers.

### 2.3 Where open-web crawling works and where it breaks

**What the law requires sites to show.**
- The Companies (Trading Disclosures) Regulations 2008 require a company's website to show its
  registered name, number, part of the UK registered in, and registered office address.
- The Electronic Commerce (EC Directive) Regulations 2002 (reg. 6) add the VAT number where the
  service provider's activity is subject to VAT.

**What I observed.**
- The *CRN* disclosure is the useful one for **entity checking**: 12 of 30 accepted homepages
  printed the target CRN, and a further 6 printed the registered postcode.
- *VAT* disclosure is weaker in practice: **10 of 30 reachable sites** (33%, 95% CI 19–51%) printed
  a VAT-specific number.

**Where it works.** Established B2B and e-commerce sites with a legal footer or T&Cs page:

| Company | Where | Text found | HMRC registered name |
|---|---|---|---|
| Victrex | `victrex.com/en/terms-of-use` | `Our VAT number is GB 604 7825 39` | `VICTREX PLC` |
| Travis Perkins | `travisperkinsplc.co.uk/about-us/terms-of-use/` | `Our VAT number is 408556737` | `TRAVIS PERKINS PLC` |
| RS Components | `uk.rs-online.com` JSON-LD | `"vatID":"GB 243 1640 91"` | `RS COMPONENTS LIMITED` |
| Little Fire Digital | JSON-LD | `"vatID":"196097366"` | `LITTLE FIRE DIGITAL LIMITED` |
| Footprint Software | page text | `Registered in England & Wales : Number : 5246159 : VAT : 887 2769 55` | `FOOTPRINT SOFTWARE LTD` |

**Where it breaks, with cases from the final run:**
- **No findable site.** 30/60 companies, including 15/20 of Tier C and 13/25 of Tier B.
  Stapleford Plumbing Services (`07911459`) and Electrodex (`05349397`) have no domain any method
  could confirm. For Tier C this is often the true state: a company incorporated in 2025 with no
  accounts filed and no web presence.
- **Brand ≠ legal name.** J.C. Bamford Excavators Limited (`00561597`) trades as JCB. Wikidata has
  no website for its CRN, and Bing returned `wikipedia.org`, `youtube.com`, `just.ro` and
  `dailymotion.com`.
- **Domains that may belong to someone else.** 12 of the 30 accepted domains rest on name evidence
  only (11 medium, 1 low confidence) and were not manually verified. Examples:
  `capitalgrowth.org` for CAPITAL & GROWTH LIMITED; `unikraft.io` for a Scottish company
  incorporated in 2025.
- **Bot walls and legacy TLS.**
  - 9 domain probes and 5 crawl requests returned `403`.
  - 10 probes failed with `SSLError` and 13 with `ConnectTimeout`.
  - 14 probes only succeeded over plain `http://`; Footprint Software's site is one of them.
  - The thin-page detector (HTTP 200 with <200 characters of server-side text) fired on 0 pages in
    the final run.
- **Group and investor sites amplify noise.**
  - `halma.com` produced 28 nine-digit false positives: 27 phone numbers, mostly international,
    from subsidiary contact lists, plus one Google Analytics ID.
  - `travisperkinsplc.co.uk/investors/analyst-consensus` produced 9 more from tables of
    three-figure £m forecasts (`£m 121 116 129`).
- **Third-party VAT numbers.** The case I built a classifier for (web agency or payment processor VAT
  in the footer) did **not occur** in this sample (`type2_third_party: 0`). The third-party numbers
  that did occur were all corporate parents or VAT groups (3.4).

---

## 3. Part 2 — proof of concept

### 3.1 Sample construction

`src/sample_builder.py` draws all 60 companies from the live Companies House register (seed
`20261117`). Every company is `Active` at sampling time.

| Tier | n | How drawn | Profile (`latest_accounts_type`) |
|---|---|---|---|
| A | 15 | Hand-picked names resolved by CH search: Renishaw, Rotork, Halma, JCB, Marshalls, Ibstock, Travis Perkins, Howden Joinery, Screwfix, RS Components, Greggs, Costain, Morgan Sindall, Victrex, Bunzl | 12 group, 3 full; incorporated 1894–2015 |
| B | 25 | CH advanced search: 5 sectors (construction, IT, wholesale, engineering, business services) × random SIC × random regional city × random result page 1–3, incorporated 2000–2016 | 18 total-exemption, 4 full, 2 abridged, 1 medium |
| C | 20 | CH advanced search: incorporated Jun 2025–Sep 2026, 10 SIC codes, random result page 1–20 | 18 no accounts filed yet, 1 total-exemption, 1 micro; 19/20 incorporated Jul–Sep 2025, 1 Feb 2026 |

**Why this is better than a list of famous brands.**
- Tiers B and C are drawn from the *register*, not from the web. A company with no website is a
  measured outcome (`no_domain_found`), not a company that was never sampled.
- A sample of FTSE names would measure only the top of the funnel: Tier A reaches 4/15 verified,
  against 0/20 for Tier C.

**Biases I know about.**
- Tier A is hand-picked by design.
- Tier B excludes dormant, micro-entity and no-accounts filers, and names matching
  `HOLDING|PROPERT|INVEST|NOMINEE|…`. That makes Tier B *more* web-visible than the typical SME, so
  the Tier B numbers are optimistic.
- Companies House's advanced-search ordering is not documented, so "random page 1–3" is not a uniform
  draw. Tier C clustered in Jul–Sep 2025 as a result.

### 3.2 Pipeline architecture

```
Company name + CRN + registered postcode (Companies House)
  │
  ├─ 1. Domain resolution          src/pipeline.py: discover_domain()
  │     Wikidata P2622→P856 (descriptive UA; retries 429/5xx/timeouts) → Clearbit autocomplete
  │     (name ratio ≥85) → Bing (label-similarity ≥50; ccTLD → .co.uk/.com swap)
  │     → DNS-resolving name guesses.
  │     Directory, social and registry hosts are excluded by exact registrable domain (is_directory()).
  │     Each candidate homepage is fetched and scored (score_domain()):
  │       high   = target CRN or registered postcode printed on the page
  │       medium = domain label ≥80 similar + full name on page + UK signal (or exact .uk/.com)
  │       low    = label ≥90 + UK signal, no name evidence
  │     Names of ≤4 characters (CDM, TFCM) require high. Guessed domains require ≥ medium.
  │
  ├─ 2. Targeted crawl              crawl(): ≤12 pages per site
  │     Homepage → same-site links ranked terms/legal > privacy/cookie > contact > about
  │     → defaults /terms /terms-and-conditions /legal /privacy /privacy-policy /contact /about.
  │     Follows the homepage redirect to the canonical host (rscomponents.co.uk → uk.rs-online.com).
  │     A page with <200 chars of server-side text is marked thin_or_js_rendered.
  │
  ├─ 3. Extraction + checksum       src/vat_extractor.py
  │     Tiers: jsonld (vatID/taxID) > gb_prefix (GB + 9/12 digits)
  │            > vat_context ("VAT" within 40 chars) > naive (any free-standing 9-digit run)
  │     EU-prefixed ids (DE/FR/PL…) → foreign_vat, never treated as UK
  │     Context label from the nearest keyword in the preceding 60 chars (vat/phone/crn/bank/…)
  │     Mod-97:   (8d1+7d2+…+2d7 + d8d9) mod 97 = 0
  │     Mod-9755: (… + 55) mod 97 = 0, first three digits ≥ 100
  │
  ├─ 4. HMRC verification           src/hmrc_verifier.py (verdicts cached by VRN in hmrc_cache.json)
  │     GET form (mdtp cookies + hidden csrfToken) → POST without auto-redirect
  │     → 303 Location = verdict (/known | /unknown) → GET result page → parse name + address.
  │     Result page 429 → keep verdict, mark name pending, back off,
  │     then resumable retry (--resume-names).
  │
  └─ 5. Entity resolution           match_entity() + finalize()
        Normalise (P L C→plc, Svcs→services, &→and, strip suffixes; drop t/a tails);
        score = max(token_sort_ratio, compact-string ratio) — the compact ratio absorbs HMRC's
        line-wrap artefacts ("CONSTR UCTION LIMI TED").
        match           compact ≥95, or no unmatched distinctive token and (score ≥88 or ≥70 + outward postcode)
        related_entity  shared distinctive token / same full postcode / score ≥60
                        / VRN printed next to the target's own CRN → VAT group or parent
        third_party     everything else; sub-typed vendor (design/powered by/payment context) or unrelated
        Only `match` populates verified_vat.
```

### 3.3 Results

Sources: `data/experiment_summary.json`, `data/poc_results.csv`.

- **Wall-clock:** 137.9 s for 60 companies with 6 workers. Mean per-company time was 12.62 s
  (max 62.21 s): discovery 10.82 s, crawl 1.80 s.
- **HMRC:** the final run made **0 requests**. All 17 company–VRN verdicts came from
  `hmrc_cache.json`, which holds 18 verdicts from live lookups earlier the same day: 13 obtained
  directly, 5 through the back-off/resume path.

**Funnel by tier**

| | Tier A (n=15) | Tier B (n=25) | Tier C (n=20) | All (n=60) |
|---|---|---|---|---|
| Domain accepted | 13 | 12 | 5 | 30 (50%) |
| — high / medium / low confidence | 7 / 6 / 0 | 8 / 4 / 0 | 3 / 1 / 1 | 18 / 11 / 1 |
| Site reachable (≥1 page with text) | 13 | 12 | 5 | 30 |
| VAT-specific text on site | 7 | 3 | 0 | 10 |
| HMRC-valid VRN found | 7 | 2 | 0 | 9 |
| **Verified as the company's own VRN** | **4 (26.7%)** | **2 (8.0%)** | **0** | **6 (10.0%)** |
| HMRC-valid, other legal entity (group/parent) | 3 | 0 | 0 | 3 |
| Invalid VAT on own site | 0 | 1 | 0 | 1 |

- **Domain sources** for the 30 accepted domains: Clearbit 16, DNS name guess 7, Wikidata 5, Bing 2.
- **95% CIs:** all 4.7–20.1%; Tier A 10.9–52.0%; Tier B 2.2–25.0%; Tier C 0–16.1%. n=60 supports
  order-of-magnitude claims, not tier-level precision.

**All six verified attachments** (`verified_vat`; every HMRC name scored 100 against the CH name):

| CRN | Company | Tier | Domain (source) | VRN | HMRC registered address |
|---|---|---|---|---|---|
| `01106260` | RENISHAW P L C | A | `renishaw.com` (Wikidata) | `GB422900581` | NEW MILLS WOTTON-UNDER-EDGE GLOS GL12 8JR |
| `00824821` | TRAVIS PERKINS PLC | A | `travisperkinsplc.co.uk` (Wikidata) | `GB408556737` | LODGE WAY HOUSE … NORTHAMPTON NN5 7UG |
| `01002091` | RS COMPONENTS LIMITED | A | `rscomponents.co.uk` (name guess) | `GB243164091` | FIFTH FLOOR TWO PANCRAS SQUARE LONDON N1C 4AG |
| `02793780` | VICTREX PLC | A | `victrex.com` (Wikidata) | `GB604782539` | … THORNTON CLEVELEYS LANCASHIRE FY5 4QD |
| `05246159` | FOOTPRINT SOFTWARE LIMITED | B | `footprintsoftware.co.uk` (name guess) | `GB887276955` | 13 HOMESTONE GARDENS LEICESTER LE5 2LJ |
| `09201920` | LITTLE FIRE DIGITAL LIMITED | B | `little-fire.com` (Clearbit) | `GB196097366` | … 57 CHIPPINGHOUSE ROAD SHEFFIELD S8 0ZF |

**Outcome distribution (60):**

| `no_domain_found` | `no_vat_on_site` | `verified_match` | `related_entity_only` | `vat_candidates_invalid` |
|---|---|---|---|---|
| 30 | 20 | 6 | 3 | 1 |

**Example rows from `data/poc_results.csv`**

| CRN | Company | Domain (source, confidence) | Evidence on site | HMRC | Outcome |
|---|---|---|---|---|---|
| `02793780` | VICTREX PLC | `victrex.com` (Wikidata, medium) | `Our VAT number is GB 604 7825 39` on `/en/terms-of-use` | `604782539` → `VICTREX PLC`, THORNTON CLEVELEYS FY5 4QD | `verified_match`, `GB604782539` |
| `01002091` | RS COMPONENTS LIMITED | `rscomponents.co.uk` → `uk.rs-online.com` (name guess, high: postcode) | JSON-LD `"vatID":"GB 243 1640 91"` | `243164091` → `RS COMPONENTS LIMITED`, N1C 4AG | `verified_match`, `GB243164091` |
| `05246159` | FOOTPRINT SOFTWARE LIMITED | `footprintsoftware.co.uk` via `http://` (name guess, high: CRN + postcode) | `Number : 5246159 : VAT : 887 2769 55`; 1 page, 1,030 chars | `887276955` → `FOOTPRINT SOFTWARE LTD`, LEICESTER LE5 2LJ | `verified_match`, `GB887276955` |
| `03006378` | SCREWFIX DIRECT LIMITED | `screwfix.com` (Bing, high: CRN on page) | `Company Registration no: 3006378 - VAT Registered: 232555575` | `232555575` → `KINGFISHER PLC`, 1 PADDINGTON SQUARE W2 1GG | `related_entity_only` (VAT group) |
| `08945856` | PURE TABLE TOP LTD | `puretabletop.com` (Clearbit, high) | `Registered Number: 08945856 … VAT Registration: GB 188298466` | Fails Mod-97/9755; HMRC `/unknown` | `vat_candidates_invalid` — the company's own published VAT number is wrong |
| `00040932` | HALMA PUBLIC LIMITED COMPANY | `halma.com` (Bing, high: postcode) | 28 nine-digit spans, 0 VAT-specific | 2 controls sent (GA id, one phone number); both `/unknown` | `no_vat_on_site` |
| `07911459` | STAPLEFORD PLUMBING SERVICES LTD | — | Bing top hosts (Part 1): `whatsapp.com`, `wa.me` | — | `no_domain_found` |

### 3.4 False-positive analysis

**Spans and checksum.**
- 113 nine-digit spans were extracted; 71 came from the naive tier and 42 from VAT-specific tiers
  (`candidate_funnel`).
- By context:

  | Context | Spans | Pass checksum | HMRC-valid |
  |---|---|---|---|
  | Phone | 42 | 0 | — |
  | No context | 29 | 1 (the Google Analytics ID) | 0 |
  | `vat_context` | 28 | 28 | 28 |
  | JSON-LD | 9 | 9 | 9 |
  | `gb_prefix` | 5 | 3 | 3 (the other 2 are Pure Table Top's invalid number) |

- The naive-tier checksum pass rate was 1/71 = 1.4%. For uniformly random 9-digit strings it is
  19,583,122 / 10⁹ = 1.96%, so the checksum acts on non-VAT digits about as theory predicts.

**False-positive rate by layer** (unique company × number × pattern, `precision_ladder`; "correct"
means HMRC-valid *and* registered to the target entity):

| Layer | Kept | Correct | FP rate |
|---|---|---|---|
| Regex only — any 9-digit span | 62 | 7 | **88.7%** |
| VAT-specific regex (GB prefix / "VAT" context / JSON-LD) | 11 | 7 | **36.4%** |
| + Mod-97 / Mod-9755 | 10 | 7 | **30.0%** |
| + HMRC checker (valid) | 10 | 7 | **30.0%** — all 3 are confirmed other-entity numbers |
| + entity name / postcode match | 7 | 7 | **0%** |

(7 keys for 6 companies: RS Components exposes the same VRN through two extraction tiers, JSON-LD
and a `GB` prefix.)

**What the matrices say** (17 unique company–VRN pairs sent to HMRC):
- **Checksum vs HMRC:** TP 9, FP 1, FN 0, TN 7. The one FP is the Google Analytics ID. HMRC never
  accepted a number that failed the checksum.
- **The checksum is not the main precision layer.** VAT-context extraction already removes most junk.
  Checksum plus HMRC took the FP rate only from 36.4% to 30.0%.
- **Entity matching is the layer that removed every remaining wrong answer.** A pipeline that stops
  at "HMRC says valid" ships the 3 group numbers, which is 3 wrong attachments out of 9 HMRC-valid
  companies.

**Type 1 false positives** (number looks like a VAT number but isn't one: 52 unique, 51 checksum
failures + 1 checksum-pass / HMRC-unknown, `data/false_positives_log.json`):

| Number | Source text | Why it isn't a VAT number |
|---|---|---|
| `117356421` | `halma.com/site-services/cookie-notice`: `_gat_UA-11735642-1` | Google Analytics property id. Passes Mod-97, HMRC `/unknown`. Only the HMRC layer catches it |
| `121116129` … (9 numbers) | `travisperkinsplc.co.uk/investors/analyst-consensus`: `PROFITS £m 121 116 129` | Three adjacent three-figure forecast columns read as one 9-digit run |
| `316148741`, `857994045` | Travis Perkins IR contacts: `+0035 316 148 741`, `+31 85 799 4045` | Irish and Dutch phone numbers |
| `801894577` | `Tel: +44 (0)7 801 894577` (Halma) | UK mobile minus the trunk prefix |
| `440207535` | `Call us on: +44 0207 535 1110` (Howdens) | Country code + London number |
| `383766284` | `Call us: +61 3 8376 6284` (Deritend) | Australian phone |
| `011441900` | `01144 1900 44` (Little Fire Digital) | Sheffield number with unusual grouping |
| `202633747` … (9 ids) | `CVE-2026-33747` … on `unikraft.io` | Security advisory ids |
| `188298466` | `United Kingdom VAT Registration: GB 188298466` (Pure Table Top) | The company's *own* published VAT number. Fails both checksums; HMRC `/unknown` (probably a typo) |

By company: Halma 28 (27 phone numbers from its subsidiaries' contact listings, plus the GA id),
Travis Perkins 11, Unikraft 9, and one each for Howden Joinery, Little Fire Digital, Pure Table Top
and Deritend.

**Type 2 false positives** (real, HMRC-valid number registered to a different legal entity):

| Target (CRN) | Number on target's site | HMRC registered name | Name score | Why it's there |
|---|---|---|---|---|
| SCREWFIX DIRECT LIMITED (`03006378`) | `232555575` (footer, T&Cs, about-us) | KINGFISHER PLC, W2 1GG | 25.0 | Parent company; most likely the representative member of a Kingfisher VAT group. Printed next to Screwfix's own CRN `3006378`, which the pipeline uses to reclassify it from `third_party` to `related_entity` |
| MARSHALLS PLC (`05100353`) | `183850248` | MARSHALLS MONO LTD, Elland HX5 9HT | 81.8 | Trading subsidiary. The footer prints `Company number: 00509579`, which Companies House lists as MARSHALLS MONO LIMITED at the same HX5 9HT address, not the plc |
| COSTAIN GROUP PLC (`01393773`) | `235983531` | COSTAIN ENGINEERING & CONSTR UCTION LIMI TED, EC3A 8BE | 35.0 | Operating subsidiary at the same registered office. HMRC's name contains line-wrap artefacts |

Three of the seven Tier A companies that publish a VAT number publish one belonging to a different
legal entity.

For invoice matching, a VAT-group number is the correct number to expect on Screwfix's invoices, but
it is not Screwfix's *registration*. These go in `related_entity_vrns`, not `verified_vat`, and the
consumer decides.

**Matcher mistakes I made and fixed during development** (earlier code versions; not reproducible
from `data/`):
- `token_set_ratio` scored `MARSHALLS MONO LTD` vs `MARSHALLS PLC` at 100, because {marshalls} is a
  subset. Fix: `token_sort_ratio` plus an unmatched-distinctive-token guard; the final score is 81.8.
- `partial_ratio` on domain labels accepted `london.io` for London Elite Training. Fix: partial
  matching only when the label is ≥60% of the name's length.
- A German `VAT ID No. DE81 4963 072` on `cdm.com` (CDM Smith SE, wrongly attributed to "CDM + LTD")
  was extracted as UK candidate `814963072`. Fix: EU-prefix detection → `foreign_vat`, and names of
  ≤4 characters need CRN/postcode evidence.

**What these numbers do not capture.**
- **Recall against truth.** For the 50 companies with `no_domain_found` or `no_vat_on_site`, I cannot
  tell "not VAT-registered" apart from "registered but we didn't find it". HMRC cannot be searched by
  name, so there is no ground-truth list to compare against.
- **Group exposure in Tier B/C.** No VAT group appeared in Tier B/C, but only 3 Tier B companies
  published a VAT number at all, so that doesn't establish the rate.
- **Domain precision.** The 12 medium/low-confidence domains were not manually verified. The 18
  high-confidence ones are backed by the CRN or registered postcode on the page.

---

## 4. Part 3 — what I would do with real resources

### 4.1 Architecture at scale (4.2M companies)

Live-crawling the register from a laptop is the wrong shape. Discovery cost was dominated by
*finding the site*: 10.82 s of the 12.62 s mean per company. Fetching the pages took 1.80 s.

1. **Spine.** The Companies House bulk product plus the streaming API: CRN, legal name, previous
   names, registered postcode, SIC, accounts category, status.
2. **Company → domain graph, built offline.** Join the spine to (a) Veridion's existing company/domain
   index, (b) Common Crawl or similar snapshots, and (c) Wikidata P2622 → P856.
   - Wikidata is entity-exact but covered only 5 of 60 sample CRNs, all Tier A.
   - The strongest generic join key the PoC found is the **CRN or registered postcode printed on the
     page** (Trading Disclosures). It backed 18 of 30 accepted domains, and a snapshot scan can test
     it for every `.uk` host at once, with no per-company search.
3. **Text from snapshots first.** Pull footer, T&Cs, legal and JSON-LD from the snapshot WARCs.
4. **Live crawl only for the tail.** Companies with no snapshot hit or stale pages, through a
   licensed SERP API (not scraped Bing) and a proxy pool.
5. **Extraction and checksum** exactly as in `vat_extractor.py`. They are cheap and deterministic.
6. **HMRC API v2** (OAuth, application-restricted) for verification, replacing the web form. Cache by
   VRN; one lookup serves every CRN that claims that VRN.
7. **Entity resolution** as in `match_entity()`, plus a group-structure table: CH PSC / parent
   relationships, or "registered office shared with a plc". This turns `related_entity` into an
   explicit `vat_group_member_of` edge instead of a review queue.
8. **Adjudication queue** for medium-confidence domains and `related_entity` matches: an LLM with the
   evidence snippets, then human audit on a stratified sample.

### 4.2 Rough cost per company

Measured quantities come from the final run. Prices are **assumed list-price ranges** for planning,
not quotes.

| Item | Measured basis | Assumption | Per 1,000 companies |
|---|---|---|---|
| Domain resolution, SERP fallback | Discovery dominated runtime; 2.28 homepage probes per company | 50% unresolved after the index join × 2 queries; $1–3 per 1k queries | $1.00–3.00 |
| Snapshot page reads (WARC range GETs) | 11.5 crawled pages per company with a site | ~11 S3 range GETs × 500 companies, in-region | < $0.01 |
| Live tail crawl bandwidth | 1.52 MB crawl + 0.26 MB probing per company (45.7 MB + 15.3 MB in this run) | 50% tail × 1.78 MB = 0.89 GB. Datacenter $0.10–1/GB; 10% of tail on residential at $3–8/GB | $0.36–1.60 |
| Snapshot index scan (fixed, amortised) | — | A few hundred dollars per snapshot join ÷ 4.2M | ≈ $0.07 |
| HMRC API v2 | 10 unique VAT-specific VRNs per 60 companies → ~167 calls per 1k | Free; the cost is throughput | $0 |
| LLM adjudication | 12 medium/low domains + 3 related-entity of 60 → 25% | $0.002–0.01 per case | $0.50–2.50 |
| **Variable total** | | | **≈ $2–7 per 1,000** |
| Human audit (fixed per cycle, not per company) | — | 2,000 stratified records × $0.50–1 | $1,000–2,000 per refresh cycle |

- **40k suppliers:** $80–290 variable + $1–2k audit. The audit dominates, so the invoice-first
  reverse path in section 1 is cheaper again.
- **4.2M companies:** about $8k–30k variable + audit + engineering. The money is not the constraint.
  HMRC throughput is: about 700k verification calls at the measured candidate rate, and up to 4.2M
  if every company produced a candidate. At an assumed 3 requests/s per application, that is about
  65 hours to 16 days of serial calls per full pass.

### 4.3 What breaks first

1. **Company → domain resolution for SMEs.**
   - 30/60 had no accepted domain.
   - Bing returned a relevant host for 2/12 scripted queries.
   - Wikidata covered 5/60, all Tier A. Clearbit supplied 16 of 30 domains, and it is a brand
     index, not an SME index.
   - 12 accepted domains rest on name evidence only, and brand/legal-name splits (JCB) defeat every
     free method.
2. **HMRC rate limits and IP throttling.** The public form keeps returning `303` verdicts under
   load, but serves the name-bearing result page only a few times per window. Recorded in
   `part1_source_research.json → development_observations`:
   - **Run 1:** 13 of 16 result pages returned `429`, with no `Retry-After`. The resume pass needed
     7 consecutive 10-minute rounds of `429` before 3 names came through.
   - **Run 2:** 3 of 8 result pages returned `429`; both pending names resolved within two
     10-minute rounds.
   - **Separate probe:** the window cleared after about 10 minutes.
   - **Part 1 run:** 3 of 4 result pages returned `429`.

   The web form cannot back a product; API v2 credentials are a prerequisite.
3. **Group VAT registrations.**
   - 3 of 7 Tier A VAT publishers showed a parent or group number.
   - At scale, one representative member's VRN (e.g. Kingfisher's) legitimately appears on many
     subsidiaries' sites. Without a group-structure table, every one of them lands in
     `related_entity` review.
4. **Anti-bot / WAF on the live tail.**
   - 9 domain probes and 5 crawl requests returned `403`; 13 probes timed out.
   - ICO is behind Cloudflare; Brave, Mojeek and Startpage block scripted clients.
   - This gets worse with crawl volume, which is why the tail should stay small.

### 4.4 Production monitoring

| Metric | Baseline in this run | Alert when |
|---|---|---|
| HMRC verification pass rate for VAT-specific, checksum-valid candidates | 9/9 unique VRNs (40 spans) valid | Drops below ~90% (extraction drift, typo-prone sources, or HMRC/API change) |
| Mod-97 pass → HMRC-valid ratio across all candidates sent | 9 of 10 (the miss: GA id) | Falls (more non-VAT digit strings leaking into the candidate set) |
| Distinct CRNs per VRN | Kingfisher `232555575` printed on Screwfix's site | One VRN claimed by ≥2 CRNs that share no name token, no postcode and no parent link → agency/template VAT or wrong-domain cluster |
| HMRC name ↔ CH name/postcode mismatch rate among valid VRNs | 3 of 9 named results were other-entity | Rises (more groups, or domain attribution degrading) |
| HMRC name-page 429 rate | 3 of 4 in the Part 1 run | Any sustained 429 on API v2 → quota exhaustion |
| Domain confidence mix of attached VRNs | 18 high / 11 medium / 1 low across accepted domains | Share of attachments resting on medium/low domains rises |
| Thin-page rate (HTTP 200, <200 chars) | 0 pages | Rises per crawl batch (WAF rollout, JS migration) |

---

## 5. Debate topics

### Topic 1 — Brute-forcing Mod-97 against HMRC's checker

The numbers, computed over all 10⁹ nine-digit strings:
- **10,304,247** pass Mod-97 (≈10.3M).
- **9,278,875** pass Mod-9755 (with the ≥100 prefix rule).
- No string passes both, so the candidate space is **19,583,122**, or 1.96% of 10⁹.

Point that list at HMRC and you get, in principle, a full reverse table VRN → registered name and
address. That table is the thing the forward problem lacks. Why it is still a bad idea:
- **Throughput.** At the web form's observed ~3 name pages per throttle window (10–70 min),
  19.58M lookups take roughly 120–870 years. At an assumed 3 requests/s on API v2 it is ~75 days of
  continuous calls.
- **Terms and law.** Systematically enumerating a government service against its intended use, and
  evading its throttling, is a terms-of-use breach. Pushing through blocks with rotating IPs moves
  toward the territory the Computer Misuse Act 1990 is written for. HMRC API credentials are issued
  to check counterparties, not to rebuild the register. I would want HMRC's explicit agreement before
  any bulk use.
- **Personal data.** A large share of VAT registrations are sole traders and partnerships. Their HMRC
  name and address are personal data under UK GDPR, which adds a lawful-basis problem to a dataset
  that would contain them by construction.
- **The join is still hard.** Even with all ~2.18M records in hand, HMRC returns a registered *name
  and address* and no CRN. This run shows what that looks like:
  - `COSTAIN ENGINEERING & CONSTR UCTION LIMI TED`: line-wrapped name.
  - `KINGFISHER PLC` for Screwfix: group representative.
  - HMRC addresses are business addresses, not necessarily the registered office: Kingfisher W2 1GG
    vs Screwfix BA22 8RT.
  - Sole traders appear under personal names with no CRN at all.

  Resolving 2.18M such strings onto 4.2M CRNs is the same fuzzy entity-resolution problem as
  section 3, at a larger scale and with no website evidence to break ties.

### Topic 2 — Keeping the dataset current

Refresh on change events, not on a calendar:
- **Companies House deltas.** The streaming API (company profile, officer and filing events), with
  the monthly bulk product as reconciliation.
  - Dissolution / strike-off notices and Gazette insolvency notices: expire or flag the VRN link.
  - Name change: re-run entity matching against the cached HMRC name.
  - Registered-office change: re-check postcode agreement.
  - New incorporations: enter a "watch" tier. None of the 20 Tier C companies had a VAT number on
    the web; the right action is a scheduled re-check, not a first-pass crawl.
- **Turnover signals from accounts.** These are weak for SMEs, because most file abridged or
  total-exemption accounts without a P&L: 18/25 of Tier B filed total-exemption accounts. Use
  accounts-category changes (micro → small → full), employee counts where filed, and procurement
  awards (`GB-COH` on Contracts Finder) as "likely crossed £90k" triggers.
- **Tiered HMRC re-verification TTLs**, cached by VRN, as `hmrc_cache.json` already does:
  - Verified matches on high-confidence domains: long TTL (e.g. quarterly).
  - Related-entity / group links: medium TTL; groups change membership.
  - Any record whose source page changed in the latest snapshot: immediate re-check.
  - Deregistrations show up as `/unknown`. A previously valid VRN turning unknown is a high-value
    event (company deregistered, or our record was wrong all along).
- **Source-page fingerprinting.** Store a hash of the footer/legal block per domain. A changed hash
  without a VRN change is free; a changed VRN triggers HMRC + match.

### Topic 3 — Detecting corruption at scale without ground truth

Invariants that need no labels:
- **One VRN → many unrelated CRNs.** Group VRNs legitimately fan out, but their members share name
  tokens, registered offices or PSC parents. A fan-out with no shared signal is an agency/template
  VAT or a domain-attribution cluster. This is the Type 2 vendor case the sample didn't contain; at
  4.2M it will.
- **Name/postcode divergence between CH and HMRC.** Track it per source and per tier. In this run, 3
  of 9 named HMRC responses diverged from the target; a jump in that share is drift.
- **Checksum-valid but HMRC-unknown rate** on VAT-specific candidates (0 of 9 in this run). Rising
  means typo-prone sources or extraction drift.
- **Evidence-strength mix.** The share of attached VRNs whose domain rests on CRN/postcode
  evidence vs name-only.
- **Stratified shadow audits.** A fixed sample per stratum (tier × domain confidence × match class),
  manually verified each cycle against invoices or the company's own documents. This is the only
  route to a recall estimate.
- **Canary customers.** Invoice-matching customers whose invoices carry VRNs: every invoice VRN that
  disagrees with ours is a labelled error, free.

### Topic 4 — Sources I would not use in a commercial product

- **Scraped commercial directories and credit bureaus** (Endole, Creditsafe, Kompass, etc.).
  Database-right and terms-of-service exposure, and their VAT fields are themselves scraped and
  unverifiable. `pipeline.py` excludes these from domain discovery (`DIRECTORY_EXACT`,
  `DIRECTORY_LABELS`).
- **Unverified "free VAT lookup" sites.** No provenance, no liability, and a poisoning risk:
  one wrong VRN copied across directories looks like independent corroboration.
- **Brute-forced or throttle-evading government endpoints** (Topic 1), including rotating IPs
  against HMRC's checker.
- **Scraped search engines.** Against terms and, as measured, also useless: 2/12 relevant.
  Licensed SERP APIs only.
- **Personal data beyond need.** Sole-trader VAT records carry individuals' names and addresses.
  Store the VRN ↔ CRN link and validation metadata, not HMRC name/address strings for
  non-company registrants.

---

## 6. Beyond the UK

Spot checks in `data/spotchecks.json` (VIES REST API, 17 Nov 2026):

| VAT id | VIES `isValid` | VIES name returned |
|---|---|---|
| `FR03552081317` | true | `ELECTRICITE DE FRANCE` |
| `DE814963072` | true | `---` |
| `ESA28015865` | true | `---` |
| `IT00488410010` | true | `TIM S.P.A.` |
| `RO1590082` | true | `OMV PETROM SA` |
| `XI220430231` | true | `TESCO PLC` |

Operational note from the same afternoon: VIES served an HTML "Server temporarily unavailable" page
with HTTP 200, and France later answered `isValid: false` with `userError: MS_MAX_CONCURRENT_REQ`.
Both are outages, not verdicts. `common.vies_lookup()` retries them and flags `vies_unavailable`
rather than recording "invalid". A VIES-backed product needs the same guard.

### Germany — the natural next market, and where verification gets harder

**What gets easier: discovery.**
- § 5 DDG (formerly § 5 TMG) requires commercial websites to carry an Impressum: legal form,
  register court and number (`HRB 12345, Amtsgericht München`), and the USt-IdNr where the business
  has one.
- Unlike the UK, where 10 of 30 reachable sites showed a VAT number, that block is legally mandated,
  conventionally at `/impressum`, and has fixed labels (`USt-IdNr.`, `Umsatzsteuer-ID`,
  `Registergericht`).
- The crawl in `crawl()` becomes one targeted page instead of a link-ranking heuristic. The register
  number gives the same entity check the UK CRN gave.

**What gets harder: three problems.**
- **Two tax numbers.** The *Steuernummer* is the domestic tax number. The *USt-IdNr.* (`DE` + 9
  digits) is issued on application, and many domestic-only firms never apply. Only the USt-IdNr. is
  the VAT id that matters cross-border. A site showing only a Steuernummer is a legitimate "no
  USt-IdNr. published" case, and the extractor must not mistake one for the other.
- **Fragmented register.** The Handelsregister is kept by local courts, so `HRB 12345` is only unique
  together with its Amtsgericht. Entity keys must be (court, register type, number).
- **VIES withholds names.** `DE814963072` comes back valid with name `---`. Our UK design depends on
  HMRC returning a name and address to match; for Germany that step disappears.
  - The alternative is BZSt's qualified confirmation request. As I understand it, you supply the
    name and address yourself and get per-field match flags, and it requires the requester's own
    German USt-IdNr.
  - That turns "verify and match" into "assert and confirm". It works only once the Impressum has
    given you a name and address to assert.

**Not tested in this phase:** Handelsregister access and the BZSt service.

### Algorithmic / near-zero-discovery markets

- **France.** VAT = `FR` + key + SIREN, key = (12 + 3 × (SIREN mod 97)) mod 97. Verified live for EDF
  and TotalEnergies (`part1_source_research.json`). SIREN is in INSEE's open SIRENE register, so
  every French company gets a *candidate* VAT number at zero crawl cost.
  - The derivation proves format, not registration: the number exists syntactically for every SIREN.
  - VIES then confirms activity *and* returns the name. This is a registry join plus one VIES call.
- **Italy.** For most companies the Partita IVA (11 digits) is the same as the company's codice
  fiscale, so `IT` + P.IVA comes straight from registry identifiers. VIES returned `TIM S.P.A.` for
  `IT00488410010`. I did not test registry access; Registro Imprese data is distributed commercially
  via InfoCamere.
- **Romania.** VAT = `RO` + CUI, but only for companies registered for VAT. ANAF publishes a free
  VAT-status web service, which I did not test. VIES returned `OMV PETROM SA` for `RO1590082`.

**Roadmap implication.**
- Ship registry-derived countries first: FR, then IT and RO subject to registry access. The cost
  per company is a registry join plus one VIES call, with precision bounded by VIES rather than by
  web extraction.
- Spend web-discovery budget (UK, DE) after those, and spend verification engineering where VIES is
  blind.

### Where the pain actually is

| Bottleneck | Countries (from this work) | Why |
|---|---|---|
| Discovery | UK | No registry link; VAT text on 10 of 30 reachable sites; domain resolution fails for half of companies |
| Verification | Germany, Spain | VIES returns valid/invalid with name `---` (tested: `DE814963072`, `ESA28015865`), so automated disambiguation needs a qualified check or another name source |
| Neither (registry-derived + VIES names) | France, and likely Italy and Romania | Candidate from registry id; VIES returns the name (tested FR, IT, RO) |

---

## 7. Repository guide and reproducibility

```
pip install -r requirements.txt          # requests, beautifulsoup4, lxml, rapidfuzz
cd src
python sample_builder.py                 # ~2 min  → data/uk_sample_companies.csv (new random draw)
python sample_builder.py --refresh-accounts   # re-derive latest_accounts_type for the existing sample, no redraw
python pipeline.py                       # ~2.5 min network + up to 17 min HMRC back-off if names are throttled
                                         #   reuses data/hmrc_cache.json; only new VRNs hit HMRC
python pipeline.py --no-backoff          # one immediate name retry instead of the 120/300/600 s back-off
python pipeline.py --resume-names        # retry name-pending VRNs every 10 min (up to 90 min), rewrite outputs
python pipeline.py --resume-names --offline   # rewrite all outputs from pipeline_state.json + hmrc_cache.json, no network
python source_testers.py                 # ~5 min  → data/part1_source_research.json (reads candidates.csv for CRN/VRN pairs)
python spotchecks.py                     # <1 min  → data/spotchecks.json (VIES name disclosure, Wikidata UA test)
```

The final artifacts were produced by:
1. `sample_builder.py`
2. `sample_builder.py --refresh-accounts`
3. `pipeline.py --no-backoff`, with the HMRC cache from the day's earlier runs
4. `source_testers.py`
5. `spotchecks.py`

| File | Role |
|---|---|
| `src/common.py` | HTTP wrapper (never raises; logs status, latency, error class), rate limiter, `safe_json`, outage-aware `vies_lookup`, Companies House parsers |
| `src/sample_builder.py` | Stratified sample from live Companies House (seed `20261117`) |
| `src/vat_extractor.py` | Extraction tiers, context labelling, Mod-97 / Mod-9755 |
| `src/hmrc_verifier.py` | HMRC web-form client (CSRF, 303 verdict, 429-aware), VIES-XI / EORI fallbacks, entity matcher |
| `src/pipeline.py` | Discovery → crawl → extract → verify → classify; metrics, FP logs, resumable state |
| `src/source_testers.py` | Part 1 source tests |
| `src/spotchecks.py` | VIES name disclosure by country; Wikidata User-Agent test |
| `data/uk_sample_companies.csv` | 60 companies, 12 columns |
| `data/poc_results.csv` | 60 rows: domain + evidence, crawl stats, candidates, HMRC result, match class, outcome, timings |
| `data/candidates.csv` | All 113 nine-digit spans with pattern, context, checksum, HMRC outcome, match class, snippet |
| `data/false_positives_log.json` | 52 Type 1 and 3 Type 2 (group/parent) cases with snippets and URLs |
| `data/experiment_summary.json` | Tier coverage, funnel, precision ladder, confusion matrices, timings, HTTP breakdown |
| `data/part1_source_research.json` | 7 sources, 102 tests, every request logged, development observations |
| `data/hmrc_cache.json` | 18 HMRC verdicts (17 used by the final run) with names and addresses |
| `data/crawl_log.jsonl` | 721 discovery and crawl requests |

**Bugs fixed for this revision** (each one changed the final data):

| Bug | Effect | Fix |
|---|---|---|
| Wikidata User-Agent | Wikidata lookups sent a browser-like UA and got `403` on every call | Descriptive UA (`common.BOT_UA`) plus retry on `429`/`5xx`/timeouts. Now 60/60 `200`; Travis Perkins and Victrex were verified on Wikidata-linked domains |
| Directory filter | The regex matched substrings, so `x\.com` blocked `victrex.com` (also `fedex.com`, `craft.co.uk`, anything containing `192`) | Exact registrable-domain / first-label matching (`is_directory()`) |
| VIES outages | Outage HTML and `MS_MAX_CONCURRENT_REQ` could crash `r.json()` or be recorded as "invalid" | `safe_json` + `vies_lookup` with retries and an explicit `vies_unavailable` flag |
| Accounts parser | The filing-history `category` filter is client-side, so busy plcs showed `none_filed`/`other` | Page through filings and read form type `AA`, skipping interim/initial accounts. 4 Tier A rows corrected to `group` |

**Limitations that remain** (not bugs):
- **Re-runs will not reproduce the exact numbers.** Websites, Bing results and HMRC throttling state
  change. `pipeline_state.json` + `hmrc_cache.json` let `--offline` regenerate the reported outputs
  exactly.
- **HMRC budget.** The public checker allows only a few name lookups per window per IP. Keep
  `hmrc_cache.json`, and use API v2 credentials for anything beyond a sample.
- **Client location.** The run machine's IP geolocates to Romania. Bing localises results to the IP
  (Romanian hosts in `part1_source_research.json`), and `cc=`/`mkt=` parameters return empty result
  pages to scripted clients.
- **Contracts Finder keyword search** timed out at 30 s for 3 of 12 names.
