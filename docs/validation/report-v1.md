# Monte Carlo validation report (protocol hvsr-mc-validation-v1, locked test set)

- Protocol SHA-256: `8768c5c50ece353b476b51915ed4038d6541026f3481dbf93d30d614333dc985` (lock commit `a3adb02a1ace38de6340b63ccb052e26c1622d1c`)
- Code commit at run time: `a3adb02a1ace38de6340b63ccb052e26c1622d1c`; generated 2026-10-21T00:24:30Z
- Replicates per scenario: [400]; truth records per scenario: 200
- **AC-S5-06 verdict: NOT MET**

The CI targets the selection-policy mean curve (Standard: all quality-screened non-overlapping windows; SDOF:
R2-selected windows in the family matching the true fundamental). Coverage of the physical transfer-function
amplitude and of the true resonance frequency is reported separately and is not a target. Coverage is computed
over replicates whose CI was estimated or limited; the no-result rate shows how often no CI was produced.

## Predeclared targets

| Target | Result | k/n | rate | exact 95% CI | p (one-sided) | alpha |
|---|---|---|---|---|---|---|
| T-COV-STD-flat_white | PASS | 377/398 | 94.7% | 92.0-96.7% | 0.43 | 0.0018 |
| T-COV-STD-flat_colored_episodes | NOT_EVALUABLE | 0/0 | n/a | n/a | n/a | 0.0018 |
| T-COV-STD-sdof_2hz | PASS | 371/398 | 93.2% | 90.3-95.5% | 0.069 | 0.0018 |
| T-COV-STD-broad_asymmetric_3hz | PASS | 380/393 | 96.7% | 94.4-98.2% | 0.96 | 0.0018 |
| T-COV-STD-two_peaks | PASS | 382/397 | 96.2% | 93.8-97.9% | 0.9 | 0.0018 |
| T-COV-STD-weak_fundamental | PASS | 366/394 | 92.9% | 89.9-95.2% | 0.041 | 0.0018 |
| T-COV-STD-regime_switching | FAIL | 174/232 | 75.0% | 68.9-80.4% | 1.5e-24 | 0.0018 |
| T-COV-STD-colored_sdof | NOT_EVALUABLE | 0/0 | n/a | n/a | n/a | 0.0018 |
| T-COV-STD-transients | PASS | 377/399 | 94.5% | 91.8-96.5% | 0.35 | 0.0018 |
| T-COV-STD-colored_mild_sdof | PASS | 369/395 | 93.4% | 90.5-95.7% | 0.096 | 0.0018 |
| T-COV-STD-episodes_lognormal | PASS | 372/392 | 94.9% | 92.2-96.9% | 0.5 | 0.0018 |
| T-COV-STD-machine_tone | PASS | 367/394 | 93.1% | 90.2-95.4% | 0.063 | 0.0018 |
| T-COV-STD-component_mismatch | PASS | 373/394 | 94.7% | 92.0-96.7% | 0.41 | 0.0018 |
| T-COV-STD-layered_sh | PASS | 372/396 | 93.9% | 91.1-96.1% | 0.19 | 0.0018 |
| T-COV-SDOF-sdof_2hz | PASS | 372/398 | 93.5% | 90.6-95.7% | 0.1 | 0.0018 |
| T-COV-SDOF-broad_asymmetric_3hz | PASS | 379/394 | 96.2% | 93.8-97.9% | 0.89 | 0.0018 |
| T-COV-SDOF-two_peaks | PASS | 383/398 | 96.2% | 93.9-97.9% | 0.9 | 0.0018 |
| T-COV-SDOF-weak_fundamental | PASS | 356/376 | 94.7% | 91.9-96.7% | 0.42 | 0.0018 |
| T-COV-SDOF-regime_switching | FAIL | 188/256 | 73.4% | 67.6-78.7% | 3.3e-30 | 0.0018 |
| T-COV-SDOF-colored_sdof | NOT_EVALUABLE | 0/0 | n/a | n/a | n/a | 0.0018 |
| T-COV-SDOF-transients | PASS | 376/391 | 96.2% | 93.8-97.8% | 0.88 | 0.0018 |
| T-COV-SDOF-colored_mild_sdof | PASS | 370/397 | 93.2% | 90.3-95.5% | 0.068 | 0.0018 |
| T-COV-SDOF-episodes_lognormal | PASS | 376/395 | 95.2% | 92.6-97.1% | 0.6 | 0.0018 |
| T-COV-SDOF-machine_tone | PASS | 373/398 | 93.7% | 90.9-95.9% | 0.15 | 0.0018 |
| T-COV-SDOF-component_mismatch | PASS | 370/394 | 93.9% | 91.1-96.1% | 0.19 | 0.0018 |
| T-COV-SDOF-layered_sh | PASS | 371/390 | 95.1% | 92.5-97.0% | 0.58 | 0.0018 |
| T-FALSEPEAK-flat_white | PASS | 0/400 | 0.0% | 0.0-0.9% | 1 | 0.0018 |
| T-FALSEPEAK-flat_colored_episodes | PASS | 42/400 | 10.5% | 7.7-13.9% | 0.39 | 0.0018 |

