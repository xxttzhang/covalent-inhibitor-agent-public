import numpy as np
import pytest

from covscreen.metrics import LOGAUC_FORMULA_VERSION, ScoreDirectionPending, adjusted_logauc, bedroc, ef_at_percent, roc_auc, sort_results


def test_score_direction_required():
    with pytest.raises(ScoreDirectionPending):
        sort_results([1, 2], [1, 0], "PENDING_CONFIRMATION")


def test_ef_random_and_perfect_shapes():
    labels = np.array([1, 0, 0, 1, 0, 0, 0, 0, 0, 0])
    assert ef_at_percent(labels, 10) == 5.0
    assert ef_at_percent(np.array([1] + [0] * 99), 1) == 100.0


def test_adjusted_logauc_requires_known_formula():
    with pytest.raises(ScoreDirectionPending):
        adjusted_logauc([1, 0], [1, 0], "higher_is_better", "PENDING_CONFIRMATION")


def test_perfect_adjusted_logauc():
    labels = np.array([1, 1] + [0] * 398)
    scores = np.array([2, 1] + list(np.linspace(-1, -398, 398)))
    assert adjusted_logauc(labels, scores, "higher_is_better") == pytest.approx(0.85538, abs=1e-6)


def test_random_adjusted_logauc_is_near_zero():
    rng = np.random.default_rng(20260725)
    labels = np.array([1] * 100 + [0] * 9900)
    scores = rng.random(labels.size)
    value = adjusted_logauc(labels, scores, "higher_is_better")
    assert abs(value) < 0.04


def test_logauc_formula_version_is_frozen():
    assert LOGAUC_FORMULA_VERSION == "dude_logauc_0.001_v1"


def test_bedroc_perfect_and_tail_ordering():
    labels = np.array([1, 1] + [0] * 98)
    scores = np.array([2, 1] + list(np.linspace(-1, -98, 98)))
    assert bedroc(labels, scores, "higher_is_better") == pytest.approx(1.0)
    tail_scores = np.arange(100, dtype=float)
    assert bedroc(labels, tail_scores, "higher_is_better") < 0.01


def test_roc_auc_respects_lower_is_better_direction():
    labels = np.array([1, 1, 0, 0])
    energy_scores = np.array([-10.0, -9.0, -2.0, -1.0])
    assert roc_auc(labels, energy_scores, "lower_is_better") == pytest.approx(1.0)
