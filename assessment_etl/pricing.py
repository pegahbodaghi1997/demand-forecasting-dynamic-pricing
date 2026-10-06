"""Auditable, rule-based next-week price proposals with no elasticity claim."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

from .forecasting import _load_panel
from .pipeline import DEFAULT_DATABASE, connect


POLICY_VERSION = "rule_v1"


@dataclass(frozen=True)
class PricingPolicy:
    """Explicit business assumptions; percentages are percentage points."""

    minimum_margin_pct: Decimal = Decimal("15")
    maximum_change_pct: Decimal = Decimal("5")
    step_pct: Decimal = Decimal("3")
    demand_trigger_pct: Decimal = Decimal("20")
    recent_weeks: int = 13

    def validate(self) -> None:
        if not all(
            value.is_finite()
            for value in (
                self.minimum_margin_pct,
                self.maximum_change_pct,
                self.step_pct,
                self.demand_trigger_pct,
            )
        ):
            raise ValueError("Policy percentages must be finite")
        if not 0 <= self.minimum_margin_pct < 100:
            raise ValueError("minimum_margin_pct must be in [0, 100)")
        if not 0 <= self.step_pct <= self.maximum_change_pct <= 100:
            raise ValueError("step_pct and maximum_change_pct must satisfy 0 <= step <= maximum <= 100")
        if not 0 <= self.demand_trigger_pct < 100:
            raise ValueError("demand_trigger_pct must be in [0, 100)")
        if self.recent_weeks < 1:
            raise ValueError("recent_weeks must be positive")

    def as_json(self) -> dict[str, str | int]:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def propose_price(
    *,
    current_price_cents: int,
    unit_cost_cents: int,
    forecast_units: int,
    recent_sales_sum: int,
    recent_sales_count: int,
    policy: PricingPolicy,
) -> dict[str, object]:
    """Apply hard price limits before the directional demand rule."""
    policy.validate()
    if current_price_cents <= 0 or unit_cost_cents < 0 or forecast_units < 0:
        raise ValueError("Price, cost, and forecast units must be valid nonnegative measures")
    if recent_sales_count < 0 or recent_sales_sum < 0:
        raise ValueError("Recent sales measures must be nonnegative")

    current = Decimal(current_price_cents)
    margin = policy.minimum_margin_pct / 100
    cap = policy.maximum_change_pct / 100
    step = policy.step_pct / 100
    trigger = policy.demand_trigger_pct / 100
    minimum = _ceil(Decimal(unit_cost_cents) / (1 - margin))
    lowest_allowed = max(1, _ceil(current * (1 - cap)))
    highest_allowed = _floor(current * (1 + cap))
    recent_mean = (
        Decimal(recent_sales_sum) / recent_sales_count
        if recent_sales_count == policy.recent_weeks
        else None
    )
    ratio = Decimal(forecast_units) / recent_mean if recent_mean and recent_mean > 0 else None
    if ratio is None:
        signal = "unavailable"
        proposed = current_price_cents
        reason = "insufficient_recent_sales" if recent_mean is None else "zero_recent_sales"
    elif ratio >= 1 + trigger:
        signal = "high"
        proposed = int((current * (1 + step)).to_integral_value(rounding=ROUND_HALF_UP))
        reason = "forecast_above_recent_mean"
    elif ratio <= 1 - trigger:
        signal = "low"
        proposed = int((current * (1 - step)).to_integral_value(rounding=ROUND_HALF_UP))
        reason = "forecast_below_recent_mean"
    else:
        signal = "neutral"
        proposed = current_price_cents
        reason = "forecast_near_recent_mean"

    if minimum > highest_allowed:
        return {
            "minimum_price_cents": minimum,
            "recommended_price_cents": None,
            "recent_mean_units": round(float(recent_mean), 3) if recent_mean is not None else None,
            "forecast_vs_recent_ratio": round(float(ratio), 4) if ratio is not None else None,
            "demand_signal": signal,
            "decision": "review",
            "reason_code": "margin_floor_exceeds_change_cap",
            "break_even_units": None,
        }

    candidate = max(minimum, lowest_allowed, min(proposed, highest_allowed))
    if minimum > current_price_cents and candidate == minimum:
        reason = "minimum_margin_floor"
    elif candidate == current_price_cents and signal in ("high", "low"):
        reason = "rounded_to_current_price"
    decision = (
        "increase" if candidate > current_price_cents
        else "decrease" if candidate < current_price_cents
        else "hold"
    )
    old_unit_margin = current_price_cents - unit_cost_cents
    new_unit_margin = candidate - unit_cost_cents
    break_even = (
        _ceil(Decimal(forecast_units * old_unit_margin) / new_unit_margin)
        if candidate != current_price_cents and old_unit_margin > 0 and new_unit_margin > 0
        else None
    )
    return {
        "minimum_price_cents": minimum,
        "recommended_price_cents": candidate,
        "recent_mean_units": round(float(recent_mean), 3) if recent_mean is not None else None,
        "forecast_vs_recent_ratio": round(float(ratio), 4) if ratio is not None else None,
        "demand_signal": signal,
        "decision": decision,
        "reason_code": reason,
        "break_even_units": break_even,
    }


def _summary(connection: sqlite3.Connection, run_id: int, *, skipped: bool) -> dict[str, object]:
    run = connection.execute(
        "SELECT * FROM pricing_run WHERE pricing_run_id = ?", (run_id,)
    ).fetchone()
    rows = connection.execute(
        """SELECT p.product_name, x.target_week,
                  x.current_price_cents / 100.0 AS current_price,
                  x.unit_cost_cents / 100.0 AS unit_cost,
                  x.minimum_price_cents / 100.0 AS minimum_price,
                  x.recommended_price_cents / 100.0 AS recommended_price,
                  100.0 * (x.current_price_cents - x.unit_cost_cents)
                        / x.current_price_cents AS current_gross_margin_pct,
                  CASE WHEN x.recommended_price_cents > 0
                       THEN 100.0 * (x.recommended_price_cents - x.unit_cost_cents)
                            / x.recommended_price_cents
                  END AS recommended_gross_margin_pct,
                  x.forecast_units, x.recent_mean_units,
                  x.forecast_vs_recent_ratio, x.demand_signal, x.decision,
                  x.reason_code, x.opening_stock_cover_days_proxy,
                  x.inventory_warning, x.break_even_units,
                  CASE WHEN x.forecast_units > 0 AND x.break_even_units IS NOT NULL
                       THEN 100.0 * (x.break_even_units - x.forecast_units)
                            / x.forecast_units
                  END AS break_even_volume_change_pct
           FROM fact_price_recommendation AS x
           JOIN dim_product AS p ON p.product_id = x.product_id
           WHERE x.pricing_run_id = ? ORDER BY p.product_name""",
        (run_id,),
    ).fetchall()
    recommendations = [dict(row) for row in rows]
    counts = {
        decision: sum(row["decision"] == decision for row in recommendations)
        for decision in ("increase", "decrease", "hold", "review")
    }
    return {
        "pricing_run_id": run_id,
        "forecast_run_id": run["forecast_run_id"],
        "base_week": run["base_week"],
        "target_week": run["target_week"],
        "policy_version": run["policy_version"],
        "policy": json.loads(run["policy_json"]),
        "decision_counts": counts,
        "recommendations": recommendations,
        "interpretation": (
            "Rule-based review proposals, not implemented prices or causal profit uplift. "
            "Break-even units compare unit gross margins using forecast volume as a reference; "
            "inventory warnings use historical opening stock only."
        ),
        "skipped_existing": skipped,
    }


def run_pricing(
    database: Path = DEFAULT_DATABASE,
    *,
    policy: PricingPolicy | None = None,
) -> dict[str, object]:
    """Version next-week proposals from the latest current forecast and actuals."""
    policy = policy or PricingPolicy()
    policy.validate()
    connection = connect(database)
    try:
        forecast = connection.execute(
            "SELECT forecast_run_id, input_max_week, input_sha256 FROM forecast_run "
            "ORDER BY forecast_run_id DESC LIMIT 1"
        ).fetchone()
        if forecast is None:
            raise ValueError("Run the demand forecast before pricing")
        panel = _load_panel(connection)
        base_week = forecast["input_max_week"]
        if panel.max_week != base_week or panel.input_sha256 != forecast["input_sha256"]:
            raise ValueError("The demand forecast is stale; rerun forecasting after the latest sales or price load")
        target_week = base_week + 1
        forecast_rows = connection.execute(
            """SELECT f.product_id, f.predicted_units, a.unit_price_cents,
                      a.unit_cost_cents, a.opening_inventory_units, a.sales_units
               FROM fact_demand_forecast AS f
               JOIN fact_weekly_product AS a
                 ON a.product_id = f.product_id AND a.week_id = ?
               WHERE f.forecast_run_id = ? AND f.week_id = ?
               ORDER BY f.product_id""",
            (base_week, forecast["forecast_run_id"], target_week),
        ).fetchall()
        if len(forecast_rows) != len(panel.products):
            raise ValueError("The next-week forecast does not cover every current product")
        history = {
            row["product_id"]: (row["sales_sum"], row["sales_count"])
            for row in connection.execute(
                """SELECT product_id, SUM(sales_units) AS sales_sum,
                          COUNT(*) AS sales_count
                   FROM fact_weekly_product WHERE week_id BETWEEN ? AND ?
                   GROUP BY product_id""",
                (base_week - policy.recent_weeks + 1, base_week),
            )
        }
        input_rows = [dict(row) for row in forecast_rows]
        fingerprint = hashlib.sha256(
            json.dumps(
                {"latest": input_rows, "recent": history}, sort_keys=True
            ).encode()
        ).hexdigest()
        specification = {
            "policy_version": POLICY_VERSION,
            "policy": policy.as_json(),
            "forecast_run_id": forecast["forecast_run_id"],
            "input_sha256": fingerprint,
        }
        run_key = hashlib.sha256(json.dumps(specification, sort_keys=True).encode()).hexdigest()
        existing = connection.execute(
            "SELECT pricing_run_id FROM pricing_run WHERE run_key = ?", (run_key,)
        ).fetchone()
        if existing:
            return _summary(connection, existing["pricing_run_id"], skipped=True)

        proposals = []
        for row in forecast_rows:
            sales_sum, sales_count = history.get(row["product_id"], (0, 0))
            proposal = propose_price(
                current_price_cents=row["unit_price_cents"],
                unit_cost_cents=row["unit_cost_cents"],
                forecast_units=row["predicted_units"],
                recent_sales_sum=sales_sum,
                recent_sales_count=sales_count,
                policy=policy,
            )
            inventory = row["opening_inventory_units"]
            sales = row["sales_units"]
            cover = 7.0 * inventory / sales if inventory is not None and sales > 0 else None
            inventory_warning = int(
                inventory is None or sales == 0 or sales > inventory or (cover is not None and cover < 7)
            )
            proposals.append(
                (row["product_id"], row["unit_price_cents"], row["unit_cost_cents"],
                 row["predicted_units"], proposal, cover, inventory_warning)
            )

        with connection:
            cursor = connection.execute(
                """INSERT INTO pricing_run
                   (run_key, forecast_run_id, base_week, target_week,
                    policy_version, policy_json, input_sha256)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_key, forecast["forecast_run_id"], base_week, target_week,
                 POLICY_VERSION, json.dumps(policy.as_json(), sort_keys=True), fingerprint),
            )
            run_id = cursor.lastrowid
            connection.executemany(
                """INSERT INTO fact_price_recommendation
                   (pricing_run_id, product_id, target_week, current_price_cents,
                    unit_cost_cents, minimum_price_cents, recommended_price_cents,
                    forecast_units, recent_mean_units, forecast_vs_recent_ratio,
                    demand_signal, decision, reason_code,
                    opening_stock_cover_days_proxy, inventory_warning, break_even_units)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (run_id, product_id, target_week, current, cost,
                     proposal["minimum_price_cents"], proposal["recommended_price_cents"],
                     units, proposal["recent_mean_units"], proposal["forecast_vs_recent_ratio"],
                     proposal["demand_signal"], proposal["decision"], proposal["reason_code"],
                     cover, warning, proposal["break_even_units"])
                    for product_id, current, cost, units, proposal, cover, warning in proposals
                ],
            )
        return _summary(connection, run_id, skipped=False)
    finally:
        connection.close()
