# Eval Results (default)

10 repos · pipeline v4 · models that answered: `gemini-3.5-flash-lite` (122 calls) · rejection rate 17% · 0 findings unlabeled or skipped (excluded from precision)

| Config | Precision | Recall | Findings | Avg time | Avg LLM calls |
|---|---|---|---|---|---|
| No Verifier | 36% | 50% | 72 | 59.0s | 8.2 |
| With Verifier | 38% | 44% | 60 | 87.4s | 12.9 |

Precision = real / labeled findings. Recall = ground-truth issues found / ground-truth issues of labeled repos (18 of 18 counted so far; repos not labeled yet are excluded). Rules: [LABELING_RULES.md](LABELING_RULES.md).

## Per repo

| Repo | Raw | Confirmed | Precision OFF | Precision ON | GT found OFF | GT found ON | Time | LLM calls |
|---|---|---|---|---|---|---|---|---|
| anxolerd/dvpwa | 5 | 3 | 80% | 67% | 1/4 | 1/4 | 60.3s | 10 |
| fportantier/vulpy | 23 | 23 | 43% | 43% | 2/4 | 2/4 | 259.8s | 28 |
| gothinkster/flask-realworld-example-app | 5 | 3 | 0% | 0% | no GT | no GT | 65.8s | 11 |
| jpadilla/pyjwt | 6 | 5 | 0% | 0% | no GT | no GT | 108.8s | 18 |
| miguelgrinberg/microblog | 12 | 8 | 0% | 0% | no GT | no GT | 125.7s | 21 |
| pallets/itsdangerous | 2 | 2 | 0% | 0% | no GT | no GT | 60.2s | 10 |
| pallets/markupsafe | 0 | 0 | n/a | n/a | no GT | no GT | 30.5s | 4 |
| sloria/environs | 4 | 2 | 25% | 50% | no GT | no GT | 54.1s | 9 |
| stamparm/dsvw | 9 | 8 | 67% | 62% | 4/5 | 3/5 | 78.7s | 13 |
| we45/vulnerable-flask-app | 6 | 6 | 83% | 83% | 2/5 | 2/5 | 29.8s | 5 |
