# Resume Bullets — SQL Analyst Agent

Form: **action → technical specifics → measured outcome.** Draft; adjust the wording to your voice.

---

## Bullets

- Built a question-to-SQL agent (Python, DuckDB, local 7B model) with a validate-and-repair loop and
  pre-registered its evaluation on 36 golden questions: retries raised execution accuracy only from
  25.0% to 27.8% (95% CI [0.0, +8.3] pts) for 73% more tokens, because 15 of 27 errors were valid
  SQL with wrong rows that no database error can flag.
- Enforced read-only access in three independent layers (parser guard, read-only connection,
  locked no-file-access config) and proved it by effects: 35 attack queries, 0 writes and 0 file
  reads with both a scripted and a real model; every result replays offline from 118 committed replies.

## Which roles this supports

- [ ] Data Scientist / ML
- [x] AI Engineer (LLM/NLP/CV)
- [ ] Data Engineer
- [x] Data Analyst / Python Developer

## Keywords this project earns

Text-to-SQL, LLM agents, execution accuracy, pre-registration, paired bootstrap, McNemar test,
DuckDB, `EXPLAIN`, SQL injection defence, read-only enforcement, replay cache, Ollama, evaluation
harness, metamorphic testing.
