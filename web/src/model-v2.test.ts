import 'fake-indexeddb/auto';
import { afterEach, describe, expect, it } from 'vitest';
import type { ContentV2, ExerciseV2 } from './account';
import {
  applyMutation,
  createSession,
  exerciseSetCounts,
  formatResult,
  newSetDefaults,
  nextTarget,
  parseDuration,
  progressRatio,
  type Command,
  type Session,
  type SetValues,
} from './model';
import {
  operationFor,
  programFromPlans,
  rebase,
  sessionFromWorkout,
  type PlanSnapshot,
  type Workout,
} from './sync-model';
import { WorkoutRepository } from './repository';
import { plan as legacyPlan } from './test-fixtures';

const now = '2026-11-02T10:00:00Z';
const target = (
  id: string,
  weight: number | null,
  reps: number | null,
  seconds: number | null = null,
  scale: number | null = null,
) => ({
  target_id: id,
  weight_g: weight,
  suggested_reps: reps,
  duration_seconds: seconds,
  scale_milli: scale,
  rep_min: null,
  rep_max: null,
});
function exercise(id: string, changes: Partial<ExerciseV2> = {}): ExerciseV2 {
  return {
    template_exercise_id: id,
    exercise_id: id,
    equipment_id: 'fixture',
    name: id,
    rule: 'fixed',
    measurement: 'reps',
    load: { kind: 'bodyweight' },
    sides: 'single',
    set_structure: 'straight',
    rest_seconds: null,
    targets: [target('a', null, 8), target('b', null, 6)],
    ...changes,
  };
}
const content: ContentV2 = {
  content_schema_version: 2,
  name: 'Synthetic v2',
  templates: [
    {
      template_id: 'mixed',
      name: 'Mixed',
      description: 'Test data',
      exercises: [
        exercise('body'),
        exercise('pair', {
          sides: 'each',
          load: { kind: 'external', weight_convention: 'per_implement_kg', implement_count: 2, step_g: 500 },
          targets: [target('a', 10000, 8), target('b', 12000, 6)],
        }),
        exercise('duration', {
          measurement: 'duration',
          sides: 'each',
          targets: [target('a', null, null, 45)],
        }),
        exercise('scale', {
          load: { kind: 'machine_scale', step_milli: 500 },
          targets: [target('a', null, 8, null, 12500)],
        }),
        exercise('myo', {
          set_structure: 'myo',
          rest_seconds: { min: 5, max: 8 },
          load: { kind: 'external', weight_convention: 'per_implement_kg', implement_count: 2, step_g: 500 },
          targets: [target('a', 12000, 15), target('b', 12000, 5), target('c', 10000, 5)],
        }),
      ],
    },
  ],
};
function v2Plan(): PlanSnapshot {
  return {
    snapshot_schema_version: 2,
    plan_snapshot_id: 'plan-v2',
    owner_id: 'alice',
    program_version_id: 'program-v2',
    template: structuredClone(content.templates[0]),
    calculated_at: now,
    history_revision: 0,
    calculator_version: 'fixed-v2',
  };
}
function fresh() {
  return createSession(programFromPlans('alice', [v2Plan()]), 'mixed', 'alice', crypto.randomUUID(), now);
}
function add(
  session: Session,
  id: string,
  changes: Partial<SetValues> = {},
  setId: string = crypto.randomUUID(),
) {
  return applyMutation(
    session,
    {
      type: 'addSet',
      exerciseId: id,
      setId,
      values: { ...newSetDefaults(session, id, 'working', changes.side), ...changes },
    },
    now,
  );
}
function server(session: Session): Workout {
  return {
    workout_id: session.id,
    owner_id: session.ownerId,
    server_revision: 1,
    status: session.status,
    plan: v2Plan(),
    started_at: now,
    updated_at: now,
    completed_at: null,
    sets: session.sets.map((s) => ({
      set_id: s.id,
      session_exercise_id: s.exerciseId,
      kind: s.kind,
      side: s.side ?? null,
      weight_g: s.weightKg === null ? null : Math.round(s.weightKg * 1000),
      reps: s.reps,
      scale_milli: s.scaleValue == null ? null : Math.round(s.scaleValue * 1000),
      duration_seconds: s.durationSeconds ?? null,
      planned_target_id: s.plannedTargetId,
      comparison_target_id: s.comparisonTarget?.id ?? null,
      deleted_at: s.deletedAt ?? null,
    })),
  };
}
const repositories: WorkoutRepository[] = [];
afterEach(async () => {
  await Promise.all(repositories.splice(0).map((r) => r.close()));
});

