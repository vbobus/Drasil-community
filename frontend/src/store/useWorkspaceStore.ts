import { create } from 'zustand';
import { fetchWorkspace } from '../api/workspace';
import type { ApiNode } from '../api/nodes';
import { getBackendUrl, type DrasilAccount } from '../api/connection';
import type { StrategyNode } from '../types/node';
import type { Category } from '../types/category';
import type { CalendarBlock } from '../types/calendar';
import { useCategoryStore } from './useCategoryStore';
import { getNodeMutationRevision, nodeHasPendingWrites, useNodeStore } from './useNodeStore';
import { calendarHasPendingWrites, getCalendarMutationRevision, useCalendarStore } from './useCalendarStore';
import { useUiStore } from './useUiStore';

const CACHE_PREFIX = 'drasil.workspace.v1:';
interface Snapshot {
  version: 1; savedAt: number; categories: Category[]; nodes: ApiNode[]; blocks: CalendarBlock[];
  scheduledTaskIds: string[]; range: { startAt: string; endAt: string }; selectedId: number | null;
}
let cacheKey: string | null = null;
let generation = 0;
let pendingLoad: Promise<void> | null = null;
let unsubscribe: (() => void)[] = [];
let persistTimer: number | undefined;

export const useWorkspaceStore = create<{ ready: boolean; legacy: boolean; isRefreshing: boolean; error: string | null }>(() => ({
  ready: false, legacy: false, isRefreshing: false, error: null,
}));

function flatten(nodes: StrategyNode[]): ApiNode[] {
  return nodes.flatMap(node => {
    const { children, ...fields } = node;
    return [{ ...fields, id: Number(node.id), parentId: node.parentId ? Number(node.parentId) : null,
      nodeType: node.type, sortOrder: node.sortOrder ?? 0, isBacklog: node.isBacklog ?? false }, ...flatten(children)];
  });
}

function resetWorkspace() {
  generation += 1;
  pendingLoad = null;
  window.clearTimeout(persistTimer);
  unsubscribe.forEach(stop => stop());
  unsubscribe = [];
  cacheKey = null;
  useNodeStore.getState().reset();
  useCalendarStore.getState().reset();
  useCategoryStore.getState().reset();
  useWorkspaceStore.setState({ ready: false, legacy: false, error: null, isRefreshing: false });
}

// Only AccountBoundary calls this, after verifying the current account.
export function initializeWorkspace(account: DrasilAccount) {
  resetWorkspace();
  const legacy = account.id === 'isolated-test-account';
  useWorkspaceStore.setState({ legacy });
  if (legacy) return;
  cacheKey = `${CACHE_PREFIX}${getBackendUrl() || window.location.origin}:${account.id}`;
  try {
    const raw = window.sessionStorage.getItem(cacheKey);
    const saved = raw ? JSON.parse(raw) as Snapshot : null;
    if (saved?.version === 1 && Date.now() - saved.savedAt < 24 * 60 * 60 * 1000) {
      hydrate(saved);
    }
  } catch { /* Storage unavailable or invalid snapshot: use the server. */ }
  const queueSave = () => {
    window.clearTimeout(persistTimer);
    persistTimer = window.setTimeout(saveSnapshot, 250);
  };
  unsubscribe = [useNodeStore.subscribe(queueSave), useCategoryStore.subscribe(queueSave), useCalendarStore.subscribe(queueSave)];
}

function hydrate(saved: Snapshot) {
  useCategoryStore.getState().hydrateCategories(saved.categories, saved.selectedId);
  const selected = useUiStore.getState().activeDestination === 'all' ? null : useCategoryStore.getState().activeCategory?.id ?? null;
  useNodeStore.getState().hydrateNodes(saved.nodes, saved.categories.map(category => category.id), selected);
  useCalendarStore.getState().hydrateCalendar(saved.blocks, saved.scheduledTaskIds, saved.range);
  useWorkspaceStore.setState({ ready: true, error: null });
}

function saveSnapshot() {
  if (!cacheKey || !useWorkspaceStore.getState().ready) return;
  const calendar = useCalendarStore.getState();
  const range = calendar.coveredRanges.find(range => range.loadedAt > 0);
  if (!range) return;
  const categories = useCategoryStore.getState();
  const snapshot: Snapshot = { version: 1, savedAt: Date.now(), categories: categories.categories,
    nodes: Object.values(useNodeStore.getState().categoryNodeCache).flatMap(flatten), blocks: calendar.blocks,
    scheduledTaskIds: calendar.scheduledTaskIds, range, selectedId: categories.activeCategory?.id ?? null };
  try {
    const json = JSON.stringify(snapshot);
    if (json.length <= 1_000_000) window.sessionStorage.setItem(cacheKey, json);
    else window.sessionStorage.removeItem(cacheKey);
  } catch { /* In-memory cache remains useful if storage is full. */ }
}

export function refreshWorkspace(options?: { force?: boolean }): Promise<void> {
  if (options?.force) {
    generation += 1;
    pendingLoad = null;
  }
  if (pendingLoad) return pendingLoad;
  const requestGeneration = generation;
  const cachedNodes = useNodeStore.getState().categoryNodeCache;
  const cachedBlocks = useCalendarStore.getState().blocks;
  const nodeRevision = getNodeMutationRevision();
  const calendarRevision = getCalendarMutationRevision();
  useWorkspaceStore.setState({ isRefreshing: true, error: null });
  if (!useWorkspaceStore.getState().ready) {
    useCategoryStore.setState({ isLoading: true });
    useNodeStore.setState({ isLoading: true });
  }
  pendingLoad = (async () => {
    try {
      const response = await fetchWorkspace();
      if (generation !== requestGeneration) return;
      if (nodeHasPendingWrites() || calendarHasPendingWrites() || nodeRevision !== getNodeMutationRevision() || calendarRevision !== getCalendarMutationRevision()) return;
      // A read started before an edit must never undo the newer local result.
      if (cachedNodes !== useNodeStore.getState().categoryNodeCache || cachedBlocks !== useCalendarStore.getState().blocks) return;
      hydrate({ ...response, version: 1, savedAt: Date.now(), range: { startAt: response.startAt, endAt: response.endAt },
        selectedId: useCategoryStore.getState().activeCategory?.id ?? null });
      saveSnapshot();
      performance.mark('drasil:workspace-ready');
    } catch (error) {
      if (generation === requestGeneration) {
        useWorkspaceStore.setState({ error: error instanceof Error ? error.message : 'Could not load workspace. Please retry.' });
        useCategoryStore.setState({ isLoading: false });
        useNodeStore.setState({ isLoading: false });
      }
    } finally {
      if (generation === requestGeneration) {
        pendingLoad = null;
        useWorkspaceStore.setState({ isRefreshing: false });
      }
    }
  })();
  return pendingLoad;
}

window.addEventListener('drasil:clear-workspace', resetWorkspace);
