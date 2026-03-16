"""BODMAS arithmetic problems for GEPA prompt optimization.

Train/val split with 4-digit numbers and 10–15 evaluations. Order of operations
(BODMAS) is easy to get wrong; a good system prompt improves accuracy.
"""

from dataclasses import dataclass


@dataclass
class Problem:
    """Single arithmetic problem: expression and expected numeric answer."""

    expr: str
    answer: float
    id: str = ""  # optional id for logging

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self.expr


# Train: 12 expressions, each with 10+ operations, 4-digit numbers, BODMAS
TRAIN = [
    Problem("1823 + 2046 * 2 - 4012 / 2 + 1500 - 800 / 4 + 612 * 2 - 400 / 2 + 319", 6552.0),
    Problem("5000 - 1234 * 2 + 3600 / 3 - 500 + 2000 - 800 / 8 + 456 * 2 - 3000 / 5 + 100", 5544.0),
    Problem("2100 + 1500 / 3 - 200 * 4 + 3200 / 4 - 600 + 900 * 2 - 4000 / 5 + 1234 - 500 * 2 + 100", 3334.0),
    Problem("3012 / 2 + 800 * 3 - 1600 / 4 + 567 - 200 * 2 + 4500 / 5 - 300 + 1200 * 1 - 418", 5055.0),
    Problem("4000 - 1000 * 2 + 2400 / 4 - 300 + 612 * 2 + 1800 / 3 - 500 * 2 + 234 - 1000 / 2", 2858.0),
    Problem("1234 + 2000 / 4 - 300 * 3 + 4500 / 5 + 800 - 1200 / 2 + 319 * 2 - 400 + 1500 / 3", 2672.0),
    Problem("5678 - 2000 / 5 + 400 * 2 - 3600 / 6 + 1000 - 800 * 1 + 2100 / 3 - 456 + 200 * 2", 6322.0),
    Problem("2800 / 4 + 1234 - 500 * 2 + 3000 / 5 - 200 + 612 * 2 + 800 / 4 - 3000 / 6 + 419", 2677.0),
    Problem("1000 * 2 - 1500 / 3 + 2400 / 4 - 200 * 3 + 800 + 3600 / 6 - 500 + 234 * 2 - 400 / 2", 2668.0),
    Problem("4500 / 5 + 800 * 2 - 2000 / 4 + 1234 - 300 * 2 + 1500 / 3 - 600 + 912 / 2 - 100 + 50", 2940.0),
    Problem("6000 - 1200 * 2 + 2800 / 4 - 500 + 400 * 3 - 3600 / 6 + 567 - 200 * 2 + 1000 / 5 + 213", 4980.0),
    Problem("3200 / 4 + 1456 - 600 * 2 + 2500 / 5 - 300 + 800 * 2 - 4000 / 8 + 319 - 500 + 200 * 1", 2375.0),
]

# Val: 4 held-out, each 10+ operations
VAL = [
    Problem("3525 / 5 + 307 - 200 * 2 + 4000 / 4 - 500 + 612 * 2 - 800 / 8 + 100 - 300 + 1500 / 3", 2536.0),
    Problem("9018 / 9 - 512 + 1000 * 2 - 2400 / 4 + 300 - 400 * 2 + 1800 / 3 - 200 + 456 - 1000 / 2", 1746.0),
    Problem("1234 + 876 * 2 - 2000 / 5 + 500 - 300 * 2 + 3600 / 6 - 800 + 419 * 2 - 600 / 3 + 100", 3024.0),
    Problem("6012 - 1512 / 3 + 400 * 2 - 3200 / 4 + 1000 - 500 * 2 + 2100 / 7 - 300 + 234 * 2 - 200", 5776.0),
]


def get_train_val():
    """Return (train_list, val_list) for use as dataset and valset."""
    return TRAIN, VAL
