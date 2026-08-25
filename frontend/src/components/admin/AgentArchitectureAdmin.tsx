import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  fetchAgentArchitecture,
  fetchAgentSessions,
  fetchAgentSessionTrace,
  type AgentArchitectureSnapshot,
  type AgentSessionSummary,
  type AgentSessionTrace,
  type ArchitectureTable,
  type ArchitectureTool,
} from '../../api/agentArchitecture';
import type { StrategistRunResponse } from '../../api/strategist';
import { StrategistAiCard } from '../planning/StrategistAiCard';
import { useCategoryStore } from '../../store/useCategoryStore';
import { useNodeStore } from '../../store/useNodeStore';
import {
  LOOP_CANVAS,
  agentLoopEdges,
  agentLoopGroups,
  agentLoopNodes,
  flowSelectionRules,
  stageForEvent,
  type AgentLoopEdge,
  type AgentLoopNode,
  type ArchitectureLayer,
} from './agentArchitectureGraph';
import './AgentArchitectureAdmin.css';

type AdminTab = 'map' | 'contracts' | 'traces' | 'data';
type TraceEvent = AgentSessionTrace['events'][number];

const TERMINAL_RUN_STATUSES = new Set(['completed', 'failed', 'cancelled']);
const tabs: Array<{ id: AdminTab; label: string; description: string }> = [
  { id: 'map', label: 'Live agent loop', description: 'Run and replay the complete flow' },
  { id: 'contracts', label: 'Tools & contracts', description: 'Inputs, outputs, validation and skills' },
  { id: 'traces', label: 'Saved sessions', description: 'Replay previous prompts from the event ledger' },
  { id: 'data', label: 'Data & endpoints', description: 'Persistence and HTTP boundaries' },
];
const layerLabels: Record<ArchitectureLayer | 'all', string> = {
  all: 'All flows',
  entry: 'User interaction',
  context: 'Context & memory',
  reasoning: 'Agents & reasoning',
  tools: 'Tools & Planner',
  safety: 'Safety & execution',
  data: 'Persistence',
};

function pretty(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function formatTime(value: string | null) {
  if (!value) return '—';
  return new Intl.DateTimeFormat(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit',
  }).format(new Date(value));
}

function eventToolName(event: TraceEvent | null): unknown {
  return event?.details.tool
    ?? (event?.details.captured as Record<string, unknown> | undefined)?.name;
}

function eventStage(event: TraceEvent) {
  return stageForEvent(event.eventType, eventToolName(event));
}

function StatusPill({ active, children }: { active: boolean; children: string }) {
  return <span className={`architecture-status${active ? ' is-active' : ''}`}><span />{children}</span>;
}

function ToolContractDetail({ tool }: { tool: ArchitectureTool }) {
  return (
    <article className="contract-detail" aria-labelledby="contract-detail-heading">
      <header className="contract-detail-header">
        <div><p className="architecture-eyebrow">Tool contract v{tool.contractVersion}</p><h3 id="contract-detail-heading">{tool.name}</h3></div>
        <StatusPill active={tool.readOnly}>{tool.readOnly ? 'Read only' : 'Drafts mutation'}</StatusPill>
      </header>
      <p className="contract-description">{tool.description}</p>
      <div className="contract-policy-grid">
        <div><span>Side effect</span><strong>{tool.sideEffect}</strong></div>
        <div><span>Approval</span><strong>{tool.approvalRequirement}</strong></div>
        <div><span>Idempotency</span><strong>{tool.idempotencyBehavior}</strong></div>
        <div><span>Returns</span><strong>{tool.resultKind}</strong></div>
      </div>
      <section className="contract-section">
        <div className="contract-section-heading"><h4>Accepted inputs</h4><span>{tool.inputFields.length} fields</span></div>
        <div className="contract-field-list">
          {tool.inputFields.length === 0 ? <p className="architecture-empty-inline">No input arguments.</p> : tool.inputFields.map((field) => (
            <details key={field.name} className="contract-field">
              <summary><code>{field.name}</code><span>{field.required ? 'required' : 'optional'}</span></summary>
              {field.description ? <p>{field.description}</p> : null}<pre>{pretty(field.schema)}</pre>
            </details>
          ))}
        </div>
      </section>
      <section className="contract-section">
        <div className="contract-section-heading"><h4>Validation and typed failures</h4><span>{tool.semanticValidator ?? 'schema validator'}</span></div>
        <div className="contract-error-list">{tool.errorCodes.map((code) => <code key={code}>{code}</code>)}</div>
      </section>
      <details className="contract-output"><summary>Returned value schema</summary><pre>{pretty(tool.outputSchema)}</pre></details>
    </article>
  );
}

function DatabaseTableDetail({ table }: { table: ArchitectureTable }) {
  return (
    <article className="data-detail">
      <header><div><p className="architecture-eyebrow">SQLite table</p><h3>{table.name}</h3></div><strong>{table.rowCount.toLocaleString()} rows</strong></header>
      <div className="data-column-list">{table.columns.map((column) => (
        <div key={column.name} className="data-column-row"><code>{column.name}</code><span>{column.type}</span><small>{column.primaryKey ? 'primary key' : column.nullable ? 'nullable' : 'required'}</small></div>
      ))}</div>
      {table.foreignKeys.length > 0 ? <section className="data-relations"><h4>Relationships</h4>{table.foreignKeys.map((key) => (
        <p key={`${key.columns.join(',')}-${key.targetTable}`}><code>{key.columns.join(', ')}</code><span>→</span><code>{key.targetTable}.{key.targetColumns.join(', ')}</code></p>
      ))}</section> : null}
    </article>
  );
}

