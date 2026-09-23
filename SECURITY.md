# Security status

This is a local-development pilot, not a production internet service. Only
instructor-owned fixtures should run on a company workstation. The submitted
code contract is restricted and interpreted; arbitrary uploaded Python is never
imported or executed, but this does not establish a production security boundary.

Keep both HTTP listeners on loopback. The `--display-only` listener serves public
aliases and scores and rejects account/submission routes. Do not expose student
ingress until TLS, explicit origins, identity lifecycle, quotas, isolation,
supervision and retention have been implemented and reviewed.

Never commit credentials, rosters, score databases, student submissions or logs.
The application rejects state inside either repository, and `.gitignore` offers
additional accident protection. It is not a substitute for reviewing every push.

Do not put secrets or live student information in public GitHub issues. Stop the
service and contact the event operator privately if a credential is disclosed.
