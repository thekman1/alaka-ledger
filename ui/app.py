"""Local-only portfolio dashboard with lazy storage access and explicit valuation gaps."""

import math
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.config import ConfigurationError, Settings
from core.asset_classifier import classify_mutual_fund
from core.database import Database, DatabaseError
from core.forex_engine import ForexEngine, FxResult
from core.portfolio_engine import (
    PortfolioError,
    PortfolioSummary,
    decimal_value,
    format_money,
    summarize_holdings,
)
from ingestion.base_parser import ParsedHolding, ParsingError
from ingestion.parsers import CdslCasParser
from core.snapshots import SnapshotError, load_saved_holdings, save_statement

_COLUMNS = ["ticker", "broker", "asset_class", "native_currency", "total_quantity", "avg_buy_price"]
_PALETTE = ["#087F8C", "#D59C25", "#C65D78", "#487B53", "#6376A8", "#7C7162"]
_FUND_COLUMN_CONFIG = {
    "Fund Category": st.column_config.TextColumn(
        "Fund Category", help="Broad category inferred offline from the scheme name; not externally verified.",
    ),
    "Scheme Category": st.column_config.TextColumn(
        "Scheme Category", help="Name-derived scheme category. Unknown means the name is insufficient or conflicting.",
    ),
}


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
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})


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
    st.dataframe(visible, hide_index=True, width="stretch", height=420)


def clear_cas_preview() -> None:
    """Discard prior parse results and passwords when the selected upload changes."""
    for key in (
        "cas_preview", "cas_error", "cas_password", "cas_source_sha256", "cas_source_format",
        "cas_statement_date", "cas_complete", "cas_save_success", "cas_save_error",
    ):
        st.session_state.pop(key, None)


def parse_cas_upload(content: bytes, suffix: str) -> None:
    """Parse on explicit action and retain only the normalized session preview."""
    password = st.session_state.pop("cas_password", None)
    clear_cas_preview()
    try:
        parser = CdslCasParser()
        st.session_state["cas_preview"] = parser.parse_bytes(content, suffix, password)
        st.session_state["cas_statement_date"] = parser.statement_date
        st.session_state["cas_source_sha256"] = sha256(content).hexdigest()
        st.session_state["cas_source_format"] = suffix.lower().lstrip(".")
    except ParsingError as error:
        st.session_state["cas_error"] = str(error)


def clear_cas_save_status() -> None:
    """Discard save feedback when its confirmed date or completeness changes."""
    st.session_state.pop("cas_save_success", None)
    st.session_state.pop("cas_save_error", None)


def load_vault_session(database: Database) -> None:
    """Replace session views only after all reads succeed."""
    holdings = database.load_holdings()
    latest = load_saved_holdings(database)
    history = database.list_snapshots()
    st.session_state.update(holdings=holdings, vault_latest=latest, vault_history=history)
    st.session_state.pop("vault_snapshot", None)


def save_cas_preview(database_path: Path) -> None:
    """Save only explicitly confirmed previews, never file contents or passwords."""
    clear_cas_save_status()
    statement_date = st.session_state.get("cas_statement_date")
    if type(statement_date) is not date or not st.session_state.get("cas_complete"):
        st.session_state["cas_save_error"] = "Confirm the statement date and complete account holdings before saving."
        return
    database = Database(database_path)
    try:
        result = save_statement(
            database, st.session_state.get("cas_preview", []), statement_date,
            st.session_state.get("cas_source_sha256", ""), st.session_state.get("cas_source_format", ""),
        )
    except (SnapshotError, DatabaseError) as error:
        st.session_state["cas_save_error"] = str(error)
        return
    if result.saved_accounts:
        message = f"Saved {result.saved_accounts} account snapshot(s) to Alaka Vault."
        if result.duplicate_accounts:
            message += f" {result.duplicate_accounts} unchanged account snapshot(s) skipped."
    else:
        message = "Already saved to Alaka Vault. No holdings were duplicated."
    st.session_state["cas_save_success"] = message
    try:
        load_vault_session(database)
    except DatabaseError:
        st.session_state["cas_save_error"] = "Save completed, but the view could not reload. Select Refresh Holdings."


def account_label(row: ParsedHolding, show_full: bool) -> str:
    """Mask account identifiers unless the user explicitly requests full provenance."""
    account_id, dp_id, client_id = row.get("account_id"), row.get("dp_id"), row.get("client_id")
    if not account_id or not dp_id or not client_id:
        return "Unknown"
    return account_id if show_full else f"DP ****{dp_id[-4:]} / Client ****{client_id[-4:]}"


def broker_label(name: Optional[str]) -> str:
    """Use a familiar display name without modifying reported broker provenance."""
    label = " ".join((name or "").split())
    if label.casefold() == "groww invest tech private limited":
        return "Groww"
    if label.casefold() == "zerodha broking limited":
        return "Zerodha"
    return label or "Unknown"