function McpReadPromotionDetail({ snapshot }: { snapshot: AgentArchitectureSnapshot }) {
  const report = snapshot.mcpReadPromotion;
  return (
    <section className="rollout-strip" aria-labelledby="mcp-read-readiness-heading">
      <header>
        <p className="architecture-eyebrow">Read authority · evidence only</p>
        <h2 id="mcp-read-readiness-heading">MCP promotion readiness</h2>
      </header>
      <p>
        Native reads remain authoritative. This report can qualify an exact build for a later
        routing change, but it cannot route traffic or change authority itself.
      </p>
      <div>
        <article>
          <StatusPill active={report.eligible}>
            {report.eligible ? 'evidence eligible' : 'not eligible'}
          </StatusPill>
          <h3>{report.requiredBuildId}</h3>
          <p>{report.reasonCode.replaceAll('_', ' ')}</p>
          <small>
            {report.requested ? 'assessment requested' : 'assessment not requested'}
            {' · '}
            authority unchanged
          </small>
        </article>
        {report.evidence.map((item) => (
          <article key={item.toolName}>
            <StatusPill active={item.blockingCount === 0 && item.matchCount >= report.minMatchesPerTool}>
              {item.blockingCount === 0 ? 'clean evidence' : `${item.blockingCount} blockers`}
            </StatusPill>
            <h3><code>{item.toolName}</code></h3>
            <p>{item.matchCount} / {report.minMatchesPerTool} required exact-build matches</p>
            <small>{item.sampleCount} samples · latest {formatTime(item.latestAt)}</small>
          </article>
        ))}
        {report.blockers.length > 0 ? (
          <article>
            <StatusPill active={false}>promotion blocked</StatusPill>
            <h3>Fail-closed blockers</h3>
            <p>{report.blockers.join(' · ')}</p>
            <small>{report.maxEvidenceAgeDays}-day evidence window</small>
          </article>
        ) : null}
      </div>
    </section>
  );
}

interface EdgeGeometry {
  path: string;
  labelX: number;
  labelY: number;
  labelWidth: number;
}

const LOOP_NODE_HEIGHT = 86;
const ROUTE_GRID = 16;
const ROUTE_CLEARANCE = 12;
type RoutePoint = { x: number; y: number };

function portOffset(edgeId: string): number {
  const hash = Array.from(edgeId).reduce((value, character) => value + character.charCodeAt(0), 0);
  return [-24, -12, 0, 12, 24][hash % 5];
}

function edgePorts(from: AgentLoopNode, to: AgentLoopNode, edgeId: string): { start: RoutePoint; end: RoutePoint } {
  const fromWidth = from.width ?? 190;
  const toWidth = to.width ?? 190;
  const fromCenter = { x: from.x + fromWidth / 2, y: from.y + LOOP_NODE_HEIGHT / 2 };
  const toCenter = { x: to.x + toWidth / 2, y: to.y + LOOP_NODE_HEIGHT / 2 };
  const offset = portOffset(edgeId);
  if (Math.abs(toCenter.x - fromCenter.x) >= Math.abs(toCenter.y - fromCenter.y)) {
    const right = toCenter.x >= fromCenter.x;
    return {
      start: { x: right ? from.x + fromWidth : from.x, y: fromCenter.y + offset },
      end: { x: right ? to.x : to.x + toWidth, y: toCenter.y - offset },
    };
  }
  const down = toCenter.y >= fromCenter.y;
  return {
    start: { x: fromCenter.x + offset, y: down ? from.y + LOOP_NODE_HEIGHT : from.y },
    end: { x: toCenter.x - offset, y: down ? to.y : to.y + LOOP_NODE_HEIGHT },
  };
}

function gridPoint(point: RoutePoint): RoutePoint {
  return { x: Math.round(point.x / ROUTE_GRID), y: Math.round(point.y / ROUTE_GRID) };
}

function routeKey(point: RoutePoint): string {
  return `${point.x}:${point.y}`;
}

function pointIsBlocked(point: RoutePoint, fromId: string, toId: string): boolean {
  const pixel = { x: point.x * ROUTE_GRID, y: point.y * ROUTE_GRID };
  return agentLoopNodes.some((node) => {
    if (node.id === fromId || node.id === toId) return false;
    const width = node.width ?? 190;
    return pixel.x >= node.x - ROUTE_CLEARANCE
      && pixel.x <= node.x + width + ROUTE_CLEARANCE
      && pixel.y >= node.y - ROUTE_CLEARANCE
      && pixel.y <= node.y + LOOP_NODE_HEIGHT + ROUTE_CLEARANCE;
  });
}

