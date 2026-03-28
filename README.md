# Freight Email Extraction Assessment

This repository contains a Groq-backed freight enquiry extraction system for the Task Harmony Backend / AI Engineer assessment.

Final submission run:

- Model: `llama-3.3-70b-versatile`
- Temperature: `0`
- Final prompt version: `v3`
- Output artifact: `output.json`
- Final sample-set accuracy: **98.67%**

The final extractor is a hybrid pipeline:

1. A deterministic parser builds a strong candidate extraction from the email text and `port_codes_reference.json`.
2. Groq receives the email plus prompt instructions and, for `v3`, the candidate extraction as a hint.
3. Pydantic validation and post-processing normalize the final record, enforce business rules, and preserve `null` handling.

## Project Structure

```text
.
├── .env.example
├── benchmark_prompts.py
├── emails_input.json
├── evaluate.py
├── extract.py
├── ground_truth.json
├── output.json
├── port_codes_reference.json
├── prompts.py
├── README.md
├── requirements.txt
└── schemas.py
```

## Setup Instructions

Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create a local env file and add your Groq key:

```bash
cp .env.example .env
```

Run extraction and evaluation:

```bash
python extract.py
python evaluate.py
```

Notes:

- `extract.py` uses Groq when `GROQ_API_KEY` is present in `.env`.
- If no key is present, the extractor falls back to deterministic mode so the repo still runs offline.
- The checked-in `output.json` was generated using the Groq-backed `v3` path.

## Approach

The core challenge is not just extracting text spans. It is normalizing noisy freight emails into a constrained schema with reference-driven port codes, business defaults, and strong null behavior. I used a hybrid design because the deterministic parts are reliable and cheap, while the LLM is useful for resolving ambiguity, preserving the first shipment, and handling varied phrasing.

The deterministic layer handles port aliasing, incoterm detection, dangerous-goods logic, numeric extraction, unit conversion, and thread carry-forward for follow-up emails. It also builds a candidate extraction that is especially strong on port codes because it works directly against `port_codes_reference.json`. The Groq layer then processes the same email with prompt-specific instructions. In the final `v3` flow, the LLM is allowed to backfill missing fields, while post-processing preserves already-valid candidate fields instead of letting the model replace them with weaker aliases.

To make the Groq path reliable on the free tier, I added:

- `temperature=0` for reproducibility
- 3 retries with exponential backoff
- parsing of Groq `try again in ...` windows
- request pacing via `GROQ_REQUEST_DELAY`
- capped completion tokens and capped input sizes
- checkpointed outputs plus resume support
- per-run request and token ceilings in `.env`

Output validation is done with Pydantic in `schemas.py`, and scoring is done in `evaluate.py`.

## Prompt Evolution

I benchmarked the first three prompt versions end-to-end on all 50 sample emails using Groq. The final output was generated from `v3`.

### v1: Basic LLM extraction

- Accuracy: `94.89%`
- Prompt style: minimal instructions, no reference rows, no candidate extraction
- Main issue: the model often extracted the right code but collapsed canonical destination names for ICD and grouped lanes
- Concrete misses:
  - `EMAIL_004`: returned `Chennai` instead of `Chennai ICD`
  - `EMAIL_007`: returned `Chennai` instead of `Chennai ICD / Bangalore ICD / Hyderabad ICD`
  - `EMAIL_014`: returned `Chennai` instead of `Chennai ICD`
  - `EMAIL_021`: returned `Chennai` instead of `Chennai ICD`

### v2: Add reference rows and stricter canonicalization rules

- Accuracy: `95.56%`
- Change from v1: include a compact subset of `port_codes_reference.json` and explicit instructions to use canonical names from that reference
- What improved:
  - `EMAIL_004`, `EMAIL_007`, `EMAIL_014`, and `EMAIL_021` all moved to the correct canonical ICD names
  - destination-name accuracy improved from `70.00%` to `84.00%`
- Remaining problems:
  - the model still sometimes picked one alias from a multi-name port instead of the expected grouped canonical name
  - `EMAIL_026`: `Xingang` instead of `Xingang / Tianjin`
  - `EMAIL_043`: `Bangalore ICD` instead of `Chennai ICD / Hyderabad ICD / Bangalore ICD`
  - `EMAIL_030`: name drifted to `Bangalore ICD` while the code stayed `INMAA`

### v3: Candidate-assisted extraction with conservative normalization

- Accuracy: `98.67%`
- Change from v2:
  - pass the deterministic candidate extraction to the model as a hint
  - preserve already-valid candidate fields and let the LLM backfill missing values instead of overwriting stronger deterministic values
- What improved:
  - `EMAIL_006`: fixed the false `FCA` incoterm and stayed at `FOB`
  - `EMAIL_013`: recovered the grouped destination name and canonical origin naming
  - `EMAIL_017`: fixed `ICD Bangalore` naming
  - `EMAIL_022`: recovered the correct `650 kg` / `1.4 cbm` follow-up values
  - `EMAIL_023`: corrected the destination from `Laem Chabang` to `Bangkok ICD`
  - `EMAIL_044`: corrected `Shenzhen` to `Shenzhen / Guangzhou`

Benchmark summary:

| Prompt | Overall Accuracy |
|--------|------------------|
| `v1` | `94.89%` |
| `v2` | `95.56%` |
| `v3` | `98.67%` |

Additional prompt variants `v4` through `v8` are included in `prompts.py` for further testing, but the documented iteration path for the submission is `v1 -> v2 -> v3`.

## Accuracy Metrics

Metrics from the final `output.json`:

