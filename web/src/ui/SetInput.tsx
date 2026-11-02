interface Props {
  label: string;
  value: string;
  onChange(value: string): void;
  parsed: number;
  step: number;
  unit: string;
  min: number;
  max: number;
  disabled: boolean;
  decrease: string;
  increase: string;
  inputMode?: 'decimal' | 'numeric' | 'text';
  print?(value: number): string;
}

export default function SetInput({
  label,
  value,
  onChange,
  parsed,
  step,
  unit,
  min,
  max,
  disabled,
  decrease,
  increase,
  inputMode = 'numeric',
  print = String,
}: Props) {
  const valid = Number.isFinite(parsed) && parsed >= min && parsed <= max;
  const change = (direction: number) =>
    onChange(print(Math.round((parsed + step * direction) * 1000) / 1000));
  return (
    <div className="input-column">
      <label>
        {label}
        <input
          inputMode={inputMode}
          aria-label={label}
          disabled={disabled}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      </label>
      <div className="stepper">
        <button
          type="button"
          aria-label={decrease}
          disabled={disabled || !valid || parsed - step < min}
          onClick={() => change(-1)}
        >
          −
        </button>
        <span>{unit}</span>
        <button
          type="button"
          aria-label={increase}
          disabled={disabled || !valid || parsed + step > max}
          onClick={() => change(1)}
        >
          +
        </button>
      </div>
    </div>
  );
}
