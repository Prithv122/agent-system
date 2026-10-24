"""Code copied from the author's own shipped repos, pinned to a commit.

| File | Source repo | Path | Commit |
|---|---|---|---|
| safety.py | query-service | src/queryservice/safety.py | 6e1900c |
| warehouse.py, results.py | sql-cookbook | src/sqlcookbook/ | ffd49cf |
| providers.py, cache.py, secret_store.py | llm-eval-harness | src/evalh/ | 07354f1 |

The provider stack comes from llm-eval-harness rather than production-rag because that copy
already drops the silent fallback chain: a model under test must never be swapped mid-run.
"""
