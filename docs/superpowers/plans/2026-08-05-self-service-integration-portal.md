# Self-Service Integration Portal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Mini-PC-hosted, tenant-isolated portal for safe n8n credential setup, selection, verification, rotation and revoke.

**Architecture:** Supabase authenticates users. A Captain Gateway portal API verifies the Supabase JWT, persists only hashed single-purpose setup tickets in MariaDB, and delegates all lifecycle changes to the existing integration setup store. React renders only secret-free status; n8n owns encrypted credentials and Gitea supplies digest-pinned templates.

**Tech Stack:** Python 3.11, FastAPI, Pydantic 2, PyJWT, MariaDB, pytest, React, TypeScript strict, Vite, Tailwind, Supabase Auth, n8n MCP, Gitea REST.

## Global Constraints

- No credential value, OAuth code, refresh token, provider response, Gateway role token or user JWT enters portal persistence, logs, artifacts, workflow JSON, prompts, Minibook or Gitea.
- Credentials are created and rotated in n8n UI only; Captain reads metadata through `list_credentials` and binds exact ID/type/project.
- Tickets are subject-, organization-, job- and credential-alias-bound, valid at most ten minutes, stored as SHA-256 only and consumed transactionally once.
- Gateway remains the sole MariaDB writer; Supabase is identity and tenant isolation only.
- The Mini-PC backend reaches Gateway only over a private mTLS service link; browsers receive no Gateway URL, role token or mTLS material, and Gateway has no public fallback route.
- Required non-ready integrations fail closed; only digest/project/correlation/deployment/execution-fenced receipts reach `ready`.
- Do not migrate, adopt, stop or delete VibeMind n8n resources or volumes.

## File Structure

- Create `gateway/portal_contracts.py`: frozen principal, ticket, action and status contracts.
- Create `gateway/portal_auth.py`: Supabase JWKS verifier and organization claim extractor.
- Create `gateway/portal_store.py`: MariaDB persistence for hashed ticket records.
- Modify `gateway/settings.py`, `gateway/store.py`, `gateway/app.py`: portal configuration, persistence delegation and authenticated routes.
- Create `tests/gateway/test_portal_contracts.py`, `tests/gateway/test_portal_auth.py`, `tests/gateway/test_portal_ticket_store.py`, `tests/gateway/test_portal_api.py`.
- Create `agenten/agent_factory/gitea_template_contracts.py`, `agenten/agent_factory/gitea_templates.py`, and `tests/agent_factory/test_gitea_templates.py`.
- Create `portal/` Vite React TypeScript strict/Tailwind client and component tests.
- Create `scripts/portal-preflight.ps1`, `scripts/deploy-portal-mini-pc.ps1`, `tests/scripts/test_portal_scripts.py`, and `docs/OPERATIONS.md`.

### Task 0: Establish the private mTLS service link

**Files:** Create `deploy/portal-link/captain-proxy.conf`, `deploy/portal-link/mini-pc-proxy.conf`, `deploy/portal-link/compose.portal-link.yml`, and `tests/scripts/test_portal_link_config.py`.

**Interfaces:** The Mini-PC backend sends requests to `https://captain-portal-link.internal`; the Captain proxy requires a trusted Mini-PC client certificate and forwards only `/v1/portal/` to `http://127.0.0.1:8090`.

