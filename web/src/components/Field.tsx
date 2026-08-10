import type { ChangeEvent, ReactNode } from "react";

type FieldProps = {
  label: ReactNode;
  name: string;
  error?: string;
  hint?: ReactNode;
  children?: ReactNode;
  type?: string;
  value?: string;
  defaultValue?: string;
  required?: boolean;
  placeholder?: string;
  inputMode?: "decimal" | "numeric" | "text";
  onChange?: (event: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => void;
  as?: "input" | "select";
  options?: Array<[string, string]>;
  className?: string;
};

export function Field({
  label,
  name,
  error,
  hint,
  children,
  type = "text",
  value,
  defaultValue,
  required,
  placeholder,
  inputMode,
  onChange,
  as = "input",
  options,
  className,
}: FieldProps) {
  const bad = Boolean(error);
  return (
    <label className={className}>
      {label}
      {as === "select" ? (
        <select
          name={name}
          value={value}
          defaultValue={defaultValue}
          required={required}
          className={bad ? "bad" : undefined}
          onChange={onChange}
        >
          {children}
          {options?.map(([optValue, optLabel]) => (
            <option key={optValue} value={optValue}>
              {optLabel}
            </option>
          ))}
        </select>
      ) : (
        <input
          name={name}
          type={type}
          value={value}
          defaultValue={defaultValue}
          required={required}
          placeholder={placeholder}
          inputMode={inputMode}
          className={bad ? "bad" : undefined}
          onChange={onChange}
        />
      )}
      {hint}
      <small className={`err${error ? " show" : ""}`}>{error ?? ""}</small>
    </label>
  );
}
