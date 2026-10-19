import os
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import streamlit as st
from dotenv import load_dotenv


load_dotenv()

DEFAULT_API_URL = os.getenv("ADMIN_API_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = httpx.Timeout(15.0, connect=5.0)

st.set_page_config(
    page_title="Mint AI Admin",
    page_icon="🌿",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .stApp { background: radial-gradient(ellipse at 50% -20%, #172439 0, #080b12 54%); }
    [data-testid="stMetric"] {
        background: linear-gradient(145deg, rgba(20,29,42,.96), rgba(12,17,26,.96));
        border: 1px solid #202a39;
        border-radius: 16px;
        padding: 16px 18px;
    }
    [data-testid="stMetricLabel"] { color: #94a3b8; }
    div[data-testid="stExpander"] { border-color: #202a39; border-radius: 14px; }
    </style>
    """,
    unsafe_allow_html=True,
)


class AdminApiError(Exception):
    pass


def api_request(
    method: str,
    path: str,
    api_url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        response = httpx.request(
            method,
            f"{api_url}{path}",
            headers={"X-Admin-Token": token},
            json=payload,
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise AdminApiError(f"Could not reach the Mint AI API: {exc}") from exc

    try:
        result = response.json()
    except ValueError:
        result = {}
    if not response.is_success:
        detail = result.get("detail", response.text) if isinstance(result, dict) else response.text
        raise AdminApiError(f"API returned {response.status_code}: {detail}")
    if not isinstance(result, dict):
        raise AdminApiError("The Mint AI API returned an unexpected response.")
    return result


def decimal_field(value: str, label: str, allow_zero: bool = False) -> str:
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise AdminApiError(f"Enter a valid {label}.") from exc
    if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
        raise AdminApiError(f"Enter a valid {label}.")
    if amount.as_tuple().exponent < -8:
        raise AdminApiError(f"{label.capitalize()} supports up to 8 decimal places.")
    return format(amount, "f")


def show_api_error(exc: AdminApiError) -> None:
    st.error(str(exc))


st.title("Mint AI · Admin")
st.caption("Operations dashboard · connected to the Mint AI API")

if st.session_state.pop("clear_admin_token_input", False):
    st.session_state.pop("admin_token_input", None)

with st.sidebar:
    st.subheader("Connection")
    api_url = st.text_input("API base URL", value=DEFAULT_API_URL).strip().rstrip("/")
    token_default = st.session_state.get("admin_token", "")
    with st.form("admin-login"):
        token = st.text_input(
            "Admin token",
            value=token_default,
            type="password",
            key="admin_token_input",
            help="Sent only to the configured Mint AI API as X-Admin-Token.",
        )
        connect = st.form_submit_button("Connect", type="primary", width="stretch")
    if connect:
        st.session_state.pop("admin_token", None)
        st.session_state.pop("admin_api_url", None)
        if not api_url or not token:
            st.error("Enter both an API URL and admin token.")
        else:
            try:
                api_request("GET", "/admin/metrics", api_url, token)
                st.session_state["admin_token"] = token
                st.session_state["admin_api_url"] = api_url
                st.success("Connected")
            except AdminApiError as exc:
                show_api_error(exc)

    if st.session_state.get("admin_token"):
        if st.button("Disconnect", width="stretch"):
            st.session_state.pop("admin_token", None)
            st.session_state.pop("admin_api_url", None)
            st.session_state["clear_admin_token_input"] = True
            st.rerun()

active_token = st.session_state.get("admin_token", "")
active_api_url = st.session_state.get("admin_api_url") or DEFAULT_API_URL

if not active_token:
    st.info("Connect with the admin token to view metrics and manage withdrawals.")
    st.stop()

if "admin_flash" in st.session_state:
    st.success(st.session_state.pop("admin_flash"))

try:
    metrics = api_request("GET", "/admin/metrics", active_api_url, active_token)
except AdminApiError as exc:
    show_api_error(exc)
    st.stop()

metric_columns = st.columns(2)
metric_columns[0].metric("Registered users", f"{metrics.get('users', 0):,}")
metric_columns[1].metric("Server slots", f"{metrics.get('server_slots', 0):,}")

st.subheader("Financial summary")
financial_rows = metrics.get("financial_by_currency", [])
if financial_rows:
    st.dataframe(financial_rows, width="stretch", hide_index=True)
else:
    st.info("No financial activity has been recorded yet.")
st.caption(
    metrics.get(
        "note",
        "Amounts are grouped by currency and are not converted. This is not audited accounting.",
    )
)

st.divider()
withdrawal_heading, refresh_column = st.columns([5, 1])
withdrawal_heading.subheader("Withdrawal operations")
if refresh_column.button("Refresh", width="stretch"):
    st.rerun()
st.caption(
    "Only TON withdrawals awaiting payout reconciliation appear here."
)

try:
    withdrawal_data = api_request(
        "GET", "/admin/withdrawals", active_api_url, active_token
    )
except AdminApiError as exc:
    show_api_error(exc)
    st.stop()

withdrawals = withdrawal_data.get("withdrawals", [])
if not withdrawals:
    st.success("No withdrawals are waiting for manual review.")

for withdrawal in withdrawals:
    withdrawal_id = str(withdrawal.get("withdrawal_id", ""))
    currency = str(withdrawal.get("currency", ""))
    heading = (
        f"{currency} · {withdrawal.get('gross_amount', '—')} gross · "
        f"user {withdrawal.get('user_id', '—')}"
    )
    with st.expander(heading, expanded=False):
        detail_columns = st.columns(3)
        detail_columns[0].write(f"**Withdrawal ID**  \n`{withdrawal_id}`")
        detail_columns[1].write(
            f"**Quoted payout**  \n"
            f"{withdrawal.get('payout_amount_before_settlement', '—')} {currency}"
        )
        detail_columns[2].write(
            f"**Platform fee**  \n{withdrawal.get('platform_fee', '—')} {currency}"
        )
        st.write(f"**Wallet:** `{withdrawal.get('wallet_address', '—')}`")
        st.write(f"**Requested:** {withdrawal.get('created_at', '—')}")

        if currency == "TON":
            st.caption(
                "The reconciled payout must equal the gross amount less the platform fee and actual TON network fee."
            )
            with st.form(f"reconcile-{withdrawal_id}"):
                st.markdown("**Reconcile actual crypto payout and gas**")
                payout_amount = st.text_input(
                    f"Net payout amount ({currency})",
                    placeholder="Required",
                    key=f"payout-{withdrawal_id}",
                )
                network_fee_ton = st.text_input(
                    "Actual network fee (TON)",
                    value="0",
                    key=f"reconcile-fee-{withdrawal_id}",
                )
                submitted = st.form_submit_button(
                    "Reconcile and complete", type="primary"
                )
            if submitted:
                try:
                    payload = {
                        "payout_amount": decimal_field(
                            payout_amount, "net payout amount"
                        ),
                        "network_fee_ton": decimal_field(
                            network_fee_ton, "network fee", allow_zero=True
                        ),
                    }
                    api_request(
                        "POST",
                        f"/admin/withdrawals/{withdrawal_id}/reconcile-network-fee",
                        active_api_url,
                        active_token,
                        payload,
                    )
                    st.session_state["admin_flash"] = (
                        "Network-fee reconciliation recorded."
                    )
                    st.rerun()
                except AdminApiError as exc:
                    show_api_error(exc)
        else:
            st.warning(f"Legacy {currency!r} withdrawals cannot be settled by the TON-only flow.")
