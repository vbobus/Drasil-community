// frontend/src/store/useNodeStore.ts
// Zustand store for managing task nodes.
// Converts flat API list into hierarchical tree for UI.

import { create } from 'zustand';
import type {
  StrategyNode,
  NodePriority,
  NodeStatus,
  NodeType,
  NodeDragPayload,
} from '../types/node';
import * as nodesApi from '../api/nodes';
import type { ApiNode } from '../api/nodes';
import { suggestEmoji } from '../lib/emoji';

interface NodeState {
  nodes: StrategyNode[]; // The root-level nodes with children nested
  categoryNodeCache: Record<number, StrategyNode[]>;
  activeCategoryId: number | null;
  isLoading: boolean;
  error: string | null;
  pendingFocusNodeId: string | null;
  activeDragPayload: NodeDragPayload | null;
  stagedNodeIds: string[];
}

interface NodeActions {
  fetchNodes: (categoryId: number) => Promise<void>;
  fetchAllNodes: (categoryIds: number[]) => Promise<void>;
  addNode: (data: { title: string; categoryId: number; parentId?: string; notes?: string; priority?: NodePriority; nodeType?: NodeType; deadlineAt?: string | null; emoji?: string; tags?: string[]; imageAttachments?: StrategyNode['imageAttachments']; isPinned?: boolean }) => Promise<ApiNode>;
  updateNode: (id: string, updates: { title?: string; notes?: string; status?: NodeStatus; priority?: NodePriority; deadlineAt?: string | null; timeEstimateMinutes?: number | null; emoji?: string; tags?: string[]; imageAttachments?: StrategyNode['imageAttachments']; isPinned?: boolean; isBacklog?: boolean; nodeType?: NodeType }) => Promise<void>;
  updateNodeStatuses: (updates: Record<string, NodeStatus>) => Promise<void>;
  moveNode: (id: string, move: { parentId?: string | null; targetIndex: number; categoryId: number; isBacklog?: boolean }) => Promise<void>;
  deleteNode: (id: string) => Promise<void>;
  stageNode: (id: string) => void;
  toggleStagedNode: (id: string) => void;
  removeStagedNode: (id: string) => void;
  reorderStagedNode: (id: string, targetId: string, dropIntent: 'before' | 'after') => void;
  clearStagedNodes: () => void;
  setActiveDragPayload: (payload: NodeDragPayload | null) => void;
  consumePendingFocusNodeId: () => void;
  clearError: () => void;
}

type NodeStore = NodeState & NodeActions;

let latestNodeLoadRequestId = 0;

function sanitizeNodeUpdates(
  updates: {
    title?: string;
    notes?: string;
    status?: NodeStatus;
    priority?: NodePriority;
    deadlineAt?: string | null;
    timeEstimateMinutes?: number | null;
    emoji?: string;
    tags?: string[];
    imageAttachments?: StrategyNode['imageAttachments'];
    isPinned?: boolean;
    isBacklog?: boolean;
    nodeType?: NodeType;
  },
) {
  const sanitized: Record<string, unknown> = {};

  for (const [key, value] of Object.entries(updates)) {
    if (value === undefined) {
      continue;
    }

    if (value === null && key !== 'deadlineAt' && key !== 'timeEstimateMinutes') {
      continue;
    }

    sanitized[key] = value;
  }

  return sanitized as typeof updates;
}

function findNodeById(nodes: StrategyNode[], id: string): StrategyNode | null {
  for (const node of nodes) {
    if (node.id === id) {
      return node;
    }
    const nested = findNodeById(node.children, id);
    if (nested) {
      return nested;
    }
  }
  return null;
}

function flattenNodeIds(nodes: StrategyNode[]): string[] {
  return nodes.flatMap((node) => [node.id, ...flattenNodeIds(node.children)]);
}

function flattenNodes(nodes: StrategyNode[]): StrategyNode[] {
  return nodes.flatMap((node) => [node, ...flattenNodes(node.children)]);
}

function flattenCachedNodeIds(cache: Record<number, StrategyNode[]>): Set<string> {
  return new Set(
    Object.values(cache).flatMap((nodes) => flattenNodeIds(nodes)),
  );
}

