# Pass 3 Eval — Sonnet 4.6 vs Opus 4.7 on Real Tomac IC Memos

**The question.** Should Pass 3 (IC memo production) in `MODEL_ROUTER.md` move from Sonnet 4.6 ($3/$15 per M) to Opus 4.7 ($5/$25 per M)?

**The cost gap.** From `MEASUREMENT_REPORT.md`: Opus is ~1.81× Sonnet on a Pass-3-shaped workload. Absolute delta ~$0.016/call. At Tomac's current pace of roughly 5-10 IC memos per week, total cost difference is on the order of $5-10/month. Cost is not the deciding factor — output quality is.

**How to use this file.** Each of the three themes below shows two anonymized memos labeled A and B. The mapping (A→model, B→model) is at the very bottom of the file. Read all three theme pairs before scrolling to the answer key. Score each pair on the rubric below, then reveal.

## Scoring rubric (per theme)

For each pair, judge A vs B on:

1. **Thesis sharpness** — does the so-what land in the first paragraph?
2. **Named-asset specificity** — MW, dollars, dates, firm names lifted from the brief without softening?
3. **Hedge identification** — does it surface the actual disagreements/risks (not just list them)?
4. **Actionable next step** — can you act on the WHAT YOU WOULD NEED TO FORM A VIEW section?
5. **Memo discipline** — six-section structure followed cleanly; no filler.

Tally per theme: A wins / B wins / tie on each dimension. After all three themes, count: if one model wins ≥10 of 15 dimensions, the eval is decisive.

---

## Theme: MISO Power Plant Acquisitions (id: `miso-power`)

### Pass 2 brief (input to both models)

```
PASS 2 BRIEF — MISO Power Plant Acquisitions

Theme ID: miso-power
Score: 95 / Conviction: High / Timing: Immediate
Check size: $200M-$1B per asset
Target returns: 18-30% levered IRR

Thesis:
DOE 202(c) orders stalled 2.5+ GW MISO coal. NIPSCO GenCo confirmed scaling: Amazon 2.4 GW + Alphabet 515 MW; total stack now $6-7B+. IURC PPA approval vote May 6, 2026 (3 days). Campbell DOE Order 202-26-16 binary May 18 (15 days). DT Midstream signed 20-yr contract for 900 MW Indiana power dev (May 1, 2026) — confirms Indiana captive-power tailwind for NIPSCO.

Structure:
Acquire at/below book from utilities exiting coal. Restructure as independent capacity, GenCo carveout, or BTM for hyperscalers.

TOP TARGET:
  Name: J.H. Campbell Power Plant
  Location: West Olive, MI
  Owner: Consumers Energy / CMS Energy (NYSE: CMS)
  Capacity: 1,560 MW coal (3 units, 1962-1980)
  Regulatory: DOE Order 202-26-16 active through May 18, 2026 (15 days). 4th DOE extension. Cumulative cost $135M+ (~$600K/day). Michigan AG: 3 DC Circuit petitions + 3 DOE rehearing requests pending.
  Score: 89 (prev 91, dir down)
  Status: Reviewing
  Recent change: Score -2: 15 days to May 18 binary; still no 5th extension issued; no private buyer RFP signal from CMS. May 18 is the action point; if denied, RFP window opens within 60 days; if granted, status quo continues at $600K/day cost burden.
  Open question: Will DOE issue 5th extension post-May 18? Does Consumers initiate private buyer RFP if extension denied?
  Transaction path: carveout
  First seen: 2026-02-22 | Days in pipeline: 70

Deal context:
  Seller motivation: DOE-forced operating cost ($600K/day = $4.5M/week). If 5th extension denied May 18, plant retires within 60 days; if granted, costs continue piling on Consumers ratepayers (FERC socialized across 11 MISO states). Either path increases pressure for private buyer RFP.
  Capital use: Acquisition at/below book ($300-500M est.); reposition as merchant capacity in MISO with potential repower to gas or coupled BTM hyperscaler offtake.
  Deal status: Thematic Watch
  Process signal: DOE 202(c) order expires May 18, 2026 — forced-exit binary event confirmed. No formal RFP or bilateral conversation with Consumers IR confirmed.

Tomac Cove fit:
  Score: 72
  Angle: structuring
  Rationale: Yoni's LS Power power-markets background relevant; no named contact at Consumers Energy CFO/IR confirmed. Bilateral path unconfirmed.
  Value-add: Bespoke distressed-asset structure (mezzanine + retirement waterfall) at $200-400M check that mega-funds avoid for headline coal exposure.
```

### Memo A  _(latency 50.3s, output 3589 tok, cache_read 0 tok)_

## J.H. Campbell — DOE 202(c) Forced-Exit, May 18 Binary

---

### THE CORE ARGUMENT

DOE Order 202-26-16 on Consumers Energy's 1,560 MW J.H. Campbell coal plant (West Olive, MI) expires May 18, 2026 — 15 days. Cumulative DOE-forced operating cost is $135M+ at ~$600K/day, with Michigan AG running parallel pressure (3 DC Circuit petitions, 3 DOE rehearing requests). Either outcome increases probability of a private-buyer RFP: denial forces retirement within 60 days and an asset disposition; a 5th extension piles cost on Consumers ratepayers and intensifies political pressure to exit. NIPSCO's Amazon 2.4 GW + Alphabet 515 MW GenCo template ($6-7B+ stack, IURC vote May 6) and DT Midstream's 900 MW Indiana 20-year contract (May 1) confirm the captive-power offtake bid is real and pricing is moving.

The play is acquire at/below book ($300-500M est.) and restructure as merchant capacity, GenCo carveout, or BTM hyperscaler offtake. Check size $200-400M fits the white space mega-funds skip for headline coal exposure. Conviction high, timing immediate, target levered IRR 18-30%.

### POINTS OF CONSENSUS

- DOE 202(c) forced-exit dynamic across MISO is real — 2.5+ GW stalled. (Pass 2)
- NIPSCO GenCo template validates hyperscaler captive offtake at scale: Amazon 2.4 GW + Alphabet 515 MW, $6-7B+ stack, IURC vote May 6.
- DT Midstream 20-year / 900 MW Indiana contract signed May 1, 2026 confirms Indiana/Midwest captive-power tailwind extends beyond NIPSCO.
- Campbell cost burden ($600K/day, $4.5M/week, $135M cumulative) is unsustainable as a steady state for Consumers Energy ratepayers.

### POINTS OF DISAGREEMENT OR TENSION

- Score moved 91 → 89 (down) — no 5th extension issued yet, no private-buyer RFP signal from CMS, no bilateral conversation with Consumers IR confirmed.
- Tomac Cove fit score is 72, not high-conviction — bilateral path unconfirmed, no named CMS CFO/IR contact established.
- Transaction path tagged "carveout" but BTM hyperscaler offtake and merchant repositioning are alternative structures with very different return profiles.

### OPEN QUESTIONS AND UNRESOLVED ISSUES

- Will DOE issue a 5th extension post-May 18, or let the order lapse?
- If lapsed, does Consumers initiate a formal private-buyer RFP within the implied 60-day retirement window?
- Book value confirmation — $300-500M is estimated, not sourced.
- Michigan AG litigation outcome on the 3 DC Circuit petitions and 3 DOE rehearing requests — does any near-term ruling foreclose extension?
- Repower-to-gas feasibility at West Olive site (interconnection, gas lateral, water rights, MI permitting).
- Hyperscaler interest on the specific site — is there an active conversation, or is this thesis-driven?

### WHAT YOU WOULD NEED TO FORM A VIEW

- Consumers Energy / CMS book value and net plant asset detail for Campbell units 1-3 (10-K, FERC Form 1).
- Direct CFO/IR contact at CMS — establish bilateral channel before any RFP launches.
- DOE signal on 5th extension intent — call DOE EDF (Greg Beard) and DOE Office of Electricity.
- MISO interconnection and capacity accreditation analysis for 1,560 MW at West Olive node — merchant value floor.
- Hyperscaler land/power scout outreach (AWS, Google, Meta, Microsoft Midwest siting teams) — is West Olive on any short list?
- Michigan PSC and AG posture — political feasibility of private acquisition vs. forced retirement.
- Repower-to-gas capex and timeline study — gas lateral access, brownfield permitting in MI.
- Comp set: NIPSCO GenCo terms (IURC May 6 docket), Talen/AWS Susquehanna, Vistra/Microsoft Comanche Peak — extract pricing, structure, capacity payment.

### KEY NAMES AND FIRMS

- Consumers Energy / CMS Energy (NYSE: CMS) — owner of J.H. Campbell, forced seller candidate.
- J.H. Campbell Power Plant — 1,560 MW coal, West Olive MI, 3 units (1962-1980).
- DOE / Office of Electricity — issuer of Order 202-26-16; May 18 binary.
- Greg Beard / DOE Office of Energy Dominance Financing — relevant for forced-exit policy posture.
- Michigan Attorney General — 3 DC Circuit petitions + 3 DOE rehearing requests pending.
- NIPSCO — GenCo carveout template; IURC vote May 6.
- Amazon / Alphabet — NIPSCO offtakers (2.4 GW / 515 MW).
- DT Midstream — 20-yr / 900 MW Indiana power dev contract signed May 1.
- IURC — Indiana regulator; May 6 PPA approval vote.
- Mark Saxe / Tomac Cove — co-founder, structuring lead.
- Yoni Gontownik / Tomac Cove — LS Power power-markets background, lead on diligence.

