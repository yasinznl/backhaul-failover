#!/usr/bin/env python3
import os
import time
import json
import shlex
import subprocess
from typing import List, Tuple, Optional
from flask import Flask, request, Response, redirect, url_for, render_template_string, jsonify

# -----------------------
# Config
# -----------------------
FAILOVER_BIN = "/usr/local/bin/backhaul-failover.sh"
SERVICE = "backhaul-failover.service"
TIMER = "backhaul-failover.timer"
WEB_SERVICE = "backhaul-failover-web.service"

USER = os.environ.get("BH_WEB_USER", "admin")
PASS = os.environ.get("BH_WEB_PASS", "admin")
PORT = int(os.environ.get("BH_WEB_PORT", "8088"))

TRAFFIC_DIR = os.environ.get("BH_TRAFFIC_DIR", "/run/backhaul-traffic")
METRICS_POINTS = int(os.environ.get("BH_METRICS_POINTS", "180"))  # ~180 minutes if 1/min, or ~6 min if 2s sample
METRICS_POLL_SEC = float(os.environ.get("BH_METRICS_POLL_SEC", "2.0"))  # browser polling interval (JS)

# -----------------------
# App
# -----------------------
app = Flask(__name__)

def run(cmd: List[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)

def is_active(unit: str) -> bool:
    return run(["systemctl", "is-active", "--quiet", unit]).returncode == 0

def ok_auth(auth) -> bool:
    return auth and auth.username == USER and auth.password == PASS

def need_auth():
    return Response("Auth required", 401, {"WWW-Authenticate": "Basic realm='Backhaul Panel'"})

@app.before_request
def protect():
    # basic auth everywhere
    auth = request.authorization
    if not ok_auth(auth):
        return need_auth()

# -----------------------
# Helpers: Tunnels
# -----------------------
def failover_list() -> Tuple[List[Tuple[str, str, str, str]], str]:
    """
    Returns rows: [(service, active, port, toml)]
    and raw output.
    """
    p = run([FAILOVER_BIN, "--list"], timeout=15)
    raw = p.stdout or ""
    rows: List[Tuple[str, str, str, str]] = []
    lines = raw.splitlines()
    if len(lines) <= 1:
        return rows, raw

    # Expect header: SERVICE ACTIVE PORT TOML
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 4:
            svc, active, port = parts[0], parts[1], parts[2]
            toml = " ".join(parts[3:])
            rows.append((svc, active, port, toml))
        elif len(parts) >= 3:
            svc, active, port = parts[0], parts[1], parts[2]
            rows.append((svc, active, port, "-"))
    return rows, raw

def failover_current() -> Tuple[str, str]:
    p = run([FAILOVER_BIN, "--current"], timeout=10)
    s = (p.stdout or "").strip()
    if not s:
        return "-", "-"
    parts = s.split()
    if len(parts) >= 2:
        return parts[0], parts[1]
    return parts[0], "-"

# -----------------------
# Helpers: Traffic timeseries
# -----------------------
def traffic_log_path(port: str) -> str:
    return os.path.join(TRAFFIC_DIR, f"port-{port}.log")

def read_traffic_points(port: str, max_points: int = 200) -> List[Tuple[int, int]]:
    """
    Returns list of (ts, bytes_sum) from /run/backhaul-traffic/port-PORT.log
    """
    path = traffic_log_path(port)
    if not os.path.exists(path):
        return []
    pts: List[Tuple[int, int]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-max_points:]
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            parts = ln.split()
            if len(parts) >= 2:
                try:
                    ts = int(float(parts[0]))
                    b = int(float(parts[1]))
                    pts.append((ts, b))
                except Exception:
                    continue
    except Exception:
        return []
    return pts

def points_to_bps(points: List[Tuple[int, int]]) -> List[Tuple[int, float]]:
    """
    Convert (ts, bytes) to (ts, bytes_per_sec) using discrete derivative.
    """
    if len(points) < 2:
        return []
    out: List[Tuple[int, float]] = []
    prev_ts, prev_b = points[0]
    for ts, b in points[1:]:
        dt = ts - prev_ts
        db = b - prev_b
        if dt <= 0 or db < 0:
            prev_ts, prev_b = ts, b
            continue
        out.append((ts, db / dt))
        prev_ts, prev_b = ts, b
    return out

def human_bps(bps: float) -> str:
    if bps < 1024:
        return f"{bps:.0f} B/s"
    kb = bps / 1024
    if kb < 1024:
        return f"{kb:.1f} KB/s"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.2f} MB/s"
    gb = mb / 1024
    return f"{gb:.2f} GB/s"

