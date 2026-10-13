import type { StrategyNode } from '../types/node';
import { isArchivedNode } from './nodeState';

export function localDayKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

export function localDayDate(day: string): Date {
  const [year, month, date] = day.split('-').map(Number);
  return new Date(year, month - 1, date);
}

export function shiftDay(day: string, offset: number): string {
  const date = localDayDate(day);
  date.setDate(date.getDate() + offset);
  return localDayKey(date);
}

export function mondayOf(day: string): string {
  const date = localDayDate(day);
  return shiftDay(day, -((date.getDay() + 6) % 7));
}

// Undated descendants stay inside their parent's card. A child explicitly
// planned for another day also gets its own card on that day.
export function planningCardsByDay(roots: StrategyNode[], scheduledDays: ReadonlyMap<string, string[]> = new Map()): Map<string, StrategyNode[]> {
  const groups = new Map<string, StrategyNode[]>();
  const seen = new Set<string>();
  const visit = (node: StrategyNode, ancestorDays: Set<string>) => {
    if (seen.has(node.id) || isArchivedNode(node)) return;
    seen.add(node.id);
    const nodeDays = [...new Set([...(node.plannedDate ? [node.plannedDate] : []), ...(scheduledDays.get(node.id) ?? [])])];
    const day = node.plannedDate ?? nodeDays[0] ?? '';
    (nodeDays.length ? nodeDays : ['']).forEach(cardDay => {
      if (!ancestorDays.has(cardDay) && (cardDay || ancestorDays.size === 0)) {
        groups.set(cardDay, [...(groups.get(cardDay) ?? []), node]);
      }
    });
    const nextDays = new Set(ancestorDays);
    nextDays.add(day);
    nodeDays.forEach(nodeDay => nextDays.add(nodeDay));
    node.children.forEach(child => visit(child, nextDays));
  };
  roots.forEach(node => visit(node, new Set()));
  return groups;
}

export function cardSubtasks(node: StrategyNode, day?: string, scheduledDays: ReadonlyMap<string, string[]> = new Map()) {
  type Row = { node: StrategyNode; parentTitle: string | null; deferredDate: string | null };
  const visit = (parent: StrategyNode, inheritedDays: string[], parentTitle: string | null): Row[] =>
    parent.children.filter(child => !isArchivedNode(child)).flatMap(child => {
      const ownDays = [...new Set([...(child.plannedDate ? [child.plannedDate] : []), ...(scheduledDays.get(child.id) ?? [])])].sort();
      const childDays = ownDays.length ? ownDays : inheritedDays;
      const deferredDate = day && childDays.length && !childDays.includes(day)
        ? childDays.find(date => date > day) ?? childDays.at(-1)! : null;
      return [{ node: child, parentTitle, deferredDate }, ...visit(child, childDays, child.title)];
    });
  return visit(node, day ? [day] : [], null);
}

export function planningDueIds(node: StrategyNode, day: string, scheduledDays: ReadonlyMap<string, string[]>): string[] {
  return [node.id, ...cardSubtasks(node, day, scheduledDays).filter(row => !row.deferredDate).map(row => row.node.id)];
}

export function planningCardComplete(node: StrategyNode, day: string, scheduledDays: ReadonlyMap<string, string[]>): boolean {
  const rows = cardSubtasks(node, day, scheduledDays);
  if (!rows.length) return node.status === 'done';
  // Containers have no separate work status. Only actual leaves due on this day
  // contribute; a container with entirely deferred work has nothing left today.
  return rows.filter(row => !row.deferredDate && !row.node.children.some(child => !isArchivedNode(child)))
    .every(row => row.node.status === 'done');
}

export function planningHasDueWork(node: StrategyNode, day: string, scheduledDays: ReadonlyMap<string, string[]>): boolean {
  const rows = cardSubtasks(node, day, scheduledDays);
  return !rows.length || rows.some(row => !row.deferredDate && !row.node.children.some(child => !isArchivedNode(child)));
}

export function planningEstimate(node: StrategyNode, day?: string, scheduledDays: ReadonlyMap<string, string[]> = new Map()): number {
  if (day) return (node.timeEstimateMinutes ?? 0) + cardSubtasks(node, day, scheduledDays)
    .filter(row => !row.deferredDate).reduce((sum, row) => sum + (row.node.timeEstimateMinutes ?? 0), 0);
  return (node.timeEstimateMinutes ?? 0)
    + node.children.filter(child => !isArchivedNode(child)).reduce((sum, child) => sum + planningEstimate(child), 0);
}

export function planningDuration(minutes: number): string {
  if (!minutes) return 'No estimate';
  return [minutes >= 60 ? `${Math.floor(minutes / 60)}h` : '', minutes % 60 ? `${minutes % 60}m` : ''].filter(Boolean).join(' ');
}