════════════════════════════════════════════════════════════
ACTION ITEMS
════════════════════════════════════════════════════════════

[ACTION-001]
Date/Deadline : 2026-05-06
Time          : TBD
Action        : Monitor IURC PPA approval vote on NIPSCO Amazon/Alphabet GenCo stack and extract pricing/structure terms from docket.
Owner         : Yoni
Parties       : IURC, NIPSCO, Amazon, Alphabet
Context       : Direct comp for Campbell GenCo carveout structure; sets pricing benchmark for hyperscaler offtake in MISO.
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-002]
Date/Deadline : 2026-05-15
Time          : TBD
Action        : Establish bilateral channel with CMS Energy CFO/IR before May 18 binary event.
Owner         : Yoni / Mark
Parties       : Consumers Energy, CMS Energy IR/CFO
Context       : No named contact at CMS confirmed; need direct line ahead of RFP window if DOE extension denied.
Dashboard     : Both
Priority      : High

[ACTION-003]
Date/Deadline : 2026-05-18
Time          : TBD
Action        : Track DOE Order 202-26-16 expiration outcome (5th extension issued vs. lapsed) and update pipeline status same day.
Owner         : Yoni
Parties       : DOE Office of Electricity, Consumers Energy
Context       : Binary trigger; lapse opens 60-day RFP window; extension continues $600K/day cost burden and political pressure.
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-004]
Date/Deadline : 2026-05-12
Time          : TBD
Action        : Call Greg Beard at DOE EDF to read DOE posture on 5th extension and broader MISO 202(c) policy.
Owner         : Yoni
Parties       : Greg Beard / DOE Office of Energy Dominance Financing
Context       : Existing relationship; intelligence on DOE intent ahead of May 18 binary.
Dashboard     : Both
Priority      : High

[ACTION-005]
Date/Deadline : 2026-05-25
Time          : TBD
Action        : Pull CMS 10-K and FERC Form 1 to confirm Campbell book value and net plant detail; refine $300-500M acquisition estimate.
Owner         : Yoni
Parties       : CMS Energy (public filings)
Context       : Acquisition price assumption is currently estimated, not sourced.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-006]
Date/Deadline : 2026-05-30
Time          : TBD
Action        : Commission MISO interconnection and capacity accreditation analysis for West Olive node (1,560 MW).
Owner         : Yoni
Parties       : Internal / external power-markets consultant
Context       : Establishes merchant value floor independent of hyperscaler offtake; needed for any structure scenario.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-007]
Date/Deadline : 2026-06-01
Time          : TBD
Action        : Initiate hyperscaler siting-team outreach (AWS, Google, Meta, Microsoft Midwest) to test West Olive interest.
Owner         : Mark / Yoni
Parties       : AWS, Google, Meta, Microsoft siting teams
Context       : Confirms whether BTM offtake structure has a real bid versus thesis-only.
Dashboard     : Both
Priority      : Medium

[ACTION-008]
Date/Deadline : No deadline
Time          : TBD
Action        : Build comp pack: NIPSCO/IURC, Talen/AWS Susquehanna, Vistra/Microsoft Comanche Peak — extract pricing, structure, capacity payment.
Owner         : Yoni
Parties       : Internal
Context       : Underwriting precedent for Campbell GenCo or BTM structure.
Dashboard     : Deal Pipeline
Priority      : Medium

════════════════════════════════════════════════════════════
DASHBOARD ROUTING SUMMARY
════════════════════════════════════════════════════════════

Chief of Staff Dashboard → 0 items:
Deal Pipeline Dashboard  → 4 items: ACTION-001, ACTION-003, ACTION-005, ACTION-006, ACTION-008
Both                     → 3 items: ACTION-002, ACTION-004, ACTION-007
No routing needed        → 0 items

### Memo B  _(latency 74.2s, output 3642 tok, cache_read 2886 tok)_

# IC MEMO — J.H. CAMPBELL POWER PLANT (MISO-POWER)
**Prepared for:** Investment Committee
**Date:** May 4, 2026
**Theme ID:** miso-power | **Score:** 95 / **Target Score:** 89 | **Conviction:** High | **Timing:** Immediate

---

## THE CORE ARGUMENT

**The so-what:** DOE Order 202-26-16 expires May 18 — 14 days from today. If denied, Consumers Energy must retire Campbell within 60 days, opening a forced-seller RFP window on 1,560 MW of MISO coal at a price well below replacement cost. If extended, Consumers absorbs $600K/day ($4.5M/week) in DOE-mandated operating costs with no recovery mechanism — accelerating the same exit, just slower. Either path leads to the same destination: CMS Energy needs to monetize this asset. The question is whether Tomac Cove is positioned to receive the call when the RFP opens.

The deal structure is the thesis. Campbell is too coal-exposed for infrastructure mega-funds on ESG mandate. It's too complex and idiosyncratic for corporate buyers. It is exactly sized ($300-500M) and structured (distressed carveout, mezzanine-compatible, repower optionality) for a principal investor with power-markets structuring capability and no headline-coal constraint. Tomac's LS Power background is a genuine right-to-win here — not a theme, a credential. The Indiana market corroborates the thesis: DT Midstream signed a 20-year contract for 900 MW of Indiana captive power on May 1, confirming hyperscaler demand is real and proximate to the NIPSCO/MISO footprint.

---

## POINTS OF CONSENSUS

- **DOE 202(c) order is the forcing function.** 202-26-16 is the 4th extension. Cumulative operating cost burden now exceeds $135M. No 5th extension has been issued. May 18 is a genuine binary.
- **MISO demand is confirmed at scale.** NIPSCO GenCo: Amazon 2.4 GW + Alphabet 515 MW contracted. Total stack $6-7B+. IURC PPA approval vote is May 6 — two days from now. DT Midstream 20-yr / 900 MW Indiana captive-power deal (May 1) is independent corroboration.
- **Seller motivation is unambiguous.** $600K/day forced operating cost with FERC socializing the burden across 11 MISO states creates political and regulatory pressure on Consumers beyond pure economics. This is not a discretionary exit — it is a managed unwind of a stranded asset.
- **Check size keeps mega-funds out.** $300-500M acquisition at/below book sits below the minimum deployment threshold for Blackstone, KKR, and Brookfield on a single asset. ESG mandate restrictions on coal exposure remove most infrastructure LP-constrained vehicles entirely.
- **Repower optionality is real.** Gas repower or BTM hyperscaler offtake (following the Blackstone/NiSource GenCo carveout template) provides the exit path that justifies 18-30% levered IRR targets.

---

## POINTS OF DISAGREEMENT OR TENSION

- **Score direction is negative.** Target score moved from 91 → 89, direction down. The May 18 binary compresses the action window without a compensating process signal. No private buyer RFP from CMS has been confirmed or signaled. The asset may retire before a bilateral conversation is possible.
- **Michigan AG opposition is not trivial.** Three DC Circuit petitions and three DOE rehearing requests pending. If the AG succeeds in accelerating retirement via a court order or DOE denial, the plant may retire before Tomac can structure a bid — removing the acquisition opportunity and leaving only a distressed debt or leaseback play.
- **No confirmed entry path.** No bilateral conversation with Consumers IR confirmed. No named contact at CMS CFO/IR level. Tomac's right-to-win is structuring capability, not relationship access — which means the first mover advantage goes to whoever has the CMS relationship, not necessarily whoever has the best structure.
- **Coal headline risk on LP base.** Tomac's emerging LP relationships — particularly any institutional LPs with ESG screens — may resist coal exposure even with a credible repower narrative. This is a fund formation risk, not a deal risk, but it's real at Tomac's current stage.

---

## OPEN QUESTIONS AND UNRESOLVED ISSUES

- **Will DOE issue a 5th extension post-May 18?** This is the single most important binary. No extension issued as of today. Historical pattern (4 prior extensions) suggests political will to extend, but cumulative cost and Michigan AG pressure make the 5th extension less certain than the 4th was.
- **Does Consumers initiate a private buyer RFP if the extension is denied?** FERC rules on MISO retirement require advance notice, but there is no confirmed obligation to run a competitive process. CMS could negotiate bilaterally with a single buyer, or seek a different regulatory resolution (rate-base retirement, accelerated depreciation, etc.).
- **What is the acquisition price if a process opens?** "$300-500M at/below book" is a range, not a number. Book value of a 1962-1980 asset after 60+ years of depreciation may be well below that range. The real floor is decommissioning cost avoidance — which is the seller's true reservation price.
- **Is repower to gas permittable at this site?** West Olive, MI site constraints, Great Lakes proximity, and Michigan environmental permitting timeline are unknown. Gas repower requires pipeline interconnect, new permits, and likely 3-5 years. BTM hyperscaler offtake is faster but requires a named counterparty.
- **IURC PPA approval (May 6) — does approval change MISO market dynamics for Campbell?** If IURC approves the NIPSCO/Amazon + Alphabet PPAs, it confirms Indiana PPA structures work for hyperscalers, which is a direct read-across to Michigan. Does it accelerate or complicate a Michigan regulatory path for a Campbell carveout?
- **Who are the other bidders if an RFP opens?** ArcLight, LS Power, and ECP all have MISO coal experience. None have confirmed interest in Campbell specifically. Timing advantage goes to whoever initiates contact with CMS in the next 14 days.

---

## WHAT YOU WOULD NEED TO FORM A VIEW