## Per-scenario results

### flat_white

Discovery: fundamental correct n/a; false plausible peak 0.0% (0/400; 0.0-0.9); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 94.7% (377/398; 92.0-96.7) | n/a |
| CI coverage of physical amplitude | 37.9% (151/398; 33.2-42.9) | n/a |
| median CI width (ln) | 0.109 | n/a |
| no-result rate | 0.5% (2/400; 0.1-1.8) | 100.0% (400/400; 99.1-100.0) |
| CI status counts | {'ci_estimated': 398, 'ci_not_estimable': 2} | {'no_selection': 400} |
| fp bias, mean ln(fp/f0) | n/a | n/a |
| median abs ln error | n/a | n/a |
| mean abs relative fp error | n/a | n/a |
| no fp in family | 100.0% (400/400; 99.1-100.0) | 100.0% (400/400; 99.1-100.0) |
| peak interval covers f0 (conditional) | n/a | n/a |
| median windows retained | 36 | 0 |
| median informative blocks | 18 | n/a |
| median uncertainty time (s) | 0.277 | n/a |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse n/a; inferred fundamental wrong n/a. Median compute: core 0.260 s, SDOF matching 0.001 s.

### flat_colored_episodes

Discovery: fundamental correct n/a; false plausible peak 10.5% (42/400; 7.7-13.9); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | n/a | n/a |
| CI coverage of physical amplitude | n/a | n/a |
| median CI width (ln) | n/a | n/a |
| no-result rate | 100.0% (400/400; 99.1-100.0) | 100.0% (400/400; 99.1-100.0) |
| CI status counts | {'ci_not_estimable': 400} | {'no_selection': 400} |
| fp bias, mean ln(fp/f0) | n/a | n/a |
| median abs ln error | n/a | n/a |
| mean abs relative fp error | n/a | n/a |
| no fp in family | 91.0% (364/400; 87.8-93.6) | 100.0% (400/400; 99.1-100.0) |
| peak interval covers f0 (conditional) | n/a | n/a |
| median windows retained | 3 | 0 |
| median informative blocks | 3 | n/a |
| median uncertainty time (s) | 0.001 | n/a |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse n/a; inferred fundamental wrong n/a. Median compute: core 0.157 s, SDOF matching 0.001 s.

### sdof_2hz

Discovery: fundamental correct 99.8% (399/400; 98.6-100.0); false plausible peak 0.2% (1/400; 0.0-1.4); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 93.2% (371/398; 90.3-95.5) | 93.5% (372/398; 90.6-95.7) |
| CI coverage of physical amplitude | 89.4% (356/398; 86.0-92.3) | 75.6% (301/398; 71.1-79.8) |
| median CI width (ln) | 0.117 | 0.125 |
| no-result rate | 0.5% (2/400; 0.1-1.8) | 0.5% (2/400; 0.1-1.8) |
| CI status counts | {'ci_estimated': 398, 'ci_not_estimable': 2} | {'ci_estimated': 398, 'ci_not_estimable': 2} |
| fp bias, mean ln(fp/f0) | -0.0131 | -0.0014 |
| median abs ln error | 0.0116 | 0.0078 |
| mean abs relative fp error | 0.0147 | 0.0086 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.0% (0/400; 0.0-0.9) |
| peak interval covers f0 (conditional) | 85.7% (341/398; 81.8-89.0) | 93.0% (370/398; 90.0-95.3) |
| median windows retained | 33 | 26 |
| median informative blocks | 18 | 18 |
| median uncertainty time (s) | 0.368 | 0.343 |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse 33.2% (133/400; 28.6-38.1); inferred fundamental wrong 0.2% (1/400; 0.0-1.4). Median compute: core 0.308 s, SDOF matching 0.018 s.

