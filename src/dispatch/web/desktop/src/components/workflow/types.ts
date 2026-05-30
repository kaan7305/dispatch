import type { NodeState } from "@/lib/workflowApi";

// Per-node `data` payload supplied to @xyflow/react custom nodes. We spread
// the WorkflowNode.params into the top level of the object so node bodies
// can read them as direct keys; runtime status is attached separately under
// `state` (populated from WorkflowRun.node_states[id] during a run).
export interface NodeData {
  state?: NodeState;
  [key: string]: unknown;
}

export interface InputSchemaEntry {
  key: string;
  label?: string;
}
