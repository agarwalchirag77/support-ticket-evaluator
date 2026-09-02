# QC Feedback Methodology

How the monthly agent feedback is scored and written. `fetch_qc_data.py` produces the
*evidence*; this file governs how to *interpret and write* it so every check-in is
consistent with the ones produced before.

## What QC measures (and what it does not)

QC scores the **written handling of a support ticket** — the public replies, internal
notes, resolution, and status as they appear in Zendesk. It is graded by an LLM against
19 metrics, each rated **1–4** or **N/A**.

**It does NOT measure:** verbal/live-call handling, Slack/huddle discussion, pairing, or
anything that happened outside the ticket text. Never frame feedback as a total judgment
of the person — it is a read on their written ticket craft for the month. State this
caveat when scores look low but you know the agent does strong live work.

## Groups

- **L1** — Chat L1 Support (group `44897999201817`): first-line chat/email.
- **L2** — General Escalation (group `6338786491161`): deep technical escalations.

Compare an agent to peers **within their own group**; L2 tickets are harder and score
differently from L1. Use `--group L1|L2` to scope.

## Weighting (12 metrics → 100)

The weighted score uses only these 12 metrics; the other 7 are recorded but weight 0.

| Metric | Name | Weight |
|--------|------|-------:|
| METRIC_1  | Clarifying Questions            | 7.5 |
| METRIC_4  | Root Cause Analysis             | 10  |
| METRIC_5  | Resolution Accuracy             | 10  |
| METRIC_6  | Detailed Resolution Steps       | 10  |
| METRIC_7  | All Concerns Addressed          | 10  |
| METRIC_9  | Proactive Updates               | 5   |
| METRIC_11 | Clear Communication             | 10  |
| METRIC_12 | Empathetic & Professional Tone  | 10  |
| METRIC_13 | Resolution Status Set Correctly | 5   |
| METRIC_15 | Workaround Provided             | 7.5 |
| METRIC_17 | KB / Docs Referenced            | 5   |
| METRIC_18 | Internal Notes Quality          | 10  |

Weight-0 (recorded, not scored): Roadmap to Resolution, Correct SLA Expectations, Timely
First Response, Resolution On Time, Custom Attributes Filled, Escalation Judgment, QC
Reopen Reason. (SLA/derived or outside written craft.)

## The N/A rule (critical for fairness)

**N/A is dropped from both numerator and denominator** — it never counts as a zero. A
metric is N/A when it genuinely doesn't apply (e.g. no workaround was needed; a ticket
referred to L2 has Root Cause / Resolution Accuracy / Detailed Steps marked N/A because
L2 owns the fix). So an agent is scored only on what their ticket actually called for.

- **Per-ticket weighted** = Σ(weight × rating) / Σ(weight), over non-N/A weighted metrics.
- **Agent monthly weighted** = the average of the per-ticket weighted scores (each ticket
  counts equally). Range 1.0–4.0.
- **Low** = a rating of **2 or below**.

Rough band reading: **≥3.7 strong · 3.4–3.7 solid · 3.0–3.4 mixed · <3.0 needs attention.**
Always read the band alongside ticket count — a 3.9 over 6 tickets is thinner evidence
than a 3.6 over 40.

## Writing the check-in

Produce **one write-up per agent**, two sections. Keep it specific, behavioral, and
system-ready (it goes into the performance system verbatim).

### Strengths
- Lead with the agent's **strongest weighted metrics** (`strongest`, and any metric
  averaging ≥3.8). Translate the metric into a **behavior**, not a score: "Consistently
  asks the right clarifying questions up front" — not "scored 4.0 on METRIC_1".
- Cite a `best_tickets` example when it makes the strength concrete.
- 3–5 bullets.

### Areas for Development
- Lead with the **weakest weighted metric** (`weakest`) and any metric averaging below
  ~3.4 or with a high `low` count. Again, name the behavior: "Root-cause explanations are
  sometimes skipped before closing" — not "METRIC_4 = 3.4".
- Ground each area in **1–2 real tickets** from `low_tickets`: reference the ticket by
  what happened (`summary`) and what specifically fell short (the low metric's
  `reasoning`), then give the forward-looking action (`improvement_note`), phrased as
  coaching. Cite the ticket id so it's auditable.
- Note relevant **flags** (`flags_pct`) — e.g. frequent SLA breaches or missing-RCA flags
  — as patterns, not one-offs.
- 3–5 bullets. Developmental tone: what to do next, not blame.

### Tone & fairness
- Balanced: real strengths even for a struggling month; a genuine growth area even for a
  strong one.
- Use the **trend** (prior months' weighted score) — call out improvement or slippage.
- If ticket count is low or confidence flags are frequent, say the read is tentative.
- Reuse the neutral, professional voice of prior check-ins. No raw metric ids or scores in
  the prose — translate everything into observable behavior.

## Reading a single ticket (Q&A)

When answering "why was ticket X rated low" / "what can be improved on ticket X" (`--ticket`):

- **Explain from the recorded evidence.** Each metric carries the evaluator's `reasoning` (why
  that score) and `improvement_note` (what to do). Use them — rephrased as clear coaching, not
  pasted — rather than inventing a rationale.
- **Weighted vs recorded.** Only the 12 weighted (`scored`) metrics move the ticket's score. A low
  *unweighted* metric is worth mentioning but didn't drag the number down — say so.
- **N/A ≠ failure.** An `N/A` metric genuinely didn't apply (e.g. no workaround was needed, or an
  L2-referred ticket where L1 doesn't own the fix). Never read N/A as a zero or a miss.
- **One ticket is one data point.** A single low ticket isn't a pattern — frame it as this ticket's
  handling, and only call something a trend if the agent-month bundle backs it up.

## Team-level reads (leaderboard, compare, changes, ranges)

When ranking or comparing across people (`--leaderboard`, `--compare`, `--changes`):

- **Always compare within a group.** L1 (chat) and L2 (escalation) tickets differ in difficulty and
  score differently — rank L1 against L1 and L2 against L2 (`--group`). A cross-group leaderboard is
  misleading. `--compare` compares an agent to *their own group's* average.
- **Respect ticket volume.** A 3.9 over 4 tickets is thinner evidence than a 3.6 over 40. The tools
  flag `insufficient_data` (< 3 tickets) and always return `n_tickets` — say so rather than ranking a
  1–2 ticket month as if it were settled. Sort confident entries above thin ones.
- **Deltas are directional, not verdicts.** `--changes` `weighted_delta` and `--compare` per-metric
  gaps point to *where to look*, not a final judgment — one weak metric or one down month is a
  coaching conversation, not a rating. Pair a drop with the specific low tickets behind it.
- **Ranges aggregate, then break down.** A quarter view gives a `window` score (all tickets in the
  range, so busier months weigh more) plus `by_month` — read both: the window for the overall level,
  `by_month` for the trajectory within it.
- Everything uses the **same weights and N/A rule** as the single-agent check-in, so a leaderboard
  score equals that agent's own bundle score — they never disagree.

## Name matching

Agent names are matched **exactly**. Some people appear under variant spellings (e.g.
"Jashmitha" vs "Jashmitha CG", two different "Muskan"s) and some as `Agent ID {n}` when
Zendesk lacked a name. Run `--list-agents --month YYYY-MM` first and confirm the exact
string before generating, so you don't split or merge people by accident.
