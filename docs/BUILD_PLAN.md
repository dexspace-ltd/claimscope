# Build plan: demo + 4-week pilot

Matches the proposal sent to the client on 11 October 2026: demo end of week 1, pilot go-live end of week 4.

## Week 0: before kick-off (on approval)

- [ ] AWS account / org unit for ClaimScope, `eu-west-2`, budgets and alerts.
- [ ] Anthropic API workspace + key in AWS Secrets Manager; confirm data handling terms (see `DECISIONS.md` D3).
- [ ] Matterport developer (SDK) key.
- [ ] Request from client: 3 anonymised past reports with photos/scans, report format, baseline time per claim.

## Week 1: working demo (no auth, anonymised data)

| Day | Work |
|---|---|
| 1 | Infra skeleton (CDK): S3 evidence bucket, CloudFront + web bucket, HTTP API, DynamoDB table, Step Functions stub. **Spike:** Matterport SDK capture + terms check. |
| 2 | Catalogue v0 (~80 items covering escape-of-water/flood reinstatement + prelims) with material rates and sources. Pricing engine + derived quantities with unit tests. |
| 3 | Stage A (per-image detection) and Stage B (room itemisation) with structured outputs; prompt caching. |
| 4 | Web: assessment → rooms/dims → photo upload + scan link capture → processing → schedule table + CSV. |
| 5 | QA rules R01–R05, R08–R10. End-to-end run on an anonymised sample; fix; demo. |

**Demo exit criteria:** photos or scan link in → priced, room-grouped schedule on screen with evidence per line and CSV out, in under ~5 minutes.

## Week 2: tuning, voice, labour

- [ ] Evaluation harness over the client's past claims (recall, precision, quantity error, £ delta).
- [ ] Prompt and catalogue iteration against the eval set.
- [ ] Voice notes: record/upload → Transcribe (`en-GB`) → per-room context.
- [ ] Labour: `labour_minutes` per catalogue item, trade rates table, materials vs labour shown per line.

## Week 3: review and reports

- [ ] Review screen: accept/edit/delete/add, evidence viewer, QA panel, room and schedule totals (NET/VAT/GROSS).
- [ ] Catalogue + trade-rates admin screens; catalogue versioning.
- [ ] PDF export in the client's report format (cover, incident report, general items, room groups with dimension tables, group totals, special notes).
- [ ] Remaining QA rules needing geometry (R06, R07, R11, R15).

## Week 4: go-live

- [ ] Admin password protection; UK hosting hardening; retention and deletion job.
- [ ] DPA signed **before** any live claim data.
- [ ] User guide; walkthrough session; cost-per-claim report.
- [ ] Go-live → second invoice.

## Out of pilot (quoted separately)

Multi-user accounts and roles, integrations, automatic room measurements from scan data (needs owner API access), subscription billing, ongoing support.
