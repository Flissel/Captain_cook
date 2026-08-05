import type { IntegrationSetupStatus } from "../types";

const statusLabels: Readonly<Record<IntegrationSetupStatus, string>> = {
  missing: "Connection missing",
  selection_required: "Selection required",
  verification_required: "Verification required",
  verification_failed: "Verification failed",
  ready: "Ready",
  revoked: "Revoked",
  expired: "Verification expired",
};

export function SetupStatus({ status }: { status: IntegrationSetupStatus }) {
  return (
    <span
      data-testid="setup-status"
      data-status={status}
      className={`status status--${status}`}
    >
      {statusLabels[status]}
    </span>
  );
}
