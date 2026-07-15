# Guided Windows Setup Design

## Goal

Provide a single, beginner-friendly setup entry point for the complete local
Captain Cook system on Windows 11. A new user starts with:

```powershell
.\setup.ps1
```

The assistant detects missing prerequisites and configuration, explains each
gap in plain language, and guides the user through resolving it. It installs
and verifies Captain Cook, Hermes Agent, Minibook, and the local supporting
services without assuming knowledge of Python, Node.js, Docker, or API keys.

## Supported Scope

The first release supports native Windows 11 and PowerShell 7. It configures:

- the Captain Cook Python runtime and offline audited demo;
- Hermes Agent from the checked-out source;
- Minibook backend, frontend, skill, and Hermes identity;
- Docker-backed Mailpit and MariaDB services;
- either a detected existing n8n instance or a Captain-owned local n8n service;
- required local configuration and optional provider credentials.

The setup does not publish services to the public internet, configure cloud
accounts on the user's behalf, or silently alter an unrelated n8n installation.

## User Experience

The setup is an interactive terminal wizard with safe defaults. Every stage
states what it is checking, why the component is needed, and what action will
occur. A successful check is skipped on later runs.

For a missing or invalid requirement, the wizard offers only actions that make
sense for that requirement: retry, automatic repair, manual instructions, or
save and continue later. Automatic installation requires explicit confirmation.
Administrator elevation is avoided where a per-user install is available and
requested only for a system component that genuinely requires it, such as
Docker Desktop.

Secrets are collected through masked prompts. Values are validated before they
are saved. Required secrets block only the feature that needs them; optional
integrations may be skipped and appear as incomplete in the final summary.

If Windows or Docker must restart, the wizard saves a non-secret checkpoint.
Running `setup.ps1` again resumes at the first incomplete stage rather than
starting over.

## Architecture

`setup.ps1` is a thin orchestrator over focused PowerShell modules under
`scripts/setup/`. Each module exposes a check operation and, where applicable,
an install or repair operation. The boundaries are:

1. **Preflight** checks Windows, PowerShell, network availability, free disk
   space, Git, Python 3.11, Node.js/npm, Docker Desktop, and required ports.
2. **Configuration** merges non-secret defaults with existing local values,
   prompts only for missing or invalid values, and writes approved settings.
3. **Captain** creates the root virtual environment, installs pinned
   requirements, and runs the offline demo.
4. **Hermes** uses the supported local source installation and setup entry
   points, then verifies the CLI.
5. **Minibook** creates its isolated Python environment, installs frontend
   packages, starts both services, installs the skill into the Hermes profile,
   registers an identity, and verifies authentication.
6. **Services** creates or validates the Captain Compose configuration and
   starts Mailpit, MariaDB, and the selected n8n mode without deleting volumes.
7. **Verification** checks every installed component through its public user
   interface rather than trusting process existence alone.

Modules return structured results containing status, a safe display message,
and a machine-readable remediation code. The orchestrator owns all interaction
and presentation, keeping component logic independently testable.

## n8n Ownership Choice

The current repository documentation assumes a VibeMind n8n instance at a
machine-specific path. That assumption is not suitable for new users.

The wizard probes a configured n8n URL first. If it responds, the user may
explicitly adopt it as an external dependency; Captain stores only its URL and
does not control its container, data, credentials, or lifecycle. If none is
available, the wizard offers a Captain-owned n8n service with its own named
volume. The two modes are mutually exclusive and recorded in local
configuration.

No setup, repair, start, or stop command removes Docker volumes. Destructive
reset is outside the easy-setup workflow and requires a separate, explicit
operator action.

## Configuration and Secret Storage

Repository-local non-secret configuration and service credentials needed by
Compose live in the gitignored root `.env`. `.env.example` contains names,
descriptions, and non-secret defaults only. Hermes-specific credentials live in
the supported user profile below `%LOCALAPPDATA%` or `%USERPROFILE%` and are
never copied into tracked files.

The wizard:

- never echoes secret values;
- never includes them in command arguments where a safer input channel exists;
- redacts known secrets from logs and error messages;
- checks that generated secret-bearing paths are ignored by Git;
- restricts file access to the current user where Windows supports it;
- refuses to proceed if a secret would be written to a tracked file.

Generated database passwords use cryptographically secure randomness unless
the user explicitly supplies one. Existing valid values are preserved.

## Lifecycle Commands

Setup creates or validates four stable root commands:

- `start.ps1` starts only Captain-owned services and local application
  processes, then reports their URLs.
- `stop.ps1` stops Captain-owned processes and containers without deleting
  data. An adopted external n8n instance is never stopped.
- `status.ps1` shows concise health; `status.ps1 -Detailed` includes sanitized
  diagnostics and suggested remediation.
- `repair.ps1` reruns checks and offers repairs for failed or incomplete
  components without redoing healthy stages.

All commands resolve paths relative to their own script location, so they work
regardless of the caller's current directory.

## Failure Handling

Failures are divided into actionable categories: prerequisite missing,
permission required, restart required, port conflict, invalid configuration,
credential rejected, service unhealthy, or verification failed. The user sees
the failed component, the reason in plain language, and the next safe action.

A sanitized log is written beneath a gitignored runtime directory. Checkpoints
contain stage names and statuses but no credential values. An interrupted setup
is safe to rerun. Component installation uses temporary paths and promotes
completed state only after verification wherever practical.

Port conflicts identify the port and owning process when Windows exposes it.
The setup does not terminate unrelated processes automatically.

## Verification and Acceptance

Automated tests exercise module result contracts and simulate:

- a clean machine with missing prerequisites;
- a fully configured machine and idempotent second run;
- a user declining an optional or elevated install;
- restart and resume behavior;
- invalid or rejected credentials;
- occupied ports and unavailable services;
- existing external n8n versus Captain-owned n8n;
- interrupted installs and targeted repair;
- log redaction and protection of secret-bearing files;
- preservation of all Docker volumes and unrelated processes.

An end-to-end acceptance run on Windows 11 proves the broader goal. Starting
from a fresh user profile, the documented single command must lead the user to
a verified system in which:

1. the Captain Cook offline demo succeeds and writes its evidence artifact;
2. Hermes CLI runs and discovers the Minibook skill;
3. Minibook web and API endpoints respond and the Hermes identity authenticates;
4. Mailpit web/API and SMTP respond;
5. MariaDB accepts an authenticated query;
6. the selected n8n endpoint responds from both host and required container
   contexts;
7. `start.ps1`, `stop.ps1`, `status.ps1`, and `repair.ps1` behave as documented;
8. a second `setup.ps1` run performs no destructive or unnecessary work; and
9. Git inspection finds no generated credential or secret.

If a required item cannot be completed, the setup remains explicitly
incomplete and gives the user a resumable remediation path. It never reports
success based only on installation commands returning zero.

## Documentation

The root README leads with the one-command Windows setup and provides a compact
manual fallback. A troubleshooting section maps each remediation code to steps
a non-technical user can follow. Existing advanced and developer setup notes
remain available but no longer form the primary onboarding path.

## Out of Scope

- macOS, Linux, and WSL setup automation in the first release;
- public hosting, TLS, firewall forwarding, or router configuration;
- automatic creation or purchase of third-party API accounts;
- destructive reset or migration of unrelated Docker data;
- a graphical installer before the terminal workflow is proven stable.
