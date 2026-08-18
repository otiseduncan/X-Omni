/**
 * Accept a settled worker only from the complete live health contract. A
 * partial response, a still-swapping router, or an unready model cannot clear
 * the visible external-workload state.
 */
export function settledWorkerHealth(payload) {
  if (
    !payload ||
    typeof payload !== "object" ||
    payload.ok !== true ||
    payload.swapping !== false ||
    payload.model?.ready !== true ||
    typeof payload.worker !== "string" ||
    !payload.worker.trim()
  ) {
    return null;
  }
  return { worker: payload.worker.trim() };
}
