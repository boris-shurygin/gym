import { describe, expect, it } from 'vitest';
import { demoProgram } from './demo-program';
import { applyMutation, createSession, newSetDefaults, nextTarget, volumeRatio } from './model';

const now = '2026-10-25T12:00:00Z';
const fresh = () => createSession(demoProgram, 'upper', 'alice', 'session', now);
const add = (id: string, weightKg = 60, reps = 10, kind: 'working' | 'warmup' = 'working') => ({
  type: 'addSet' as const,
  exerciseId: 'bench',
  setId: id,
  values: { weightKg, reps, kind },
});

describe('Предзаполнение новых подходов', () => {
  function varied() {
    const program = structuredClone(demoProgram);
    program.templates[0].exercises[0].targets[1] = { id: 'bench-2', weightKg: 65, reps: 8 };
    return createSession(program, 'upper', 'alice', 'varied', now);
  }

  it('берёт собственную цель следующей позиции, когда предыдущая выполнена по плану', () => {
    const session = applyMutation(varied(), add('one'), now);
    expect(newSetDefaults(session, 'bench', 'working')).toEqual({ kind: 'working', weightKg: 65, reps: 8 });
  });

  it.each([
    [62.5, 10],
    [60, 9],
    [62.501, 9],
  ])('переносит оба фактических значения при изменении веса или повторений: %s × %s', (weightKg, reps) => {
    const session = applyMutation(varied(), add('one', weightKg, reps), now);
    expect(newSetDefaults(session, 'bench', 'working')).toEqual({ kind: 'working', weightKg, reps });
    expect(nextTarget(session, 'bench').target).toEqual({ id: 'bench-2', weightKg: 65, reps: 8 });
    expect(session.snapshot.exercises[0].targets[0].weightKg).toBe(60);
  });

  it('повторяет последнее отклонение до возвращения к плановым значениям', () => {
    let session = applyMutation(varied(), add('one', 62.5, 9), now);
    session = applyMutation(session, add('two', 62.5, 9), now);
    expect(newSetDefaults(session, 'bench', 'working').weightKg).toBe(62.5);
    session = applyMutation(
      session,
      {
        type: 'editSet',
        setId: 'two',
        values: { kind: 'working', weightKg: 65, reps: 8 },
      },
      now,
    );
    expect(newSetDefaults(session, 'bench', 'working')).toEqual({ kind: 'working', weightKg: 60, reps: 10 });
  });

  it('игнорирует разминку, удалённые результаты и рабочие подходы другого упражнения', () => {
    let session = applyMutation(fresh(), add('one', 65, 9), now);
    session = applyMutation(session, add('warmup', 20, 15, 'warmup'), now);
    session = applyMutation(session, add('deleted', 90, 3), now);
    session = applyMutation(session, { type: 'deleteSet', setId: 'deleted' }, now);
    session = applyMutation(session, { ...add('other', 100, 5), exerciseId: 'row' }, now);
    expect(newSetDefaults(session, 'bench', 'working')).toEqual({ kind: 'working', weightKg: 65, reps: 9 });
  });

  it('для дополнительного подхода переносит отклонение, сохраняя сравнение с последней целью', () => {
    let session = fresh();
    for (let i = 0; i < 3; i++) session = applyMutation(session, add(String(i), 65, 9), now);
    expect(newSetDefaults(session, 'bench', 'working')).toEqual({ kind: 'working', weightKg: 65, reps: 9 });
    expect(nextTarget(session, 'bench').plannedTargetId).toBeNull();
    expect(nextTarget(session, 'bench').target.weightKg).toBe(60);
  });

  it('разминка использует 70% веса следующего рабочего и полтора числа повторений', () => {
    expect(newSetDefaults(fresh(), 'bench', 'warmup')).toEqual({ kind: 'warmup', weightKg: 42, reps: 15 });
    const session = applyMutation(fresh(), add('one', 65, 9), now);
    expect(newSetDefaults(session, 'bench', 'warmup')).toEqual({ kind: 'warmup', weightKg: 45.5, reps: 14 });
    expect(newSetDefaults(session, 'bench', 'working')).toEqual({ kind: 'working', weightKg: 65, reps: 9 });
  });

  it('разминка поддерживает точные граммы, нулевой вес и допустимый предел повторений', () => {
    let session = applyMutation(fresh(), add('one', 60.001, 7), now);
    expect(newSetDefaults(session, 'bench', 'warmup')).toEqual({
      kind: 'warmup',
      weightKg: 42.001,
      reps: 11,
    });
    session = applyMutation(session, add('two', 0, 1000), now);
    expect(newSetDefaults(session, 'bench', 'warmup')).toEqual({ kind: 'warmup', weightKg: 0, reps: 1000 });
  });
});

