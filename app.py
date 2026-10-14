# app.py
"""NetPulse — Streamlit dashboard.

The UI never calls ping/psutil directly; it only reads from SQLite. The
background worker is started exactly once via @st.cache_resource.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

import database as db
from monitor import NetworkMonitor

DB_PATH = "network_monitor.db"
TRAFFIC_LABEL = "Host traffic"
STATUS_EMOJI = {"UP": "🟢", "DOWN": "🔴", "WAITING": "🟡"}

st.set_page_config(
    page_title="NetPulse Network Monitor",
    page_icon="📡",
    layout="wide",
)


@st.cache_resource
def boot() -> NetworkMonitor:
    """Initialise the DB and start the background worker exactly once per process."""
    db.init_db(DB_PATH)
    worker = NetworkMonitor(DB_PATH, traffic_interval=3)
    worker.start()
    return worker


worker = boot()


# --------------------------------------------------------------------------- helpers
def rate(value) -> str:
    """Format a bytes-per-second number into a human readable string."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    for unit in ("B/s", "KB/s", "MB/s"):
        if abs(v) < 1024:
            return f"{v:,.1f} {unit}"
        v /= 1024
    return f"{v:,.1f} GB/s"


def rows_to_df(rows, columns=None) -> pd.DataFrame:
    data = [dict(r) for r in rows]
    df = pd.DataFrame(data)
    if df.empty and columns:
        df = pd.DataFrame(columns=columns)
    return df


def ts(hours_ago: float) -> str:
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def load_samples(since=None) -> pd.DataFrame:
    return rows_to_df(db.get_samples(DB_PATH, since=since))


def latest_traffic_df(minutes: int = 15) -> pd.DataFrame:
    """Most recent host-traffic sample per interface."""
    df = load_samples(since=ts(minutes / 60.0))
    if df.empty or "device_name" not in df.columns:
        return df
    df = df[df["device_name"] == TRAFFIC_LABEL]
    if df.empty:
        return df
    return df.sort_values("id").groupby("interface", as_index=False).last()


def fmt_latency(value) -> str:
    if value is None or not pd.notna(value):
        return "—"
    return f"{float(value):.1f} ms"


# --------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("📡 NetPulse")
    page = st.radio(
        "Navigation",
        ["Overview", "Devices", "Traffic analytics", "Alerts", "About project"],
    )
    refresh_seconds = st.select_slider(
        "Refresh interval (seconds)", options=[5, 10, 15, 30, 60], value=10
    )
    auto_refresh = st.toggle("Auto-refresh", value=True)
    st.caption(f"Worker thread: {'running ✅' if worker.is_alive() else 'stopped ⛔'}")


# --------------------------------------------------------------------------- Overview
if page == "Overview":
    st.subheader("Overview")

    devices = rows_to_df(
        db.get_devices(DB_PATH),
        ["id", "name", "host", "interval_seconds", "enabled", "created_at"],
    )
    latest = rows_to_df(db.get_latest_samples(DB_PATH))
    traffic = latest_traffic_df()

    # Build `merged` with an explicit, stable column set.
    # NOTE: both `devices` and `latest` contain `created_at`, so we rename the
    # one coming from `latest` BEFORE merging to avoid _x/_y suffix collisions.
    if devices.empty:
        merged = pd.DataFrame(
            columns=["name", "host", "status", "latency_ms", "last_checked"]
        )
    elif latest.empty:
        merged = pd.DataFrame(
            {
                "name": devices["name"],
                "host": devices["host"],
                "status": "WAITING",
                "latency_ms": None,
                "last_checked": None,
            }
        )
    else:
        latest_slim = latest[
            ["device_id", "status", "latency_ms", "created_at"]
        ].rename(columns={"created_at": "last_checked"})

        merged = devices.merge(
            latest_slim,
            left_on="id",
            right_on="device_id",
            how="left",
        )[["name", "host", "status", "latency_ms", "last_checked"]].copy()

        merged["status"] = merged["status"].fillna("WAITING")

    total = len(merged)
    online = int((merged["status"] == "UP").sum()) if total else 0
    offline = total - online
    download = float(traffic["rx_bytes_per_sec"].sum()) if not traffic.empty else 0.0
    upload = float(traffic["tx_bytes_per_sec"].sum()) if not traffic.empty else 0.0

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Devices", total)
    m2.metric("Online", online)
    m3.metric("Offline / waiting", offline)
    m4.metric("Download", rate(download))
    m5.metric("Upload", rate(upload))

    st.divider()
    left, right = st.columns([3, 2])

    with left:
        st.markdown("**Device status**")
        if merged.empty:
            st.info("No devices configured yet. Add one on the **Devices** page.")
        else:
            # Build the display frame directly with explicit column names so no
            # rename lookup can fail (avoids KeyError from missing/duplicated names).
            table = pd.DataFrame(
                {
                    "Device": merged["name"],
                    "Host": merged["host"],
                    "Status": merged["status"].map(
                        lambda s: f"{STATUS_EMOJI.get(s, '⚪')} {s}"
                    ),
                    "Latency": merged["latency_ms"].apply(fmt_latency),
                    "Last checked": merged["last_checked"],
                }
            )
            st.dataframe(table, use_container_width=True, hide_index=True)

    with right:
        st.markdown("**Current throughput per interface**")
        if traffic.empty:
            st.info("Waiting for the first traffic samples…")
        else:
            chart = traffic.melt(
                id_vars="interface",
                value_vars=["rx_bytes_per_sec", "tx_bytes_per_sec"],
                var_name="direction",
                value_name="bytes_per_sec",
            )
            chart["direction"] = chart["direction"].map(
                {"rx_bytes_per_sec": "Download", "tx_bytes_per_sec": "Upload"}
            )
            fig = px.bar(
                chart,
                x="interface",
                y="bytes_per_sec",
                color="direction",
                barmode="group",
            )
            fig.update_layout(
                margin=dict(l=0, r=0, t=10, b=0),
                legend_title_text="",
                yaxis_title="bytes/sec",
                xaxis_title="",
            )
            st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.markdown("**Latest alerts**")
    recent_alerts = rows_to_df(db.get_alerts(DB_PATH, limit=5))
    if recent_alerts.empty:
        st.caption("No alerts recorded yet.")
    else:
        st.dataframe(
            recent_alerts[["created_at", "severity", "title", "message"]],
            use_container_width=True,
            hide_index=True,
        )


