"""
EV Tree Scorer — XGBoost + SHAP replacement for additive percentile scoring.

Plugs into ev_grinder.py after the dedup step. Same input (deduped survivors
with values arrays), same output contract (quality_score, setup_score,
market_score, predicted_wr, predicted_mfe, ev per signal).

Internally:
  - Two XGBoost models: binary classifier (WR) + regressor (MFE on winners)
  - 5-fold stratified CV for honest out-of-sample estimates
  - SHAP values for per-signal, per-feature contribution decomposition
  - Category-balanced SHAP aggregation for setup_score / market_score

Usage:
    from ev_tree_scorer import tree_score_signals

    results, model_info = tree_score_signals(
        deduped_survivors, all_signals, label="pre",
        top_n_features=200, assumed_stop_adr=1.0)
"""

import time
import numpy as np
import xgboost as xgb
import shap
from sklearn.model_selection import StratifiedKFold
from collections import defaultdict


def _build_feature_matrix(deduped_survivors, n_signals, top_n=200):
    """Build raw feature matrix from deduped survivors.

    Selects top N features by screening strength (same metric used in dedup:
    max of wr_spread and mfe_spread/10). Returns matrix + metadata.

    Args:
        deduped_survivors: list of dicts with 'values' arrays from dedup step.
        n_signals: int, number of signals.
        top_n: int, max features to feed into tree model.

    Returns:
        (X, feature_meta, selected_indices)
        X: np.ndarray (n_signals, n_selected) — raw values, NaN preserved.
        feature_meta: list of dicts (name, source, instrument, expression, etc.)
        selected_indices: list of ints — indices into deduped_survivors.
    """
    # Rank by screening strength, take top N
    strengths = []
    for i, s in enumerate(deduped_survivors):
        strength = max(s.get("wr_spread", 0), s.get("mfe_spread", 0) / 10.0)
        strengths.append((strength, i))
    strengths.sort(reverse=True)

    selected = [idx for _, idx in strengths[:top_n]]
    selected.sort()  # preserve original order for reproducibility

    n_feat = len(selected)
    X = np.full((n_signals, n_feat), np.nan, dtype=np.float64)

    feature_meta = []
    for col, surv_idx in enumerate(selected):
        s = deduped_survivors[surv_idx]
        vals = s.get("values")
        if vals is not None:
            v = np.asarray(vals, dtype=np.float64)
            if len(v) == n_signals:
                X[:, col] = v

        feature_meta.append({
            "name": s.get("name"),
            "source": s.get("source", "market"),
            "instrument": s.get("instrument"),
            "expression": s.get("expression"),
            "screen_type": s.get("screen_type"),
            "wr_spread": s.get("wr_spread"),
            "mfe_spread": s.get("mfe_spread"),
            "direction": s.get("direction"),
            "col_idx": col,
            "original_idx": surv_idx,
            # Preserve decile data for fallback / UI
            "decile_boundaries": s.get("decile_boundaries"),
            "decile_wr": s.get("decile_wr"),
            "decile_mfe": s.get("decile_mfe"),
            "n_per_decile": s.get("n_per_decile"),
        })

    return X, feature_meta, selected


def _impute_nan(X):
    """Replace NaN with per-column median. XGBoost handles NaN natively but
    SHAP TreeExplainer can be finicky with too many NaNs.

    Returns (X_filled, medians) so live scoring can use the same medians.
    """
    medians = np.nanmedian(X, axis=0)
    # Where all values are NaN, use 0
    medians = np.where(np.isfinite(medians), medians, 0.0)

    X_filled = X.copy()
    for j in range(X.shape[1]):
        mask = ~np.isfinite(X_filled[:, j])
        X_filled[mask, j] = medians[j]

    return X_filled, medians


