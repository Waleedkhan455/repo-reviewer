# Eval Results (default)

10 repos · model `gemini-3.8-flash` · pipeline v4 · rejection rate 17% · 72 findings unlabeled or skipped (excluded from precision)

| Config | Precision | Recall | Findings | Avg time | Avg LLM calls |
|---|---|---|---|---|---|
| No Verifier | n/a | 0% | 72 | 59.0s | 8.2 |
| With Verifier | n/a | 0% | 60 | 87.4s | 12.9 |

Precision = real / labeled findings. Recall = ground-truth issues found / ground-truth issues (18 total). Rules: [LABELING_RULES.md](LABELING_RULES.md).

## Per repo

| Repo | Raw | Confirmed | Precision OFF | Precision ON | GT found OFF | GT found ON | Time | LLM calls |
|---|---|---|---|---|---|---|---|---|
| anxolerd/dvpwa | 5 | 3 | n/a | n/a | 0/4 | 0/4 | 60.3s | 10 |
| fportantier/vulpy | 23 | 23 | n/a | n/a | 0/4 | 0/4 | 259.8s | 28 |
| gothinkster/flask-realworld-example-app | 5 | 3 | n/a | n/a | 0 (no GT) | 0 (no GT) | 65.8s | 11 |
| jpadilla/pyjwt | 6 | 5 | n/a | n/a | 0 (no GT) | 0 (no GT) | 108.8s | 18 |
| miguelgrinberg/microblog | 12 | 8 | n/a | n/a | 0 (no GT) | 0 (no GT) | 125.7s | 21 |
| pallets/itsdangerous | 2 | 2 | n/a | n/a | 0 (no GT) | 0 (no GT) | 60.2s | 10 |
| pallets/markupsafe | 0 | 0 | n/a | n/a | 0 (no GT) | 0 (no GT) | 30.5s | 4 |
| sloria/environs | 4 | 2 | n/a | n/a | 0 (no GT) | 0 (no GT) | 54.1s | 9 |
| stamparm/dsvw | 9 | 8 | n/a | n/a | 0/5 | 0/5 | 78.7s | 13 |
| we45/vulnerable-flask-app | 6 | 6 | n/a | n/a | 0/5 | 0/5 | 29.8s | 5 |
