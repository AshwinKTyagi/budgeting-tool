import type { AccountVersion } from "../lib/types";

type AccountSelectProps = {
  name?: string;
  value: string;
  onChange: (value: string) => void;
  accounts: AccountVersion[];
  required?: boolean;
  className?: string;
  error?: boolean;
  emptyLabel?: string;
};

export function AccountSelect({
  name = "account_id",
  value,
  onChange,
  accounts,
  required,
  className,
  error,
  emptyLabel,
}: AccountSelectProps) {
  return (
    <select
      name={name}
      value={value}
      required={required}
      className={[className, error ? "bad" : ""].filter(Boolean).join(" ") || undefined}
      onChange={(event) => onChange(event.target.value)}
    >
      {emptyLabel ? <option value="">{emptyLabel}</option> : null}
      {accounts.length === 0 && !emptyLabel ? (
        <option value="">— add an account first —</option>
      ) : null}
      {accounts.map((account) => (
        <option key={account.entity_id} value={account.entity_id}>
          {account.name} ({account.entity_id})
        </option>
      ))}
    </select>
  );
}