def _train_wr_model(X, y_win, n_splits=5, seed=42):
    """Train XGBoost binary classifier for win rate prediction with CV.

    Returns:
        (model, cv_predictions, cv_metrics)
        model: trained on full data (for SHAP + live scoring)
        cv_predictions: np.ndarray (n_signals,) — out-of-fold predicted probabilities
        cv_metrics: dict with cv_auc, cv_logloss, etc.
    """
    from sklearn.metrics import roc_auc_score, log_loss

    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "max_depth": 3,
        "min_child_weight": 20,
        "subsample": 0.7,
        "colsample_bytree": 0.5,
        "learning_rate": 0.05,
        "reg_alpha": 1.0,
        "reg_lambda": 5.0,
        "seed": seed,
        "verbosity": 0,
    }

    n_signals = X.shape[0]
    oof_preds = np.full(n_signals, np.nan, dtype=np.float64)

    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    fold_metrics = []

    for fold, (train_idx, val_idx) in enumerate(skf.split(X, y_win)):
        dtrain = xgb.DMatrix(X[train_idx], label=y_win[train_idx])
        dval = xgb.DMatrix(X[val_idx], label=y_win[val_idx])

        model_fold = xgb.train(
            params, dtrain,
            num_boost_round=500,
            evals=[(dval, "val")],
            early_stopping_rounds=30,
            verbose_eval=False,
        )

        preds = model_fold.predict(dval)
        oof_preds[val_idx] = preds

        auc = roc_auc_score(y_win[val_idx], preds)
        ll = log_loss(y_win[val_idx], preds)
        fold_metrics.append({"fold": fold + 1, "auc": auc, "logloss": ll,
                             "n_trees": model_fold.best_iteration + 1})

    # CV aggregate
    cv_auc = roc_auc_score(y_win, oof_preds)
    cv_logloss = log_loss(y_win, oof_preds)

    # Train final model on all data
    dtrain_full = xgb.DMatrix(X, label=y_win)
    # Use median n_trees from folds
    median_trees = int(np.median([f["n_trees"] for f in fold_metrics]))
    final_model = xgb.train(params, dtrain_full, num_boost_round=median_trees,
                            verbose_eval=False)

    cv_metrics = {
        "cv_auc": round(cv_auc, 4),
        "cv_logloss": round(cv_logloss, 4),
        "n_folds": n_splits,
        "fold_details": fold_metrics,
        "final_n_trees": median_trees,
    }

    return final_model, oof_preds, cv_metrics


def _train_mfe_model(X, y_mfe, winner_mask, n_splits=5, seed=42):
    """Train XGBoost regressor for MFE prediction on winners only.

    Args:
        X: full feature matrix (all signals)
        y_mfe: move_adr values (NaN for losers)
        winner_mask: boolean array — True for winners
        n_splits: CV folds
        seed: random seed

    Returns:
        (model, oof_predictions_all, cv_metrics)
        oof_predictions_all: np.ndarray (n_signals,) — predictions for ALL signals
            (trained on winners, applied to everyone for live scoring)
        cv_metrics: dict
    """
    from sklearn.metrics import r2_score
    try:
        from sklearn.metrics import root_mean_squared_error
    except ImportError:
        from sklearn.metrics import mean_squared_error
        def root_mean_squared_error(y_true, y_pred):
            return mean_squared_error(y_true, y_pred) ** 0.5

    # Extract winner-only data
    X_win = X[winner_mask]
    y_win_mfe = y_mfe[winner_mask]

    # Clean: remove NaN targets
    valid = np.isfinite(y_win_mfe)
    X_win_clean = X_win[valid]
    y_win_clean = y_win_mfe[valid]
    n_winners = len(y_win_clean)

    if n_winners < 30:
        print(f"    WARNING: Only {n_winners} winners with valid MFE — skipping MFE model")
        return None, np.full(X.shape[0], np.nan), {"status": "skipped", "n_winners": n_winners}

    params = {
        "objective": "reg:squarederror",
        "eval_metric": "rmse",
        "max_depth": 3,
        "min_child_weight": 15,
        "subsample": 0.7,
        "colsample_bytree": 0.5,
        "learning_rate": 0.05,
        "reg_alpha": 1.0,
        "reg_lambda": 5.0,
        "seed": seed,
        "verbosity": 0,
    }

    # CV on winners only
    oof_preds_win = np.full(n_winners, np.nan, dtype=np.float64)

    # Can't stratify continuous targets — use regular KFold
    from sklearn.model_selection import KFold
    kf = KFold(n_splits=n_splits, shuffle=True, random_state=seed)
    fold_metrics = []

    for fold, (train_idx, val_idx) in enumerate(kf.split(X_win_clean)):
        dtrain = xgb.DMatrix(X_win_clean[train_idx], label=y_win_clean[train_idx])
        dval = xgb.DMatrix(X_win_clean[val_idx], label=y_win_clean[val_idx])

        model_fold = xgb.train(
            params, dtrain,
            num_boost_round=500,
            evals=[(dval, "val")],
            early_stopping_rounds=30,
            verbose_eval=False,
        )

        preds = model_fold.predict(dval)
        oof_preds_win[val_idx] = preds

        rmse = root_mean_squared_error(y_win_clean[val_idx], preds)
        r2 = r2_score(y_win_clean[val_idx], preds)
        fold_metrics.append({"fold": fold + 1, "rmse": round(rmse, 4),
                             "r2": round(r2, 4),
                             "n_trees": model_fold.best_iteration + 1})

    cv_rmse = root_mean_squared_error(y_win_clean, oof_preds_win)
    cv_r2 = r2_score(y_win_clean, oof_preds_win)

    # Train final model on all winners
    dtrain_full = xgb.DMatrix(X_win_clean, label=y_win_clean)
    median_trees = int(np.median([f["n_trees"] for f in fold_metrics]))
    final_model = xgb.train(params, dtrain_full, num_boost_round=median_trees,
                            verbose_eval=False)

    # Predict on ALL signals (not just winners) — this is what live scoring does
    dall = xgb.DMatrix(X)
    all_preds = final_model.predict(dall)
    all_preds = np.maximum(all_preds, 0.0)  # MFE can't be negative

    cv_metrics = {
        "cv_rmse": round(cv_rmse, 4),
        "cv_r2": round(cv_r2, 4),
        "n_winners": n_winners,
        "n_folds": n_splits,
        "fold_details": fold_metrics,
        "final_n_trees": median_trees,
    }

    return final_model, all_preds, cv_metrics


