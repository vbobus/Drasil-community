import { expect, test } from '../support/fixtures/strategist.fixture';

test('remembered preferences can be disabled, restored, and deleted', async ({
  page,
  mockWorkspace,
}) => {
  let active = true;
  let deleted = false;
  const preference = {
    id: 'isolated-gap-preference',
    revision: 1,
    domain: 'scheduling',
    preferenceType: 'inter_task_gap',
    scopeType: 'global',
    scopeId: null,
    strength: 'hard',
    source: 'approved_memory_update',
    confidence: 1,
    active,
    description: 'Leave 15 minutes between tasks lasting at least 45 minutes.',
    value: {
      interTaskGapMinutes: 15,
      minimumTaskDurationMinutes: 45,
      shorterTaskGapMinutes: 0,
    },
    constraints: [],
    createdAt: '2026-07-30T17:04:00Z',
    updatedAt: '2026-07-30T17:04:00Z',
  };

  await page.route('**/api/v1/preferences/remembered?includeInactive=true', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        preferences: deleted ? [] : [{ ...preference, active }],
        summary: {
          activeCount: deleted ? 0 : active ? 1 : 0,
          inactiveCount: deleted ? 0 : active ? 0 : 1,
          hardConstraintCount: 0,
          issueCount: 0,
        },
      }),
    });
  });
  await page.route('**/api/v1/preferences/remembered/isolated-gap-preference', async (route) => {
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON() as { active: boolean };
      active = body.active;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...preference, active }),
      });
      return;
    }
    if (route.request().method() === 'DELETE') {
      deleted = true;
      await route.fulfill({ status: 204, body: '' });
      return;
    }
    await route.fallback();
  });

  const workspace = await mockWorkspace();
  const boot = workspace.waitForBoot();
  await page.goto('/');
  await boot;
  await page.getByRole('button', { name: 'Settings' }).click();
  await page.getByRole('button', { name: 'Memory', exact: true }).click();

  const card = page
    .locator('.settings-memory-item')
    .filter({ hasText: 'Leave 15 minutes between tasks lasting at least 45 minutes.' });
  await card.getByRole('button', { name: 'Disable' }).click();
  await expect(card.getByRole('button', { name: 'Restore' })).toBeVisible();
  await card.getByRole('button', { name: 'Restore' }).click();
  await expect(card.getByRole('button', { name: 'Disable' })).toBeVisible();

  page.once('dialog', (dialog) => dialog.accept());
  await card.getByRole('button', { name: 'Delete' }).click();
  await expect(card).toHaveCount(0);
});

test('memory center creates and edits a typed preference', async ({
  page,
  mockWorkspace,
}) => {
  let savedPreference: Record<string, unknown> | null = null;

  await page.route('**/api/v1/preferences/remembered?includeInactive=true', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        preferences: savedPreference ? [savedPreference] : [],
        summary: {
          activeCount: savedPreference ? 1 : 0,
          inactiveCount: 0,
          hardConstraintCount: 0,
          issueCount: 0,
        },
      }),
    });
  });
  await page.route('**/api/v1/preferences/remembered', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback();
      return;
    }
    const body = route.request().postDataJSON() as Record<string, unknown>;
    savedPreference = {
      id: 'created-preference',
      revision: 1,
      source: 'user_settings',
      confidence: 1,
      active: true,
      constraints: [],
      createdAt: '2026-08-11T10:00:00Z',
      updatedAt: '2026-08-11T10:00:00Z',
      issue: null,
      ...body,
    };
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify(savedPreference),
    });
  });
  await page.route('**/api/v1/preferences/remembered/created-preference', async (route) => {
    if (route.request().method() !== 'PATCH') {
      await route.fallback();
      return;
    }
    const body = route.request().postDataJSON() as Record<string, unknown>;
    savedPreference = {
      ...(savedPreference ?? {}),
      ...body,
      revision: 2,
      updatedAt: '2026-08-11T10:05:00Z',
    };
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(savedPreference),
    });
  });

  const workspace = await mockWorkspace();
  const boot = workspace.waitForBoot();
  await page.goto('/');
  await boot;
  await page.getByRole('button', { name: 'Settings' }).click();
  await page.getByRole('button', { name: 'Memory', exact: true }).click();

  await page.getByTitle('Add preference').click();
  await page.getByRole('textbox', { name: 'Preference', exact: true }).fill('Use a 17-minute gap between tasks.');
  await page.getByLabel('Rule type').selectOption('gap');
  await page.getByLabel('Gap minutes').fill('17');
  await page.locator('.settings-memory-editor').getByRole('button', { name: 'Add preference' }).click();
  await expect(page.getByText('17-minute gap between tasks', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: 'Edit Use a 17-minute gap between tasks.' }).click();
  await page.getByRole('textbox', { name: 'Preference', exact: true }).fill('Use a 19-minute gap between tasks.');
  await page.getByLabel('Gap minutes').fill('19');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByText('19-minute gap between tasks', { exact: true })).toBeVisible();
});
