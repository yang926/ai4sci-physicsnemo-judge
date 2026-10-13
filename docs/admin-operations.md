# Private judge administration

The separate `judge_admin` package manages the existing event database, using
the judge's shared transactional Challenge lifecycle. It does not initialize
or upgrade production state on startup. Updating judge code requires the
reviewed deployment and scoring-version migration; never bypass its fingerprint
check or silently rewrite scoring history. The same
authenticated administrator server provides an explicitly isolated demo; demo
records and scores are synthetic, not GPU evaluation results or capacity evidence.

The instance alias, Secure Link hostnames, private overlay addresses and state
path below are examples. Use the exact values verified for your deployment and
keep its private configuration outside Git. These instructions do not report the
configuration or status of a running service.

## Access and separation

| Endpoint | Purpose | Access |
| --- | --- | --- |
| `127.0.0.1:8090` | Existing student API | Existing individual credentials/tunnels |
| `127.0.0.1:8091/display` | Existing live projector | Private Mac tunnel or the owner-only [projector Secure Link](secure-projector.md) |
| `127.0.0.1:8092/` | Live administration | Administrator password and authenticated session |
| `127.0.0.1:8092/demo-admin` | Rehearsal administration | Same administrator authentication; separate demo DB/backups/audit |
| `127.0.0.1:8092/demo` | Synthetic projector with DEMO banner | Administrator authentication |
| Optional private overlay `:8094` | Same administration and demo routes | Owner-only Brev HTTPS Secure Link, then the existing administrator password |

The optional projector adapter uses 8093; the optional administrator listener
uses 8094. Neither replaces the local endpoints. The live projector is never
redirected to demo state. The demo uses simulated service transitions, not systemd, student
credentials, or live workers. This is application-level separation within the
same trusted administrator process/account, not a separate security principal.

On the instructor's Mac, keep the existing 8091 tunnel and add:

```bash
brev port-forward example-judge --port 8092:8092
```

Open `http://localhost:8092/` and sign in. This loopback listener remains a
tunnel-only fallback; do not expose its plain HTTP port publicly. Use the current
administrator password. Never put it in a URL, Git, screenshots, logs, or shell
arguments. With `password_store` configured, the service uses an authoritative
salted password verifier, not a retrievable plaintext password. The
`/etc/ai4sci-judge-admin/admin-password` and its systemd `LoadCredential` runtime
copy are legacy bootstrap files, **not the current password** when
`password_store` is configured.

### Change the administrator password

On the live administration page, choose **비밀번호 변경** and enter the current
password, the new password, and its confirmation. The new password must contain
4–128 printable characters. A short PIN is only an inner convenience behind the
owner-only Brev login; retain that outer login and the private network restrictions.
The separate demo page does not expose this global account setting.

The change is saved atomically to the service-owned 0600 verifier file under a
0700 directory. Passwords are salted and hashed with PBKDF2-SHA256 (600,000
iterations); plaintext is not saved. Success expires **all** administrator sessions
on both listeners, including the current one. Sign in again with the new password.
The password survives restarts, and changing it does not change students, scores,
submission credentials, or Challenge states. Both login and current-password
checks are rate-limited. No password or verifier is included in status or audit data.

This design runs one administrator service process; its two listeners share one
authentication lock and session registry. Do not run multiple administrator
processes against the same verifier or replace it externally while the service
is running. An explicitly configured missing, malformed, linked, or incorrectly
permissioned verifier fails closed and never falls back to a bootstrap password.
Credential initialization refuses to overwrite an existing verifier. If the
password is lost, perform a reviewed, private, stop-service recovery rather than
deleting the verifier while the service is running.

### Optional owner-only Brev link

`https://admin-example.gobrev.dev/` is an illustrative administrator link;
replace its hostname with your registered Secure Link hostname.
Brev must require the approved owner's login (`public: false`, only the
instructor's authorized email). After Brev login, use the **current**
administrator password. Do not run a password initializer again. `/demo-admin` and
`/demo` on the same host retain their isolated demo behavior.

To enable this path, add the following to the existing root-owned configuration:

```json
"secure_link": {
  "hostname": "admin-example.gobrev.dev",
  "bind_address": "100.64.0.10",
  "port": 8094,
  "allowed_peers": [
    "100.64.0.21", "100.64.0.22", "100.64.0.23", "100.64.0.10"
  ]
}
```

