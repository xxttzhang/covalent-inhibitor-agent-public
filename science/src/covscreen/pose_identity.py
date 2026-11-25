"""Exact structure-record identities shared with vendor Python runtimes."""
from decimal import Decimal, InvalidOperation


def exact_pose_index(value) -> int:
    """Preserve a zero-based identity across Python, JSON and JS consumers."""
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("CovDock pose index must be an exact non-negative integer") from exc
    # JSON consumers use JavaScript numbers; reject identities they cannot retain.
    if (not number.is_finite() or number < 0 or number > 2**53 - 1
            or number != number.to_integral_value()):
        raise ValueError("CovDock pose index must be an exact non-negative safe integer")
    return int(number)
