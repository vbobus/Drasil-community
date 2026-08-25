import { test as base } from '@playwright/test';
import type { ApiNode } from '../../../src/api/nodes';
import type { Category } from '../../../src/types/category';
import { mockWorkspace as registerWorkspaceRoutes, waitForWorkspaceBoot } from '../helpers/network';
import { CategoryFactory } from './factories/categoryFactory';
import { NodeFactory } from './factories/nodeFactory';

export interface MockWorkspaceOptions {
  categories?: Category[];
  nodesByCategoryId?: Record<number, ApiNode[]>;
}

export interface MockWorkspaceResult {
  categories: Category[];
  nodesByCategoryId: Record<number, ApiNode[]>;
  waitForBoot: (categoryId?: number) => Promise<unknown[]>;
}

interface AppFixtures {
  categoryFactory: CategoryFactory;
  nodeFactory: NodeFactory;
  mockWorkspace: (options?: MockWorkspaceOptions) => Promise<MockWorkspaceResult>;
}

function buildDefaultNodesByCategoryId(
  categories: Category[],
  nodeFactory: NodeFactory,
): Record<number, ApiNode[]> {
  return Object.fromEntries(
    categories.map((category, index) => [
      category.id,
      [
        nodeFactory.build({
          categoryId: category.id,
          title: index === 0 ? 'Review weekly priorities' : `Plan ${category.name}`,
          sortOrder: 0,
        }),
      ],
    ]),
  );
}

export const test = base.extend<AppFixtures>({
  categoryFactory: async ({ page: _page }, fixtureUse) => {
    void _page;
    const categoryFactory = new CategoryFactory();
    await fixtureUse(categoryFactory);
    categoryFactory.cleanup();
  },
  nodeFactory: async ({ page: _page }, fixtureUse) => {
    void _page;
    const nodeFactory = new NodeFactory();
    await fixtureUse(nodeFactory);
    nodeFactory.cleanup();
  },
  mockWorkspace: async ({ page, categoryFactory, nodeFactory }, fixtureUse) => {
    await fixtureUse(async (options = {}) => {
      const categories =
        options.categories && options.categories.length > 0
          ? options.categories
          : [
              categoryFactory.build({
                name: 'Work',
                accentColor: '#1f6feb',
                iconSlug: 'briefcase',
                sortOrder: 0,
              }),
            ];
      const nodesByCategoryId =
        options.nodesByCategoryId ?? buildDefaultNodesByCategoryId(categories, nodeFactory);

      await registerWorkspaceRoutes(page, {
        categories,
        nodesByCategoryId,
      });

      return {
        categories,
        nodesByCategoryId,
        waitForBoot: (categoryId = categories[0]?.id ?? 0) =>
          waitForWorkspaceBoot(page, categoryId),
      };
    });
  },
});
