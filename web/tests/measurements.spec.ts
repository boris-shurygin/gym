import { expect, test, type Page } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('gym.mode', 'demo'));
});

async function start(page: Page, exercise: string) {
  await page.goto('/#/app');
  await page.getByRole('button', { name: /Новые виды записи/ }).click();
  await expect(page.locator('.preview-row').filter({ hasText: 'Боковая планка' })).toContainText('0:25');
  await expect(page.locator('.preview-row').filter({ hasText: 'Тренажёр со шкалой' })).not.toContainText(
    'кг',
  );
  await page.getByRole('button', { name: 'Начать тренировку', exact: true }).click();
  await openExercise(page, exercise);
}

async function openExercise(page: Page, name: string) {
  await page.getByRole('button', { name: new RegExp(name) }).click();
}

const save = (page: Page) => page.getByRole('button', { name: 'Записать подход', exact: true }).click();
const record = (page: Page, index: number) =>
  page.getByRole('button', { name: `Редактировать подход ${index}`, exact: true });

test('стороны сохраняются независимо; разминка, черновики, исправление и offline не создают вторую сторону', async ({
  page,
  context,
}) => {
  await start(page, 'Зашагивание');
  const left = page.getByRole('button', { name: 'Левая', exact: true });
  const right = page.getByRole('button', { name: 'Правая', exact: true });
  const reps = page.getByRole('textbox', { name: 'Повторения', exact: true });
  await expect(left).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('textbox', { name: /кг/ })).toHaveCount(0);
  await reps.fill('7');
  await right.click();
  await reps.fill('6');
  await left.click();
  await expect(reps).toHaveValue('7');
  await save(page);
  await expect(record(page, 1)).toContainText('Левая');
  await expect(record(page, 1)).toContainText('7 повт.');
  await expect(right).toHaveAttribute('aria-pressed', 'true');
  await expect(reps).toHaveValue('6');
  await expect(page.locator('.plan')).toContainText('0 / 2 пар · 1 / 4 сторон');
  await save(page);
  await expect(left).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.plan')).toContainText('1 / 2 пар · 2 / 4 сторон');
  await page.getByRole('button', { name: 'Разминка', exact: true }).click();
  await expect(reps).toHaveValue('11');
  await save(page);
  await expect(left).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.plan')).toContainText('1 / 2 пар · 2 / 4 сторон');
  await expect(page.getByRole('button', { name: 'Рабочий', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await record(page, 1).click();
  await expect(right).toBeDisabled();
  await reps.fill('5');
  await page.getByRole('button', { name: 'Сохранить изменения', exact: true }).click();
  await expect(record(page, 1)).toContainText('5 повт.');
  await expect(page.getByText('Можно открыть без сети', { exact: true })).toBeVisible();
  await context.setOffline(true);
  await page.reload();
  await page.getByRole('button', { name: 'Продолжить тренировку' }).click();
  await openExercise(page, 'Зашагивание');
  await expect(record(page, 1)).toContainText('Левая');
  await expect(record(page, 2)).toContainText('Правая');
  await expect(record(page, 3)).toContainText('Разминка');
  await record(page, 2).click();
  page.once('dialog', (dialog) => void dialog.accept());
  await page.getByRole('button', { name: 'Удалить подход', exact: true }).click();
  await expect(page.locator('.plan')).toContainText('0 / 2 пар · 1 / 4 сторон');
  await page.getByRole('button', { name: 'Завершить упражнение', exact: true }).click();
  await expect(page.getByRole('button', { name: /Зашагивание/ })).toContainText('0 / 2 пар · 1 / 4 сторон');
});

test('время записывается в секундах, шкала без килограммов, две гантели имеют явный множитель', async ({
  page,
}) => {
  await start(page, 'Боковая планка');
  const duration = page.getByRole('textbox', { name: 'Время, мм:сс', exact: true });
  await expect(duration).toHaveValue('0:25');
  await expect(page.getByRole('textbox', { name: 'Повторения' })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: /кг/ })).toHaveCount(0);
  await duration.fill('0:75');
  await save(page);
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('button', { name: /Редактировать подход/ })).toHaveCount(0);
  await duration.fill('0:30');
  await save(page);
  await expect(record(page, 1)).toContainText('0:30');
  await expect(record(page, 1)).not.toContainText('кг');
  await expect(page.getByRole('progressbar', { name: 'Время относительно плана' })).toHaveAttribute(
    'aria-valuenow',
    '120',
  );
  await page.getByRole('button', { name: 'Разминка', exact: true }).click();
  await expect(duration).toHaveValue('');
  await duration.fill('0:10');
  await save(page);
  await expect(page.getByRole('button', { name: 'Правая', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await page.getByRole('button', { name: 'Завершить упражнение', exact: true }).click();
  await openExercise(page, 'Тренажёр со шкалой');
  const scale = page.getByRole('textbox', { name: 'Показание шкалы', exact: true });
  await expect(scale).toHaveValue('7');
  await page.getByRole('button', { name: 'Разминка', exact: true }).click();
  await expect(scale).toHaveValue('');
  await scale.fill('3,5');
  await save(page);
  await expect(record(page, 1)).toContainText('Шкала 3,5');
  await expect(scale).toHaveValue('7');
  await save(page);
  await expect(record(page, 2)).toContainText('План: Шкала 7 × 12');
  await expect(record(page, 2)).not.toContainText('кг');
  await expect(page.getByRole('progressbar')).toHaveCount(0);
  await page.getByRole('button', { name: 'Завершить упражнение', exact: true }).click();
  await openExercise(page, 'Жим двух гантелей');
  await expect(page.getByText('На один снаряд · одновременно 2', { exact: true })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'На один снаряд, кг' })).toHaveValue('6');
  await save(page);
  await expect(record(page, 1)).toContainText('144 / 144 кг·повт.');
});

