"""Leakage-safe, direct 26-week sales forecasting and rolling-origin evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor

from .pipeline import DEFAULT_DATABASE, connect


FEATURE_VERSION = "direct_horizon_v1"
MODEL_CONFIGS = {
    "gbdt_mae": {
        "loss": "absolute_error",
        "max_iter": 160,
        "learning_rate": 0.06,
        "max_leaf_nodes": 15,
        "min_samples_leaf": 30,
        "l2_regularization": 1.0,
    },
    "gbdt_rmse": {
        "loss": "squared_error",
        "max_iter": 160,
        "learning_rate": 0.06,
        "max_leaf_nodes": 15,
        "min_samples_leaf": 30,
        "l2_regularization": 1.0,
    },
}
BASELINE_METHODS = ("seasonal_52", "recent_13_mean")


@dataclass(frozen=True)
class Panel:
    products: list[tuple[int, str]]
    sales: np.ndarray
    prices: np.ndarray
    max_week: int
    input_sha256: str


def _load_panel(connection: sqlite3.Connection) -> Panel:
    rows = connection.execute(
        """
        SELECT f.product_id, p.product_name, f.week_id,
               f.sales_units, f.unit_price_cents
        FROM fact_weekly_product AS f
        JOIN dim_product AS p ON p.product_id = f.product_id
        ORDER BY f.product_id, f.week_id
        """
    ).fetchall()
    if not rows:
        raise ValueError("No cleaned weekly sales data are available")
    max_week = max(row["week_id"] for row in rows)
    products = sorted({(row["product_id"], row["product_name"]) for row in rows})
    product_index = {product_id: index for index, (product_id, _) in enumerate(products)}
    sales = np.full((len(products), max_week), np.nan, dtype=np.float64)
    prices = np.full_like(sales, np.nan)
    digest = hashlib.sha256()
    for row in rows:
        product_id = row["product_id"]
        week = row["week_id"]
        index = product_index[product_id]
        sales[index, week - 1] = row["sales_units"]
        prices[index, week - 1] = row["unit_price_cents"] / 100.0
        digest.update(
            f"{product_id}|{week}|{row['sales_units']}|{row['unit_price_cents']}\n".encode()
        )
    if np.isnan(sales).any() or np.isnan(prices).any():
        first = np.argwhere(np.isnan(sales) | np.isnan(prices))[0]
        product = products[int(first[0])][1]
        week = int(first[1]) + 1
        raise ValueError(
            f"Forecasting needs continuous weekly history; {product!r} lacks week {week}"
        )
    return Panel(products, sales, prices, max_week, digest.hexdigest())


def _origin_features(panel: Panel, product_index: int, origin: int) -> tuple[float, ...]:
    """Only values at or before the forecast origin are read here."""
    history = panel.sales[product_index, :origin]
    price_history = panel.prices[product_index, :origin]
    recent_13 = history[-13:]
    price_mean_13 = float(np.mean(price_history[-13:]))
    return (
        float(history[-1]),
        float(history[-4]),
        float(history[-13]),
        float(history[-26]),
        float(history[-52]),
        float(np.mean(history[-4:])),
        float(np.mean(recent_13)),
        float(np.mean(history[-26:])),
        float(np.mean(history[-52:])),
        float(np.std(recent_13)),
        float(np.mean(recent_13 == 0)),
        float(np.mean(recent_13) - np.mean(history[-26:-13])),
        float(price_history[-1]),
        price_mean_13,
        float(price_history[-1] / price_mean_13) if price_mean_13 else 1.0,
    )


def _feature_vector(
    panel: Panel,
    product_index: int,
    origin: int,
    horizon_step: int,
    origin_features: tuple[float, ...] | None = None,
) -> tuple[float, ...]:
    if origin < 52 or not 1 <= horizon_step <= 26:
        raise ValueError("At least 52 observed weeks and horizon 1..26 are required")
    target_week = origin + horizon_step
    seasonal_week = target_week - 52
    if seasonal_week > origin:
        raise AssertionError("Seasonal feature would read beyond the forecast origin")
    phase = 2 * math.pi * ((target_week - 1) % 52) / 52
    known = origin_features or _origin_features(panel, product_index, origin)
    return (
        float(product_index),
        float(horizon_step),
        float(target_week),
        math.sin(phase),
        math.cos(phase),
        float(panel.sales[product_index, seasonal_week - 1]),
        *known,
    )


def _training_matrix(panel: Panel, through_week: int, horizon: int) -> tuple[np.ndarray, np.ndarray]:
    features: list[tuple[float, ...]] = []
    targets: list[float] = []
    # Every second origin keeps the training set compact while exposing all
    # horizons and all products. Target weeks never exceed through_week.
    for origin in range(52, through_week, 2):
        furthest = min(horizon, through_week - origin)
        for product_index in range(len(panel.products)):
            known = _origin_features(panel, product_index, origin)
            for step in range(1, furthest + 1):
                features.append(
                    _feature_vector(panel, product_index, origin, step, known)
                )
                targets.append(panel.sales[product_index, origin + step - 1])
    return np.asarray(features, dtype=np.float64), np.asarray(targets, dtype=np.float64)


def _future_matrix(panel: Panel, origin: int, horizon: int) -> np.ndarray:
    features = []
    for product_index in range(len(panel.products)):
        known = _origin_features(panel, product_index, origin)
        for step in range(1, horizon + 1):
            features.append(_feature_vector(panel, product_index, origin, step, known))
    return np.asarray(features, dtype=np.float64)


def _baseline_predictions(panel: Panel, origin: int, horizon: int) -> dict[str, np.ndarray]:
    seasonal = np.column_stack(
        [panel.sales[:, origin + step - 53] for step in range(1, horizon + 1)]
    )
    recent_mean = np.mean(panel.sales[:, origin - 13 : origin], axis=1)
    return {
        "seasonal_52": seasonal,
        "recent_13_mean": np.repeat(recent_mean[:, None], horizon, axis=1),
    }


def _fit_model(name: str, features: np.ndarray, targets: np.ndarray) -> HistGradientBoostingRegressor:
    model = HistGradientBoostingRegressor(
        **MODEL_CONFIGS[name],
        categorical_features=[0],
        early_stopping=False,
        random_state=42,
    )
    model.fit(features, targets)
    return model


def _metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float | int | None]:
    actual = actual.ravel()
    predicted = predicted.ravel()
    absolute_error = np.abs(actual - predicted)
    positive = actual > 0
    denominator = float(np.sum(actual))
    return {
        "observations": int(actual.size),
        "zero_actuals": int(np.sum(~positive)),
        "mae": round(float(np.mean(absolute_error)), 3),
        "rmse": round(float(np.sqrt(np.mean((actual - predicted) ** 2))), 3),
        "mape_positive_pct": (
            round(float(np.mean(absolute_error[positive] / actual[positive]) * 100), 3)
            if np.any(positive)
            else None
        ),
        "mape_coverage_pct": round(float(np.mean(positive) * 100), 2),
        "wape_pct": round(float(np.sum(absolute_error) / denominator * 100), 3)
        if denominator > 0
        else None,
        "bias_units": round(float(np.mean(predicted - actual)), 3),
    }


def _run_key(panel: Panel, horizon: int, origins: list[int]) -> str:
    specification = {
        "input_sha256": panel.input_sha256,
        "horizon": horizon,
        "origins": origins,
        "feature_version": FEATURE_VERSION,
        "model_configs": MODEL_CONFIGS,
        "sklearn_version": sklearn.__version__,
    }
    return hashlib.sha256(json.dumps(specification, sort_keys=True).encode()).hexdigest()


def _existing_summary(connection: sqlite3.Connection, run_key: str) -> dict[str, object] | None:
    row = connection.execute(
        """
        SELECT forecast_run_id, input_max_week, horizon_weeks, selected_method,
               selection_rule, development_metrics_json, holdout_metrics_json,
               training_rows
        FROM forecast_run WHERE run_key = ?
        """,
        (run_key,),
    ).fetchone()
    if row is None:
        return None
    run_id = row["forecast_run_id"]
    backtests = connection.execute(
        """
        SELECT b.method, b.fold_number, b.origin_week, b.product_id,
               p.product_name, b.actual_units, b.predicted_units
        FROM forecast_backtest AS b
        JOIN dim_product AS p ON p.product_id = b.product_id
        WHERE b.forecast_run_id = ?
        ORDER BY b.method, b.fold_number, b.product_id, b.target_week
        """,
        (run_id,),
    ).fetchall()
    fold_metrics = {}
    for method in (*BASELINE_METHODS, *MODEL_CONFIGS):
        fold_metrics[method] = []
        for fold_number in range(1, 5):
            subset = [
                item
                for item in backtests
                if item["method"] == method and item["fold_number"] == fold_number
            ]
            actual = np.asarray([item["actual_units"] for item in subset])
            predicted = np.asarray([item["predicted_units"] for item in subset])
            fold_metrics[method].append(_metrics(actual, predicted))
    holdout = [
        item
        for item in backtests
        if item["method"] == row["selected_method"] and item["fold_number"] == 4
    ]
    product_errors: dict[str, list[float]] = {}
    for item in holdout:
        product_errors.setdefault(item["product_name"], []).append(
            abs(item["actual_units"] - item["predicted_units"])
        )
    worst_products = sorted(
        (
            {"product": name, "mae": round(float(np.mean(errors)), 3)}
            for name, errors in product_errors.items()
        ),
        key=lambda item: item["mae"],
        reverse=True,
    )[:5]
    forecast_count, first_week, last_week = connection.execute(
        """
        SELECT COUNT(*), MIN(week_id), MAX(week_id)
        FROM fact_demand_forecast WHERE forecast_run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return {
        "forecast_run_id": run_id,
        "input_max_week": row["input_max_week"],
        "horizon_weeks": row["horizon_weeks"],
        "backtest_origins": sorted({item["origin_week"] for item in backtests}),
        "selected_method": row["selected_method"],
        "selection_rule": row["selection_rule"],
        "development_metrics": json.loads(row["development_metrics_json"]),
        "holdout_metrics": json.loads(row["holdout_metrics_json"]),
        "fold_metrics": fold_metrics,
        "worst_holdout_products": worst_products,
        "training_rows": row["training_rows"],
        "forecast_rows": forecast_count,
        "forecast_weeks": [first_week, last_week],
        "error_band": "90th percentile absolute error by horizon on development folds",
        "skipped_existing": True,
    }


