# Eval Results (quality-v2)

10 repos · pipeline v9 · models that answered: `gemini-3.5-flash-lite` (104 calls) · rejection rate 14% · 0 findings unlabeled or skipped (excluded from precision)

| Config | Precision | Recall | Findings | Avg time | Avg LLM calls |
|---|---|---|---|---|---|
| No Verifier | 71% | 61% | 35 | 107.7s | 8.5 |
| With Verifier | 73% | 50% | 30 | 118.9s | 10.4 |

Precision = real / labeled findings. Recall = ground-truth issues found / ground-truth issues of labeled repos (18 of 18 counted so far; repos not labeled yet are excluded). Rules: [LABELING_RULES.md](LABELING_RULES.md).

## Per repo

| Repo | Raw | Confirmed | Precision OFF | Precision ON | GT found OFF | GT found ON | Time | LLM calls |
|---|---|---|---|---|---|---|---|---|
| anxolerd/dvpwa | 3 | 1 | 100% | 100% | 2/4 | 1/4 | 58.3s | 8 |
| fportantier/vulpy | 11 | 11 | 64% | 64% | 2/4 | 2/4 | 161.0s | 18 |
| gothinkster/flask-realworld-example-app | 1 | 1 | 100% | 100% | no GT | no GT | 47.7s | 8 |
| jpadilla/pyjwt | 1 | 1 | 0% | 0% | no GT | no GT | 121.2s | 20 |
| miguelgrinberg/microblog | 1 | 1 | 0% | 0% | no GT | no GT | 563.6s | 11 |
| pallets/itsdangerous | 1 | 1 | 0% | 0% | no GT | no GT | 66.4s | 11 |
| pallets/markupsafe | 0 | 0 | n/a | n/a | no GT | no GT | 28.9s | 5 |
| sloria/environs | 1 | 0 | 0% | n/a | no GT | no GT | 51.1s | 8 |
| stamparm/dsvw | 7 | 6 | 86% | 83% | 4/5 | 3/5 | 61.0s | 10 |
| we45/vulnerable-flask-app | 9 | 8 | 89% | 100% | 3/5 | 3/5 | 29.5s | 5 |
