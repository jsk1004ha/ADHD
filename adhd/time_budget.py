"""Shared validation for opt-in wall-time limits."""

import math


class TimeBudgetExpired(RuntimeError):
    """An owned process was not launched because its wall budget expired."""


def validate_seconds(value, name, *, maximum=86400, allow_zero=False) -> float:
    """Accept only finite, bounded numeric seconds; permit short test budgets."""
    if type(value) not in (int, float):
        raise ValueError(name + ' must be a finite number of seconds')
    try:
        seconds = float(value)
    except OverflowError as error:
        raise ValueError(name + ' must be a finite number of seconds') from error
    if not math.isfinite(seconds) or seconds < 0 or (seconds == 0 and not allow_zero) or seconds > maximum:
        raise ValueError(name + ' must be within the allowed seconds range')
    return seconds
