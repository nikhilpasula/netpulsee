# monitor.py
"""Background worker: pings devices and samples host interface counters."""

from __future__ import annotations

import platform
import re
import subprocess
import threading
import time

import psutil

import database as db

_SYSTEM = platform.system().lower()
IS_WINDOWS = _SYSTEM.startswith("win")
IS_DARWIN = _SYSTEM == "darwin"

# Matches both "time=12.3 ms" and "time<1ms"
_TIME_RE = re.compile(r"time[=<]\s*([0-9]+(?:\.[0-9]+)?)\s*ms", re.IGNORECASE)


def ping_host(host: str, timeout_seconds: int = 2):
    """Ping `host` once with the system ping binary.

    Returns (True, latency_ms) on success, (False, None) otherwise.
    """
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_seconds * 1000)), host]
    else:
        # Linux -W is seconds, macOS -W is milliseconds
        wait = str(int(timeout_seconds * 1000)) if IS_DARWIN else str(int(timeout_seconds))
        cmd = ["ping", "-c", "1", "-W", wait, host]

    kwargs = {
        "capture_output": True,
        "text": True,
        "errors": "replace",
        "timeout": timeout_seconds + 3,
    }
    if IS_WINDOWS:
        # Don't flash a console window when running under pythonw / Streamlit
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        result = subprocess.run(cmd, **kwargs)
    except (subprocess.TimeoutExpired, OSError):
        return False, None

    if result.returncode != 0:
        return False, None

    output = (result.stdout or "") + (result.stderr or "")
    match = _TIME_RE.search(output)
    return True, (float(match.group(1)) if match else None)


class NetworkMonitor:
    """Daemon thread that writes samples and alerts into SQLite."""

    def __init__(self, db_path: str = db.DEFAULT_DB, traffic_interval: int = 3):
        self.db_path = db_path
        self.traffic_interval = max(1, int(traffic_interval))
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._last_counters: dict = {}      # nic -> (bytes_recv, bytes_sent)
        self._last_time: float | None = None
        self._last_status: dict = {}        # device_id -> "UP" / "DOWN"
        self._next_check: dict = {}         # device_id -> monotonic deadline
        self._last_error: tuple = (None, 0.0)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread
        self._thread = threading.Thread(
            target=self.run, name="netpulse-monitor", daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------- sampling
    def traffic(self) -> None:
        """Sample per-interface I/O counters and store bytes-per-second deltas."""
        counters = psutil.net_io_counters(pernic=True)
        now = time.monotonic()

        if self._last_time is None:
            # First pass only establishes a baseline (no rate can be computed yet)
            self._last_time = now
            self._last_counters = {
                nic: (c.bytes_recv, c.bytes_sent) for nic, c in counters.items()
            }
            return

        elapsed = max(now - self._last_time, 1e-6)

        for nic, c in counters.items():
            prev_rx, prev_tx = self._last_counters.get(
                nic, (c.bytes_recv, c.bytes_sent)
            )
            rx = max(0.0, (c.bytes_recv - prev_rx) / elapsed)
            tx = max(0.0, (c.bytes_sent - prev_tx) / elapsed)

            db.insert_sample(
                self.db_path,
                device_id=None,
                device_name="Host traffic",
                host=None,
                status="OK",
                latency_ms=None,
                interface=nic,
                rx_bytes_per_sec=rx,
                tx_bytes_per_sec=tx,
            )

        self._last_time = now
        self._last_counters = {
            nic: (c.bytes_recv, c.bytes_sent) for nic, c in counters.items()
        }

    def check_devices(self) -> None:
        """Ping every enabled device whose interval has elapsed; alert on transitions."""
        devices = db.get_devices(self.db_path)
        now = time.monotonic()

        for device in devices:
            device_id = int(device["id"])
            interval = int(device["interval_seconds"] or 15)

            if now < self._next_check.get(device_id, 0.0):
                continue
            self._next_check[device_id] = now + max(1, interval)

            ok, latency = ping_host(device["host"])
            status = "UP" if ok else "DOWN"

            db.insert_sample(
                self.db_path,
                device_id=device_id,
                device_name=device["name"],
                host=device["host"],
                status=status,
                latency_ms=latency,
                interface=None,
                rx_bytes_per_sec=0,
                tx_bytes_per_sec=0,
            )

            previous = self._last_status.get(device_id)
            if previous is not None and previous != status:
                severity = "success" if status == "UP" else "warning"
                db.insert_alert(
                    self.db_path,
                    severity,
                    f"{device['name']} is {status}",
                    f"{device['name']} changed from {previous} to {status}",
                )

            self._last_status[device_id] = status

    # ----------------------------------------------------------------------- loop
    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.traffic()
                self.check_devices()
            except Exception as exc:  # never let the worker die silently
                self._log_error(exc)
            self.stop_event.wait(self.traffic_interval)

    def _log_error(self, exc: Exception) -> None:
        """Record an error alert, throttled to avoid flooding the table."""
        message = f"{type(exc).__name__}: {exc}"
        last_message, last_time = self._last_error
        now = time.monotonic()
        if message == last_message and (now - last_time) < 60:
            return
        self._last_error = (message, now)
        try:
            db.insert_alert(self.db_path, "error", "Monitor error", message)
        except Exception:
            pass