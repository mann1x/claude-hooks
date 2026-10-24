# Decision models on real councils — plan H6, 2026-10-15

H4 benched the decision models on 25 hand-written cases, where Jev scored
50/50. H6 asks the same questions of cases mined from **144 real councils**
(`transcript.db` under `/srv/dev-disk-by-label-opt/dev`, 20 projects, May–Oct
2026), with the state each live seam sends (`consultants/engine/decision.py`,
cut by `decision_client.bound_text`). Every call goes through
`decision_client.evaluate`, the client the seams use (`--client production`).
One repeat; floor 0.6 for "confident & wrong"; Jev `jev-latest` on TypeSafe,
the others on solidpc xOllama 0.40.3-xollama.2.

## Cases and where the labels come from

| seam | cases | label |
|---|---|---|
| struggle | 78: 1 stuck, 37 recovered, 40 progressing | the lane's own next 6 calls: stuck = ≥ 4 of the 6 shown calls and ≥ 5 of the next brought nothing new (repeat call, repeat output or error); recovered = ≥ 2 shown stale but the next ≥ 5 new; progressing = the rest with the next ≥ 5 new. Windows in between are left out. |
| citations | 101: 82 supported, 19 not | a reading of claim + cited code at the commit the council saw (`git rev-list --before`); claims that stop at the cite or cannot be judged from the window were left out (19 of 120 read) |
| review | 20: 7 right, 13 wrong | the reviewer's own next message on that answer ("Your A was right — measured", "FALSIFIED BOTH of your hypotheses", "Council produced a NON-ANSWER"); follow-ups that only asked something new were left out (18 of 38) |

The citation and review readings are one reader's (Claude, this session),
each with a recorded reason in the local `labels.json`. Cases stay local:
councils from other repos carry their code. Rebuild with
`mine_decision_cases.py` then `--assemble`.

## What it shows

1. **The hand-written set was a ceiling.** On real cases Jev answers
   177/199 (H4: 50/50), clef-flash 168, nimble 166, tev1 142 (H4: 44/50).
2. **citations works on Jev.** At the live floor (0.6) Jev marks 11 of the
   19 unsupported cites with 1 false note on 82 supported ones (AUC 0.97).
   The local models are as safe (0–1 false notes) but catch 4–5 of 19.
3. **struggle is harmless and unproven.** Real lanes almost never get stuck:
   1 window in 547 met the rule; 37 looked stuck and recovered on their own.
   At the live high-stakes floor (0.85) no target nudged a lane that did not
   need it except clef-flash once (1 of 77); none caught the one stuck lane.
4. **review / confidence ranks on Jev but sits low.** Jev orders answers
   well (AUC 0.85, all three answers whose fault shows in the text at
   p ≤ 0.09), but rates the answers the reviewer accepted at a mean p of
   0.46 (wrong ones 0.20). The `confidence` seam puts that value on the
   channel the escalator, the low-confidence interrupt and the adversary
   checkpoint read, so as-is it would read sound answers as shaky. The local
   models barely rank (AUC 0.56–0.74) and rate wrong answers sound (mean p
   0.69–0.75); clef-flash rated the non-answer 0.76.
5. **Most wrong answers were wrong by evidence the council never had** (10
   of 13: later measurements). No decision model can see that from the
   answer; the review question's accuracy is bounded on any server.
6. **tev1's loaded window is ~2050 tokens.** A raw POST was refused on 12
   long states; `evaluate`'s cut-and-retry answers all of them (raw run
   137/199 → 142/199).

## Floors per server

- **Jev:** citations at 0.6 as shipped (0.8 trades 3 hits for the one false
  note); struggle at 0.85 as shipped. The review `confidence` value needs a
  per-server mapping, not a floor: its scale differs from the critic's
  self-rating, and 20 cases (7 positive) are too few to fit one.
- **Local (clef-flash best, then nimble):** fine for citations at 0.6 where
  code must not leave the host, at a third of Jev's recall; not for review
  or confidence.
- No default changes: `[decision]` stays off.

## Caveats

One reader for the readings; one repeat; 1 stuck struggle case and 20 review
cases are thin; some citation cases repeat one claim across sessions (the
same researcher report re-read by a follow-up).

## By target

