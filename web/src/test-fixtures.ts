import type { PlanSnapshot, Workout } from './sync-model';
import type { Content } from './account';
import examples from '../../contracts/examples.json';

const content = examples.examples.find((e) => e.operation === 'get_program')!.output as unknown as {
  version: { content: Content };
};
export function plan(owner = 'alice'): PlanSnapshot {
  return {
    plan_snapshot_id: `plan_${owner}`,
    snapshot_schema_version: 1,
    owner_id: owner,
    program_version_id: 'v1',
    template: structuredClone(content.version.content.templates[0]),
    calculated_at: '2026-10-30T10:00:00Z',
    history_revision: 0,
    calculator_version: 'fixed-v1',
  };
}
export function workout(id: string = crypto.randomUUID(), revision = 1): Workout {
  return {
    workout_id: id,
    owner_id: 'alice',
    server_revision: revision,
    status: 'active',
    plan: plan(),
    started_at: '2026-10-30T10:00:00Z',
    completed_at: null,
    updated_at: '2026-10-30T10:00:00Z',
    sets: [],
  };
}
