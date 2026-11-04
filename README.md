# Bank analytics on a governed semantic layer

Does accuracy in AI analytics come from the model, or from governed business context?
This project tests that on public FDIC call-report data, then uses the result to build
a natural-language analytics app.

- **Live app:** https://fdic-semantic-layer.streamlit.app/
- **Article:** **Article:** coming soon

## The result

One AI model (Claude Haiku 5.5), the same data and the same 58 questions, in three setups.
Only the business context changes.

| Setup | What the model gets | Correct on in-scope questions | Same result in all 3 runs (lowest set) |
| --- | --- | --- | --- |
| A. No semantic model | Table and column names; writes its own SQL | 70% | 83% |
| B. Semantic model as documentation | The governed definitions in its prompt; writes its own SQL | 98% | 88% |
| C. Semantic model enforced | The same definitions, applied in code; no SQL access | 100% | 100% |

168 graded attempts per setup across four question sets. By set:

| Question set | A | B | C |
| --- | --- | --- | --- |
| Single-trap, development (30 questions) | 79% | 100% | 100% |
| Single-trap, held out (12) | 83% | 97% | 100% |
| Multi-rule, development (8) | 42% | 96% | 88% |
| Multi-rule, held out (8) | 50% | 92% | 88% |

Setup C's misses on the multi-rule sets are all one question it was deliberately not built for
(comparing individual banks across two quarters). It declined that question in 5 of 6 attempts.

What the transcripts show:

- **Without a semantic model, the SQL was fine and the definitions were wrong.** The model summed
  year-to-date income as if it were quarterly, annualized Q2 income by 4 instead of 2, averaged
  bank ratios instead of computing the industry ratio, and answered questions the data cannot answer.
- **With the definitions as documentation, the remaining errors were slips in applying them:**
  filtering a holding company by name instead of its ID, deriving quarterly income by hand,
  dropping a digit in a growth rate.
- **With the definitions enforced, those slips could not happen.** Code derived the figures.

## What's in this repo

**1. The experiment.** A frozen FDIC snapshot, a semantic model in YAML, three agent setups,
four question sets with hand-checked answers, a code-based grader and every run transcript.

**2. The app** (`streamlit run app.py`), with two pages:

- **Ask the data:** ask questions in plain English and get a headline answer, insights and
  charts. The AI model plans governed metric queries; code computes every number and draws
  every chart. Each answer shows its queries, data table and compiled SQL.
- **The experiment:** the recorded answers behind the results above, side by side by setup.

## How the semantic layer works

`semantic_model.yaml` is the single source of business definitions: units, year-to-date rules,
population rules, metric formulas, dimensions, the bank / holding company / region ontology
and an out-of-scope list.

- `semantic_layer.py` compiles a request such as
  `query_metrics(metrics=["net_income"], period="2025Q3", filters={"region": "Chicago"})`
  into SQL from those definitions, runs it and returns results with units and notes.
  Anything the model does not define is refused with a reason.
- `prompts.py` renders the same YAML into the glossary that setups B and C read.

## Repository layout

| Path | Contents |
| --- | --- |
| `01_fetch.py` | Pulls quarterly financials and bank attributes from the FDIC BankFind API |
| `data/snapshot_2026Q2/` | The frozen data used for every result (2024Q3–2026Q2) |
| `semantic_model.yaml` | Governed definitions |
| `semantic_layer.py`, `test_semantic_layer.py` | The compiler and its check against the answer key |
| `prompts.py`, `prompts/` | Prompt rendering, and the exact prompts each setup received |
| `agent.py`, `grader.py`, `run_experiment.py`, `regrade.py`, `show_run.py` | Experiment harness |
| `questions_*.yaml`, `answer_key_*.json`, `build_answer_key.py` | Questions, SQL and expected answers |
| `runs/` | Every attempt: configuration, transcript, answer and grade |
| `app.py`, `ask.py`, `analyst.py`, `charts.py`, `experiment.py` | The Streamlit app |
| `DECISIONS.md` | Every design decision and change, dated |

## Run it yourself

Requires Python 3.12 and an Anthropic API key.

```bash
git clone https://github.com/changezakram/fdic-semantic-agent.git
cd fdic-semantic-agent
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python test_semantic_layer.py          # the semantic layer reproduces the answer key
python build_answer_key.py dev         # rebuild an answer key from the raw data
```

To run the experiment or the app, set your key first:

```bash
export ANTHROPIC_API_KEY=...           # never commit it
python run_experiment.py --set dev --reps 3 --label my-run
python show_run.py                     # read the transcripts of the latest run
```

```bash
mkdir -p .streamlit
echo 'ANTHROPIC_API_KEY = "..."' > .streamlit/secrets.toml   # git-ignored
streamlit run app.py
```

## Method and limits

- **Held-out questions** were written before any tuning and locked with a file fingerprint
  (recorded in `DECISIONS.md`); the runner refuses to use them unless the fingerprint matches.
- **No AI grader.** Answers are graded by code against fixed tolerances. Three grading rules were
  corrected during review, applied to every setup and every run, and documented.
- **Frozen configuration.** The results come from the commit tagged `final-v1`
  (semantic model v0.1.1). Later versions of the semantic model serve the app.
- **Limits.** One model; small error counts (4 vs. 0), so the documentation-vs-enforcement gap is
  directional; a best-case glossary that fits in one prompt; 58 questions on one public dataset;
  ratios use quarter-end balances, so they differ slightly from FDIC's published figures.

## Data

Public FDIC call-report data from the [FDIC BankFind Suite API](https://api.fdic.gov/banks),
fetched on 2026-11-02. This project is not affiliated with the FDIC. Figures follow the
definitions in `semantic_model.yaml` and are for research and illustration, not investment advice.

## Author

Changez Akram · [changezakram.github.io](https://changezakram.github.io)
