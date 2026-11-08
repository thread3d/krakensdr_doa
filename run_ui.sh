#!/bin/bash
# run_ui.sh -- start the KrakenSDR DoA web interface.
#
# The UI is a Dash app served by quart on port 8080. Getting it running on a
# current stack needed three compatibility fixes, all applied by
# _ui/_web_interface/dash_compat.py:
#
#   * dash_devices 0.1.3 imports werkzeug's get_current_traceback, removed in
#     werkzeug 2.1 (this host has 2.2.3)
#   * dash_devices imports the standalone `dash_renderer` package, folded into
#     Dash 2.x as dash._dash_renderer
#   * the UI's own code imports dash_core_components / dash_html_components,
#     removed in Dash 2.x
#
# plus one source fix: dcc.Location no longer accepts `children` (views/main.py)
# and `quart` had to be installed (dash_devices declares no requirements at all).
#
# IMPORTANT -- SAME USER AS THE DAQ:
# The DoA app attaches to the DAQ chain's shared memory and control FIFOs. Those
# are created by whoever starts the chain, so this must run as the SAME user, or
# the attach fails with
#     OS error: [Errno 13] Permission denied: .../bw_delay_sync_iq
# Either run both as root (`sudo ./scripts/sudo-daq-run.sh` then `sudo ./run_ui.sh`)
# or -- better -- raise usbfs once as root and then run BOTH as your normal user.
#
# Usage:
#   ./run_ui.sh                 # foreground, 0.0.0.0:8080
#   ./run_ui.sh -d              # background, logs to _share/logs/krakensdr_doa/ui.log
#   PORT=9000 ./run_ui.sh
#   PYTHON=/path/to/python ./run_ui.sh

set -u

ROOT=$(cd "$(dirname "$0")" && pwd)
PORT="${PORT:-8080}"
BACKGROUND=0
[ "${1:-}" = "-d" ] && BACKGROUND=1

ok()   { printf '\033[32m[ok]\033[0m %s\n' "$*"; }
bad()  { printf '\033[31m[FAIL]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- interpreter
# Must be able to import numba (the DSP) AND quart/dash (the UI server).
pick_python() {
  local c
  for c in ${PYTHON:-} \
           "$HOME/miniforge3/envs/kraken/bin/python3" \
           "$HOME/miniconda3/envs/kraken/bin/python3" \
           "$HOME/miniforge3/bin/python3" \
           /usr/bin/python3; do
    [ -n "$c" ] || continue
    [ -x "$c" ] || continue
    if "$c" -c "import numba, quart, dash, pyargus" >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  return 1
}

if ! PY=$(pick_python); then
  bad "no interpreter found that can import numba, quart, dash and pyargus"
  echo "     tried: \$PYTHON, ~/miniforge3/envs/kraken/bin/python3, /usr/bin/python3"
  echo "     if quart is the missing piece:  <python> -m pip install quart"
  exit 2
fi
ok "interpreter: $PY"

# ------------------------------------------------------------- shared folders
# maindash constructs the SignalProcessor immediately, which opens
# _share/DOA_value.html -- so this directory must exist before the app starts.
mkdir -p "$ROOT/_share/logs/krakensdr_doa" "$ROOT/_share/logs/heimdall_daq_fw" "$ROOT/_share/records"
ok "shared folders ready under $ROOT/_share"

# ------------------------------------------------------- DAQ accessibility
DAQ_CTL="$ROOT/../heimdall_daq_fw/Firmware/_data_control"
if [ -d "$DAQ_CTL" ]; then
  if [ -w "$DAQ_CTL/bw_delay_sync_iq" ] 2>/dev/null; then
    ok "DAQ control FIFOs are writable -- the UI can attach"
  elif [ -e "$DAQ_CTL/bw_delay_sync_iq" ]; then
    warn "DAQ control FIFOs exist but are NOT writable by $(id -un)"
    echo "     The DoA app will start and serve the page, but with no data."
    echo "     They are owned by: $(stat -c '%U' "$DAQ_CTL/bw_delay_sync_iq" 2>/dev/null)"
    echo "     Run this UI as the same user that runs the DAQ chain."
    echo "       both as root    : sudo ./scripts/sudo-daq-run.sh ; sudo ./run_ui.sh"
    echo "       both as you     : raise usbfs once, then run both without sudo"
  else
    warn "no DAQ control FIFOs yet -- start the DAQ chain first for live data"
  fi
else
  warn "cannot find $DAQ_CTL (expected heimdall_daq_fw as a sibling of this repo)"
fi

# ---------------------------------------------------------------------- run
cd "$ROOT/_ui/_web_interface" || { bad "cannot cd to the web interface"; exit 1; }
LOGFILE="$ROOT/_share/logs/krakensdr_doa/ui.log"

echo
echo "KrakenSDR DoA UI  ->  http://0.0.0.0:${PORT}"
echo

if [ "$BACKGROUND" -eq 1 ]; then
  setsid nohup "$PY" -u app.py >"$LOGFILE" 2>&1 </dev/null &
  PID=$!
  sleep 6
  if kill -0 "$PID" 2>/dev/null; then
    ok "running in background (pid $PID), log: $LOGFILE"
    echo "     stop with: kill $PID"
  else
    bad "it exited immediately -- log follows:"
    tail -20 "$LOGFILE"
    exit 1
  fi
else
  exec "$PY" -u app.py
fi