- **DOE extension tracking (daily).** Monitor Federal Register and DOE ORDA for any 5th extension filing. If nothing by May 16, assume denial is probable and initiate outreach the following day.
- **CMS Energy IR/CFO contact.** Need a named contact at Consumers Energy willing to receive a non-public expression of interest. Outreach should frame Tomac as a structured-finance buyer, not a coal operator — emphasizing retirement waterfall and cost certainty for Consumers ratepayers.
- **Campbell book value and decommissioning liability.** Pull CMS Energy 10-K (2025) for gross plant / accumulated depreciation on Campbell units. Estimate decommissioning liability (ash pond, site remediation) — this is the seller's floor and Tomac's pricing anchor.
- **MISO capacity market prices (2026/27 auction clearing).** Campbell's merchant value post-carveout depends on MISO capacity clearing prices. Get current auction results and forward curve from a power markets desk (LS Power contact or independent power analyst).
- **Michigan gas repower permitting timeline.** One conversation with a Michigan environmental attorney to understand whether a gas repower at West Olive is viable in a 3-year window, and what the permitting sequence looks like.
- **Hyperscaler demand signal for Michigan.** Is any named hyperscaler seeking large-load power in Michigan or MISO-Midwest? Amazon and Alphabet are locked into Indiana. Microsoft, Meta, and Oracle have active MISO siting activity — check against known announced facilities in Michigan.
- **LP temperature check on coal.** Before committing structuring resources, confirm with 2-3 anchor LP prospects whether a coal carveout with documented repower plan is fundable. This gates the entire thesis at Tomac's current stage.

---

## KEY NAMES AND FIRMS

| Name / Firm | Context |
|---|---|
| **Consumers Energy / CMS Energy (NYSE: CMS)** | Owner of J.H. Campbell. Forced seller via DOE order. No confirmed bilateral contact. |
| **DOE / ORDA** | Issuer of Order 202-26-16. 4th extension. 5th extension binary May 18. |
| **Michigan AG** | 3 DC Circuit petitions + 3 DOE rehearing requests filed opposing extensions. Key risk to timeline. |
| **Amazon** | 2.4 GW contracted with NIPSCO GenCo. IURC PPA vote May 6. |
| **Alphabet** | 515 MW contracted with NIPSCO GenCo. IURC PPA vote May 6. |
| **DT Midstream** | Signed 20-yr contract for 900 MW Indiana captive power, May 1, 2026. Confirms Indiana demand. |
| **NIPSCO / NiSource** | GenCo scaling confirmed. Blackstone/NiSource carveout template is the structural analog. |
| **FERC** | Socializing Campbell operating costs across 11 MISO states. Creates multi-state political pressure. |
| **IURC** | Indiana utility regulator. PPA approval vote May 6 — MISO read-across signal. |
| **Yoni Gontownik / Tomac Cove** | Acquirer. LS Power background = power-markets structuring credential. Right-to-win angle: structuring, not relationships. |
| **Mark Saxe / Tomac Cove** | Co-founder. ex-BlackRock infra. Relevant to LP relationships and fund positioning. |
| **ArcLight, LS Power, ECP** | Most likely competing bidders if RFP opens. All have MISO coal experience. None confirmed on Campbell. |

---

## ACTION ITEMS

```
════════════════════════════════════════════════════════════
ACTION ITEMS
════════════════════════════════════════════════════════════

[ACTION-001]
Date/Deadline : 2026-05-06
Time          : EOD
Action        : Monitor IURC PPA approval vote outcome for NIPSCO/Amazon + Alphabet
                and assess Michigan read-across implications for Campbell carveout path.
Owner         : Yoni
Parties       : IURC, NiSource/NIPSCO, Amazon, Alphabet
Context       : IURC vote today. Approval confirms Indiana PPA structure works for
                hyperscalers — direct analog to Michigan regulatory path for Campbell BTM play.
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-002]
Date/Deadline : 2026-05-16
Time          : EOD
Action        : Monitor Federal Register and DOE ORDA daily for 5th extension filing
                on Order 202-26-16; if no extension filed by May 16, treat May 18
                denial as base case and initiate CMS Energy outreach May 19.
Owner         : Yoni
Parties       : DOE ORDA, CMS Energy / Consumers Energy
Context       : May 18 is the binary. No extension as of today. 4 prior extensions
                create false comfort — Michigan AG opposition and $135M+ cumulative
                cost burden make 5th extension genuinely uncertain.
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-003]
Date/Deadline : 2026-05-09
Time          : TBD
Action        : Pull CMS Energy 2025 10-K to extract Campbell gross plant value,
                accumulated depreciation, and decommissioning liability estimate
                to establish seller floor and Tomac bid anchor.
Owner         : Yoni
Parties       : CMS Energy (public filing)
Context       : Cannot price a bid without knowing book value and decommissioning
                liability. This is the pricing foundation for any LOI.
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-004]
Date/Deadline : 2026-05-09
Time          : TBD
Action        : Identify named contact at CMS Energy CFO or IR level through
                LS Power network or investment banking relationships for warm
                outreach upon May 18 extension denial.
Owner         : Yoni
Parties       : CMS Energy / Consumers Energy (CFO/IR), LS Power network
Context       : No bilateral conversation confirmed. Right-to-win is structuring
                capability, but relationship access is the gating factor for
                receiving the RFP. Must have a name before the binary fires.
Dashboard     : Both
Priority      : High

[ACTION-005]
Date/Deadline : 2026-05-12
Time          : TBD
Action        : Get current MISO capacity market clearing prices and 2026/27
                forward curve from a power markets contact to validate merchant
                revenue assumptions underpinning 18-30% levered IRR target.
Owner         : Yoni
Parties       : LS Power contact or independent power markets analyst
Context       : Campbell's post-carveout merchant value is the IRR driver.
                Need actual clearing prices, not thematic assumptions.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-006]
Date/Deadline : 2026-05-14
Time          : TBD
Action        : Conduct one conversation with a Michigan environmental attorney
                to assess gas repower permittability at West Olive and realistic
                timeline for site permits given Great Lakes proximity.
Owner         : Yoni
Parties       : Michigan environmental attorney (to be identified)
Context       : Repower optionality is central to the exit thesis and LP narrative.
                If repower is a 7-year permitting process, the thesis changes.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-007]
Date/Deadline : 2026-05-12
Time          : TBD
Action        : Conduct temperature check with 2-3 anchor LP prospects on coal
                carveout exposure with documented repower plan to confirm fundability
                before committing structuring resources to Campbell.
Owner         : Mark
Parties       : Tomac Cove anchor LP prospects (to be named by Mark)
Context       : Coal headline risk on LP base is a fund-formation risk that gates
                the entire thesis. Must validate before expending diligence capital.
Dashboard     : CoS
Priority      : High

════════════════════════════════════════════════════════════
DASHBOARD ROUTING SUMMARY
════════════════════════════════════════════════════════════

Chief of Staff Dashboard  → 1 item:  ACTION-007
Deal Pipeline Dashboard   → 5 items: ACTION-001, ACTION-002, ACTION-003, ACTION-005, ACTION-006
Both                      → 1 item:  ACTION-004
No routing needed         → 0 items
════════════════════════════════════════════════════════════
```

---

## Theme: European LNG Regas & FSRU (id: `eu-lng-fsru`)

### Pass 2 brief (input to both models)

```
PASS 2 BRIEF — European LNG Regas & FSRU

Theme ID: eu-lng-fsru
Score: 92 / Conviction: High / Timing: Immediate
Check size: $100M-$1B
Target returns: 10-20% project IRR + commodity sharing

Thesis:
Hormuz partial reopening April 17 (US blockade continues; ~5% pre-war volume per May 1). UAE quits OPEC effective May 1, 2026 — long-term supply policy independence target 5M bpd by 2027. Qatar FM (12.8 MTPA Trains 4+6 offline 3-5 yrs) creates structural multi-year deficit. TTF €13.90/MMBtu; JKM $15.24/MMBtu. EU jet fuel crisis ($1,700-2,100/tonne, 6-week reserves) confirms structural import dependence. Hoegh Evi: Igneo 50% (closed Apr 2025) + Hoegh Family 50%. Igneo new ~€2B fund first close Q2 2026 confirmed.

Structure:
Co-invest alongside FSRU operators on conversions or regas capacity contracts.

TOP TARGET:
  Name: Hoegh Evi
  Location: Bermuda / Oslo
  Owner: Igneo Infrastructure Partners (50%, closed Apr 2025) + Hoegh Family / Aequitas (50%)
  Capacity: 9 FSRUs (per current Igneo disclosure) + LNG carriers
  Regulatory: Zeeland EOI closed Mar 6. Commercial process end-2026. FID Q3 2027. Ops Q3 2029. Egypt 10-yr EGAS charter active. FSRU charter rates softening from peak. Igneo's fourth European fund targeting ~€2B first close Q2 2026 (per Infrastructure Investor exclusive).
  Score: 95 (prev 96, dir down)
  Status: Active Pursuit
  Recent change: Score -1: Igneo new fund €2B Q2 2026 first close confirmed (Infrastructure Investor April 2026); Hoegh Evi 50% acquisition closed April 2025 (refresh of last week's framing). UAE OPEC exit May 1 supportive of long-term LNG import structural demand. Hormuz at 5% pre-war volume per May 1 sustains structural FSRU charter premium.
  Open question: Zeeland equity co-invest window before Q3 2027 FID? Igneo €2B fourth European fund LP slot for Mark? GIC->IFM->VTTI chain or direct Igneo GP-level?
  Transaction path: minority growth
  First seen: 2026-02-22 | Days in pipeline: 70

Deal context:
  Seller motivation: Igneo Q2 2026 fund close needs new LPs; Hoegh Evi growth capital for Zeeland and follow-on FSRU conversions.
  Capital use: Igneo €2B fund LP commitment OR Zeeland greenfield FSRU equity co-invest at FID Q3 2027.
  Deal status: Potential
  Process signal: Igneo €2B fund first close confirmed Q2 2026 (Infrastructure Investor, April 2026 exclusive). Zeeland EOI closed March 6, FID Q3 2027. No bilateral conversation with Igneo LP relations or Hoegh management confirmed.

Tomac Cove fit:
  Score: 80
  Angle: sourcing
  Rationale: Mark's BlackRock infrastructure relationships provide bilateral channel to Igneo / First Sentier LP relations team.
  Value-add: LP commitment + Zeeland co-invest layer; Mark's LP-quality relationships earn invitation.
```

