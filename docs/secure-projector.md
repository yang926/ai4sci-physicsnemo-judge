# Open the scoreboard through Brev

The optional adapter replaces the instructor's repeated port-forward command
with a Brev Secure Link. Brev must require the instructor's own login. A
shareable URL alone is not authentication; do not enable public access or grant
the whole event organization access when only the instructor is intended.

Connection: authenticated HTTPS Secure Link → private port 8093 adapter → existing
`127.0.0.1:8091` read-only projector. Student submissions on 8090 and private
administration on 8092 are unchanged. Do not add raw TCP/public port mappings.

The Secure Link hostname and private overlay addresses below are illustrative.
Replace them with the exact current hostname, VM address and authorized peers
verified for your deployment. They do not identify an existing link or establish
that an installation has passed the verification steps below.

## Operator installation

Review `ops/projector_proxy.py` and `ops/ai4sci-projector-link.service` first.
Install the script as root-owned, non-writable-by-service
`/usr/local/lib/ai4sci-projector-link/projector_proxy.py`, and the unit under
`/etc/systemd/system/`. Create root-owned `/etc/ai4sci-projector-link.env` with
one non-secret value matching the registered Secure Link exactly:

```ini
PROJECTOR_HOSTNAME=scoreboard-example.gobrev.dev
```

The default bind and permitted peer are both `127.0.0.1`. If Brev's NetBird
gateway reaches the VM through its private overlay interface instead of
loopback, set the exact VM IPv4 and explicitly authorized gateway/health-check
peer IPv4 addresses in the same environment file. Example values:

```ini
PROJECTOR_BIND_ADDRESS=100.64.0.10
PROJECTOR_ALLOWED_PEERS=100.64.0.21,100.64.0.22,100.64.0.23,100.64.0.10
```

Also add only your verified exact addresses to the unit's `IPAddressAllow` via a
root-owned drop-in; retain `IPAddressDeny=any` and `IPAddressAllow=localhost`.
Do not permit the whole overlay subnet or add a public TCP mapping. The adapter
checks the real socket peer before allocating a worker or reading HTTP.
Forwarded headers cannot bypass this check. Changing gateway IPs requires an
explicit allowlist update; failure is closed. The allowed-address parser accepts
only `127.0.0.1` or individual IPv4 addresses within `100.64.0.0/10`, never
`0.0.0.0`, public IPs, CIDRs or IPv6. It never binds the whole range.

The program needs Python 3.10+ standard library only. The unit uses an isolated
dynamic user, hides judge data and credentials, and defaults to loopback-only IP
traffic. Inspect the installed service and any exact-peer drop-in on the host.
No scoring modules, database, workers or scoring fingerprint need to change.

The adapter accepts GET/HEAD only, for the display, its two assets and board
API. HEAD reads the same bounded GET response and omits the body because the
original projector has no HEAD handler. Cookies, bearer credentials and
forwarding headers never reach the judge. Foreign/duplicate Host or Origin
headers, request bodies, unknown paths and redirects are rejected. Responses
are capped at 2 MiB. At most 16 workers and 16 deadline watchdogs run, with
5-second socket timeouts and a 15-second total request deadline.

## Verify before sharing the link

- Read back the Secure Link's exact destination port, non-public setting and
  allowed identity. Test the actual browser login; an API policy response alone
  is not proof that the browser flow succeeded.
- Signed-out requests must not receive either the page or `/api/board`. An
  unauthorized account must also be denied.
- Authenticated `/display`, assets and `/api/board?challenge=1` must work.
  Check all Challenges and Overall, NVIDIA Sans and automatic updates.
- `/api/me`, `/api/submissions`, `/api/reset`, `/admin` and all non-GET/HEAD
  methods must fail. The direct VM address must not bypass Brev authentication.
- Confirm 8090, 8091 and 8092 remain loopback-only. Port 8093 must bind only
  loopback or the exact approved private overlay IP, with unlisted peers denied.
  Confirm no public TCP mapping, scoring fingerprint change or event data change.

Use the hostname's root routes, not a `/scoreboard` prefix: the existing page
uses absolute asset/API paths. Open it as a normal tab; its CSP intentionally
blocks embedding in an iframe. Local requests to `localhost:8093` without the
registered Host are intentionally rejected. Preserve the exact hostname check;
when the gateway cannot reach loopback, use only the private-interface and
exact-peer configuration above, not a wildcard/public listener.

Brev login sessions can expire. Reopening the HTTPS link may require signing
in again, but no instructor-side SSH tunnel should be necessary.