test('миоповторения продолжаются короткими целями и дополнительными подходами в той же форме', async ({
  page,
}) => {
  await start(page, 'Миоповторения');
  const reps = page.getByRole('textbox', { name: 'Повторения' });
  const weight = page.getByRole('textbox', { name: 'Вес, кг' });
  await expect(page.getByRole('heading', { name: 'Рабочий подход 1 · активация' })).toBeVisible();
  await expect(reps).toHaveValue('14');
  await reps.fill('13');
  await save(page);
  await expect(reps).toHaveValue('5');
  await expect(weight).toHaveValue('18');
  await save(page);
  await expect(reps).toHaveValue('5');
  await expect(weight).toHaveValue('16');
  await page.getByRole('button', { name: 'Разминка', exact: true }).click();
  await expect(reps).toHaveValue('8');
  await save(page);
  await expect(reps).toHaveValue('5');
  await expect(weight).toHaveValue('16');
  await save(page);
  await save(page);
  await expect(
    page.getByText('Дополнительный · цель последнего планового подхода', { exact: true }),
  ).toBeVisible();
  await expect(reps).toHaveValue('5');
  await expect(weight).toHaveValue('16');
  await save(page);
  await expect(page.locator('.plan')).toContainText('4 / 4 подходов серии · +1 дополнительно');
  await expect(record(page, 6)).toContainText('Дополнительный');
  await expect(page.getByRole('button', { name: /мини-подход|групп/i })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Рабочий подход 6', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Завершить упражнение', exact: true }).click();
  page.once('dialog', (dialog) => void dialog.accept());
  await page.getByRole('button', { name: 'Завершить тренировку', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'История тренировок' })).toBeVisible();
  await page.getByRole('button', { name: /Новые виды записи/ }).click();
  await openExercise(page, 'Миоповторения');
  await expect(record(page, 1)).toContainText('Активация');
  await expect(record(page, 6)).toContainText('16 кг × 5');
});
