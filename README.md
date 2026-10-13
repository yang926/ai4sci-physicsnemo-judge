# PhysicsNeMo Judge

Code-submission judge and individual leaderboard for the PhysicsNeMo tutorial.
Challenges 1-4, all eleven Levels, are included.

**Provisional task-completion scoring, not a public internet service.**
The supported event connection uses private reverse-SSH tunnels from the judge
host to enrolled student Launchables. Both HTTP listeners stay on loopback; never expose
them directly over the internet. GitHub hosts source, not the running judge.
Verify the running deployment's pinned versions before opening submissions;
changes in this repository do not prove that a server has been updated.

Instance aliases, Secure Link hostnames and private overlay addresses in these
guides are illustrative. Replace them with the exact values verified for your
deployment. Loopback addresses and documented service ports describe the local
connections; the examples do not identify or attest to a running deployment.

## What runs where

- Students practice and submit saved exercise code from their Challenge notebooks.
- One administrator-owned GPU host stores accounts, the queue and results,
  and runs one evaluation worker per GPU.
- The instructor opens the read-only scoreboard through an SSH tunnel or an
  [instructor-only Brev Secure Link](docs/secure-projector.md).
  A separate authenticated operator listener manages backups and resets.
  Neither page requires PhysicsNeMo or student access codes on the Mac.

See [connections and accounts](docs/connections.md),
[scoring and accepted code](docs/pilot.md), and [security limits](SECURITY.md).

## Administrator tools

The separate `judge_admin` package provides a password-protected operator page
on loopback port 8092. An optional owner-only Brev Secure Link adds a separate
private-interface listener, retaining the administrator password and CSRF checks.
It is never mounted on the student API or projector.
See [operator setup and recovery](docs/admin-operations.md).

- Back up and download a consistent SQLite snapshot, including committed WAL data.
- Close one Challenge now or in five minutes, let accepted jobs finish, retry
  eligible system errors, and finalize that Challenge's score/rank snapshot.
- Pause intake, let queued grading finish, then reset scores or the entire event.
- Preserve a mandatory pre-operation backup and a separate operator audit trail.
- Restore a compatible, verified snapshot from this event while all writers are stopped.
- Rehearse the same controls on an explicitly separate demo DB. The demo's names
  and scores are synthetic display fixtures, not GPU evaluation results.

Challenge closure leaves the API and workers running and persists across restarts.
Finalization requires a closed Challenge with no pending jobs or unresolved system
errors. Maintenance pause/resume does not reopen Challenges.

Resetting scores removes submissions and final snapshots and reopens all four
Challenges; it keeps participant identities, nicknames, workspace bindings and
revocations. Resetting the event clears these too, but does **not** rotate the
external enrollment master: existing VMs can be enrolled again on resume.
Restoring a compatible backup also restores Challenge deadlines, closure state,
final results, registrations, and revocations from that snapshot. Backups from
before lifecycle support are incompatible with this restore workflow.
Reset and restore leave intake paused until the operator explicitly resumes it.
Scheduled on-server backups are not protection against deletion of the entire VM;
download a private copy elsewhere and preserve the matching code/environment and
external enrollment secrets separately.

## Get the source

```bash
git clone --recurse-submodules https://github.com/yang926/ai4sci-physicsnemo-judge.git
cd ai4sci-physicsnemo-judge
```

The `course` submodule pins the teaching implementation to the commit recorded
in this judge release. Check it with `git submodule status course`. It is the
trusted evaluation input, not a place to upload student code. Updating it
requires a fresh scoring state.
An explicit `AI4SCI_COURSE_ROOT` can select another instructor-approved checkout;
its actual source files are fingerprinted so old/new scores are not mixed.

Use the course's Python 3.12 / PhysicsNeMo 2.2.2 environment, following
[its setup guide](course/ETC/environment/SETUP.md).
The lightweight package metadata does **not** install CUDA or the complete
scientific stack. Run from this checkout in the activated course environment:

```bash
python -m ai4sci_judge --help
```

Installing the CLI is optional: `uv pip install --python "$VIRTUAL_ENV/bin/python" -e .`.
If installed away from this source checkout, set `AI4SCI_COURSE_ROOT` explicitly.

## Local rehearsal

Choose a new private state directory outside both repositories and outside
Jupyter's served directory. The example uses a sibling directory:

```bash
export AI4SCI_JUDGE_STATE="$PWD/../ai4sci-private-state"
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" init --steps 2 --device cpu
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" add-participant
```

For this manual local rehearsal, the account command creates an unnamed private account and prints its random
personal credential once. It does not assign a random nickname. The participant
chooses a nickname in Jupyter before submitting. Provision it
privately into that participant's workspace, outside the teaching checkout and
Jupyter's served directory. Do not put it in GitHub, notebook cells, screenshots,
URLs or chat. Only a hash is stored. An event Launchable can instead use
[automatic private enrollment](docs/automatic-connection.md), with a distinct
credential per immutable organization/VM identity. This is not Brev SSO and does
not assign a public nickname. A shared Launchable credential is never acceptable.

Use separate terminals with the same environment and state directory:

```bash
# Submission API: loopback locally, private reverse-SSH transport for the event.
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" serve --port 8090

# Read-only listener for the instructor's tunnel. No submission/account routes.
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" serve --port 8091 --display-only

# Development worker. Two steps check plumbing, not convergence.
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" worker --device cpu
```