function updateNodeStatusesInTree(
  nodes: StrategyNode[],
  statusById: Record<string, NodeStatus>,
): StrategyNode[] {
  let didChange = false;
  const nextNodes = nodes.map((node) => {
    const children = updateNodeStatusesInTree(node.children, statusById);
    const nextStatus = statusById[node.id] ?? node.status;
    if (nextStatus === node.status && children === node.children) {
      return node;
    }

    didChange = true;
    return {
      ...node,
      status: nextStatus,
      children,
    };
  });

  return didChange ? nextNodes : nodes;
}

function waitForNextPaint(): Promise<void> {
  if (typeof window === 'undefined' || typeof window.requestAnimationFrame !== 'function') {
    return Promise.resolve();
  }

  return new Promise((resolve) => {
    window.requestAnimationFrame(() => {
      window.setTimeout(resolve, 0);
    });
  });
}

function collectDescendantIds(node: StrategyNode): string[] {
  return node.children.flatMap((child) => [child.id, ...collectDescendantIds(child)]);
}

function arePathsEqual(first: number[], second: number[]): boolean {
  return first.length === second.length && first.every((value, index) => value === second[index]);
}

function isPathPrefix(prefix: number[], path: number[]): boolean {
  return prefix.length <= path.length && prefix.every((value, index) => value === path[index]);
}

function reindexSiblings(nodes: StrategyNode[]): StrategyNode[] {
  return nodes.map((node, index) =>
    node.sortOrder === index ? node : { ...node, sortOrder: index },
  );
}

function findNodePath(
  nodes: StrategyNode[],
  id: string,
  path: number[] = [],
): number[] | null {
  for (let index = 0; index < nodes.length; index += 1) {
    const node = nodes[index];
    if (node.id === id) {
      return [...path, index];
    }

    path.push(index);
    const childPath = findNodePath(node.children, id, path);
    path.pop();

    if (childPath) {
      return childPath;
    }
  }

  return null;
}

function getNodeAtPath(nodes: StrategyNode[], path: number[]): StrategyNode | null {
  let siblings = nodes;
  let node: StrategyNode | null = null;

  for (const index of path) {
    node = siblings[index] ?? null;
    if (!node) {
      return null;
    }
    siblings = node.children;
  }

  return node;
}

function getSiblingsAtParentPath(
  nodes: StrategyNode[],
  parentPath: number[],
): StrategyNode[] | null {
  if (parentPath.length === 0) {
    return nodes;
  }

  return getNodeAtPath(nodes, parentPath)?.children ?? null;
}

function replaceSiblingsAtParentPath(
  nodes: StrategyNode[],
  parentPath: number[],
  nextSiblings: StrategyNode[],
): StrategyNode[] {
  if (parentPath.length === 0) {
    return nextSiblings;
  }

  const [index, ...childPath] = parentPath;
  const node = nodes[index];
  const nextNodes = [...nodes];

  nextNodes[index] = {
    ...node,
    children: replaceSiblingsAtParentPath(node.children, childPath, nextSiblings),
  };

  return nextNodes;
}

function removeNodeAtPath(nodes: StrategyNode[], path: number[]): StrategyNode[] {
  const [index, ...childPath] = path;
  const nextNodes = [...nodes];

  if (childPath.length === 0) {
    nextNodes.splice(index, 1);
    return reindexSiblings(nextNodes);
  }

  const node = nodes[index];
  nextNodes[index] = {
    ...node,
    children: removeNodeAtPath(node.children, childPath),
  };

  return nextNodes;
}

function updateMovedBranch(
  node: StrategyNode,
  parentId: string | null,
  categoryId: number,
  isBacklog?: boolean,
): StrategyNode {
  const nextBacklogState = isBacklog ?? node.isBacklog;
  if (categoryId === node.categoryId && nextBacklogState === node.isBacklog) {
    return node.parentId === parentId ? node : { ...node, parentId };
  }

  const updateDescendants = (children: StrategyNode[]): StrategyNode[] =>
    children.map((child) => ({
      ...child,
      categoryId,
      isBacklog: nextBacklogState,
      children: updateDescendants(child.children),
    }));

  return {
    ...node,
    parentId,
    categoryId,
    isBacklog: nextBacklogState,
    children: updateDescendants(node.children),
  };
}