function findClearRoute(edge: AgentLoopEdge, start: RoutePoint, end: RoutePoint, congestion: Map<string, number>): RoutePoint[] {
  const origin = gridPoint(start);
  const target = gridPoint(end);
  const targetKey = routeKey(target);
  const open: Array<{ point: RoutePoint; score: number }> = [{ point: origin, score: 0 }];
  const cameFrom = new Map<string, RoutePoint>();
  const cost = new Map<string, number>([[routeKey(origin), 0]]);
  const closed = new Set<string>();
  const maxX = Math.floor(LOOP_CANVAS.width / ROUTE_GRID);
  const maxY = Math.floor(LOOP_CANVAS.height / ROUTE_GRID);

  while (open.length > 0) {
    let bestIndex = 0;
    for (let index = 1; index < open.length; index += 1) {
      if (open[index].score < open[bestIndex].score) bestIndex = index;
    }
    const [{ point }] = open.splice(bestIndex, 1);
    const key = routeKey(point);
    if (closed.has(key)) continue;
    if (key === targetKey) {
      const path = [point];
      let cursor = point;
      while (cameFrom.has(routeKey(cursor))) {
        cursor = cameFrom.get(routeKey(cursor)) as RoutePoint;
        path.push(cursor);
      }
      return path.reverse().map((item) => ({ x: item.x * ROUTE_GRID, y: item.y * ROUTE_GRID }));
    }
    closed.add(key);
    const neighbors = [
      { x: point.x + 1, y: point.y }, { x: point.x - 1, y: point.y },
      { x: point.x, y: point.y + 1 }, { x: point.x, y: point.y - 1 },
    ];
    for (const neighbor of neighbors) {
      const neighborKey = routeKey(neighbor);
      if (neighbor.x < 1 || neighbor.y < 1 || neighbor.x >= maxX || neighbor.y >= maxY
        || closed.has(neighborKey) || pointIsBlocked(neighbor, edge.from, edge.to)) continue;
      const nextCost = (cost.get(key) ?? 0) + 1 + (congestion.get(neighborKey) ?? 0) * 9;
      if (nextCost >= (cost.get(neighborKey) ?? Number.POSITIVE_INFINITY)) continue;
      cost.set(neighborKey, nextCost);
      cameFrom.set(neighborKey, point);
      const distance = Math.abs(target.x - neighbor.x) + Math.abs(target.y - neighbor.y);
      open.push({ point: neighbor, score: nextCost + distance });
    }
  }
  return [start, { x: (start.x + end.x) / 2, y: start.y }, { x: (start.x + end.x) / 2, y: end.y }, end];
}

function simplifyRoute(points: RoutePoint[]): RoutePoint[] {
  return points.filter((point, index) => {
    if (index === 0 || index === points.length - 1) return true;
    const previous = points[index - 1];
    const next = points[index + 1];
    return !((previous.x === point.x && point.x === next.x) || (previous.y === point.y && point.y === next.y));
  });
}

