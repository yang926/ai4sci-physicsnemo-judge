# Event Launchable connection

The student workflow is Launchable → Jupyter → register a nickname → submit code.
The instructor's laptop displays the scoreboard; it is not a submission relay.

The root-only enrollment service runs on the audit VM. Its organization-scoped
read-only Brev key discovers running VMs whose server-provided Launchable and
organization labels match the event. It excludes audit and unrelated instances.
No participant list needs to be entered for each new VM.

The event Launchable installs a dedicated public SSH key with a forced receiver,
no interactive shell, agent, X11 or PTY access. Forwarding is restricted to
`127.0.0.1:8090`. The audit service opens and maintains the encrypted connection
directly to each student through Brev's SSH gateway, then sends a unique personal
judge configuration over SSH stdin. The student config is mode 600, outside the
notebook directory. The template contains no secret or shared participant token.

Credentials are derived from a private server-side master and immutable
organization/VM IDs. Reconnecting the same VM keeps its account, nickname and
scores. A nickname is a public label, not identity proof or a Brev SSO login.
A deleted/replaced VM is a new account unless the organizer explicitly performs
an identity recovery; it must not claim another account by typing its nickname.

## Security and lifecycle

- Keep `/var/lib/ai4sci-enrollment` root-only. GPU workers must not read it.
- Use an event-expiring **read-only** Brev key; never copy a personal Brev login
  credential, administrator SSH key or enrollment master into a student VM.
- Host keys are pinned per immutable VM ID at first connection over the endpoint
  supplied by the authenticated Brev API. This is first-use trust, not a Brev-
  signed host certificate. Changed keys fail closed and need organizer review.
- An unavailable judge does not stop local practice. The notebook retries only
  read requests; it does not automatically resubmit code or register a nickname.
- The daemon reconnects interrupted SSH sessions. Stopping audit pauses remote
  submissions; starting it resumes its enabled services. Existing files persist.
- The projector listener remains read-only on port 8091. No public judge firewall
  port or anonymous enrollment endpoint is required.

`ops/ai4sci-enrollment.service` is the service template. Its private JSON config
selects organization, Launchable, audit ID, state directory and secret-file paths.
Deploy and verify these together; the template alone is not a ready deployment.

This transport does not change the task-completion rubric, learner code,
original physics problems or training settings. Use a new private state when the
version fingerprint changes. Retain previous states rather than mixing scores.