### Memo A  _(latency 50.5s, output 3632 tok, cache_read 5921 tok)_

════════════════════════════════════════════════════════════
HOEGH EVI / IGNEO — EUROPEAN FSRU CO-INVEST
IC MEMO — PASS 3
Date: 2026-05-04 | Theme: eu-lng-fsru | Score 92 | Conviction High
════════════════════════════════════════════════════════════

## THE CORE ARGUMENT

European LNG import dependence is now structural, not cyclical. Hormuz remains at ~5% pre-war volume (US blockade continues post April 17 partial reopening), UAE exits OPEC effective May 1 2026 targeting 5M bpd policy independence by 2027, and Qatar's 12.8 MTPA Trains 4+6 are offline 3-5 years. TTF prints €13.90/MMBtu, JKM $15.24/MMBtu, and EU jet fuel reserves sit at six weeks at $1,700-2,100/tonne. FSRU charter rates have softened from peak but the multi-year regas deficit underwrites premium capacity pricing through at least 2029.

Hoegh Evi (9 FSRUs + carriers, Igneo 50% / Hoegh Family 50%) is the cleanest pure-play exposure. Two entry points are live simultaneously: (i) Igneo's fourth European infrastructure fund, ~€2B target, first close confirmed Q2 2026 per Infrastructure Investor, and (ii) Zeeland greenfield FSRU co-invest at FID Q3 2027 (EOI closed March 6, ops Q3 2029). Check size $100M-$1B targeting 10-20% project IRR plus commodity sharing. Mark's BlackRock LP-relations heritage is the sourcing angle — bilateral channel to Igneo / First Sentier LP team before the fund closes.

## POINTS OF CONSENSUS

- **Structural deficit is real, not narrative.** Hormuz 5% throughput, UAE OPEC exit, Qatar 12.8 MTPA offline 3-5 yrs — three independent signals point to multi-year EU regas tightness.
- **Hoegh Evi is the asset.** 9 FSRUs operating, Egypt 10-yr EGAS charter active, Zeeland in development. Igneo closed its 50% April 2025 — ownership is settled and growth-capital-receptive.
- **Igneo Q2 2026 first close is the timing window.** Confirmed by Infrastructure Investor April 2026 exclusive. ~€2B target. New LPs being onboarded now.
- **TC right-to-win is sourcing, not capital.** Mark's BlackRock relationships into Igneo / First Sentier LP relations earn the invitation; we are not competing on price.

## POINTS OF DISAGREEMENT OR TENSION

- **Score moved 96 → 95 (down).** FSRU charter rates softening from peak — entry timing still favorable but the curve is no longer accelerating.
- **Two structurally different products in one target.** Igneo €2B LP commitment is a fund-of-9-plus-future-FSRUs blind pool; Zeeland co-invest is a single asset at FID. Different IRRs, different J-curves, different governance. We have not decided which.
- **Indirect access path unproven.** GIC → IFM → VTTI chain referenced as alternative routing. No confirmation that path opens Igneo GP-level access vs direct LP relations outreach.

## OPEN QUESTIONS AND UNRESOLVED ISSUES

- Does Zeeland have an equity co-invest sleeve sized for $100M-$1B before Q3 2027 FID, or is it pre-committed to Igneo balance sheet?
- Is there a Mark-sized LP slot in Igneo's €2B fourth European fund first close Q2 2026, and what are minimums?
- Direct Igneo GP-level access vs GIC → IFM → VTTI indirect chain — which opens the door faster?
- FSRU charter rate trajectory: how much softening is already priced into Hoegh Evi's NAV, and what is the floor underwritten by Egypt EGAS + Zeeland contracted offtake?
- US blockade duration on Hormuz — base case for re-normalization timeline materially affects 2027-2029 FSRU pricing.

## WHAT YOU WOULD NEED TO FORM A VIEW

- Bilateral conversation: Mark to Igneo / First Sentier LP relations team re: Q2 2026 first close LP slot and minimum.
- Bilateral conversation: Hoegh Evi management (Bermuda / Oslo) re: Zeeland equity co-invest window, FID timing, and capital structure intent.
- Charter book diligence: contracted vs spot mix across the 9 FSRUs, weighted-average charter duration, counterparty quality (EGAS plus 8 others).
- Zeeland greenfield economics: capex, contracted offtake, EU regulatory path post March 6 EOI close, commercial process end-2026.
- Igneo fourth fund LPA review: co-invest rights, GP economics, sector allocation guardrails, Hoegh Evi follow-on capacity.
- Comp set: FSRU operator multiples (Excelerate, New Fortress regas, BW LNG) and recent secondaries on Igneo's prior European funds.

## KEY NAMES AND FIRMS

- **Hoegh Evi** / Bermuda + Oslo — 9 FSRUs plus LNG carriers; target asset; 50% Igneo / 50% Hoegh Family + Aequitas.
- **Igneo Infrastructure Partners** / First Sentier — 50% owner of Hoegh Evi (closed April 2025); fourth European fund ~€2B first close Q2 2026.
- **Hoegh Family / Aequitas** — 50% co-owner of Hoegh Evi.
- **EGAS** / Egypt — 10-yr FSRU charter counterparty.
- **Zeeland** / Netherlands — greenfield FSRU project; EOI closed March 6; FID Q3 2027; ops Q3 2029.
- **GIC / IFM / VTTI** — indirect access chain referenced as alternative routing.
- **Mark Saxe** / Tomac Cove — sourcing angle; BlackRock infra LP relationships into Igneo / First Sentier.
- **Infrastructure Investor** — source for Igneo €2B Q2 2026 first close confirmation (April 2026 exclusive).

════════════════════════════════════════════════════════════
ACTION ITEMS
════════════════════════════════════════════════════════════

[ACTION-001]
Date/Deadline : 2026-05-15
Time          : TBD
Action        : Mark to initiate bilateral outreach to Igneo / First Sentier LP relations re: €2B fourth European fund Q2 2026 first close, request LP slot sizing and minimums.
Owner         : Mark
Parties       : Mark Saxe (Tomac Cove), Igneo Infrastructure Partners LP relations, First Sentier
Context       : Q2 2026 first close window is open now; Mark's BlackRock relationships are the sourcing edge and decay if not activated pre-close.
Dashboard     : Both
Priority      : High

[ACTION-002]
Date/Deadline : 2026-05-22
Time          : TBD
Action        : Schedule introductory call with Hoegh Evi management (Bermuda or Oslo) re: Zeeland equity co-invest window before Q3 2027 FID.
Owner         : Yoni
Parties       : Yoni Gontownik, Hoegh Evi management, Hoegh Family / Aequitas
Context       : Zeeland EOI closed March 6; commercial process runs through end-2026; co-invest sleeve sizing must be confirmed before FID.
Dashboard     : Both
Priority      : High

[ACTION-003]
Date/Deadline : 2026-05-18
Time          : TBD
Action        : Pull FSRU charter rate curve and Excelerate / New Fortress / BW LNG comp set for Hoegh Evi NAV triangulation.
Owner         : Yoni
Parties       : Yoni Gontownik
Context       : Score moved 96 → 95 on charter rate softening; need to quantify how much is priced into entry.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-004]
Date/Deadline : 2026-05-25
Time          : TBD
Action        : Diligence GIC → IFM → VTTI indirect access chain vs direct Igneo GP-level outreach; determine fastest path to Hoegh Evi exposure.
Owner         : Yoni
Parties       : Yoni Gontownik, Mark Saxe, GIC contacts (TBD), IFM contacts (TBD)
Context       : Two routing options on the table; need to pick before Q2 2026 Igneo first close to avoid running both in parallel and burning relationships.
Dashboard     : Both
Priority      : Medium

[ACTION-005]
Date/Deadline : 2026-06-05
Time          : TBD
Action        : Request Igneo fourth fund LPA draft for review of co-invest rights, sector allocation, and Hoegh Evi follow-on capacity.
Owner         : Mark
Parties       : Mark Saxe, Igneo LP relations, Tomac Cove counsel
Context       : LPA terms drive whether the LP commitment delivers Zeeland co-invest optionality or is purely blind-pool exposure.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-006]
Date/Deadline : No deadline
Time          : TBD
Action        : Monitor Hormuz throughput weekly and track US blockade policy signals for re-normalization base case affecting 2027-2029 FSRU pricing.
Owner         : Yoni
Parties       : Yoni Gontownik
Context       : Hormuz at 5% pre-war volume sustains FSRU charter premium; base case timeline materially affects deal IRR.
Dashboard     : Deal Pipeline
Priority      : Low

════════════════════════════════════════════════════════════
DASHBOARD ROUTING SUMMARY
════════════════════════════════════════════════════════════