### broad_asymmetric_3hz

Discovery: fundamental correct 99.5% (398/400; 98.2-99.9); false plausible peak 0.2% (1/400; 0.0-1.4); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 96.7% (380/393; 94.4-98.2) | 96.2% (379/394; 93.8-97.9) |
| CI coverage of physical amplitude | 40.5% (159/393; 35.6-45.5) | 22.6% (89/394; 18.6-27.0) |
| median CI width (ln) | 0.091 | 0.105 |
| no-result rate | 1.8% (7/400; 0.7-3.6) | 1.5% (6/400; 0.6-3.2) |
| CI status counts | {'ci_estimated': 393, 'ci_not_estimable': 7} | {'ci_estimated': 394, 'ci_not_estimable': 5, 'no_selection': 1} |
| fp bias, mean ln(fp/f0) | -0.0617 | -0.0634 |
| median abs ln error | 0.0509 | 0.0626 |
| mean abs relative fp error | 0.0606 | 0.0613 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.2% (1/400; 0.0-1.4) |
| peak interval covers f0 (conditional) | 61.8% (243/393; 56.8-66.7) | 34.5% (136/394; 29.8-39.4) |
| median windows retained | 36 | 27 |
| median informative blocks | 18 | 18 |
| median uncertainty time (s) | 0.364 | 0.344 |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse 60.7% (242/399; 55.7-65.5); inferred fundamental wrong 0.5% (2/400; 0.1-1.8). Median compute: core 0.288 s, SDOF matching 0.017 s.

### two_peaks

Discovery: fundamental correct 100.0% (400/400; 99.1-100.0); false plausible peak 0.0% (0/400; 0.0-0.9); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 96.2% (382/397; 93.8-97.9) | 96.2% (383/398; 93.9-97.9) |
| CI coverage of physical amplitude | 91.4% (363/397; 88.2-94.0) | 73.6% (293/398; 69.0-77.9) |
| median CI width (ln) | 0.126 | 0.150 |
| no-result rate | 0.8% (3/400; 0.2-2.2) | 0.5% (2/400; 0.1-1.8) |
| CI status counts | {'ci_estimated': 397, 'ci_not_estimable': 3} | {'ci_estimated': 398, 'ci_not_estimable': 2} |
| fp bias, mean ln(fp/f0) | -0.0014 | +0.0108 |
| median abs ln error | 0.0093 | 0.0129 |
| mean abs relative fp error | 0.0107 | 0.0140 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.0% (0/400; 0.0-0.9) |
| peak interval covers f0 (conditional) | 91.2% (362/397; 88.0-93.8) | 79.4% (316/398; 75.1-83.3) |
| median windows retained | 36 | 27 |
| median informative blocks | 18 | 18 |
| median uncertainty time (s) | 0.355 | 0.335 |

R2 vs fundamental: R2-preferred family wrong 32.0% (128/400; 27.5-36.8); SDOF mean R2 above Standard windows while |fp error| worse 63.7% (255/400; 58.8-68.5); inferred fundamental wrong 0.0% (0/400; 0.0-0.9). Median compute: core 0.264 s, SDOF matching 0.037 s.

### weak_fundamental

Discovery: fundamental correct 99.8% (399/400; 98.6-100.0); false plausible peak 0.0% (0/400; 0.0-0.9); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 92.9% (366/394; 89.9-95.2) | 94.7% (356/376; 91.9-96.7) |
| CI coverage of physical amplitude | 84.8% (334/394; 80.8-88.2) | 73.1% (275/376; 68.4-77.6) |
| median CI width (ln) | 0.156 | 0.231 |
| no-result rate | 1.5% (6/400; 0.6-3.2) | 6.0% (24/400; 3.9-8.8) |
| CI status counts | {'ci_estimated': 394, 'ci_not_estimable': 6} | {'ci_estimated': 372, 'ci_limited': 4, 'ci_not_estimable': 24} |
| fp bias, mean ln(fp/f0) | -0.0038 | +0.0113 |
| median abs ln error | 0.0143 | 0.0215 |
| mean abs relative fp error | 0.0173 | 0.0255 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.0% (0/400; 0.0-0.9) |
| peak interval covers f0 (conditional) | 90.9% (358/394; 87.6-93.5) | 86.4% (325/376; 82.6-89.7) |
| median windows retained | 36 | 15 |
| median informative blocks | 18 | 13 |
| median uncertainty time (s) | 0.358 | 0.328 |

