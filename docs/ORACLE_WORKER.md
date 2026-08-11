# Running your own always-on Optimize worker (Oracle Cloud, free)

This is the owner's runbook for setting up a **free, always-on computer** that
does the heavy "Start deep search" work for the Anvitech app. You do not need
to be a programmer to follow this — it's mostly copy-and-paste.

**Why bother?** Today, "Start deep search" either runs on our free GitHub
compute (a few minutes, decent) or, if that's unavailable, on Render's tiny
free server (slow). A dedicated free Oracle Cloud machine is faster and always
on, so deep searches finish quicker and can search harder (see the "deep knob"
in step 6). If you skip this whole runbook, **nothing breaks** — the app just
keeps using GitHub/local like it does today.

---

## 1. Create the Oracle Cloud account (free)

1. Go to https://www.oracle.com/cloud/free/ and sign up for an **Always Free**
   account.
2. Oracle will ask for a credit card during signup. This is only to confirm
   you're a real person — the "Always Free" resources we use here (the small
   ARM virtual machine) do not get charged. Pick the free/no-spend tier and do
   not enable any paid upgrade.
3. Finish account verification (email + phone, as prompted).

## 2. Create the virtual machine (VM)

This is the "computer in the cloud" that will run the worker.

1. In the Oracle Cloud console, go to **Compute → Instances → Create Instance**.
2. Name it something like `anvitech-worker`.
3. **Image:** choose **Ubuntu 22.04** (or newer).
4. **Shape:** choose **VM.Standard.A1.Flex** (this is the free ARM shape) and
   set it to **4 OCPU / 24 GB memory** — the maximum the free tier allows.
5. **Networking:** leave the default. Do **not** open any inbound ports beyond
   the default SSH port (22) — this worker only ever calls *out* to our app
   and to GitHub, it never needs anyone to connect *in* to it.
6. When you create the instance, Oracle will offer to download an SSH key pair
   (a `.key` file). **Save this file somewhere safe** — you need it to log in.
7. Click **Create**. Wait a couple of minutes for the VM to show status
   **Running**, then note its **Public IP address** (shown on the instance
   page) — you'll need it in step 4.

## 3. Create a GitHub access token (read-only)

The worker needs to download (but never change) the app's code, so it always
runs the latest version.

1. Go to https://github.com/settings/personal-access-tokens/new (GitHub →
   Settings → Developer settings → Personal access tokens → Fine-grained
   tokens).
2. **Repository access:** select **Only select repositories** and pick this
   repo (`riittiin/anvitech-ppc-engine`) — nothing else.
3. **Permissions:** under Repository permissions, set **Contents: Read-only**.
   Leave everything else as "No access."
4. Generate the token and **copy it immediately** (GitHub only shows it once).
   This is the value you'll paste as "GitHub read-only token" in step 4.

## 4. Log into the VM and run the setup script

1. From your own computer's terminal, SSH into the VM (replace the path and
   IP with your own):
   ```
   ssh -i /path/to/your-oracle-key.key ubuntu@<the VM's public IP>
   ```
2. Download the setup script from the repo and run it:
   ```
   curl -O https://raw.githubusercontent.com/riittiin/anvitech-ppc-engine/main/scripts/oracle_worker_setup.sh
   bash oracle_worker_setup.sh
   ```
3. The script will ask you four questions — paste these values when
   prompted:
   - **App URL** — the live app's web address, e.g.
     `https://anvitech-ppc.onrender.com`
   - **OPTIMIZE_WORKER_SECRET** — the same secret value that's already set on
     Render (ask whoever manages the Render dashboard, or look it up there
     under Environment).
   - **GitHub read-only token** — the token you created in step 3.
   - (It will also ask for the GitHub repo name — just press Enter to accept
     the default.)
4. The script installs everything it needs and starts the worker as a
   background service that automatically restarts if the VM reboots. That's
   it — you're done.

## 5. Verify it's working

1. Check the worker service is running:
   ```
   sudo systemctl status anvitech-optimize-worker
   ```
   You should see `active (running)` in green.
2. Watch it live:
   ```
   journalctl -u anvitech-optimize-worker -f
   ```
   This streams the worker's log. Leave this open.
3. In another browser tab, log into the Anvitech app as admin and press
   **"Start deep search."**
4. Within a few seconds you should see a line like `poller: claiming job ...`
   appear in the log you're watching — that's your Oracle box picking up the
   work instead of GitHub or Render. When it finishes you'll see
   `poller: job ... finished rc=0`, and the app's Optimize panel will show the
   result as usual.

## 6. Render settings to double-check

On the Render dashboard, under the app's Environment tab, make sure these are
set:

**Prerequisite:** the Oracle claim window only engages when both `GITHUB_DISPATCH_TOKEN`
and `OPTIMIZE_WORKER_SECRET` are already set on Render. They are set on the live
deployment, so you should not need to add them — but if the worker never picks up jobs,
confirm both are present.

- `ORACLE_CLAIM_TIMEOUT_MIN=3` — this is already the default even if you don't
  set it. It's how long the app waits for your Oracle box to pick up a job
  before it falls back to GitHub. You don't need to change this unless you
  want a shorter/longer wait.
- `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE=300` — this is the "deep search" knob.
  Because your own dedicated box is faster and always available, it can afford
  to search harder than the shared GitHub runner does by default. Setting this
  makes every deep search more thorough. (You can leave this unset if you'd
  rather keep the current search depth — the app works fine either way.)

## 7. Day-to-day operation

- **Updates:** you never need to log in and update anything by hand. Every
  time it picks up a job, the box automatically pulls the latest code from
  `main` first, so it's always current with whatever's been shipped.
- **Restarting the worker** (e.g. after you change a setting): SSH back in and
  run:
  ```
  sudo systemctl restart anvitech-optimize-worker
  ```
- **Retiring the box** (if you ever want to stop using it): on Render, set
  `ORACLE_CLAIM_TIMEOUT_MIN=0` (this tells the app to stop waiting for any
  Oracle worker and go straight back to GitHub/local), then simply delete the
  VM instance in the Oracle Cloud console. Nothing else needs to change — the
  app keeps working exactly as it did before you set this up.

---

## Failure modes (what happens when things go wrong)

You don't need to do anything for any of these — they're all handled
automatically. This table is here so you know the app never depends on the
Oracle box being perfectly reliable.

| failure | behavior |
|---|---|
| Box down / rebooting | claim window (3 min) expires → GitHub tier → local watchdog. Button never dies. |
| Box crashes mid-job | claimed but no result → existing 40-min watchdog → local (same as a dead GitHub run today). |
| Render redeploys new engine code | box `git reset`s to `origin/main` before every job — never runs stale code against a new app. |
| App asleep (free Render) | worker's existing wake-tolerant retries (already built for GitHub) apply unchanged. |
| Both Oracle and GitHub compute one job | first `/optimize/result` wins; second 409s (existing guard). Wasted minutes only. |
| Secret mismatch | 403 on poll; poller logs and keeps polling — visible in `journalctl`, never crashes. |