Chief of Staff Dashboard → 0 items
Deal Pipeline Dashboard  → 2 items: ACTION-003, ACTION-005, ACTION-006 (3 items)
Both                     → 3 items: ACTION-001, ACTION-002, ACTION-004
No routing needed        → 0 items

### Memo B  _(latency 66.4s, output 3328 tok, cache_read 4131 tok)_

# IC MEMO — EUROPEAN LNG REGAS & FSRU (HOEGH EVI)
**Theme: eu-lng-fsru | Score: 92 | Conviction: High | Timing: Immediate**
**Date: May 4, 2026 | Check Size: $100M–$1B | Target Returns: 10–20% project IRR + commodity upside**

---

## THE CORE ARGUMENT

The Qatar FM curtailment (Trains 4+6 offline, 12.8 MTPA, 3–5 year horizon) combined with Hormuz operating at ~5% of pre-war volume as of May 1 has created a structural multi-year LNG supply deficit into Europe that cannot be resolved by spot market mechanics alone. TTF at €13.90/MMBtu and JKM at $15.24/MMBtu confirm the price signal. EU jet fuel reserves at 6 weeks ($1,700–2,100/tonne) underscore how thin European energy import infrastructure remains. FSRU charter rates, while softening from peak, are still elevated relative to mid-cycle — and structural demand (not spot demand) is driving the investment case.

The entry point is Hoegh Evi: 9 FSRUs, Bermuda/Oslo domicile, 50% owned by Igneo Infrastructure Partners (acquisition closed April 2025) + 50% Hoegh Family/Aequitas. Igneo's fourth European fund is confirmed at ~€2B first close in Q2 2026. Two parallel access paths exist: (1) LP commitment into the Igneo €2B fund before first close, and (2) direct equity co-invest on the Zeeland FSRU greenfield at FID (Q3 2027). The Tomac Cove right-to-win is Mark's BlackRock infrastructure LP-relationship network — a bilateral channel to Igneo's LP relations team that no cold-call pitch replicates. This is a sourcing play, not a competitive auction. The window is Q2 2026 for the fund LP slot and Q3 2027 for Zeeland co-invest. Neither is open indefinitely.

---

## POINTS OF CONSENSUS

- **Structural deficit is real and multi-year.** Qatar FM offline 3–5 years at 12.8 MTPA is not a temporary disruption — it resets European LNG import dependency structurally.
- **Hormuz is not normalizing.** US blockade continues; volume at ~5% of pre-war levels as of May 1. UAE OPEC exit effective May 1 (targeting 5M bpd by 2027) adds long-term supply policy uncertainty that benefits LNG import infrastructure holders.
- **Hoegh Evi is the right vehicle.** 9 FSRUs with an active 10-year EGAS charter (Egypt), Igneo as institutional co-owner, and the Zeeland process as a concrete near-term capital event. Not a greenfield bet — operating assets with a defined growth pipeline.
- **Igneo fund timing creates LP urgency.** First close confirmed Q2 2026 per *Infrastructure Investor* (April 2026 exclusive). LP slot decisions are being made now.
- **Mark's network is the moat.** BlackRock infrastructure LP relationships provide direct access to Igneo's LP relations and potentially First Sentier's broader GP network. This is the reason Tomac gets invited; without it, this is not an actionable opportunity.

---

## POINTS OF DISAGREEMENT OR TENSION

- **Charter rate direction is adverse.** Score moved from 96 → 95 on softening FSRU charter rates from peak. If spot rates continue to decline before Zeeland reaches FID (Q3 2027), the co-invest entry multiple compresses while structural demand remains unproven in underwriting.
- **Hormuz reopening risk is asymmetric.** A full Hormuz reopening (not currently the base case, but not zero probability) would rapidly reduce European structural LNG import dependency, deflating the thesis. The 5%/pre-war volume figure is a snapshot, not a forecast.
- **Igneo fund LP vs. Zeeland co-invest: which path?** These are not the same risk profile. An LP commitment into the €2B fund is diversified, manager-risk-bearing, and less control. A Zeeland co-invest at FID is concentrated, asset-specific, and has a 2027–2029 construction period. The memo does not resolve which path Tomac would pursue or whether the check size ($100M–$1B) applies to one or both.
- **Process signal is weak.** No bilateral conversation with Igneo LP relations or Hoegh management has been confirmed. Intelligence is public (Infrastructure Investor, Igneo disclosure) — not proprietary. The competitive sourcing advantage exists only if Mark actually executes the outreach before first close.

---

## OPEN QUESTIONS AND UNRESOLVED ISSUES

- **Zeeland co-invest window timing**: Does Igneo offer co-invest rights to LP commitments made at first close (Q2 2026), or only to follow-on investors closer to FID (Q3 2027)? This determines whether the two paths are sequential or independent.
- **Igneo €2B fund: LP minimum commitment and terms**: What is the minimum LP ticket size for a new manager of Tomac Cove's profile? First close pricing typically carries a fee discount — is Tomac Cove's $100M–$1B range sufficient for a first-close LP conversation?
- **GP-level access chain**: The brief references a GIC→IFM→VTTI chain as an alternative to direct Igneo GP-level access. What is the correct bilateral path — through Igneo LP relations, through First Sentier (Igneo's parent), or through a shared LP intermediary?
- **EGAS charter terms**: The Egypt 10-year EGAS charter is described as "active" but no rate or expiry date is provided. Understanding the remaining term and rate lock is central to valuing the existing fleet cashflow floor.
- **Hoegh Family / Aequitas 50% stake**: Is the Hoegh Family side exploring any liquidity? A full or partial secondary on the family stake would open a different acquisition path entirely and bypass the Igneo fund structure.
- **Zeeland regulatory status**: EOI closed March 6, commercial process end-2026, FID Q3 2027. What is the permitting risk between commercial process close and FID? Any parallel Dutch/Belgian regulatory dependencies?
- **FSRU charter rate floor**: How far do rates need to fall before the Zeeland underwriting breaks? Is there a contracted-rate floor in the Zeeland commercial process, or is it floating?

---

## WHAT YOU WOULD NEED TO FORM A VIEW

- **Igneo €2B fund data room / PPM**: Request via Mark's network. Key items: fund mandate, co-invest rights provisions, first-close LP discount, Hoegh Evi portfolio weight, Zeeland capital plan and timeline.
- **Hoegh Evi FSRU fleet utilization and charter roll schedule**: Which of the 9 FSRUs are contracted, at what rates, and when do charters expire? Spot rate exposure vs. locked cashflow by vessel.
- **Zeeland project economics**: FSRU capacity (MTPA), tolling structure vs. equity, capex estimate, projected COD Q3 2029, contractual off-take at FID.
- **FSRU charter rate deck**: Current spot and 12-month forward rates for FSRUs of Hoegh Evi's vessel class. Need a third-party view (Fearnley, Clarksons) on rate normalization trajectory through 2027.
- **Qatar FM restoration timeline**: What is the credible restoration range for Trains 4+6? 3 years (base) vs. 5 years (bear) has a material impact on European structural LNG import demand through the investment holding period.
- **Hormuz scenario analysis**: Probability-weighted impact on TTF and JKM of partial vs. full Hormuz normalization. Does the IRR hold at 10–20% if TTF reverts to €9–10/MMBtu?
- **Mark-to-Igneo relationship map**: Which specific individuals at Igneo LP relations has Mark worked with, and at what seniority? Is this a direct GP conversation or a two-hop introduction?

---

## KEY NAMES AND FIRMS

| Name / Firm | Context |
|---|---|
| **Hoegh Evi** | Primary target. 9 FSRUs + LNG carriers. Bermuda/Oslo domicile. |
| **Igneo Infrastructure Partners** | 50% owner of Hoegh Evi (closed April 2025). Managing ~€2B fourth European fund, first close Q2 2026. Subsidiary of First Sentier Investors. |
| **Hoegh Family / Aequitas** | 50% owner of Hoegh Evi. Founding family. No current liquidity signal. |
| **First Sentier Investors** | Parent of Igneo. Australian/global asset manager. Potential intermediary channel. |
| **EGAS (Egyptian Natural Gas Holding Company)** | Counter-party on active 10-year Hoegh Evi FSRU charter. Egypt. |
| **Qatar FM (LNG)** | Trains 4+6 offline; 12.8 MTPA out of global market for 3–5 years. Core demand driver. |
| **Mark Saxe** | Tomac Cove co-founder (ex-BlackRock infra). Primary relationship channel to Igneo LP relations. |
| **GIC / IFM / VTTI** | Referenced in brief as an alternative GP-access chain. Exact pathway to Igneo unclear. |
| ***Infrastructure Investor*** | Published April 2026 exclusive confirming Igneo €2B fund first close target Q2 2026. Primary public signal. |

---

## ACTION ITEMS

════════════════════════════════════════════════════════════
ACTION ITEMS
════════════════════════════════════════════════════════════

[ACTION-001]
Date/Deadline : 2026-05-09
Time          : TBD
Action        : Mark to map his direct BlackRock-era contacts at Igneo LP relations / First Sentier and identify the highest-seniority bilateral path before Igneo Q2 2026 first close.
Owner         : Mark Saxe
Parties       : Mark Saxe (Tomac Cove) → Igneo Infrastructure Partners LP Relations / First Sentier Investors
Context       : Igneo €2B fourth European fund first close confirmed Q2 2026; LP slot and potential Zeeland co-invest rights require relationship activation before close. No bilateral conversation confirmed yet.
Dashboard     : Both
Priority      : High

[ACTION-002]
Date/Deadline : 2026-05-14
Time          : TBD
Action        : Request Hoegh Evi FSRU fleet charter roll schedule and Zeeland project term sheet / commercial process materials from Igneo or via Mark's network.
Owner         : Yoni / Mark Saxe
Parties       : Tomac Cove → Igneo Infrastructure Partners
Context       : Cannot underwrite Zeeland co-invest or LP commitment without fleet utilization data, EGAS charter terms, and Zeeland project economics (capacity, tolling structure, COD Q3 2029).
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-003]
Date/Deadline : 2026-05-11
Time          : TBD
Action        : Commission third-party FSRU charter rate deck from Fearnley or Clarksons covering spot + 12-month forward rates for Hoegh Evi vessel class.
Owner         : Yoni
Parties       : Tomac Cove → Fearnley LNG / Clarksons Research
Context       : Charter rates softening from peak (score -1 on this factor). Need an independent view on rate normalization trajectory through Q3 2027 FID to stress-test Zeeland co-invest IRR.
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-004]
Date/Deadline : 2026-05-16
Time          : TBD
Action        : Pull Qatar FM Trains 4+6 restoration timeline consensus from LNG broker contacts and build 3-year vs. 5-year demand scenario for European structural LNG import dependency.
Owner         : Yoni
Parties       : Tomac Cove → LNG broker / commodity desk contacts (TBD)
Context       : Qatar FM offline (12.8 MTPA, 3–5 years) is the core structural demand driver. Range matters materially for IRR underwriting through the holding period.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-005]
Date/Deadline : 2026-05-19
Time          : TBD
Action        : Determine whether Igneo LP co-invest rights on Zeeland attach at first close (Q2 2026) or separately at FID (Q3 2027), and confirm minimum LP ticket size for a new manager at Tomac Cove's scale.
Owner         : Mark Saxe
Parties       : Mark Saxe (Tomac Cove) → Igneo LP Relations
Context       : The two entry paths (LP fund commitment vs. Zeeland direct co-invest) may be sequential or independent — this determines which to prioritize and what check size to bring to the first conversation.
Dashboard     : Both
Priority      : High