def cas_preview_frame(preview: Sequence[ParsedHolding], show_account_ids: bool) -> pd.DataFrame:
    """Format snapshot values and account provenance without changing parsed records."""
    fund_categories = [
        classify_mutual_fund(row["security_name"], row.get("asset_class", "Unclassified"))
        for row in preview
    ]
    return pd.DataFrame([
        {
            "Broker / DP": broker_label(row.get("broker")),
            "Account": account_label(row, show_account_ids),
            "ISIN": row["isin"], "Security": row["security_name"],
            "Asset Class": row.get("asset_class", "Unclassified"),
            "Fund Category": category.broad or "-",
            "Scheme Category": category.scheme or "-",
            "Quantity": str(row["quantity"]), "Currency": row["native_currency"],
            "Statement Price": str(row["price"]) if row["price"] is not None else "-",
            "Statement Value": str(row["market_value"]) if row["market_value"] is not None else "-",
            "Source": row["source"],
        }
        for row, category in zip(preview, fund_categories)
    ])


def render_cas_import(database_path: Path) -> None:
    """Accept a local CAS upload without changing persisted cost-basis holdings."""
    with st.expander("Import CAS", expanded=True):
        uploaded = st.file_uploader(
            "CAS Statement", type=["pdf", "csv"], key="cas_upload",
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
            status = "Saved snapshot" if st.session_state.get("cas_save_success") else "Unsaved snapshot"
            st.caption(f"{status} | {len(preview):,} holdings | Statement values, not acquisition costs")
            show_account_ids = st.checkbox("Show full account IDs", value=False)
            missing_identity = any(
                not all(row.get(field) for field in ("account_id", "broker", "dp_id", "client_id"))
                for row in preview
            )
            if missing_identity:
                st.warning("Saving is blocked: some holdings have no complete broker/DP and account identity.")
            frame = cas_preview_frame(preview, show_account_ids)
            st.dataframe(frame, hide_index=True, width="stretch", column_config=_FUND_COLUMN_CONFIG)
            render_cas_save_controls(database_path, missing_identity)


def render_cas_save_controls(database_path: Path, missing_identity: bool) -> None:
    """Require an explicit date and completeness confirmation before saving."""
    statement_date = st.date_input(
        "Statement Date", value=None, min_value=date(1900, 1, 1), max_value=date.today(),
        key="cas_statement_date", on_change=clear_cas_save_status,
        help="Prefilled from a recognized statement date when available; editable before saving. All accounts in this upload must share it.",
    )
    complete = st.checkbox(
        "Complete holdings for every included account on this date", key="cas_complete",
        on_change=clear_cas_save_status,
        help="Partial exports cannot replace a full account snapshot. Verify the preview against the statement.",
    )
    st.button(
        "Save to Vault", icon=":material/save:", on_click=save_cas_preview, args=(database_path,),
        disabled=missing_identity or statement_date is None or not complete,
    )
    if st.session_state.get("cas_save_success"):
        st.success(st.session_state["cas_save_success"])
    if st.session_state.get("cas_save_error"):
        st.error(st.session_state["cas_save_error"])


def render_saved_snapshots(database_path: Path) -> None:
    """Browse latest saved account holdings and immutable historical snapshots."""
    history = st.session_state.get("vault_history", [])
    if not history:
        return
    st.subheader("Saved CAS Holdings")
    show_account_ids = st.checkbox("Show full saved account IDs", key="vault_show_ids")
    choices = {
        int(row["snapshot_id"]): (
            f"{row['statement_date']} | {broker_label(row['broker'])} | "
            f"DP ****{row['dp_id'][-4:]} / Client ****{row['client_id'][-4:]} | #{row['snapshot_id']}"
        ) for row in history
    }
    selected = st.selectbox(
        "Snapshot", [None, *choices], key="vault_snapshot",
        format_func=lambda value: "Latest per account" if value is None else choices[value],
    )
    try:
        rows = st.session_state["vault_latest"] if selected is None else load_saved_holdings(Database(database_path), selected)
    except DatabaseError as error:
        st.error(str(error))
        return
    st.caption(f"{len(rows):,} holdings | Statement valuations, not live prices or acquisition costs")
    frame = cas_preview_frame(rows, show_account_ids)
    frame.insert(0, "Statement Date", [row["statement_date"] for row in rows])
    st.dataframe(frame, hide_index=True, width="stretch", column_config=_FUND_COLUMN_CONFIG)


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
        for key in ("holdings", "vault_latest", "vault_history", "vault_snapshot"):
            st.session_state.pop(key, None)
        clear_cas_save_status()
        st.session_state["database_identity"] = identity
    if st.button("Refresh Holdings"):
        try:
            with st.spinner("Loading holdings..."):
                database = Database(settings.database_path)
                database.initialize()
                load_vault_session(database)
        except DatabaseError as error:
            for key in ("holdings", "vault_latest", "vault_history"):
                st.session_state.pop(key, None)
            st.error(str(error))
            return
    render_cas_import(settings.database_path)
    render_saved_snapshots(settings.database_path)
    render_legacy_portfolio(settings.allow_market_data)


def render_legacy_portfolio(allow_market_data: bool) -> None:
    """Keep transaction cost-basis presentation separate from saved statement valuations."""
    holdings = st.session_state.get("holdings")
    if holdings is None:
        st.info("Portfolio not loaded.")
        return
    if not holdings:
        if not st.session_state.get("vault_latest"):
            st.info("No holdings recorded.")
        return
    try:
        fx = forex_service(allow_market_data).usd_inr() if any(
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