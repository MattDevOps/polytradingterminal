## Re-enable Poly Monitor autostart

Two separate mechanisms were disabled on 2026-04-14. Re-enable whichever you need.

### 1. Startup shortcut (`Poly Monitor.lnk`)

Launches the monitor GUI when you log in.

The original `.lnk` was deleted from:
`C:\Users\matts\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\`

To recreate it, right-click `poly_monitor.pyw` in the repo root, **Create shortcut**, rename to `Poly Monitor.lnk`, and move it into the Startup folder above. (Or use `shell:startup` in the Run dialog to open that folder.)

### 2. Scheduled task `\PolyTradingScanner`

Runs `python -m poly --scan` every 2 hours. Still registered, just disabled.

Re-enable:

```
schtasks /change /tn PolyTradingScanner /enable
```

Disable again:

```
schtasks /change /tn PolyTradingScanner /disable
```

Inspect:

```
schtasks /query /tn PolyTradingScanner /fo LIST /v
```

Current task command (for reference if it ever needs rebuilding):

```
cmd /c cd /d C:\Users\matts\Documents\git\polytradingterminal && C:\Users\matts\AppData\Local\Programs\Python\Python311\python.exe -m poly --scan
```

Trigger: hourly repeat every 2h, started 2026-04-12 04:27, runs as `matts`, interactive only, no start on battery.
