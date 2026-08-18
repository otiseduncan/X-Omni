import assert from "node:assert/strict";
import test from "node:test";

import { settledWorkerHealth } from "../src/lib/workerState.js";

test("only complete ready health proof settles the visible worker state", () => {
  const ready = {
    ok: true,
    core: "running",
    worker: "omni",
    swapping: false,
    model: { ready: true },
  };

  assert.deepEqual(settledWorkerHealth(ready), { worker: "omni" });
  assert.equal(settledWorkerHealth({ ...ready, ok: false }), null);
  assert.equal(settledWorkerHealth({ ...ready, swapping: true }), null);
  assert.equal(settledWorkerHealth({ ...ready, model: { ready: false } }), null);
  assert.equal(settledWorkerHealth({ ...ready, worker: "" }), null);
  assert.equal(settledWorkerHealth(null), null);
});