# -----------------------
# Live logs via SSE
# -----------------------
def sse_format(data: str, event: Optional[str] = None) -> str:
    # Minimal SSE message
    msg = ""
    if event:
        msg += f"event: {event}\n"
    for line in data.splitlines():
        msg += f"data: {line}\n"
    msg += "\n"
    return msg

@app.get("/api/logs/live")
def api_logs_live():
    unit = request.args.get("unit", SERVICE)
    lines = int(request.args.get("lines", "80"))

    def generate():
        # Tail first:
        try:
            tail = run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"], timeout=10).stdout or ""
            for ln in tail.splitlines():
                yield sse_format(ln)
        except Exception as e:
            yield sse_format(f"[tail error] {e}")

        # Follow:
        cmd = ["journalctl", "-u", unit, "-f", "-o", "cat", "--no-pager"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            while True:
                if proc.stdout is None:
                    break
                line = proc.stdout.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                yield sse_format(line.rstrip("\n"))
        except GeneratorExit:
            # client disconnected
            return
        except Exception as e:
            yield sse_format(f"[follow error] {e}")

    return Response(generate(), mimetype="text/event-stream")

# -----------------------
# Metrics API
# -----------------------
@app.get("/api/metrics")
def api_metrics():
    # Primary + port
    primary_svc, primary_port = failover_current()

    # Load timeseries (bytes derivative)
    bps_series = []
    kpi_now = 0.0
    kpi_1m = 0.0
    kpi_5m = 0.0

    if primary_port and primary_port != "-":
        pts = read_traffic_points(primary_port, max_points=METRICS_POINTS)
        bps_pts = points_to_bps(pts)
        # Provide last N points (ts, bps)
        bps_series = [{"t": ts, "bps": bps} for ts, bps in bps_pts[-METRICS_POINTS:]]

        # KPIs
        if bps_pts:
            kpi_now = bps_pts[-1][1]

            # avg last 1m (approx last 60s)
            now_ts = bps_pts[-1][0]
            last_60 = [v for (t, v) in bps_pts if t >= now_ts - 60]
            if last_60:
                kpi_1m = sum(last_60) / len(last_60)

            # avg last 5m
            last_300 = [v for (t, v) in bps_pts if t >= now_ts - 300]
            if last_300:
                kpi_5m = sum(last_300) / len(last_300)

    # system states
    data = {
        "primary": {"service": primary_svc, "port": primary_port},
        "units": {
            "failover_service": "active" if is_active(SERVICE) else "inactive",
            "timer": "active" if is_active(TIMER) else "inactive",
            "web": "active" if is_active(WEB_SERVICE) else "inactive",
        },
        "traffic": {
            "series": bps_series,
            "now": {"bps": kpi_now, "human": human_bps(kpi_now)},
            "avg1m": {"bps": kpi_1m, "human": human_bps(kpi_1m)},
            "avg5m": {"bps": kpi_5m, "human": human_bps(kpi_5m)},
        },
        "poll_sec": METRICS_POLL_SEC,
    }
    return jsonify(data)

# -----------------------
# Actions API
# -----------------------
@app.post("/api/action")
def api_action():
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip()
    svc = (payload.get("service") or "").strip()

    allowed_actions = {
        "run_once",
        "timer_on", "timer_off",
        "web_restart",
        "switch",
        "tunnel_start", "tunnel_stop", "tunnel_restart",
    }
    if action not in allowed_actions:
        return jsonify({"ok": False, "error": "Invalid action"}), 400

    try:
        if action == "run_once":
            out = run(["systemctl", "start", SERVICE], timeout=15).stdout
            return jsonify({"ok": True, "output": out or "Ran once"})

        if action == "timer_on":
            out = run(["systemctl", "enable", "--now", TIMER], timeout=15).stdout
            return jsonify({"ok": True, "output": out or "Timer enabled"})

        if action == "timer_off":
            out = run(["systemctl", "disable", "--now", TIMER], timeout=15).stdout
            return jsonify({"ok": True, "output": out or "Timer disabled"})

        if action == "web_restart":
            out = run(["systemctl", "restart", WEB_SERVICE], timeout=15).stdout
            return jsonify({"ok": True, "output": out or "Web restarted"})

        # Tunnel actions require service name
        if action in {"switch", "tunnel_start", "tunnel_stop", "tunnel_restart"} and not svc:
            return jsonify({"ok": False, "error": "Missing service"}), 400

        if action == "switch":
            out = run([FAILOVER_BIN, "--switch", svc], timeout=60).stdout
            return jsonify({"ok": True, "output": out[-6000:] if out else "OK"})

        if action == "tunnel_start":
            out = run([FAILOVER_BIN, "--start", svc], timeout=20).stdout
            return jsonify({"ok": True, "output": out[-4000:] if out else f"Started {svc}"})

        if action == "tunnel_stop":
            out = run([FAILOVER_BIN, "--stop", svc], timeout=20).stdout
            return jsonify({"ok": True, "output": out[-4000:] if out else f"Stopped {svc}"})

        if action == "tunnel_restart":
            out = run([FAILOVER_BIN, "--restart", svc], timeout=30).stdout
            return jsonify({"ok": True, "output": out[-4000:] if out else f"Restarted {svc}"})

        return jsonify({"ok": False, "error": "Unhandled"}), 500

    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Command timeout"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# -----------------------
# UI
# -----------------------
TPL = r"""
<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Backhaul Command Center</title>

  <!-- Bootstrap -->
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.rtl.min.css" rel="stylesheet">
  <!-- Icons -->
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <!-- Chart.js -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>

  <style>
    :root{
      --bg0:#070B14;
      --bg1:#0B1220;
      --card:#0F1930;
      --card2:#0C162B;
      --line:#223055;
      --txt:#E8EEFF;
      --muted:#9FB0D8;
      --good:#22c55e;
      --warn:#f59e0b;
      --bad:#ef4444;
      --accent:#7c3aed;
      --accent2:#06b6d4;
      --shadow: 0 10px 30px rgba(0,0,0,.35);
      --radius:18px;
    }
    body{
      background: radial-gradient(1200px 700px at 20% 10%, rgba(124,58,237,.25), transparent 55%),
                  radial-gradient(900px 600px at 80% 30%, rgba(6,182,212,.18), transparent 60%),
                  linear-gradient(180deg, var(--bg0), var(--bg1));
      color: var(--txt);
      min-height: 100vh;
    }
    .topbar{
      position: sticky; top: 0; z-index: 50;
      backdrop-filter: blur(10px);
      background: rgba(7,11,20,.45);
      border-bottom: 1px solid rgba(34,48,85,.6);
    }
    .brand{
      letter-spacing: .3px;
      font-weight: 800;
    }
    .chip{
      border: 1px solid rgba(34,48,85,.8);
      background: rgba(15,25,48,.7);
      border-radius: 999px;
      padding: .35rem .7rem;
      display: inline-flex;
      align-items: center;
      gap: .45rem;
      font-size: .85rem;
      color: var(--muted);
    }
    .dot{ width:10px; height:10px; border-radius:50%; display:inline-block; }
    .dot.good{ background: var(--good); box-shadow: 0 0 0 4px rgba(34,197,94,.12); }
    .dot.warn{ background: var(--warn); box-shadow: 0 0 0 4px rgba(245,158,11,.12); }
    .dot.bad{  background: var(--bad);  box-shadow: 0 0 0 4px rgba(239,68,68,.12); }

    .panel{
      background: linear-gradient(180deg, rgba(15,25,48,.95), rgba(12,22,43,.95));
      border: 1px solid rgba(34,48,85,.75);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }
    .panel .panel-h{
      padding: 14px 16px;
      border-bottom: 1px solid rgba(34,48,85,.6);
      display:flex; align-items:center; justify-content:space-between; gap:10px;
    }
    .panel .panel-b{ padding: 14px 16px; }

    .btn-soft{
      border-radius: 14px;
      border: 1px solid rgba(34,48,85,.8);
      background: rgba(17,26,46,.55);
      color: var(--txt);
    }
    .btn-soft:hover{ border-color: rgba(124,58,237,.9); }
    .btn-accent{
      border-radius: 14px;
      border: 1px solid rgba(124,58,237,.8);
      background: rgba(124,58,237,.18);
      color: var(--txt);
    }
    .btn-accent:hover{ background: rgba(124,58,237,.28); }
    .btn-danger-soft{
      border-radius: 14px;
      border: 1px solid rgba(239,68,68,.6);
      background: rgba(239,68,68,.12);
      color: var(--txt);
    }

    .kpi{
      border: 1px solid rgba(34,48,85,.75);
      background: rgba(11,18,32,.55);
      border-radius: 16px;
      padding: 12px;
    }
    .kpi .label{ color: var(--muted); font-size: .85rem; }
    .kpi .value{ font-size: 1.25rem; font-weight: 800; letter-spacing:.2px; }
    .kpi .sub{ color: var(--muted); font-size: .8rem; margin-top:2px; }

    .table-dark{
      --bs-table-bg: transparent;
      --bs-table-color: var(--txt);
      --bs-table-border-color: rgba(34,48,85,.6);
    }
    .table thead th{
      color: var(--muted);
      font-weight: 700;
      border-bottom: 1px solid rgba(34,48,85,.8) !important;
    }
    .table tbody td{
      border-top: 1px solid rgba(34,48,85,.45) !important;
      vertical-align: middle;
    }

    .logbox{
      height: 360px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12.5px;
      line-height: 1.45;
      background: rgba(7,11,20,.65);
      border: 1px solid rgba(34,48,85,.75);
      border-radius: 16px;
      padding: 12px;
      white-space: pre-wrap;
    }
    .logbox .muted{ color: rgba(159,176,216,.85); }

    .nav-pills .nav-link{
      color: var(--muted);
      border-radius: 14px;
      border: 1px solid rgba(34,48,85,.55);
      background: rgba(15,25,48,.35);
    }
    .nav-pills .nav-link.active{
      color: var(--txt);
      border-color: rgba(124,58,237,.95);
      background: rgba(124,58,237,.2);
    }

    .toast-container{ z-index: 9999; }
    .toast{
      background: rgba(15,25,48,.95);
      border: 1px solid rgba(34,48,85,.85);
      color: var(--txt);
      border-radius: 16px;
      box-shadow: var(--shadow);
    }

    @media (max-width: 576px){
      .logbox{ height: 300px; }
      .kpi .value{ font-size: 1.05rem; }
    }
  </style>
</head>

<body>
  <div class="topbar py-3">
    <div class="container d-flex flex-wrap align-items-center justify-content-between gap-2">
      <div class="d-flex align-items-center gap-2">
        <i class="bi bi-cpu-fill" style="font-size:1.3rem;color:rgba(124,58,237,.95)"></i>
        <div>
          <div class="brand">Backhaul Command Center</div>
          <div class="small" style="color:var(--muted)">failover • tunnels • logs • traffic</div>
        </div>
      </div>

      <div class="d-flex flex-wrap gap-2">
        <span class="chip"><span id="dot-service" class="dot warn"></span> Failover <span id="st-service">…</span></span>
        <span class="chip"><span id="dot-timer" class="dot warn"></span> Timer <span id="st-timer">…</span></span>
        <span class="chip"><span id="dot-web" class="dot warn"></span> Web <span id="st-web">…</span></span>
      </div>
    </div>
  </div>

  <div class="container py-4">
    <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
      <ul class="nav nav-pills gap-2" id="tabs">
        <li class="nav-item"><button class="nav-link active" data-bs-toggle="pill" data-bs-target="#tab-dash" type="button">داشبورد</button></li>
        <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-tunnels" type="button">تانل‌ها</button></li>
        <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-logs" type="button">لاگ‌ها</button></li>
      </ul>

      <div class="d-flex gap-2">
        <button class="btn btn-soft btn-sm" onclick="act('run_once')"><i class="bi bi-play-fill"></i> اجرای یک‌بار</button>
        <button class="btn btn-accent btn-sm" onclick="act('timer_on')"><i class="bi bi-lightning-charge-fill"></i> Timer On</button>
        <button class="btn btn-soft btn-sm" onclick="act('timer_off')"><i class="bi bi-pause-fill"></i> Timer Off</button>
        <button class="btn btn-soft btn-sm" onclick="act('web_restart')"><i class="bi bi-arrow-repeat"></i> Restart Web</button>
      </div>
    </div>

    <div class="tab-content">
      <!-- DASH -->
      <div class="tab-pane fade show active" id="tab-dash">
        <div class="row g-3">
          <div class="col-12 col-lg-5">
            <div class="panel">
              <div class="panel-h">
                <div class="d-flex align-items-center gap-2">
                  <i class="bi bi-hdd-network-fill" style="color:rgba(6,182,212,.95)"></i>
                  <div class="fw-bold">Primary</div>
                </div>
                <div class="small" style="color:var(--muted)">شناسه اصلی و وضعیت</div>
              </div>
              <div class="panel-b">
                <div class="kpi mb-2">
                  <div class="label">Service</div>
                  <div class="value" id="primary-svc">…</div>
                  <div class="sub">Port: <span id="primary-port">…</span></div>
                </div>

                <div class="row g-2">
                  <div class="col-4">
                    <div class="kpi">
                      <div class="label">Now</div>
                      <div class="value" id="kpi-now">…</div>
                      <div class="sub">لحظه‌ای</div>
                    </div>
                  </div>
                  <div class="col-4">
                    <div class="kpi">
                      <div class="label">Avg 1m</div>
                      <div class="value" id="kpi-1m">…</div>
                      <div class="sub">میانگین ۱ دقیقه</div>
                    </div>
                  </div>
                  <div class="col-4">
                    <div class="kpi">
                      <div class="label">Avg 5m</div>
                      <div class="value" id="kpi-5m">…</div>
                      <div class="sub">میانگین ۵ دقیقه</div>
                    </div>
                  </div>
                </div>

                <div class="mt-3 d-flex gap-2">
                  <button class="btn btn-accent btn-sm" onclick="refreshAll()"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
                  <button class="btn btn-soft btn-sm" onclick="scrollChartToEnd()"><i class="bi bi-graph-up"></i> Focus Now</button>
                </div>
              </div>
            </div>
          </div>

          <div class="col-12 col-lg-7">
            <div class="panel">
              <div class="panel-h">
                <div class="d-flex align-items-center gap-2">
                  <i class="bi bi-activity" style="color:rgba(124,58,237,.95)"></i>
                  <div class="fw-bold">Live Traffic</div>
                </div>
                <div class="small" style="color:var(--muted)">نمودار زنده مصرف (B/s)</div>
              </div>
              <div class="panel-b">
                <canvas id="chart" height="120"></canvas>
                <div class="small mt-2" style="color:var(--muted)">
                  نکته: نمودار بر اساس لاگ‌های /run/backhaul-traffic ساخته می‌شود.
                </div>
              </div>
            </div>
          </div>

          <div class="col-12">
            <div class="panel">
              <div class="panel-h">
                <div class="d-flex align-items-center gap-2">
                  <i class="bi bi-terminal-fill" style="color:rgba(245,158,11,.95)"></i>
                  <div class="fw-bold">Action Output</div>
                </div>
                <div class="small" style="color:var(--muted)">خروجی آخرین عملیات</div>
              </div>
              <div class="panel-b">
                <div class="logbox" id="outbox"><span class="muted">Ready.</span></div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- TUNNELS -->
      <div class="tab-pane fade" id="tab-tunnels">
        <div class="panel">
          <div class="panel-h">
            <div class="d-flex align-items-center gap-2">
              <i class="bi bi-diagram-3-fill" style="color:rgba(6,182,212,.95)"></i>
              <div class="fw-bold">Tunnel Manager</div>
            </div>
            <button class="btn btn-soft btn-sm" onclick="loadTunnels()"><i class="bi bi-arrow-clockwise"></i> Reload</button>
          </div>
          <div class="panel-b">
            <div class="table-responsive">
              <table class="table table-dark table-sm align-middle" id="tunnels-table">
                <thead>
                  <tr>
                    <th>Service</th>
                    <th>Active</th>
                    <th>Port</th>
                    <th>Toml</th>
                    <th style="width: 260px;">Actions</th>
                  </tr>
                </thead>
                <tbody id="tunnels-body">
                  <tr><td colspan="5" style="color:var(--muted)">Loading…</td></tr>
                </tbody>
              </table>
            </div>
            <div class="small" style="color:var(--muted)">
              Tip: Switch فقط وقتی انجام می‌شود که candidate واقعاً healthy شود (طبق منطق اسکریپت).
            </div>
          </div>
        </div>
      </div>

      <!-- LOGS -->
      <div class="tab-pane fade" id="tab-logs">
        <div class="row g-3">
          <div class="col-12 col-lg-6">
            <div class="panel">
              <div class="panel-h">
                <div class="d-flex align-items-center gap-2">
                  <i class="bi bi-journal-text" style="color:rgba(245,158,11,.95)"></i>
                  <div class="fw-bold">Logs (Tail)</div>
                </div>
                <button class="btn btn-soft btn-sm" onclick="loadTail()"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
              </div>
              <div class="panel-b">
                <div class="logbox" id="tailbox"><span class="muted">Loading…</span></div>
              </div>
            </div>
          </div>

          <div class="col-12 col-lg-6">
            <div class="panel">
              <div class="panel-h">
                <div class="d-flex align-items-center gap-2">
                  <i class="bi bi-broadcast" style="color:rgba(34,197,94,.95)"></i>
                  <div class="fw-bold">Logs (Live)</div>
                </div>
                <div class="d-flex gap-2">
                  <button class="btn btn-accent btn-sm" onclick="startLive()"><i class="bi bi-play-fill"></i> Start</button>
                  <button class="btn btn-soft btn-sm" onclick="stopLive()"><i class="bi bi-stop-fill"></i> Stop</button>
                </div>
              </div>
              <div class="panel-b">
                <div class="logbox" id="livebox"><span class="muted">Click Start to stream live logs.</span></div>
              </div>
            </div>
          </div>
        </div>
      </div>

    </div>
  </div>

  <!-- Toasts -->
  <div class="toast-container position-fixed bottom-0 start-0 p-3">
    <div id="toast" class="toast" role="alert" aria-live="assertive" aria-atomic="true">
      <div class="toast-body" id="toastBody">…</div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>

  <script>
    const outbox = document.getElementById('outbox');
    const tailbox = document.getElementById('tailbox');
    const livebox = document.getElementById('livebox');

    let liveSource = null;

    function toast(msg){
      document.getElementById('toastBody').textContent = msg;
      const t = new bootstrap.Toast(document.getElementById('toast'), {delay: 2200});
      t.show();
    }

    function appendOut(text){
      if(!text) return;
      outbox.textContent = (text + "\\n\\n" + outbox.textContent).slice(0, 12000);
    }

    async function act(action, service=null){
      try{
        toast("Executing…");
        const res = await fetch('/api/action', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({action, service})
        });
        const j = await res.json();
        if(!j.ok){
          toast("Error: " + (j.error || "unknown"));
          appendOut("[ERROR] " + (j.error || "unknown"));
          return;
        }
        toast("Done");
        appendOut(j.output || "OK");
        await refreshAll();
        await loadTunnels();
      }catch(e){
        toast("Network/Server error");
        appendOut("[EXCEPTION] " + e);
      }
    }

    // --------- Tunnels ----------
    async function loadTunnels(){
      try{
        // Reuse server-render list by hitting /api/action? no. We'll call a tiny helper:
        const res = await fetch('/api/tunnels');
        const j = await res.json();
        const body = document.getElementById('tunnels-body');
        body.innerHTML = "";
        if(!j.rows || j.rows.length === 0){
          body.innerHTML = `<tr><td colspan="5" style="color:var(--muted)">No tunnels found.</td></tr>`;
          return;
        }
        for(const r of j.rows){
          const badge = r.active === 'yes'
            ? `<span class="badge" style="background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.45);color:var(--txt)">ACTIVE</span>`
            : `<span class="badge" style="background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.35);color:var(--txt)">OFF</span>`;

          body.insertAdjacentHTML('beforeend', `
            <tr>
              <td style="white-space:nowrap">${escapeHtml(r.service)}</td>
              <td>${badge}</td>
              <td>${escapeHtml(r.port)}</td>
              <td style="max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted)">${escapeHtml(r.toml)}</td>
              <td class="d-flex flex-wrap gap-2">
                <button class="btn btn-accent btn-sm" onclick="act('switch', '${escapeAttr(r.service)}')"><i class="bi bi-shuffle"></i> Switch</button>
                <button class="btn btn-soft btn-sm" onclick="act('tunnel_start', '${escapeAttr(r.service)}')">Start</button>
                <button class="btn btn-soft btn-sm" onclick="act('tunnel_restart', '${escapeAttr(r.service)}')">Restart</button>
                <button class="btn btn-danger-soft btn-sm" onclick="act('tunnel_stop', '${escapeAttr(r.service)}')">Stop</button>
              </td>
            </tr>
          `);
        }
      }catch(e){
        appendOut("[tunnels error] " + e);
      }
    }

    function escapeHtml(s){ return (s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"); }
    function escapeAttr(s){ return (s||"").replaceAll("'","\\'"); }

    // --------- Logs ----------
    async function loadTail(){
      try{
        const res = await fetch('/api/logs/tail');
        const j = await res.json();
        tailbox.textContent = j.text || "";
        tailbox.scrollTop = tailbox.scrollHeight;
      }catch(e){
        tailbox.textContent = "[tail error] " + e;
      }
    }

    function startLive(){
      stopLive();
      livebox.textContent = "";
      liveSource = new EventSource('/api/logs/live?unit={{SERVICE}}&lines=80');
      liveSource.onmessage = (ev) => {
        livebox.textContent += ev.data + "\\n";
        if(livebox.textContent.length > 20000){
          livebox.textContent = livebox.textContent.slice(-18000);
        }
        livebox.scrollTop = livebox.scrollHeight;
      };
      liveSource.onerror = () => {
        livebox.textContent += "\\n[stream] disconnected\\n";
      };
      toast("Live logs started");
    }

    function stopLive(){
      if(liveSource){
        liveSource.close();
        liveSource = null;
        toast("Live logs stopped");
      }
    }

    // --------- Chart ----------
    let chart = null;
    let lastSeriesLen = 0;

    function makeChart(){
      const ctx = document.getElementById('chart');
      chart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: [],
          datasets: [{
            label: 'Traffic (B/s)',
            data: [],
            borderWidth: 2,
            tension: 0.25,
            pointRadius: 0,
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: '#9FB0D8' } },
            tooltip: {
              callbacks: {
                label: (ctx) => {
                  const v = ctx.raw || 0;
                  return ' ' + humanBps(v);
                }
              }
            }
          },
          scales: {
            x: { ticks: { color: '#9FB0D8' }, grid: { color: 'rgba(34,48,85,.25)' } },
            y: { ticks: { color: '#9FB0D8' }, grid: { color: 'rgba(34,48,85,.25)' } }
          }
        }
      });
    }

    function humanBps(bps){
      if(bps < 1024) return `${bps.toFixed(0)} B/s`;
      const kb = bps/1024;
      if(kb < 1024) return `${kb.toFixed(1)} KB/s`;
      const mb = kb/1024;
      if(mb < 1024) return `${mb.toFixed(2)} MB/s`;
      const gb = mb/1024;
      return `${gb.toFixed(2)} GB/s`;
    }

    function setStatusDot(idDot, idText, state){
      const dot = document.getElementById(idDot);
      const t = document.getElementById(idText);
      t.textContent = state;
      dot.classList.remove('good','warn','bad');
      if(state === 'active'){ dot.classList.add('good'); }
      else { dot.classList.add('bad'); }
    }

    async function refreshAll(){
      try{
        const res = await fetch('/api/metrics');
        const m = await res.json();

        document.getElementById('primary-svc').textContent = m.primary.service || '-';
        document.getElementById('primary-port').textContent = m.primary.port || '-';

        setStatusDot('dot-service','st-service', m.units.failover_service);
        setStatusDot('dot-timer','st-timer', m.units.timer);
        setStatusDot('dot-web','st-web', m.units.web);

        document.getElementById('kpi-now').textContent = m.traffic.now.human;
        document.getElementById('kpi-1m').textContent = m.traffic.avg1m.human;
        document.getElementById('kpi-5m').textContent = m.traffic.avg5m.human;

        // Update chart
        const series = m.traffic.series || [];
        const labels = series.map(x => {
          const d = new Date(x.t * 1000);
          return d.toLocaleTimeString('fa-IR', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        });
        const data = series.map(x => x.bps);

        if(chart){
          chart.data.labels = labels;
          chart.data.datasets[0].data = data;
          chart.update('none');
        }

        // auto refresh period
        const poll = (m.poll_sec || 2.0) * 1000;
        window.__poll = poll;

      }catch(e){
        appendOut("[metrics error] " + e);
      }
    }

    function scrollChartToEnd(){
      // nothing needed; chart always shows latest. (placeholder)
      toast("Focused on latest");
    }

    // --------- boot ----------
    makeChart();
    loadTunnels();
    loadTail();
    refreshAll();

    // periodic refresh
    setInterval(() => refreshAll(), 2000);
  </script>
</body>
</html>
"""

@app.get("/")
def index():
    # Render the UI (single page)
    return render_template_string(TPL, SERVICE=SERVICE)

# Extra API endpoints used by UI
@app.get("/api/tunnels")
def api_tunnels():
    rows, _raw = failover_list()
    payload = {"rows": [{"service": r[0], "active": r[1], "port": r[2], "toml": r[3]} for r in rows]}
    return jsonify(payload)

@app.get("/api/logs/tail")
def api_logs_tail():
    unit = request.args.get("unit", SERVICE)
    lines = int(request.args.get("lines", "250"))
    try:
        txt = run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"], timeout=10).stdout or ""
        return jsonify({"ok": True, "text": txt})
    except Exception as e:
        return jsonify({"ok": False, "text": f"[tail error] {e}"}), 500

if __name__ == "__main__":
    # For systemd usage; production can be behind a reverse proxy.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
