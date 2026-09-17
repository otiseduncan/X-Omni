import { ChevronDown, Layers } from "lucide-react";

import Artifact from "./cards/Cards.jsx";
import { evidenceSummary } from "../lib/responsePresentation.js";

/**
 * The collapsed "Sources & work" control attached to one assistant reply.
 * Closed by default; opening it renders every evidence card unchanged.
 */
export default function EvidenceDisclosure({ evidence, onCameraCapture }) {
  const { count, attention, label } = evidenceSummary(evidence);
  if (count === 0) return null;
  return (
    <details className={`evidence-disclosure${attention ? " needs-attention" : ""}`}>
      <summary
        className="evidence-summary"
        aria-label={`Sources and work, ${label}${attention ? ", something needs a look" : ""}`}
      >
        <Layers size={12} aria-hidden="true" />
        <span>Sources &amp; work</span>
        <small>{label}</small>
        {attention && <small className="evidence-attention">needs a look</small>}
        <ChevronDown className="disclosure-chevron" size={13} aria-hidden="true" />
      </summary>
      <div className="evidence-body">
        {evidence.map((item) => (
          <Artifact
            artifact={item.artifact}
            key={item.key}
            onCameraCapture={onCameraCapture}
          />
        ))}
      </div>
    </details>
  );
}