function buildEdgeGeometries(): Map<string, EdgeGeometry> {
  const nodeById = new Map(agentLoopNodes.map((node) => [node.id, node]));
  const congestion = new Map<string, number>();
  const geometries = new Map<string, EdgeGeometry>();
  for (const edge of agentLoopEdges) {
    const from = nodeById.get(edge.from);
    const to = nodeById.get(edge.to);
    if (!from || !to) continue;
    const { start, end } = edgePorts(from, to, edge.id);
    const routed = findClearRoute(edge, start, end, congestion);
    const points = simplifyRoute([start, ...routed, end]);
    for (const point of routed) {
      const key = routeKey(gridPoint(point));
      congestion.set(key, (congestion.get(key) ?? 0) + 1);
    }
    const segments = points.slice(1).map((point, index) => ({
      from: points[index],
      to: point,
      length: Math.abs(point.x - points[index].x) + Math.abs(point.y - points[index].y),
    }));
    const labelSegment = [...segments].sort((left, right) => {
      const horizontalDifference = Number(right.from.y === right.to.y) - Number(left.from.y === left.to.y);
      return horizontalDifference || right.length - left.length;
    })[0];
    const horizontal = labelSegment.from.y === labelSegment.to.y;
    geometries.set(edge.id, {
      path: points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point.y}`).join(' '),
      labelX: (labelSegment.from.x + labelSegment.to.x) / 2 + (horizontal ? 0 : 10),
      labelY: (labelSegment.from.y + labelSegment.to.y) / 2 + (horizontal ? -9 : 3),
      labelWidth: Math.max(44, edge.label.length * 5.6 + 14),
    });
  }
  return geometries;
}

const edgeGeometryById = buildEdgeGeometries();

interface AgentLoopGraphProps {
  events: TraceEvent[];
  selectedEventIndex: number;
  selectedNodeId: string;
  layer: ArchitectureLayer | 'all';
  zoom: number;
  onNodeSelect: (nodeId: string) => void;
}

function AgentLoopGraph({ events, selectedEventIndex, selectedNodeId, layer, zoom, onNodeSelect }: AgentLoopGraphProps) {
  const nodeById = useMemo(() => new Map(agentLoopNodes.map((node) => [node.id, node])), []);
  const visitedStages = useMemo(() => new Set(events.slice(0, selectedEventIndex + 1).map(eventStage)), [events, selectedEventIndex]);
  const currentStage = events[selectedEventIndex] ? eventStage(events[selectedEventIndex]) : null;
  const visibleNodeIds = useMemo(() => new Set(agentLoopNodes.filter((node) => layer === 'all' || node.layer === layer).map((node) => node.id)), [layer]);

  return (
    <div className="agent-loop-viewport" aria-label="Interactive agent architecture flowchart">
      <div className="agent-loop-canvas" style={{ width: LOOP_CANVAS.width * zoom, height: LOOP_CANVAS.height * zoom }}>
        <div className="agent-loop-scale" style={{ width: LOOP_CANVAS.width, height: LOOP_CANVAS.height, transform: `scale(${zoom})` }}>
          <div className="primary-flow-direction"><strong>Primary direction</strong><span>request → understand → reason ↔ use tools → propose → approve → execute → respond</span></div>
          {agentLoopGroups.map((group) => <section key={group.id} className={`loop-group group-${group.kind}`} style={{ left: group.x, top: group.y, width: group.width, height: group.height }}><header><strong>{group.label}</strong><span>{group.plainTitle}</span></header></section>)}
          <svg
            className="agent-loop-edges"
            viewBox={`0 0 ${LOOP_CANVAS.width} ${LOOP_CANVAS.height}`}
            role="img"
            aria-label="Directional data-flow relationships between architecture components"
          >
            <defs>
              {(['normal', 'loop', 'storage', 'conditional', 'human'] as const).map((kind) => (
                <marker key={kind} id={`arrow-${kind}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" /></marker>
              ))}
            </defs>
            {agentLoopEdges.map((edge) => {
              const from = nodeById.get(edge.from);
              const to = nodeById.get(edge.to);
              if (!from || !to) return null;
              const visible = layer === 'all' || visibleNodeIds.has(edge.from) || visibleNodeIds.has(edge.to);
              const active = currentStage === edge.from || currentStage === edge.to;
              const visited = visitedStages.has(edge.from) && visitedStages.has(edge.to);
              const related = selectedNodeId === edge.from || selectedNodeId === edge.to;
              const geometry = edgeGeometryById.get(edge.id);
              if (!geometry) return null;
              return (
                <g
                  key={edge.id}
                  className={`loop-edge-group edge-${edge.kind}${active ? ' is-active' : ''}${visited ? ' is-visited' : ''}${related ? ' is-related' : ' is-unrelated'}${visible ? '' : ' is-dimmed'}`}
                  data-edge-id={edge.id}
                  data-direction={`${edge.from}-to-${edge.to}`}
                >
                  <title>{`${from.plainTitle} → ${to.plainTitle}: ${edge.label}`}</title>
                  <path d={geometry.path} className="loop-edge-halo" />
                  <path
                    d={geometry.path}
                    className="loop-edge"
                    markerEnd={`url(#arrow-${edge.kind})`}
                  />
                  <rect
                    className="loop-edge-label-background"
                    x={geometry.labelX - geometry.labelWidth / 2}
                    y={geometry.labelY - 9}
                    width={geometry.labelWidth}
                    height={16}
                    rx={6}
                  />
                  <text
                    className="loop-edge-label"
                    x={geometry.labelX}
                    y={geometry.labelY + 2}
                    textAnchor="middle"
                  >
                    {edge.label}
                  </text>
                </g>
              );
            })}
          </svg>
          {agentLoopNodes.map((node) => {
            const active = currentStage === node.id;
            const visited = visitedStages.has(node.id);
            const dimmed = layer !== 'all' && node.layer !== layer;
            const role = ['model', 'planner'].includes(node.id) ? 'agent' : ['database', 'ledger', 'memory'].includes(node.id) ? 'store' : ['api', 'registry', 'planner_bridge'].includes(node.id) ? 'boundary' : 'component';
            return (
              <button key={node.id} type="button" className={`loop-node node-${role} layer-${node.layer}${active ? ' is-active' : ''}${visited ? ' is-visited' : ''}${selectedNodeId === node.id ? ' is-selected' : ''}${dimmed ? ' is-dimmed' : ''}`} style={{ left: node.x, top: node.y, width: node.width ?? 190 }} onClick={() => onNodeSelect(node.id)} aria-pressed={selectedNodeId === node.id}>
                <small>{layerLabels[node.layer]}</small><strong>{node.label}</strong><span>{node.plainTitle}</span>{active ? <em>current event</em> : null}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function NodeInspector({ node, events, currentEvent }: { node: AgentLoopNode; events: TraceEvent[]; currentEvent: TraceEvent | null }) {
  const matchingEvents = events.filter((event) => eventStage(event) === node.id);
  const connections = agentLoopEdges.filter((edge) => edge.from === node.id || edge.to === node.id);
  return (
    <aside className="loop-node-inspector">
      <div className={`inspector-layer layer-${node.layer}`}>{layerLabels[node.layer]}</div>
      <p className="architecture-eyebrow">{node.label}</p><h2>{node.plainTitle}</h2><p>{node.summary}</p>
      <dl><div><dt>What comes in</dt><dd>{node.receives}</dd></div><div><dt>What comes out</dt><dd>{node.produces}</dd></div></dl>
      <div className="architecture-chip-row">{node.implementation.map((item) => <code key={item}>{item}</code>)}</div>
      <section className="node-paths"><h3>Possible paths</h3>{connections.map((edge) => {
        const outgoing = edge.from === node.id;
        const peerId = outgoing ? edge.to : edge.from;
        const peer = agentLoopNodes.find((item) => item.id === peerId);
        return <div key={edge.id} className={`path-${edge.kind}`}><span>{outgoing ? '→' : '←'}</span><p><strong>{edge.label}</strong><small>{outgoing ? `Next: ${peer?.plainTitle}` : `From: ${peer?.plainTitle}`}</small></p></div>;
      })}</section>
      <div className="node-event-count"><strong>{matchingEvents.length}</strong><span>events at this stage in the selected turn</span></div>
      {currentEvent && eventStage(currentEvent) === node.id ? <p className="node-current-note">The replay cursor is currently showing this stage.</p> : null}
    </aside>
  );
}

function EventInspector({ event, index, total, request }: { event: TraceEvent | null; index: number; total: number; request?: Record<string, unknown> }) {
  if (!event) return <div className="event-inspector architecture-empty">Run a prompt or choose a saved session to inspect captured values.</div>;
  const captured = event.details.captured ?? event.details;
  return (
    <section className="event-inspector">
      <header><div><p className="architecture-eyebrow">Event {index + 1} of {total}</p><h3>{event.eventType.replaceAll('_', ' ')}</h3></div><time>{formatTime(event.createdAt)}</time></header>
      <div className="event-meta"><span>source <strong>{event.source}</strong></span><span>sequence <strong>{event.sequence}</strong></span>{event.goalRevision !== null ? <span>goal revision <strong>{event.goalRevision}</strong></span> : null}</div>
      {eventStage(event) === 'prompt' && request ? <details open><summary>Browser → API request envelope</summary><pre>{pretty(request)}</pre></details> : null}
      <details open><summary>Captured inputs, outputs and state</summary><pre>{pretty(captured)}</pre></details>
      <details><summary>Trace identity</summary><pre>{pretty({ eventId: event.id, turnId: event.turnId, runId: event.runId, goalId: event.goalId, correlationId: event.correlationId })}</pre></details>
    </section>
  );
}

function SessionBrowser({ sessions, selectedSessionId, onSelect }: { sessions: AgentSessionSummary[]; selectedSessionId: string | null; onSelect: (id: string) => void }) {
  return <aside className="trace-session-browser"><header><p className="architecture-eyebrow">Persisted event ledger</p><h2>Sessions</h2></header>{sessions.map((session) => (
    <button key={session.id} type="button" className={selectedSessionId === session.id ? 'is-active' : ''} onClick={() => onSelect(session.id)}><strong>{session.title}</strong><p>{session.latestPrompt ?? 'No prompt'}</p><span>{session.turnCount} turns · {session.eventCount} events · {formatTime(session.updatedAt)}</span></button>
  ))}</aside>;
}

function FlowSelectionGuide({ events, onSelectStage }: { events: TraceEvent[]; onSelectStage: (stageId: string) => void }) {
  return <section className="flow-selection-guide"><header><div><p className="architecture-eyebrow">Routing logic in real-world terms</p><h2>When each flow should be selected</h2></div><p>A highlighted rule was observed in the selected turn. Use the warning signs to judge whether routing and recovery behaved correctly.</p></header><div className="flow-rule-grid">{flowSelectionRules.map((rule) => {
    const observed = events.some((event) => {
      if (!rule.eventTypes.includes(event.eventType)) return false;
      const eventText = pretty(event.details).toLowerCase();
      if (rule.id === 'clarification' && !eventText.includes('clarification')) return false;
      if (rule.id === 'conflict' && !eventText.includes('conflict')) return false;
      if (rule.id === 'continuation' && !eventText.includes('active_request_continuation')) return false;
      if (!rule.toolNames?.length) return true;
      const toolName = eventToolName(event);
      return typeof toolName === 'string' && rule.toolNames.includes(toolName);
    });
    return <button key={rule.id} type="button" className={observed ? 'is-observed' : ''} onClick={() => onSelectStage(rule.stageId)}><div><span>{rule.routeLabel}</span>{observed ? <em>observed this turn</em> : null}</div><h3>{rule.title}</h3><dl><div><dt>Use when</dt><dd>{rule.selectedWhen}</dd></div><div><dt>Correct behavior</dt><dd>{rule.expectedBehavior}</dd></div><div className="rule-warning"><dt>Warning sign</dt><dd>{rule.warningSign}</dd></div></dl></button>;
  })}</div></section>;
}

export function AgentArchitectureAdmin({ standalone = false }: { standalone?: boolean }) {
  const [activeTab, setActiveTab] = useState<AdminTab>('map');
  const [snapshot, setSnapshot] = useState<AgentArchitectureSnapshot | null>(null);
  const [sessions, setSessions] = useState<AgentSessionSummary[]>([]);
  const [trace, setTrace] = useState<AgentSessionTrace | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const [selectedEventIndex, setSelectedEventIndex] = useState(-1);
  const [selectedNodeId, setSelectedNodeId] = useState('context');
  const [selectedToolName, setSelectedToolName] = useState<string | null>(null);
  const [selectedTableName, setSelectedTableName] = useState<string | null>(null);
  const [toolQuery, setToolQuery] = useState('');
  const [dataQuery, setDataQuery] = useState('');
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [observedRun, setObservedRun] = useState<{ sessionId: string; turnId: string; status: string } | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isTraceLoading, setIsTraceLoading] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [layer, setLayer] = useState<ArchitectureLayer | 'all'>('all');
  const [zoom, setZoom] = useState(0.58);
  const [error, setError] = useState<string | null>(null);
  const traceRefreshTimer = useRef<number | null>(null);
  const activeCategory = useCategoryStore((state) => state.activeCategory);
  const fetchCategories = useCategoryStore((state) => state.fetchCategories);
  const categoryLoading = useCategoryStore((state) => state.isLoading);
  const fetchNodes = useNodeStore((state) => state.fetchNodes);
  const stagedNodeIds = useNodeStore((state) => state.stagedNodeIds);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [nextSnapshot, nextSessions] = await Promise.all([fetchAgentArchitecture(), fetchAgentSessions()]);
        if (cancelled) return;
        setSnapshot(nextSnapshot); setSessions(nextSessions);
        setSelectedToolName(nextSnapshot.tools[0]?.name ?? null);
        setSelectedTableName(nextSnapshot.database.tables[0]?.name ?? null);
      } catch (loadError) {
        if (!cancelled) setError(loadError instanceof Error ? loadError.message : 'Architecture data did not load.');
      } finally { if (!cancelled) setIsLoading(false); }
    };
    void load();
    return () => { cancelled = true; if (traceRefreshTimer.current !== null) window.clearTimeout(traceRefreshTimer.current); };
  }, []);

  useEffect(() => { void fetchCategories(); }, [fetchCategories]);
  useEffect(() => { if (activeCategory) void fetchNodes(activeCategory.id); }, [activeCategory, fetchNodes]);

  const turnEvents = useMemo(() => trace?.events.filter((event) => event.turnId === selectedTurnId) ?? [], [selectedTurnId, trace]);
  const currentEvent = selectedEventIndex >= 0 ? turnEvents[selectedEventIndex] ?? null : null;
  const selectedRun = trace?.runs.find((run) => run.turnId === selectedTurnId) ?? null;
  const displayedNodeId = currentEvent ? eventStage(currentEvent) : selectedNodeId;
  const selectedNode = agentLoopNodes.find((node) => node.id === displayedNodeId) ?? agentLoopNodes[0];
  const usedToolNames = useMemo(() => [...new Set(turnEvents.map(eventToolName).filter((name): name is string => typeof name === 'string' && Boolean(name)))], [turnEvents]);
  const usedSkills = useMemo(() => snapshot?.skills.filter((skill) => skill.recommendedTools.some((tool) => usedToolNames.includes(tool)) || usedToolNames.includes('load_skill') && turnEvents.some((event) => pretty(event.details).includes(skill.id))) ?? [], [snapshot, turnEvents, usedToolNames]);

  useEffect(() => {
    if (!isPlaying || turnEvents.length === 0) return;
    const timer = window.setInterval(() => {
      setSelectedEventIndex((current) => {
        if (current >= turnEvents.length - 1) { setIsPlaying(false); return current; }
        return current + 1;
      });
    }, 900);
    return () => window.clearInterval(timer);
  }, [isPlaying, turnEvents.length]);

  const filteredTools = useMemo(() => {
    const query = toolQuery.trim().toLowerCase();
    if (!snapshot || !query) return snapshot?.tools ?? [];
    return snapshot.tools.filter((tool) => `${tool.name} ${tool.description} ${tool.resultKind}`.toLowerCase().includes(query));
  }, [snapshot, toolQuery]);
  const selectedTool = snapshot?.tools.find((tool) => tool.name === selectedToolName) ?? filteredTools[0] ?? null;
  const filteredTables = useMemo(() => {
    const query = dataQuery.trim().toLowerCase();
    if (!snapshot || !query) return snapshot?.database.tables ?? [];
    return snapshot.database.tables.filter((table) => table.name.toLowerCase().includes(query));
  }, [dataQuery, snapshot]);
  const selectedTable = snapshot?.database.tables.find((table) => table.name === selectedTableName) ?? filteredTables[0] ?? null;

  const applyTrace = (nextTrace: AgentSessionTrace, turnId?: string | null, followLatest = true) => {
    const nextTurnId = turnId ?? nextTrace.turns.at(-1)?.id ?? null;
    const nextEvents = nextTrace.events.filter((event) => event.turnId === nextTurnId);
    setTrace(nextTrace); setSelectedTurnId(nextTurnId);
    if (followLatest) setSelectedEventIndex(nextEvents.length - 1);
  };

  const handleSessionSelect = async (sessionId: string) => {
    setIsPlaying(false);
    setSelectedSessionId(sessionId); setIsTraceLoading(true); setError(null);
    try { applyTrace(await fetchAgentSessionTrace(sessionId)); setActiveTab('map'); }
    catch (loadError) { setError(loadError instanceof Error ? loadError.message : 'Session trace did not load.'); }
    finally { setIsTraceLoading(false); }
  };

  const handleChatRunUpdate = useCallback((result: StrategistRunResponse) => {
    const status = result.run?.status ?? (result.turn.error ? 'failed' : result.turn.response ? 'completed' : 'running');
    setSelectedSessionId(result.session.id);
    setSelectedTurnId(result.turn.id);
    setRunStatus(status);
    setObservedRun({ sessionId: result.session.id, turnId: result.turn.id, status });
    if (TERMINAL_RUN_STATUSES.has(status)) {
      void fetchAgentSessions().then(setSessions).catch(() => undefined);
    }
    if (traceRefreshTimer.current !== null) window.clearTimeout(traceRefreshTimer.current);
    traceRefreshTimer.current = window.setTimeout(() => {
      void fetchAgentSessionTrace(result.session.id).then((nextTrace) => {
        const nextEvents = nextTrace.events.filter((event) => event.turnId === result.turn.id);
        setTrace(nextTrace);
        setSelectedEventIndex(nextEvents.length - 1);
      }).catch(() => undefined);
    }, 80);
  }, []);

  const handleChatSessionActivity = useCallback((sessionId: string, turnId: string) => {
    window.setTimeout(() => {
      void fetchAgentSessionTrace(sessionId).then((nextTrace) => {
        const nextEvents = nextTrace.events.filter((event) => event.turnId === turnId);
        setTrace(nextTrace);
        setSelectedSessionId(sessionId);
        setSelectedTurnId(turnId);
        setSelectedEventIndex(nextEvents.length - 1);
      }).catch(() => undefined);
    }, 120);
  }, []);

  useEffect(() => {
    if (!observedRun || TERMINAL_RUN_STATUSES.has(observedRun.status)) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const nextTrace = await fetchAgentSessionTrace(observedRun.sessionId);
        if (cancelled) return;
        const nextEvents = nextTrace.events.filter((event) => event.turnId === observedRun.turnId);
        setTrace(nextTrace);
        setSelectedEventIndex(nextEvents.length - 1);
      } catch { /* The event ledger can briefly trail initial run creation. */ }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 750);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [observedRun]);

  const handleNodeSelect = (nodeId: string) => {
    setSelectedNodeId(nodeId);
    const lastMatch = turnEvents.findLastIndex((event) => eventStage(event) === nodeId);
    setSelectedEventIndex(lastMatch);
  };

  if (isLoading) return <div className="architecture-loading">Loading the live agent architecture…</div>;
  if (error && !snapshot) return <div className="architecture-loading architecture-error" role="alert">{error}</div>;
  if (!snapshot) return null;

  return (
    <main className={`architecture-admin${standalone ? ' is-standalone' : ''}`} data-testid="agent-architecture-admin">
      <header className="architecture-hero">
        <div><p className="architecture-eyebrow">Internal live debugger · read only until normal review approval</p><h1>Agent architecture</h1><p>See every loop, replay real persisted events, and inspect what the system knew at each stage.</p></div>
        <div className="architecture-provider-card"><span>Current runtime</span><strong>{snapshot.provider.activeProvider}</strong><small>{snapshot.provider.model ?? 'default model'} · {snapshot.provider.transport}</small></div>
      </header>
      {error ? <div className="architecture-warning" role="alert">{error}</div> : null}
      <nav className="architecture-tabs" aria-label="Architecture debugger sections">{tabs.map((tab) => <button key={tab.id} type="button" className={activeTab === tab.id ? 'is-active' : ''} onClick={() => setActiveTab(tab.id)} aria-current={activeTab === tab.id ? 'page' : undefined}><strong>{tab.label}</strong><span>{tab.description}</span></button>)}</nav>

      {activeTab === 'map' ? <div className="architecture-tab-panel live-map-tab">
        <section className="debug-chat-workbench">
          <header><div><p className="architecture-eyebrow">Same AI workspace, instrumented</p><h2>Conversation and live execution</h2><p>Continue naturally across turns. Agent replies, clarifications, proposals, edits, decisions and approvals render exactly as they do on the Drasil homepage while the graph follows the selected run.</p></div><span className={`run-status status-${runStatus ?? 'idle'}`}><i />{runStatus ?? 'ready'}</span></header>
          <div className="architecture-chat-host">
            {activeCategory ? <StrategistAiCard embedded activeCategory={activeCategory} currentTask={null} stagedNodeIds={stagedNodeIds} isExpanded={false} onToggleExpanded={() => undefined} onClose={() => undefined} launchRequest={null} onLaunchHandled={() => undefined} onRunUpdate={handleChatRunUpdate} onSessionActivity={handleChatSessionActivity} /> : <div className="architecture-empty">{categoryLoading ? 'Loading the AI workspace…' : 'Create a workspace before starting a conversation.'}</div>}
          </div>
        </section>

        <section className="loop-toolbar">
          <div className="layer-filter" role="group" aria-label="Flowchart layer">{(Object.keys(layerLabels) as Array<ArchitectureLayer | 'all'>).map((item) => <button key={item} type="button" className={layer === item ? 'is-active' : ''} onClick={() => setLayer(item)}>{layerLabels[item]}</button>)}</div>
          <div className="flow-legend" aria-label="Flow types"><span className="legend-live">✓ live runtime only</span><span className="legend-loop">agent loop</span><span className="legend-storage">stored/read</span><span className="legend-conditional">conditional</span><span className="legend-human">human choice</span></div>
          <div className="zoom-controls"><button type="button" onClick={() => setZoom((value) => Math.max(0.45, value - 0.1))} aria-label="Zoom out">−</button><span>{Math.round(zoom * 100)}%</span><button type="button" onClick={() => setZoom((value) => Math.min(1.15, value + 0.1))} aria-label="Zoom in">+</button></div>
        </section>

        <section className="agent-loop-layout">
          <AgentLoopGraph events={turnEvents} selectedEventIndex={selectedEventIndex} selectedNodeId={displayedNodeId} layer={layer} zoom={zoom} onNodeSelect={handleNodeSelect} />
          <NodeInspector node={selectedNode} events={turnEvents} currentEvent={currentEvent} />
        </section>

        <FlowSelectionGuide events={turnEvents} onSelectStage={handleNodeSelect} />

        <section className="replay-console">
          <header><div><p className="architecture-eyebrow">State-by-state replay</p><h2>Walk through the actual run</h2></div>{trace ? <label>Turn<select value={selectedTurnId ?? ''} onChange={(event) => { const turnId = event.target.value; setSelectedTurnId(turnId); const count = trace.events.filter((item) => item.turnId === turnId).length; setSelectedEventIndex(count - 1); }}><option value="" disabled>Select a turn</option>{trace.turns.map((turn, index) => <option key={turn.id} value={turn.id}>{index + 1}. {turn.prompt.slice(0, 80)}</option>)}</select></label> : null}</header>
          <div className="replay-summary-grid"><div><span>Events</span><strong>{turnEvents.length}</strong></div><div><span>Tools used</span><strong>{usedToolNames.length}</strong><small>{usedToolNames.join(', ') || 'none yet'}</small></div><div><span>Skills inferred</span><strong>{usedSkills.length}</strong><small>{usedSkills.map((skill) => skill.title).join(', ') || 'base agent profile'}</small></div><div><span>Session</span><strong>{selectedSessionId ? 'continuing' : 'new'}</strong><small>{selectedSessionId ?? 'not started'}</small></div></div>
          <div className="replay-controls"><button type="button" onClick={() => setSelectedEventIndex(0)} disabled={turnEvents.length === 0}>First</button><button type="button" onClick={() => setSelectedEventIndex((value) => Math.max(0, value - 1))} disabled={selectedEventIndex <= 0}>Previous</button><button type="button" className="replay-play" onClick={() => { if (selectedEventIndex >= turnEvents.length - 1) setSelectedEventIndex(0); setIsPlaying((value) => !value); }} disabled={turnEvents.length === 0}>{isPlaying ? 'Pause' : 'Play'}</button><button type="button" onClick={() => setSelectedEventIndex((value) => Math.min(turnEvents.length - 1, value + 1))} disabled={selectedEventIndex >= turnEvents.length - 1}>Next</button><button type="button" onClick={() => setSelectedEventIndex(turnEvents.length - 1)} disabled={turnEvents.length === 0}>Latest</button><input type="range" min="0" max={Math.max(0, turnEvents.length - 1)} value={Math.max(0, selectedEventIndex)} onChange={(event) => { setIsPlaying(false); setSelectedEventIndex(Number(event.target.value)); }} disabled={turnEvents.length === 0} aria-label="Trace replay position" /></div>
          <div className="replay-detail-grid"><div className="event-rail">{turnEvents.map((event, index) => <button key={event.id} type="button" className={selectedEventIndex === index ? 'is-active' : ''} onClick={() => { setIsPlaying(false); setSelectedEventIndex(index); }}><span>{index + 1}</span><strong>{event.eventType.replaceAll('_', ' ')}</strong><small>{agentLoopNodes.find((node) => node.id === eventStage(event))?.label}</small></button>)}</div><EventInspector event={currentEvent} index={selectedEventIndex} total={turnEvents.length} request={selectedRun?.request} /></div>
        </section>

      </div> : null}

      {activeTab === 'contracts' ? <div className="architecture-tab-panel contract-layout"><aside className="contract-browser"><label><span>Registered tools</span><input type="search" value={toolQuery} onChange={(event) => setToolQuery(event.target.value)} placeholder="Search tool, result or purpose…" /></label><div className="contract-tool-list">{filteredTools.map((tool) => <button key={tool.name} type="button" className={selectedTool?.name === tool.name ? 'is-active' : ''} onClick={() => setSelectedToolName(tool.name)}><span><strong>{tool.name}</strong><small>{tool.resultKind}</small></span><em>{tool.readOnly ? 'read' : 'draft'}</em></button>)}</div><section className="contract-skill-index"><h3>Agent skills</h3>{snapshot.skills.map((skill) => <details key={skill.id}><summary>{skill.title}</summary><p>{skill.summary}</p><small>Available to: {skill.available_to.join(', ')}</small><div>{skill.recommendedTools.map((tool) => <code key={tool}>{tool}</code>)}</div></details>)}</section></aside>{selectedTool ? <ToolContractDetail tool={selectedTool} /> : <div className="architecture-empty">No tool matches this search.</div>}</div> : null}

      {activeTab === 'traces' ? <div className="architecture-tab-panel trace-layout"><SessionBrowser sessions={sessions} selectedSessionId={selectedSessionId} onSelect={(id) => void handleSessionSelect(id)} /><section className="saved-trace-summary">{isTraceLoading ? <div className="architecture-empty">Loading trace…</div> : trace ? <><p className="architecture-eyebrow">{trace.session.provider} · {trace.session.model ?? 'default model'}</p><h2>{trace.session.title}</h2><p>{trace.turns.length} turns and {trace.events.length} persisted events are available. Choose this session to open its latest turn on the live map.</p><button type="button" onClick={() => setActiveTab('map')}>Open interactive replay</button><div>{trace.turns.map((turn) => <article key={turn.id}><time>{formatTime(turn.createdAt)}</time><h3>{turn.prompt}</h3><p>{turn.response.summary ?? turn.error ?? 'Run in progress'}</p></article>)}</div></> : <div className="architecture-empty">Choose a session to replay it.</div>}</section></div> : null}

      {activeTab === 'data' ? (
        <div className="architecture-tab-panel data-tab">
          <McpReadPromotionDetail snapshot={snapshot} />
          <section className="rollout-strip">
            <header>
              <p className="architecture-eyebrow">Write authority</p>
              <h2>Family rollout</h2>
            </header>
            <div>
              {snapshot.rollout.families.map((family) => (
                <article key={family.family}>
                  <StatusPill active={family.enabled}>
                    {family.enabled ? 'native enabled' : 'legacy authority'}
                  </StatusPill>
                  <h3>{family.family}</h3>
                  <p>{family.operationKinds.join(' · ')}</p>
                  <small>{family.evidenceCount} evidence records</small>
                </article>
              ))}
            </div>
          </section>
          <div className="data-layout">
            <aside className="data-browser">
              <label>
                <span>Database tables</span>
                <input
                  type="search"
                  value={dataQuery}
                  onChange={(event) => setDataQuery(event.target.value)}
                  placeholder="goals, events, tasks…"
                />
              </label>
              {filteredTables.map((table) => (
                <button
                  key={table.name}
                  type="button"
                  className={selectedTable?.name === table.name ? 'is-active' : ''}
                  onClick={() => setSelectedTableName(table.name)}
                >
                  <strong>{table.name}</strong><span>{table.rowCount}</span>
                </button>
              ))}
            </aside>
            {selectedTable
              ? <DatabaseTableDetail table={selectedTable} />
              : <div className="architecture-empty">No table matches this search.</div>}
          </div>
          <section className="api-route-section">
            <header>
              <p className="architecture-eyebrow">HTTP endpoints</p>
              <h2>Current API surface</h2>
            </header>
            <div className="api-route-grid">
              {snapshot.apiRoutes.map((route) => (
                <article key={`${route.path}-${route.methods.join('-')}`}>
                  <code>{route.methods.join(' · ')}</code>
                  <strong>{route.path}</strong>
                  <span>{route.tags.join(', ') || route.name}</span>
                </article>
              ))}
            </div>
          </section>
        </div>
      ) : null}
    </main>
  );
}
