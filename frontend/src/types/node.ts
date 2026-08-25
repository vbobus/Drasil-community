// frontend/src/types/node.ts
// Core node types for the Drasil task tree.
// Used by NodeCard, future Zustand node store, and backend schemas.

export type NodeStatus = 'todo' | 'in-progress' | 'done';
export type NodeType = 'task' | 'habit';
export type NodePriority = 'low' | 'medium' | 'high';

export interface NodeImageAttachment {
  id: string;
  name: string;
  dataUrl: string;
}

export type RecurrenceFrequency = 'daily' | 'weekly';

export interface TaskRecurrenceRule {
  id: string;
  revision: number;
  frequency: RecurrenceFrequency;
  interval: number;
  daysOfWeek: string[];
  startDate: string;
  endDate?: string | null;
  preferredStartTime: string;
  durationMinutes: number;
  timezone?: string | null;
}

export interface NodeDragPayload {
  id: string;
  parentId: string | null;
  categoryId: number;
  sortOrder: number;
}

export interface StrategyNode {
  id: string;
  title: string;
  notes?: string | null;
  status: NodeStatus;
  priority: NodePriority;
  type: NodeType;
  categoryId: number;
  parentId?: string | null;
  deadlineAt?: string | null;
  timeEstimateMinutes?: number | null;
  emoji?: string | null;
  tags: string[];
  imageAttachments: NodeImageAttachment[];
  isPinned: boolean;
  isRecurring: boolean;
  recurrenceRule?: TaskRecurrenceRule | null;
  isBacklog: boolean;
  sortOrder?: number;
  children: StrategyNode[];
}
