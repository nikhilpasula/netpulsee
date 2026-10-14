# 📡 NetPulse — Real-Time Network Monitoring Dashboard

A local-only network monitor built with **Streamlit**, **psutil**, **Plotly** and
plain **SQLite**. A background worker pings your devices and samples this
computer's interface counters; the dashboard reads everything from SQLite.

No cloud services, no API keys, no Docker.

---

## Project structure
netpulse/
├── app.py # Streamlit UI (reads SQLite only)
├── monitor.py # Background daemon thread: ping + psutil traffic sampling
├── database.py # SQLite data access layer (all SQL lives here)
├── requirements.txt
├── README.md
├── .gitignore
└── network_monitor.db # created on first run (git-ignored)