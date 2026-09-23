# AI4Science PhysicsNeMo Judge

Code-submission judge and individual leaderboard for the AI4Science Korea 2026
PhysicsNeMo tutorial. Challenges 1-4, all eleven Levels, are included.

**Development pilot, not a public event service.** Scores are provisional.
The web servers bind to loopback only. Do not expose them directly to students
over the internet. GitHub hosts the source, not the running judge.

## What runs where

- Students practice in their own Brev GPU workspaces and export exercise code.
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

The `course` submodule pins the teaching implementation to commit
`101b443ffc134378f3a3602ccdc0b9eae77edc93`. It is the trusted evaluation input,
not a place to upload student code. Updating it requires a fresh scoring state.
An explicit `AI4SCI_COURSE_ROOT` can select another instructor-approved checkout;
its actual source files are fingerprinted so old/new scores are not mixed.

Use the course's Python 3.12 / PhysicsNeMo 2.2.2 environment, following
[its setup guide](https://github.com/yang926/AI4Sci-PhysicsNeMo-Bootcamp/blob/101b443ffc134378f3a3602ccdc0b9eae77edc93/ETC/environment/SETUP.md).
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
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" add-participant participant-001
```

The account command prints a random access code once. Deliver it privately to
that participant. Do not put it in GitHub, shared notebooks, screenshots, URLs,
or chat. Only a hash is stored. A Brev login and a judge account are separate.

Use separate terminals with the same environment and state directory:

```bash
# Local submission page and API. Future student HTTPS ingress must be reviewed.
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" serve --port 8090

# Read-only listener for the instructor's tunnel. No submission/account routes.
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" serve --port 8091 --display-only

# Development worker. Two steps check plumbing, not convergence.
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" worker --device cpu
```

The local submission page is `http://127.0.0.1:8090/`.
The read-only projector page is `http://127.0.0.1:8091/display`.
These addresses refer to the computer running the browser. To view the remote
server from a Mac, establish the tunnel in [connections.md](docs/connections.md).
No tunnel or Brev instance is created by this repository.

The display shows overall and per-Challenge standings, updates every five
seconds and supports full screen and automatic paging for 110 participants.
Disconnected displays retain the last received results with a visible warning.
The pilot total is 400, not an approved event rubric.

## Student submission

The teaching notebook exports a JSON file with `challenge` and `sources`, or
students can select saved exercise `.py` files on the submission page. The server
uses the authenticated account, never a user-supplied identity in the JSON.
Datasets, checkpoints, API keys and local `metrics.json` are not submissions.

The browser needs a reachable, reviewed HTTPS submission URL for the real event.
That ingress is **not implemented or deployed yet**. Do not give students admin
SSH access or the instructor's localhost address as a workaround.

## Tests

```bash
python -m pytest tests -q
# Optional browser suite; requires Chromium and websocket-client:
AI4SCI_TEST_CHROMIUM=/absolute/path/to/chrome python -m pytest tests/test_judge_projector.py -q
```

The CPU training tests run completed fixtures through all eleven Levels without
editing lesson files. Browser tests use disposable state and synthetic people.
GPU timing, eight-worker capacity, 110-person load and production isolation still
need rehearsal on the selected Brev hardware.

Extraction verification on 2026-09-23: 66 core tests passed, including all eleven
Levels through CPU worker subprocesses. The five optional Chromium checks also
passed. Package build checks confirmed all four UI assets are included.

## Repository boundary

This repository contains only application code, tests and documentation. Keep
rosters, credentials, databases, submitted code and private evaluation logs out
of Git. The original teaching checkout remains unchanged; its earlier embedded
judge is retained for compatibility. Use this repository for further judge work.

Source was extracted from the teaching repository's local judge on 2026-09-23;
the separate package, explicit course dependency and read-only listener were
added here. See [NOTICE](NOTICE) for provenance.
