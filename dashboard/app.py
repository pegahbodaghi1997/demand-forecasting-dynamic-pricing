"""Interactive weekly sales, forecast, and inventory dashboard."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from dashboard.data import database_path, load_dashboard_data, period_metrics


st.set_page_config(page_title="Weekly commodity dashboard", layout="wide")


@st.cache_data(show_spinner=False)
def cached_data(path: str, modified_ns: int):
    """Reload the views when the database file changes."""
    del modified_ns
    return load_dashboard_data(path)


def format_money(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:,.2f}"


def format_percent(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:.1%}"


st.title("Weekly commodity dashboard")
st.caption("Observed sales and the latest 26-week sales forecast · Weeks are relative IDs · Source currency is unspecified")

path = database_path()
if not path.is_file():
    st.error("Dashboard data is missing. Load the workbook locally or provide a dashboard snapshot.")
    st.code("python -m assessment_etl load path/to/workbook.xlsx\npython -m dashboard.export_snapshot")
    st.stop()

try:
    data = cached_data(str(path), path.stat().st_mtime_ns)
except (OSError, ValueError) as exc:
    st.error(str(exc))
    st.stop()

if data.products.empty or data.portfolio.empty:
    st.warning("No cleaned historical sales are available yet.")
    st.stop()

product_names = sorted(data.products["product_name"].unique().tolist())
last_week = int(data.portfolio["week_id"].max())
with st.sidebar:
    st.header("Filters")
    selected_product = st.selectbox("Product", ["All products", *product_names])
    history_options = [13, 26, 52, 104, "All history"]
    history_window = st.selectbox("Historical chart window", history_options, index=2)
    st.caption(f"Last observed week: {last_week} · Products: {len(product_names)}")

selected_rows = (
    data.products
    if selected_product == "All products"
    else data.products.loc[data.products["product_name"] == selected_product]
)
product_count = len(product_names) if selected_product == "All products" else 1
recent = period_metrics(selected_rows, last_week=last_week, product_count=product_count)
previous = period_metrics(
    selected_rows, last_week=last_week - 13, product_count=product_count
)
growth = (
    recent["revenue"] / previous["revenue"] - 1
    if recent["complete"] and previous["complete"] and previous["revenue"] > 0
    else None
)
chart_start = (
    int(data.portfolio["week_id"].min())
    if history_window == "All history"
    else last_week - history_window + 1
)

overview, forecast_tab, inventory_tab, method_tab = st.tabs(
    ["Overview", "Demand forecast", "Inventory diagnostics", "Data and method"]
)

with overview:
    st.subheader(f"Observed weeks {recent['first_week']}–{recent['last_week']}")
    cards = st.columns(4)
    cards[0].metric("Revenue (currency unspecified)", format_money(recent["revenue"]))
    cards[1].metric("Revenue vs prior 13 weeks", format_percent(growth))
    cards[2].metric("Gross margin", format_percent(recent["gross_margin"]))
    cards[3].metric("Sales units", f"{recent['sales_units']:,}")
    if not recent["complete"]:
        st.warning(
            f"This period contains {recent['rows']:,} of {recent['expected_rows']:,} "
            "expected product-week rows. Totals describe observed rows only."
        )
    if growth is None:
        st.caption("Period growth requires two complete 13-week panels and positive prior revenue.")
    st.caption(f"Source-reported operating profit: {format_money(recent['operating_profit'])}; unavailable if any included value is missing.")

    if selected_product == "All products":
        chart_data = data.portfolio.loc[
            data.portfolio["week_id"] >= chart_start, ["week_id", "revenue", "gross_profit"]
        ]
    else:
        chart_data = selected_rows.loc[
            selected_rows["week_id"] >= chart_start, ["week_id", "revenue", "gross_profit"]
        ]
    chart_long = chart_data.melt("week_id", var_name="measure", value_name="amount")
    chart = (
        alt.Chart(chart_long)
        .mark_line(point=True)
        .encode(
            x=alt.X("week_id:Q", title="Relative week ID"),
            y=alt.Y("amount:Q", title="Source currency units"),
            color=alt.Color("measure:N", title="Measure"),
            tooltip=["week_id:Q", "measure:N", alt.Tooltip("amount:Q", format=",.2f")],
        )
        .properties(height=330)
    )
    st.altair_chart(chart, width="stretch")

    recent_products = selected_rows.loc[
        selected_rows["week_id"].between(last_week - 12, last_week)
    ]
    ranked = (
        recent_products.groupby("product_name", as_index=False)
        .agg(revenue=("revenue", "sum"), sales_units=("sales_units", "sum"), gross_profit=("gross_profit", "sum"))
        .sort_values("revenue", ascending=False)
    )
    ranked["gross_margin_pct"] = ranked["gross_profit"].div(ranked["revenue"].where(ranked["revenue"] > 0)).mul(100)
    st.subheader("Products in the latest 13 weeks")
    st.dataframe(
        ranked[["product_name", "revenue", "sales_units", "gross_margin_pct"]],
        hide_index=True,
        width="stretch",
    )

with forecast_tab:
    st.subheader("Future recorded-sales proxy")
    st.caption("The model predicts sales units. Lost sales are unknown, so this is not unrestricted demand.")
    forecast_rows = (
        data.forecast
        if selected_product == "All products"
        else data.forecast.loc[data.forecast["product_name"] == selected_product]
    )
    if forecast_rows.empty:
        st.info("No forecast has been stored for this selection.")
    else:
        method = str(forecast_rows["selected_method"].iloc[0])
        run_id = int(forecast_rows["forecast_run_id"].iloc[0])
        st.caption(f"Latest run: {run_id} · Selected method: {method}")
        observed = (
            data.portfolio.loc[data.portfolio["week_id"] >= max(last_week - 51, chart_start), ["week_id", "sales_units"]]
            if selected_product == "All products"
            else selected_rows.loc[selected_rows["week_id"] >= max(last_week - 51, chart_start), ["week_id", "sales_units"]]
        ).copy()
        observed["series"] = "Observed sales"
        predicted = (
            forecast_rows.groupby("week_id", as_index=False)["predicted_units"].sum()
            if selected_product == "All products"
            else forecast_rows[["week_id", "predicted_units"]].copy()
        ).rename(columns={"predicted_units": "sales_units"})
        predicted["series"] = "Forecast"
        combined = pd.concat([observed, predicted], ignore_index=True)
        lines = (
            alt.Chart(combined)
            .mark_line(point=True)
            .encode(
                x=alt.X("week_id:Q", title="Relative week ID"),
                y=alt.Y("sales_units:Q", title="Units"),
                color=alt.Color("series:N", title="Series"),
                tooltip=["week_id:Q", "series:N", alt.Tooltip("sales_units:Q", format=",.0f")],
            )
            .properties(height=350)
        )
        if selected_product != "All products":
            band = (
                alt.Chart(forecast_rows)
                .mark_area(opacity=0.18, color="#1f77b4")
                .encode(
                    x="week_id:Q",
                    y="lower_error_band_units:Q",
                    y2="upper_error_band_units:Q",
                )
            )
            st.altair_chart(band + lines, width="stretch")
            st.caption("Shaded range: historical 90th-percentile absolute-error band by horizon; it is not a calibrated probability interval.")
        else:
            st.altair_chart(lines, width="stretch")
            st.caption("Product error bands are not added together; an aggregate uncertainty band has not been validated.")
        st.metric("Forecast units across shown future weeks", f"{int(forecast_rows['predicted_units'].sum()):,}")
        st.dataframe(
            predicted.rename(columns={"sales_units": "forecast_units"}),
            hide_index=True,
            width="stretch",
        )

with inventory_tab:
    st.subheader(f"Opening inventory at observed week {last_week}")
    st.caption("Stock cover = 7 × opening units / observed sales units. It is a historical proxy, not formal DSI or proof of a stockout.")
    latest = selected_rows.loc[selected_rows["week_id"] == last_week].copy()
    if latest.empty:
        st.info("This product has no row in the latest observed week.")
    else:
        flags = int(latest["sales_exceed_opening_inventory"].sum())
        missing_cover = int(latest["opening_stock_cover_days_proxy"].isna().sum())
        cards = st.columns(3)
        cards[0].metric("Rows with sales above opening inventory", f"{flags:,}")
        cards[1].metric("Zero opening inventory rows", f"{int((latest['opening_inventory_units'] == 0).sum()):,}")
        cards[2].metric("Stock-cover proxy unavailable", f"{missing_cover:,}")
        st.dataframe(
            latest[
                ["product_name", "sales_units", "opening_inventory_units", "opening_stock_cover_days_proxy", "sales_exceed_opening_inventory"]
            ].sort_values("opening_stock_cover_days_proxy", na_position="last"),
            hide_index=True,
            width="stretch",
        )
        st.caption("Sales may exceed opening inventory because in-week receipts are unknown.")

with method_tab:
    st.subheader("Coverage and interpretation")
    st.write(
        f"Historical rows: {len(data.products):,} · Observed weeks: {data.portfolio['week_id'].nunique():,} "
        f"· Forecast rows: {len(data.forecast):,}"
    )
    st.write("Revenue and gross profit are recalculated from cleaned sales, unit price, and unit cost. Portfolio margins divide summed gross profit by summed revenue.")
    st.write("Operating profit comes from the workbook and is shown as unavailable for a period if any included row lacks it.")
    st.write("Formal days sales of inventory, days sales outstanding, and customer default risk cannot be calculated from the available workbook fields.")
    st.write("Forecast selection uses rolling historical backtests and development MAE. The final historical check was viewed during model comparison and is not a fresh untouched test.")
    st.download_button(
        "Download selected historical rows (CSV)",
        selected_rows.to_csv(index=False).encode("utf-8"),
        file_name="observed_product_week.csv",
        mime="text/csv",
    )
