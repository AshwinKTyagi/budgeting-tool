import { useState, type FormEvent, type ReactNode } from "react";
import { ApiError } from "../lib/api";
import { today } from "../lib/constants";
import { FieldError } from "../lib/money";
import { useBudget } from "../context/BudgetContext";

type FormShellProps = {
  children: (ctx: {
    errors: Record<string, string>;
    setErrors: (errors: Record<string, string>) => void;
    busy: boolean;
  }) => ReactNode;
  onSubmit: (fields: Record<string, string>) => Promise<void>;
  /** When false, leave field values after a successful submit (e.g. policy). Default true. */
  resetOnSuccess?: boolean;
};

function readFields(form: HTMLFormElement): Record<string, string> {
  const out: Record<string, string> = {};
  const elements = form.querySelectorAll<HTMLInputElement | HTMLSelectElement>("[name]");
  for (const input of elements) out[input.name] = input.value.trim();
  return out;
}

function setDefaultDates(form: HTMLFormElement): void {
  for (const input of form.querySelectorAll<HTMLInputElement>('input[type="date"]')) {
    if (input.value === "") input.value = today();
  }
}

export function FormShell({ children, onSubmit, resetOnSuccess = true }: FormShellProps) {
  const { showFlash } = useBudget();
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setErrors({});
    setBusy(true);
    try {
      await onSubmit(readFields(form));
      if (resetOnSuccess) {
        form.reset();
        setDefaultDates(form);
      }
    } catch (err) {
      if (err instanceof FieldError) {
        if (err.field) setErrors({ [err.field]: err.message });
        else showFlash("error", err.message);
      } else if (err instanceof ApiError) {
        const fields = err.fieldErrors();
        const keys = Object.keys(fields);
        setErrors(fields);
        if (keys.length === 0) {
          showFlash("error", err.message, err.code);
        }
      } else {
        showFlash("error", err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <form noValidate onSubmit={handleSubmit}>
      {children({ errors, setErrors, busy })}
    </form>
  );
}
