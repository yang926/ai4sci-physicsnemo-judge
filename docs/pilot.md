# Challenge 1-4 scoring pilot

This course-specific service supports local rehearsal and the event's enrolled
Launchable/private reverse-SSH topology. It is not a public internet endpoint;
the running deployment's pinned revisions still need release verification.
It accepts individual submissions, queues fixed-budget evaluations and displays
standings for the selected Challenge or an instructor-selected overall view.
All eleven Levels are included: Wave,
Fluid, Climate, and Neural Operators (FNO, AFNO and PINO).

## Start locally

Use the course Python environment and run these commands from this judge repository
root in the course Python environment. Keep judge state outside the repository and outside Jupyter's served
directory. The following path is an example private directory, not a URL.

```bash
python -m ai4sci_judge --state /srv/ai4sci-judge-state init --steps 2 --device cpu
python -m ai4sci_judge --state /srv/ai4sci-judge-state add-participant
```

The second command prints a randomly generated participant access code once.
Provision each personal code privately into its workspace, outside the course and Jupyter root. It is not a Brev credential.
Without an optional name, `add-participant` creates an unnamed private account.
The student enters **Nickname** and clicks **Register nickname** in the notebook
submission panel. No random public nickname is assigned. The name is saved to
the authenticated account and reused in all four Challenges and the scoreboard.
Brev account names are not fetched automatically. Without a valid personal
credential, submission is rejected, not recorded anonymously.
Only its hash is stored in the database. Do not put codes in URLs, notebooks,
Git, screenshots or shell command arguments.

Start the submission API, read-only display and worker in separate terminals:

```bash
python -m ai4sci_judge --state /srv/ai4sci-judge-state serve --port 8090
python -m ai4sci_judge --state /srv/ai4sci-judge-state serve --port 8091 --display-only
python -m ai4sci_judge --state /srv/ai4sci-judge-state worker --device cpu
```

The submission API is `http://127.0.0.1:8090` on the judge host. Automatic event
enrollment carries the student's own loopback port 8090 to that API through a
restricted reverse-SSH connection. The instructor separately forwards port
8091 to the Mac for viewing. These manual `serve` commands create no tunnels.
See [connections](connections.md); the Mac display URL is not the student API
address. Keep both HTTP listeners off the public internet.

## Projector and notebook

Both `/` and `/display` show the read-only scoreboard. There is no web upload
form. Open `http://127.0.0.1:8091/display` on the
host or on the Mac after forwarding port 8091. These are local addresses, not
public event links. Students submit and read their personal results inside
the Challenge notebook, using a privately configured workspace credential.

The projector opens on Challenge 1 with three columns: rank, registered name
and score for that Challenge. Select Challenges 1-4 under **View results** during
the session; no earlier scores or totals are shown in these views. Select
**Overall results** at wrap-up to show seven columns: rank, name, C1-C4 and total.
Overall uses the server's sum of each Challenge's best completed submission,
out of 400. Missing Challenges add zero; someone without any completed score
stays unranked with a dash instead of a misleading scored zero. Ties share a rank.
Queue counts follow the selected scope; the registered count covers all named
participants. Neither automatic paging nor refresh changes the selected view.
Large, high-contrast text and a full-screen button support projection. The page
requests only the selected Challenge until Overall is explicitly selected.
On narrow screens, the seven-column table scrolls horizontally within its region.
It has no login
form, access code, source upload, or personal submission history.
Use suitable public aliases when issuing accounts; do not register email
addresses or other private information as nicknames.

Scores refresh every five seconds. Pages advance every five seconds, showing
ten people per page (or the remaining participants on the last page). Shorter
screens keep the same page size. Auto paging can be paused; arrow buttons or
left/right keys navigate manually. Reduced-motion
preferences disable automatic paging initially. Ties retain the server's joint
ranks; zero is distinct from an ungraded dash. All 110 participants are reachable
through the pages, including participants without a completed score.

`/display?ranking=4&rotate=0` opens Challenge 4 with automatic paging paused.
Choose `ranking=1` through `4`, or `ranking=overall` for overall results;
`rotate=1` enables paging. Invalid view values fall back to Challenge 1.
All scores and totals remain on the server.
The URL saves only these public display settings, never an access code.