function insertNodeIntoTree(
  nodes: StrategyNode[],
  nodeToInsert: StrategyNode,
  parentId: string | null,
  targetIndex: number,
): { nodes: StrategyNode[]; inserted: boolean } {
  const insertIntoSiblings = (siblings: StrategyNode[]): StrategyNode[] => {
    const nextSiblings = [...siblings];
    const clampedIndex = Math.max(0, Math.min(targetIndex, nextSiblings.length));
    nextSiblings.splice(clampedIndex, 0, nodeToInsert);
    return reindexSiblings(nextSiblings);
  };

  if (parentId === null) {
    return {
      nodes: insertIntoSiblings(nodes),
      inserted: true,
    };
  }

  const parentPath = findNodePath(nodes, parentId);
  if (!parentPath) {
    return { nodes, inserted: false };
  }

  const insertAtParentPath = (
    siblings: StrategyNode[],
    path: number[],
  ): StrategyNode[] => {
    const [index, ...childPath] = path;
    const node = siblings[index];
    const nextSiblings = [...siblings];

    nextSiblings[index] =
      childPath.length === 0
        ? {
            ...node,
            children: insertIntoSiblings(node.children),
          }
        : {
            ...node,
            children: insertAtParentPath(node.children, childPath),
          };

    return nextSiblings;
  };

  return {
    nodes: insertAtParentPath(nodes, parentPath),
    inserted: true,
  };
}

function moveNodeInTree(
  nodes: StrategyNode[],
  id: string,
  move: { parentId?: string | null; targetIndex: number; categoryId: number; isBacklog?: boolean },
): StrategyNode[] | null {
  const parentId = move.parentId ?? null;
  const sourcePath = findNodePath(nodes, id);
  if (!sourcePath) {
    return null;
  }

  const removedNode = getNodeAtPath(nodes, sourcePath);
  if (!removedNode) {
    return null;
  }

  const targetParentPath = parentId ? findNodePath(nodes, parentId) : [];
  if (parentId && !targetParentPath) {
    return null;
  }
  if (parentId && targetParentPath && isPathPrefix(sourcePath, targetParentPath)) {
    return null;
  }

  const movedNode = updateMovedBranch(
    removedNode,
    parentId,
    move.categoryId,
    move.isBacklog,
  );

  const sourceParentPath = sourcePath.slice(0, -1);
  if (targetParentPath && arePathsEqual(sourceParentPath, targetParentPath)) {
    const siblings = getSiblingsAtParentPath(nodes, sourceParentPath);
    if (!siblings) {
      return null;
    }

    const sourceIndex = sourcePath[sourcePath.length - 1];
    const nextSiblings = [...siblings];
    nextSiblings.splice(sourceIndex, 1);
    const insertionIndex = Math.max(0, Math.min(move.targetIndex, nextSiblings.length));

    if (sourceIndex === insertionIndex && movedNode === removedNode) {
      return nodes;
    }

    nextSiblings.splice(insertionIndex, 0, movedNode);
    return replaceSiblingsAtParentPath(
      nodes,
      sourceParentPath,
      reindexSiblings(nextSiblings),
    );
  }

  const removal = {
    nodes: removeNodeAtPath(nodes, sourcePath),
    removedNode,
  };
  const insertion = insertNodeIntoTree(
    removal.nodes,
    movedNode,
    parentId,
    move.targetIndex,
  );

  return insertion.inserted ? insertion.nodes : null;
}

function resolveNextStagedNodeIds(
  currentIds: string[],
  targetId: string,
  nodes: StrategyNode[],
  mode: 'stage' | 'toggle',
): string[] {
  const flatNodes = flattenNodes(nodes);
  const nodeById = new Map(flatNodes.map((node) => [node.id, node]));
  const targetNode = nodeById.get(targetId);

  if (!targetNode) {
    return currentIds;
  }

  const descendantIds = new Set(collectDescendantIds(targetNode));
  const nextIds = currentIds.filter((id) => !descendantIds.has(id));

  if (mode === 'toggle' && nextIds.includes(targetId)) {
    return nextIds.filter((id) => id !== targetId);
  }

  let parentId = targetNode.parentId ?? null;
  while (parentId) {
    if (nextIds.includes(parentId)) {
      return nextIds;
    }
    parentId = nodeById.get(parentId)?.parentId ?? null;
  }

  if (nextIds.includes(targetId)) {
    return nextIds;
  }

  return [...nextIds, targetId];
}

