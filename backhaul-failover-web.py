#!/usr/bin/env python3
import os, subprocess
from flask import Flask, request, Response, redirect, url_for, render_template_string

FAILOVER_BIN = "/usr/local/bin/backhaul-failover.sh"
SERVICE = "backhaul-failover.service"
TIMER = "backhaul-failover.timer"
WEB_SERVICE = "backhaul-failover-web.service"

USER = os.environ.get("BH_WEB_USER", "admin")
PASS = os.environ.get("BH_WEB_PASS", "admin")
PORT = int(os.environ.get("BH_WEB_PORT", "8088"))

app = Flask(__name__)

def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def ok_auth(auth):
    return auth and auth.username == USER and auth.password == PASS

def need_auth():
    return Response("Auth required", 401, {"WWW-Authenticate": "Basic realm='Backhaul Panel'"})

@app.before_request
def protect():
    if request.path.startswith("/static"):
        return
    auth = request.authorization
    if not ok_auth(auth):
        return need_auth()

TPL = """
<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Backhaul Failover Panel</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background:#0b1220; color:#e7eefc; }
    .card { background:#111a2e; border:1px solid #24314d; border-radius:18px; }
    .btn { border-radius: 14px; }
    pre { background:#0a1020; border:1px solid #24314d; padding:12px; border-radius:14px; color:#d7e3ff; }
    .badge { border-radius: 12px; }
    a { color:#a8c7ff; }
    .muted { color:#9db0d9; }
  </style>
</head>
<body class="py-4">
<div class="container">
  <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-3">
    <div>
      <h4 class="m-0">پنل مدیریت Backhaul Failover</h4>
      <div class="muted small mt-1">Responsive UI + کنترل کامل سرویس‌ها</div>
    </div>
    <div class="d-flex gap-2">
      <a class="btn btn-outline-light btn-sm" href="{{ url_for('index') }}">Refresh</a>
      <a class="btn btn-outline-info btn-sm" href="{{ url_for('logs') }}">Logs</a>
      <a class="btn btn-outline-warning btn-sm" href="{{ url_for('status') }}">System Status</a>
    </div>
  </div>

  <div class="row g-3">
    <div class="col-12 col-lg-6">
      <div class="card p-3">
        <h6 class="mb-3">وضعیت</h6>
        <div class="d-flex flex-wrap gap-2">
          <span class="badge text-bg-secondary">Failover Service: {{ svc_state }}</span>
          <span class="badge text-bg-secondary">Timer: {{ timer_state }}</span>
          <span class="badge text-bg-secondary">Web: {{ web_state }}</span>
        </div>
        <div class="mt-3">
          <div class="muted small">Primary: <b>{{ primary }}</b></div>
        </div>
        <div class="mt-3 d-flex flex-wrap gap-2">
          <a class="btn btn-success btn-sm" href="{{ url_for('run_once') }}">Run once</a>
          <a class="btn btn-outline-light btn-sm" href="{{ url_for('timer_on') }}">Enable timer</a>
          <a class="btn btn-outline-warning btn-sm" href="{{ url_for('timer_off') }}">Disable timer</a>
          <a class="btn btn-outline-info btn-sm" href="{{ url_for('web_restart') }}">Restart web</a>
        </div>
      </div>
    </div>

    <div class="col-12 col-lg-6">
      <div class="card p-3">
        <h6 class="mb-3">تانل‌ها</h6>
        <div class="table-responsive">
          <table class="table table-dark table-sm align-middle">
            <thead><tr><th>Service</th><th>Active</th><th>Port</th><th></th></tr></thead>
            <tbody>
            {% for r in rows %}
              <tr>
                <td style="white-space:nowrap">{{ r[0] }}</td>
                <td>
                  {% if r[1]=='yes' %}
                    <span class="badge text-bg-success">yes</span>
                  {% else %}
                    <span class="badge text-bg-danger">no</span>
                  {% endif %}
                </td>
                <td>{{ r[2] }}</td>
                <td>
                  <a class="btn btn-info btn-sm" href="{{ url_for('switch', svc=r[0]) }}">Switch</a>
                  <a class="btn btn-outline-light btn-sm" href="{{ url_for('tunnel_start', svc=r[0]) }}">Start</a>
                  <a class="btn btn-outline-warning btn-sm" href="{{ url_for('tunnel_restart', svc=r[0]) }}">Restart</a>
                  <a class="btn btn-outline-danger btn-sm" href="{{ url_for('tunnel_stop', svc=r[0]) }}">Stop</a>
                </td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
        <div class="muted small">یوزر/پسورد پیشفرض: <b>admin/admin</b> — پورت: <b>{{ web_port }}</b></div>
      </div>
    </div>

    <div class="col-12">
      <div class="card p-3">
        <h6 class="mb-3">خروجی آخرین عملیات</h6>
        <pre>{{ out }}</pre>
      </div>
    </div>
  </div>
</div>
</body>
</html>
"""

