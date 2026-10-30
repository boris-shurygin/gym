import { api, type Content, type ProgramView, type SessionInfo } from './account';

function exercise(
  id: string,
  name: string,
  equipment: string,
  convention: Content['templates'][number]['exercises'][number]['weight_convention'],
  weight = 20000,
) {
  return {
    template_exercise_id: id,
    exercise_id: id,
    equipment_id: equipment,
    name,
    weight_convention: convention,
    weight_step_g: 1000,
    rule: 'fixed' as const,
    targets: Array.from({ length: 3 }, (_, index) => ({
      target_id: `${id}-set-${index + 1}`,
      weight_g: weight,
      suggested_reps: 8,
      rep_min: null,
      rep_max: null,
    })),
  };
}

// Данные для проверки приложения; фактический вес пользователь вводит сам.
export const stagingProgram: Content = {
  name: 'Тестовая программа staging',
  templates: [
    {
      template_id: 'staging-test-a',
      name: 'Тестовая A — верх тела',
      description: 'Проверка подходов и синхронизации. Задай свой фактический вес.',
      exercises: [
        exercise('test-bench', 'Жим лёжа', 'barbell', 'total_external_kg'),
        exercise('test-pulldown', 'Тяга верхнего блока', 'machine', 'machine_marking_kg'),
        exercise('test-curl', 'Сгибание рук с гантелями', 'dumbbells', 'per_implement_kg', 5000),
      ],
    },
    {
      template_id: 'staging-test-b',
      name: 'Тестовая B — низ тела',
      description: 'Проверка второй тренировки и истории. Задай свой фактический вес.',
      exercises: [
        exercise('test-squat', 'Приседания', 'barbell', 'total_external_kg'),
        exercise('test-deadlift', 'Румынская тяга', 'barbell', 'total_external_kg'),
        exercise('test-legpress', 'Жим ногами', 'machine', 'machine_marking_kg', 40000),
      ],
    },
  ],
};

interface Change {
  change_id: string;
  change_revision: number;
  content_digest: string;
}

export async function publishStagingProgram(session: SessionInfo): Promise<ProgramView> {
  const owner = session.user_id,
    key = `gym.staging-program:${owner}:v1`;
  const current = await api<ProgramView>('/api/v1/program');
  if (current.owner_id !== owner) throw new Error('account_mismatch');
  if (current.version) {
    localStorage.removeItem(key);
    return current;
  }
  let requests: { propose: string; publish: string };
  try {
    requests = JSON.parse(localStorage.getItem(key) ?? 'null');
    if (
      !requests ||
      ![requests.propose, requests.publish].every(
        (id) => typeof id === 'string' && /^[a-f0-9-]{36}$/.test(id),
      )
    )
      throw new Error();
  } catch {
    requests = { propose: crypto.randomUUID(), publish: crypto.randomUUID() };
    localStorage.setItem(key, JSON.stringify(requests));
  }
  const change = await api<Change>(
    '/api/v1/program/changes',
    {
      request_id: requests.propose,
      expected_owner_id: owner,
      proposal: {
        base_program_version_id: null,
        content: stagingProgram,
        rationale:
          'Тестовые шаблоны staging. Плановые веса условные, для проверки интерфейса; фактические значения вводит пользователь.',
      },
    },
    session,
  );
  const path = `/api/v1/program/changes/${change.change_id}`;
  await api(
    `${path}/validate`,
    { change_id: change.change_id, expected_revision: change.change_revision },
    session,
  );
  await api(
    `${path}/approve-and-publish`,
    {
      request_id: requests.publish,
      expected_owner_id: owner,
      change_id: change.change_id,
      expected_revision: change.change_revision,
      base_program_version_id: null,
      content_digest: change.content_digest,
    },
    session,
  );
  const published = await api<ProgramView>('/api/v1/program');
  if (published.owner_id !== owner || !published.version) throw new Error('publication_unconfirmed');
  localStorage.removeItem(key);
  return published;
}