R2 vs fundamental: R2-preferred family wrong 100.0% (400/400; 99.1-100.0); SDOF mean R2 above Standard windows while |fp error| worse 64.0% (256/400; 59.1-68.7); inferred fundamental wrong 0.2% (1/400; 0.0-1.4). Median compute: core 0.269 s, SDOF matching 0.035 s.

### regime_switching

Discovery: fundamental correct 100.0% (400/400; 99.1-100.0); false plausible peak 0.0% (0/400; 0.0-0.9); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 75.0% (174/232; 68.9-80.4) | 73.4% (188/256; 67.6-78.7) |
| CI coverage of physical amplitude | 31.0% (72/232; 25.1-37.4) | 34.0% (87/256; 28.2-40.1) |
| median CI width (ln) | 0.237 | 0.237 |
| no-result rate | 42.0% (168/400; 37.1-47.0) | 36.0% (144/400; 31.3-40.9) |
| CI status counts | {'ci_estimated': 232, 'ci_not_estimable': 168} | {'ci_estimated': 256, 'ci_not_estimable': 144} |
| fp bias, mean ln(fp/f0) | -0.0023 | +0.0397 |
| median abs ln error | 0.0216 | 0.0397 |
| mean abs relative fp error | 0.0256 | 0.0431 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.0% (0/400; 0.0-0.9) |
| peak interval covers f0 (conditional) | 95.3% (221/232; 91.7-97.6) | 63.3% (162/256; 57.1-69.2) |
| median windows retained | 36 | 26 |
| median informative blocks | 18 | 17 |
| median uncertainty time (s) | 0.215 | 0.213 |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse 76.2% (305/400; 71.8-80.3); inferred fundamental wrong 0.0% (0/400; 0.0-0.9). Median compute: core 0.235 s, SDOF matching 0.017 s.

### colored_sdof

Discovery: fundamental correct 0.0% (0/400; 0.0-0.9); false plausible peak 0.5% (2/400; 0.1-1.8); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | n/a | n/a |
| CI coverage of physical amplitude | n/a | n/a |
| median CI width (ln) | n/a | n/a |
| no-result rate | 100.0% (400/400; 99.1-100.0) | 100.0% (400/400; 99.1-100.0) |
| CI status counts | {'ci_not_estimable': 400} | {'ci_not_estimable': 2, 'no_selection': 398} |
| fp bias, mean ln(fp/f0) | -1.5003 | -0.0976 |
| median abs ln error | 1.5003 | 0.0976 |
| mean abs relative fp error | 0.7769 | 0.0929 |
| no fp in family | 99.5% (398/400; 98.2-99.9) | 99.5% (398/400; 98.2-99.9) |
| peak interval covers f0 (conditional) | n/a | n/a |
| median windows retained | 1 | 0 |
| median informative blocks | 2 | 4 |
| median uncertainty time (s) | 0.001 | 0.002 |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse 0.0% (0/2; 0.0-84.2); inferred fundamental wrong 100.0% (400/400; 99.1-100.0). Median compute: core 0.155 s, SDOF matching 0.001 s.

### transients

Discovery: fundamental correct 99.0% (396/400; 97.5-99.7); false plausible peak 0.2% (1/400; 0.0-1.4); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 94.5% (377/399; 91.8-96.5) | 96.2% (376/391; 93.8-97.8) |
| CI coverage of physical amplitude | 63.7% (254/399; 58.7-68.4) | 67.3% (263/391; 62.4-71.9) |
| median CI width (ln) | 0.118 | 0.126 |
| no-result rate | 0.2% (1/400; 0.0-1.4) | 2.2% (9/400; 1.0-4.2) |
| CI status counts | {'ci_estimated': 399, 'ci_not_estimable': 1} | {'ci_estimated': 391, 'ci_not_estimable': 9} |
| fp bias, mean ln(fp/f0) | -0.0076 | +0.0300 |
| median abs ln error | 0.0191 | 0.0334 |
| mean abs relative fp error | 0.0253 | 0.0357 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.0% (0/400; 0.0-0.9) |
| peak interval covers f0 (conditional) | 92.7% (370/399; 89.7-95.1) | 72.9% (285/391; 68.2-77.2) |
| median windows retained | 25 | 20 |
| median informative blocks | 16 | 15 |
| median uncertainty time (s) | 0.359 | 0.325 |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse 70.2% (281/400; 65.5-74.7); inferred fundamental wrong 1.0% (4/400; 0.3-2.5). Median compute: core 0.250 s, SDOF matching 0.012 s.