describe('v2 measurements, sides and fixed targets', () => {
  it('keeps seconds and scale separate from kilograms and repetitions through server projection', () => {
    let session = add(fresh(), 'duration', { side: 'left', durationSeconds: 40 });
    session = add(session, 'scale', { scaleValue: 12.125, reps: 9 });
    const restored = sessionFromWorkout(server(session));
    expect(restored.sets[0]).toMatchObject({ weightKg: null, reps: null, durationSeconds: 40, side: 'left' });
    expect(restored.sets[1]).toMatchObject({ weightKg: null, reps: 9, scaleValue: 12.125 });
    expect(formatResult(restored.sets[0])).toBe('0:40');
    expect(formatResult(restored.sets[1])).toBe('Шкала 12,125 × 9');
    expect(
      progressRatio(
        restored.sets[1],
        restored.snapshot.exercises.find((e) => e.id === 'scale'),
      ),
    ).toBeNull();
    expect(restored.snapshot.exercises.find((e) => e.id === 'pair')?.load).toEqual({
      kind: 'external',
      weightConvention: 'per_implement_kg',
      implementCount: 2,
      stepKg: 0.5,
    });
  });

  it('binds sides independently and frees only the deleted planned position', () => {
    let session = add(fresh(), 'pair', { side: 'left', weightKg: 10.125, reps: 7 }, 'left-a');
    expect(nextTarget(session, 'pair', undefined, 'left').plannedTargetId).toBe('b');
    expect(nextTarget(session, 'pair', undefined, 'right').plannedTargetId).toBe('a');
    expect(newSetDefaults(session, 'pair', 'working', 'right')).toMatchObject({ weightKg: 10, reps: 8 });
    expect(newSetDefaults(session, 'pair', 'working', 'left')).toMatchObject({ weightKg: 10.125, reps: 7 });
    session = add(session, 'pair', { side: 'right' }, 'right-a');
    session = add(session, 'pair', { side: 'left' }, 'left-b');
    session = add(session, 'pair', { side: 'left' }, 'left-extra');
    expect(exerciseSetCounts(session, 'pair')).toEqual({
      planned: 3,
      totalPlanned: 4,
      additional: 1,
      pairs: 1,
    });
    session = applyMutation(session, { type: 'deleteSet', setId: 'left-a' }, now);
    expect(nextTarget(session, 'pair', undefined, 'left').plannedTargetId).toBe('a');
    expect(session.sets.find((s) => s.id === 'left-b')?.plannedTargetId).toBe('b');
    expect(exerciseSetCounts(session, 'pair')).toEqual({
      planned: 2,
      totalPlanned: 4,
      additional: 1,
      pairs: 0,
    });
    expect(() =>
      applyMutation(
        session,
        { type: 'editSet', setId: 'right-a', values: { ...session.sets[1], side: 'left' } },
        now,
      ),
    ).toThrow();
  });

  it('uses every myo target and keeps extra sets short without a new activation', () => {
    let session = add(fresh(), 'myo', { weightKg: 13, reps: 17 });
    expect(newSetDefaults(session, 'myo', 'working')).toMatchObject({ weightKg: 12, reps: 5 });
    session = add(session, 'myo', { weightKg: 11, reps: 4 });
    expect(newSetDefaults(session, 'myo', 'working')).toMatchObject({ weightKg: 10, reps: 5 });
    session = add(session, 'myo');
    session = add(session, 'myo', { reps: 3 });
    expect(nextTarget(session, 'myo').plannedTargetId).toBeNull();
    expect(newSetDefaults(session, 'myo', 'working')).toMatchObject({ weightKg: 10, reps: 5 });
    expect(session.snapshot.exercises.find((e) => e.id === 'myo')?.restSeconds).toEqual({ min: 5, max: 8 });
  });

  it('offers incomplete manual warmups without inventing time or scale', () => {
    const session = fresh();
    expect(newSetDefaults(session, 'pair', 'warmup', 'left')).toMatchObject({
      weightKg: 7,
      reps: 12,
      side: 'left',
    });
    expect(newSetDefaults(session, 'body', 'warmup')).toMatchObject({ weightKg: null, reps: 12 });
    expect(newSetDefaults(session, 'scale', 'warmup')).toMatchObject({
      weightKg: null,
      scaleValue: null,
      reps: 12,
    });
    expect(newSetDefaults(session, 'duration', 'warmup', 'right')).toMatchObject({
      weightKg: null,
      reps: null,
      durationSeconds: null,
      side: 'right',
    });
    const saved = add(session, 'pair', newSetDefaults(session, 'pair', 'warmup', 'left'));
    expect(saved.sets[0].plannedTargetId).toBeNull();
    expect(nextTarget(saved, 'pair', undefined, 'left').plannedTargetId).toBe('a');
    for (const [id, values] of [
      ['body', { weightKg: 0 }],
      ['duration', { side: 'left', durationSeconds: null, reps: 45 }],
      ['scale', { scaleValue: null, weightKg: 12.5 }],
      ['pair', { side: null }],
    ] as [string, Partial<SetValues>][])
      expect(() => add(session, id, values)).toThrow();
    expect(parseDuration('1:59')).toBe(119);
    for (const invalid of ['1:60', '1:5', '0:00', '60', '1440:01']) expect(parseDuration(invalid)).toBeNull();
  });
});

