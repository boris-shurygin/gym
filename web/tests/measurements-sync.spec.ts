import { expect, test, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { demoProgram } from '../src/demo-program';
import type { ContentV2 } from '../src/account';

const backend = 'http://127.0.0.1:4174';
const template = demoProgram.templates.find((item) => item.id === 'measurements')!;
const content: ContentV2 = {
  content_schema_version: 2,
  name: 'Синтетическая программа измерений',
  templates: [
    {
      template_id: template.id,
      name: template.name,
      description: template.description,
      exercises: template.exercises.map((exercise) => ({
        template_exercise_id: exercise.id,
        exercise_id: exercise.id,
        equipment_id: `${exercise.id}-equipment`,
        name: exercise.name,
        measurement: exercise.measurement!,
        sides: exercise.sides!,
        set_structure: exercise.setStructure!,
        rest_seconds: exercise.restSeconds ?? null,
        rule: 'fixed',
        load:
          exercise.load!.kind === 'external'
            ? {
                kind: 'external',
                weight_convention: exercise.load!.weightConvention,
                implement_count: exercise.load!.implementCount,
                step_g: Math.round(exercise.load!.stepKg * 1000),
              }
            : exercise.load!.kind === 'machine_scale'
              ? { kind: 'machine_scale', step_milli: Math.round(exercise.load!.step * 1000) }
              : { kind: 'bodyweight' },
        targets: exercise.targets.map((target) => ({
          target_id: target.id,
          weight_g: target.weightKg === null ? null : Math.round(target.weightKg * 1000),
          suggested_reps: target.reps,
          scale_milli: target.scaleValue == null ? null : Math.round(target.scaleValue * 1000),
          duration_seconds: target.durationSeconds ?? null,
          rep_min: null,
          rep_max: null,
        })),
      })),
    },
  ],
};

async function synced(page: Page) {
  await expect(page.getByRole('region', { name: 'Синхронизация' })).toContainText(
    '0 действий ожидают доставки',
    { timeout: 15000 },
  );
}

async function complete(page: Page) {
  await page.getByRole('button', { name: 'Завершить упражнение', exact: true }).click();
  page.once('dialog', (dialog) => void dialog.accept());
  await page.getByRole('button', { name: 'Завершить тренировку', exact: true }).click();
  await synced(page);
}

test('одобрение v2, offline-запись сторон и синхронизация сохраняют смешанную историю v1/v2', async ({
  page,
  context,
  request,
}) => {
  test.setTimeout(60000);
  const fixture = (await (await request.post(`${backend}/__test/fixture`)).json()).fixture;
  await context.addCookies([{ name: 'gym_e2e_fixture', value: fixture, url: 'http://127.0.0.1:4173' }]);
  const headers = { 'X-Test-Fixture': fixture, 'X-Gym-Schema-Version': '2' };
  await page.goto('/#/app');
  await page.locator('.template-card').first().click();
  await page.getByRole('button', { name: 'Начать тренировку', exact: true }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await page.getByRole('textbox', { name: 'Вес, кг' }).fill('61');
  await page.getByRole('textbox', { name: 'Повторения' }).fill('7');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await complete(page);
  const current = await (await request.get(`${backend}/api/v1/program`, { headers })).json();
  const session = await (await request.get(`${backend}/auth/session`, { headers })).json();
  const proposed = await request.post(`${backend}/api/v1/program/changes`, {
    headers: { ...headers, 'X-CSRF-Token': session.csrf_token, 'X-Gym-Expected-Owner': session.user_id },
    data: {
      request_id: randomUUID(),
      expected_owner_id: session.user_id,
      proposal: {
        base_program_version_id: current.version.program_version_id,
        content,
        rationale: 'Синтетическая проверка единиц, сторон и истории',
      },
    },
  });
  expect(proposed.ok()).toBe(true);
  const change = await proposed.json();
  await page.goto(`/#/program?change_id=${change.change_id}`);
  const review = page.locator('.program-review');
  await expect(review).toContainText('Свой вес');
  await expect(review).toContainText('на каждую сторону');
  await expect(review).toContainText('0:25');
  await expect(review).toContainText('Шкала 7 × 12');
  await expect(review).toContainText('одновременно 2');
  await expect(review).toContainText('активация и короткие подходы');
  await expect(review).not.toContainText('null');
  await review.getByRole('button', { name: 'Одобрить и опубликовать', exact: true }).click();
  await expect(review).not.toBeVisible();
  await page.goto('/#/app');
  await page.getByRole('button', { name: /Новые виды записи/ }).click();
  await page.getByRole('button', { name: 'Начать тренировку', exact: true }).click();
  await page.getByRole('button', { name: /Боковая планка/ }).click();
  await synced(page);
  await expect(page.getByText('Можно открыть без сети', { exact: true })).toBeVisible();
  await context.setOffline(true);
  await page.getByRole('textbox', { name: 'Время, мм:сс' }).fill('0:31');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
    '0:31',
  );
  await expect(page.getByRole('button', { name: 'Правая', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await page.getByRole('textbox', { name: 'Время, мм:сс' }).fill('0:27');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  // click() does not await the async handler; App renders this record only after
  // the IndexedDB transaction commits. Wait for that acknowledgement before reload.
  await expect(page.getByRole('button', { name: 'Редактировать подход 2', exact: true })).toContainText(
    '0:27',
  );
  await page.reload();
  await page.getByRole('button', { name: 'Продолжить тренировку' }).click();
  await page.getByRole('button', { name: /Боковая планка/ }).click();
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
    '0:31',
  );
  await expect(page.getByRole('button', { name: 'Редактировать подход 2', exact: true })).toContainText(
    '0:27',
  );
  await context.setOffline(false);
  await complete(page);
  const history = await (await request.get(`${backend}/api/v1/workouts`, { headers })).json();
  expect(history.items).toHaveLength(2);
  const legacy = history.items.find(
    (item: { plan: { snapshot_schema_version: number } }) => item.plan.snapshot_schema_version === 1,
  );
  const measured = history.items.find(
    (item: { plan: { snapshot_schema_version: number } }) => item.plan.snapshot_schema_version === 2,
  );
  expect(legacy.sets[0]).toMatchObject({ weight_g: 61000, reps: 7 });
  expect(measured.sets).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ side: 'left', duration_seconds: 31, weight_g: null, reps: null }),
      expect.objectContaining({ side: 'right', duration_seconds: 27, weight_g: null, reps: null }),
    ]),
  );
  await page.getByRole('button', { name: 'Серверная история и графики', exact: true }).click();
  const rows = page.locator('.server-workout');
  await expect(rows).toHaveCount(2);
  await rows.filter({ hasText: 'Новые виды записи' }).locator('summary').click();
  await expect(rows.filter({ hasText: 'Новые виды записи' })).toContainText('Рабочий · левая: 0:31');
  await expect(rows.filter({ hasText: 'Новые виды записи' })).toContainText('Рабочий · правая: 0:27');
  const old = rows.filter({ hasNotText: 'Новые виды записи' });
  await old.locator('summary').click();
  await expect(old).toContainText('61 кг × 7');
});