### colored_mild_sdof

Discovery: fundamental correct 99.2% (397/400; 97.8-99.8); false plausible peak 0.0% (0/400; 0.0-0.9); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 93.4% (369/395; 90.5-95.7) | 93.2% (370/397; 90.3-95.5) |
| CI coverage of physical amplitude | 85.8% (339/395; 82.0-89.1) | 62.2% (247/397; 57.2-67.0) |
| median CI width (ln) | 0.165 | 0.169 |
| no-result rate | 1.2% (5/400; 0.4-2.9) | 0.8% (3/400; 0.2-2.2) |
| CI status counts | {'ci_estimated': 395, 'ci_not_estimable': 5} | {'ci_estimated': 397, 'ci_not_estimable': 3} |
| fp bias, mean ln(fp/f0) | -0.0115 | +0.0052 |
| median abs ln error | 0.0225 | 0.0170 |
| mean abs relative fp error | 0.0268 | 0.0210 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.0% (0/400; 0.0-0.9) |
| peak interval covers f0 (conditional) | 91.6% (362/395; 88.5-94.2) | 90.7% (360/397; 87.4-93.4) |
| median windows retained | 27 | 22 |
| median informative blocks | 17 | 17 |
| median uncertainty time (s) | 0.368 | 0.360 |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse 37.8% (151/400; 33.0-42.7); inferred fundamental wrong 0.8% (3/400; 0.2-2.2). Median compute: core 0.268 s, SDOF matching 0.013 s.

### episodes_lognormal

Discovery: fundamental correct 99.8% (399/400; 98.6-100.0); false plausible peak 0.0% (0/400; 0.0-0.9); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 94.9% (372/392; 92.2-96.9) | 95.2% (376/395; 92.6-97.1) |
| CI coverage of physical amplitude | 71.2% (279/392; 66.4-75.6) | 73.2% (289/395; 68.5-77.5) |
| median CI width (ln) | 0.124 | 0.134 |
| no-result rate | 2.0% (8/400; 0.9-3.9) | 1.2% (5/400; 0.4-2.9) |
| CI status counts | {'ci_estimated': 392, 'ci_not_estimable': 8} | {'ci_estimated': 395, 'ci_not_estimable': 5} |
| fp bias, mean ln(fp/f0) | -0.0003 | +0.0379 |
| median abs ln error | 0.0195 | 0.0388 |
| mean abs relative fp error | 0.0242 | 0.0416 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.0% (0/400; 0.0-0.9) |
| peak interval covers f0 (conditional) | 92.9% (364/392; 89.8-95.2) | 63.0% (249/395; 58.1-67.8) |
| median windows retained | 33 | 25 |
| median informative blocks | 18 | 18 |
| median uncertainty time (s) | 0.351 | 0.339 |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse 79.5% (318/400; 75.2-83.4); inferred fundamental wrong 0.2% (1/400; 0.0-1.4). Median compute: core 0.263 s, SDOF matching 0.016 s.

### machine_tone

Discovery: fundamental correct 100.0% (400/400; 99.1-100.0); false plausible peak 0.0% (0/400; 0.0-0.9); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 93.1% (367/394; 90.2-95.4) | 93.7% (373/398; 90.9-95.9) |
| CI coverage of physical amplitude | 58.9% (232/394; 53.8-63.8) | 62.8% (250/398; 57.9-67.6) |
| median CI width (ln) | 0.108 | 0.120 |
| no-result rate | 1.5% (6/400; 0.6-3.2) | 0.5% (2/400; 0.1-1.8) |
| CI status counts | {'ci_estimated': 394, 'ci_not_estimable': 6} | {'ci_estimated': 398, 'ci_not_estimable': 2} |
| fp bias, mean ln(fp/f0) | -0.0004 | +0.0363 |
| median abs ln error | 0.0216 | 0.0381 |
| mean abs relative fp error | 0.0251 | 0.0392 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.0% (0/400; 0.0-0.9) |
| peak interval covers f0 (conditional) | 91.6% (361/394; 88.4-94.2) | 65.6% (261/398; 60.7-70.2) |
| median windows retained | 36 | 26 |
| median informative blocks | 18 | 18 |
| median uncertainty time (s) | 0.355 | 0.345 |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse 71.8% (287/400; 67.1-76.1); inferred fundamental wrong 0.0% (0/400; 0.0-0.9). Median compute: core 0.257 s, SDOF matching 0.018 s.

