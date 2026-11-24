// Compile-only upgrade canary. No provider, credential store, or prompt is run.
import { Agent, type AgentEvent, type AgentOptions, type AgentTool, type StreamFn } from "@earendil-works/pi-agent-core";
import { createModels, type Api, type Model } from "@earendil-works/pi-ai";
import { Type } from "typebox";

const params = Type.Object({ id: Type.String() }, { additionalProperties: false });

export function checkSdkSurface(streamFn: StreamFn, model: Model<Api>) {
  const probe: AgentTool<typeof params, { id: string }> = {
    name: "compile_only_probe",
    label: "Compile-only probe",
    description: "Not registered in a product runtime.",
    parameters: params,
    async execute(_callId, args, signal) {
      signal?.throwIfAborted();
      return { content: [{ type: "text", text: args.id }], details: { id: args.id } };
    },
  };
  const options: AgentOptions = {
    initialState: { systemPrompt: "Compile only", model, tools: [probe] },
    streamFn,
    toolExecution: "sequential",
    async beforeToolCall({ args }, signal) {
      signal?.throwIfAborted();
      return args === undefined ? { block: true, reason: "No arguments" } : undefined;
    },
    finishTurn: () => ({ action: "end" }),
    prepareRequest: () => undefined,
    transformContext: async (messages) => messages,
  };
  const agent = new Agent(options);
  const unsubscribe = agent.subscribe(async (event: AgentEvent, signal) => {
    if (event.type === "tool_execution_end") {
      const error: boolean = event.isError;
      void error;
    }
    void signal;
  });
  // Verify signatures without executing a prompt/continuation.
  const prompt: (input: string) => Promise<void> = agent.prompt.bind(agent);
  const resume: () => Promise<void> = agent.continue.bind(agent);
  const idle: () => Promise<void> = agent.waitForIdle.bind(agent);
  const cancel: () => void = agent.abort.bind(agent);
  const models = createModels();
  const providerStream: StreamFn = models.streamSimple.bind(models);
  return { agent, unsubscribe, prompt, resume, idle, cancel, providerStream };
}
