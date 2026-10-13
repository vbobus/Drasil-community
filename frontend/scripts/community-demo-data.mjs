// Fictional showcase data. Never import an account, environment file, or database.
export const demoDay = '2026-10-12';
export const categories = [
  { id: 1, name: 'Work', accentColor: '#80602f', iconSlug: 'briefcase', sortOrder: 0 },
  { id: 2, name: 'Personal', accentColor: '#4f6a50', iconSlug: 'heart-pulse', sortOrder: 1 },
  { id: 3, name: 'Learning', accentColor: '#6c6280', iconSlug: 'book-open', sortOrder: 2 },
];
const task = (id, title, categoryId, plannedDate, minutes, extra = {}) => ({
  id, title, categoryId, plannedDate, timeEstimateMinutes: minutes,
  notes: null, status: 'todo', priority: 'medium', nodeType: 'task', parentId: null,
  deadlineAt: null, emoji: null, tags: [], imageAttachments: [], isPinned: false,
  isRecurring: false, recurrenceRule: null, sortOrder: id, isBacklog: false, ...extra,
});
export function createDemoState() {
  const nodes = [
    task(101, 'Publish the field guide', 1, demoDay, 120, { priority: 'high' }),
    task(102, 'Draft the introduction', 1, demoDay, 60, { parentId: 101, priority: 'high' }),
    task(103, 'Review illustrations', 1, demoDay, 45, { parentId: 101 }),
    task(104, 'Prepare release notes', 1, demoDay, 30),
    task(105, 'Test mobile navigation', 1, '2026-10-13', 60),
    task(106, 'Publish the preview', 1, '2026-10-14', 30),
    task(107, 'Collect reader feedback', 1, '2026-10-15', 45),
    task(108, 'Plan the next edition', 1, '2026-10-16', 45),
    task(201, 'Take a walk', 2, demoDay, 20),
    task(202, 'Plan dinners for the week', 2, '2026-10-13', 30),
    task(203, 'Water the plants', 2, '2026-10-14', 10),
    task(301, 'Practice a new sketch technique', 3, demoDay, 30),
    task(302, 'Read the accessibility guide', 3, '2026-10-14', 45),
  ];
  const block = (id, taskId, start, end) => {
    const node = nodes.find(item => item.id === taskId);
    const category = categories.find(item => item.id === node.categoryId);
    return { id, taskId, categoryId: category.id, title: node.title, emoji: null,
      categoryName: category.name, categoryColor: category.accentColor,
      startAt: `${demoDay}T${start}:00`, endAt: `${demoDay}T${end}:00`,
      variant: 'scheduled', completionStatus: 'not_started', isPinned: false,
      isProtected: false, isRecurring: false, hierarchyDepth: 0, renderedChildTaskIds: [] };
  };
  const blocks = [block(901, 102, '09:00', '10:00'), block(902, 201, '12:00', '12:20'), block(903, 301, '16:00', '16:30')];
  const habits = {
    today: demoDay, totalXp: 230, level: 3, levelXp: 30, totalCheckIns: 23, bestStreak: 7,
    badges: [
      { id: 'first', title: 'First check-in', description: '1 check-in', earned: true },
      { id: 'ten', title: '10 check-ins', description: '10 check-ins', earned: true },
      { id: 'streak', title: '7-check-in streak', description: '7 consecutive scheduled check-ins', earned: true },
      { id: 'fifty', title: '50 check-ins', description: '50 check-ins', earned: false },
    ],
    habits: [
      ['Read 10 pages', 'After breakfast', 'book', 7],
      ['Walk for 15 minutes', 'After lunch', 'activity', 5],
      ['Practice a sketch', 'Before closing my notebook', 'sprout', 3],
    ].map(([title, cue, icon, streak], index) => ({
      id: `demo-habit-${index}`, title, cue, icon, weekdays: [0, 1, 2, 3, 4],
      createdOn: '2026-09-21', archived: false, currentStreak: streak, bestStreak: streak,
      totalCheckIns: [10, 8, 5][index],
      week: Array.from({ length: 7 }, (_, day) => ({
        date: `2026-10-${String(day + 6).padStart(2, '0')}`,
        scheduled: day < 4 || day === 6, completed: day < 4,
      })),
    })),
  };
  return { nodes, blocks, habits, approved: false };
}

export function reviewResult(approved = false) {
  const version = { contractVersion: '2.0', revision: 1 };
  const binding = { ...version, sessionId: 'demo-chat', goalId: 'goal:demo', goalRevision: 1,
    changeSetId: 'changeset:demo', changeSetRevision: 1, simulationId: 'simulation:demo',
    simulationInputFingerprint: 'a'.repeat(64), simulationOutputFingerprint: 'b'.repeat(64),
    correlationId: 'correlation:demo', causationId: 'causation:demo' };
  const reviewId = 'review:demo';
  const entity = { ...version, ref: { contractVersion: '2.0', kind: 'task', entityId: 'task:104', revision: 1 },
    label: 'Prepare release notes', context: 'Work' };
  const document = { ...version, ...binding, reviewId,
    summary: 'Schedule release notes after the introduction.', decisions: [],
    blocks: [{ ...version, kind: 'semantic_diff', blockId: 'block:demo', title: 'Schedule change',
      changes: [{ ...version, changeId: 'operation:demo', summary: 'Reserve 30 minutes for release notes',
        entities: [entity], before: 'Monday · planned, without a time', after: 'Monday · 10:30–11:00 AM' }] }],
    controls: ['approve', 'edit', 'reject'].map(action => ({ ...version, action,
      reviewId, reviewRevision: 1, binding, eventId: `event:demo:${action}`,
      idempotencyKey: `idem:demo:${action}`, ...(action === 'approve' ? { commandId: 'command:demo' } : {}) })),
  };
  return {
    session: { id: 'demo-chat', title: 'Plan the field guide', providerLabel: 'Demo response',
      modelName: 'Mock response', createdAt: `${demoDay}T09:15:00Z`, updatedAt: `${demoDay}T09:15:00Z` },
    turn: { id: approved ? 'demo-approved' : 'demo-review', prompt: 'Schedule release notes after the introduction.',
      createdAt: `${demoDay}T09:15:00Z`, response: { providerLabel: 'Demo response', mode: 'review',
        summary: approved ? 'Release notes scheduled for 10:30–11:00 AM.' : 'Review the proposed time before applying it.',
        contextCards: [], suggestedPrompts: [], ...(approved ? {} : { reviewDocument: document }) } },
  };
}
