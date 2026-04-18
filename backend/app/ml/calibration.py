"""Probability calibration via temperature scaling.

A model's *ranking* of outcomes can be correct while its *probabilities* are
mis-scaled. Dixon-Coles in particular tends to be overconfident on heavy
favourites — the backtest's calibration table showed predicted 0.84 actually
hitting 0.77, predicted 0.93 actually hitting 0.67. The fix is post-hoc:
take a held-out window of (raw_probs, observed_outcome) pairs, learn one
scalar `T`, and reshape every future prediction through it.

Why temperature scaling specifically (vs Platt, isotonic, beta calibration)
- One parameter, so it can't overfit on the small windows we get during a
  walk-forward backtest (~100-500 examples).
- Preserves rank ordering: argmax(p) is unchanged, so a bet that the raw
  model wanted to take is still attractive after calibration if it had real
  edge to begin with — it just gets re-priced.
- Preserves sum-to-one for any K, so we can use the same code for 1X2
  (K=3) and OU 2.5 (K=2) and any future market.
- Strictly causal in our backtest because we fit on a window of *past*
  predictions whose outcomes are already known.

Math
----
Treat the model's output probability vector as a softmax with implicit
"logits" `log p`. A temperature `T > 0` rescales those logits:

    p_calibrated[k] = exp(log p[k] / T) / sum_j exp(log p[j] / T)
                    = p[k]^(1/T) / sum_j p[j]^(1/T)

T = 1 is the identity. T > 1 *softens* (pulls probabilities toward uniform —
the right move when the model is overconfident). T < 1 *sharpens*. We fit T
by minimizing negative log-likelihood on the held-out window.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize_scalar

_EPS = 1e-12


def _softmax_with_temp(log_probs: np.ndarray, T: float) -> np.ndarray:
    """Softmax of (log_probs / T), computed in log-space for stability.

    log_probs shape: (..., K). Returns probs of the same shape.
    """
    scaled = log_probs / T
    m = scaled.max(axis=-1, keepdims=True)
    e = np.exp(scaled - m)
    return e / e.sum(axis=-1, keepdims=True)


@dataclass
class TemperatureScaler:
    """One scalar temperature shared across all classes.

    Use one scaler per market: fit it on a window of (raw_probs, label)
    pairs, then call `apply()` on every future prediction.
    """

    T: float = 1.0
    fitted: bool = False
    n_fit: int = 0

    def fit(self, probs: np.ndarray, labels: np.ndarray) -> "TemperatureScaler":
        """Fit T to minimize NLL on (probs, labels).

        Parameters
        ----------
        probs : (N, K) array of class probabilities — rows must sum to 1.
        labels : (N,) integer array of class indices in [0, K).

        Returns self for chaining.
        """
        probs = np.asarray(probs, dtype=float)
        labels = np.asarray(labels, dtype=int)
        if probs.ndim != 2:
            raise ValueError(f"probs must be 2-D (N, K), got shape {probs.shape}")
        if probs.shape[0] != labels.shape[0]:
            raise ValueError(
                f"probs and labels length mismatch: {probs.shape[0]} vs {labels.shape[0]}"
            )
        if probs.shape[0] == 0:
            return self

        log_probs = np.log(np.clip(probs, _EPS, 1.0))
        N = probs.shape[0]
        idx = np.arange(N)

        def nll(T: float) -> float:
            scaled = log_probs / T
            m = scaled.max(axis=1, keepdims=True)
            log_norm = m.squeeze(axis=1) + np.log(np.exp(scaled - m).sum(axis=1))
            log_p_true = scaled[idx, labels] - log_norm
            return float(-log_p_true.mean())

        # Bounded search: T in [0.05, 20.0] is plenty for any sane mis-calibration.
        res = minimize_scalar(nll, bounds=(0.05, 20.0), method="bounded")
        self.T = float(res.x)
        self.fitted = True
        self.n_fit = N
        return self

    def apply(self, probs: np.ndarray) -> np.ndarray:
        """Reshape `probs` through the learned temperature.

        Accepts (K,) or (N, K). Returns the same shape. If the scaler hasn't
        been fitted (or T == 1.0 within tolerance) the input is returned
        unchanged so this is safe to call unconditionally.
        """
        if not self.fitted or abs(self.T - 1.0) < 1e-9:
            return probs
        probs = np.asarray(probs, dtype=float)
        log_probs = np.log(np.clip(probs, _EPS, 1.0))
        return _softmax_with_temp(log_probs, self.T)
