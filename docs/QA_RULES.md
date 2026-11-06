# QA rules: initial set

Derived from reviewing a real escape-of-water schedule of works (a one-bed flat, 5 schedule groups, £20.3k net) against its Matterport scan, 11 October 2026. The source document is live claim data and is **not** stored in this repo.

Each rule produces a finding with severity `error` (likely wrong £), `warn` (likely incomplete or inconsistent) or `info`.

| ID | Rule | Severity | Observed in the source schedule |
|---|---|---|---|
| R01 | `M2` item whose quantity equals the room's perimeter (or an `M` item equal to its floor area) | error | Lounge vinyl and plywood at 17.6 m², equal to the skirting length; the room's floor is 29.4 m² (34 m² on the scan) |
| R02 | Item with unit `EACH` and "per room" wording, with a non-integer quantity | error | Anti-fungal sealant "standard size room" × 9.24 = £1,471 instead of £159 |
| R03 | Duplicate line: same item code + room + operation | error | Toilet cistern remove/refit listed twice |
| R04 | Duplicate prelim across General Items and a room | warn | Hall/stairs/landing protection in both, at different rates |
| R05 | Same item code priced at different rates in one schedule | warn | Skirting £24/m vs £17/m; protection £16.66 vs £24 per unit |
| R06 | Continuous floor finish replaced in some rooms but not all rooms sharing it | error | Vinyl noted as continuous through all rooms; corridor and a small room unscheduled (~10 m²) |
| R07 | Total scheduled floor area well below scan floor area for a whole-floor finish | warn | 37 m² scheduled vs ~60–65 m² of vinyl on a 69 m² flat |
| R08 | `take_up` without matching `supply` + `fit` | error | Bathroom floor tiles taken up, DPM applied, no new floor covering |
| R09 | `supply` + `fit` of wall tiles without `take_up` / removal | warn | 18.1 m² wall tiles supplied and fitted, no removal line |
| R10 | Stain block without a following paint coat on the same surface | warn | Bedroom 1 ceiling stain block, no ceiling emulsion |
| R11 | Visible damage in evidence with no line on that surface | warn | Lounge walls stained with mould at the skirting line; no wall decoration line |
| R12 | Incident report mentions an element with no line for it | warn | "Damage to walls behind the kitchen units"; no unit remove/refit |
| R13 | Prelim inconsistent with property context | info | Stairs/landing and driveway protection in a 9th-floor flat |
| R14 | Drying required (per incident report) but drying covers less than the affected area | warn | 4 m² minimal drying in one room only |
| R15 | Room present in scan or floor plan with no schedule group | warn | Corridor and small room |
| R16 | Cover data incomplete (date of loss, etc.) | info | Date of loss blank on cover; present in incident report |
| R17 | Spec inconsistency between like rooms (e.g. premium vs standard emulsion) | info | Bedroom 2 premium emulsion, Bedroom 1 standard |

Rules R01, R02, R03, R05, R08–R10 can be checked from the schedule alone. R06, R07, R11, R15 need room geometry or evidence. R12 and R14 need the incident narrative (voice note or typed).