| target | accuracy | mean Brier | confident & wrong | median ms | p90 ms | errors |
|---|---|---|---|---|---|---|
| `typesafe:jev-latest` | 177/199 | 0.090 | 3 | 261 | 285 | 0 |
| `xollama:tev1:latest` | 142/199 | 0.200 | 7 | 220 | 418 | 0 |
| `xollama:nimble:latest` | 166/199 | 0.133 | 18 | 354 | 746 | 0 |
| `xollama:clef-flash:latest` | 168/199 | 0.114 | 11 | 484 | 1106 | 0 |

## Accuracy by seam

| seam | `typesafe:jev-latest` | `xollama:tev1:latest` | `xollama:nimble:latest` | `xollama:clef-flash:latest` |
|---|---|---|---|---|
| struggle | 71/78 | 49/78 | 69/78 | 70/78 |
| citations | 90/101 | 85/101 | 88/101 | 90/101 |
| review | 16/20 | 8/20 | 9/20 | 8/20 |

## Accuracy by label class

| seam | class | `typesafe:jev-latest` | `xollama:tev1:latest` | `xollama:nimble:latest` | `xollama:clef-flash:latest` |
|---|---|---|---|---|---|
| struggle | recovered | 31/37 | 14/37 | 30/37 | 32/37 |
| struggle | stuck | 0/1 | 1/1 | 0/1 | 0/1 |
| struggle | progressing | 40/40 | 34/40 | 39/40 | 38/40 |
| citations | wrong-line | 59/68 | 54/68 | 57/68 | 59/68 |
| citations | names-it | 31/33 | 31/33 | 31/33 | 31/33 |
| review | True | 3/7 | 7/7 | 7/7 | 6/7 |
| review | False | 13/13 | 1/13 | 2/13 | 2/13 |

## Floor sweep (noul answers)

A floor lets an answer through when its confidence |2p−1| is at least the floor; below it the seam reads "unsure" and keeps today's behaviour. Cells: right / let through (of all).

| floor | `typesafe:jev-latest` | `xollama:tev1:latest` | `xollama:nimble:latest` | `xollama:clef-flash:latest` |
|---|---|---|---|---|
| 0.0 | 177 / 199 (199) | 142 / 199 (199) | 166 / 199 (199) | 168 / 199 (199) |
| 0.2 | 172 / 185 (199) | 100 / 134 (199) | 155 / 183 (199) | 162 / 188 (199) |
| 0.4 | 152 / 160 (199) | 70 / 87 (199) | 141 / 161 (199) | 155 / 179 (199) |
| 0.6 | 106 / 109 (199) | 37 / 44 (199) | 121 / 139 (199) | 145 / 156 (199) |
| 0.7 | 79 / 81 (199) | 25 / 27 (199) | 107 / 122 (199) | 127 / 136 (199) |
| 0.8 | 41 / 42 (199) | 14 / 15 (199) | 86 / 93 (199) | 110 / 116 (199) |
| 0.9 | 13 / 13 (199) | 2 / 2 (199) | 64 / 65 (199) | 58 / 58 (199) |

## struggle: nudge (stuck)

Acts when p points yes with confidence ≥ the floor; the live floor is 0.85. Cells: hits / 1 that should act · false alarms / 77 that should not.

| floor | `typesafe:jev-latest` | `xollama:tev1:latest` | `xollama:nimble:latest` | `xollama:clef-flash:latest` |
|---|---|---|---|---|
| 0.4 | 0 · 1 | 0 · 7 | 0 · 1 | 0 · 5 |
| 0.6 | 0 · 1 | 0 · 3 | 0 · 1 | 0 · 2 |
| 0.7 | 0 · 1 | 0 · 0 | 0 · 1 | 0 · 1 |
| 0.8 | 0 · 1 | 0 · 0 | 0 · 0 | 0 · 1 |
| 0.85 (live) | 0 · 0 | 0 · 0 | 0 · 0 | 0 · 1 |
| 0.9 | 0 · 0 | 0 · 0 | 0 · 0 | 0 · 0 |

## citations: note (unsupported)

Acts when p points no with confidence ≥ the floor; the live floor is 0.6. Cells: hits / 19 that should act · false alarms / 82 that should not.