The sample addresses illustrate three proxy gateways and the judge's own
private address for local health checks. Replace them with the exact current
addresses verified for your Brev deployment. Network policy must also limit
8094 to those exact sources. Never bind to `0.0.0.0`, open a
public TCP port, or allow the entire overlay range. If gateway addresses
change, review and update the exact list; unknown peers fail closed before
HTTP parsing or worker allocation. The listener permits at most eight
concurrent requests and retains the existing management-operation timeout.

This native listener reuses the local handler, password, sessions and storage.
It requires the exact configured Host and an exact HTTPS Origin for every
POST, preserves CSRF checks, and sets a separate port-specific
`Secure; HttpOnly; SameSite=Strict` cookie. Forwarded headers are not trusted.
Only a top-level cross-site GET navigation to `/`, `/admin` or `/demo-admin`
is accepted to support Brev's login return. Cross-site API, download, iframe
and mutation requests remain blocked. There is no database-upload endpoint.

Omitting `secure_link` leaves 8092 as the only administrator listener. A bad
configuration or unavailable second port fails startup and closes the first
socket. The CLI closes both listeners on normal shutdown or SIGTERM. An
administrator-only service restart requires a fresh app login, but does not
change the password, event records, scoring workers or student credentials.
Keep the existing administrator sudo-helper sandbox; do not copy the
projector adapter's `NoNewPrivileges` settings into this service.

Before enabling the link, preserve the old config, validate the owner-only
Brev access policy and private gateway ACL, and record the live fingerprint
and counts. After deployment, verify the real HTTPS login, `/api/status`,
logout, private 8092 fallback, and unchanged fingerprint/counts. Do not reset
the live event merely to test access. Destructive rehearsal belongs only in
the separate demo. Keep passwords, cookies and CSRF tokens out of logs.

## Deployment contract

The supplied service units use `/srv/ai4sci-judge/current`; it and its resolved
release, helper, config, units, and interpreter must not be writable by
`ai4sci-judge`. Both administrator and backup services use
`/srv/ai4sci-judge/venv/bin/python` and that working directory. The course defaults
to the release's pinned `course/`; do
not change `AI4SCI_COURSE_ROOT`, dependencies, or scoring files during this
deployment. Check the stored and computed fingerprint before/after deployment.

Install the following reviewed files with the stated ownership/modes:

| Source | Installed path | Owner/mode |
| --- | --- | --- |
| `ops/ai4sci-judge-control` | `/usr/local/sbin/ai4sci-judge-control` | root:root 0755 |
| `ops/ai4sci-judge-admin.sudoers` | `/etc/sudoers.d/ai4sci-judge-admin` | root:root 0440 |
| `ops/ai4sci-judge-admin.service` | `/etc/systemd/system/ai4sci-judge-admin.service` | root:root 0644 |
| `ops/ai4sci-judge-backup.service` | `/etc/systemd/system/ai4sci-judge-backup.service` | root:root 0644 |
| `ops/ai4sci-judge-backup.timer` | `/etc/systemd/system/ai4sci-judge-backup.timer` | root:root 0644 |

Create `/etc/ai4sci-judge-admin` as root:ai4sci-judge 0750 and its `config.json`
as root:ai4sci-judge 0640. An example configuration is:

```json
{
  "live_state": "/var/lib/ai4sci-judge/state",
  "live_backups": "/var/lib/ai4sci-judge-admin/live-backups",
  "demo_state": "/var/lib/ai4sci-judge-admin/demo-state",
  "demo_backups": "/var/lib/ai4sci-judge-admin/demo-backups",
  "password_store": "/var/lib/ai4sci-judge-admin/auth/password.json",
  "port": 8092,
  "projector_url": "https://scoreboard-example.gobrev.dev/display"
}
```

Set `live_state` to the existing event's actual state path and `projector_url`
to its verified projector URL. The API, display, workers, enrollment and
administrator must agree on the same event state; do not move an existing
database to match this example.

Create `/var/lib/ai4sci-judge-admin`, its `auth` directory, and the two backup
directories as ai4sci-judge:ai4sci-judge 0700, outside both repositories. The
following initializers require new paths and refuse to overwrite existing files/state.
Run from the reviewed root-owned application checkout, not an untrusted tree:

