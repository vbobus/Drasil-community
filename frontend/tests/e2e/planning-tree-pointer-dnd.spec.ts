import { expect, test } from '../support/fixtures';

test.describe('Planning tree pointer drag and virtualization', () => {
  test('right-click deletes a task and its full subtree after confirmation', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const category = categoryFactory.build({
      name: 'Work',
      accentColor: '#1f6feb',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const parent = nodeFactory.build({
      id: 3001,
      categoryId: category.id,
      title: 'Delete project subtree',
      sortOrder: 0,
    });
    const child = nodeFactory.build({
      id: 3002,
      categoryId: category.id,
      parentId: parent.id,
      title: 'Delete child task',
      sortOrder: 0,
    });
    const grandchild = nodeFactory.build({
      id: 3003,
      categoryId: category.id,
      parentId: child.id,
      title: 'Delete grandchild task',
      sortOrder: 0,
    });
    const sibling = nodeFactory.build({
      id: 3004,
      categoryId: category.id,
      title: 'Keep sibling task',
      sortOrder: 1,
    });
    let deletedNodeId: number | null = null;
    await page.route(`**/api/v1/nodes/${parent.id}`, async (route) => {
      if (route.request().method() !== 'DELETE') {
        await route.fallback();
        return;
      }
      deletedNodeId = parent.id;
      await route.fulfill({ status: 204 });
    });
    const workspace = await mockWorkspace({
      categories: [category],
      nodesByCategoryId: {
        [category.id]: [parent, child, grandchild, sibling],
      },
    });

    const workspaceBoot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await workspaceBoot;

    const tree = page.getByRole('tree', { name: /work task nodes/i });
    const parentCard = tree.locator('.node-card-wrapper[data-depth="0"]').filter({
      has: page.getByText(parent.title, { exact: true }),
    });
    await parentCard.locator('.node-card-row').click({ button: 'right' });
    const deleteMenuItem = page.getByRole('menuitem', { name: 'Delete task and 2 subtasks' });
    await expect(deleteMenuItem.locator('.lucide-trash-2')).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Select for execution' })
      .locator('.lucide-list-checks')).toBeVisible();
    await deleteMenuItem.click();

    const dialog = page.getByRole('dialog', { name: `Delete “${parent.title}”?` });
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('.lucide-triangle-alert')).toBeVisible();
    await expect(dialog).toContainText(
      'This permanently deletes this task and all 2 of its subtasks and children.',
    );
    await dialog.getByRole('button', { name: 'Delete task and 2 subtasks' }).click();

    await expect(tree.getByText(parent.title, { exact: true })).toHaveCount(0);
    await expect(tree.getByText(child.title, { exact: true })).toHaveCount(0);
    await expect(tree.getByText(grandchild.title, { exact: true })).toHaveCount(0);
    await expect(tree.getByText(sibling.title, { exact: true })).toBeVisible();
    expect(deletedNodeId).toBe(parent.id);
  });

  test('drags a child task out to the root order with an immediate optimistic update', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const category = categoryFactory.build({
      name: 'Work',
      accentColor: '#1f6feb',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const rootTask = nodeFactory.build({
      id: 3101,
      categoryId: category.id,
      title: 'Launch plan',
      sortOrder: 0,
    });
    const childTask = nodeFactory.build({
      id: 3102,
      categoryId: category.id,
      parentId: rootTask.id,
      title: 'Draft implementation outline',
      sortOrder: 0,
    });
    const designTask = nodeFactory.build({
      id: 3103,
      categoryId: category.id,
      title: 'Design visuals',
      sortOrder: 1,
    });

    let movePayload: unknown = null;
    await page.route('**/api/v1/nodes/*/move', async (route) => {
      movePayload = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...childTask,
          parentId: null,
          sortOrder: 2,
        }),
      });
    });

    const workspace = await mockWorkspace({
      categories: [category],
      nodesByCategoryId: {
        [category.id]: [rootTask, childTask, designTask],
      },
    });

    const workspaceBoot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await workspaceBoot;

    const tree = page.getByRole('tree', { name: /work task nodes/i });
    const childRow = tree.locator('.node-card-wrapper').filter({
      hasText: 'Draft implementation outline',
    });
    const designRow = tree.locator('.node-card-wrapper').filter({ hasText: 'Design visuals' });

    await tree.locator('.node-card-wrapper').filter({ hasText: 'Launch plan' }).hover();
    await expect(
      tree.locator('.planning-tree-virtual-row.hover-descendant', {
        hasText: 'Draft implementation outline',
      }),
    ).toBeVisible();

    const sourceBox = await childRow.locator('.node-card-row').boundingBox();
    const targetBox = await designRow.locator('.node-card-row').boundingBox();
    expect(sourceBox).not.toBeNull();
    expect(targetBox).not.toBeNull();
    if (!sourceBox || !targetBox) return;
    await page.mouse.move(sourceBox.x + 8, sourceBox.y + sourceBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(targetBox.x + 8, targetBox.y + targetBox.height - 3, { steps: 12 });
    await page.mouse.up();

    await expect(
      tree.locator(
        '.node-card-wrapper[data-lane="active"][data-depth="0"] > .node-card > .node-card-row .node-title',
      ),
    ).toHaveText(['Launch plan', 'Design visuals', 'Draft implementation outline']);
    expect(movePayload).toEqual({
      parentId: null,
      targetIndex: 2,
      isBacklog: false,
    });
  });
});