════════════════════════════════════════════════════════════
DASHBOARD ROUTING SUMMARY
════════════════════════════════════════════════════════════

Chief of Staff Dashboard  → 0 items (no scheduling/recruiting/admin actions)
Deal Pipeline Dashboard   → 3 items: ACTION-002, ACTION-003, ACTION-004
Both                      → 2 items: ACTION-001, ACTION-005
No routing needed         → 0 items

---

## Theme: DC Electrical Contractor Roll-up (id: `epc-rollup`)

### Pass 2 brief (input to both models)

```
PASS 2 BRIEF — DC Electrical Contractor Roll-up

Theme ID: epc-rollup
Score: 88 / Conviction: High / Timing: Immediate
Check size: $75M-$800M
Target returns: 25-35% IRR, 3-4x MOIC

Thesis:
$255B market, no player >5%. LGN 2026 guidance $3.7-3.9B rev / $400-430M EBITDA; backlog $3.7B (+49%); DC = 49% of backlog. Faith Technologies ABC #1 DC contractor 2026, $1.6B revenue (Pittsboro IN expansion April 21 — $67.5M, 200 jobs). SoftBank Piketon mid-2026. Meta Richland Parish peak 5,000 workers June 2026; $875M LA contracts. DT Midstream 900 MW Indiana 20-yr power dev contract (May 1) adds parallel labor demand. PJM RBP 14.9 GW labor demand 2027-2031.

Structure:
Platform at 6-8x, add-ons at 4-6x. Exit 10-12x to LGN/DY/STRL/CBRE in 3-5 yrs.

TOP TARGET:
  Name: Tier 2/3 Private Subs (OH/PA/TX/VA/IN/LA)
  Location: SoftBank/Meta/Faith corridors + DT Midstream IN gas dev
  Owner: Family-owned, 20-40 yr old
  Capacity: $20-80M rev, 50-200 journeymen
  Regulatory: LGN guidance reaffirmed. Faith Pittsboro IN. Meta Richland Parish peak 5,000 by June 2026. SoftBank Piketon mid-2026. PJM RBP 14.9 GW labor demand layer. DT Midstream 900 MW Indiana 20-yr contract (May 1) adds 4th IN corridor data point.
  Score: 97 (prev 97, dir unchanged)
  Status: Active Pursuit
  Recent change: DT Midstream 900 MW Indiana 20-yr power development contract (May 1) reinforces IN corridor labor demand alongside Faith Pittsboro and SoftBank Piketon. ElectriGen 1.8 GW Texas BTM gas LOI (Apr 27) adds ERCOT data point. Anchor sourcing remains the gating step.
  Open question: Specific named Tier 2/3 sub targets in IN (Pittsboro/Indianapolis), LA (Richland Parish), OH (Piketon), TX (ERCOT)? Anchor platform candidate selection?
  Transaction path: minority growth
  First seen: 2026-02-22 | Days in pipeline: 70

Deal context:
  Seller motivation: Family ownership succession; ESOP exits; capital constraints in scaling for hyperscale workloads.
  Capital use: Platform formation + add-on M&A; 6-8x platform, 4-6x add-ons; 3-5 yr exit to LGN/DY/STRL.
  Deal status: Potential
  Process signal: Confirmed structural demand catalysts: Faith Pittsboro IN April 21, SoftBank Piketon mid-2026, Meta peak 5,000 workers June 2026, PJM RBP 14.9 GW. No specific named anchor target locked yet — sourcing is the gating step.

Tomac Cove fit:
  Score: 80
  Angle: operating
  Rationale: Yoni's power-markets background credibly differentiates TC from generic PE for sponsor selection at family-owned subs (PJM/MISO hyperscaler customer credibility).
  Value-add: Sector network + RBP intelligence + hyperscaler customer relationships make TC the credibility partner for $20-80M shop owners.
```

### Memo A  _(latency 52.6s, output 3929 tok, cache_read 5921 tok)_

# IC MEMO — DC Electrical Contractor Roll-up

**Theme:** epc-rollup | **Score:** 88 | **Conviction:** High | **Timing:** Immediate
**Check:** $75M–$800M | **Returns:** 25–35% IRR, 3–4x MOIC | **Path:** Minority growth / platform formation

═══════════════════════════════════════════════════════════

## THE CORE ARGUMENT

The DC electrical contracting market is a $255B fragmented opportunity with no player above 5% share, and the demand catalysts are now stacked and dated. LGN's 2026 guidance ($3.7–3.9B revenue, $400–430M EBITDA, backlog +49% to $3.7B with DC at 49%) confirms the public comps are working, and Faith Technologies' ascent to #1 DC contractor in 2026 ($1.6B revenue) — punctuated by the April 21 Pittsboro IN expansion ($67.5M, 200 jobs) — proves Tier 1 capacity is binding. The arbitrage is clear: buy Tier 2/3 family-owned subs at 4–6x in the SoftBank Piketon, Meta Richland Parish, Faith Pittsboro, and DT Midstream Indiana corridors, build a platform at 6–8x, exit at 10–12x to LGN, DY, STRL, or CBRE in 3–5 years.

Tomac Cove's right-to-win is operating-angle credibility: Yoni's PJM/MISO power-markets background differentiates TC from generic PE when courting $20–80M family-owned shop owners who care about hyperscaler customer continuity post-close. The platform thesis is structurally sound; the gating step is anchor target selection, not market validation.

## POINTS OF CONSENSUS

- **Market structure validates roll-up:** $255B TAM, no player >5%, family-owned succession dynamics across 20–40 year-old shops.
- **Demand is dated and stacked:** Faith Pittsboro IN April 21 (live), SoftBank Piketon OH mid-2026, Meta Richland Parish LA peak 5,000 workers June 2026 ($875M LA contracts), DT Midstream 900 MW Indiana 20-yr power dev contract (May 1), PJM RBP 14.9 GW labor demand layer 2027–2031.
- **Public comp economics work:** LGN 2026 guidance reaffirmed; backlog +49%; DC = 49% of backlog confirms hyperscale concentration is rewarded, not penalized.
- **Multiple arbitrage is real:** 4–6x add-on entry, 6–8x platform, 10–12x exit to LGN/DY/STRL/CBRE.
- **Geographic concentration is intentional:** OH/PA/TX/VA/IN/LA mapped to named anchor demand.

## POINTS OF DISAGREEMENT OR TENSION

- **Anchor platform vs. greenfield build:** Brief flags "anchor sourcing remains the gating step" — unclear whether to acquire a Tier 2 platform first or assemble from multiple Tier 3 shops simultaneously.
- **Indiana corridor concentration risk:** Three IN data points (Faith Pittsboro, SoftBank Piketon adjacent, DT Midstream 900 MW) — concentration is a feature for sourcing but a risk if any single hyperscaler reschedules.
- **Minority growth structure tension:** Transaction path listed as minority growth, but platform roll-up economics typically require control. Needs clarification on TC's preferred ownership posture.

## OPEN QUESTIONS AND UNRESOLVED ISSUES

- Specific named Tier 2/3 sub targets in IN (Pittsboro/Indianapolis), LA (Richland Parish), OH (Piketon), TX (ERCOT) — none identified yet.
- Anchor platform candidate selection: who is the cornerstone acquisition?
- Journeyman labor availability vs. demand stack — 5,000 peak workers at Meta alone suggests wage inflation pressure on margins.
- Union vs. open-shop posture across target geographies (LA/TX open shop; PA/OH mixed).
- Bonding capacity of $20–80M revenue targets at hyperscale project scale.
- Customer concentration tolerance — what % of platform revenue from single hyperscaler is acceptable to exit buyers?

