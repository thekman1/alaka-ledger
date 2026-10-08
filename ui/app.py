"""Local-only portfolio dashboard with lazy storage access and explicit valuation gaps."""

import math
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.config import ConfigurationError, Settings
from core.database import Database, DatabaseError
from core.forex_engine import ForexEngine, FxResult
from core.portfolio_engine import (
    PortfolioError,
    PortfolioSummary,
    decimal_value,
    format_money,
    summarize_holdings,
)
from ingestion.base_parser import ParsingError
from ingestion.parsers import CdslCasParser

_COLUMNS = ["ticker", "broker", "asset_class", "native_currency", "total_quantity", "avg_buy_price"]
_PALETTE = ["#087F8C", "#D59C25", "#C65D78", "#487B53", "#6376A8", "#7C7162"]


@st.cache_resource(show_spinner=False)
def forex_service(allow_network: bool) -> ForexEngine:
    """Reuse the locked public FX cache across reruns; never cache holdings globally."""
    return ForexEngine(allow_network=allow_network)


def filter_page(
    frame: pd.DataFrame,
    query: str,
    brokers: List[str],
    currencies: List[str],
    page: int,
    page_size: int,
) -> Tuple[pd.DataFrame, int, int]:
    """Apply literal filters and clamp pagination without mutating the source frame."""
    if page_size <= 0:
        raise ValueError("Page size must be positive.")
    filtered = frame
    if query.strip():
        filtered = filtered[filtered["ticker"].str.contains(query.strip(), case=False, regex=False)]
    if brokers:
        filtered = filtered[filtered["broker"].isin(brokers)]
    if currencies:
        filtered = filtered[filtered["native_currency"].isin(currencies)]
    total = len(filtered)
    pages = max(1, math.ceil(total / page_size))
    selected = min(max(1, page), pages)
    start = (selected - 1) * page_size
    return filtered.iloc[start : start + page_size].copy(), total, pages


def render_distribution(summary: PortfolioSummary) -> None:
    """Draw a cost-basis allocation only when every currency is convertible."""
    st.subheader("Asset Distribution")
    st.caption("Cost basis / INR")
    values = {key: value for key, value in summary.cost_by_asset_inr.items() if value > 0}
    if not values:
        st.info("No complete allocation available.")
        return
    figure = go.Figure(
        data=[go.Pie(
            labels=list(values),
            values=[float(value) for value in values.values()],
            customdata=[format_money(value, "INR") for value in values.values()],
            hole=0.68,
            sort=False,
            marker=dict(colors=_PALETTE, line=dict(color="#FAFCFC", width=2)),
            textinfo="percent",
            hovertemplate="%{label}<br>%{customdata}<extra></extra>",
        )]
    )
    figure.update_layout(
        height=350,
        margin=dict(l=12, r=12, t=12, b=45),
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(family="monospace", color="#20292B", size=12),
        legend=dict(orientation="h", y=-0.08, x=0.5, xanchor="center"),
    )
    st.plotly_chart(figure, use_container_width=True, config={"displayModeBar": False})


def render_metrics(summary: PortfolioSummary, fx: Optional[FxResult]) -> None:
    """Display actual cost values while refusing to mislabel them as market wealth."""
    st.metric("Total Net Worth", "Not valued")
    st.caption("Market prices, cash balances and liabilities are not available.")
    st.metric(
        "Portfolio Cost Basis",
        format_money(summary.total_cost_inr, "INR") if summary.total_cost_inr is not None else "FX unavailable",
    )
    st.subheader("Split by Currency")
    st.caption("Native cost basis")
    for currency, amount in sorted(summary.cost_by_currency.items()):
        st.metric(currency, format_money(amount, currency))
    if fx is not None:
        if fx.quote is None:
            st.warning("USD translation is disabled or unavailable. Native totals remain available.")
        else:
            st.caption(
                f"USD/INR {fx.quote.rate:,.4f} | {fx.status} | "
                f"{fx.quote.observed_at:%Y-%m-%d %H:%M} UTC"
            )
            if fx.status == "stale":
                st.warning("INR cost uses a stale FX quote.")