- [ ] **Step 1: Write the failing proxy-boundary test**

    def test_link_requires_client_certificate_and_forwards_only_portal_routes() -> None:
        config = Path("deploy/portal-link/captain-proxy.conf").read_text(encoding="utf-8")
        assert "ssl_verify_client on" in config
        assert 'location /v1/portal/' in config
        assert 'proxy_pass http://127.0.0.1:8090' in config
        assert 'location / {' in config and 'return 404' in config

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m pytest -q tests/scripts/test_portal_link_config.py`

Expected: FAIL because the service-link configuration is absent.

- [ ] **Step 3: Implement mTLS proxy configuration**

Mount the Captain server certificate/key and Mini-PC client CA only from local gitignored secret paths. Set `ssl_verify_client on`, allow only the exact portal prefix, strip inbound `Authorization` headers, add a fixed internal forwarding identity header at the Captain proxy, and return 404 for every other route. The Mini-PC proxy must verify the Captain certificate hostname and never disable TLS verification.

- [ ] **Step 4: Run configuration tests and compose validation**

Run: `py -3.11 -m pytest -q tests/scripts/test_portal_link_config.py; docker compose -f deploy/portal-link/compose.portal-link.yml config`

Expected: PASS without starting a container.

- [ ] **Step 5: Commit**

Run: `git add deploy/portal-link tests/scripts/test_portal_link_config.py; git commit -m "feat: add private portal gateway link"`

### Task 1: Define tenant and ticket contracts

**Files:** Create `gateway/portal_contracts.py`; create `tests/gateway/test_portal_contracts.py`.

**Interfaces:** Produce `PortalPrincipalV1`, `PortalSetupTicketRequestV1`, `PortalSetupTicketV1`, and `PortalSetupActionRequestV1`. `PortalSetupTicketRequestV1` has `job_id: UUID`, `organization_id: str`, `subject_id: str`, `credential_alias: str`, `issued_at: datetime`, and `expires_at: datetime`.

- [ ] **Step 1: Write the failing test**

    def test_ticket_rejects_expiry_longer_than_ten_minutes() -> None:
        with pytest.raises(ValidationError, match="at most ten minutes"):
            PortalSetupTicketRequestV1(job_id=UUID("10000000-0000-0000-0000-000000000001"), organization_id="org-a", subject_id="user-a", credential_alias="CRM_PRIMARY", issued_at=NOW, expires_at=NOW + timedelta(minutes=11))

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m pytest -q tests/gateway/test_portal_contracts.py`

Expected: FAIL because `gateway.portal_contracts` is absent.

- [ ] **Step 3: Implement the minimal frozen contracts**

    class PortalSetupTicketRequestV1(_FrozenContract):
        job_id: UUID
        organization_id: str = Field(pattern=IDENTIFIER_PATTERN)
        subject_id: str = Field(pattern=IDENTIFIER_PATTERN)
        credential_alias: str = Field(pattern=IDENTIFIER_PATTERN)
        issued_at: datetime
        expires_at: datetime

        @model_validator(mode="after")
        def require_short_lived_expiry(self) -> Self:
            if not self.issued_at < self.expires_at <= self.issued_at + timedelta(minutes=10):
                raise ValueError("portal ticket expiry must be at most ten minutes")
            return self

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.11 -m pytest -q tests/gateway/test_portal_contracts.py`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add gateway/portal_contracts.py tests/gateway/test_portal_contracts.py; git commit -m "feat: define portal setup ticket contracts"`

### Task 2: Verify Supabase user identity

**Files:** Modify `requirements.txt` and `gateway/settings.py`; create `gateway/portal_auth.py`; create `tests/gateway/test_portal_auth.py`.

**Interfaces:** Produce `require_portal_principal(request: Request) -> PortalPrincipalV1`. It verifies RS256 issuer, audience and JWKS `kid`, then reads `sub` and the configured `organization_id` claim.

- [ ] **Step 1: Write the failing test**

    def test_portal_auth_rejects_wrong_audience_without_echoing_token() -> None:
        with pytest.raises(HTTPException, match="invalid portal identity"):
            require_portal_principal(request_with_signed_jwt(audience="wrong"), settings)

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m pytest -q tests/gateway/test_portal_auth.py`

Expected: FAIL because `require_portal_principal` is absent.

- [ ] **Step 3: Implement strict settings and verifier**

    portal_supabase_issuer: str
    portal_supabase_audience: str
    portal_supabase_jwks_url: str
    portal_organization_claim: str = "organization_id"

Pin `PyJWT[crypto]` in `requirements.txt`. Cache public JWKS keys by `kid` with a bounded TTL. Return the fixed `invalid portal identity` response for missing, expired, malformed, wrong-issuer, wrong-audience or claimless tokens; never log a token.

- [ ] **Step 4: Run tests**

Run: `py -3.11 -m pytest -q tests/gateway/test_portal_auth.py tests/gateway/test_settings.py`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add requirements.txt gateway/settings.py gateway/portal_auth.py tests/gateway/test_portal_auth.py; git commit -m "feat: authenticate portal users with supabase jwt"`