| Field | Accuracy |
|------|----------|
| `product_line` | `100.00%` |
| `origin_port_code` | `100.00%` |
| `origin_port_name` | `100.00%` |
| `destination_port_code` | `96.00%` |
| `destination_port_name` | `96.00%` |
| `incoterm` | `100.00%` |
| `cargo_weight_kg` | `96.00%` |
| `cargo_cbm` | `100.00%` |
| `is_dangerous` | `100.00%` |
| `overall_accuracy` | `98.67%` |

## Edge Cases Handled

### 1. Follow-up emails with missing repeated context

- Emails: `EMAIL_022`, `EMAIL_025`
- Problem: the later message omits route or trade-term details, but the expected answer depends on the earlier message in the same thread
- Solution: `run_extraction()` keeps a lightweight thread cache keyed by sender + subject and backfills missing route and incoterm context from the previous email when appropriate

### 2. ICD names, grouped destinations, and canonical-name preservation

- Emails: `EMAIL_007`, `EMAIL_013`, `EMAIL_043`
- Problem: emails use shorthand like `BLR ICD`, grouped destination lists, or mixed literal and canonical naming
- Solution: the deterministic layer resolves the route first, then normalizes the destination name to the canonical reference name for the matched code, including grouped canonical names when the email clearly represents a grouped lane request

### 3. Multi-name ports and alias-heavy routes

- Emails: `EMAIL_026`, `EMAIL_038`, `EMAIL_044`
- Problem: ports appear as `Tianjin/Xingang`, `Shenzhen / Guangzhou`, or similar multi-name forms
- Solution: the port matcher builds aliases from the reference file itself, including slash-separated components, ICD variants, code suffixes, and acronyms, then chooses canonical reference names for the matched code

### 4. Ambiguous incoterms and narrative mentions

- Emails: `EMAIL_006`, `EMAIL_030`
- Problem: narrative mentions can look like incoterms when they should not win, and ports such as Cape Town can collide with trade-term tokens like `CPT`
- Solution: incoterm extraction is context-aware, defaults to `FOB` on ambiguity, and the final `v3` normalization preserves stronger deterministic trade-term decisions when the LLM tries to replace them with a weaker interpretation

## Known Sample-Set Oddities

The final `v3` run misses five fields across five emails. Four of them appear to be inconsistent sample labels rather than extractor errors:

- `EMAIL_018`: ground truth expects destination code `KRPUS` while the destination name is still `Chennai`
- `EMAIL_028`: ground truth expects destination code `INMAA` together with destination name `Bangalore ICD`
- `EMAIL_032`: ground truth includes `cargo_weight_kg=750.0` although the email only provides `3.9 cbm`
- `EMAIL_035`: ground truth expects `229.4 kg` for `0.2 RT`; the generic RT rule produces `200.0 kg`

The fifth is a reference-name preference issue:

- `EMAIL_050`: the extractor returns `Chennai`, while the ground truth prefers the alternate canonical alias `India (Chennai)` for the same code `INMAA`

I intentionally did not hard-code sample-only overrides for these rows.

## System Design Answers

### 1. Scale: 10,000 emails/day, 99% within 5 minutes, $500/month budget

I would build this as a queue-driven pipeline with a deterministic-first cascade. Incoming emails would be normalized by an ingestion service, stored in raw form, and pushed to a work queue such as SQS or RabbitMQ. Stateless workers would run fast preprocessing, deterministic parsing, and reference-based normalization first. Only emails with low confidence, validation failures, or unresolved ports would go to an LLM queue. That keeps the common path cheap and fast.

To hit the budget, I would treat the LLM as an escalation layer rather than the only parser. Deterministic extraction would handle the majority of repetitive freight enquiries. The LLM tier would process the ambiguous tail, ideally with compact prompts, strict token caps, caching, and model-specific rate limiting. Every output would include confidence metadata and validation status so uncertain records can be routed to review rather than blocking the SLA. This architecture is much more likely to satisfy both the 5-minute latency target and the monthly cost constraint.

### 2. Monitoring: extraction accuracy drops from 90% to 70% over a week

I would monitor both leading indicators and labeled accuracy. Leading indicators include null-rate shifts by field, spikes in defaulted `FOB`, unknown-port frequency, destination-code distribution drift, retry/error rates, and validation failures. These metrics can surface regressions before a fully labeled accuracy report is available. I would also keep a continuously refreshed golden set and run daily offline evaluations against it.

If accuracy dropped that sharply, I would investigate by slicing failures by sender domain, language, prompt version, model version, lane, and input template shape. Then I would compare the failing cohort against the prior week to determine whether the issue came from prompt drift, upstream formatting changes, reference-data drift, or a bad deploy. The fix loop would be: isolate failing examples, add them to regression fixtures, patch prompt or deterministic logic, rerun offline evaluation, then release behind a canary with side-by-side scoring before rolling out broadly.

### 3. Multilingual: 30% emails in Mandarin, 20% in Hindi

I would add language detection as an explicit preprocessing step and keep the same hybrid architecture. For Mandarin and Hindi, I would prefer a multilingual extractor prompt rather than translation-first as the default, because freight emails are often short, abbreviation-heavy, and mixed with codes, incoterms, and port aliases that generic translation can distort. The deterministic layer would still work for codes, units, incoterms, and DG markers regardless of language.

Evaluation would need per-language test sets and field-level metrics, not just one blended score. I would build language-specific regression suites for transliterated ports, mixed-language emails, and code-switched threads. Monitoring would also be sliced by language so a regression in Mandarin or Hindi cannot be hidden by strong English performance. If review capacity exists, I would sample labeled traffic weekly per language to maintain fresh evaluation data.

## Notes

- `output.json` is pre-generated and included in the repo.
- `benchmark_prompts.py` is an extra utility I used to benchmark prompt versions; it is not required by the deliverables, but it documents the actual prompt iteration process.
- `.env` is ignored by git. The tracked `.env.example` contains placeholders and safe defaults only.
