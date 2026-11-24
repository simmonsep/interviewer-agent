import assert from "node:assert/strict";
import { Agent } from "@earendil-works/pi-agent-core";
import { createModels } from "@earendil-works/pi-ai";
import { Type } from "typebox";

assert.equal(typeof createModels, "function");
assert.equal(Type.Object({ id: Type.String() }).type, "object");
let streamInvocations = 0;
const agent = new Agent({
  initialState: { systemPrompt: "PM0-02 import check; never prompt.", tools: [] },
  // A sentinel for accidental calls, not a provider StreamFn implementation.
  streamFn() {
    streamInvocations++;
    throw new Error("PM0-02 must not invoke a model stream");
  },
  toolExecution: "sequential",
});
const unsubscribe = agent.subscribe(() => assert.fail("No run should emit events"));
assert.equal(agent.state.isStreaming, false);
assert.equal(agent.state.tools.length, 0);
agent.abort(); // idle only: active-run abort belongs to PM0-03/04.
await agent.waitForIdle();
unsubscribe();
assert.equal(streamInvocations, 0);
console.log(JSON.stringify({ status: "passed", scope: "imports, idle construction and method surface only", streamInvocations, modelCalls: 0 }));
