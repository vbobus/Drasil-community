import { faker } from '@faker-js/faker';
import type { ApiNode } from '../../../../src/api/nodes';

const EMOJIS = ['📝', '🧠', '🔎', '📅', '🔥'];

export class NodeFactory {
  private nextId: number;
  private readonly createdIds = new Set<number>();

  constructor(seed = 20260510) {
    faker.seed(seed);
    this.nextId = 1000;
  }

  build(overrides: Partial<ApiNode> & Pick<ApiNode, 'categoryId'>): ApiNode {
    const id = overrides.id ?? this.nextId++;

    this.createdIds.add(id);

    return {
      id,
      title: overrides.title ?? faker.helpers.arrayElement([
        'Review weekly priorities',
        'Revise the highest-impact parts of Resume',
        'Update job history bullets',
        'Audit portfolio screenshots',
      ]),
      notes: overrides.notes ?? null,
      status: overrides.status ?? 'todo',
      priority: overrides.priority ?? 'medium',
      nodeType: overrides.nodeType ?? 'task',
      categoryId: overrides.categoryId,
      parentId: overrides.parentId ?? null,
      plannedDate: overrides.plannedDate ?? null,
      deadlineAt: overrides.deadlineAt ?? null,
      timeEstimateMinutes: overrides.timeEstimateMinutes ?? null,
      emoji: overrides.emoji ?? faker.helpers.arrayElement(EMOJIS),
      tags: overrides.tags ?? [],
      imageAttachments: overrides.imageAttachments ?? [],
      isPinned: overrides.isPinned ?? false,
      isRecurring: overrides.isRecurring ?? false,
      recurrenceRule: overrides.recurrenceRule ?? null,
      sortOrder: overrides.sortOrder ?? this.createdIds.size - 1,
      isBacklog: overrides.isBacklog ?? false,
    };
  }

  cleanup() {
    this.createdIds.clear();
    this.nextId = 1000;
  }
}
