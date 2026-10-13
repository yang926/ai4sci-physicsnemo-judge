# Students, judge host and instructor Mac

## Event topology

The supported event path is the enrolled student Launchable and private reverse
SSH, not a public submission website. Verify the running course/judge revisions,
enrollment configuration and workers before opening submissions. Updating these
source files does not establish that the deployed services use the new release.

`example-judge` is an illustrative instance name and SSH alias. Replace it with
the exact judge instance verified through your authorized Brev account. Here,
"audit" refers to that administrator-owned judge host, not a fixed instance name.
Loopback addresses refer to the machine named in each example.

| Connection | Endpoint in that machine | Transport and purpose |
|---|---|---|
| Student notebook kernel → judge API | Student `127.0.0.1:8090` | Private reverse-SSH forward to audit `127.0.0.1:8090`; personal bearer credential |
| Judge workers → queue/results | Private local disk on audit | One host, one SQLite state, one worker per GPU |
| Instructor Mac → scoreboard | Mac `127.0.0.1:8091/display` | Separate instructor SSH forward to audit's read-only port 8091 |

The root-only enrollment service on audit discovers ready VMs through the Brev
organization API. It accepts only server-attested organization/Launchable labels,
then connects to the student's restricted SSH receiver through Brev's gateway.
That connection creates the student's loopback API forward and provisions a
unique private credential. See [automatic enrollment](automatic-connection.md).
Students do not receive administrator SSH access to audit, and the Launchable
does not contain a shared token or an administrator private key.

Use one administrator-owned host with eight GPUs for the current SQLite queue.
Eight independent VMs require a different queue/worker transport; a shared
SQLite network mount is not supported. Student GPUs are for local practice.

The Mac only displays standings. Closing it or losing its tunnel does not stop
supervised grading on audit. Stopping audit pauses enrollment and submissions;
it does not prevent students from running the Challenges locally.

## Instructor-only viewing

For browser-only access without a local SSH process, an operator can configure
the [instructor-only Brev Secure Link](secure-projector.md). It uses a separate
restricted display adapter and leaves ports 8090, 8091 and 8092 unchanged. The following
SSH connection remains available as a fallback.

The [administrator page](admin-operations.md) can have its own owner-only Secure
Link on a separate private-interface listener. Brev login does not replace the
administrator password. Do not attach admin routes to the display adapter or
share the administrator link/password with participants.

After audit's read-only listener is running on port 8091, run on the Mac:

```bash
brev port-forward example-judge --port 8091:8091
```

Verify the actual judge's name with `brev ls` before using it in the command.
Open `http://127.0.0.1:8091/display` on the Mac. Keep both
ports at 8091: the server checks the HTTP Host port, so `8092:8091` returns 403.
If the local port is busy, inspect the existing listener before starting another
forward; do not kill an unrelated process.