### Task 3: Persist and consume opaque setup tickets

**Files:** Create `gateway/portal_store.py`; modify `gateway/store.py` and `gateway/app.py`; create `tests/gateway/test_portal_ticket_store.py` and `tests/gateway/test_portal_api.py`.

**Interfaces:** Add `POST /v1/portal/integration-setups/{job_id}/tickets`, `GET /v1/portal/integration-setups/{job_id}`, `POST /v1/portal/integration-setups/{job_id}/discover`, `POST /v1/portal/integration-setups/{job_id}/select`, and `POST /v1/portal/integration-setups/{job_id}/actions`.

- [ ] **Step 1: Write failing tenant and replay tests**

    def test_org_b_cannot_read_org_a_setup_or_consume_org_a_ticket(client) -> None:
        ticket = issue_ticket(client, principal=ORG_A, job_id=JOB_ID)
        assert read_surface(client, principal=ORG_B, job_id=JOB_ID).status_code == 404
        assert consume_ticket(client, principal=ORG_B, ticket=ticket).status_code == 403

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3.11 -m pytest -q tests/gateway/test_portal_ticket_store.py tests/gateway/test_portal_api.py`

Expected: FAIL because ticket storage and portal routes are absent.

- [ ] **Step 3: Implement transactional ticket storage**

    raw = secrets.token_urlsafe(32)
    digest = sha256(raw.encode("utf-8")).hexdigest()
    insert_ticket(ticket_id=uuid4(), token_sha256=digest, job_id=job_id,
                  organization_id=principal.organization_id, subject_id=principal.subject_id,
                  credential_alias=alias, expires_at=expires_at, used_at=None)

Store `ticket_id`, digest, job, organization, subject, alias, expiry, used timestamp and timestamps. Consumption compares SHA-256 with `compare_digest`, checks every binding and updates `used_at` in the same transaction. Portal routes delegate selection and rotation/revoke to existing `IntegrationSetupPlanner` and `IntegrationSetupMutationV1`; no route accepts secret material.

- [ ] **Step 4: Run isolated MariaDB tests**

Run: `$env:TEST_MARIADB_DSN = '<isolated DSN ending in /captain_test>'; py -3.11 -m pytest -q tests/gateway/test_portal_ticket_store.py tests/gateway/test_portal_api.py tests/gateway/test_integration_setup_api.py`

Expected: PASS with cross-tenant denial, expiry denial, one-time consumption and secret-free responses.

- [ ] **Step 5: Commit**

Run: `git add gateway/portal_store.py gateway/store.py gateway/app.py tests/gateway/test_portal_ticket_store.py tests/gateway/test_portal_api.py; git commit -m "feat: add tenant scoped integration portal api"`

### Task 4: Pin Gitea templates by digest

**Files:** Create `agenten/agent_factory/gitea_template_contracts.py`, `agenten/agent_factory/gitea_templates.py`, `tests/agent_factory/test_gitea_templates.py`.

**Interfaces:** `GiteaTemplateReleaseV1(repository, revision, path, contents_url, sha256)` and `GiteaTemplateClient.fetch_verified_template(release) -> ArtifactRef`.

- [ ] **Step 1: Write failing mismatch test**

    async def test_fetch_verified_template_rejects_changed_bytes() -> None:
        with pytest.raises(GiteaTemplateError, match="template digest mismatch"):
            await client.fetch_verified_template(release(expected_sha256="0" * 64))

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m pytest -q tests/agent_factory/test_gitea_templates.py`