| floor | `typesafe:jev-latest` | `xollama:tev1:latest` | `xollama:nimble:latest` | `xollama:clef-flash:latest` |
|---|---|---|---|---|
| 0.4 | 14 · 5 | 9 · 3 | 6 · 1 | 6 · 1 |
| 0.6 (live) | 11 · 1 | 4 · 0 | 5 · 0 | 4 · 1 |
| 0.7 | 10 · 1 | 3 · 0 | 5 · 0 | 4 · 1 |
| 0.8 | 8 · 0 | 2 · 0 | 4 · 0 | 4 · 0 |
| 0.9 | 3 · 0 | 2 · 0 | 3 · 0 | 0 · 0 |

## Misses

- `typesafe:jev-latest` struggle-0805 (truth False): p=0.54, confidence=0.08
- `typesafe:jev-latest` struggle-0925 (truth False): p=0.52, confidence=0.04
- `typesafe:jev-latest` struggle-0935 (truth False): p=0.58, confidence=0.16
- `typesafe:jev-latest` struggle-0940 (truth False): p=0.53, confidence=0.06
- `typesafe:jev-latest` struggle-1042 (truth True): p=0.36, confidence=0.28
- `typesafe:jev-latest` struggle-1076 (truth False): p=0.92, confidence=0.84
- `typesafe:jev-latest` struggle-1167 (truth False): p=0.64, confidence=0.28
- `typesafe:jev-latest` citations-0113 (truth True): p=0.44, confidence=0.12
- `typesafe:jev-latest` citations-0338 (truth False): p=0.58, confidence=0.16
- `typesafe:jev-latest` citations-0401 (truth True): p=0.13, confidence=0.74
- `typesafe:jev-latest` citations-0610 (truth True): p=0.29, confidence=0.42
- `typesafe:jev-latest` citations-0313 (truth True): p=0.36, confidence=0.28
- `typesafe:jev-latest` citations-0024 (truth False): p=0.51, confidence=0.02
- `typesafe:jev-latest` citations-0104 (truth True): p=0.3, confidence=0.4
- `typesafe:jev-latest` citations-0453 (truth True): p=0.25, confidence=0.5
- `typesafe:jev-latest` citations-0433 (truth True): p=0.21, confidence=0.58
- `typesafe:jev-latest` citations-0265 (truth True): p=0.47, confidence=0.06
- `typesafe:jev-latest` citations-0732 (truth True): p=0.49, confidence=0.02
- `typesafe:jev-latest` review-1304 (truth True): p=0.2, confidence=0.6
- `typesafe:jev-latest` review-1326 (truth True): p=0.22, confidence=0.56
- `typesafe:jev-latest` review-1331 (truth True): p=0.39, confidence=0.22
- `typesafe:jev-latest` review-1336 (truth True): p=0.33, confidence=0.34
- `xollama:tev1:latest` struggle-0656 (truth False): p=0.5358, confidence=0.0717
- `xollama:tev1:latest` struggle-0788 (truth False): p=0.5779, confidence=0.1559
- `xollama:tev1:latest` struggle-0794 (truth False): p=0.6129, confidence=0.2258
- `xollama:tev1:latest` struggle-0805 (truth False): p=0.6906, confidence=0.3812
- `xollama:tev1:latest` struggle-0826 (truth False): p=0.7093, confidence=0.4185
- `xollama:tev1:latest` struggle-0840 (truth False): p=0.6691, confidence=0.3381
- `xollama:tev1:latest` struggle-0853 (truth False): p=0.6191, confidence=0.2382
- `xollama:tev1:latest` struggle-0897 (truth False): p=0.5382, confidence=0.0764
- `xollama:tev1:latest` struggle-0919 (truth False): p=0.5133, confidence=0.0266
- `xollama:tev1:latest` struggle-0925 (truth False): p=0.8484, confidence=0.6968
- `xollama:tev1:latest` struggle-0928 (truth False): p=0.654, confidence=0.3079
- `xollama:tev1:latest` struggle-0931 (truth False): p=0.5987, confidence=0.1975
- `xollama:tev1:latest` struggle-0935 (truth False): p=0.8446, confidence=0.6892
- `xollama:tev1:latest` struggle-0940 (truth False): p=0.8496, confidence=0.6992
- `xollama:tev1:latest` struggle-0971 (truth False): p=0.548, confidence=0.0959
- `xollama:tev1:latest` struggle-0996 (truth False): p=0.6119, confidence=0.2238
- `xollama:tev1:latest` struggle-1004 (truth False): p=0.5407, confidence=0.0814
- `xollama:tev1:latest` struggle-1062 (truth False): p=0.5219, confidence=0.0439
- `xollama:tev1:latest` struggle-1076 (truth False): p=0.7183, confidence=0.4367
- `xollama:tev1:latest` struggle-1093 (truth False): p=0.6023, confidence=0.2047
- `xollama:tev1:latest` struggle-1102 (truth False): p=0.6628, confidence=0.3256
- `xollama:tev1:latest` struggle-1129 (truth False): p=0.6678, confidence=0.3356
- `xollama:tev1:latest` struggle-1199 (truth False): p=0.7458, confidence=0.4915
- `xollama:tev1:latest` struggle-0951 (truth False): p=0.7008, confidence=0.4015
- `xollama:tev1:latest` struggle-0031 (truth False): p=0.5164, confidence=0.0329
- `xollama:tev1:latest` struggle-0847 (truth False): p=0.5186, confidence=0.0371
- `xollama:tev1:latest` struggle-0896 (truth False): p=0.5147, confidence=0.0293
- `xollama:tev1:latest` struggle-1192 (truth False): p=0.5265, confidence=0.0531
- `xollama:tev1:latest` struggle-0988 (truth False): p=0.6271, confidence=0.2542
- `xollama:tev1:latest` citations-0279 (truth True): p=0.4437, confidence=0.1125
- `xollama:tev1:latest` citations-0666 (truth True): p=0.2302, confidence=0.5397
- `xollama:tev1:latest` citations-0180 (truth True): p=0.4437, confidence=0.1125
- `xollama:tev1:latest` citations-0338 (truth False): p=0.5715, confidence=0.1431
- `xollama:tev1:latest` citations-0542 (truth False): p=0.6585, confidence=0.317
- `xollama:tev1:latest` citations-0401 (truth True): p=0.2659, confidence=0.4683
- `xollama:tev1:latest` citations-0610 (truth True): p=0.3371, confidence=0.3259
- `xollama:tev1:latest` citations-0313 (truth True): p=0.271, confidence=0.458
- `xollama:tev1:latest` citations-1280 (truth False): p=0.5567, confidence=0.1134
- `xollama:tev1:latest` citations-1219 (truth True): p=0.4504, confidence=0.0991
- `xollama:tev1:latest` citations-0109 (truth True): p=0.4205, confidence=0.159
- `xollama:tev1:latest` citations-0176 (truth True): p=0.361, confidence=0.2781
- `xollama:tev1:latest` citations-0424 (truth True): p=0.3554, confidence=0.2893
- `xollama:tev1:latest` citations-0323 (truth False): p=0.566, confidence=0.1319
- `xollama:tev1:latest` citations-0381 (truth False): p=0.576, confidence=0.1519
- `xollama:tev1:latest` citations-0541 (truth False): p=0.6095, confidence=0.2191
- `xollama:tev1:latest` review-1302 (truth False): p=0.775, confidence=0.5499
- `xollama:tev1:latest` review-1312 (truth False): p=0.6424, confidence=0.2849
- `xollama:tev1:latest` review-1315 (truth False): p=0.7743, confidence=0.5487
- `xollama:tev1:latest` review-1317 (truth False): p=0.889, confidence=0.7779
- `xollama:tev1:latest` review-1319 (truth False): p=0.524, confidence=0.0481
- `xollama:tev1:latest` review-1320 (truth False): p=0.8031, confidence=0.6063
- `xollama:tev1:latest` review-1321 (truth False): p=0.6514, confidence=0.3027
- `xollama:tev1:latest` review-1322 (truth False): p=0.9251, confidence=0.8502
- `xollama:tev1:latest` review-1323 (truth False): p=0.592, confidence=0.184
- `xollama:tev1:latest` review-1324 (truth False): p=0.7991, confidence=0.5982
- `xollama:tev1:latest` review-1325 (truth False): p=0.5272, confidence=0.0545
- `xollama:tev1:latest` review-1327 (truth False): p=0.8283, confidence=0.6566
- `xollama:nimble:latest` struggle-0805 (truth False): p=0.6585, confidence=0.317
- `xollama:nimble:latest` struggle-0925 (truth False): p=0.6812, confidence=0.3623
- `xollama:nimble:latest` struggle-0935 (truth False): p=0.6569, confidence=0.3137
- `xollama:nimble:latest` struggle-0940 (truth False): p=0.6812, confidence=0.3623
- `xollama:nimble:latest` struggle-1042 (truth True): p=0.2479, confidence=0.5042
- `xollama:nimble:latest` struggle-1076 (truth False): p=0.8798, confidence=0.7595
- `xollama:nimble:latest` struggle-1093 (truth False): p=0.5043, confidence=0.0086
- `xollama:nimble:latest` struggle-1199 (truth False): p=0.6317, confidence=0.2634
- `xollama:nimble:latest` struggle-0988 (truth False): p=0.5862, confidence=0.1724
- `xollama:nimble:latest` citations-0338 (truth False): p=0.8979, confidence=0.7958
- `xollama:nimble:latest` citations-0542 (truth False): p=0.8926, confidence=0.7852
- `xollama:nimble:latest` citations-1280 (truth False): p=0.8922, confidence=0.7844
- `xollama:nimble:latest` citations-0535 (truth False): p=0.9361, confidence=0.8723
- `xollama:nimble:latest` citations-0114 (truth True): p=0.4991, confidence=0.0017
- `xollama:nimble:latest` citations-0808 (truth False): p=0.8985, confidence=0.7971
- `xollama:nimble:latest` citations-0672 (truth True): p=0.2658, confidence=0.4683
- `xollama:nimble:latest` citations-0323 (truth False): p=0.855, confidence=0.7099
- `xollama:nimble:latest` citations-0381 (truth False): p=0.9723, confidence=0.9446
- `xollama:nimble:latest` citations-0541 (truth False): p=0.6145, confidence=0.2291
- `xollama:nimble:latest` citations-0320 (truth False): p=0.5596, confidence=0.1192
- `xollama:nimble:latest` citations-1263 (truth False): p=0.5095, confidence=0.0189
- `xollama:nimble:latest` citations-0021 (truth False): p=0.8865, confidence=0.7731
- `xollama:nimble:latest` review-1302 (truth False): p=0.9254, confidence=0.8507
- `xollama:nimble:latest` review-1312 (truth False): p=0.648, confidence=0.296
- `xollama:nimble:latest` review-1315 (truth False): p=0.8998, confidence=0.7997
- `xollama:nimble:latest` review-1317 (truth False): p=0.6989, confidence=0.3978
- `xollama:nimble:latest` review-1319 (truth False): p=0.9093, confidence=0.8186
- `xollama:nimble:latest` review-1320 (truth False): p=0.9293, confidence=0.8587
- `xollama:nimble:latest` review-1321 (truth False): p=0.834, confidence=0.6681
- `xollama:nimble:latest` review-1322 (truth False): p=0.9232, confidence=0.8464
- `xollama:nimble:latest` review-1323 (truth False): p=0.8408, confidence=0.6816
- `xollama:nimble:latest` review-1324 (truth False): p=0.9277, confidence=0.8553
- `xollama:nimble:latest` review-1327 (truth False): p=0.8481, confidence=0.6961
- `xollama:clef-flash:latest` struggle-0925 (truth False): p=0.7891, confidence=0.5781
- `xollama:clef-flash:latest` struggle-0935 (truth False): p=0.8229, confidence=0.6457
- `xollama:clef-flash:latest` struggle-0940 (truth False): p=0.7891, confidence=0.5781
- `xollama:clef-flash:latest` struggle-1042 (truth True): p=0.3216, confidence=0.3569
- `xollama:clef-flash:latest` struggle-1076 (truth False): p=0.9494, confidence=0.8988
- `xollama:clef-flash:latest` struggle-1093 (truth False): p=0.5248, confidence=0.0495
- `xollama:clef-flash:latest` struggle-1081 (truth False): p=0.5087, confidence=0.0174
- `xollama:clef-flash:latest` struggle-1135 (truth False): p=0.7096, confidence=0.4192
- `xollama:clef-flash:latest` citations-0338 (truth False): p=0.9451, confidence=0.8902
- `xollama:clef-flash:latest` citations-0542 (truth False): p=0.9319, confidence=0.8638
- `xollama:clef-flash:latest` citations-0313 (truth True): p=0.1221, confidence=0.7559
- `xollama:clef-flash:latest` citations-1280 (truth False): p=0.7767, confidence=0.5534
- `xollama:clef-flash:latest` citations-0109 (truth True): p=0.4262, confidence=0.1475
- `xollama:clef-flash:latest` citations-0808 (truth False): p=0.667, confidence=0.3341
- `xollama:clef-flash:latest` citations-0323 (truth False): p=0.9122, confidence=0.8243
- `xollama:clef-flash:latest` citations-0381 (truth False): p=0.8763, confidence=0.7527
- `xollama:clef-flash:latest` citations-0541 (truth False): p=0.5578, confidence=0.1155
- `xollama:clef-flash:latest` citations-1263 (truth False): p=0.7443, confidence=0.4886
- `xollama:clef-flash:latest` citations-0021 (truth False): p=0.548, confidence=0.096
- `xollama:clef-flash:latest` review-1336 (truth True): p=0.214, confidence=0.572
- `xollama:clef-flash:latest` review-1302 (truth False): p=0.8973, confidence=0.7945
- `xollama:clef-flash:latest` review-1303 (truth False): p=0.7562, confidence=0.5125
- `xollama:clef-flash:latest` review-1312 (truth False): p=0.7986, confidence=0.5971
- `xollama:clef-flash:latest` review-1315 (truth False): p=0.7652, confidence=0.5304
- `xollama:clef-flash:latest` review-1317 (truth False): p=0.9315, confidence=0.8631
- `xollama:clef-flash:latest` review-1319 (truth False): p=0.8424, confidence=0.6849
- `xollama:clef-flash:latest` review-1322 (truth False): p=0.9381, confidence=0.8762
- `xollama:clef-flash:latest` review-1323 (truth False): p=0.7986, confidence=0.5972
- `xollama:clef-flash:latest` review-1324 (truth False): p=0.706, confidence=0.412
- `xollama:clef-flash:latest` review-1325 (truth False): p=0.7415, confidence=0.483
- `xollama:clef-flash:latest` review-1327 (truth False): p=0.793, confidence=0.5859

