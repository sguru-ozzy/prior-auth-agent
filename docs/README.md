# Design and decisions

The [design document](https://docs.google.com/document/d/1S0ZwLwwnvf1CUEN2kFZnqWK1KuNc32psnmL2j5aATPA)
describes the overall approach. The [repository README](../README.md) covers setup and common commands;
the [testing guide](testing.md) covers additional CLI cases.

## PR decision records

- [PR 1: Reproducible foundation](decisions/001-repository-foundation.md)
- [PR 2: Validation and results](decisions/002-validation-and-results.md)
- [PR 3: Guarded browser inspection](decisions/003-guarded-browser-inspection.md)

Records for PRs 1–3 were written retrospectively from their merged changes and validation evidence.
They describe those milestones; the source and configuration show current behavior.

Each PR adds a short record under `decisions/`, using the [template](decisions/template.md).
Capture the decision, reason, tradeoff, verification and next step. Keep commands in the testing guide and link
to configuration rather than copying it. Later decisions should reference the earlier record they change.