describe('v1/v2 offline envelopes and conflict replay', () => {
  it('preserves saved v2 wires across reopen, repeated command and plan replacement', async () => {
    const name = crypto.randomUUID(),
      r = new WorkoutRepository(name);
    repositories.push(r);
    const session = fresh();
    const start: Command = {
      id: crypto.randomUUID(),
      ownerId: 'alice',
      sessionId: session.id,
      expectedRevision: 0,
      createdAt: now,
      mutation: { type: 'start', session },
    };
    const saved = await r.commit(start);
    const command: Command = {
      ...start,
      id: crypto.randomUUID(),
      expectedRevision: saved.revision,
      mutation: {
        type: 'addSet',
        exerciseId: 'duration',
        setId: 'duration-left',
        values: newSetDefaults(saved, 'duration', 'working', 'left'),
      },
    };
    await r.commit(command);
    const before = await r.read('alice'),
      bytes = JSON.stringify(before.operations.map((op) => op.wire));
    expect(before.operations[1].wire).toMatchObject({
      schema_version: 2,
      command: { weight_g: null, reps: null, scale_milli: null, duration_seconds: 45, side: 'left' },
    });
    await r.close();
    const reopened = new WorkoutRepository(name);
    repositories.push(reopened);
    await reopened.receive('alice', { plans: [legacyPlan()] });
    await reopened.commit(command);
    expect(JSON.stringify((await reopened.read('alice')).operations.map((op) => op.wire))).toBe(bytes);
    expect((await reopened.read('alice')).sessions[0].sets).toHaveLength(1);
  });

  it('leaves legacy operation content and fingerprints untouched when v2 plans arrive', async () => {
    const r = new WorkoutRepository(crypto.randomUUID());
    repositories.push(r);
    const plan = legacyPlan(),
      session = createSession(
        programFromPlans('alice', [plan]),
        plan.template.template_id,
        'alice',
        crypto.randomUUID(),
        now,
      );
    const started = await r.commit({
      id: crypto.randomUUID(),
      ownerId: 'alice',
      sessionId: session.id,
      expectedRevision: 0,
      createdAt: now,
      mutation: { type: 'start', session },
    });
    await r.commit({
      id: crypto.randomUUID(),
      ownerId: 'alice',
      sessionId: session.id,
      expectedRevision: started.revision,
      createdAt: now,
      mutation: {
        type: 'addSet',
        exerciseId: session.snapshot.exercises[0].id,
        setId: crypto.randomUUID(),
        values: newSetDefaults(started, session.snapshot.exercises[0].id, 'working'),
      },
    });
    const before = (await r.read('alice')).operations;
    await r.receive('alice', { plans: [v2Plan()] });
    expect((await r.read('alice')).operations).toEqual(before);
    expect(before[0].wire?.schema_version).toBe(1);
    expect(before[1].wire?.command).toMatchObject({ type: 'add_set', weight_g: 60000, reps: 10 });
    expect(JSON.stringify(before.map((operation) => operation.wire))).not.toMatch(
      /duration_seconds|scale_milli|"side"/,
    );
  });

  it('replays an explicit local choice without consuming another side target', () => {
    const base = fresh(),
      local = add(base, 'pair', { side: 'left', weightKg: 11 }, 'left'),
      remote = server(add(base, 'pair', { side: 'right' }, 'right'));
    const command: Command = {
      id: crypto.randomUUID(),
      ownerId: 'alice',
      sessionId: base.id,
      expectedRevision: 0,
      createdAt: now,
      mutation: {
        type: 'addSet',
        exerciseId: 'pair',
        setId: 'left',
        values: {
          kind: 'working',
          side: 'left',
          weightKg: 11,
          reps: 8,
          scaleValue: null,
          durationSeconds: null,
        },
      },
    };
    const preview = rebase(remote, local, [command], now);
    expect(preview.session.sets.map((s) => [s.side, s.plannedTargetId])).toEqual([
      ['right', 'a'],
      ['left', 'a'],
    ]);
    expect(operationFor(command, preview.session, 'device', { server_revision: 1 })).toMatchObject({
      schema_version: 2,
      command: { side: 'left', weight_g: 11000 },
    });
    expect(remote.sets).toHaveLength(1);
    expect(local.sets).toHaveLength(1);
  });
});