```bash
sudo -u ai4sci-judge /srv/ai4sci-judge/venv/bin/python -m judge_admin init-password-store --file /var/lib/ai4sci-judge-admin/auth/password.json --stdin < /path/to/private/one-time-password
sudo -u ai4sci-judge /srv/ai4sci-judge/venv/bin/python -m judge_admin init-demo --state /var/lib/ai4sci-judge-admin/demo-state
```

Supply one UTF-8 password through private redirected stdin; the CLI refuses an
echoing terminal and accepts no password argument. The input file above is a
placeholder for an operator-controlled 0600 secret, not a repository file. Handle
and retire that temporary input according to the private backup policy. The
verifier itself, not a plaintext copy, must be included in private recovery backups.

`password_store` takes precedence whenever that key is present. The old
`password_file` configuration works only when `password_store` is absent and
retains its 24-character minimum; that legacy mode does not expose password
changes in the UI. Existing systemd `LoadCredential` files may remain for unit
compatibility but have no authentication authority in store mode. For a fresh
unit installation, either retain its required bootstrap file or remove the unused
`LoadCredential` directive through a reviewed unit change; do not leave a mandatory
credential source missing.

Do not run an initializer against `live_state`. Do not replace the enrollment
master, API credentials, state pointers, or existing systemd units.

Before activating, validate `visudo -cf` for the proposed rule and
`systemd-analyze verify` for the three new units. Run CPU tests, verify the
production fingerprint/count baseline, and confirm 8092 is free. Then install
only these files, reload systemd unit definitions, and start/enable only
`ai4sci-judge-admin.service`. The old API/display/enrollment/workers need no
restart. Confirm that the installed helper's `status` works through the actual
administrator unit sandbox; testing sudo from an ordinary SSH shell alone
does not establish this. Inspect `/proc/<admin-PID>/status` and require
`NoNewPrivs: 0`. Some systemd 249 sandbox directives implicitly force this
flag even when `systemctl show NoNewPrivileges` reports false.

Create the first consistent backup with `systemctl start
ai4sci-judge-backup.service`; inspect its successful exit and private manifest.
Then enable/start `ai4sci-judge-backup.timer`. Confirm its next run and the last
backup result. `OnCalendar=hourly`, a delay of up to five minutes, and
`Persistent=true` provide scheduled on-host backups; a timer being enabled
does not prove backups are succeeding. A missed run may execute after startup.
Backup CLI does not require the HTTP password or service-control privileges.

Rehearse pause/reset/restore/resume only under `/demo-admin` during initial
deployment. Compare live counts, fingerprint, and existing service PIDs
afterward. Verify anonymous admin API/demo requests are rejected and that the
Mac's existing live projector continues to work.

## Challenge closure and final results

Each Challenge has its own persistent state: `open`, `closing`, `closed`, or
`finalized`. The administrator page shows all four states, the deadline, and
each Challenge's queued, running, and system-error counts.

- **Close in 5 minutes** changes an open Challenge to `closing`, with a deadline
  five minutes after the server records the action. Submissions remain accepted
  strictly before that deadline. Repeated requests cannot extend the deadline.
- **Close now** closes just the selected Challenge immediately. The deadline
  is enforced in the same database transaction that accepts submissions, including
  after an API restart. Other Challenges keep their existing states.
- Already accepted jobs continue grading. Wait for that Challenge's queued and
  running counts to reach zero. Closing does not itself freeze the displayed scores.
- If eligible attempts have `system_error`, inspect and correct the worker issue,
  then use **Retry system errors**. This requeues the original accepted attempts
  for a closing or closed Challenge, preserving their submitted code and timestamps;
  it does not reopen submissions or rerun completed grades.
- **Finalize results** requires a closed Challenge with no queued, running, or
  unresolved system-error attempts. It saves the Challenge's final score/rank
  snapshot and finalization time. A finalized Challenge cannot accept submissions
  or be reopened through these actions.

These controls change SQLite state without stopping or starting the student API,
enrollment, projector, or workers. Global maintenance pause is a separate operation
for backup recovery and resets. Maintenance resume does not reopen closed or
finalized Challenges. A new rehearsal/event can reopen all Challenges only through
the existing guarded reset workflow below.