Expected: FAIL because the Gitea client is absent.

- [ ] **Step 3: Implement read-only digest verification**

    body = response.content
    if sha256(body).hexdigest() != release.sha256:
        raise GiteaTemplateError("template digest mismatch")
    return ArtifactRef(uri=f"artifact://gitea/{release.sha256}", sha256=release.sha256)

Use an injected HTTP client. The portal receives only artifact digest and release metadata; the Gitea credential never enters its bundle.

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.11 -m pytest -q tests/agent_factory/test_gitea_templates.py`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add agenten/agent_factory/gitea_template_contracts.py agenten/agent_factory/gitea_templates.py tests/agent_factory/test_gitea_templates.py; git commit -m "feat: verify gitea integration templates by digest"`

### Task 5: Build the React self-service portal

**Files:** Create `portal/package.json`, `portal/tsconfig.json`, `portal/vite.config.ts`, `portal/tailwind.config.ts`, `portal/src/types.ts`, `portal/src/api.ts`, `portal/src/App.tsx`, `portal/src/components/IntegrationCard.tsx`, `portal/src/components/SetupStatus.tsx`, `portal/src/api.test.ts`, `portal/src/components/IntegrationCard.test.tsx`.

**Interfaces:** `getSetupSurface(jobId, accessToken): Promise<PortalSetupSurface>` consumes the Task 3 API. `IntegrationCard` receives only status, credential type, label, project metadata and `n8nCredentialsUrl`.

- [ ] **Step 1: Write failing UI redaction test**

    it("links to n8n and never renders secret fields", () => {
      render(<IntegrationCard action={missingBearerAction} onCheck={vi.fn()} />)
      expect(screen.getByRole("link", { name: "Connect in n8n" })).toHaveAttribute("href", missingBearerAction.n8nCredentialsUrl)
      expect(screen.queryByText(/token|secret|password/i)).not.toBeInTheDocument()
    })

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix portal test -- --run`

Expected: FAIL because the portal package is absent.

- [ ] **Step 3: Implement strict typed UI**

    export async function getSetupSurface(jobId: string, accessToken: string): Promise<PortalSetupSurface> {
      return request<PortalSetupSurface>(`/v1/portal/integration-setups/${jobId}`, accessToken)
    }

Use Supabase Auth only to obtain the browser session. Render `missing`, `selection_required`, `verification_required`, `verification_failed`, `ready`, `revoked`, and `expired` explicitly. `Connect in n8n` opens only the Gateway-issued n8n credential URL in a new tab. Action errors map to fixed public text, never response bodies.

- [ ] **Step 4: Run checks**

Run: `npm --prefix portal run lint; npm --prefix portal run test -- --run; npm --prefix portal run build`

Expected: PASS with TypeScript strict mode.

- [ ] **Step 5: Commit**

Run: `git add portal; git commit -m "feat: add self service integration portal"`

### Task 6: Add Mini-PC operations boundary

**Files:** Create `scripts/portal-preflight.ps1`, `scripts/deploy-portal-mini-pc.ps1`, `tests/scripts/test_portal_scripts.py`, `docs/OPERATIONS.md`; modify `.env.example` and `README.md`.

**Interfaces:** `portal-preflight.ps1` emits redacted URL/status/version JSON. `deploy-portal-mini-pc.ps1 -Apply` validates configuration then runs only `docker compose up -d --build portal`.

- [ ] **Step 1: Write failing deployment safety test**

    def test_portal_deploy_refuses_missing_env_and_never_removes_volumes() -> None:
        source = Path("scripts/deploy-portal-mini-pc.ps1").read_text(encoding="utf-8")
        assert "required portal environment is missing" in source
        assert "compose down -v" not in source.lower()

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.11 -m pytest -q tests/scripts/test_portal_scripts.py`

