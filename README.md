BACKHAUL FAILOVER (Traffic-Aware) – Production Edition


This system monitors multiple systemd services matching:

    backhaul-iran*.service

It ensures that only ONE tunnel remains active at a time.
If the primary tunnel becomes unhealthy, the system automatically
switches to the next healthy backup service.

Unlike basic monitoring systems, this solution verifies REAL TRAFFIC,
not just connection state.

---------------------------------------------------------------------
 HEALTH CHECK CONDITIONS
---------------------------------------------------------------------

A tunnel is considered HEALTHY only if ALL conditions are met:

1) Service is ACTIVE (systemctl is-active)
2) Port is LISTENING
3) No fatal log errors in recent time window
4) At least one TCP ESTABLISHED connection exists
5) Traffic >= 50 KB/s

If traffic drops below 50 KB/s for multiple consecutive checks,
failover will be triggered.

---------------------------------------------------------------------
 ANTI-FLAP PROTECTION
---------------------------------------------------------------------

FAIL_THRESHOLD = 3

This means:
The primary must fail health checks 3 consecutive times before switching.

With default timer (60s interval):

≈ 3 minutes of sustained failure required before failover.

This prevents switching during short traffic dips.

---------------------------------------------------------------------
 INSTALL OR UPDATE
---------------------------------------------------------------------

Always installs latest version from GitHub:

    curl -fsSL https://raw.githubusercontent.com/yasinznl/backhaul-failover/main/install.sh | sudo bash

---------------------------------------------------------------------
 SYSTEMD TIMER
---------------------------------------------------------------------

Timer file:

    /etc/systemd/system/backhaul-failover.timer

Default execution interval:

    Every 60 seconds

To change interval (example: 30 seconds):

Edit timer file:
    sudo nano /etc/systemd/system/backhaul-failover.timer

Modify:
    OnUnitActiveSec=30s

Then reload:
    sudo systemctl daemon-reload
    sudo systemctl restart backhaul-failover.timer

---------------------------------------------------------------------
 QUICK COMMANDS
---------------------------------------------------------------------

Check service status:

    systemctl status backhaul-failover.service

Check timer status:

    systemctl status backhaul-failover.timer

View last 200 log lines:

    journalctl -u backhaul-failover.service -n 200 --no-pager -o cat

Live logs:

    journalctl -u backhaul-failover.service -f -o cat

Run monitor immediately (manual execution):

    sudo systemctl start backhaul-failover.service

---------------------------------------------------------------------
 INTERACTIVE MENU
---------------------------------------------------------------------

Launch menu:

    sudo backhaul-failover-menu

Main features:

1) Status view
2) Run once (manual health check)
3) View logs
4) Start/Stop/Restart timer
5) List available tunnels
6) Manual switch to specific tunnel
7) Start/Stop/Restart specific tunnel
8) Live traffic monitoring
9) Uninstall

---------------------------------------------------------------------
 MANUAL SWITCHING (CLI)
---------------------------------------------------------------------

List tunnels:

    sudo /usr/local/bin/backhaul-failover.sh --list

Show current primary:

    sudo /usr/local/bin/backhaul-failover.sh --current

Switch manually:

    sudo /usr/local/bin/backhaul-failover.sh --switch backhaul-iran407.service

Start tunnel:

    sudo /usr/local/bin/backhaul-failover.sh --start backhaul-iran407.service

Stop tunnel:

    sudo /usr/local/bin/backhaul-failover.sh --stop backhaul-iran407.service

Restart tunnel:

    sudo /usr/local/bin/backhaul-failover.sh --restart backhaul-iran407.service

---------------------------------------------------------------------
 TRAFFIC MONITORING
---------------------------------------------------------------------

Monitor traffic on port 407:

    sudo /usr/local/bin/backhaul-failover.sh --watch-traffic 407 1

This calculates:

    (bytes_received + bytes_acked) / time

Displays:
    Bytes per second
    KB/s
    MB/s

Failover threshold is:

    50 KB/s

If traffic remains below 50 KB/s for 3 consecutive checks,
automatic switching occurs.

---------------------------------------------------------------------
 UNINSTALL
---------------------------------------------------------------------

Disable timer and remove all components:

    sudo systemctl disable --now backhaul-failover.timer
    sudo systemctl stop backhaul-failover.service

    sudo rm -f /etc/systemd/system/backhaul-failover.service
    sudo rm -f /etc/systemd/system/backhaul-failover.timer
    sudo rm -f /usr/local/bin/backhaul-failover.sh
    sudo rm -f /usr/local/bin/backhaul-failover-menu

    sudo systemctl daemon-reload
    sudo systemctl reset-failed

---------------------------------------------------------------------
 HOW FAILOVER WORKS (SIMPLIFIED FLOW)
---------------------------------------------------------------------

Timer triggers monitor →
Check primary health →
If healthy → keep running →
If unhealthy → increase fail counter →
If fail counter >= threshold →
    Try backups in priority order →
    Start candidate →
    Wait until healthy →
    Stop old primary →
    Keep only one active →

---------------------------------------------------------------------
 PRODUCTION NOTES
---------------------------------------------------------------------

• Designed for high-traffic tunnel environments
• Detects silent degradation (connected but no data)
• Only one service remains active at a time
• Safe against short traffic drops
• Fully systemd managed
• No external dependencies required

---------------------------------------------------------------------
 END OF DOCUMENT
---------------------------------------------------------------------