/**
 * Utility to convert flat array from backend into a nested tree.
 */
function buildTree(flatNodes: ApiNode[]): StrategyNode[] {
  const nodeMap: Record<string, StrategyNode> = {};
  const roots: StrategyNode[] = [];

  // Initialize nodes with string IDs and empty children
  flatNodes.forEach((n) => {
    nodeMap[n.id.toString()] = {
      ...n,
      id: n.id.toString(),
      type: n.nodeType === 'habit' ? 'habit' : 'task',
      parentId: n.parentId?.toString() ?? null,
      tags: n.tags ?? [],
      imageAttachments: n.imageAttachments ?? [],
      isPinned: n.isPinned ?? false,
      isRecurring: n.isRecurring ?? false,
      recurrenceRule: n.recurrenceRule ?? null,
      children: [],
    };
  });

  // Nest them
  flatNodes.forEach((n) => {
    const node = nodeMap[n.id.toString()];
    if (n.parentId) {
      const parent = nodeMap[n.parentId.toString()];
      if (parent) {
        parent.children.push(node);
      } else {
        // Parent might be missing if filtered? Treat as root as fallback.
        roots.push(node);
      }
    } else {
      roots.push(node);
    }
  });

  const sortTree = (nodes: StrategyNode[]): StrategyNode[] =>
    [...nodes]
      .sort((a, b) => (a.sortOrder ?? 0) - (b.sortOrder ?? 0))
      .map((node) => ({
        ...node,
        children: sortTree(node.children),
      }));

  return sortTree(roots);
}