The board API is intentionally readable without a participant token on the
loopback listeners. A query selecting one Challenge returns that view; an
unfiltered `/api/board` returns all published Challenge scores and totals.
Neither response includes private tokens or submitted code. The projector
requests only its selected Challenge, or the unfiltered response in Overall.
View selection is not an access-control rule hiding other published scores.

A lost connection keeps the last received scores, shows a warning and receipt
time, and pauses automatic paging until connection recovery. "Submissions scored"
counts completed submissions in the selected scope, not distinct participants.
Switching views clears old scores until new results arrive. No fictitious scores
are inserted by the application, and the **Preview · provisional scoring** note remains visible
until a separately reviewed event rubric is implemented.

Two training steps verify the plumbing only. Create a fresh state with an
agreed training budget for meaningful rehearsal. `init` refuses to overwrite
an existing database. Trusted code, configuration or dependency changes require
a new pilot state, so results from different versions are never mixed.
The rubric is `bootcamp-task-completion-pilot-v3`. Preserve v1/v2 databases; create
a fresh v3 state so earlier PDE-only scores are not mixed with complete-task scores.
The event numerical-feedback budget remains **200 steps**, with the existing
**600-second whole-submission limit**, including all selected Levels. This is
separate from the longer student practice recipes. Rehearse the exact deployed
release at those settings; a two-step local test does not establish its GPU time.

## Student workflow

1. Leave `USE_REFERENCE = False` and complete the linked `.py` exercises:
   equations/speed/conditions for Wave; equations/conditions/geometry for Fluid;
   equations/parameters/conditions/analytic solutions for Climate; dataset/model
   functions and PINO physics for Operators. See [the task mapping](../course/ETC/course_materials/CHALLENGE_CONTRACTS.md).
2. Run the final notebook cell. Before the first submission, enter **Nickname**
   and click **Register nickname**. Names must be unique and contain 1-40 visible
   characters. The same name is reused across all four Challenges. Select the
   completed Levels. All Levels start selected; Ctrl/Cmd-click to deselect an
   unfinished Level. A selected unfinished function blocks the whole attempt.
   Running this cell or Run All does not register or submit.
3. Click **Submit code**. The notebook sends saved exercise functions directly
   to the judge API; there is no separate login or website upload.
4. Read queue status, points and evaluation details in the same panel. Pending
   jobs refresh every five seconds. Use **Refresh results** to check again.

**Save nickname** can correct a name without changing the account or its scores.
An unnamed account cannot submit and is not shown on the public scoreboard.
The projector has no nickname or account-editing controls.

