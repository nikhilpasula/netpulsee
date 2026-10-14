# 📡 NetPulse — Real-Time Network Monitoring Dashboard

A local-only network monitor built with **Streamlit**, **psutil**, **Plotly**, and **plain SQLite**.

A background worker pings your devices and samples this computer's interface counters; the dashboard reads everything from SQLite. No cloud services, no API keys, no Docker.

---

## Features

- **Live device uptime** — pings each device on its own interval, records UP/DOWN with latency.
- **Per-interface throughput** — samples `psutil.net_io_counters` every 3 seconds and computes bytes/sec per NIC.
- **State-change alerts** — automatic alerts on UP ↔ DOWN transitions.
- **Traffic analytics** — throughput and device-latency line charts over 1h / 6h / 12h / 24h / 72h / 168h windows.
- **Auto-refresh** — configurable 5–60s refresh in the sidebar.
- **Dark, engineer-native UI** — inspired by Linear, Vercel, Sentry, and Warp (see [Design language](#design-language)).

---

## Project structure
netpulse/
├── app.py # Streamlit UI (reads SQLite only)
├── monitor.py # Background daemon thread: ping + psutil sampling
├── database.py # SQLite data-access layer (all SQL lives here)
├── requirements.txt
├── README.md
├── .gitignore
└── network_monitor.db # created on first run (git-ignored)
