# Interview Prep — SQL Analyst Agent

**Five questions, five answers.** Draft answers grounded in the repo; rewrite each one in your own
words until you can say it without reading.

---

### Q1. Walk me through the architecture in 90 seconds.

A question goes to a writer model, which sees the schema (plus the allowed values of short text
columns like `status`) and answers in a small JSON protocol: SQL, ask for the schema, or refuse. The
SQL then goes through three deterministic checks. First a guard asks DuckDB's own parser whether it
is exactly one `SELECT`. Then `EXPLAIN` binds every table and column without running anything, so a
made-up column fails with the engine's "did you mean" hint. Then it runs on a connection that is
read-only, has file access switched off, and has its configuration locked, with a row cap and a
timeout. If `EXPLAIN` or the engine fails, the error text goes back to the writer for another
attempt, up to a budget. An explainer model states the answer, and a deterministic check traces
every number it uses back to the result rows. There is no framework: the loop is about 60 lines,
because the loop is what I was measuring.

### Q2. Why a plain loop instead of LangGraph or CrewAI?

The research question was the effect of one mechanism: feeding validator errors back for a bounded
number of retries. That is a state machine with four fixed steps. A framework would hide the exact
control flow I needed to keep identical across arms, and add a dependency tree. The arms differ
only in max attempts (1, 2, 3); the first prompt is byte-identical, so the replay cache serves arm
A's first reply to B and C and any difference is purely the repairs. I also refused to call it a
"multi-agent team": it is two model calls plus deterministic checks.

### Q3. What's the weakest part, and what breaks first at scale?

Measured weakness first: the loop only sees errors the database reports. 15 of the 27 wrong
one-shot answers were valid SQL with the wrong rows, like `status = 'Cancelled'` against data that
says `'cancelled'`, and no retry can see those. Second, the 7B writer used the `refuse` action to
describe an error instead of fixing it in 6 of 10 repair chances. At scale, the schema in the prompt
breaks first (7 tables fit, 700 don't), so I'd retrieve tables per question and measure retrieval
recall separately. Read-only would move from a connection setting to a database role with SELECT
grants and server-side timeouts.

### Q4. How do you know it works? What did you measure, against what baseline?

Execution accuracy on 36 questions over a seeded warehouse, 12 SQL patterns x 3, each reference
recomputed independently in plain Python. Baseline is arm A, one shot with no repair, same model,
same prompt. The plan was pre-registered and frozen before any model call: C beats A if the paired
95% bootstrap CI of the difference excludes 0. Result: 25.0% vs 27.8%, +2.8 points, CI [0.0, +8.3],
McNemar p = 1.0, so not supported. Safety is measured by effects, not by which layer stopped it:
35 attack queries, 0 writes, 0 file reads, database hash unchanged, with both a scripted writer and
the real model. Every number replays offline from 118 committed model replies; the three arms
replay byte-identically from a fresh clone.

### Q5. Your loop didn't help. Isn't that a failed project?

It is a measured null with a clear cause, which is more useful than an unmeasured "agent with
self-correction". The ceiling for any repair loop was the 11 errors the database reported; the loop
recovered one, and I can show why: 6 times the model used "refuse" to explain the error, and one
parse error was classified unsafe by the guard and ended the question. I found both after the
results, so I recorded them and did not re-score; fixing them is a second pre-registered run. The
bigger lesson is that the binding constraint for small models is silent semantic errors, which only
a check that knows the intended answer could catch. The agent deliberately isn't given one, because
that would stop measuring a realistic loop.

---

## 30-second pitch

LLM-written SQL is often wrong, and the popular fix is an agent that checks and retries its own
SQL. I built one with three read-only safety layers and measured whether the retry loop helps,
under a plan frozen before any model call. On 36 questions with a local 7B model it didn't: 25% to
27.8%, not significant, for 73% more tokens. The traces show why: most errors were valid SQL with
wrong rows, which no database error can flag. And the attack suite showed zero writes, by effects,
not by trust.