The direct SSH alternative, after Brev has created the alias, is:

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:8091:127.0.0.1:8091 example-judge
```

Keepalives detect dead connections; they do not guarantee reconnection after
Mac sleep or a network change. Reconnect and refresh the display. Do not bind
either listener to `0.0.0.0`. The display listener denies authenticated account
and submission routes. Public board responses contain aliases/scores, not
credentials or submitted code; an unfiltered board request can include all
Challenge scores and the stored total. The projector requests only the selected
Challenge by default. Choosing **Overall results** switches to the unfiltered
board and shows C1-C4, total and overall rank; no extra endpoint or token is needed.

[Brev connectivity documentation](https://docs.nvidia.com/brev/cli/connectivity).

## Student connection and nickname

1. The student starts the configured event Launchable and opens Jupyter.
2. Audit's enrollment service verifies that VM's origin, provisions one private
   account and maintains its SSH connection. Enrollment is not instantaneous:
   Brev build readiness, discovery and connection retries must complete.
3. The notebook reads the mode-600 `~/.config/ai4sci/judge.json`, outside the
   course directory served by Jupyter. Its URL is the student's loopback API,
   not the instructor Mac's localhost address.
4. In **Submit your code**, the student enters a public **Nickname** and clicks
   **Register nickname**. The account starts unnamed; no random public nickname
   or Brev account name is assigned. One nickname is used for all four Challenges.
5. The student saves the Python exercises, selects all completed Levels and
   clicks **Submit code**. The kernel sends the saved functions with its private
   credential and reads queue/results in the same panel. No website upload or
   separate judge login is required.

A nickname is a public label, not identity proof. Identity is tied to the
authenticated immutable organization/VM binding. Reconnecting the same VM
retains its account and scores; deleting and replacing a VM creates a new
identity unless an organizer separately handles recovery. Typing someone
else's nickname cannot adopt their account.

The notebook never registers a nickname or submits code through Run All.
Automatic reconnect/read retries are separate from submission; POST requests
are not automatically replayed. The server deduplicates identical submissions.

## Account lifecycle

Manual local rehearsals may use `add-participant` and the private connection
hook below. The configured event uses automatic per-VM provisioning instead of
one manual account command for every learner. Tokens are bearer credentials,
not OTPs or Brev passwords. Only hashes are stored in the judge database;
the root-only enrollment master must remain private.

From an administrator shell on the judge host, set `AI4SCI_JUDGE_STATE` to the
actual private state path, then use:

```bash
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" list-participants
python -m ai4sci_judge --state "$AI4SCI_JUDGE_STATE" revoke-participant --participant PARTICIPANT_ID
```

Replace `PARTICIPANT_ID` with the exact private ID from the listing, not a
nickname or VM name. The listing exposes no tokens or token hashes. Revocation
is permanent within this scoring state: authenticated API calls are denied,
and reenrolling the same VM cannot restore access. Historical submissions,
nicknames and scores remain; revocation does not cancel previously queued or
running grading work or erase the public score. There is no public admin route.

Automatic expiry, in-place credential rotation, unrevocation and transferring
scores to a replacement VM are not implemented. Handle recovery deliberately
rather than deleting account rows or changing tokens behind enrollment's back.
End the event by stopping services and applying the agreed private-data
retention policy; revocation alone is not a storage cleanup policy.

## Manual private connection hook

For a local/manual deployment, the course's `ETC.runtime.judge_client` can
persist an already-provisioned personal `AI4SCI_JUDGE_TOKEN` and
`AI4SCI_JUDGE_URL` from a private environment into the Jupyter user's
`~/.config/ai4sci/judge.json`:

```bash
python -m ETC.runtime.judge_client
```

Run it from the course root, with secrets supplied privately rather than in
the command line or notebook. It does not issue an account, replace an existing
configuration or print the token. Normal event students do not need this step.

The client rejects credential-bearing URLs and authentication redirects and
sends the token only in the Authorization header. Plain HTTP is allowed only
on loopback, including the encrypted event tunnel. Non-loopback URLs require
HTTPS. A browser-authenticated Brev Secure Link is not automatically an API
endpoint: a login-page redirect cannot substitute for API authentication.

## Before opening submissions

- Verify the pinned course/judge fingerprints, private enrollment configuration,
  one-host/eight-worker GPU layout and fixed grading budget on the actual host.
- Keep both HTTP listeners on loopback. A future public-HTTPS topology needs a
  separately reviewed gateway, trusted origins and production serving design;
  do not weaken Host/Origin checks to make a URL work.
- Check automatic enrollment, nickname registration and submission from a
  non-admin participant, not only administrator-created VMs.
- Rehearse burst load, duplicate attempts, GPU/process failures and recovery;
  smaller tests do not prove 110-instance availability or permissions.
- Restrict worker resources, network and access to private enrollment secrets.
  The bounded interpreter is not a sandbox for arbitrary Python.
- Keep private state on local persistent disk, outside Jupyter and Git, with
  tested backups, operator access controls, deadlines and a retention policy.
