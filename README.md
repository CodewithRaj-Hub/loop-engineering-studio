# Loop Engineering Studio

A terminal app (Python + [Textual](https://github.com/textualize/textual)) that makes **four engineering loops** visible around a Requirements Review Agent, with **two human gates**.

| Loop | What you see |
|---|---|
| **Agent loop** | The review agent works stage by stage; every model turn and tool call streams into a collapsible trace tree |
| **Verification loop** | A grader reads the *file* the agent wrote (not the chat) against a rubric, fails it with quoted gaps, and sends the agent back |
| **Event-driven loop** | Change `requirements.json` anywhere (TUI button, your editor, another terminal) and a review starts by itself: `EVENT ▸ QUEUE ▸ AGENT ▸ GRADER ▸ STORE ▸ REPORT` |
| **Hill-climb loop** | Batch results → analyst proposes a change to `AGENTS.md` with quoted evidence → **you approve** → batch re-run → pass-rate chart v1 → v2 |

Human gates: **approve a review** (the agent never closes its own work) and **approve a harness change** (the analyst proposes, plain code applies, a human decides).

## Try it without installing anything

* **Interactive explainer:** `docs/index.html` is a single page that explains what an agent is and steps through five agent loops with animations. It is published with GitHub Pages: `https://<your-user>.github.io/loop-engineering-studio/`
* **Run the real app in your browser:** open the repo in **GitHub Codespaces** (the `.devcontainer` installs everything), then run `reqreview tui --demo` in the terminal.

## Quickstart (no API key needed)

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
reqreview tui --demo
```

Demo mode replays **scripted** runs (see *Honest notes*). Try this:

1. Select `AUTH-142`, press **r**. Watch the trace tree: the grader fails clarity and depth, the agent revises, the grader passes.
2. Press **s** (or run `reqreview sim add NEW-1 --demo --text "REQUIREMENT: Demo\n\nIt should be fast."` in a second terminal). A review starts with no click; open the **Event Loop** tab (`2`).
3. Press **a**, add your own requirement; same thing.
4. Press **3**, **Run batch**, **Analyse**, read the evidence and diff, **Approve change**, select `v2`, **Run batch** again: the chart shows the improvement.

## Live mode (OpenRouter or OpenAI)

```bash
cp .env.example .env        # set OPENROUTER_API_KEY, or OPENAI_API_KEY (+ LLM_PROVIDER=openai if both are set)
reqreview doctor            # which provider, which models, a real tool-calling test per model, plain-English fixes
reqreview tui               # or: reqreview tui --provider openai
```

| | OpenRouter (default) | OpenAI |
|---|---|---|
| Key | `OPENROUTER_API_KEY` | `OPENAI_API_KEY` (`OPENAI_BASE_URL` for Azure or compatible servers) |
| Models | free tool-capable models are **discovered at runtime**, with request-level fallbacks | `gpt-5.4-mini` for every role; override with `REVIEW_MODEL`, `GRADER_MODEL`, `ANALYST_MODEL` |
| Limits | free tier is about 20 requests/min, a client-side limiter keeps you under it | none by default (`OPENAI_RPS` to throttle) |

* **Every model is tested before use** with one real tool-calling request. A model that returns 403 or 404 is skipped, and a job that hits a refusal mid-run retries once on another model.
* A hill-climb batch makes many calls, so the app shows an estimate and asks before running.
* Use synthetic data only: free endpoints may log prompts.

## When something goes wrong

Errors are explained where you are looking, in plain English: **what happened, the likely cause, and how to fix it**.

* **Trace tree:** a failed run ends in a red node with the explanation. Steps carry elapsed time (`+12.4s`).
* **Diagnostics tab (`4`):** every failure, warning and throttle in one table. Select a row for cause, fix and the raw error to copy. **Re-check provider** probes the models again.
* **Report tab:** a failed review shows the same explanation instead of an empty report.
* **Files:** `data/logs/studio.log` (readable, one line per event, step and grading pass) and `data/logs/errors.log` (full tracebacks). `reqreview logs` prints the tail, `reqreview logs --errors` only problems.

## How the event-driven loop works

`requirements.json` is the **system of record**. The TUI buttons, `reqreview sim`, and any editor all just change that file. A watcher diffs it by content hash and appends `requirement.created/updated/removed` to `data/events.jsonl`. A dispatcher then applies the rules: debounce 1.5 s, review only if the hash changed, one review at a time, an edit to an **Approved** requirement marks it *Stale* instead of silently re-opening it. Select any event in the log to highlight its whole chain (shared correlation id).

## CLI

```
reqreview tui [--demo] [--requirements PATH] [--data-dir PATH] [--speed 1.0] [--autoplay]
reqreview doctor [--provider openrouter|openai]
reqreview logs [--errors] [-n 60]
reqreview sim add|edit|rm ID [--text T | --file F] [--demo]
reqreview replay data/reviews/AUTH-142/run_1.json
```

Keys: `1 2 3 4` tabs · `u` upload · `r` review · `a` add · `e` edit · `p` approve · `s` simulate webhook · `i` analyse · `t` theme · `?` help · `q` quit.

## Project layout

```
reqreview/
  agent.py        review agent + grader loop (adapted from the original single file)
  llm.py          OpenRouter or OpenAI: model selection, probe, failover, rate limiter, quota
  diag.py         plain-English error explanations, studio.log / errors.log
  events.py       event log, file watcher, dispatcher
  store.py        requirements, runs, approvals, status machine, harness versions
  runner.py       LiveRunner / ReplayRunner behind one interface
  hillclimb.py    batch, analyst, apply_change
  demo_data.py    scripted runs for demo mode
  tui/            Textual app, widgets, modals, Improve tab
tests/            parsing, store, events/dispatcher, hill-climb, TUI smoke
docs/             index.html (interactive explainer, GitHub Pages), architecture, video storyboard, LinkedIn draft
MASTER_PLAN.md    build plan and status for any coding agent
```

