"""Trial objective. Crashes never become a numeric score."""

from __future__ import annotations

import statistics

# Held-out numbers never enter this function. Callers pass training scores only.
SELECTION_RULE = (
    "Winner = highest mean score on the held-out datasets among the validation "
    "set. Tie-break: higher training objective, then lexicographically smaller "
    "config SHA-256. The validation set is the top N completed training trials "
    "plus the baseline trial when that trial completed and was not already included. "
    "Held-out scores are not passed to Optuna. If the study has no held-out "
    "datasets, the winner is the completed trial with the highest training "
    "objective, with the same SHA-256 tie-break. A missing or failed score is "
    "not a zero."
)


def objective_value(scores: list[float], penalty: float) -> float:
    """Mean training score, minus an optional population-stdev penalty.

    One training dataset has no stdev term. The penalty is zero unless the
    experiment set ``variability_penalty``.
    """
    if not scores:
        raise ValueError("objective needs at least one score")
    mean = statistics.fmean(scores)
    if penalty == 0 or len(scores) < 2:
        return float(mean)
    return float(mean - penalty * statistics.pstdev(scores))