The notebook API listens at `http://127.0.0.1:8090` for local rehearsal.
Both `/` and `/display` show standings only; there is no web upload form.
The read-only projector page is `http://127.0.0.1:8091/display`.
These addresses refer to the computer running the browser. To view the remote
server from a Mac, use a configured [Secure Link](docs/secure-projector.md) or
establish the tunnel in [connections.md](docs/connections.md).
These manual `serve` commands do not create a Brev instance or tunnel. The
separately configured enrollment service maintains student API tunnels; the
instructor establishes the display tunnel independently when not using the
optional Secure Link. The link does not expose submissions or administrator tools.

The display opens on Challenge 1: rank, participant name and that Challenge's
score only. Use **View results** to select Challenges 1-4 during the session.
At wrap-up, select **Overall results** for rank, name, C1-C4 scores and total
(400 maximum). Each Challenge contributes its best completed submission;
missing scores contribute zero, and participants with no completed score remain
unranked. This is a manual view change, never an automatic Challenge rotation.
`/display?ranking=overall` opens the overall view directly.
Both views update every five seconds and support full screen and automatic
page changes every five seconds, with ten participants per page.
Page changes use a short directional slide; incoming score changes briefly
highlight only the affected cells. Initial loading and unchanged scores do not
flash. Both effects respect the browser's reduced-motion preference.
Disconnected displays retain the last received results with a visible warning.
All Challenge scores and the pilot total remain stored on the server.
The pilot total is 400, not an approved event rubric.

The projector uses the tutorial's [NVIDIA/event branding](docs/branding.md).
Administrator clones require authorized GitHub access if this repository's
visibility is restricted. The student course is a separate public dependency
and does not clone this repository.

## Student submission

In the **Submit your code** section of any Challenge notebook, the participant
enters a **Nickname** and clicks **Register nickname**. The authenticated profile
endpoint saves it to that participant only; the same name is reused in all four
Challenges and the public scoreboard. Names are unique, 1-40 visible characters.
Choose an alias that does not include a real name, email address or other personal
information; nicknames appear on the event scoreboard.
**Save nickname** can correct a name without losing earlier submissions or points.
The API rejects submissions from unnamed accounts. Run All never registers names.

The updated teaching notebooks collect the required functions from saved `.py`
files when the student clicks **Submit code**. The kernel sends `challenge` and
`sources` to `POST /api/submissions`, then reads `GET /api/me` for queue status,
points and evaluation details. Running cells never submits automatically. No
separate website login or file upload is part of the student workflow. The server
uses the authenticated workspace credential, never a supplied name in the JSON.
Datasets, checkpoints, API keys and local `metrics.json` are not submissions.

With automatic enrollment configured, the Launchable kernel uses its own
`http://127.0.0.1:8090`, carried through the private reverse-SSH connection to the
judge API. A unique credential is provisioned outside the notebook directory.
This student loopback address is not the Mac's display address. No public HTTPS
endpoint or student administrator SSH access is needed for this topology.
Stopping the judge host pauses submissions, not local practice. A different
public-HTTPS deployment would need its own reviewed ingress, origins and isolation
controls.

## Revoke a participant credential

Run these only in an administrator shell on the judge host, using its private
state directory. `PARTICIPANT_ID` means the exact private ID from the first
command, not a nickname, email or Brev instance name:

```bash
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" list-participants
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" revoke-participant --participant PARTICIPANT_ID
```

The listing contains account IDs, nicknames and revocation status, never tokens
or hashes. Revocation permanently disables that credential for this scoring
state and preserves submissions and scores. It is not score deletion or token
rotation, and restarting enrollment cannot reactivate the same workspace.
There is no public administration endpoint or automatic token-expiry policy.
See [account lifecycle](docs/connections.md#account-lifecycle) for the limits.

## Tests

```bash
python -m pytest tests -q
# Optional browser suite; requires Chromium and websocket-client:
AI4SCI_TEST_CHROMIUM=/absolute/path/to/chrome python -m pytest tests/test_judge_projector.py -q
# Optional compatibility override (the pinned course bridge runs in the full suite):
AI4SCI_NOTEBOOK_ROOT=/absolute/path/to/updated-course python -m pytest tests/test_notebook_bridge.py -q
```

The CPU training tests run completed fixtures through all eleven Levels without
editing lesson files. The required cross-repository rehearsal registers one notebook
nickname, submits all eleven Levels as that account, reads personal results,
and verifies the separate service's Challenge-only display responses. Browser
tests use disposable state and synthetic people.
GPU timing, eight-worker capacity, 110-person load and production isolation still
need rehearsal on the selected Brev hardware.

The test suite includes all eleven Levels through CPU worker subprocesses.
Optional Chromium checks cover the read-only standings at `/` and `/display`.
The course's `ETC/tests/test_notebook_submission.py` covers the notebook client
and controls. A release must pin a course with a compatible notebook client;
the bridge fails instead of skipping when that integration is missing. The
submodule is not automatically updated by changes to the student-facing UI.

## Repository boundary

This repository contains only application code, tests and documentation. Keep
rosters, credentials, databases, backups, submitted code and private evaluation
logs out of Git. This source archive excludes private operational records and
runtime state. Its Git history has been sanitized and commit identities
anonymized; it is not a backup of a running event.

Student notebook controls live in the teaching repository; its earlier
embedded judge is retained for compatibility and local tests. Use this repository
for judge deployment. The pinned scoring submodule is unchanged by UI edits.

Source was extracted from the teaching repository's local judge on 2026-09-23;
the separate package, explicit course dependency and read-only listener were
added here. See [NOTICE](NOTICE) for provenance.