def run_forecast(
    database: Path = DEFAULT_DATABASE,
    *,
    horizon: int = 26,
) -> dict[str, object]:
    """Evaluate on 4 chronological windows, select on 3, and forecast ahead."""
    if not 1 <= horizon <= 26:
        raise ValueError("Supported forecast horizon is 1..26 weeks")
    connection = connect(database)
    try:
        panel = _load_panel(connection)
        origins = [panel.max_week - multiple * horizon for multiple in (4, 3, 2, 1)]
        if origins[0] < 104:
            raise ValueError(
                "At least 104 observed weeks before the first backtest origin are required"
            )
        key = _run_key(panel, horizon, origins)
        existing = _existing_summary(connection, key)
        if existing is not None:
            return existing

        methods = (*BASELINE_METHODS, *MODEL_CONFIGS)
        fold_actuals: list[np.ndarray] = []
        fold_predictions: dict[str, list[np.ndarray]] = {name: [] for name in methods}
        training_rows = 0
        for origin in origins:
            actual = panel.sales[:, origin : origin + horizon]
            fold_actuals.append(actual)
            predictions = _baseline_predictions(panel, origin, horizon)
            training_features, training_targets = _training_matrix(panel, origin, horizon)
            future_features = _future_matrix(panel, origin, horizon)
            training_rows = len(training_targets)
            for name in MODEL_CONFIGS:
                model = _fit_model(name, training_features, training_targets)
                predictions[name] = np.maximum(
                    model.predict(future_features).reshape(len(panel.products), horizon),
                    0.0,
                )
            for name in methods:
                fold_predictions[name].append(predictions[name])

        development_metrics = {
            name: _metrics(
                np.concatenate(fold_actuals[:3]),
                np.concatenate(fold_predictions[name][:3]),
            )
            for name in methods
        }
        holdout_metrics = {
            name: _metrics(fold_actuals[3], fold_predictions[name][3])
            for name in methods
        }
        fold_metrics = {
            name: [
                _metrics(actual, predicted)
                for actual, predicted in zip(fold_actuals, fold_predictions[name])
            ]
            for name in methods
        }
        best = min(methods, key=lambda name: (
            development_metrics[name]["mae"],
            development_metrics[name]["rmse"],
        ))
        best_baseline = min(
            BASELINE_METHODS, key=lambda name: development_metrics[name]["mae"]
        )
        selected = (
            best_baseline
            if best in MODEL_CONFIGS
            and development_metrics[best_baseline]["mae"]
            <= 1.01 * development_metrics[best]["mae"]
            else best
        )
        selection_rule = (
            "Lowest pooled MAE on the first three 26-week backtests; prefer the "
            "best baseline when it is within 1% of the best learned model. "
            "The final 26-week backtest is reported without influencing selection."
        )
        if selected in MODEL_CONFIGS:
            full_features, full_targets = _training_matrix(panel, panel.max_week, horizon)
            final_model = _fit_model(selected, full_features, full_targets)
            final_predictions = np.maximum(
                final_model.predict(_future_matrix(panel, panel.max_week, horizon))
                .reshape(len(panel.products), horizon),
                0.0,
            )
            training_rows = len(full_targets)
        else:
            final_predictions = _baseline_predictions(panel, panel.max_week, horizon)[
                selected
            ]
            training_rows = 0

        # Historical absolute-error bands use development folds only. They
        # are descriptive ranges, not calibrated probabilistic intervals.
        absolute_dev_errors = np.concatenate(
            [
                np.abs(fold_actuals[index] - fold_predictions[selected][index])
                for index in range(3)
            ],
            axis=0,
        )
        horizon_error_band = np.quantile(absolute_dev_errors, 0.9, axis=0)
        backtest_rows = []
        for name in methods:
            for fold_number, origin in enumerate(origins, start=1):
                actual = fold_actuals[fold_number - 1]
                predicted = fold_predictions[name][fold_number - 1]
                for product_index, (product_id, _) in enumerate(panel.products):
                    for step in range(horizon):
                        backtest_rows.append(
                            (
                                name,
                                fold_number,
                                origin,
                                product_id,
                                origin + step + 1,
                                int(actual[product_index, step]),
                                float(predicted[product_index, step]),
                            )
                        )

        with connection:
            cursor = connection.execute(
                """
                INSERT INTO forecast_run (
                    run_key, input_max_week, horizon_weeks, input_sha256,
                    feature_version, selected_method, selection_rule,
                    selected_parameters_json, development_metrics_json,
                    holdout_metrics_json, training_rows
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    panel.max_week,
                    horizon,
                    panel.input_sha256,
                    FEATURE_VERSION,
                    selected,
                    selection_rule,
                    json.dumps(MODEL_CONFIGS.get(selected, {}), sort_keys=True),
                    json.dumps(development_metrics, sort_keys=True),
                    json.dumps(holdout_metrics, sort_keys=True),
                    training_rows,
                ),
            )
            run_id = cursor.lastrowid
            connection.executemany(
                """
                INSERT INTO forecast_backtest (
                    forecast_run_id, method, fold_number, origin_week,
                    product_id, target_week, actual_units, predicted_units
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ((run_id, *row) for row in backtest_rows),
            )
            forecast_rows = []
            for product_index, (product_id, _) in enumerate(panel.products):
                for step in range(horizon):
                    week = panel.max_week + step + 1
                    connection.execute(
                        """
                        INSERT INTO dim_week (week_id, relative_year, week_in_year)
                        VALUES (?, ?, ?)
                        ON CONFLICT(week_id) DO NOTHING
                        """,
                        (week, (week - 1) // 52 + 1, (week - 1) % 52 + 1),
                    )
                    point = max(0, round(float(final_predictions[product_index, step])))
                    band = float(horizon_error_band[step])
                    forecast_rows.append(
                        (
                            run_id,
                            product_id,
                            week,
                            point,
                            max(0, round(point - band)),
                            max(point, round(point + band)),
                        )
                    )
            connection.executemany(
                """
                INSERT INTO fact_demand_forecast (
                    forecast_run_id, product_id, week_id, predicted_units,
                    lower_error_band_units, upper_error_band_units
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                forecast_rows,
            )

        selected_holdout = fold_predictions[selected][3]
        worst_products = sorted(
            (
                {
                    "product": name,
                    "mae": round(
                        float(np.mean(np.abs(fold_actuals[3][index] - selected_holdout[index]))),
                        3,
                    ),
                }
                for index, (_, name) in enumerate(panel.products)
            ),
            key=lambda item: item["mae"],
            reverse=True,
        )[:5]
        return {
            "forecast_run_id": run_id,
            "input_max_week": panel.max_week,
            "horizon_weeks": horizon,
            "backtest_origins": origins,
            "selected_method": selected,
            "selection_rule": selection_rule,
            "development_metrics": development_metrics,
            "holdout_metrics": holdout_metrics,
            "fold_metrics": fold_metrics,
            "worst_holdout_products": worst_products,
            "training_rows": training_rows,
            "forecast_rows": len(forecast_rows),
            "forecast_weeks": [panel.max_week + 1, panel.max_week + horizon],
            "error_band": "90th percentile absolute error by horizon on development folds",
            "skipped_existing": False,
        }
    finally:
        connection.close()
