# Security status

This is a course-specific provisional judge, not a public internet service. Only
instructor-owned fixtures should run on a company workstation. The submitted
code contract is restricted and interpreted; arbitrary uploaded Python is never
imported or executed, but this does not establish a production security boundary.

Keep both HTTP listeners on loopback. The event enrollment service uses private,
restricted reverse SSH to carry student loopback port 8090 to the audit API;
each workspace receives its own bearer credential. This encrypted transport
does not require a public HTTPS endpoint. The instructor's separate tunnel
forwards the read-only `--display-only` listener on port 8091, which rejects
account/submission routes. Do not confuse this viewer with the submission API.

An unfiltered board GET intentionally returns published nicknames, scores and
totals without participant authentication, not tokens, uploads or private history.
The UI's current-Challenge selection is a display choice, not score secrecy.

The configured Launchable contains only the enrollment public key; administrator
private keys, the organization-scoped read key and enrollment token master stay
on audit, outside worker access. First-use student host keys are pinned against
endpoints from the authenticated Brev inventory; changed keys fail closed.
Do not weaken SSH restrictions or HTTP Host/Origin checks to solve connectivity.
Before using a different public-ingress topology, review TLS, origins, production
serving, identity lifecycle, quotas, worker isolation, supervision and retention.

`list-participants` and `revoke-participant --participant ID` are local
administrator CLI commands, not public HTTP routes. Listings omit tokens and
hashes. Revocation permanently disables the credential within that scoring
state, including later enrollment of the same VM, while retaining submissions
and scores. It does not cancel already queued/running work or erase published
scores. Automatic expiry, in-place rotation and unrevocation are not implemented.
See [account lifecycle](docs/connections.md#account-lifecycle).

Keep trusted code/configuration frozen while a state accepts work. Repeated
fingerprint checks intentionally reject changes rather than mixing versions;
do not reduce this to a startup-only check. Use a fresh state for a changed
fingerprint and preserve old results. Deployment templates and tests are not
proof of a live host's resource limits: verify active services and worker limits
on each deployed release.

Never commit credentials, rosters, score databases, student submissions or logs.
The application rejects state inside either repository, and `.gitignore` offers
additional accident protection. It is not a substitute for reviewing every push.

Do not put secrets or live student information in public GitHub issues. Stop the
service and contact the event operator privately if a credential is disclosed.
