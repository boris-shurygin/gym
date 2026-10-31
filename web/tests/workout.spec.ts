import { expect, test, type Page } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('gym.mode', 'demo'));
});

async function start(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: /Верх тела.*Грудь/ }).click();
  await page.getByRole('button', { name: 'Начать тренировку', exact: true }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
}

test('запись, плановые значения, исправление, удаление и история переживают перезапуск без сети', async ({
  page,
  context,
}) => {
  await start(page);
  await expect(page.getByText('Можно открыть без сети', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: 'Вес, кг' }).fill('70');
  await page.getByRole('textbox', { name: 'Повторения' }).fill('9');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
    '70 кг × 9',
  );
  await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('70');
  await expect(page.getByRole('textbox', { name: 'Повторения' })).toHaveValue('9');
  await expect(page.getByText('105%', { exact: true })).toBeVisible();
  await context.setOffline(true);
  await page.reload();
  await page.getByRole('button', { name: 'Продолжить тренировку' }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
    '70 кг × 9',
  );
  await page.getByRole('button', { name: 'Редактировать подход 1', exact: true }).click();
  await page.getByRole('textbox', { name: 'Вес, кг' }).fill('65');
  await page.getByRole('button', { name: 'Сохранить изменения' }).click();
  await expect(page.getByText('97,5%', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await page.getByRole('button', { name: 'Редактировать подход 2', exact: true }).click();
  page.once('dialog', (dialog) => void dialog.accept());
  await page.getByRole('button', { name: 'Удалить подход', exact: true }).click();
  await expect(page.getByRole('button', { name: /Редактировать подход/ })).toHaveCount(1);
  await page.getByRole('button', { name: 'К упражнениям', exact: false }).click();
  page.once('dialog', (dialog) => void dialog.accept());
  await page.getByRole('button', { name: 'Завершить тренировку', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'История тренировок' })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'История', exact: false }).click();
  await page.getByRole('button', { name: /Верх тела.*1 рабочий подход/ }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
    '65 кг × 9',
  );
  await expect(page.getByRole('status')).toContainText('6 изменений');
});

test('разминка предзаполнена, рабочие изменения переносятся и восстанавливаются без сети', async ({
  page,
  context,
}) => {
  await start(page);
  const weight = page.getByRole('textbox', { name: 'Вес, кг' });
  const reps = page.getByRole('textbox', { name: 'Повторения' });
  await page.getByRole('button', { name: 'Разминка', exact: true }).click();
  await expect(weight).toHaveValue('42');
  await expect(reps).toHaveValue('15');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
    '42 кг × 15',
  );
  await expect(page.getByText('Вес в кг · 0 / 3 рабочих подходов')).toBeVisible();
  await expect(weight).toHaveValue('60');
  await expect(reps).toHaveValue('10');
  await weight.fill('65');
  await reps.fill('9');
  await page.getByRole('button', { name: 'Рабочий', exact: true }).click();
  await expect(weight).toHaveValue('65');
  await expect(reps).toHaveValue('9');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Редактировать подход 2', exact: true })).toContainText(
    '65 кг × 9',
  );
  await expect(weight).toHaveValue('65');
  await expect(reps).toHaveValue('9');
  await expect(page.getByText('97,5%', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Разминка', exact: true }).click();
  await expect(weight).toHaveValue('45.5');
  await expect(reps).toHaveValue('14');
  await page.getByRole('button', { name: 'Рабочий', exact: true }).click();
  await expect(weight).toHaveValue('65');
  await expect(reps).toHaveValue('9');
  await context.setOffline(true);
  await page.reload();
  await page.getByRole('button', { name: 'Продолжить тренировку' }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await expect(weight).toHaveValue('65');
  await expect(reps).toHaveValue('9');
  await page.getByRole('button', { name: 'Редактировать подход 1', exact: true }).click();
  await expect(weight).toHaveValue('42');
  await expect(reps).toHaveValue('15');
});

test('ошибка записи очереди не сохраняет подход и оставляет ввод', async ({ page }) => {
  await start(page);
  await page.evaluate(() => {
    const original = IDBObjectStore.prototype.add;
    IDBObjectStore.prototype.add = function (value, key) {
      if (this.name === 'outbox') throw new DOMException('Simulated quota', 'QuotaExceededError');
      return key === undefined ? original.call(this, value) : original.call(this, value, key);
    };
  });
  await page.getByRole('textbox', { name: 'Вес, кг' }).fill('62,5');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Не удалось сохранить');
  await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('62,5');
  await expect(page.getByRole('button', { name: /Редактировать подход/ })).toHaveCount(0);
  await page.reload();
  await page.getByRole('button', { name: 'Продолжить тренировку' }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await expect(page.getByText('Пока нет записанных подходов')).toBeVisible();
  await expect(page.getByRole('status')).toContainText('1 изменение');
});

test('две вкладки не перезаписывают подход по устаревшей ревизии', async ({ page, context }) => {
  await start(page);
  const other = await context.newPage();
  await other.goto('/');
  await other.getByRole('button', { name: 'Продолжить тренировку' }).click();
  await other.getByRole('button', { name: /Жим лёжа/ }).click();
  await other.getByRole('textbox', { name: 'Вес, кг' }).fill('65');
  await other.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await page.getByRole('textbox', { name: 'Вес, кг' }).fill('70');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('другой вкладке');
  await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('70');
  await page.reload();
  await page.getByRole('button', { name: 'Продолжить тренировку' }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await expect(page.getByRole('button', { name: /Редактировать подход/ })).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'Редактировать подход 1' })).toContainText('65 кг');
});

test('кнопки меняют только ввод, разминка не закрывает план, повтор отправки формы не дублирует подход', async ({
  page,
}) => {
  await start(page);
  const saveBounds = await page.getByRole('button', { name: 'Записать подход', exact: true }).boundingBox();
  const navBounds = await page.getByRole('navigation', { name: 'Разделы' }).boundingBox();
  expect(saveBounds).not.toBeNull();
  expect(navBounds).not.toBeNull();
  expect(saveBounds!.y + saveBounds!.height).toBeLessThanOrEqual(navBounds!.y);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', { name: 'Увеличить вес', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('62.5');
  await page.getByRole('button', { name: 'Уменьшить повторения', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Повторения' })).toHaveValue('9');
  await expect(page.getByRole('button', { name: /Редактировать подход/ })).toHaveCount(0);
  await page.getByRole('button', { name: 'К упражнениям', exact: false }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('62.5');
  await page.getByRole('button', { name: 'Разминка', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('42');
  await expect(page.getByRole('textbox', { name: 'Повторения' })).toHaveValue('15');
  await page.getByRole('textbox', { name: 'Вес, кг' }).fill('20');
  await page.getByRole('textbox', { name: 'Повторения' }).fill('8');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await expect(page.getByText('Вес в кг · 0 / 3 рабочих подходов')).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('60');
  await page.locator('form').evaluate((form) => {
    (form as HTMLFormElement).requestSubmit();
    (form as HTMLFormElement).requestSubmit();
  });
  await expect(page.getByRole('button', { name: /Редактировать подход/ })).toHaveCount(2);
  await expect(page.getByText('Вес в кг · 1 / 3 рабочих подходов')).toBeVisible();
});
