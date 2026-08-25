import { expect, test } from '../support/fixtures/strategist.fixture';

test.describe('Workspace sidebar', () => {
  test('renames the selected workspace from the tree header menu', async ({
    page,
    categoryFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 701,
      name: 'Work',
      accentColor: '#516ff2',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [] },
    });
    let renamePayload: unknown = null;
    await page.route(`**/api/v1/categories/${work.id}`, async (route) => {
      if (route.request().method() !== 'PATCH') {
        await route.continue();
        return;
      }
      renamePayload = route.request().postDataJSON();
      await route.fulfill({ json: { ...work, name: 'Work Ops' } });
    });

    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByLabel('Workspace options for Work').click();
    await page.getByRole('menuitem', { name: 'Rename workspace' }).click();
    const dialog = page.getByRole('dialog', { name: 'Rename “Work”' });
    await dialog.getByLabel('Workspace name').fill('Work Ops');
    await dialog.getByRole('button', { name: 'Save name' }).click();

    await expect.poll(() => renamePayload).toEqual({ name: 'Work Ops' });
    await expect(page.getByRole('heading', { name: 'Work Ops tree' })).toBeVisible();
    await expect(
      page.getByRole('button', { name: 'Work Ops workspace (active)', exact: true }),
    ).toBeVisible();
    await expect(dialog).toHaveCount(0);
  });

  test('keeps titles readable and exposes destructive options from the tree header', async ({
    page,
    categoryFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 701,
      name: 'Work',
      accentColor: '#516ff2',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const unexpected = categoryFactory.build({
      id: 702,
      name: 'Drasil',
      accentColor: '#8b5cf6',
      iconSlug: 'wrench',
      sortOrder: 1,
    });
    const workspace = await mockWorkspace({
      categories: [work, unexpected],
      nodesByCategoryId: { [work.id]: [], [unexpected.id]: [] },
    });
    let deleteRequests = 0;
    await page.route(`**/api/v1/categories/${unexpected.id}`, async (route) => {
      if (route.request().method() !== 'DELETE') {
        await route.continue();
        return;
      }
      deleteRequests += 1;
      await route.fulfill({ status: 204 });
    });

    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    const workspaceSection = page.locator('.sidebar-section').first();
    const drasilSidebarButton = workspaceSection.getByRole('button', {
      name: 'Drasil workspace',
      exact: true,
    });
    await expect(drasilSidebarButton).toBeVisible();
    const titleFits = await drasilSidebarButton.locator('.sidebar-item-label').evaluate(
      (label) => label.scrollWidth <= label.clientWidth,
    );
    expect(titleFits).toBe(true);
    await expect(workspaceSection.locator('.sidebar-workspace-menu-trigger')).toHaveCount(0);

    await drasilSidebarButton.click();
    await expect(page.getByRole('heading', { name: 'Drasil tree' })).toBeVisible();
    await expect(page.getByLabel('Workspace options for Drasil')).toBeVisible();
    await expect(page.getByLabel('Workspace options for All')).toHaveCount(0);
    await page.getByLabel('Workspace options for Drasil').click();
    await expect(page.getByRole('menuitem', { name: 'Rename workspace' })).toBeVisible();
    await page.getByRole('menuitem', { name: 'Delete workspace' }).click();

    const firstConfirmation = page.getByRole('dialog', { name: 'Delete “Drasil”?' });
    await expect(firstConfirmation).toBeVisible();
    expect(deleteRequests).toBe(0);
    await firstConfirmation
      .getByRole('button', { name: 'Continue to final confirmation' })
      .click();

    const finalConfirmation = page.getByRole('dialog', {
      name: 'Type “Drasil” to confirm',
    });
    const permanentDelete = finalConfirmation.getByRole('button', {
      name: 'Delete workspace permanently',
    });
    await expect(permanentDelete).toBeDisabled();
    await finalConfirmation.getByLabel('Type Drasil to confirm deletion').fill('drasil');
    await expect(permanentDelete).toBeDisabled();
    await finalConfirmation.getByLabel('Type Drasil to confirm deletion').fill('Drasil');
    await expect(permanentDelete).toBeEnabled();
    expect(deleteRequests).toBe(0);

    await permanentDelete.click();

    await expect.poll(() => deleteRequests).toBe(1);
    await expect(page.getByLabel('Workspace options for Drasil')).toHaveCount(0);
    await expect(drasilSidebarButton).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Work tree' })).toBeVisible();
    await expect(finalConfirmation).toHaveCount(0);
  });

  test('keeps every workspace creation option visible outside the narrow sidebar', async ({
    page,
    categoryFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 701,
      name: 'Work',
      accentColor: '#516ff2',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [] },
    });

    await page.setViewportSize({ width: 1280, height: 860 });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: 'Add workspace' }).click();

    const editor = page.locator('.sidebar-add-editor');
    await expect(editor).toBeVisible();
    await expect(page.getByLabel('New workspace name')).toBeVisible();
    await expect(page.getByLabel('Search workspace icons')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Cancel' })).toBeVisible();

    const geometry = await editor.evaluate((element) => {
      const editorBounds = element.getBoundingClientRect();
      const sidebar = element.closest('.sidebar');
      const sidebarBounds = sidebar?.getBoundingClientRect();
      const probe = element.ownerDocument.elementFromPoint(
        editorBounds.right - 6,
        editorBounds.top + 24,
      );
      return {
        editorWidth: editorBounds.width,
        editorRight: editorBounds.right,
        sidebarWidth: sidebarBounds?.width ?? 0,
        sidebarOverflow: sidebar
          ? element.ownerDocument.defaultView?.getComputedStyle(sidebar).overflow
          : null,
        hitInsideEditor: probe ? element.contains(probe) : false,
        viewportWidth: element.ownerDocument.documentElement.clientWidth,
      };
    });

    expect(geometry.editorWidth).toBeGreaterThan(geometry.sidebarWidth);
    expect(geometry.editorRight).toBeLessThanOrEqual(geometry.viewportWidth);
    expect(geometry.sidebarOverflow).toBe('visible');
    expect(geometry.hitInsideEditor).toBe(true);

    const finalIcon = page.getByRole('option').last();
    await finalIcon.scrollIntoViewIfNeeded();
    await expect(finalIcon).toBeVisible();
    await finalIcon.click();
    await expect(finalIcon).toHaveAttribute('aria-selected', 'true');
  });
});
