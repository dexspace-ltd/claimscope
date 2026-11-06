# ClaimScope

AI-assisted schedules of works for UK property insurance claims. Photos, 3D scan views and voice notes in; an itemised, priced, insurer-ready schedule out. The AI drafts; the assessor approves.

> Working codename. Built for a UK loss-assessing firm (name withheld); demo and 4-week pilot proposed October 2026.

## Docs

- [Architecture](docs/ARCHITECTURE.md): system design, AI pipeline, pricing engine, data model
- [Build plan](docs/BUILD_PLAN.md): week 1 demo → week 4 pilot go-live
- [QA rules](docs/QA_RULES.md): automated checks, derived from a real schedule review
- [Decisions](docs/DECISIONS.md): made and open

## Layout

```
apps/web/        Static HTML/CSS/JS front end (S3 + CloudFront); React later if needed
services/api/    Python 3.12 Lambdas: API, pipeline steps, pricing engine, QA rules
infra/           AWS CDK (Python), eu-west-2
schemas/         JSON Schemas: catalogue item, schedule
fixtures/        Anonymised test assessments only. Never commit live claim data.
tools/compare/   Local-only comparison of a draft against the original report
```

## Develop

```bash
# API and pricing engine tests
cd services/api && uv run --group dev pytest -q

# Infrastructure (eu-west-2)
cd infra && uv venv --python 3.12 .venv && uv pip install --python .venv -r requirements.txt
source .venv/bin/activate && npx aws-cdk@2 synth     # first deploy: npx aws-cdk@2 bootstrap, then deploy
```

Evidence: a public Matterport link, a phone walkthrough video with spoken commentary, or both.

Demo flow: paste a public Matterport link → share the tab → capture the floor plan view (rooms and sizes) → walk each room (views are kept automatically) → draft schedule → download the JSON.

Compare a draft with the original report **locally** (the report never leaves your machine; output goes to `local/`):

```bash
uv run tools/compare/compare.py --report path/to/original.pdf --draft claimscope-<id>.json
```

Preview the web app locally against the deployed API (works while the public site is switched off):

```bash
python3 tools/dev/serve.py
```

The pipeline reads the Anthropic key from Secrets Manager (`claimscope/anthropic-api-key`); create it before Stage A/B are wired in.

## Data rule

No live claim documents, photos, policy numbers or personal data in this repository. Real claims are processed only in the hosted platform, after the client's data processing agreement is signed.