## WHAT YOU WOULD NEED TO FORM A VIEW

- **Named target list:** 8–12 Tier 2/3 subs across the four corridors with revenue, journeyman count, ownership age, and bonding capacity.
- **Faith Technologies / LGN management calls:** validate Tier 1 capacity constraint and sub-contracting flow-down economics.
- **Hyperscaler procurement check:** Meta, SoftBank/Stargate, AWS sourcing teams on multi-vendor preferences and qualification thresholds for new entrants.
- **Bonding and surety market diligence:** capacity for $500M+ aggregate platform on hyperscale jobs.
- **Wage inflation data:** IBEW Local rates IN/OH/LA/TX 2024–2026; non-union benchmarks.
- **Comparable transactions:** recent platform formations (Bernhard, Faith pre-ABC, DY add-ons) — multiples paid, structure, earn-out terms.
- **Anchor candidate IOIs:** soft-circle 2–3 platform candidates before committing capital.

## KEY NAMES AND FIRMS

- **Faith Technologies / ABC** — #1 DC contractor 2026, $1.6B revenue, Pittsboro IN expansion April 21 ($67.5M, 200 jobs).
- **LGN (Limbach / proxy)** — Public comp; 2026 guidance $3.7–3.9B rev, $400–430M EBITDA, backlog $3.7B (+49%), DC = 49%.
- **DY (Dycom)** — Exit comp at 10–12x.
- **STRL (Sterling Infrastructure)** — Exit comp.
- **CBRE** — Strategic exit candidate.
- **SoftBank** — Piketon OH project mid-2026; Stargate-adjacent.
- **Meta** — Richland Parish LA, $875M LA contracts, 5,000 peak workers June 2026.
- **DT Midstream** — 900 MW Indiana 20-yr power dev contract (May 1).
- **ElectriGen** — 1.8 GW Texas BTM gas LOI (April 27); ERCOT data point.
- **PJM** — RBP 14.9 GW labor demand 2027–2031.

════════════════════════════════════════════════════════════
ACTION ITEMS
════════════════════════════════════════════════════════════

[ACTION-001]
Date/Deadline : 2026-05-18 (2 weeks)
Time          : TBD
Action        : Build named target list of 8–12 Tier 2/3 electrical subs across IN (Pittsboro/Indianapolis), LA (Richland Parish), OH (Piketon), TX (ERCOT) corridors with revenue, journeyman count, ownership age, bonding capacity.
Owner         : Yoni
Parties       : Tomac Cove sourcing; industry consultants; ABC/IBEW directories
Context       : Anchor sourcing is the gating step per Pass 2 brief; without named targets the platform cannot launch.
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-002]
Date/Deadline : 2026-05-11 (1 week)
Time          : TBD
Action        : Schedule call with LGN and Faith Technologies management or sell-side analysts to validate Tier 1 capacity constraint and sub-contracting flow-down economics.
Owner         : Yoni
Parties       : LGN IR; Faith Technologies / ABC; sell-side analysts covering EPC
Context       : Need to confirm $255B market structure and Tier 1 sub-out economics before committing platform capital.
Dashboard     : Both
Priority      : High

[ACTION-003]
Date/Deadline : 2026-05-25 (3 weeks)
Time          : TBD
Action        : Conduct hyperscaler procurement check with Meta, SoftBank/Stargate, AWS sourcing teams on multi-vendor preferences and new-entrant qualification thresholds.
Owner         : Yoni / Mark
Parties       : Meta procurement; SoftBank/Stargate; AWS data center sourcing
Context       : Customer concentration tolerance and qualification gates determine viable platform scale and exit comps.
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-004]
Date/Deadline : 2026-05-18 (2 weeks)
Time          : TBD
Action        : Pull comparable transaction data on recent EPC platform formations (Bernhard, Faith pre-ABC, DY add-ons) — multiples paid, structure, earn-out terms.
Owner         : Mark
Parties       : Tomac Cove research; M&A databases (Pitchbook, Mergermarket)
Context       : Required to validate 4–6x add-on / 6–8x platform / 10–12x exit assumptions.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-005]
Date/Deadline : 2026-05-25 (3 weeks)
Time          : TBD
Action        : Diligence bonding and surety market capacity for $500M+ aggregate platform on hyperscale jobs; pull IBEW Local wage rates IN/OH/LA/TX 2024–2026 and non-union benchmarks.
Owner         : Yoni
Parties       : Surety brokers; IBEW Locals; ABC chapters
Context       : Wage inflation and bonding capacity are the two operating risks that compress 25–35% IRR target.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-006]
Date/Deadline : 2026-06-08 (5 weeks)
Time          : TBD
Action        : Soft-circle 2–3 anchor platform candidates with IOIs to test seller receptivity before committing capital.
Owner         : Yoni / Mark
Parties       : Identified Tier 2 platform candidates (TBD post ACTION-001)
Context       : Brief flags transaction path as minority growth but platform economics may require control — IOI feedback resolves structure question.
Dashboard     : Both
Priority      : High

[ACTION-007]
Date/Deadline : No deadline
Time          : TBD
Action        : Clarify TC ownership posture (minority growth vs. control) given platform roll-up economics typically require control.
Owner         : Yoni / Mark
Parties       : Tomac Cove principals
Context       : Internal alignment needed before approaching anchor candidates with structure proposal.
Dashboard     : CoS
Priority      : Medium

════════════════════════════════════════════════════════════
DASHBOARD ROUTING SUMMARY
════════════════════════════════════════════════════════════

Chief of Staff Dashboard → 1 item: ACTION-007
Deal Pipeline Dashboard  → 4 items: ACTION-001, ACTION-003, ACTION-004, ACTION-005
Both                     → 2 items: ACTION-002, ACTION-006
No routing needed        → 0 items

### Memo B  _(latency 67.2s, output 3407 tok, cache_read 4131 tok)_

# IC MEMO — DC ELECTRICAL CONTRACTOR ROLL-UP
### Theme: `epc-rollup` | Score: 88 | Conviction: High | Date: May 4, 2026

---

## THE CORE ARGUMENT

The US hyperscale buildout is creating a structural labor shortage in DC electrical contracting that the market cannot solve quickly. $255B market, no player above 5% share, dominated by family-owned Tier 2/3 subs running $20-80M in revenue with 50-200 journeymen — companies that cannot scale their balance sheets fast enough to capture the work in front of them. The demand catalysts are not forecasts; they are signed contracts with hard dates: Faith Technologies expanding in Pittsboro IN (April 21, $67.5M, 200 jobs), SoftBank Piketon OH (mid-2026), Meta Richland Parish LA (peak 5,000 workers, June 2026, $875M in contracts), PJM Reliability Backstop Procurement (14.9 GW UCAP, labor demand layer 2027-2031), DT Midstream 900 MW Indiana 20-year power development contract (signed May 1). LGN 2026 guidance of $3.7-3.9B revenue / $400-430M EBITDA with a $3.7B backlog (+49%) and DC at 49% of backlog confirms that the publicly-traded acquirers are already paying premium multiples for exactly this capability.

The play is to build a platform at 6-8x EBITDA by aggregating Tier 2/3 private subs in the OH/PA/TX/VA/IN/LA corridors directly aligned to hyperscaler construction footprints, then exit in 3-5 years at 10-12x to LGN, DY, STRL, or CBRE. Target: 25-35% IRR, 3-4x MOIC. Check size $75M-$800M. The gating step is not thesis validation — it is sourcing the anchor platform candidate.

---

## POINTS OF CONSENSUS

- **Market is fragmented and structurally so.** No contractor above 5% of a $255B market. Consolidation logic is unambiguous.
- **Demand catalysts are confirmed, dated, and geographic.** Four distinct corridor anchors with signed contracts or announced capital: IN (Faith Pittsboro + DT Midstream 900 MW + SoftBank Piketon), LA (Meta Richland Parish), TX (ElectriGen 1.8 GW BTM gas LOI, April 27), OH (SoftBank Piketon).
- **Strategic buyer appetite is real and priced.** LGN guidance reaffirmed at 10-12x implied exit multiple. Backlog +49% YoY with DC at nearly half of total — public comps are telling you what they will pay.
- **Seller universe is actionable.** Family-owned, 20-40 year-old businesses facing succession events, ESOP exits, or capital constraints. Classic PE entry profile. No auction process required at platform entry.
- **Tomac Cove differentiation is credible.** PJM/MISO sector network and RBP intelligence give TC a specific and articulable edge over generic PE in sponsor selection conversations with $20-80M shop owners. Score: 80 TC fit.
- **Transaction path is minority growth equity** — lower entry basis, preserves management alignment, right structure for founder-owned businesses.

---

## POINTS OF DISAGREEMENT OR TENSION

- **No anchor platform candidate identified.** The single most critical variable — which specific company becomes the platform — is unresolved. Everything else is thesis; this is execution. Score of 97 on the target reflects opportunity quality, not deal certainty.
- **Labor supply constraint is a double-edged input.** The same shortage driving valuation also limits scalability post-acquisition. Acquiring a 100-journeyman shop doesn't solve for a 5,000-worker Meta site peak — it requires a labor aggregation strategy that is not yet defined.
- **ElectriGen LOI (April 27, 1.8 GW Texas BTM gas)** is a data point, not a confirmed deal. ERCOT corridor inclusion depends on whether this transaction closes and what it reveals about subcontractor demand in TX.
- **PJM RBP 14.9 GW labor demand layer (2027-2031)** is a structural tailwind but competes with hyperscaler labor for the same journeymen pool. Not clear whether this amplifies or cannibilizes the DC sub thesis.
- **Exit multiple compression risk.** 10-12x exit assumes LGN/DY/STRL strategic appetite persists. If public market multiples compress or strategic acquirers face integration digestion post-2027, exit window narrows.

