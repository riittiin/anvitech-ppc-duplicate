#!/usr/bin/env bash
# One-shot setup for the Anvitech always-on Optimize worker (Oracle free ARM VM).
# Run as a user with sudo on a fresh Ubuntu box:  bash oracle_worker_setup.sh
set -euo pipefail

echo "== Anvitech Optimize worker setup =="
read -rp "App URL (e.g. https://anvitech-ppc.onrender.com): " APP_URL
read -rp "OPTIMIZE_WORKER_SECRET (same value as on Render): " SECRET
read -rp "GitHub read-only token (fine-grained PAT, Contents:read): " PAT
read -rp "GitHub repo (default riittiin/anvitech-ppc-engine): " REPO
REPO=${REPO:-riittiin/anvitech-ppc-engine}

sudo apt-get update -y && sudo apt-get install -y python3 python3-pip git

REPO_DIR="$HOME/anvitech-ppc-engine"
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone "https://x-access-token:${PAT}@github.com/${REPO}.git" "$REPO_DIR"
fi
python3 -m pip install --user -r "$REPO_DIR/requirements.txt"

# The CP solver. PINNED, and deliberately NOT in requirements.txt: Render runs
# only the replay path and must never have it (cp_engine's replay modules are
# tested to import without pyjobshop or ortools). But THIS box is where a cp
# deep search actually solves, so without it every cp contest fails here and
# there is NO local fallback to catch it — Render cannot solve either.
#
# The version is pinned because cp_engine reaches pyjobshop's INTERNAL API
# (CPModel(data).model / .variables / assign_vars) for Rule 1's per-shift roster
# and the fairness objective; tests/test_cp_escape_hatch.py is the canary.
#
# ALREADY BUILT A BOX BEFORE 2026-08-14? Run this one line on it:
#   python3 -m pip install --user pyjobshop==0.0.9
python3 -m pip install --user pyjobshop==0.0.9

sudo tee /etc/anvitech-worker.env >/dev/null <<EOF
APP_URL=${APP_URL}
OPTIMIZE_WORKER_SECRET=${SECRET}
REPO_DIR=${REPO_DIR}
EOF
sudo chmod 600 /etc/anvitech-worker.env

sudo tee /etc/systemd/system/anvitech-optimize-worker.service >/dev/null <<EOF
[Unit]
Description=Anvitech Optimize worker (poll-and-claim)
After=network-online.target
Wants=network-online.target

[Service]
User=${USER}
EnvironmentFile=/etc/anvitech-worker.env
ExecStart=/usr/bin/python3 ${REPO_DIR}/scripts/oracle_optimize_worker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now anvitech-optimize-worker
echo "== done. Check: sudo systemctl status anvitech-optimize-worker =="