def render_grid(holdings: List[Dict[str, str]]) -> None:
    """Render literal search, broker/currency filters, and bounded page navigation."""
    st.subheader("Unified Holdings")
    frame = pd.DataFrame(holdings, columns=_COLUMNS)
    query = st.text_input("Ticker", placeholder="Search ticker")
    brokers = st.multiselect("Broker", sorted(frame["broker"].unique().tolist()))
    currencies = st.multiselect("Currency", sorted(frame["native_currency"].unique().tolist()))
    page_size = st.selectbox("Rows per page", [10, 25, 50, 100], index=1)
    _, total, pages = filter_page(frame, query, brokers, currencies, 1, page_size)
    page_key = "holdings_page"
    st.session_state[page_key] = min(max(1, int(st.session_state.get(page_key, 1))), pages)
    page = st.number_input("Page", min_value=1, max_value=pages, step=1, key=page_key)
    visible, _, _ = filter_page(frame, query, brokers, currencies, int(page), page_size)
    visible["avg_buy_price"] = [
        format_money(decimal_value(str(row["avg_buy_price"])), str(row["native_currency"]))
        for _, row in visible.iterrows()
    ]
    visible = visible.rename(columns={
        "ticker": "Ticker", "broker": "Broker", "asset_class": "Asset Class",
        "native_currency": "Currency", "total_quantity": "Quantity", "avg_buy_price": "Average Buy Price",
    })
    st.caption(f"{total:,} holdings | Page {int(page)} of {pages}")
    st.dataframe(visible, hide_index=True, use_container_width=True, height=420)


def clear_cas_preview() -> None:
    """Discard prior parse results and passwords when the selected upload changes."""
    for key in ("cas_preview", "cas_error", "cas_password"):
        st.session_state.pop(key, None)


def parse_cas_upload(content: bytes, suffix: str) -> None:
    """Parse on explicit action and retain only the normalized session preview."""
    password = st.session_state.pop("cas_password", None)
    clear_cas_preview()
    try:
        st.session_state["cas_preview"] = CdslCasParser().parse_bytes(content, suffix, password)
    except ParsingError as error:
        st.session_state["cas_error"] = str(error)


def render_cas_import() -> None:
    """Accept a local CAS upload without changing persisted cost-basis holdings."""
    with st.expander("Import CAS", expanded=True):
        uploaded = st.file_uploader(
            "CAS Statement", type=["pdf", "xlsx"], key="cas_upload",
            on_change=clear_cas_preview,
        )
        if uploaded is not None:
            if uploaded.size > 20 * 1024 * 1024:
                st.error("Select a statement no larger than 20 MiB.")
                return
            suffix = Path(uploaded.name).suffix.lower()
            if suffix == ".pdf":
                st.text_input("PDF Password", type="password", key="cas_password")
            st.button(
                "Parse Statement", on_click=parse_cas_upload,
                args=(uploaded.getvalue(), suffix),
            )
        if st.session_state.get("cas_error"):
            st.error(st.session_state["cas_error"])
        preview = st.session_state.get("cas_preview")
        if preview:
            st.subheader("CAS Snapshot")
            st.caption(f"Unsaved snapshot | {len(preview):,} holdings | Statement values, not acquisition costs")
            frame = pd.DataFrame([
                {
                    "ISIN": row["isin"], "Security": row["security_name"],
                    "Quantity": str(row["quantity"]), "Currency": row["native_currency"],
                    "Statement Price": str(row["price"]) if row["price"] is not None else "-",
                    "Statement Value": str(row["market_value"]) if row["market_value"] is not None else "-",
                    "Source": row["source"],
                }
                for row in preview
            ])
            st.dataframe(frame, hide_index=True, use_container_width=True)


def main() -> None:
    """Load holdings only on explicit action, then retain them in this browser session."""
    st.set_page_config(page_title="Alaka-Ledger", layout="wide")
    st.title("Alaka-Ledger")
    st.caption("Portfolio Overview")
    try:
        settings = Settings.from_environment()
    except ConfigurationError as error:
        st.warning(str(error))
        return
    st.caption("Local storage | " + ("Public FX enabled" if settings.allow_market_data else "Offline mode"))
    identity = str(settings.database_path)
    if st.session_state.get("database_identity") != identity:
        st.session_state.pop("holdings", None)
        st.session_state["database_identity"] = identity
    if st.button("Refresh Holdings"):
        try:
            with st.spinner("Loading holdings..."):
                database = Database(settings.database_path)
                database.initialize()
                st.session_state["holdings"] = database.load_holdings()
        except DatabaseError as error:
            st.session_state.pop("holdings", None)
            st.error(str(error))
            return
    render_cas_import()
    holdings = st.session_state.get("holdings")
    if holdings is None:
        st.info("Portfolio not loaded.")
        return
    if not holdings:
        st.info("No holdings recorded.")
        return
    try:
        fx = forex_service(settings.allow_market_data).usd_inr() if any(
            holding["native_currency"] == "USD" for holding in holdings
        ) else None
        rate = fx.quote.rate if fx is not None and fx.quote is not None else None
        summary = summarize_holdings(holdings, rate)
        render_metrics(summary, fx)
        render_distribution(summary)
        render_grid(holdings)
    except PortfolioError as error:
        st.error(str(error))


if __name__ == "__main__":
    main()