## Live shadow scores, 2026-10-18 to 10-22 (added 2026-10-24)

Every council since shadow mode deployed asked `jev-latest` the
`confidence` seam and recorded the answer without acting on it: 11
councils, 34 events, no errors. Read from each council's
`transcript.db` (`runtime_events` kind `decision`).

| council | graded (owner) | synthesizer `sound` p | critic `enough` p | answer chars (12k bound) |
|---|---|---|---|---|
| cline-sync run 1 (1033-5a67) | weak | 0.14 | 0.18 | 5,654 whole |
| run 2 (1120-95ce) | weak | 0.17 | 0.10, 0.17 | 5,047 whole |
| run 3 (1257-42d3) | weak | 0.14 | 0.13, 0.10 | 5,567 whole |
| run 4 (1358-a02b) | weak | 0.12 | 0.19, 0.06 | 6,469 whole |
| run 5 (1458-cb89) | weak | 0.11 | 0.16, 0.24 | 14,605 cut |
| run 6 (1554-40e9) | lost a finding | 0.19 | 0.17, 0.31 | 17,037 cut |
| run 7 (1747-c73d) | research best yet | 0.16 | 0.22, 0.25 | 16,537 cut |
| run 8 (1911-d11b) | best answer of the eight | 0.14 | 0.22, 0.13 | 19,951 cut |
| run 9 (2314-4c0f) | best run yet | 0.16 | 0.24, 0.14 | 27,516 cut |
| giano (1804-ad1f) | accepted | 0.46 | 0.31, 0.28 | 15,147 cut |
| giano (2241-1d84) | accepted | 0.30 | 0.29, 0.41 | 45,295 cut |

Findings:

- On the nine audit councils Jev's answer score is flat, 0.11-0.19, while
  the owner's grades run from weak (runs 1-5) to the best yet (runs 7-9):
  means 0.136 (runs 1-5) against 0.153 (runs 7-9). It does not rank them.
- Every one of those scores is below the label the advice would give it
  ("accepted averaged p 0.46, wrong ones 0.20"). Acting, the critic of
  the best run would be told its evidence scores like a wrong answer's.
- The score is made on a cut copy more often than not: every answer past
  `MAX_PART_CHARS` (12,000) is sent as its head and tail, and the critic's
  `evidence` (all reports) is always cut. Runs 1-4 were sent whole and
  are just as flat, so the cut is not the whole story, but the events did
  not record it. From 2026-10-24 each event carries `cut` (characters
  removed per part, `decision.cut_parts()`) and the miner keeps it.

Not a basis for acting: `confidence` stays in shadow.