### component_mismatch

Discovery: fundamental correct 93.5% (374/400; 90.6-95.7); false plausible peak 0.2% (1/400; 0.0-1.4); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 94.7% (373/394; 92.0-96.7) | 93.9% (370/394; 91.1-96.1) |
| CI coverage of physical amplitude | 88.8% (350/394; 85.3-91.8) | 75.9% (299/394; 71.4-80.0) |
| median CI width (ln) | 0.093 | 0.099 |
| no-result rate | 1.5% (6/400; 0.6-3.2) | 1.5% (6/400; 0.6-3.2) |
| CI status counts | {'ci_estimated': 394, 'ci_not_estimable': 6} | {'ci_estimated': 394, 'ci_not_estimable': 6} |
| fp bias, mean ln(fp/f0) | -0.0149 | -0.0039 |
| median abs ln error | 0.0095 | 0.0057 |
| mean abs relative fp error | 0.0133 | 0.0071 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.0% (0/400; 0.0-0.9) |
| peak interval covers f0 (conditional) | 85.8% (338/394; 81.9-89.1) | 90.1% (355/394; 86.7-92.9) |
| median windows retained | 35 | 27 |
| median informative blocks | 18 | 18 |
| median uncertainty time (s) | 0.356 | 0.333 |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse 30.2% (121/400; 25.8-35.0); inferred fundamental wrong 6.5% (26/400; 4.3-9.4). Median compute: core 0.280 s, SDOF matching 0.018 s.

### layered_sh

Discovery: fundamental correct 99.8% (399/400; 98.6-100.0); false plausible peak 0.0% (0/400; 0.0-0.9); source flag 0.0% (0/400; 0.0-0.9).

| Metric | Standard | SDOF |
|---|---|---|
| CI coverage of policy mean | 93.9% (372/396; 91.1-96.1) | 95.1% (371/390; 92.5-97.0) |
| CI coverage of physical amplitude | 88.4% (350/396; 84.8-91.4) | 86.2% (336/390; 82.3-89.4) |
| median CI width (ln) | 0.112 | 0.118 |
| no-result rate | 1.0% (4/400; 0.3-2.5) | 2.5% (10/400; 1.2-4.5) |
| CI status counts | {'ci_estimated': 396, 'ci_not_estimable': 4} | {'ci_estimated': 390, 'ci_not_estimable': 10} |
| fp bias, mean ln(fp/f0) | -0.0058 | +0.0064 |
| median abs ln error | 0.0098 | 0.0095 |
| mean abs relative fp error | 0.0115 | 0.0106 |
| no fp in family | 0.0% (0/400; 0.0-0.9) | 0.0% (0/400; 0.0-0.9) |
| peak interval covers f0 (conditional) | 91.2% (361/396; 87.9-93.8) | 86.9% (339/390; 83.2-90.1) |
| median windows retained | 36 | 28 |
| median informative blocks | 18 | 18 |
| median uncertainty time (s) | 0.354 | 0.325 |

R2 vs fundamental: R2-preferred family wrong n/a; SDOF mean R2 above Standard windows while |fp error| worse 50.0% (200/400; 45.0-55.0); inferred fundamental wrong 0.2% (1/400; 0.0-1.4). Median compute: core 0.256 s, SDOF matching 0.047 s.

## Scope

- All signals are synthetic (`synthetic-3c-v1`): independent Gaussian components shaped by a known transfer
  function. They are research fixtures, not physical wavefield simulations; real-data coverage is not established.
- The layered case is a 1D SH transfer function computed by this project, not a published benchmark (AC-S5-07 BLOCKED).
- The 95 % label remains nominal: `coverage_calibrated` stays false in every analysis output.
