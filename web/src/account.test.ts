import { describe, expect, it } from 'vitest';
import { toProgram } from './account';
import { readFileSync } from 'node:fs';

const examples = JSON.parse(
  readFileSync(new URL('../../contracts/examples.json', import.meta.url), 'utf8'),
).examples;
const view = examples.find((e: { operation: string }) => e.operation === 'get_program').output;

describe('серверный план', () => {
  it('сохраняет версию и конкретные цели fixed, переводя граммы в кг', () => {
    const program = toProgram(view);
    expect(program.versionId).toBe(view.version.program_version_id);
    expect(program.templates[0].exercises[0].targets[0]).toEqual({ id: 'target-a', weightKg: 60, reps: 10 });
    expect(view.version.content.templates[0].exercises[0].targets[0].weight_g).toBe(60000);
  });
  it('отклоняет программу другого владельца', () => {
    expect(() => toProgram({ ...view, owner_id: 'other-owner' })).toThrow();
  });
});
