# Approved integration overlay

This work package has an explicit integration lease. Follow this order:

1. Discover MCP tools exposed by the leased server and record the relevant tool inventory.
2. Prefer native n8n nodes over generic HTTP or code nodes whenever a supported native node exists.
3. Validate the workflow structure and credentials-by-reference configuration without exposing secret values.
4. Test the isolated workflow with the approved disposable correlation ID.
5. Evidence must include the real workflow ID, execution or call ID, outcome, and content digests.

Do not start, stop, recreate, or adopt Docker containers. Do not create, delete, or migrate Docker volumes.
Treat runtime unavailability as infrastructure
failure and do not fabricate identifiers or successful calls.
