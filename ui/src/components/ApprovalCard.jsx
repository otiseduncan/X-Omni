import { ChevronDown, Loader2, ShieldAlert, ShieldCheck, ShieldX } from "lucide-react";

/**
 * Destructive actions stop here. The model can request them; only Otis
 * can let them run. Args are shown in full so approval is informed
 * rather than a reflex.
 */
const COPY = {
  pending: {
    title: "Approval required",
    message: null,
    Icon: ShieldAlert,
  },
  deciding: {
    title: "Submitting decision",
    message: "Waiting for Core to claim this decision.",
    Icon: Loader2,
  },
  approved: {
    title: "Approved",
    message: "Approved. Waiting for an execution claim; no success receipt yet.",
    Icon: ShieldCheck,
  },
  executing: {
    title: "Executing",
    message: "Executing the approved action. Waiting for its terminal receipt.",
    Icon: Loader2,
  },
  succeeded: {
    title: "Receipt verified",
    message: "Core completed the approved tool call. Its terminal receipt confirms execution.",
    Icon: ShieldCheck,
  },
  failed: {
    title: "Failed",
    message: "Execution failed. No successful execution is being claimed.",
    Icon: ShieldX,
  },
  denied: {
    title: "Denied",
    message: "Denied. Nothing was run.",
    Icon: ShieldX,
  },
  expired: {
    title: "Expired",
    message: "This approval expired. Nothing was run.",
    Icon: ShieldX,
  },
  indeterminate: {
    title: "Outcome indeterminate",
    message: "Outcome unknown; the action may have executed and was not run again.",
    Icon: ShieldAlert,
  },
};

function toolLabel(value) {
  return String(value || "approved action").replace(/_/g, " ");
}

function completedLabel(value) {
  if (!value) return "completion time unavailable";
  try {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  } catch {
    return String(value);
  }
}

function ReceiptFacts({ receipt }) {
  if (!receipt) return null;
  const receiptId = receipt.receipt_id || receipt.id;
  return (
    <dl className="receipt-facts">
      {receiptId && (
        <div>
          <dt>Receipt</dt>
          <dd><code>{receiptId}</code></dd>
        </div>
      )}
      <div>
        <dt>Tool</dt>
        <dd>{toolLabel(receipt.tool_name)}</dd>
      </div>
      <div>
        <dt>Executed</dt>
        <dd>{receipt.executed === true ? "yes" : "no or unverified"}</dd>
      </div>
      <div>
        <dt>Completed</dt>
        <dd>{completedLabel(receipt.completed_at)}</dd>
      </div>
      {receipt.result_hash && (
        <div>
          <dt>Result hash</dt>
          <dd><code>{receipt.result_hash}</code></dd>
        </div>
      )}
    </dl>
  );
}

export default function ApprovalCard({ approval, status = "pending", receipt, onDecide, disabled }) {
  const state = COPY[status] || COPY.pending;
  const Icon = state.Icon;
  const outcomeMessage = status === "indeterminate"
    ? approval.outcome_message || receipt?.outcome_message || receipt?.error || state.message
    : state.message;

  if (status === "succeeded" && receipt) {
    return (
      <details className="card approval approval-succeeded inline-disclosure receipt-disclosure">
        <summary
          className="disclosure-summary"
          aria-label={`Completed action details for ${toolLabel(approval.tool)}.`}
        >
          <ShieldCheck size={14} aria-hidden="true" />
          <span className="disclosure-copy">
            <strong>Action completed</strong>
            <small>{toolLabel(approval.tool)}</small>
          </span>
          <ChevronDown className="disclosure-chevron" size={15} aria-hidden="true" />
        </summary>
        <div className="disclosure-body">
          <p className="approval-summary">{approval.summary}</p>
          {approval.args && Object.keys(approval.args).length > 0 && (
            <pre className="pre">{JSON.stringify(approval.args, null, 2)}</pre>
          )}
          <div className="approval-outcome">
            <p className="approval-decided">{state.message}</p>
            <ReceiptFacts receipt={receipt} />
          </div>
        </div>
      </details>
    );
  }

  return (
    <div className={`card approval approval-${status}`}>
      <div className="card-head">
        <Icon
          size={14}
          aria-hidden="true"
          className={["deciding", "executing"].includes(status) ? "spin" : ""}
        />
        <span>{state.title}</span>
      </div>
      <p className="approval-summary">{approval.summary}</p>

      {approval.args && Object.keys(approval.args).length > 0 && (
        <pre className="pre">{JSON.stringify(approval.args, null, 2)}</pre>
      )}

      {status === "pending" ? (
        <div className="approval-actions" style={{ marginTop: 11 }}>
          <button
            className="btn approve"
            onClick={() => onDecide(approval.id, true)}
            disabled={disabled}
          >
            Approve
          </button>
          <button
            className="btn deny"
            onClick={() => onDecide(approval.id, false)}
            disabled={disabled}
          >
            Deny
          </button>
        </div>
      ) : (
        <div className="approval-outcome">
          <p
            className="approval-decided"
            role={["failed", "indeterminate"].includes(status) ? "alert" : "status"}
          >
            {outcomeMessage}
          </p>
          <ReceiptFacts receipt={receipt} />
          {status === "failed" && receipt?.error && (
            <p className="approval-error">{receipt.error}</p>
          )}
          {status === "indeterminate" && receipt?.error && receipt.error !== outcomeMessage && (
            <p className="approval-error">{receipt.error}</p>
          )}
        </div>
      )}
    </div>
  );
}
