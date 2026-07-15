# Local Delivery Stack Design

## Goal

Give Captain Cook a reproducible local delivery stack without creating a
second n8n installation or taking ownership of another project's n8n data.
Captain Cook will use the existing VibeMind n8n instance and will run its own
Mailpit and MariaDB services.

## Existing n8n instance

The existing instance is defined by
`C:\Users\User\Desktop\Vibemind_V1\vibemind-os\voice\docker-compose.n8n.yml`.
It publishes n8n at `http://localhost:15678`, uses the container name
`vibemind-n8n`, and persists data in `voice_vibemind-n8n-data`.

Captain Cook will treat this instance as an external dependency:

- Captain Cook will not mount, rename, migrate, or delete the VibeMind volume.
- Captain Cook will not duplicate the VibeMind n8n service in its Compose file.
- Host-side tools use `http://localhost:15678`.
- Captain Cook containers use `http://host.docker.internal:15678`.
- Starting n8n remains the responsibility of the VibeMind Compose project.

This preserves existing workflows, credentials, encryption configuration, and
ownership boundaries.

## Captain Cook services

The repository-level `docker-compose.yml` will define two services:

1. **Mailpit** provides SMTP on host port `1025` and its web/API interface on
   host port `8025`. Captain workflows running in the external n8n container
   reach SMTP through `host.docker.internal:1025`.
2. **MariaDB 11.8** provides the future Captain ledger database on host port
   `3306`. It uses a named `ledger_data` volume and a healthcheck based on
   `healthcheck.sh --connect --innodb_initialized`.

Both services use fixed image versions, restart unless stopped, and expose
healthchecks. The Compose project does not join or mutate VibeMind networks.

## Configuration and secrets

The existing gitignored root `.env` will hold local values. `.env.example`
will document placeholders and non-secret defaults for:

- `N8N_URL` and `N8N_CONTAINER_URL`
- Mailpit web and SMTP ports/URLs
- MariaDB port, database, application user, application password, and root
  password
- the shared `Europe/Berlin` timezone

No credentials are committed. The existing n8n owner setup remains unchanged
inside the VibeMind project. Captain Cook documentation will not repeat the
existing VibeMind development credentials.

## Startup and operator workflow

The README will document this sequence:

1. Start the existing VibeMind n8n Compose file.
2. Start Captain Cook's Mailpit and MariaDB services.
3. Check n8n at `http://localhost:15678`, Mailpit at
   `http://localhost:8025`, and the Captain Compose health status.

Stopping Captain Cook with `docker compose down` preserves `ledger_data`.
Operators must not use `docker compose down -v` unless they intentionally want
to erase the Captain ledger. Captain commands never remove either n8n volume.

## Failure handling

- If port `15678` is unavailable, the n8n preflight reports that the external
  dependency is missing; Captain does not start a replacement instance.
- If ports `8025`, `1025`, or `3306` are occupied, Compose fails explicitly and
  the conflicting service is reported.
- Mailpit and MariaDB healthchecks distinguish a running container from a
  usable service.
- A missing or malformed required `.env` value makes Compose interpolation
  fail before containers start.

## Verification

Implementation is accepted when all of the following pass:

- `docker compose config` validates the Captain Compose file.
- The existing VibeMind n8n instance starts with its existing volume and
  responds on port `15678`.
- Captain Mailpit and MariaDB become healthy.
- Mailpit's web API responds on port `8025`, and SMTP port `1025` accepts a TCP
  connection.
- MariaDB accepts an authenticated query using the application credentials.
- Container-to-host access to n8n through `host.docker.internal:15678` is
  verified from the Captain Compose network.
- Existing repository tests remain green.

## Memory maintenance

The durable project memory will record that Captain Cook reuses VibeMind's n8n
instance at port `15678`, while owning separate Mailpit and MariaDB services.
The expected writable Codex project-memory directory will be created because it
is currently absent. The long-form Brain mirror remains a read source and is
not directly mutated as part of this repository setup.
