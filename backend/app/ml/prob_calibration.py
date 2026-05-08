"""Per-market probability calibration via isotonic regression.

Why this exists
---------------
Production track record (n=36 settled legs through 2026-05-08) showed
the composer's raw model probabilities are systematically miscalibrated:

    model says 65% → actual hit rate 25%   (40-point overconfidence)
    model says 95% → actual hit rate 50%   (45-point overconfidence)

This isn't a Dixon-Coles bug — DC fits goal expectations, not market
probabilities. The miscalibration emerges when we collapse the joint
score matrix into market payouts (1X2, OU lines, BTTS). Different
markets distort the underlying Poisson differently, so we need a
*per-market* calibrator, not one global temperature.

Approach
--------
Isotonic regression on historical (raw_prob, hit) pairs, fit separately
per base market. Isotonic preserves rank order while bending the
probability axis to match observed frequencies — no parametric form to
get wrong, no overfit on small samples (the constraint of monotonicity
is the regularizer).

Each base market gets its own fit:
    1X2 / double_chance / over_under_2.5 / over_under_3.5 / btts / etc.

A market with too few historical samples (< MIN_SAMPLES) falls back to
the identity function — better to use raw probs than to overfit a
calibrator on 5 picks.

Output
------
A simple JSON blob with one ascending (raw, calibrated) pair list per
market. Composer loads it on every request via a module-level cache.
The fit script writes it; the API reads it. No SQL, no cross-process
state — just a file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


logger = logging.getLogger(__name__)


# Below this many samples we don't trust the calibrator and fall back
# to identity. Picked conservatively — isotonic is non-parametric so
# even 30 samples gives a usable curve, but we want at least one full
# matchday's worth of independent picks per market to dampen variance.
MIN_SAMPLES = 30

CALIBRATION_PATH = Path("calibration_curves.json")


@dataclass
class IsotonicCurve:
    """A monotone non-decreasing piecewise-constant calibration curve.

    Stored as two parallel arrays: `xs` are the raw probabilities at the
    breakpoints, `ys` are the calibrated outputs. We do linear
    interpolation between breakpoints when applying — sklearn's
    IsotonicRegression also does this internally.
    """

    xs: list[float] = field(default_factory=list)
    ys: list[float] = field(default_factory=list)
    n_train: int = 0

    def is_trained(self) -> bool:
        return len(self.xs) >= 2 and self.n_train >= MIN_SAMPLES

    def apply(self, raw_prob: float) -> float:
        """Map a raw probability through the curve. Identity fallback
        when the curve isn't trained."""
        if not self.is_trained():
            return raw_prob
        # Clamp to the training range. Clipping rather than extrapolating
        # is the safe choice — extrapolating an isotonic fit outside its
        # support is meaningless.
        if raw_prob <= self.xs[0]:
            return self.ys[0]
        if raw_prob >= self.xs[-1]:
            return self.ys[-1]
        # Linear interpolation between the two surrounding breakpoints.
        # We could binary-search for speed but n_breakpoints is tiny
        # (≤ unique input values) so a linear scan is fine.
        for i in range(1, len(self.xs)):
            if raw_prob <= self.xs[i]:
                x0, x1 = self.xs[i - 1], self.xs[i]
                y0, y1 = self.ys[i - 1], self.ys[i]
                if x1 == x0:
                    return y0
                t = (raw_prob - x0) / (x1 - x0)
                return y0 + t * (y1 - y0)
        return self.ys[-1]


@dataclass
class CalibrationBundle:
    """Per-market calibration curves loaded from disk."""

    curves: dict[str, IsotonicCurve] = field(default_factory=dict)
    fitted_at: str | None = None
    sample_size: int = 0

    def apply(self, base_market: str, raw_prob: float) -> float:
        curve = self.curves.get(base_market)
        if curve is None:
            return raw_prob
        return curve.apply(raw_prob)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fitted_at": self.fitted_at,
            "sample_size": self.sample_size,
            "curves": {
                m: {"xs": c.xs, "ys": c.ys, "n_train": c.n_train}
                for m, c in self.curves.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CalibrationBundle":
        curves = {}
        for m, c in (payload.get("curves") or {}).items():
            curves[m] = IsotonicCurve(
                xs=list(c.get("xs") or []),
                ys=list(c.get("ys") or []),
                n_train=int(c.get("n_train") or 0),
            )
        return cls(
            curves=curves,
            fitted_at=payload.get("fitted_at"),
            sample_size=int(payload.get("sample_size") or 0),
        )


# ---- fit -----------------------------------------------------------------


def fit_curve(probs: list[float], hits: list[bool]) -> IsotonicCurve:
    """Fit one isotonic curve. Returns identity (untrained) when there
    aren't enough samples."""
    if len(probs) < MIN_SAMPLES:
        return IsotonicCurve(n_train=len(probs))
    try:
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        logger.warning("sklearn missing — calibration will be identity")
        return IsotonicCurve(n_train=len(probs))

    x = np.asarray(probs, dtype=float)
    y = np.asarray([1 if h else 0 for h in hits], dtype=float)

    # out_of_bounds='clip' so apply() outside training range returns
    # the boundary y values rather than raising.
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(x, y)

    # Sample the fitted curve at the unique input values to get a
    # serializable breakpoint list. We sort by x so apply()'s scan is
    # left-to-right.
    unique_x = sorted(set(x.tolist()))
    breakpoints_x = unique_x
    breakpoints_y = [float(iso.predict([px])[0]) for px in breakpoints_x]
    return IsotonicCurve(
        xs=breakpoints_x,
        ys=breakpoints_y,
        n_train=len(probs),
    )


def _base_market(market: str) -> str:
    """Map our composer's market label to a calibration bucket. We
    deliberately keep OU lines separate (over_under_2.5 ≠ over_under_3.5)
    since their calibration profiles are very different — the model's
    confidence on a 0.5 line is structurally different from on a 4.5
    line."""
    if market.startswith("over_under_"):
        return market
    if market.startswith("OU_"):
        return f"over_under_{market[3:]}"
    return market


# ---- I/O -----------------------------------------------------------------


_BUNDLE_CACHE: CalibrationBundle | None = None


def load_bundle(path: Path = CALIBRATION_PATH) -> CalibrationBundle:
    """Load + cache the on-disk bundle. Returns an empty bundle (every
    apply returns identity) when the file is missing or unreadable —
    composer keeps working, just uncalibrated."""
    global _BUNDLE_CACHE
    if _BUNDLE_CACHE is not None:
        return _BUNDLE_CACHE
    try:
        if not path.exists():
            _BUNDLE_CACHE = CalibrationBundle()
            return _BUNDLE_CACHE
        payload = json.loads(path.read_text())
        _BUNDLE_CACHE = CalibrationBundle.from_dict(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("calibration load failed: %s — using identity", exc)
        _BUNDLE_CACHE = CalibrationBundle()
    return _BUNDLE_CACHE


def reload_bundle(path: Path = CALIBRATION_PATH) -> CalibrationBundle:
    """Force a fresh read — used by tests + the fit script after writing."""
    global _BUNDLE_CACHE
    _BUNDLE_CACHE = None
    return load_bundle(path)


def save_bundle(bundle: CalibrationBundle, path: Path = CALIBRATION_PATH) -> None:
    path.write_text(json.dumps(bundle.to_dict(), indent=2))


def calibrate_prob(market: str, raw_prob: float) -> float:
    """Module-level convenience used by the composer."""
    bundle = load_bundle()
    return bundle.apply(_base_market(market), raw_prob)
