import { apiRequest } from './http';

const BASE = '/api/v1/admin/agent-architecture';

export interface ArchitecturePipelineNode {
  id: string;
  label: string;
  kind: string;
  description: string;
}

export interface ArchitectureAgent {
  id: string;
  version: string;
  title: string;
  summary: string;
  duties: string[];
  boundaries: string[];
  available_capabilities: string[];
}

export interface ArchitectureSkill {
  id: string;
  version: string;
  title: string;
  summary: string;
  available_to: string[];
  use_for: string[];
  compatible_output_modes: string[];
  notFor: string[];
  recommendedTools: string[];
  dependencies: string[];
  evidenceRequirements: Record<string, unknown>;
}

export interface ToolInputField {
  name: string;
  required: boolean;
  description: string | null;
  schema: unknown;
}

export interface ArchitectureTool {
  name: string;
  description: string;
  contractVersion: string;
  readOnly: boolean;
  sideEffect: string;
  approvalRequirement: string;
  idempotencyBehavior: string;
  externalSurfaces: string[];
  resultKind: string;
  inputFields: ToolInputField[];
  outputSchema: unknown;
  errorCodes: string[];
  semanticValidator: string | null;
}

export interface ArchitectureTable {
  name: string;
  rowCount: number;
  columns: Array<{
    name: string;
    type: string;
    nullable: boolean;
    primaryKey: boolean;
  }>;
  foreignKeys: Array<{
    columns: string[];
    targetTable: string;
    targetColumns: string[];
  }>;
}

export interface AgentArchitectureSnapshot {
  generatedAt: string;
  definition: {
    toolContractBoundary: string;
    authorityRule: string;
  };
  pipeline: ArchitecturePipelineNode[];
  agents: ArchitectureAgent[];
  skills: ArchitectureSkill[];
  tools: ArchitectureTool[];
  connections: Array<{
    id: string;
    label: string;
    protocol: string;
    kind: string;
    enabled: boolean;
    description: string;
  }>;
  mcpServers: Array<Record<string, unknown>>;
  mcpBoundaryNote: string;
  executionMigration: {
    mcpReadAuthority: string;
    mcpReadShadowEnabled: boolean;
    mcpReadPromotionEligible: boolean;
    writeAuthority: string;
    retirementAllowed: boolean;
    [key: string]: string | boolean;
  };
  mcpReadPromotion: {
    requested: boolean;
    eligible: boolean;
    reasonCode: string;
    requiredBuildId: string;
    requiredTools: string[];
    minMatchesPerTool: number;
    maxEvidenceAgeDays: number;
    evidence: Array<{
      toolName: string;
      sampleCount: number;
      matchCount: number;
      mismatchCount: number;
      candidateFailureCount: number;
      candidateErrorCount: number;
      invalidEventCount: number;
      blockingCount: number;
      latestAt: string | null;
    }>;
    blockers: string[];
    changesAuthority: false;
  };
  provider: {
    activeProvider: string;
    model: string | null;
    reasoningEffort: string | null;
    transport: string;
  };
  rollout: {
    nativeReviewRequired: boolean;
    shadowSampling: boolean;
    shadowModelTools: boolean;
    families: Array<{
      family: string;
      flag: string;
      enabled: boolean;
      operationKinds: string[];
      evidenceCount: number;
    }>;
  };
  apiRoutes: Array<{
    path: string;
    methods: string[];
    name: string;
    tags: string[];
  }>;
  database: {
    engine: string;
    tables: ArchitectureTable[];
  };
}

export interface AgentSessionSummary {
  id: string;
  title: string;
  provider: string;
  model: string | null;
  updatedAt: string;
  turnCount: number;
  eventCount: number;
  latestPrompt: string | null;
}

export interface AgentSessionTrace {
  session: {
    id: string;
    title: string;
    provider: string;
    model: string | null;
    createdAt: string;
    updatedAt: string;
  };
  turns: Array<{
    id: string;
    prompt: string;
    createdAt: string;
    error: string | null;
    response: {
      mode: string | null;
      summary: string | null;
      proposalKind: string | null;
      hasClarification: boolean;
      handoffOutcome: unknown;
    };
  }>;
  runs: Array<{
    id: string;
    turnId: string;
    status: string;
    controlPlane: string | null;
    goalId: string | null;
    goalRevision: number | null;
    correlationId: string;
    prompt: string;
    error: string | null;
    request?: Record<string, unknown>;
    createdAt: string;
    startedAt: string | null;
    completedAt: string | null;
  }>;
  events: Array<{
    id: string;
    sequence: number;
    eventType: string;
    source: string;
    turnId: string | null;
    runId: string | null;
    goalId: string | null;
    goalRevision: number | null;
    correlationId: string;
    createdAt: string;
    details: Record<string, unknown>;
  }>;
}

export async function fetchAgentArchitecture(): Promise<AgentArchitectureSnapshot> {
  return apiRequest<AgentArchitectureSnapshot>(BASE);
}

export async function fetchAgentSessions(): Promise<AgentSessionSummary[]> {
  const payload = await apiRequest<{ sessions: AgentSessionSummary[] }>(
    `${BASE}/sessions?limit=30`,
  );
  return payload.sessions;
}

export async function fetchAgentSessionTrace(sessionId: string): Promise<AgentSessionTrace> {
  return apiRequest<AgentSessionTrace>(
    `${BASE}/sessions/${encodeURIComponent(sessionId)}`,
  );
}
