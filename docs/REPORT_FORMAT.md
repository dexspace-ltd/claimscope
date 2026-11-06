# Report format (from the client's sample schedules)

Structure taken from two sample reports supplied by the founder on 12 October 2026: an *Insurer Schedule* and a *Fully Costed SOW* for the same claim. The documents are live claim data and are **not** stored in this repo; only their layout is recorded here.

## Pages

1. **Cover.** Assessor firm logo (top right). *Claim Manager* box: firm name, address, company number, office number, FCA number, VAT number. *Schedule of Works* table: Insured's Name, Address, City, County, Postcode, Policy No, Claim No, Date of Loss, Type of Loss (e.g. EOW), Peril, Schedule Date (dd/mm/yyyy), Schedule Name, Total Price (NET), Tax (Client), Total Price (GROSS).
2. **Incident report.** Client, Policy/Mortgage Roll No, Claim No, Date of Incident, Date damage noticed, Detail of the damage, Detailed Cause, Further info.
3. **One section per room, General Items first.** Header table (ROOM, Client, Claim Number, Damage, Policy Number); two photos; *Dimensions in metric (M)* table: Wall Size (M2) = Length × Breadth × Height → Total, Square Size (M2), Linear Form (lm); line table: Line item no. (numbering continues across rooms), Sub Category, Description, Unit (Each / HR / M2 / M / NR), Qty, and Cost in the costed version; Group Total; Special Notes (justification text).
4. Footer on every page: firm website and colour bars.

## Two versions

| | Insurer Schedule | Fully Costed SOW |
|---|---|---|
| Line costs | No | Yes, per line |
| Totals | Cover only | Cover and per-room Group Total |

## What ClaimScope mirrors (demo)

`apps/web/report.html` follows this structure on the Dexspace letterhead (decision A18), with a Fully costed / Insurer schedule switch. Fields ClaimScope does not collect yet (insured's name, address, policy number, incident report) show as blank or "—".

## What the comparison showed (12 Oct 2026)

A first draft from an auto-walk of the same property, compared with `tools/compare` against the Fully Costed SOW: 4 of 77 report lines matched; £2.5k draft vs £20.3k report (net). Most of the report's value is strip-out and reinstatement the scan cannot show (continuous vinyl through all rooms, damp subfloor needing DPM, plywood, latex screed) plus 13 general items. The assessor knows these from the incident report and moisture readings. Next: capture the incident report (typed or voice) and feed it to itemisation, apply continuous-floor rules across rooms (QA R06), and extend the general-items rules and catalogue.

## Incident brief: measured effect (12 Oct 2026)

Same property, same 68 auto-walk views, no floor plan, placeholder rates. Matched against the 77-line Fully Costed SOW with `tools/compare` (locally). The brief was typed in the assessor's words (cause, affected areas, continuous vinyl, damp subfloor needing DPM, walls behind kitchen units), with no names, addresses or dates.

| Draft | Lines matched within rooms | Report lines covered property-wide | Report value covered | Draft net |
|---|---|---|---|---|
| First draft (before changes) | 4 (5%) | – | – | £2,549 |
| New pipeline, no brief | 8 (10%) | 13 (17%) | £2,988 | £2,212 |
| New pipeline, with brief | 26 (34%) | 40 (52%) | £13,415 of £20,295 (66%) | £9,762 |

Remaining gap: (1) room assignment without a floor plan (rooms fragment; two bedrooms merged); read the floor plan before walking. (2) Placeholder rates are well below the client's (e.g. vinyl supply about a fifth); curated or client rates (D4). (3) Some scope: wall tiles behind units, plaster, radiators, WC refit.