Expected: FAIL because the scripts are absent.

- [ ] **Step 3: Implement preflight and deployment guard**

    $required = 'CAPTAIN_PORTAL_URL','CAPTAIN_PORTAL_SUPABASE_URL','CAPTAIN_PORTAL_SUPABASE_ANON_KEY','CAPTAIN_PORTAL_GATEWAY_URL','CAPTAIN_PORTAL_GITEA_URL'
    $missing = $required | Where-Object { -not $env:$_ }
    if ($missing) { throw 'required portal environment is missing' }

Preflight performs GET requests without authorization headers and reports only host/status/version. Deployment requires `-Apply`, runs `docker compose config` first and never changes n8n or database volumes.

- [ ] **Step 4: Run checks**

Run: `py -3.11 -m pytest -q tests/scripts/test_portal_scripts.py; git diff --check`

Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add scripts/portal-preflight.ps1 scripts/deploy-portal-mini-pc.ps1 tests/scripts/test_portal_scripts.py .env.example README.md docs/OPERATIONS.md; git commit -m "docs: add mini pc portal operations"`

### Task 7: Prove live evidence

**Files:** Create `tests/live/test_portal_integration_live.py`; modify `docs/superpowers/plans/2026-08-04-integration-control-plane.md`.

**Interfaces:** Live fixtures read explicit, disposable test-provider configuration only. They return status and evidence references, not provider payloads.

- [ ] **Step 1: Write opt-in cross-tenant live denial**

    pytestmark = pytest.mark.live

    def test_portal_rejects_cross_tenant_ticket_before_provider_call(live_portal) -> None:
        assert live_portal.use_ticket(subject="org-b-user", ticket=live_portal.org_a_ticket()).status_code == 403

- [ ] **Step 2: Run without live configuration**

Run: `py -3.11 -m pytest -q -m live tests/live/test_portal_integration_live.py`

Expected: SKIP with a missing-live-prerequisites reason; never PASS.

- [ ] **Step 3: Implement isolated Bearer and OAuth cases**

Use a disposable Bearer endpoint and a real sandbox OAuth client with an exact callback allowlist. Assert only redacted receipt fields, deployment/execution IDs, digest, project, status and correlation. Restart the portal and Gateway between discovery and probe; assert the ticket is unusable after restart and the setup revision is monotonic.

- [ ] **Step 4: Run complete local gates**

Run: `py -3.11 -m pytest -q -m "not live"; py -3.11 -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py; py -3.11 -m compileall -q agenten blockchain chats config gateway; py -3.11 scripts/verify_submission.py`

Expected: PASS. Report live evidence separately as skipped, failed or verified-live.

- [ ] **Step 5: Run opted-in live gates only after Mini-PC, Supabase, Gitea and provider settings exist**

Run: `py -3.11 -m pytest -q -m live tests/live/test_portal_integration_live.py`

Expected: PASS only with real Bearer and OAuth provider proof; absent prerequisites remain BLOCKED.

- [ ] **Step 6: Commit**

Run: `git add tests/live/test_portal_integration_live.py docs/superpowers/plans/2026-08-04-integration-control-plane.md; git commit -m "test: verify portal integration evidence"`

## Plan Self-Review

- Spec coverage: Tasks 1-3 implement tenant identity, ticketing, setup actions and fail-closed persistence. Task 4 implements Gitea digest pinning. Task 5 delivers user interaction. Task 6 deploys safely on the Mini-PC. Task 7 proves Bearer/OAuth, restart, rotation, revoke and redaction.
- Placeholder scan: each task names files, interfaces, assertions, commands and expected output. Live prerequisites are explicit gates, not an unimplemented behavior.
- Type consistency: Task 2 returns `PortalPrincipalV1`, Task 3 consumes it with `PortalSetupTicketRequestV1`, Task 5 consumes the secret-free setup surface, and Task 7 verifies the same portal routes.
