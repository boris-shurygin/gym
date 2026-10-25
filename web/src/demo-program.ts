import type { Program, Exercise } from './model';

function exercise(id: string, name: string, equipment: string, weightKg: number, reps: number): Exercise {
  return {
    id,
    name,
    equipment,
    weightStepKg: id === 'curl' ? 0.5 : 2.5,
    targets: [1, 2, 3].map((n) => ({ id: `${id}-${n}`, weightKg, reps })),
  };
}

// Synthetic fixture for local development, not a personalised recommendation or published user program.
export const DEMO_OWNER = 'local-demo';
export const demoProgram: Program = {
  versionId: 'synthetic-program-v1',
  templates: [
    {
      id: 'upper',
      name: 'Верх тела',
      description: 'Грудь · спина · плечи',
      exercises: [
        exercise('bench', 'Жим лёжа', 'Штанга · общий вес', 60, 10),
        exercise('row', 'Тяга верхнего блока', 'Блочный тренажёр', 45, 12),
        exercise('press', 'Жим сидя', 'Тренажёр', 25, 10),
        exercise('curl', 'Сгибание рук', 'Гантель · вес одной', 10, 12),
      ],
    },
    {
      id: 'legs',
      name: 'День ног',
      description: 'Ноги · ягодичные · икры',
      exercises: [
        exercise('squat', 'Приседания', 'Штанга · общий вес', 50, 10),
        exercise('legpress', 'Жим ногами', 'Тренажёр · указанная нагрузка', 80, 12),
        exercise('legcurl', 'Сгибание ног', 'Тренажёр', 20, 12),
        exercise('calf', 'Подъём на носки', 'Тренажёр', 25, 15),
      ],
    },
  ],
};
