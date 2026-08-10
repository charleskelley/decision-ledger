# Scenarios — Calibration Provenance

This document records what the scenario generator's distributions are
*actually* informed by, and scopes a polish-phase project to do the
raw-RBA-dataset analysis properly.

The DR-10 hybrid data strategy specifies "calibrate the scenario generator's
baseline behavior distributions from the DAS Group RBA dataset." That
intent is real; the *raw-dataset analysis* it implies has not been done.
This document closes the gap honestly: the generator's distributions are
informed by the *published descriptive statistics* in Wiefling et al.
(ACM TOPS 2022), not by ingesting the 31.3M-event dataset and fitting
distributions to it. The polish-phase plan below records what that proper
calibration would look like as a follow-on project.

---

## Source

Wiefling, S., Jørgensen, P. R., Thunem, S., Iacono, L. L. (2022).
*Pump Up Password Security! Evaluating and Enhancing Risk-Based
Authentication on a Real-World Large-Scale Online Service.*
**ACM Transactions on Privacy and Security 26(1), Article 6.**
DOI: [10.1145/3546069](https://doi.org/10.1145/3546069). Open-access
preprint: [arXiv:2206.15139](https://arxiv.org/abs/2206.15139). Dataset on
[Zenodo](https://doi.org/10.5281/zenodo.6782156) and
[GitHub](https://github.com/das-group/rba-dataset).

The dataset captures 31.3M login attempts from 3.3M users on a Norwegian
SSO service (Telenor) over more than one year (Feb 2020 – Feb 2021). The
paper's published descriptive statistics inform the generator's archetype
distributions.

---

## Calibration table — published statistics → generator parameters

Each row maps a generator concern to a paper-cited number with section
attribution. Section references are to the arXiv version; the same
statistics appear in the ACM TOPS 2022 publication.

| Generator concern | Paper-cited number | Source |
|---|---|---|
| Auth outcome weights (success vs failure) | 12.5M successful / 18.8M failed (≈ 40% / 60%) | §4.1 |
| Failed-attempt funnel (where failures stop) | 90% at password / 9.9% at OTP / 0.1% at mobile-verify | §4.1 |
| Username-correct-but-fail rate | 25% of failed attempts had a correct username | §4.1 |
| ATO base rate | 87 confirmed account-takeover successes among 12.5M successful logins (~7×10⁻⁶) | §4.1 |
| Per-user login frequency | mean 3.8, median 2, SD 9.35, max 5,972 | §4.1 |
| User activity split | 48.3% logged < 1 month total; 22.4% daily | §4.1 |
| Device class split | 65.3% mobile / 34.6% desktop+other | §4.1 |
| Desktop OS shares | Windows 79.2% / macOS 19.4% / Linux 1.4% | §4.1 |
| Mobile OS shares | Android 64.9% / iOS 35.1% | §4.1 |
| Browser shares | Chrome 59.8% / Safari 27.4% / Edge 5.9% / Firefox 3.0% | §4.1 |
| Feature weights — IP / ASN / country | 0.6 / 0.3 / 0.1 | §4.3 |
| Feature weights — UA full / browser / OS / device | 0.53 / 0.27 / 0.19 / 0.01 | §4.3 |
| Attacker taxonomy | Naive / VPN / Targeted / Very Targeted | §5, Fig 2 |

These numbers informed the design of `generator/scenarios/*.yaml` and
`reasoner/account_takeover/scorer/trainer.py:_generate_sample`, but were not used in a
data-fitting sense — no empirical distribution was extracted from the raw
dataset and used to set parameters.

---

## Per-scenario summary

The eight scenarios under `generator/scenarios/` map to risk patterns
with corresponding generator parameters and expected pipeline actions:

| Scenario | Pattern | Expected action(s) | Paper signal |
|---|---|---|---|
| `baseline_normal` | Established device, low velocity, single home city | ALLOW (typically fast-path) | Login frequency + device + browser distributions (§4.1) |
| `credential_stuffing_burst` | Short bursts of failed attempts with username guesses | BLOCK or HOLD | Failed-attempt funnel + naive-attacker IP-rotation patterns (§4.1, §5) |
| `high_velocity_legitimate` | Genuine user with elevated velocity (e.g., shared service account) | ALLOW or CHALLENGE | Activity split — daily users (22.4%) vs sub-monthly (48.3%) (§4.1) |
| `geo_impossible` | Impossible-travel velocity between successive logins | BLOCK | VPN-attacker geographic spoofing pattern (§5) |
| `device_fingerprint_anomaly` | Login from a never-seen device fingerprint | CHALLENGE or HOLD | Targeted-attacker device-mimicry pattern (§5) |
| `novel_entity` | Account with sparse history (new user) | HOLD | Login-history size distribution (§4.1, Fig 1) |
| `post_breach_ato` | Replay-style attack with high success rate from new device + impossible travel | HOLD or BLOCK | Very-targeted attacker profile — the 87 confirmed ATOs (§5) |
| `adversarial_probe` | Probing pattern that tests the system's policy boundaries | HOLD | Designed pattern; no direct paper signal |

The scenarios are *designed against the same archetypes the paper
characterizes*, not derived from the data via clustering or distribution
fitting.

---

## Qualitative-inference parameters

The following generator parameters have **no direct paper-cited number**.
They are designed by intuition aligned to the scenario archetype rather
than calibrated from data:

- Per-user IP-address diversity distribution.
- Per-user ASN diversity / ASN reputation pools.
- Per-user geographic-mobility patterns (country and city counts per user).
- Time-of-day / day-of-week login cadence.
- Inter-login interval distributions.
- Velocity-window thresholds at 1-min / 5-min / 60-min / 1440-min.
- Travel-speed thresholds for impossible-travel detection (1000–5000 km/h
  in the trainer's archetype distributions).
- Device-consistency-score and user-agent-consistency-score
  distributions.

Each of these is recorded in code with the scenario archetype in mind but
not validated against empirical data. The polish-phase project below
addresses every entry on this list.

---

## Decisions & Tradeoffs

- **Qualitative-from-published over raw-dataset analysis.** Scope
  discipline. The MVP delivers an *architecture* — the framework, the
  reasoner pattern, the audit-replay guarantee, the eval harness. Adding
  a full RBA-data analysis would be a small ML-research project on top
  of the architecture, not part of it. DR-10 frames this honestly: "the
  architecture is the point, not the specific data source."
- **Cited paper statistics > fabricated numbers.** Any parameter that
  could be backed by a published number is — see the calibration table
  above. Anything that couldn't be is explicitly listed under
  *Qualitative-inference parameters*. No invented citations.
- **Fix the unsupported claim in `baseline_normal.yaml`.** That YAML's
  prose previously stated *"Calibrated against DAS RBA dataset
  distributions for a representative normal."* That was inaccurate (no
  calibration was performed). Replaced with a pointer to this document.
- **Polish-phase plan recorded, not deferred silently.** The follow-up
  work is scoped explicitly so it can be picked up as a separate
  portfolio artifact rather than vanishing into "we'll do it later."

---

## Polish phase — full RBA calibration project

A scoped follow-on project that converts this honest qualitative
calibration into a proper data-fitted calibration. Worth doing as a
**separate portfolio artifact** — it's a clean ML-research piece on top
of the framework: data ingestion → EDA → distribution fitting → scenario
re-tune → empirical validation.

### Deliverables

1. **Dataset acquisition.** Download the published RBA dataset (Zenodo
   doi:10.5281/zenodo.6782156 or GitHub `das-group/rba-dataset`).
   Snapshot to `scratch/rba/` (gitignored — large file).
2. **EDA notebook** (`scratch/rba-eda.ipynb` or `eval/notebooks/rba-eda.ipynb`):
   - Per-user login-frequency histogram with P25 / P50 / P75 / P90 / P99.
   - Per-user diversity histograms — IPs, ASNs, countries, cities, device
     fingerprints, user-agents.
   - Inter-login-interval distribution (CDF).
   - Time-of-day and day-of-week login aggregates.
   - Outcome-rate breakdown by device class and OS.
   - Attack-vs-legitimate feature distributions (using the dataset's
     attack labels).
3. **Distribution-fitting pass.** Log-normal or power-law fits for
   heavy-tailed counts; categorical priors for user-agent / device / OS;
   mixture models where appropriate. Record fitted parameters.
4. **Scenario re-tune.** Update each `generator/scenarios/*.yaml`'s
   `velocity`, `device`, `network`, `geo`, `auth` blocks to match the
   fitted distributions. Record the diff with before/after numbers.
5. **Documentation update.** Replace each
   *Qualitative-inference parameters* entry above with empirical numbers
   and a section citation pointing to the EDA notebook.
6. **Validation.** Regenerate 50-event runs per scenario; for each
   feature, confirm the generated distribution matches the empirical RBA
   distribution within tolerance via Kolmogorov-Smirnov or chi-square
   test. Record p-values per feature per scenario.
7. **Writeup.** Brief retrospective: which scenario parameters changed
   most? Where did intuition match the data? Where did it not? What did
   the validation tests catch?

### Estimated effort

- 1–2 days for EDA + fitting + scenario re-tune.
- Half a day for validation + writeup.
- Half a day for committing notebook artifacts and updating this
  document.

### Why it's worth doing

Converts the framework's reference implementation into a small but real
ML-research artifact. Showcases data-science engineering practice
end-to-end (download → EDA → fit → apply → validate → document) on a
publicly available dataset. Stands as a portfolio add-on independent of
the framework itself.

---

## Cross-references

- **[`account-takeover.md`](account-takeover.md)** — the ATO reasoner
  architecture that consumes the scenarios.
- **[`reasoner.md`](../index.md)** — abstract reasoner typology.
- **DR-10** — hybrid data strategy (calibration intent and the
  scoping framing this document closes).