def _compute_shap_scores(model_wr, model_mfe, X, feature_meta, n_signals):
    """Compute SHAP values and aggregate into setup_score / market_score.

    SHAP values sum to the model's prediction (log-odds for WR, raw for MFE).
    We aggregate by category (market vs setup) and normalize to 0-100.

    Returns:
        (shap_wr, shap_mfe, setup_scores, market_scores, quality_scores)
    """
    # WR SHAP
    explainer_wr = shap.TreeExplainer(model_wr)
    shap_wr = explainer_wr.shap_values(X)  # (n_signals, n_features)

    # MFE SHAP (if model exists)
    if model_mfe is not None:
        explainer_mfe = shap.TreeExplainer(model_mfe)
        shap_mfe = explainer_mfe.shap_values(X)
    else:
        shap_mfe = np.zeros_like(shap_wr)

    # Identify market vs setup features
    is_market = np.array([f.get("source") == "market" for f in feature_meta])
    is_setup = ~is_market

    n_market = int(is_market.sum())
    n_setup = int(is_setup.sum())

    # Aggregate SHAP contributions by category
    # Use WR SHAP as primary (it's the main prediction)
    if n_market > 0:
        market_shap_sum = shap_wr[:, is_market].sum(axis=1)
    else:
        market_shap_sum = np.zeros(n_signals)

    if n_setup > 0:
        setup_shap_sum = shap_wr[:, is_setup].sum(axis=1)
    else:
        setup_shap_sum = np.zeros(n_signals)

    # Normalize each to 0-100 using percentile rank
    from scipy.stats import rankdata

    def _to_percentile(arr):
        n = len(arr)
        if n < 2:
            return np.full(n, 50.0)
        ranks = rankdata(arr, method='average')
        return (ranks - 1) / (n - 1) * 100.0

    setup_scores = _to_percentile(setup_shap_sum)
    market_scores = _to_percentile(market_shap_sum)

    # Quality score: 50/50 blend (same category balance as additive model)
    quality_scores = 0.5 * setup_scores + 0.5 * market_scores

    return shap_wr, shap_mfe, setup_scores, market_scores, quality_scores


