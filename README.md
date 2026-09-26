# AI4Science PhysicsNeMo Judge

Code-submission judge and individual leaderboard for the AI4Science Korea 2026
PhysicsNeMo tutorial. Challenges 1-4, all eleven Levels, are included.

**Development pilot, not a public event service.** Scores are provisional.
The web servers bind to loopback only. Do not expose them directly to students
over the internet. GitHub hosts the source, not the running judge.

## What runs where

- Students practice and submit saved exercise code from their Challenge notebooks.
- One administrator-owned GPU host stores accounts, the queue and results,
  and runs one evaluation worker per GPU.
- The instructor's Mac only opens a read-only scoreboard through an SSH tunnel.
  It does not need PhysicsNeMo, the score database or student access codes.

See [connections and accounts](docs/connections.md),
[scoring and accepted code](docs/pilot.md), and [security limits](SECURITY.md).

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

The account command creates an unnamed private account and prints its random
personal credential once. It does not assign a random nickname. The participant
chooses a nickname in Jupyter before submitting. Provision it
privately into that participant's workspace, outside the teaching checkout and
Jupyter's served directory. Do not put it in GitHub, notebook cells, screenshots,
URLs or chat. Only a hash is stored. Brev-to-judge identity provisioning is not
automatic yet; one shared Launchable credential is never acceptable.

Use separate terminals with the same environment and state directory:

```bash
# Local notebook submission API. Event HTTPS ingress is still deployment work.
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
server from a Mac, establish the tunnel in [connections.md](docs/connections.md).
No tunnel or Brev instance is created by this repository.

The display shows only the current Challenge: rank, participant name and score.
It opens on Challenge 1; the instructor selects Challenges 1-4. Earlier scores
and the overall total are not shown. It updates every five seconds and supports
full screen and automatic paging for 110 participants.
Disconnected displays retain the last received results with a visible warning.
All Challenge scores and the pilot total remain stored on the server.
The pilot total is 400, not an approved event rubric.

## Student submission

In the **Submit your code** section of any Challenge notebook, the participant
enters a **Nickname** and clicks **Register nickname**. The authenticated profile
endpoint saves it to that participant only; the same name is reused in all four
Challenges and the public scoreboard. Names are unique, 1-40 visible characters.
**Save nickname** can correct a name without losing earlier submissions or points.
The API rejects submissions from unnamed accounts. Run All never registers names.

The updated teaching notebooks collect the required functions from saved `.py`
files when the student clicks **Submit code**. The kernel sends `challenge` and
`sources` to `POST /api/submissions`, then reads `GET /api/me` for queue status,
points and evaluation details. Running cells never submits automatically. No
separate website login or file upload is part of the student workflow. The server
uses the authenticated workspace credential, never a supplied name in the JSON.
Datasets, checkpoints, API keys and local `metrics.json` are not submissions.

The student's instance needs a reachable, reviewed HTTPS API URL for the event.
That ingress is **not implemented or deployed yet**. Do not give students admin
SSH access or the instructor's localhost address as a workaround.

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
rosters, credentials, databases, submitted code and private evaluation logs out
of Git. Student notebook controls live in the teaching repository; its earlier
embedded judge is retained for compatibility and local tests. Use this repository
for judge deployment. The pinned scoring submodule is unchanged by UI edits.

Source was extracted from the teaching repository's local judge on 2026-09-23;
the separate package, explicit course dependency and read-only listener were
added here. See [NOTICE](NOTICE) for provenance.