# --------------------------------------------------------------------------- Devices
elif page == "Devices":
    st.subheader("Devices")

    flash = st.session_state.pop("flash", None)
    if flash:
        st.success(flash)

    with st.form("add_device_form", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 2, 1])
        name = c1.text_input("Device name", placeholder="Office router")
        host = c2.text_input("Host or IP", placeholder="192.168.1.1")
        interval = c3.number_input(
            "Interval (s)", min_value=5, max_value=3600, value=15, step=5
        )
        submitted = st.form_submit_button("Add device")

    if submitted:
        if not name.strip() or not host.strip():
            st.error("Both name and host are required.")
        else:
            try:
                db.add_device(DB_PATH, name.strip(), host.strip(), int(interval))
                st.session_state["flash"] = f"Added {name.strip()} ({host.strip()})"
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except Exception as exc:  # unexpected SQLite error
                st.error(f"Could not add device: {exc}")

    st.divider()

    devices = rows_to_df(
        db.get_devices(DB_PATH),
        ["id", "name", "host", "interval_seconds", "enabled", "created_at"],
    )
    latest = rows_to_df(db.get_latest_samples(DB_PATH))

    status_map: dict = {}
    latency_map: dict = {}
    if not latest.empty:
        for _, row in latest.iterrows():
            did = int(row["device_id"])
            status_map[did] = row["status"]
            latency_map[did] = row["latency_ms"]

    if devices.empty:
        st.info("No devices yet — add one with the form above.")

    for _, device in devices.iterrows():
        did = int(device["id"])
        status = status_map.get(did, "WAITING")

        with st.container(border=True):
            c1, c2, c3 = st.columns([3, 2, 1])
            c1.markdown(f"### {STATUS_EMOJI.get(status, '⚪')} {device['name']}")
            c1.caption(f"`{device['host']}` · every {int(device['interval_seconds'])} s")
            c2.metric("Latency", fmt_latency(latency_map.get(did)))
            c2.caption(f"Status: {status}")
            if c3.button("Remove", key=f"remove_{did}"):
                db.delete_device(DB_PATH, did)
                st.session_state["flash"] = f"Removed {device['name']}"
                st.rerun()