---

## OPEN QUESTIONS AND UNRESOLVED ISSUES

- **Who are the named Tier 2/3 subs in the four IN corridors** (Pittsboro/Indianapolis area, Piketon OH, Richland Parish LA, ERCOT TX)? This is the single open question that gates everything.
- **Anchor platform candidate selection**: What is the screening framework — revenue floor, journeyman count, union vs. non-union, geography breadth, existing hyperscaler relationships?
- **DT Midstream 900 MW Indiana 20-year contract (signed May 1):** Who are the electrical subs already engaged on this project? Do any qualify as platform candidates?
- **Meta Richland Parish $875M LA contracts:** Which subs are currently holding paper on this work? Contract holders are the most actionable acquisition targets.
- **Faith Technologies Pittsboro IN expansion ($67.5M, April 21):** Faith is ABC's #1 DC contractor at $1.6B revenue — is it a competitor, a potential LP/advisor, or an exit acquirer? Clarify its role in the ecosystem.
- **Labor aggregation model:** Post-acquisition, how does the platform solve for peak site demand (5,000 workers) without organic headcount that takes years to certify?
- **Union exposure:** OH and PA journeymen markets are heavily IBEW. What is the labor relations strategy at the platform level?
- **ESOP exit mechanics:** How does minority growth equity interact with ESOP succession structures? Legal/tax overlay needs diligence.

---

## WHAT YOU WOULD NEED TO FORM A VIEW

- **Named target list:** Commission a market map of Tier 2/3 private electrical contractors in IN (Pittsboro, Indianapolis, Terre Haute), OH (Piketon, Columbus), LA (Richland Parish, Baton Rouge), TX (ERCOT service territory). Filter: $20-80M revenue, 50-200 journeymen, DC work >30% of revenue. 15-20 names to start.
- **Sub contract holder identification:** Pull public filings and press releases for Faith Pittsboro, Meta Richland Parish, SoftBank Piketon, DT Midstream Indiana — identify named electrical subs on each project. These are warm approach targets.
- **LGN/DY M&A activity check:** What have LGN and DY acquired in the last 24 months, at what multiples, and in which geographies? Confirms exit comp set and flags names they already own (to avoid building into their portfolio).
- **IBEW penetration by corridor:** Union density data for IN/OH/LA/TX electrical markets. Determines labor strategy and wage stack assumptions.
- **Expert calls:** One call each with a DC GC project manager (Faith or peer), an IBEW business manager in IN or OH, and an M&A advisor active in specialty contractor space (Capstone Partners, FOCUS Investment Banking, or Construction Executive network).
- **Minority growth equity precedent set:** Find 3-5 closed minority PE deals in specialty contractor space ($20-80M revenue) — validate entry multiple range (6-8x), governance structure, and founder retention outcomes.
- **Platform CEO candidate:** If the founding owner stays as operator, what's the succession plan? If TC installs a CEO, who? This question needs an answer before first close.

---

## KEY NAMES AND FIRMS

| Name / Firm | Context |
|---|---|
| **LGN (Limbach Holdings)** | Public specialty contractor; 2026 guidance $3.7-3.9B rev / $400-430M EBITDA; backlog $3.7B (+49%); DC = 49% of backlog; primary exit acquirer comp |
| **DY (Dycom Industries)** | Public specialty contractor; exit acquirer comp set |
| **STRL (Sterling Infrastructure)** | Public specialty contractor; exit acquirer comp set |
| **CBRE** | Facilities/infrastructure services; exit acquirer comp set |
| **Faith Technologies** | ABC #1 DC contractor 2026; $1.6B revenue; Pittsboro IN expansion April 21 ($67.5M, 200 jobs); competitor / ecosystem reference |
| **SoftBank** | Piketon OH project; mid-2026 construction peak; demand catalyst |
| **Meta** | Richland Parish LA; $875M contracts; peak 5,000 workers June 2026; demand catalyst |
| **DT Midstream** | 900 MW Indiana 20-year power development contract signed May 1, 2026; 4th IN corridor data point; sub engagement TBD |
| **ElectriGen** | 1.8 GW Texas BTM gas LOI, April 27; ERCOT corridor signal |
| **PJM** | Reliability Backstop Procurement; 14.9 GW UCAP target; 2027-2031 labor demand layer |
| **Yoni Gontownik / Tomac Cove** | Deal principal; PJM/MISO network + RBP intelligence as TC-specific credibility angle with family-owned subs |
| **Mark Saxe / Tomac Cove** | Co-founder; ex-BlackRock infra |

---

## ACTION ITEMS

```
════════════════════════════════════════════════════════════
ACTION ITEMS
════════════════════════════════════════════════════════════

[ACTION-001]
Date/Deadline : 2026-05-11
Time          : TBD
Action        : Commission market map of Tier 2/3 private DC electrical contractors
                in IN/OH/LA/TX corridors — filter $20-80M revenue, 50-200 journeymen,
                DC work >30% of revenue; target 15-20 named firms.
Owner         : Yoni
Parties       : Capstone Partners / FOCUS Investment Banking / Construction Executive
                network (one firm to engage)
Context       : Named target list is the single gating step before any investment
                activity; everything else is thesis.
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-002]
Date/Deadline : 2026-05-11
Time          : TBD
Action        : Pull named electrical subcontractors from public filings and press
                releases for Faith Pittsboro IN, Meta Richland Parish LA, SoftBank
                Piketon OH, and DT Midstream Indiana 900 MW project.
Owner         : Yoni
Parties       : Faith Technologies, Meta, SoftBank, DT Midstream (public filings only
                — no NDA required at this stage)
Context       : Contract holders on active hyperscaler sites are the warmest approach
                targets; eliminates cold sourcing.
Dashboard     : Deal Pipeline
Priority      : High

[ACTION-003]
Date/Deadline : 2026-05-15
Time          : TBD
Action        : Pull LGN and DY M&A acquisition log for last 24 months — geographies,
                multiples paid, company sizes — to validate exit comp set and identify
                geographies they already own.
Owner         : Yoni
Parties       : Bloomberg / Capital IQ / public filings
Context       : Confirms 10-12x exit multiple assumption and flags names to avoid
                acquiring into LGN/DY existing portfolio.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-004]
Date/Deadline : 2026-05-18
Time          : TBD
Action        : Schedule expert call with DC general contractor project manager
                (Faith Technologies peer network) to validate labor shortage,
                sub selection criteria, and union exposure in IN/OH corridors.
Owner         : Yoni
Parties       : Faith Technologies peer / GLG or Tegus for expert sourcing
Context       : Labor aggregation model and IBEW exposure are unresolved; expert
                call is fastest path to grounded assumptions before any LOI.
Dashboard     : Both
Priority      : Medium

[ACTION-005]
Date/Deadline : 2026-05-18
Time          : TBD
Action        : Pull 3-5 closed minority growth PE transactions in specialty
                contractor space ($20-80M revenue) to validate 6-8x entry multiple
                range, governance structure, and founder retention outcomes.
Owner         : Yoni
Parties       : PitchBook / Capital IQ
Context       : Entry multiple and minority structure assumptions need precedent
                validation before IC can form a final view.
Dashboard     : Deal Pipeline
Priority      : Medium

[ACTION-006]
Date/Deadline : 2026-05-22
Time          : TBD
Action        : Identify platform CEO candidate profile — determine whether founding
                owner retention is the plan or whether TC installs an operating
                executive, and if so, begin sourcing.
Owner         : Yoni / Mark
Parties       : TC internal; specialty contractor executive network if external hire
Context       : Platform CEO question must be answered before first close; currently
                unaddressed in Pass 2 brief.
Dashboard     : Both
Priority      : Medium

════════════════════════════════════════════════════════════
DASHBOARD ROUTING SUMMARY
════════════════════════════════════════════════════════════

Chief of Staff Dashboard  → 0 items
Deal Pipeline Dashboard   → 4 items: ACTION-001, ACTION-002, ACTION-003, ACTION-005
Both                      → 2 items: ACTION-004, ACTION-006
No routing needed         → 0 items
════════════════════════════════════════════════════════════
```

---

## Answer key (don't peek until you've scored all three)

<details>
<summary>Click to reveal A/B → model mapping</summary>

- **MISO Power Plant Acquisitions** — A = `claude-opus-4-7`, B = `claude-sonnet-4-6`
- **European LNG Regas & FSRU** — A = `claude-opus-4-7`, B = `claude-sonnet-4-6`
- **DC Electrical Contractor Roll-up** — A = `claude-opus-4-7`, B = `claude-sonnet-4-6`

</details>

## Decision frame

- If Opus wins decisively (≥10/15): swap Pass 3 to Opus in `MODEL_ROUTER.md`, monthly cost goes up ~$5-10. Worth it if quality lift is real.
- If Sonnet wins or it's a tie: keep current routing, document the eval, move on.
- If quality is similar but Opus is materially better at one specific dimension (e.g. hedge identification): consider a hybrid — Sonnet for routine memos, Opus only for high-stakes IC packages.