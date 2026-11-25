from __future__ import annotations

import math

import numpy as np
from sklearn.metrics import roc_auc_score


class ScoreDirectionPending(RuntimeError):
    pass


def sort_results(scores, labels, direction: str):
    if direction not in {"higher_is_better", "lower_is_better"}:
        raise ScoreDirectionPending("score direction is PENDING_CONFIRMATION")
    scores = np.asarray(scores, dtype=float)
    labels = np.asarray(labels, dtype=int)
    order = np.argsort(-scores if direction == "higher_is_better" else scores, kind="mergesort")
    return scores[order], labels[order], order


def ef_at_percent(labels, percent: float) -> float:
    labels = np.asarray(labels, dtype=int)
    n = len(labels)
    total = labels.sum()
    if n == 0 or total == 0:
        return float("nan")
    k = max(1, math.ceil(n * percent / 100.0))
    return float(labels[:k].sum() / k / (total / n))


def roc_auc(labels, scores, direction: str | None = None) -> float:
    """Return ROC-AUC after orienting energy-like scores when required."""
    values = np.asarray(scores, dtype=float)
    if direction == "lower_is_better":
        values = -values
    elif direction not in {None, "higher_is_better"}:
        raise ScoreDirectionPending("score direction is PENDING_CONFIRMATION")
    return float(roc_auc_score(labels, values))


LOGAUC_FORMULA_VERSION = "dude_logauc_0.001_v1"


def _rank_curve(labels, scores, direction: str) -> tuple[np.ndarray, np.ndarray]:
    """Return the empirical ROC curve in ranking order, including (0, 0)."""
    scores_sorted, labels_sorted, _ = sort_results(scores, labels, direction)
    y = np.concatenate(([0.0], np.cumsum(labels_sorted) / max(1, labels_sorted.sum())))
    negatives = 1 - labels_sorted
    x = np.concatenate(([0.0], np.cumsum(negatives) / max(1, negatives.sum())))
    return x.astype(float), y.astype(float)


def logauc(labels, scores, direction: str, xmin: float = 0.001) -> float:
    """Calculate the DUD-E/COValid semilog ROC area as a fraction.

    The published definition integrates the ROC curve from ``xmin`` to 1 using
    trapezoids in log10(false-positive-rate) space.  The curve is linearly
    interpolated at ``xmin`` when the finite ranking has no point exactly there.
    """
    if not 0 < xmin < 1:
        raise ValueError("xmin must be between 0 and 1")
    labels = np.asarray(labels, dtype=int)
    if labels.size == 0 or labels.sum() == 0 or labels.sum() == labels.size:
        return float("nan")
    x, y = _rank_curve(labels, scores, direction)
    keep = x > 0
    x, y = x[keep], y[keep]
    if x[0] > xmin:
        # A finite library usually starts at 1/N_decoy.  Extend the first
        # empirical segment to the fixed benchmark lower limit.
        y_at_min = y[0]
        x = np.concatenate(([xmin], x))
        y = np.concatenate(([y_at_min], y))
    else:
        y_at_min = float(np.interp(xmin, x, y))
        keep = x >= xmin
        x, y = x[keep], y[keep]
        if x.size == 0 or x[0] > xmin:
            x = np.concatenate(([xmin], x))
            y = np.concatenate(([y_at_min], y))
        else:
            x[0], y[0] = xmin, y_at_min
    if x[-1] < 1.0:
        x = np.concatenate((x, [1.0]))
        y = np.concatenate((y, [1.0]))
    log_x = np.log10(x)
    integrate = np.trapezoid if hasattr(np, "trapezoid") else np.trapz
    area = float(integrate(y, log_x) / math.log10(1.0 / xmin))
    return max(0.0, min(1.0, area))


def adjusted_logauc(labels, scores, direction: str, formula_version: str = LOGAUC_FORMULA_VERSION) -> float:
    """Return adjusted LogAUC as a fraction (0.85538 is the perfect limit).

    Formula frozen from the published DUD-E/COValid convention:
    ``adjusted LogAUC = LogAUC_0.001 - 0.14462``.  Reports may multiply this
    value by 100 to display percentage units.
    """
    if formula_version != LOGAUC_FORMULA_VERSION:
        raise ScoreDirectionPending(f"unknown adjusted LogAUC formula version: {formula_version}")
    return logauc(labels, scores, direction) - 0.14462


def top_active_count(labels, n: int) -> int:
    labels = np.asarray(labels, dtype=int)
    return int(labels[: max(0, n)].sum())


def bedroc(labels, scores, direction: str, alpha: float = 20.0) -> float:
    """Calculate BEDROC(alpha), normalized to [0, 1]."""
    if alpha <= 0:
        raise ValueError("alpha must be positive")
    _, labels_sorted, _ = sort_results(scores, labels, direction)
    labels_sorted = np.asarray(labels_sorted, dtype=int)
    n_total = float(labels_sorted.size)
    n_active = float(labels_sorted.sum())
    if n_total == 0 or n_active == 0 or n_active == n_total:
        return float("nan")
    ra = n_active / n_total
    ranks = np.flatnonzero(labels_sorted == 1) + 1.0
    weighted_sum = float(np.exp(-alpha * ranks / n_total).sum())
    factor1 = ra * math.sinh(alpha / 2.0) / (math.cosh(alpha / 2.0) - math.cosh(alpha / 2.0 - alpha * ra))
    factor2 = (math.exp(alpha / n_total) - 1.0) / (ra * (1.0 - math.exp(-alpha)))
    constant = 1.0 / (1.0 - math.exp(alpha * (1.0 - ra)))
    return float(max(0.0, min(1.0, weighted_sum * factor1 * factor2 + constant)))


def summarize(labels, scores, direction: str, adjusted_logauc_formula: str = LOGAUC_FORMULA_VERSION) -> dict[str, float | str]:
    scores_sorted, labels_sorted, _ = sort_results(scores, labels, direction)
    report: dict[str, float | str] = {
        "roc_auc": roc_auc(np.asarray(labels), np.asarray(scores), direction),
        "ef1_percent": ef_at_percent(labels_sorted, 1.0),
        "ef5_percent": ef_at_percent(labels_sorted, 5.0),
        "bedroc_alpha20": bedroc(labels, scores, direction, alpha=20.0),
        "top10_active": top_active_count(labels_sorted, 10),
        "top25_active": top_active_count(labels_sorted, 25),
        "top50_active": top_active_count(labels_sorted, 50),
        "score_direction": direction,
    }
    try:
        report["adjusted_logauc"] = adjusted_logauc(labels, scores, direction, adjusted_logauc_formula)
        report["adjusted_logauc_percent"] = float(report["adjusted_logauc"]) * 100.0
    except (ScoreDirectionPending, NotImplementedError) as exc:
        report["adjusted_logauc"] = float("nan")
        report["adjusted_logauc_status"] = str(exc)
    return report