# ----------------------------------------------------------------- Traffic analytics
elif page == "Traffic analytics":
    st.subheader("Traffic analytics")

    c1, c2 = st.columns(2)
    hours = c1.selectbox(
        "History window",
        options=[1, 6, 12, 24, 72, 168],
        index=2,
        format_func=lambda h: f"Last {h} hour(s)",
    )

    samples = load_samples(since=ts(hours))

    traffic_all = pd.DataFrame()
    if not samples.empty and "device_name" in samples.columns:
        traffic_all = samples[samples["device_name"] == TRAFFIC_LABEL]

    if traffic_all.empty:
        interface_names: list = []
    else:
        interface_names = sorted(
            traffic_all["interface"].dropna().unique().tolist()
        )

    choice = c2.selectbox("Interface", ["All interfaces"] + interface_names)

    traffic = traffic_all
    if not traffic.empty and choice != "All interfaces":
        traffic = traffic[traffic["interface"] == choice]

    if traffic.empty:
        st.info("No host-traffic samples in this window yet.")
    else:
        traffic = traffic.copy()
        traffic["created_at"] = pd.to_datetime(traffic["created_at"])
        melted = traffic.melt(
            id_vars=["created_at", "interface"],
            value_vars=["rx_bytes_per_sec", "tx_bytes_per_sec"],
            var_name="direction",
            value_name="bytes_per_sec",
        )
        melted["direction"] = melted["direction"].map(
            {"rx_bytes_per_sec": "Download", "tx_bytes_per_sec": "Upload"}
        )
        melted = melted.sort_values("created_at")

        fig = px.line(
            melted,
            x="created_at",
            y="bytes_per_sec",
            color="direction",
            line_group="interface" if choice == "All interfaces" else None,
            title="Network throughput",
        )
        fig.update_layout(
            legend_title_text="", xaxis_title="", yaxis_title="bytes/sec"
        )
        st.plotly_chart(fig, use_container_width=True)

        last = traffic.sort_values("id").groupby("interface", as_index=False).last()
        d1, d2 = st.columns(2)
        d1.metric("Latest download", rate(last["rx_bytes_per_sec"].sum()))
        d2.metric("Latest upload", rate(last["tx_bytes_per_sec"].sum()))

    st.divider()
    st.markdown("**Device latency**")

    if samples.empty or "device_id" not in samples.columns:
        st.info("No device samples in this window yet.")
    else:
        device_samples = samples[samples["device_id"].notna()].copy()
        if device_samples.empty:
            st.info("No device samples in this window yet.")
        else:
            device_samples["created_at"] = pd.to_datetime(device_samples["created_at"])
            device_samples = device_samples.sort_values("created_at")
            fig2 = px.line(
                device_samples,
                x="created_at",
                y="latency_ms",
                color="device_name",
                markers=True,
                title="Device latency",
            )
            fig2.update_layout(
                legend_title_text="", xaxis_title="", yaxis_title="latency (ms)"
            )
            st.plotly_chart(fig2, use_container_width=True)


# ---------------------------------------------------------------------------- Alerts
elif page == "Alerts":
    st.subheader("Alerts")

    alerts = rows_to_df(db.get_alerts(DB_PATH, limit=200))
    if alerts.empty:
        st.info("No alerts recorded yet.")
    else:
        severities = ["All"] + sorted(alerts["severity"].dropna().unique().tolist())
        selected = st.selectbox("Severity filter", severities)
        view = alerts if selected == "All" else alerts[alerts["severity"] == selected]

        st.metric("Alerts shown", len(view))
        st.dataframe(
            view[["created_at", "severity", "title", "message"]],
            use_container_width=True,
            hide_index=True,
        )


# --------------------------------------------------------------------- About project
elif page == "About project":
    st.subheader("About NetPulse")
    st.markdown(
        """
### How it works

NetPulse has exactly two processes-worth of moving parts inside one Python app:

1. **`monitor.py` — background worker.** A daemon thread is started once when the
   Streamlit process boots (`@st.cache_resource`). Every 3 seconds it:
   * reads `psutil.net_io_counters(pernic=True)`, computes the per-interface
     **bytes/second** from the delta since the previous tick (divided by
     `time.monotonic()` elapsed time) and stores one row per interface with
     `device_name = "Host traffic"`;
   * pings each enabled device whose own `interval_seconds` has elapsed using the
     operating system's `ping` binary (`-n 1 -w <ms>` on Windows, `-c 1 -W <…>` on
     Unix/macOS) and stores an `UP`/`DOWN` sample with the parsed latency;
   * writes an **alert** whenever a device changes state (DOWN → `warning`,
     UP → `success`), and an `error` alert if the loop itself throws.

2. **`app.py` — Streamlit UI.** It never pings and never touches `psutil`. It only
   runs `SELECT` queries against SQLite and renders the results with Pandas and
   Plotly. `database.py` is the single data-access layer; `samples` and `alerts`
   are the shared state between the worker and the UI.

SQLite runs in **WAL** mode with a 10-second busy timeout, so the UI can read
while the worker writes without "database is locked" errors.

### Limitations

* **ICMP may be blocked.** Many corporate networks, cloud VMs and some home
  routers drop ICMP echo requests. A device showing `DOWN` may simply refuse to
  answer pings — it does not necessarily mean the host is offline.
* **Bandwidth is local only.** Traffic numbers come from *this computer's*
  network interfaces. NetPulse cannot see traffic on your router or on other
  machines, and summing multiple interfaces (e.g. Wi-Fi + VPN + Ethernet) can
  double-count tunnelled traffic.
* **Latency parsing is English-only.** The parser looks for `time=12.3 ms` /
  `time<1ms`. On a localized Windows/macOS with translated `ping` output the
  status is still correct, but latency may show as `—`.
* **No history pruning.** Samples accumulate forever; delete
  `network_monitor.db` (or add a cleanup job) if the file grows too large.
* **Authorization.** Only monitor hosts and networks you own or have explicit
  permission to probe. Ping-based scanning of third-party networks may be
  illegal in your jurisdiction.
        """
    )


# ------------------------------------------------------------------- auto refresh
if auto_refresh:
    time.sleep(refresh_seconds)
    st.rerun()