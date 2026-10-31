export default function FinishExerciseButton({
  disabled,
  onFinish,
}: {
  disabled: boolean;
  onFinish: () => void;
}) {
  return (
    <button type="button" className="primary wide finish" disabled={disabled} onClick={onFinish}>
      Завершить упражнение
    </button>
  );
}