The event Launchable provisions private per-workspace accounts automatically;
the manual `add-participant` command above is for local/operator-managed cases.
Administrator-only `list-participants` and `revoke-participant --participant ID`
operate on the judge host. Revocation preserves scores and permanently blocks
that credential in this state; enrollment cannot reactivate the same VM.
It does not rotate tokens, cancel existing work or erase published scores.
See [account lifecycle](connections.md#account-lifecycle) before using it.

The server uses the authenticated participant identity, never a name supplied
inside the uploaded file. A submission is one immutable source snapshot.
Omitted Levels score zero. For a later attempt, include the earlier completed
Levels again if you want them counted. Best *submission* scores are retained;
Levels from different attempts are not silently stitched together.

## Accepted code

The submitted file is **not imported or executed as Python**. For Challenges
1-3, the evaluator extracts all required `student_*` functions and interprets a
restricted language: local assignments, tuple unpacking, bounded literal
dictionaries/tuples, supplied parameter dictionaries, arithmetic, `.diff()`,
`sin()`, `cos()` and `exp()`. Geometry is bounded rectangle data, not executable
CSG code. Old PDE-only submissions receive an explicit update/completion error.
The notebook checks `submission_contract: 3` from the authenticated account API
before sending code. A new course cannot silently use an older PDE-only judge.
Keep the original argument names and order. Numerical defaults from uploaded
code are not evaluated; the trusted lesson supplies its own defaults.

For condition, wave-speed and analytic-solution comparisons, written decimal
coefficients are interpreted exactly before arithmetic on both sides. For example,
`(x-.3)**2` and `x**2-.6*x+.09` receive the same credit. Extra nonzero terms do
not disappear just because they are smaller than floating-point precision.
Numeric literal length and exponent limits prevent oversized rational allocation.
The existing numeric checks for geometry and physical parameters are unchanged.
Positive and negative powers, reciprocal denominators and expression structure
all count toward symbolic-comparison limits; over-budget expressions are rejected
before expansion for PDEs as well as conditions, speed and analytic solutions.

Imports, loops, branches, helper-function calls, decorators, file/network access,
arbitrary attributes and higher derivatives are not supported. Reference
function calls are not available in the interpreter. Uploads are limited to
64 KiB per lesson and bounded syntax/expression complexity. Other parts of a
full uploaded lesson file are ignored, including `main()` and imports.

For Challenge 4, the interpreter extracts `build_datasets`, `build_model` and,
for PINO, `ReactionDiffusionPDE.__init__`. The accepted constructor patterns are
`TensorDataset(*pairs)`, `TensorDataset(pairs[0], pairs[1])`, `FNO(...)` and
`AFNO(...)`, with the original keyword settings and `**model_config`. Local
assignments, tuple unpacking and a bounded comprehension over the three supplied
pairs are supported. AFNO grid checks may use `%`, comparisons, `any`, `all`,
`assert` or an `if` containing a single `raise`, without an `else` branch.
Keep model channels, dimensions and every configuration value unchanged.

Model calls first produce inert descriptions. Only after comparison with the
frozen configuration does trusted code construct the specified PhysicsNeMo model.
An oversized or different model is never instantiated from a submission. Dataset
checks reject swapped splits, changed targets or repeated normalization. AFNO
must explicitly reject a grid incompatible with either patch dimension. PINO
uses the existing `Symbol`/`Function` declarations and `self.equations` assignment;
its residual must match `u - u_xx - u_yy - f`. The validated submitted expression
is passed to PhysicsInformer and cross-checked against an independent FFT residual.

The server owns `build_physics`, normalization, loaders and training. Those
functions and the local YAML files are not accepted as student overrides.

This is a deliberate course-specific contract, **not** a sandbox for arbitrary
Python submissions. Replacing it with `exec`, imports of uploads or unrestricted
subprocess commands would require a new isolation design.

## Provisional scoring

Rules live in [catalog.py](../ai4sci_judge/catalog.py). The board API includes the
active rubric and source/configuration fingerprint; the simple projector page
shows the selected Challenge or overall standings and queue counts. Inspect the private
state settings for the fixed numerical-feedback budget.

Each Level has 100 pilot points:

- 100 implementation points, split equally over required checks. Challenges 1-3
  include PDE residuals, conditions, wave speed, chip geometry, climate coefficients
  and baseline analytic expressions where requested. Challenge 4 checks data,
  specified model construction and PINO physics. Symbolic coefficients catch
  missing terms hidden by zero training defaults. Use the documented residual
  form; arbitrary rescaling is not accepted.
- Zero numerical-quality points. After **all** components match, trusted code
  trains with the fixed budget/seed and returns independent numerical diagnostics.
  These errors are feedback, not optimizer-tuning competition points. Fully
  correct implementations tie; there is no hidden runtime or submission-time
  tiebreaker.

Wrong, well-formed implementations retain credit for their passed components
and skip training. Missing, unfinished or unsupported functions invalidate the
Level and receive an explicit diagnostic; no instructor fallback is used.
All submitted implementation checks are recorded before numerical training.
A training failure, nonfinite feedback or timeout after this checkpoint preserves
the already-checked implementation points and labels numerical feedback failed,
unavailable or timed out; it never manufactures successful numerical metrics.
A failure before the implementation checkpoint produces a server error without
inventing a score. Previously recorded best scores remain unchanged.

Level scores are averaged within each Challenge: 3 Wave, 3 Fluid, 2 Climate,
3 Neural Operators.
The board retains each participant's highest completed submission per Challenge.
The pilot total is **400** (100 per Challenge), still not an approved event rubric.
Ranking uses the displayed two-decimal scores and assigns joint ranks.
Participants without a completed submission are unranked. Server failures and
timeouts do not replace an existing best score with zero.

For Fluid, feedback reports unweighted PDE residuals, boundary and flux errors.
Level 1 also compares all supplied OpenFOAM points and exports matched-scale
reference/prediction/error plots. None of these numerical errors contributes points. Wave Levels 2-3 and
Fluid Levels 2-3 have no supplied independent solution truth. Residual checks
alone do not establish complete physical accuracy.

Challenge 4 numerical feedback uses test relative L2, physical-unit RMSE and independent
FFT PDE RMSE, not training loss. All three models use the same server-generated
periodic reaction-diffusion dataset and the original 64x64 course model configs.
The pilot uses **64/16/16 train/validation/test samples**, max Fourier mode 6,
and fixed seed 1729 (distinct split streams). This is smaller than the lesson's
8,000/1,000/1,000 dataset and is explicitly a throughput/development pilot.
Change and calibrate these frozen settings before claiming event readiness;
doing so requires a new scoring state. Data are generated once per submission,
then reused across its three models. Normalization uses training data only.

Reference answers remain visible in the teaching repository. This system does
not claim to detect copying or prove independent work. With fixed training and
equivalent equations many participants can tie, which is appropriate for a
completion-oriented workshop. A model-optimization competition needs a different
submission contract and rubric.

## Workers and recovery

The current queue uses SQLite on **one host/local disk**, with atomic claims.
For a single host with eight GPUs, create a separate CUDA pilot state using
`init --device cuda`, then run eight worker processes with `--device cuda --gpu 0`
through `--gpu 7`. Each process receives only its assigned GPU through
`CUDA_VISIBLE_DEVICES`. Do not mix CPU and CUDA runs in one scoring session.
Use identical GPU models and recheck runtime on the pinned event release.
Earlier hardware rehearsals are not proof that an updated release has been
tested, nor a guarantee of 110 student GPU allocations.

Eight separate Brev VMs are **not** supported by sharing the SQLite database over
a network mount. That topology needs a central queue/database and remote worker
transport. The single-host design must not be presented as already supporting it.

Each participant may have one queued/running submission at a time. There is a
15-second cooldown, 30 attempts per Challenge, duplicate-source reuse, and a
600-second whole-submission limit. A worker recovering an expired lease marks
the interrupted job as a server error; the participant can resubmit. Completed
scores survive process restarts. A worker that finishes an expired lease cannot
overwrite the recovered state. If all workers stop, start a worker to recover
expired jobs. Inspect private `runs/<submission-id>-*/runner.log` for failures.
Run records are retained; there is no automatic cleanup or retention policy yet.

## Before the event

- Agree on and calibrate the rubric, accepted changes, learning budget,
  submission limit, deadline and whether the board is formative or competitive.
- Benchmark all Levels on the actual GPU, including incorrect, slow and
  nonfinite cases. Rehearse a 110-participant burst and resubmissions.
- Verify private reverse-SSH enrollment, credential revocation, operator access,
  process supervision, backups, quotas, deadlines and data-retention controls.
  Current HTTP serving is loopback-only, with same-origin/Host checks and no CORS.
  A future public-HTTPS deployment needs separately reviewed production ingress;
  it is not required by the event's private-tunnel transport.
- Keep the control database and secrets out of student-accessible machines.
  The restricted interpreter is not a general security boundary. Deploy workers
  in disposable, restricted environments with resource/network limits and review
  the deployment before accepting externally supplied submissions.
- Do not run untrusted student submissions directly on a company workstation.
  Local development should use instructor-owned test fixtures only.

## Verification

```bash
python -m pytest tests/test_judge.py tests/test_judge_training.py tests/test_judge_operators.py -q
```

The training tests submit all eleven Levels through the queue and child-process
runner using completed exercise implementations as **test fixtures**, for two CPU steps each.
They do not alter student exercise files and do not certify convergence, GPU
capacity, production security, or a public event deployment.

Local verification on 2026-09-23 also executed all four modified notebooks as
separate reference-mode copies, including their plots and submission panels.
Those checks covered the earlier upload UI. The current workflow replaces it
with notebook controls; browser regressions now require a display-only root.
Existing learner notebook outputs were preserved, not cleared or certified.

Optional real-browser regression tests (requires Chromium and websocket-client):

```bash
AI4SCI_TEST_CHROMIUM=/absolute/path/to/chrome python -m pytest tests/test_judge_projector.py -q
```

These use disposable private state and synthetic 110-person data, never event
results. They check 1920x1080/1366x768 projection layouts, narrow-screen overflow,
pagination, joint ranks, zero/empty states, loss and recovery of connection,
public-page privacy, unsafe nickname escaping, and the absence of upload/login controls.
Without the browser executable setting the optional tests are skipped.
The page uses local system fonts, including Apple SD Gothic Neo and Malgun
Gothic fallbacks. A Linux screenshot host needs an installed Korean font; it
does not download fonts or analytics from an external service.