def tree_score_signals(deduped_survivors, all_signals, label,
                       top_n_features=200, assumed_stop_adr=1.0):
    """Main entry point: tree-based scoring replacement for additive model.

    Args:
        deduped_survivors: list of dicts from dedup step (with 'values' arrays)
        all_signals: list of signal dicts
        label: str ("pre" or "post")
        top_n_features: int — max features to feed into tree
        assumed_stop_adr: float — loss side of EV equation

    Returns:
        (results, model_info)
        results: list of dicts, one per signal:
            {quality_score, setup_score, market_score, predicted_wr, predicted_mfe, ev}
        model_info: dict with model objects, CV metrics, SHAP data, feature importance
    """
    print(f"\n  ── TREE SCORING ({label.upper()}) ──")
    t0 = time.time()
    n_signals = len(all_signals)

    # Step 1: Build feature matrix
    print(f"  Building feature matrix (top {top_n_features} features)...")
    X_raw, feature_meta, selected_indices = _build_feature_matrix(
        deduped_survivors, n_signals, top_n=top_n_features)
    n_feat = X_raw.shape[1]
    nan_pct = np.isnan(X_raw).mean() * 100
    print(f"  Matrix: {n_signals} × {n_feat}, NaN: {nan_pct:.1f}%")

    n_market = sum(1 for f in feature_meta if f["source"] == "market")
    n_setup = n_feat - n_market
    print(f"  Features: {n_market} market + {n_setup} setup")

    # Impute NaN for training
    X, medians = _impute_nan(X_raw)

    # Build target arrays
    y_win = np.array(["WIN" in s.get("classification", "") for s in all_signals],
                     dtype=np.float64)
    y_mfe = np.array([s.get("move_adr") or np.nan for s in all_signals],
                     dtype=np.float64)
    winner_mask = y_win.astype(bool)

    n_winners = int(winner_mask.sum())
    n_losers = n_signals - n_winners
    wr_base = n_winners / n_signals
    print(f"  Targets: {n_winners}W + {n_losers}L (base WR: {wr_base:.1%})")

    # Step 2: Train WR classifier
    print(f"\n  Training WR classifier (5-fold CV)...")
    t_wr = time.time()
    model_wr, oof_wr, cv_wr = _train_wr_model(X, y_win)
    print(f"  WR model: CV AUC={cv_wr['cv_auc']:.4f}, "
          f"CV logloss={cv_wr['cv_logloss']:.4f}, "
          f"trees={cv_wr['final_n_trees']} ({time.time()-t_wr:.1f}s)")

    # Step 3: Train MFE regressor
    print(f"\n  Training MFE regressor (5-fold CV on {n_winners} winners)...")
    t_mfe = time.time()
    model_mfe, pred_mfe_all, cv_mfe = _train_mfe_model(X, y_mfe, winner_mask)
    if model_mfe is not None:
        print(f"  MFE model: CV RMSE={cv_mfe['cv_rmse']:.4f}, "
              f"CV R²={cv_mfe['cv_r2']:.4f}, "
              f"trees={cv_mfe['final_n_trees']} ({time.time()-t_mfe:.1f}s)")
    else:
        print(f"  MFE model: skipped (too few winners)")

    # Step 4: SHAP decomposition
    print(f"\n  Computing SHAP values...")
    t_shap = time.time()
    shap_wr, shap_mfe, setup_scores, market_scores, quality_scores = \
        _compute_shap_scores(model_wr, model_mfe, X, feature_meta, n_signals)
    print(f"  SHAP done ({time.time()-t_shap:.1f}s)")

    # Step 5: Assemble predictions
    # Use OOF predictions for WR (honest out-of-sample)
    predicted_wr = np.clip(oof_wr, 0.01, 0.99)

    # Use full-model predictions for MFE (regressor, less overfitting concern)
    predicted_mfe = np.maximum(pred_mfe_all, 0.0)

    # EV = (WR × MFE) − ((1 − WR) × assumed_stop)
    ev = predicted_wr * predicted_mfe - (1.0 - predicted_wr) * assumed_stop_adr

    # Build results list (same contract as additive model)
    results = []
    for si in range(n_signals):
        results.append({
            "quality_score": round(float(quality_scores[si]), 2),
            "setup_score": round(float(setup_scores[si]), 2),
            "market_score": round(float(market_scores[si]), 2),
            "predicted_wr": round(float(predicted_wr[si]), 4),
            "predicted_mfe": round(float(predicted_mfe[si]), 3),
            "ev": round(float(ev[si]), 3),
        })

    elapsed = time.time() - t0

    # ── Diagnostics ──
    print(f"\n  Score distributions:")
    print(f"    quality_score: min={quality_scores.min():.1f} "
          f"med={np.median(quality_scores):.1f} max={quality_scores.max():.1f}")
    print(f"    setup_score:   min={setup_scores.min():.1f} "
          f"med={np.median(setup_scores):.1f} max={setup_scores.max():.1f}")
    print(f"    market_score:  min={market_scores.min():.1f} "
          f"med={np.median(market_scores):.1f} max={market_scores.max():.1f}")
    print(f"    predicted_wr:  min={predicted_wr.min():.4f} "
          f"med={np.median(predicted_wr):.4f} max={predicted_wr.max():.4f}")
    print(f"    predicted_mfe: min={predicted_mfe.min():.2f} "
          f"med={np.median(predicted_mfe):.2f} max={predicted_mfe.max():.2f}")
    print(f"    ev:            min={ev.min():.3f} "
          f"med={np.median(ev):.3f} max={ev.max():.3f}")

    # Calibration check: top/bottom decile actual WR
    n_dec = max(n_signals // 10, 1)
    order = np.argsort(predicted_wr)
    bot_wr = float(y_win[order[:n_dec]].mean())
    top_wr = float(y_win[order[-n_dec:]].mean())
    print(f"\n  Calibration check (OOF predictions):")
    print(f"    Bottom 10% predicted WR: actual WR = {bot_wr:.1%}")
    print(f"    Top 10% predicted WR:    actual WR = {top_wr:.1%}")
    spread = top_wr - bot_wr
    if top_wr > bot_wr:
        print(f"    ✓ D10-D1 spread: +{spread:.1%}")
    else:
        print(f"    ⚠ WARNING: Top ({top_wr:.1%}) ≤ Bottom ({bot_wr:.1%})")

    # Example coverage
    example_indices = [i for i, s in enumerate(all_signals) if s.get("is_example")]
    examples_scored = sum(1 for i in example_indices
                         if results[i]["quality_score"] is not None)
    print(f"\n  Examples scored: {examples_scored}/{len(example_indices)}")

    # Feature importance (top 20 by gain)
    importance = model_wr.get_score(importance_type='gain')
    # Map xgb feature names (f0, f1, ...) to real names
    imp_named = {}
    for fkey, gain in importance.items():
        idx = int(fkey.replace("f", ""))
        if idx < len(feature_meta):
            imp_named[feature_meta[idx]["name"]] = round(gain, 2)
    top_imp = sorted(imp_named.items(), key=lambda x: x[1], reverse=True)[:20]

    print(f"\n  Top 20 features by gain (WR model):")
    for name, gain in top_imp:
        src = "S" if any(f["name"] == name and f["source"] != "market"
                         for f in feature_meta) else "M"
        print(f"    [{src}] {name}: {gain:.2f}")

    print(f"\n  Tree scoring complete ({elapsed:.1f}s)")

    # Build model_info for serialization and A/B comparison
    model_info = {
        "method": "xgboost_shap",
        "top_n_features": top_n_features,
        "n_features_used": n_feat,
        "n_market": n_market,
        "n_setup": n_setup,
        "cv_wr": cv_wr,
        "cv_mfe": cv_mfe,
        "feature_importance_top20": top_imp,
        "feature_meta": feature_meta,
        "medians": medians.tolist(),
        "d10_d1_spread": round(spread, 4),
        "elapsed_s": round(elapsed, 1),
        # Keep model objects for live scoring serialization
        "_model_wr": model_wr,
        "_model_mfe": model_mfe,
        "_shap_wr": shap_wr,
        "_shap_mfe": shap_mfe,
    }

    return results, model_info