export const useNodeStore = create<NodeStore>((set, get) => ({
  nodes: [],
  categoryNodeCache: {},
  activeCategoryId: null,
  isLoading: false,
  error: null,
  pendingFocusNodeId: null,
  activeDragPayload: null,
  stagedNodeIds: [],

  fetchNodes: async (categoryId) => {
    const requestId = ++latestNodeLoadRequestId;
    set({ isLoading: true, error: null });
    try {
      const flat = await nodesApi.fetchNodes(categoryId);
      const tree = buildTree(flat);
      if (requestId !== latestNodeLoadRequestId) {
        return;
      }
      set((state) => ({
        categoryNodeCache: {
          ...state.categoryNodeCache,
          [categoryId]: tree,
        },
        nodes: tree,
        activeCategoryId: categoryId,
        isLoading: false,
      }));
    } catch {
      if (requestId === latestNodeLoadRequestId) {
        set({ error: 'Failed to load nodes.', isLoading: false });
      }
    }
  },

  fetchAllNodes: async (categoryIds) => {
    const requestId = ++latestNodeLoadRequestId;
    set({ isLoading: true, error: null });

    if (categoryIds.length === 0) {
      set({
        nodes: [],
        categoryNodeCache: {},
        activeCategoryId: null,
        isLoading: false,
      });
      return;
    }

    try {
      const loadedCategories = await Promise.all(
        categoryIds.map(async (categoryId) => {
          const flat = await nodesApi.fetchNodes(categoryId);
          return [categoryId, buildTree(flat)] as const;
        }),
      );
      if (requestId !== latestNodeLoadRequestId) {
        return;
      }

      const categoryNodeCache = Object.fromEntries(loadedCategories) as Record<
        number,
        StrategyNode[]
      >;
      set({
        categoryNodeCache,
        nodes: loadedCategories.flatMap(([, tree]) => tree),
        activeCategoryId: null,
        isLoading: false,
      });
    } catch {
      if (requestId === latestNodeLoadRequestId) {
        set({ error: 'Failed to load all workspace tasks.', isLoading: false });
      }
    }
  },

  addNode: async (data) => {
    set({ error: null });
    const isAggregateView = get().activeCategoryId === null;
    try {
      const createdNode = await nodesApi.createNode({
        ...data,
        emoji: data.emoji ?? suggestEmoji(data.title, data.nodeType ?? 'task'),
      });
      // Re-fetch the affected workspace so tree structure and sort order stay canonical.
      if (isAggregateView) {
        const flat = await nodesApi.fetchNodes(data.categoryId);
        const tree = buildTree(flat);
        set((state) => {
          const nextCategoryNodeCache = {
            ...state.categoryNodeCache,
            [data.categoryId]: tree,
          };
          return {
            categoryNodeCache: nextCategoryNodeCache,
            nodes:
              state.activeCategoryId === null
                ? Object.values(nextCategoryNodeCache).flat()
                : nextCategoryNodeCache[state.activeCategoryId] ?? state.nodes,
          };
        });
      } else {
        await get().fetchNodes(data.categoryId);
      }
      set({ pendingFocusNodeId: createdNode.id.toString() });
      return createdNode;
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to create node.';
      set({ error: message });
      throw error instanceof Error ? error : new Error(message);
    }
  },

  updateNode: async (id, updates) => {
    if (get().error !== null) {
      set({ error: null });
    }
    const previousNodes = get().nodes;
    const previousCategoryNodeCache = get().categoryNodeCache;
    try {
      const sanitizedUpdates = sanitizeNodeUpdates(updates);
      const shouldRefetchAfterUpdate = sanitizedUpdates.isBacklog !== undefined;
      const normalizedUpdates = {
        ...sanitizedUpdates,
        ...(sanitizedUpdates.nodeType ? { type: sanitizedUpdates.nodeType } : {}),
      };

      if (!shouldRefetchAfterUpdate) {
        const updateRecursive = (nodes: StrategyNode[]): StrategyNode[] =>
          nodes.map((n) => {
            if (n.id === id) return { ...n, ...normalizedUpdates };
            if (n.children.length > 0) return { ...n, children: updateRecursive(n.children) };
            return n;
          });

        set((state) => {
          const nextCategoryNodeCache = Object.fromEntries(
            Object.entries(state.categoryNodeCache).map(([categoryId, nodes]) => [
              categoryId,
              updateRecursive(nodes),
            ]),
          ) as Record<number, StrategyNode[]>;
          const nextNodes =
            state.activeCategoryId !== null
              ? nextCategoryNodeCache[state.activeCategoryId] ?? updateRecursive(state.nodes)
              : updateRecursive(state.nodes);

          return {
            nodes: nextNodes,
            categoryNodeCache: nextCategoryNodeCache,
          };
        });
      }

      const currentNode =
        findNodeById(previousNodes, id) ??
        findNodeById(Object.values(previousCategoryNodeCache).flat(), id);

      await nodesApi.updateNode(id, sanitizedUpdates);
      if (shouldRefetchAfterUpdate) {
        if (currentNode) {
          const flat = await nodesApi.fetchNodes(currentNode.categoryId);
          const tree = buildTree(flat);
          set((state) => {
            const nextCategoryNodeCache = {
              ...state.categoryNodeCache,
              [currentNode.categoryId]: tree,
            };
            return {
              categoryNodeCache: nextCategoryNodeCache,
              nodes:
                state.activeCategoryId === null
                  ? Object.values(nextCategoryNodeCache).flat()
                  : nextCategoryNodeCache[state.activeCategoryId] ?? state.nodes,
            };
          });
          return;
        }
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to update node.';
      set({
        error: message,
        nodes: previousNodes,
        categoryNodeCache: previousCategoryNodeCache,
      });
      throw error instanceof Error ? error : new Error(message);
    }
  },

  updateNodeStatuses: async (updates) => {
    const ids = Object.keys(updates);
    if (ids.length === 0) {
      return;
    }

    if (get().error !== null) {
      set({ error: null });
    }
    const previousNodes = get().nodes;
    const previousCategoryNodeCache = get().categoryNodeCache;

    try {
      set((state) => {
        const nextCategoryNodeCache = Object.fromEntries(
          Object.entries(state.categoryNodeCache).map(([categoryId, nodes]) => [
            categoryId,
            updateNodeStatusesInTree(nodes, updates),
          ]),
        ) as Record<number, StrategyNode[]>;
        const nextNodes =
          state.activeCategoryId !== null
            ? nextCategoryNodeCache[state.activeCategoryId] ??
              updateNodeStatusesInTree(state.nodes, updates)
            : updateNodeStatusesInTree(state.nodes, updates);

        return {
          nodes: nextNodes,
          categoryNodeCache: nextCategoryNodeCache,
        };
      });

      await waitForNextPaint();

      await Promise.all(
        ids.map((id) => nodesApi.updateNode(id, { status: updates[id] })),
      );
    } catch (error) {
      const message =
        error instanceof Error ? error.message : 'Failed to update node statuses.';
      set({
        error: message,
        nodes: previousNodes,
        categoryNodeCache: previousCategoryNodeCache,
      });
      throw error instanceof Error ? error : new Error(message);
    }
  },

  moveNode: async (id, move) => {
    set({ error: null });
    const previousNodes = get().nodes;
    const previousCategoryNodeCache = get().categoryNodeCache;
    const nextNodes = moveNodeInTree(previousNodes, id, move);

    try {
      if (nextNodes) {
        set((state) => ({
          nodes: nextNodes,
          categoryNodeCache: {
            ...state.categoryNodeCache,
            [move.categoryId]: nextNodes,
          },
        }));
      }

      await nodesApi.moveNode(id, move);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to move node.';
      set({
        error: message,
        nodes: previousNodes,
        categoryNodeCache: previousCategoryNodeCache,
      });
      throw error instanceof Error ? error : new Error(message);
    }
  },

  deleteNode: async (id) => {
    set({ error: null });
    try {
      await nodesApi.deleteNode(id);
      // Local removal
      const removeRecursive = (nodes: StrategyNode[]): StrategyNode[] =>
        nodes
          .filter((n) => n.id !== id)
          .map((n) => ({ ...n, children: removeRecursive(n.children) }));

      set((state) => {
        const nextCategoryNodeCache = Object.fromEntries(
          Object.entries(state.categoryNodeCache).map(([categoryId, categoryNodes]) => [
            categoryId,
            removeRecursive(categoryNodes),
          ]),
        ) as Record<number, StrategyNode[]>;
        const nextNodes =
          state.activeCategoryId === null
            ? Object.values(nextCategoryNodeCache).flat()
            : nextCategoryNodeCache[state.activeCategoryId] ?? removeRecursive(state.nodes);
        const remainingIds = flattenCachedNodeIds(nextCategoryNodeCache);

        return {
          nodes: nextNodes,
          categoryNodeCache: nextCategoryNodeCache,
          stagedNodeIds: state.stagedNodeIds.filter((nodeId) => remainingIds.has(nodeId)),
        };
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to delete node.';
      set({ error: message });
      throw error instanceof Error ? error : new Error(message);
    }
  },

  stageNode: (id) => {
    set((state) => ({
      stagedNodeIds: resolveNextStagedNodeIds(state.stagedNodeIds, id, get().nodes, 'stage'),
    }));
  },

  toggleStagedNode: (id) => {
    set((state) => ({
      stagedNodeIds: resolveNextStagedNodeIds(state.stagedNodeIds, id, get().nodes, 'toggle'),
    }));
  },

  removeStagedNode: (id) => {
    set((state) => ({
      stagedNodeIds: state.stagedNodeIds.filter((nodeId) => nodeId !== id),
    }));
  },

  reorderStagedNode: (id, targetId, dropIntent) => {
    set((state) => {
      const currentIndex = state.stagedNodeIds.indexOf(id);
      const targetIndex = state.stagedNodeIds.indexOf(targetId);

      if (
        currentIndex < 0 ||
        targetIndex < 0 ||
        currentIndex === targetIndex ||
        id === targetId
      ) {
        return {};
      }

      let insertionIndex = targetIndex + (dropIntent === 'after' ? 1 : 0);
      if (currentIndex < insertionIndex) {
        insertionIndex -= 1;
      }

      if (
        insertionIndex < 0 ||
        targetIndex < 0 ||
        insertionIndex >= state.stagedNodeIds.length
      ) {
        return {};
      }

      const nextStagedNodeIds = [...state.stagedNodeIds];
      const [movedId] = nextStagedNodeIds.splice(currentIndex, 1);
      nextStagedNodeIds.splice(insertionIndex, 0, movedId);

      return {
        stagedNodeIds: nextStagedNodeIds,
      };
    });
  },

  clearStagedNodes: () => {
    set({ stagedNodeIds: [] });
  },

  setActiveDragPayload: (payload) => {
    set({ activeDragPayload: payload });
  },

  consumePendingFocusNodeId: () => {
    set({ pendingFocusNodeId: null });
  },

  clearError: () => {
    set({ error: null });
  },
}));
