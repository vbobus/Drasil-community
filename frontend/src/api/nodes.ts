// frontend/src/api/nodes.ts
// API client for task nodes.

import type {
  NodeImageAttachment,
  NodePriority,
  NodeStatus,
  NodeType,
  TaskRecurrenceRule,
} from '../types/node';
import { apiRequest } from './http';

const BASE = '/api/v1/nodes';

export interface ApiNode {
  id: number;
  title: string;
  notes?: string | null;
  status: NodeStatus;
  priority: NodePriority;
  nodeType: NodeType | 'strategy';
  categoryId: number;
  parentId: number | null;
  deadlineAt?: string | null;
  timeEstimateMinutes?: number | null;
  emoji?: string | null;
  tags: string[];
  imageAttachments: NodeImageAttachment[];
  isPinned: boolean;
  isRecurring: boolean;
  recurrenceRule?: TaskRecurrenceRule | null;
  sortOrder: number;
  isBacklog: boolean;
}

/** Fetch nodes for a category. Returns a flat list from API. */
export async function fetchNodes(categoryId: number): Promise<ApiNode[]> {
  return apiRequest<ApiNode[]>(`${BASE}?categoryId=${categoryId}`);
}

/** Create a new node. */
export async function createNode(data: {
  title: string;
  categoryId: number;
  notes?: string;
  priority?: NodePriority;
  nodeType?: NodeType;
  parentId?: string;
  deadlineAt?: string | null;
  emoji?: string;
  tags?: string[];
  imageAttachments?: NodeImageAttachment[];
  isPinned?: boolean;
}): Promise<ApiNode> {
  const payload = {
    title: data.title,
    categoryId: data.categoryId,
    notes: data.notes ?? null,
    priority: data.priority ?? 'medium',
    nodeType: data.nodeType || 'task',
    parentId: data.parentId ? parseInt(data.parentId) : null,
    deadlineAt: data.deadlineAt ?? null,
    emoji: data.emoji ?? null,
    tags: data.tags ?? [],
    imageAttachments: data.imageAttachments ?? [],
    isPinned: data.isPinned ?? false,
  };

  return apiRequest<ApiNode>(BASE, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** Update node status or title. */
export async function updateNode(
  id: string,
  updates: {
    title?: string;
    notes?: string;
    status?: NodeStatus;
    priority?: NodePriority;
    deadlineAt?: string | null;
    timeEstimateMinutes?: number | null;
    emoji?: string;
    tags?: string[];
    imageAttachments?: NodeImageAttachment[];
    isPinned?: boolean;
    isBacklog?: boolean;
    nodeType?: NodeType;
  }
): Promise<ApiNode> {
  return apiRequest<ApiNode>(`${BASE}/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
}

export async function moveNode(
  id: string,
  move: {
    parentId?: string | null;
    targetIndex: number;
    isBacklog?: boolean;
  }
): Promise<ApiNode> {
  return apiRequest<ApiNode>(`${BASE}/${id}/move`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      parentId: move.parentId ? parseInt(move.parentId, 10) : null,
      targetIndex: move.targetIndex,
      isBacklog: move.isBacklog,
    }),
  });
}

/** Delete a node. */
export async function deleteNode(id: string): Promise<void> {
  return apiRequest<void>(`${BASE}/${id}`, { method: 'DELETE' });
}