`POST /api/challenges/action` accepts exactly `challenge` (`"1"`–`"4"`) and
`action` (`"schedule_close"`, `"close"`, `"retry_errors"`, or `"finalize"`). It
requires the existing administrator session, matching Origin, and CSRF token.
The equivalent `/api/demo/challenges/action` affects only the isolated demo.
Every accepted action records a requested audit entry before the database change
and a completed entry afterward; retry completions include the number requeued.
An action that fails validation never reports completion. `/api/status` includes
server time and all four lifecycle records; the demo status endpoint reports only
demo records.

## Maintenance and reset

Pause stops live API and automatic enrollment, including student transport;
already queued/running jobs continue draining on the eight workers. It does
not stop the projector. Wait for **queued = 0 and running = 0**. Reset/restore
then stop workers, verify all writers are stopped, check the database inside
an immediate transaction, and create a before-operation backup.

- **Reset scores** (`RESET SCORES`) deletes submissions/results and final snapshots,
  and returns all Challenges to `open` with no deadline or finalization time,
  preserving participant registration, nicknames, workspace bindings, and revocations.
- **Reset event** (`RESET EVENT`) also deletes registrations, bindings, and
  revocations. It does not rotate the external enrollment master: previously
  issued VM credentials can be re-enrolled after resume. This is not a
  security credential rotation.
- **Restore** (`RESTORE BACKUP`) accepts a verified backup from this state
  and restores database records, including registrations and revocations at
  snapshot time, as well as Challenge deadlines, closure state, and final snapshots.
  It can undo later revocations. Backups made before lifecycle support are
  incompatible and cannot silently reopen Challenges or discard final results.
  Cross-state, mismatched
  fingerprint/settings/schema, corrupt, or queued/running snapshots are
  rejected. There is no arbitrary uploaded-database restore endpoint.

After reset/restore, inspect the counts and board, then explicitly resume.
Resume starts workers/API/enrollment and checks their running state; existing
systemd ordering starts enrollment after API. A command failure or timeout is
not success: leave maintenance in place and inspect status. A timed-out
systemctl operation may still be completing, so do not assume its prior state
or delete lock files. If a process crashes, verify the actual service state
before continuing; the administrator is not a general systemctl console.

The root-owned helper accepts only one exact verb: `status`, `pause`,
`quiesce`, or `resume`, controlling only the API, enrollment, and workers 0–7.
It imports only isolated system Python's standard library, sanitizes the
environment, serializes calls, and checks transitions. It never reads SQLite,
credentials, request-supplied paths, or arbitrary units. The unprivileged
backend checks queue quiescence. `NoNewPrivileges=true` must **not** be added
to the administrator service because it would disable the fixed sudo gate;
neither should directives that implicitly enable it, such as
`RestrictAddressFamilies` or `ProtectKernelTunables`/`ProtectKernelModules` on
systemd 249. The backup unit can and does use it. Do not grant generic sudo
or systemctl.

## Backup is not complete disaster recovery

Database snapshots use SQLite's backup API, not a copy of only the live WAL
database file. They include a manifest/hash and integrity/schema/fingerprint
checks, including the lifecycle table and finalized score/rank snapshots.
Backups with queued/running rows are consistent historical snapshots
but deliberately not eligible for restore. Backup and maintenance operations
share a cross-process lock. Each context has its own `audit.jsonl`; no live
record or participant credential is needed for a demo.

These backups stay on the same VM and have no automatic pruning policy.
Monitor disk space, backup-service failures, and the last successful backup;
arrange reviewed off-host retention separately. Mode 0400 snapshot files and
0500 snapshot directories prevent casual edits, not a malicious owner/root
from changing them. The audit log is private but not externally tamper-proof.

A DB backup does **not** contain the automatic enrollment token master, Brev
API/SSH credentials, enrollment configuration, administrator password,
service definitions, pinned source/dependencies, or submitted-code artifacts
outside SQLite. Protect those separately under a tested private recovery
procedure. Static `static.tar` design backups are not DB backups. No cloud
snapshot, off-host backup, or whole-VM disaster recovery is claimed here.

Do not change existing state paths for a new event without coordinating API,
display, workers, enrollment configuration, and admin configuration together.
The prior state and its backups should be preserved until retention is
explicitly approved. Reverting the admin deployment requires disabling its
timer/service and removing only its installed capability/configuration after
review; it must not erase the event database or existing backups.
