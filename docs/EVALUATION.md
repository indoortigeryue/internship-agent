# Evaluating the eligibility screener

How the screening engines were compared, what the agent design is, and what didn't work.

## Results

39 held-out postings, sampled after code and prompts were frozen, labeled blind by hand:

| Engine | Apply/skip accuracy | Eligible jobs wrongly rejected | Restricted jobs wrongly accepted | Cost / posting |
|---|---|---|---|---|
| Regex rules | 67% | 7 / 25 | 6 / 14 | $0 |
| Claude Opus 5.5, single call | 97% | 0 / 25 | 1 / 14 | $0.0205 |
| Claude Haiku 5.5, single call | 95% | 0 / 25 | 2 / 14 | $0.0005 |
| **Cascade (Haiku → Opus when uncertain)** | **97%** | **0 / 25** | **1 / 14** | **$0.0084** |

The cascade matches a single Opus call at 59% lower cost, escalating 35% of postings.

## Why rules weren't enough

The regex engine was wrong a third of the time, in ways more rules can't fix:

- **It matched text from other jobs.** LinkedIn pages carry a "similar jobs" sidebar, so postings were flagged citizens-only because *another* company's listing said "(U.S. Person Required)". 8 of the 88 dev postings were rejected this way.
- **It can't read hedging.** "This position *may* require export-controlled access" is boilerplate; "This position *requires*…" is a hard requirement. Each new phrasing needed another rule.
- **It misses plain statements** it has no pattern for.

## Agent design

- **Triage** (Haiku) returns structured output: reasoning, a sponsorship value, and a citizenship value, each value backed by a **verbatim quote** from the JD.
- **Router** (plain code, free) escalates when the quote doesn't literally appear in the JD (hallucinated evidence), when the call is nuanced (full-time-only scoping, hedged export language), when triage claims a hard citizenship or ITAR requirement, or when triage found nothing but the JD contains restriction language.
- **Specialist** (Opus) re-reads the JD from scratch without seeing triage's answer, so it can't anchor on it.

Both agents share one Pydantic schema and one rule set, generated from the user's profile (`_rules_for` in [`screener/agents.py`](../screener/agents.py)). Every call is traced (tokens, dollars, latency, escalation reason) and cached by its full request, which makes experiments reproducible and re-runs free.

## The verifier that didn't help

The first design was an **extractor + verifier** loop (evaluator-optimizer), including a **cross-vendor** verifier: Claude extracts, GPT-5.5 audits. On the dev set:

| v1 config (dev, 88 postings) | Apply/skip accuracy | False rejections | False accepts | Cost / posting |
|---|---|---|---|---|
| Opus, single call | 97% | 0 | 3 | $0.019 |
| GPT-5.5, single call | 95% | 0 | 4 | $0.014 |
| Opus + Opus verifier | 97% | 0 | 3 | $0.040 |
| Opus + GPT-5.5 verifier | 94% | 1 | 4 | $0.038 |

Verification doubled the cost and added nothing, and the cross-vendor verifier overturned one correct answer. The remaining errors were the same for every model from both vendors: *"must be a U.S. person… or eligible to obtain the required export authorization"* was read as an opening, when for the candidate it effectively isn't one. **The errors were correlated**: a verifier with the same rules shares the extractor's blind spot. So the missing domain rule was fixed for every config, and the second model was put to work on **cost** instead of a second opinion. That became the cascade.

## Structured output is generated in schema order

Testing the H-1B rules turned up a DoorDash posting: *"F-1 students are eligible if they are using CPT. DoorDash does not support students on J-1 visas."* Haiku labeled it **no_sponsorship** for an H-1B candidate, while its own `reasoning` field said the J-1 clause *"does not apply to an H-1B holder… should be classified as none_stated."*

`reasoning` was the **last** field in the schema, so the label was emitted before the argument was written. Moving `reasoning` first fixed the case. The full evaluation was then re-run on the new schema: Opus and the cascade were unchanged, and Haiku on its own improved from 92% to 95%. The pre-fix runs are in `screener/results/v2/`.

## Method

- **Data.** 128 real postings from the tracker, with JD text re-fetched through `verify.py`'s career-site fetchers. Pages carry the JD twice plus site chrome, and trimming that halved token cost. Scraped text stays local; [`screener/corpus.json`](../screener/corpus.json) lists the postings.
- **Dev set (88).** Stratified by the regex verdict. Where regex and every pipeline agreed, that label was accepted; all 30 disagreements were hand-labeled, and 10 agreements were spot-checked (10/10 correct). **The rules were revised after error analysis on this set**, so its scores (cascade 100%) are optimistic.
- **Test set (40 → 39).** Labeled **blind** (predictions hidden), every posting by hand except 12 containing none of the words a restriction can be written with (`sponsor`, `citizen`, `export`, `clearance`, `authoriz`, `visa`, `ITAR`, …). That rule held for 20/20 postings on dev. One UK posting was out of scope.
- **Small n.** 38/39 has a 95% confidence interval of about 87–99%. The ranking is clear; the exact percentages aren't.
- **Only the F-1 configuration is evaluated.** Other statuses use the same agents with status-specific rules, checked on individual cases but not on a labeled set.

## Remaining errors

- **Siemens Healthineers** (every system): *"must be able to work with controlled technology in accordance with US export control law"*, plus a company-wide compliance statement. Labeled restricted; the models read it as boilerplate. It's genuinely borderline, and it argues for an explicit "unsure → ask the human" verdict.
- **Router gap.** One restriction was written as a form field (*"Security Access Level: US Citizenship"*), which the router's pattern misses. Haiku caught it anyway.

## Reproducing

```bash
cd screener
../.venv/bin/python evaluate.py --split test          # re-score from committed results + labels
../.venv/bin/python run.py --config cascade --split test   # re-run (needs the JD text: sample.py)
```

| Path | Contents |
|---|---|
| `results/` | current runs (dev); `results/test/` held-out set |
| `results/v2/` | the same configs before the reasoning-first schema fix |
| `results/v1/` | extractor-verifier ablations |
| `labels.json`, `labels_test.json` | hand labels |
