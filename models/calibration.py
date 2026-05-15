"""
Platt scaling — logistic recalibration of model probabilities.

Trains a 2-parameter logistic that maps the model's raw probability to a
calibrated probability:

    p_cal = sigmoid(a * logit(p_raw) + b)

When (a, b) = (1, 0) the function is identity (no recalibration). The fit
uses maximum-likelihood gradient descent against historical (predicted,
actual outcome) pairs from the prediction log.

Persists the fit to predictions/calibration.json so the dashboard can
load and apply it on every render.
"""

import json
import math
import os
import urllib.request
import ssl as _ssl_compat
_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
from datetime import datetime, date, timedelta


PRED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "predictions")
CAL_PATH = os.path.join(PRED_DIR, "calibration.json")
EPS = 1e-6


def logit(p: float) -> float:
    p = max(EPS, min(1 - EPS, p))
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def apply_platt(p: float, params: dict | None) -> float:
    """Apply (a, b) recalibration. If params is None, returns p unchanged."""
    if not params or "a" not in params or "b" not in params:
        return p
    return sigmoid(params["a"] * logit(p) + params["b"])


def apply_calibration(p: float, params: dict | None) -> float:
    """
    Apply whichever calibration is in params:
      - if 'isotonic_xs'/'isotonic_ys': linear-interp via the isotonic mapping
      - else if 'a'/'b': Platt scaling
      - else: identity
    """
    if not params:
        return p
    xs = params.get("isotonic_xs")
    ys = params.get("isotonic_ys")
    if xs and ys and len(xs) == len(ys) and len(xs) >= 2:
        return _interp(p, xs, ys)
    return apply_platt(p, params)


