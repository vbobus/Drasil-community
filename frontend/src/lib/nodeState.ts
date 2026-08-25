import type { StrategyNode } from '../types/node';

export function isArchivedNode(node: StrategyNode): boolean {
  return node.tags.some((tag) => tag.trim().toLowerCase() === 'state:archived');
}

export function isActiveTreeNode(node: StrategyNode): boolean {
  return !node.isBacklog && !isArchivedNode(node);
}

export function isBacklogTreeNode(node: StrategyNode): boolean {
  return node.isBacklog && !isArchivedNode(node);
}

export function visibleChildrenForLane(
  node: StrategyNode,
  lane: 'active' | 'backlog' | 'archived',
): StrategyNode[] {
  if (lane === 'archived') {
    return node.children.filter(isArchivedNode);
  }
  if (lane === 'backlog') {
    return node.children.filter(isBacklogTreeNode);
  }
  return node.children.filter(isActiveTreeNode);
}

export function countVisibleTreeNodes(
  nodes: StrategyNode[],
  lane: 'active' | 'backlog' | 'archived',
): number {
  return nodes.reduce(
    (count, node) => count + 1 + countVisibleTreeNodes(visibleChildrenForLane(node, lane), lane),
    0,
  );
}
