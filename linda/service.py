"""Make Linda start at login and restart if she crashes. Starts the web server (server.py);
add --classic to start the terminal version (main.py) instead.

  python service.py login-item     RECOMMENDED. Creates Linda.command (auto-restart loop) and adds it to Login Items.
                                   It runs inside Terminal, so Microphone/Calendar permissions you granted to
                                   Terminal just work.
  python service.py install        Alternative: background LaunchAgent (no window). macOS may not show the
                                   microphone/calendar permission prompts for background Python; if Linda is deaf
                                   or has no calendar, use login-item instead.
  python service.py uninstall | status | logs | show

Add --awake to either install command to stop the Mac from idle-sleeping while Linda runs."""
import os, pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
PY = sys.executable
LABEL = "com.linda.assistant"
PLIST = pathlib.Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG = pathlib.Path.home() / "Library/Logs/linda.log"
COMMAND = pathlib.Path.home() / "Linda.command"
UID = os.getuid()
awake = "--awake" in sys.argv
ENTRY = "main.py" if "--classic" in sys.argv else "server.py"


def plist_text():
    args = (["/usr/bin/caffeinate", "-i"] if awake else []) + [PY, str(HERE / ENTRY)]
    arr = "".join(f"<string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{arr}</array>
  <key>WorkingDirectory</key><string>{HERE}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{LOG}</string>
  <key>StandardErrorPath</key><string>{LOG}</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
</dict></plist>
"""


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def install():
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(plist_text())
    sh("launchctl", "bootout", f"gui/{UID}/{LABEL}")
    r = sh("launchctl", "bootstrap", f"gui/{UID}", str(PLIST))
    print("Installed. Linda starts at login and restarts if she stops." if r.returncode == 0 else r.stderr)
    print(f"Logs: python service.py logs   ({LOG})")


def uninstall():
    sh("launchctl", "bootout", f"gui/{UID}/{LABEL}")
    PLIST.unlink(missing_ok=True)
    COMMAND.unlink(missing_ok=True)
    print("Removed LaunchAgent and Linda.command. (Remove 'Linda' from Login Items manually if listed.)")


def login_item():
    prefix = "/usr/bin/caffeinate -i " if awake else ""
    COMMAND.write_text(f"""#!/bin/bash
trap 'exit 0' INT
cd "{HERE}"
while true; do
  {prefix}"{PY}" {ENTRY}
  echo "Linda stopped. Restarting in 5 s (Ctrl-C to quit)..."; sleep 5
done
""")
    COMMAND.chmod(0o755)
    script = (f'tell application "System Events" to make login item at end with properties '
              f'{{path:"{COMMAND}", hidden:false}}')
    r = sh("osascript", "-e", script)
    if r.returncode == 0:
        print(f"Created {COMMAND} and added it to Login Items. Linda will open in Terminal at login.")
    else:
        print(f"Created {COMMAND}.\nAdd it manually: System Settings > General > Login Items > '+' > Linda.command")
    print("Tip: minimize the Terminal window; Linda keeps running.")


def status():
    r = sh("launchctl", "print", f"gui/{UID}/{LABEL}")
    print("\n".join(l for l in r.stdout.splitlines() if "state" in l or "pid" in l or "last exit" in l)
          if r.returncode == 0 else "LaunchAgent not loaded.")


cmds = {"install": install, "uninstall": uninstall, "login-item": login_item, "status": status,
        "logs": lambda: os.execvp("tail", ["tail", "-n", "50", "-f", str(LOG)]),
        "show": lambda: print(plist_text())}
cmd = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
if cmd in cmds:
    cmds[cmd]()
else:
    print(__doc__)