def is_active(unit):
    return run(["systemctl", "is-active", "--quiet", unit]).returncode == 0

@app.get("/")
def index():
    svc_state = "active" if is_active(SERVICE) else "inactive"
    timer_state = "active" if is_active(TIMER) else "inactive"
    web_state = "active" if is_active(WEB_SERVICE) else "inactive"

    cur = run([FAILOVER_BIN, "--current"])
    primary = cur.stdout.strip() or "-"

    lst = run([FAILOVER_BIN, "--list"])
    rows = []
    lines = lst.stdout.splitlines()
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 3:
            rows.append([parts[0], parts[1], parts[2]])

    out = request.args.get("msg", "OK")
    return render_template_string(TPL, svc_state=svc_state, timer_state=timer_state,
                                  web_state=web_state, primary=primary, rows=rows, out=out,
                                  web_port=PORT)

@app.get("/run-once")
def run_once():
    run(["systemctl", "start", SERVICE])
    return redirect(url_for("index", msg="Ran once"))

@app.get("/timer/on")
def timer_on():
    run(["systemctl", "enable", "--now", TIMER])
    return redirect(url_for("index", msg="Timer enabled"))

@app.get("/timer/off")
def timer_off():
    run(["systemctl", "disable", "--now", TIMER])
    return redirect(url_for("index", msg="Timer disabled"))

@app.get("/web/restart")
def web_restart():
    run(["systemctl", "restart", WEB_SERVICE])
    return redirect(url_for("index", msg="Web panel restarted"))

@app.get("/switch/<path:svc>")
def switch(svc):
    r = run([FAILOVER_BIN, "--switch", svc])
    return redirect(url_for("index", msg=r.stdout[-4000:]))

@app.get("/tunnel/start/<path:svc>")
def tunnel_start(svc):
    r = run([FAILOVER_BIN, "--start", svc])
    return redirect(url_for("index", msg=r.stdout[-2000:] or f"Started {svc}"))

@app.get("/tunnel/stop/<path:svc>")
def tunnel_stop(svc):
    r = run([FAILOVER_BIN, "--stop", svc])
    return redirect(url_for("index", msg=r.stdout[-2000:] or f"Stopped {svc}"))

@app.get("/tunnel/restart/<path:svc>")
def tunnel_restart(svc):
    r = run([FAILOVER_BIN, "--restart", svc])
    return redirect(url_for("index", msg=r.stdout[-2000:] or f"Restarted {svc}"))

@app.get("/logs")
def logs():
    j = run(["journalctl", "-u", SERVICE, "-n", "400", "--no-pager", "-o", "cat"])
    html = f"""
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <div style="font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
                white-space:pre-wrap;background:#0a1020;color:#d7e3ff;padding:14px;border-radius:14px;
                border:1px solid #24314d; margin:12px;">
    {j.stdout}
    </div>
    <div style="margin:12px"><a href="/" style="color:#a8c7ff">← Back</a></div>
    """
    return html

@app.get("/status")
def status():
    s1 = run(["systemctl", "status", SERVICE, "--no-pager", "-l"]).stdout
    s2 = run(["systemctl", "status", TIMER, "--no-pager", "-l"]).stdout
    s3 = run(["systemctl", "status", WEB_SERVICE, "--no-pager", "-l"]).stdout
    html = f"""
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <div style="font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
                white-space:pre-wrap;background:#0a1020;color:#d7e3ff;padding:14px;border-radius:14px;
                border:1px solid #24314d; margin:12px;">
[SERVICE]\n{s1}\n\n[TIMER]\n{s2}\n\n[WEB]\n{s3}
    </div>
    <div style="margin:12px"><a href="/" style="color:#a8c7ff">← Back</a></div>
    """
    return html

if __name__ == "__main__":
    # Run behind systemd; debug server is OK for local, but we run as service here.
    app.run(host="0.0.0.0", port=PORT)