describe('Снимок и фактические подходы', () => {
  it('снимок не меняется при изменении исходной программы или результата', () => {
    const program = structuredClone(demoProgram);
    const session = createSession(program, 'upper', 'alice', 'session', now);
    program.templates[0].exercises[0].targets[0].weightKg = 500;
    const saved = applyMutation(session, add('one', 70, 9), now);
    expect(session.sets).toHaveLength(0);
    expect(saved.snapshot.exercises[0].targets[0].weightKg).toBe(60);
    expect(nextTarget(saved, 'bench').target.weightKg).toBe(60);
    expect(volumeRatio(saved.sets[0])).toBe(1.05);
  });

  it('разминка не занимает плановый слот, дополнительные берут последнюю цель', () => {
    let session = applyMutation(fresh(), add('warmup', 20, 8, 'warmup'), now);
    for (let n = 1; n <= 4; n++) session = applyMutation(session, add(`working-${n}`), now);
    expect(session.sets[0].comparisonTarget).toBeNull();
    expect(session.sets[1].plannedTargetId).toBe('bench-1');
    expect(session.sets[4].plannedTargetId).toBeNull();
    expect(session.sets[4].comparisonTarget?.id).toBe('bench-3');
  });

  it('удаление/исправление не перепривязывает цели остальных подходов', () => {
    let session = applyMutation(fresh(), add('one'), now);
    session = applyMutation(session, add('two'), now);
    session = applyMutation(session, { type: 'deleteSet', setId: 'one' }, now);
    expect(session.sets[1].plannedTargetId).toBe('bench-2');
    expect(nextTarget(session, 'bench').plannedTargetId).toBe('bench-1');
    session = applyMutation(
      session,
      { type: 'editSet', setId: 'two', values: { weightKg: 65, reps: 9, kind: 'working' } },
      now,
    );
    expect(session.sets[1].plannedTargetId).toBe('bench-2');
    expect(volumeRatio(session.sets[1])).toBe(0.975);
  });

  it('завершение неполной тренировки и исправление истории сохраняют снимок', () => {
    let session = applyMutation(fresh(), add('one'), now);
    session = applyMutation(session, { type: 'complete' }, now);
    session = applyMutation(
      session,
      { type: 'editSet', setId: 'one', values: { kind: 'working', weightKg: 55, reps: 8 } },
      now,
    );
    expect(session.status).toBe('completed');
    expect(session.sets).toHaveLength(1);
    expect(session.snapshot.exercises[0].targets).toHaveLength(3);
  });

  it('пустая тренировка не завершается и упражнение вне шаблона не добавляется', () => {
    expect(() => applyMutation(fresh(), { type: 'complete' }, now)).toThrow();
    expect(() => applyMutation(fresh(), { ...add('one'), exerciseId: 'foreign' }, now)).toThrow();
    const cancelled = applyMutation(fresh(), { type: 'cancelEmpty' }, now);
    expect(() => applyMutation(cancelled, add('one'), now)).toThrow();
  });

  it.each([
    [NaN, 10],
    [-1, 10],
    [Infinity, 10],
    [Number.MAX_VALUE, 2],
    [60, 1.5],
    [60, 0],
    [60, Number.MAX_SAFE_INTEGER + 1],
  ])('не сохраняет некорректные вес/повторения %s / %s', (weight, reps) => {
    expect(() => applyMutation(fresh(), add('one', weight, reps), now)).toThrow();
  });

  it('при нулевой плановой нагрузке не придумывает процент', () => {
    const program = structuredClone(demoProgram);
    program.templates[0].exercises[0].targets[0].weightKg = 0;
    const session = applyMutation(createSession(program, 'upper', 'alice', 'zero', now), add('one', 0), now);
    expect(volumeRatio(session.sets[0])).toBeNull();
  });
});
