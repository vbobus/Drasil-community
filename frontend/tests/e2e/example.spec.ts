import { expect, test } from '../support/fixtures';

test('loads a workspace tree from deterministic route stubs', async ({
  page,
  categoryFactory,
  nodeFactory,
  mockWorkspace,
}) => {
  const category = categoryFactory.build({
    name: 'Resume polish',
    accentColor: '#1f6feb',
    iconSlug: 'briefcase',
    sortOrder: 0,
  });
  const rootTask = nodeFactory.build({
    categoryId: category.id,
    title: 'Revise the highest-impact parts of Resume',
    sortOrder: 0,
  });
  const childTask = nodeFactory.build({
    categoryId: category.id,
    parentId: rootTask.id,
    title: 'Update job history bullets',
    sortOrder: 0,
  });

  const workspace = await mockWorkspace({
    categories: [category],
    nodesByCategoryId: {
      [category.id]: [rootTask, childTask],
    },
  });

  // Given a workspace boot sequence with deterministic network fixtures.
  const workspaceBoot = workspace.waitForBoot(category.id);

  // When the user opens the app shell.
  await page.goto('/');
  await workspaceBoot;

  // Then the workspace chrome and seeded task tree render from the mock graph.
  await expect(page.getByTestId('app-shell')).toBeVisible();
  await expect(page.getByLabel('Search tasks in this workspace')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Add task', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /resume polish workspace \(active\)/i })).toBeVisible();
  await expect(page.getByRole('treeitem', { name: new RegExp(rootTask.title) })).toBeVisible();
  await expect(page.getByRole('treeitem', { name: new RegExp(childTask.title) })).toBeVisible();
});