def _interp(x: float, xs: list, ys: list) -> float:
    """Linear interpolation. xs must be non-decreasing."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    # Binary search would be cleaner; xs is small so linear scan is fine.
    for i in range(1, len(xs)):
        if x <= xs[i]:
            x0, x1 = xs[i - 1], xs[i]
            y0, y1 = ys[i - 1], ys[i]
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def fit_isotonic(pairs: list[tuple]) -> dict:
    """
    Pool-Adjacent-Violators (PAV) algorithm for isotonic regression.
    Maps p_raw → empirical actual rate, monotonically non-decreasing.

    Returns dict with isotonic_xs and isotonic_ys (anchor points), plus
    log-loss before/after for comparison.
    """
    if not pairs:
        return {"isotonic_xs": [], "isotonic_ys": [], "n": 0,
                "log_loss_before": None, "log_loss_after": None}

    # Sort by predicted probability
    sorted_pairs = sorted(pairs, key=lambda r: r[0])
    xs = [p for p, _ in sorted_pairs]
    ys = [float(y) for _, y in sorted_pairs]
    n = len(xs)

    # PAV: maintain blocks of (sum_y, count); merge while non-monotone
    block_sum = ys[:]   # sum of y in block
    block_cnt = [1] * n
    block_x   = xs[:]   # representative x = mean of x in block
    starts    = list(range(n))

    i = 1
    while i < len(block_sum):
        # block i mean vs block i-1 mean
        if (block_sum[i] / block_cnt[i]) < (block_sum[i - 1] / block_cnt[i - 1]):
            # merge i-1 and i
            block_sum[i - 1] += block_sum[i]
            block_cnt[i - 1] += block_cnt[i]
            # x of merged block = weighted mean (use cumulative averaging)
            block_x[i - 1] = (block_x[i - 1] * (block_cnt[i - 1] - block_cnt[i])
                              + block_x[i] * block_cnt[i]) / block_cnt[i - 1]
            del block_sum[i]
            del block_cnt[i]
            del block_x[i]
            if i > 1:
                i -= 1
        else:
            i += 1

    iso_xs = block_x
    iso_ys = [s / c for s, c in zip(block_sum, block_cnt)]

    # Bookend with (0, ys[0]) and (1, ys[-1]) so apply_calibration extrapolates safely.
    if iso_xs[0] > 0.0:
        iso_xs = [0.0] + iso_xs
        iso_ys = [iso_ys[0]] + iso_ys
    if iso_xs[-1] < 1.0:
        iso_xs = iso_xs + [1.0]
        iso_ys = iso_ys + [iso_ys[-1]]

    # Compute log loss before/after
    def nll(map_fn):
        loss = 0.0
        for p, y in pairs:
            pc = max(EPS, min(1 - EPS, map_fn(p)))
            loss += -(y * math.log(pc) + (1 - y) * math.log(1 - pc))
        return loss / len(pairs)

    nll_before = nll(lambda p: p)
    nll_after = nll(lambda p: _interp(p, iso_xs, iso_ys))

    return {
        "isotonic_xs": iso_xs,
        "isotonic_ys": iso_ys,
        "n": n,
        "log_loss_before": nll_before,
        "log_loss_after":  nll_after,
        "fit_at": datetime.now().isoformat(),
        "method": "isotonic",
    }


def fit_platt(pairs: list[tuple], lr: float = 0.05, n_iter: int = 5000,
              l2: float = 0.01) -> dict:
    """
    Fit (a, b) via gradient descent on logistic NLL.
    pairs: list of (p_raw, y) where y ∈ {0, 1}.
    L2 prior pulls weakly toward the identity transform (1, 0).

    Returns dict {a, b, n, log_loss_before, log_loss_after}.
    """
    if not pairs:
        return {"a": 1.0, "b": 0.0, "n": 0,
                "log_loss_before": None, "log_loss_after": None}

    a, b = 1.0, 0.0
    n = len(pairs)

    def nll(a_, b_):
        loss = 0.0
        for p, y in pairs:
            z = a_ * logit(p) + b_
            pc = sigmoid(z)
            pc = max(EPS, min(1 - EPS, pc))
            loss += -(y * math.log(pc) + (1 - y) * math.log(1 - pc))
        return loss / n

    nll_before = nll(1.0, 0.0)

    for _ in range(n_iter):
        grad_a = 0.0
        grad_b = 0.0
        for p, y in pairs:
            x = logit(p)
            pc = sigmoid(a * x + b)
            err = pc - y
            grad_a += err * x
            grad_b += err
        grad_a /= n
        grad_b /= n
        # L2 toward identity
        grad_a += l2 * (a - 1.0)
        grad_b += l2 * b
        a -= lr * grad_a
        b -= lr * grad_b

    nll_after = nll(a, b)

    return {
        "a": a, "b": b, "n": n,
        "log_loss_before": nll_before,
        "log_loss_after":  nll_after,
        "fit_at": datetime.now().isoformat(),
    }


# ---------- Persistence ----------

def save_params(params: dict, path: str = CAL_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(params, f, indent=2, default=str)
    return path


def load_params(path: str = CAL_PATH) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------- Historical-pair builder ----------

def _fetch_hr_outcomes(game_pk: int, cache: dict) -> set | None:
    """Returns set of batter_ids who hit ≥1 HR in this game. None on error."""
    if game_pk in cache:
        return cache[game_pk]
    url = f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=15, context=_UNVERIFIED_SSL).read())
    except Exception:
        cache[game_pk] = None
        return None
    hrs = set()
    for side in ("away", "home"):
        for _, pdata in data.get("teams", {}).get(side, {}).get("players", {}).items():
            stats = pdata.get("stats", {}).get("batting", {})
            if (stats.get("homeRuns", 0) or 0) > 0:
                hrs.add(pdata["person"]["id"])
    cache[game_pk] = hrs
    return hrs


def build_pairs_from_logs(pred_dir: str = PRED_DIR,
                          skip_today: bool = True) -> list[tuple]:
    """
    Walks the predictions/*.jsonl log directory, fetches actual HR outcomes
    for every game, and returns a list of (p_raw, y) pairs.
    """
    pairs = []
    cache: dict = {}
    today_str = date.today().strftime("%Y-%m-%d")
    files = sorted(f for f in os.listdir(pred_dir) if f.endswith(".jsonl"))
    for fn in files:
        date_str = fn[:-6]
        if skip_today and date_str >= today_str:
            continue
        path = os.path.join(pred_dir, fn)
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                except json.JSONDecodeError:
                    continue
                gpk = p.get("game_pk")
                bid = p.get("batter_id")
                pred = p.get("p_at_least_one")
                if not gpk or not bid or pred is None:
                    continue
                hrs = _fetch_hr_outcomes(gpk, cache)
                if hrs is None:
                    continue
                y = 1 if bid in hrs else 0
                pairs.append((float(pred), y))
    return pairs


def fit_from_logs(pred_dir: str = PRED_DIR) -> dict:
    """
    Fits both Platt scaling and isotonic regression on the historical
    prediction log, picks whichever has the lower log loss, saves it to
    disk, and returns the chosen params.
    """
    pairs = build_pairs_from_logs(pred_dir)
    if not pairs:
        return {}

    platt = fit_platt(pairs)
    iso = fit_isotonic(pairs)

    # Pick whichever has lower log loss after recalibration
    if iso.get("log_loss_after") is not None and platt.get("log_loss_after") is not None:
        chosen = iso if iso["log_loss_after"] < platt["log_loss_after"] else platt
    else:
        chosen = iso if iso.get("log_loss_after") is not None else platt

    chosen["candidates"] = {
        "platt":    {"a": platt.get("a"), "b": platt.get("b"),
                     "log_loss": platt.get("log_loss_after")},
        "isotonic": {"log_loss": iso.get("log_loss_after"),
                     "n_anchors": len(iso.get("isotonic_xs", []))},
        "baseline_log_loss": platt.get("log_loss_before"),
    }
    save_params(chosen)
    return chosen


if __name__ == "__main__":
    print("Fitting calibration from prediction logs…")
    print(f"  pred dir: {PRED_DIR}")
    pairs = build_pairs_from_logs()
    print(f"  pairs:    {len(pairs)}")
    if not pairs:
        print("  No pairs found.")
        raise SystemExit(1)

    avg_pred = sum(p for p, _ in pairs) / len(pairs)
    avg_actual = sum(y for _, y in pairs) / len(pairs)
    print(f"  avg pred:   {avg_pred*100:.2f}%")
    print(f"  avg actual: {avg_actual*100:.2f}%")

    platt = fit_platt(pairs)
    iso = fit_isotonic(pairs)
    print(f"\nPlatt:    log loss {platt['log_loss_after']:.4f}  (a={platt['a']:.3f}, b={platt['b']:.3f})")
    print(f"Isotonic: log loss {iso['log_loss_after']:.4f}  ({len(iso['isotonic_xs'])} anchors)")
    print(f"Baseline: log loss {platt['log_loss_before']:.4f}")

    chosen = iso if iso["log_loss_after"] < platt["log_loss_after"] else platt
    method = "isotonic" if "isotonic_xs" in chosen else "platt"
    save_params(chosen)
    improvement = (platt["log_loss_before"] - chosen["log_loss_after"]) / platt["log_loss_before"] * 100
    print(f"\nWinner: {method} ({improvement:+.2f}% log loss improvement)")
    print(f"Saved → {CAL_PATH}")

    print(f"\nRemap on sample predictions:")
    for raw in [0.05, 0.10, 0.15, 0.17, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60]:
        cal = apply_calibration(raw, chosen)
        delta = (cal - raw) * 100
        print(f"  {raw*100:5.1f}%  →  {cal*100:5.1f}%   ({delta:+.1f}pp)")