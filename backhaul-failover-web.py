#!/usr/bin/env python3
import os
import time
import subprocess
from typing import List, Dict, Tuple
from flask import Flask, request, Response, jsonify, render_template_string

# -----------------------------
# Configuration
# -----------------------------
FAILOVER_BIN = os.environ.get("BH_FAILOVER_BIN", "/usr/local/bin/backhaul-failover.sh")
SERVICE_FAILOVER = os.environ.get("BH_FAILOVER_SERVICE", "backhaul-failover.service")
TIMER_FAILOVER = os.environ.get("BH_FAILOVER_TIMER", "backhaul-failover.timer")
SERVICE_WEB = os.environ.get("BH_WEB_SERVICE", "backhaul-failover-web.service")

# Basic Auth
WEB_USER = os.environ.get("BH_WEB_USER", "admin")
WEB_PASS = os.environ.get("BH_WEB_PASS", "admin")

# Listen
WEB_HOST = os.environ.get("BH_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("BH_WEB_PORT", "8088"))

# Traffic log dir
TRAFFIC_DIR = os.environ.get("BH_TRAFFIC_DIR", "/run/backhaul-traffic")
MAX_SERIES_POINTS = int(os.environ.get("BH_MAX_SERIES_POINTS", "240"))

# Switch log
SWITCH_LOG = os.environ.get("BH_SWITCH_LOG", "/var/log/backhaul-switch.log")

app = Flask(__name__)

def run(cmd: List[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)

def is_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0

def auth_ok() -> bool:
    auth = request.authorization
    return bool(auth and auth.username == WEB_USER and auth.password == WEB_PASS)

def require_auth():
    return Response("Authentication required", 401, {"WWW-Authenticate": "Basic realm='Backhaul Command Center'"})

@app.before_request
def guard():
    if not auth_ok():
        return require_auth()

# -----------------------------
# Robust primary detection (FIX)
# -----------------------------
def get_current_primary() -> Tuple[str, str]:
    """
    Use --current-raw to avoid log pollution.
    Expected: "backhaul-iran403.service\t403"
    """
    try:
        p = run([FAILOVER_BIN, "--current-raw"], timeout=10)
        s = (p.stdout or "").strip()
        if not s:
            return "-", "-"
        lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
        for ln in reversed(lines):
            parts = ln.split("\t")
            if len(parts) >= 2 and parts[0].endswith(".service") and parts[1].isdigit():
                return parts[0], parts[1]
            w = ln.split()
            if len(w) >= 2 and w[0].endswith(".service") and w[1].isdigit():
                return w[0], w[1]
        return "-", "-"
    except Exception:
        return "-", "-"

# -----------------------------
# Tunnels parsing
# -----------------------------
def get_tunnels() -> List[Dict[str, str]]:
    p = run([FAILOVER_BIN, "--list-raw"], timeout=15)
    raw = p.stdout or ""
    rows: List[Dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 4:
            rows.append({
                "service": parts[0],
                "active": parts[1],
                "port": parts[2],
                "toml": parts[3],
            })
    return rows

# -----------------------------
# Traffic logs
# -----------------------------
def traffic_log_path(port: str) -> str:
    return os.path.join(TRAFFIC_DIR, f"port-{port}.log")

def ensure_traffic_dir():
    try:
        os.makedirs(TRAFFIC_DIR, exist_ok=True)
    except Exception:
        pass

def read_last_lines(path: str, max_lines: int) -> List[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return lines[-max_lines:]
    except Exception:
        return []

def parse_points(lines: List[str]) -> List[Tuple[int, int]]:
    pts: List[Tuple[int, int]] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split()
        if len(parts) < 2:
            continue
        try:
            ts = int(float(parts[0]))
            b = int(float(parts[1]))
            pts.append((ts, b))
        except Exception:
            continue
    return pts

def points_to_bps(points: List[Tuple[int, int]]) -> List[Tuple[int, float]]:
    if len(points) < 2:
        return []
    out: List[Tuple[int, float]] = []
    prev_ts, prev_b = points[0]
    for ts, b in points[1:]:
        dt = ts - prev_ts
        db = b - prev_b
        prev_ts, prev_b = ts, b
        if dt <= 0:
            continue
        if db < 0:
            continue
        out.append((ts, db / dt))
    return out

def avg(values: List[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)

# ---- Mbps helpers ----
def bps_to_mbps(bytes_per_sec: float) -> float:
    return (bytes_per_sec * 8.0) / 1_000_000.0

def human_mbps(bytes_per_sec: float) -> str:
    return f"{bps_to_mbps(bytes_per_sec):.2f} Mbps"

# ---- Python sampler (keeps chart alive even if timer is slow) ----
def get_port_bytes_sum_now(port: str) -> int:
    try:
        p = run(["ss", "-tinH", f"( sport = :{port} )"], timeout=3)
        out = p.stdout or ""
        rx = 0
        tx = 0
        for tok in out.replace("\n", " ").split():
            if tok.startswith("bytes_received:"):
                v = tok.split(":", 1)[1]
                if v.isdigit():
                    rx += int(v)
            elif tok.startswith("bytes_acked:"):
                v = tok.split(":", 1)[1]
                if v.isdigit():
                    tx += int(v)
        return rx + tx
    except Exception:
        return 0

def traffic_record_sample_py(port: str) -> None:
    if not port or port == "-":
        return
    ensure_traffic_dir()
    ts = int(time.time())
    b = get_port_bytes_sum_now(port)
    path = traffic_log_path(port)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {b}\n")
        lines = read_last_lines(path, max_lines=500)
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass

# -----------------------------
# Logs: Tail + Live (SSE)
# -----------------------------
def sse(data: str) -> str:
    return "data: " + data.replace("\n", "\\n") + "\n\n"

@app.get("/api/logs/tail")
def api_logs_tail():
    unit = request.args.get("unit", SERVICE_FAILOVER)
    lines = int(request.args.get("lines", "250"))
    try:
        txt = run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"], timeout=12).stdout or ""
        return jsonify({"ok": True, "text": txt})
    except Exception as e:
        return jsonify({"ok": False, "text": f"[tail error] {e}"}), 500

@app.get("/api/logs/live")
def api_logs_live():
    unit = request.args.get("unit", SERVICE_FAILOVER)
    initial_lines = int(request.args.get("lines", "80"))

    def stream():
        try:
            tail = run(["journalctl", "-u", unit, "-n", str(initial_lines), "--no-pager", "-o", "cat"], timeout=10).stdout or ""
            for ln in tail.splitlines():
                yield sse(ln)
        except Exception as e:
            yield sse(f"[tail error] {e}")

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
                yield sse(line.rstrip("\n"))
        except GeneratorExit:
            return
        except Exception as e:
            yield sse(f"[follow error] {e}")

    return Response(stream(), mimetype="text/event-stream")

# -----------------------------
# Switch log API
# -----------------------------
@app.get("/api/switch-log")
def api_switch_log():
    lines = int(request.args.get("lines", "200"))
    if lines < 1:
        lines = 1
    if lines > 2000:
        lines = 2000

    if not os.path.exists(SWITCH_LOG):
        return jsonify({"ok": True, "text": ""})

    try:
        last = read_last_lines(SWITCH_LOG, max_lines=lines)
        return jsonify({"ok": True, "text": "".join(last)})
    except Exception as e:
        return jsonify({"ok": False, "text": f"[switch log error] {e}"}), 500

# -----------------------------
# Metrics
# -----------------------------
@app.get("/api/metrics")
def api_metrics():
    primary_svc, primary_port = get_current_primary()

    # Always take a sample so UI is never empty
    if primary_port not in ("", "-"):
        traffic_record_sample_py(primary_port)

    series = []
    now_bps = 0.0
    avg_60 = 0.0
    avg_300 = 0.0

    if primary_port not in ("", "-"):
        path = traffic_log_path(primary_port)
        lines = read_last_lines(path, max_lines=MAX_SERIES_POINTS + 20)
        pts = parse_points(lines)
        bps_pts = points_to_bps(pts)

        bps_pts = bps_pts[-MAX_SERIES_POINTS:]
        series = [{"t": t, "bps": v} for (t, v) in bps_pts]

        if bps_pts:
            now_ts = bps_pts[-1][0]
            now_bps = bps_pts[-1][1]
            last60 = [v for (t, v) in bps_pts if t >= now_ts - 60]
            last300 = [v for (t, v) in bps_pts if t >= now_ts - 300]
            avg_60 = avg(last60)
            avg_300 = avg(last300)

    data = {
        "primary": {"service": primary_svc, "port": primary_port},
        "units": {
            "failover": "active" if is_active(SERVICE_FAILOVER) else "inactive",
            "timer": "active" if is_active(TIMER_FAILOVER) else "inactive",
            "web": "active" if is_active(SERVICE_WEB) else "inactive",
        },
        "traffic": {
            "series": series,  # bps is BYTES/s
            "now":  {"bps": now_bps,  "mbps": bps_to_mbps(now_bps),  "human": human_mbps(now_bps)},
            "avg1m":{"bps": avg_60,   "mbps": bps_to_mbps(avg_60),   "human": human_mbps(avg_60)},
            "avg5m":{"bps": avg_300,  "mbps": bps_to_mbps(avg_300),  "human": human_mbps(avg_300)},
        }
    }
    return jsonify(data)

@app.get("/api/tunnels")
def api_tunnels():
    rows = get_tunnels()
    return jsonify({"ok": True, "rows": rows})

# -----------------------------
# Actions
# -----------------------------
@app.post("/api/action")
def api_action():
    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").strip()
    svc = (payload.get("service") or "").strip()

    allowed = {
        "run_once",
        "timer_on", "timer_off",
        "web_restart",
        "switch",
        "tunnel_start", "tunnel_stop", "tunnel_restart",
    }
    if action not in allowed:
        return jsonify({"ok": False, "error": "Invalid action"}), 400

    try:
        if action == "run_once":
            out = run(["systemctl", "start", SERVICE_FAILOVER], timeout=20).stdout or ""
            return jsonify({"ok": True, "output": out or "Failover check executed."})

        if action == "timer_on":
            out = run(["systemctl", "enable", "--now", TIMER_FAILOVER], timeout=20).stdout or ""
            return jsonify({"ok": True, "output": out or "Timer enabled."})

        if action == "timer_off":
            out = run(["systemctl", "disable", "--now", TIMER_FAILOVER], timeout=20).stdout or ""
            return jsonify({"ok": True, "output": out or "Timer disabled."})

        if action == "web_restart":
            out = run(["systemctl", "restart", SERVICE_WEB], timeout=20).stdout or ""
            return jsonify({"ok": True, "output": out or "Web service restarted."})

        if action in {"switch", "tunnel_start", "tunnel_stop", "tunnel_restart"} and not svc:
            return jsonify({"ok": False, "error": "Missing service name"}), 400

        if action == "switch":
            out = run([FAILOVER_BIN, "--switch", svc], timeout=90).stdout or ""
            return jsonify({"ok": True, "output": out[-8000:] if out else "Switch command completed."})

        if action == "tunnel_start":
            out = run([FAILOVER_BIN, "--start", svc], timeout=25).stdout or ""
            return jsonify({"ok": True, "output": out[-4000:] if out else f"Started {svc}"})

        if action == "tunnel_stop":
            out = run([FAILOVER_BIN, "--stop", svc], timeout=25).stdout or ""
            return jsonify({"ok": True, "output": out[-4000:] if out else f"Stopped {svc}"})

        if action == "tunnel_restart":
            out = run([FAILOVER_BIN, "--restart", svc], timeout=35).stdout or ""
            return jsonify({"ok": True, "output": out[-6000:] if out else f"Restarted {svc}"})

        return jsonify({"ok": False, "error": "Unhandled action"}), 500

    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Command timed out"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# -----------------------------
# UI
# -----------------------------
HTML = r"""
<!doctype html>
<html lang="en" dir="ltr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Backhaul Command Center</title>

  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>

  <style>
    :root{
      --bg0:#060812;
      --bg1:#0b1220;
      --line:rgba(90,120,200,.22);
      --text:#eaf0ff;
      --muted:#9fb0d8;
      --good:#22c55e;
      --bad:#ef4444;
      --accent:#7c3aed;
      --accent2:#06b6d4;
      --shadow: 0 14px 40px rgba(0,0,0,.35);
      --r:18px;
    }

    body{
      background:
        radial-gradient(900px 600px at 15% 10%, rgba(124,58,237,.22), transparent 60%),
        radial-gradient(900px 600px at 85% 30%, rgba(6,182,212,.16), transparent 62%),
        linear-gradient(180deg, var(--bg0), var(--bg1));
      color: var(--text);
      min-height:100vh;
    }

    .topbar{
      position: sticky; top: 0; z-index: 50;
      backdrop-filter: blur(10px);
      background: rgba(6,8,18,.55);
      border-bottom: 1px solid var(--line);
    }

    .brand{ font-weight: 900; letter-spacing: .3px; font-size: 1.05rem; }
    .subbrand{ color: var(--muted); font-size: .85rem; }

    .chip{
      border: 1px solid var(--line);
      background: rgba(15,26,51,.55);
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
    .dot.bad{  background: var(--bad);  box-shadow: 0 0 0 4px rgba(239,68,68,.12); }

    .panel{
      background: linear-gradient(180deg, rgba(15,26,51,.92), rgba(11,22,45,.92));
      border: 1px solid var(--line);
      border-radius: var(--r);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .panel-h{
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display:flex; justify-content:space-between; align-items:center; gap:10px;
    }
    .panel-b{ padding: 14px 16px; }

    .btn-soft{
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(15,26,51,.45);
      color: var(--text);
    }
    .btn-soft:hover{ border-color: rgba(124,58,237,.85); }
    .btn-accent{
      border-radius: 14px;
      border: 1px solid rgba(124,58,237,.75);
      background: rgba(124,58,237,.18);
      color: var(--text);
    }
    .btn-danger-soft{
      border-radius: 14px;
      border: 1px solid rgba(239,68,68,.55);
      background: rgba(239,68,68,.12);
      color: var(--text);
    }

    .kpi{
      border: 1px solid var(--line);
      background: rgba(8,12,24,.35);
      border-radius: 16px;
      padding: 12px;
    }
    .kpi .label{ color: var(--muted); font-size: .85rem; }
    .kpi .value{ font-size: 1.2rem; font-weight: 900; letter-spacing:.2px; }
    .kpi .sub{ color: var(--muted); font-size: .8rem; }

    .logbox{
      height: 360px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12.5px;
      line-height: 1.45;
      background: rgba(6,8,18,.6);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px;
      white-space: pre-wrap;
    }

    .table-dark{
      --bs-table-bg: transparent;
      --bs-table-color: var(--text);
      --bs-table-border-color: var(--line);
    }
    .table thead th{
      color: var(--muted);
      font-weight: 800;
      border-bottom: 1px solid var(--line) !important;
    }
    .table tbody td{
      border-top: 1px solid rgba(90,120,200,.18) !important;
      vertical-align: middle;
    }

    .chartWrap{
      position: relative;
      width: 100%;
      height: 320px;
      overflow: hidden;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: rgba(6,8,18,.35);
    }
    @media (max-width: 576px){
      .chartWrap{ height: 240px; }
      .logbox{ height: 300px; }
      .kpi .value{ font-size: 1.05rem; }
    }

    .toast-container{ z-index: 9999; }
    .toast{
      background: rgba(15,26,51,.96);
      border: 1px solid var(--line);
      color: var(--text);
      border-radius: 16px;
      box-shadow: var(--shadow);
    }

    .nav-pills .nav-link{
      color: var(--muted);
      border-radius: 14px;
      border: 1px solid rgba(90,120,200,.18);
      background: rgba(15,26,51,.35);
    }
    .nav-pills .nav-link.active{
      color: var(--text);
      border-color: rgba(124,58,237,.9);
      background: rgba(124,58,237,.2);
    }
    .tiny{ color: var(--muted); font-size: .85rem; }
  </style>
</head>

<body>
  <div class="topbar py-3">
    <div class="container d-flex flex-wrap align-items-center justify-content-between gap-2">
      <div class="d-flex align-items-center gap-2">
        <i class="bi bi-cpu-fill" style="font-size:1.35rem;color:rgba(124,58,237,.95)"></i>
        <div>
          <div class="brand">Backhaul Command Center</div>
          <div class="subbrand">failover • tunnels • logs • traffic</div>
        </div>
      </div>
      <div class="d-flex flex-wrap gap-2">
        <span class="chip"><span id="dot-failover" class="dot bad"></span> Failover: <span id="st-failover">…</span></span>
        <span class="chip"><span id="dot-timer" class="dot bad"></span> Timer: <span id="st-timer">…</span></span>
        <span class="chip"><span id="dot-web" class="dot bad"></span> Web: <span id="st-web">…</span></span>
      </div>
    </div>
  </div>

  <div class="container py-4">
    <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
      <ul class="nav nav-pills gap-2">
        <li class="nav-item"><button class="nav-link active" data-bs-toggle="pill" data-bs-target="#tab-dashboard" type="button">Dashboard</button></li>
        <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-tunnels" type="button">Tunnels</button></li>
        <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-logs" type="button">Logs</button></li>
      </ul>

      <div class="d-flex flex-wrap gap-2">
        <button class="btn btn-soft btn-sm" onclick="action('run_once')"><i class="bi bi-play-fill"></i> Run check</button>
        <button class="btn btn-accent btn-sm" onclick="action('timer_on')"><i class="bi bi-lightning-charge-fill"></i> Timer on</button>
        <button class="btn btn-soft btn-sm" onclick="action('timer_off')"><i class="bi bi-pause-fill"></i> Timer off</button>
        <button class="btn btn-soft btn-sm" onclick="action('web_restart')"><i class="bi bi-arrow-repeat"></i> Restart web</button>
      </div>
    </div>

    <div class="tab-content">
      <!-- Dashboard -->
      <div class="tab-pane fade show active" id="tab-dashboard">
        <div class="row g-3">
          <div class="col-12 col-lg-5">
            <div class="panel">
              <div class="panel-h">
                <div class="d-flex align-items-center gap-2">
                  <i class="bi bi-hdd-network-fill" style="color:rgba(6,182,212,.95)"></i>
                  <div class="fw-bold">Primary</div>
                </div>
                <div class="tiny">current active tunnel</div>
              </div>
              <div class="panel-b">
                <div class="kpi mb-2">
                  <div class="label">Service</div>
                  <div class="value" id="primary-svc">…</div>
                  <div class="sub">Port: <span id="primary-port">…</span></div>
                </div>

                <div class="row g-2">
                  <div class="col-4"><div class="kpi">
                    <div class="label">Now</div>
                    <div class="value" id="kpi-now">…</div>
                    <div class="sub">Mbps</div>
                  </div></div>
                  <div class="col-4"><div class="kpi">
                    <div class="label">Avg 1m</div>
                    <div class="value" id="kpi-1m">…</div>
                    <div class="sub">Mbps</div>
                  </div></div>
                  <div class="col-4"><div class="kpi">
                    <div class="label">Avg 5m</div>
                    <div class="value" id="kpi-5m">…</div>
                    <div class="sub">Mbps</div>
                  </div></div>
                </div>

                <div class="mt-3 d-flex flex-wrap gap-2">
                  <button class="btn btn-accent btn-sm" onclick="refreshAll(true)"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
                  <button class="btn btn-soft btn-sm" onclick="loadTunnels()"><i class="bi bi-diagram-3-fill"></i> Reload tunnels</button>
                  <button class="btn btn-soft btn-sm" onclick="loadTail()"><i class="bi bi-journal-text"></i> Refresh logs</button>
                  <button class="btn btn-soft btn-sm" onclick="loadSwitchLog()"><i class="bi bi-clock-history"></i> Switch log</button>
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
                <div class="tiny">Mbps • capped points</div>
              </div>
              <div class="panel-b">
                <div class="chartWrap">
                  <canvas id="chart"></canvas>
                </div>
                <div class="tiny mt-2">Source: {{TRAFFIC_DIR}}/port-&lt;PORT&gt;.log</div>
              </div>
            </div>
          </div>

          <div class="col-12 col-lg-6">
            <div class="panel">
              <div class="panel-h">
                <div class="d-flex align-items-center gap-2">
                  <i class="bi bi-clock-history" style="color:rgba(6,182,212,.95)"></i>
                  <div class="fw-bold">Switch History</div>
                </div>
                <button class="btn btn-soft btn-sm" onclick="loadSwitchLog()"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
              </div>
              <div class="panel-b">
                <div class="logbox" id="switchbox">Loading…</div>
                <div class="tiny mt-2">File: /var/log/backhaul-switch.log</div>
              </div>
            </div>
          </div>

          <div class="col-12 col-lg-6">
            <div class="panel">
              <div class="panel-h">
                <div class="d-flex align-items-center gap-2">
                  <i class="bi bi-terminal-fill" style="color:rgba(245,158,11,.95)"></i>
                  <div class="fw-bold">Action Output</div>
                </div>
                <div class="tiny">latest output is on top</div>
              </div>
              <div class="panel-b">
                <div class="logbox" id="outbox">Ready.</div>
              </div>
            </div>
          </div>

        </div>
      </div>

      <!-- Tunnels -->
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
              <table class="table table-dark table-sm align-middle">
                <thead>
                  <tr>
                    <th>Service</th>
                    <th>State</th>
                    <th>Port</th>
                    <th>Config (toml)</th>
                    <th style="width: 280px;">Actions</th>
                  </tr>
                </thead>
                <tbody id="tunnelsBody">
                  <tr><td colspan="5" class="tiny">Loading…</td></tr>
                </tbody>
              </table>
            </div>
            <div class="tiny">Switch will only finalize if the target becomes healthy.</div>
          </div>
        </div>
      </div>

      <!-- Logs -->
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
                <div class="logbox" id="tailbox">Loading…</div>
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
                <div class="logbox" id="livebox">Click Start to stream live logs.</div>
              </div>
            </div>
          </div>
        </div>
      </div>

    </div>
  </div>

  <div class="toast-container position-fixed bottom-0 start-0 p-3">
    <div id="toast" class="toast" role="alert" aria-live="assertive" aria-atomic="true">
      <div class="toast-body" id="toastBody">…</div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>

  <script>
    const MAX_POINTS = 180;
    const POLL_MS = 2000;
    let lastChartKey = "";
    let chart;
    let liveSource = null;
    let refreshBusy = false;

    function toast(msg){
      document.getElementById("toastBody").textContent = msg;
      const t = new bootstrap.Toast(document.getElementById("toast"), {delay: 2000});
      t.show();
    }

    function setDot(dotId, textId, state){
      const dot = document.getElementById(dotId);
      const txt = document.getElementById(textId);
      txt.textContent = state;
      dot.classList.remove("good","bad");
      dot.classList.add(state === "active" ? "good" : "bad");
    }

    function pushOutput(text){
      const out = document.getElementById("outbox");
      const now = new Date().toLocaleString();
      const block = `[${now}] ${text}\n\n`;
      out.textContent = (block + out.textContent).slice(0, 14000);
    }

    function escapeHtml(s){
      return (s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
    }
    function escapeAttr(s){
      return (s||"").replaceAll("'","\\'");
    }

    function makeChart(){
      const ctx = document.getElementById("chart");
      chart = new Chart(ctx, {
        type: "line",
        data: { labels: [], datasets: [{
          label: "Traffic (Mbps)",
          data: [],
          borderWidth: 2,
          tension: 0.25,
          pointRadius: 0,
        }]},
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          normalized: true,
          plugins: {
            legend: { labels: { color: "#9fb0d8" } },
            tooltip: {
              callbacks: {
                label: (ctx) => " " + Number(ctx.raw || 0).toFixed(2) + " Mbps"
              }
            }
          },
          scales: {
            x: { ticks: { color: "#9fb0d8", maxTicksLimit: 8 }, grid: { color: "rgba(90,120,200,.15)" } },
            y: { ticks: { color: "#9fb0d8", maxTicksLimit: 6 }, grid: { color: "rgba(90,120,200,.15)" } }
          }
        }
      });
    }

    async function action(actionName, service=null){
      try{
        toast("Executing…");
        const res = await fetch("/api/action", {
          method: "POST",
          headers: {"Content-Type":"application/json"},
          credentials: "same-origin",
          body: JSON.stringify({action: actionName, service})
        });
        const j = await res.json();
        if(!j.ok){
          toast("Error");
          pushOutput("ERROR: " + (j.error || "unknown"));
          return;
        }
        toast("Done");
        pushOutput(j.output || "OK");
        await refreshAll(true);
        await loadTunnels();
        await loadSwitchLog();
      }catch(e){
        toast("Network error");
        pushOutput("EXCEPTION: " + e);
      }
    }

    async function loadTunnels(){
      const body = document.getElementById("tunnelsBody");
      body.innerHTML = `<tr><td colspan="5" class="tiny">Loading…</td></tr>`;
      try{
        const res = await fetch("/api/tunnels", { credentials: "same-origin" });
        const j = await res.json();
        if(!j.ok || !j.rows){
          body.innerHTML = `<tr><td colspan="5" class="tiny">Failed to load tunnels.</td></tr>`;
          return;
        }
        if(j.rows.length === 0){
          body.innerHTML = `<tr><td colspan="5" class="tiny">No tunnels found.</td></tr>`;
          return;
        }
        body.innerHTML = "";
        for(const r of j.rows){
          const stateBadge = (r.active === "yes")
            ? `<span class="badge" style="background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.45)">ACTIVE</span>`
            : `<span class="badge" style="background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.35)">OFF</span>`;

          body.insertAdjacentHTML("beforeend", `
            <tr>
              <td style="white-space:nowrap">${escapeHtml(r.service)}</td>
              <td>${stateBadge}</td>
              <td>${escapeHtml(r.port)}</td>
              <td style="max-width:320px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted)">
                ${escapeHtml(r.toml)}
              </td>
              <td class="d-flex flex-wrap gap-2">
                <button class="btn btn-accent btn-sm" onclick="action('switch','${escapeAttr(r.service)}')">
                  <i class="bi bi-shuffle"></i> Switch
                </button>
                <button class="btn btn-soft btn-sm" onclick="action('tunnel_start','${escapeAttr(r.service)}')">Start</button>
                <button class="btn btn-soft btn-sm" onclick="action('tunnel_restart','${escapeAttr(r.service)}')">Restart</button>
                <button class="btn btn-danger-soft btn-sm" onclick="action('tunnel_stop','${escapeAttr(r.service)}')">Stop</button>
              </td>
            </tr>
          `);
        }
      }catch(e){
        body.innerHTML = `<tr><td colspan="5" class="tiny">Error: ${escapeHtml(String(e))}</td></tr>`;
      }
    }

    async function loadTail(){
      const box = document.getElementById("tailbox");
      box.textContent = "Loading…";
      try{
        const res = await fetch("/api/logs/tail?lines=300", { credentials: "same-origin" });
        const j = await res.json();
        box.textContent = j.text || "";
        box.scrollTop = box.scrollHeight;
      }catch(e){
        box.textContent = "Error: " + e;
      }
    }

    async function loadSwitchLog(){
      const box = document.getElementById("switchbox");
      box.textContent = "Loading…";
      try{
        const res = await fetch("/api/switch-log?lines=200", { credentials: "same-origin" });
        const j = await res.json();
        box.textContent = j.text || "";
        box.scrollTop = box.scrollHeight;
      }catch(e){
        box.textContent = "Error: " + e;
      }
    }

    function startLive(){
      stopLive();
      const box = document.getElementById("livebox");
      box.textContent = "";
      liveSource = new EventSource("/api/logs/live?lines=80");
      liveSource.onmessage = (ev) => {
        box.textContent += ev.data.replaceAll("\\n","\n") + "\n";
        if(box.textContent.length > 24000){
          box.textContent = box.textContent.slice(-20000);
        }
        box.scrollTop = box.scrollHeight;
      };
      liveSource.onerror = () => {
        box.textContent += "\n[stream disconnected]\n";
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

    async function refreshAll(manual=false){
      if(refreshBusy && !manual) return;
      refreshBusy = true;
      try{
        const res = await fetch("/api/metrics", { credentials: "same-origin" });
        const m = await res.json();

        document.getElementById("primary-svc").textContent = m.primary.service || "-";
        document.getElementById("primary-port").textContent = m.primary.port || "-";

        setDot("dot-failover","st-failover", m.units.failover);
        setDot("dot-timer","st-timer", m.units.timer);
        setDot("dot-web","st-web", m.units.web);

        document.getElementById("kpi-now").textContent = (m.traffic.now && m.traffic.now.mbps !== undefined) ? Number(m.traffic.now.mbps).toFixed(2) : "0.00";
        document.getElementById("kpi-1m").textContent  = (m.traffic.avg1m && m.traffic.avg1m.mbps !== undefined) ? Number(m.traffic.avg1m.mbps).toFixed(2) : "0.00";
        document.getElementById("kpi-5m").textContent  = (m.traffic.avg5m && m.traffic.avg5m.mbps !== undefined) ? Number(m.traffic.avg5m.mbps).toFixed(2) : "0.00";

        // series is bytes/s -> convert to Mbps for chart
        const series = (m.traffic.series || []).slice(-MAX_POINTS);
        const labels = series.map(x => {
          const d = new Date(x.t * 1000);
          return d.toLocaleTimeString(undefined, {hour:"2-digit", minute:"2-digit", second:"2-digit"});
        });
        const data = series
          .map(x => Number(x.bps))
          .map(v => (Number.isFinite(v) ? (v * 8 / 1_000_000) : 0));

        const key = String(labels.length) + ":" + (labels[labels.length-1] || "");
        if(key !== lastChartKey){
          lastChartKey = key;
          chart.data.labels = labels;
          chart.data.datasets[0].data = data;
          chart.update("none");
        }

      }catch(e){
        pushOutput("metrics error: " + e);
      }finally{
        refreshBusy = false;
      }
    }

    makeChart();
    loadTunnels();
    loadTail();
    loadSwitchLog();
    refreshAll(true);
    setInterval(() => refreshAll(false), POLL_MS);
  </script>
</body>
</html>
"""

@app.get("/")
def index():
    return render_template_string(HTML, TRAFFIC_DIR=TRAFFIC_DIR)

if __name__ == "__main__":
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True)
