import type { AccountVersion } from "../lib/types";

type AccountSelectProps = {
  name?: string;
  value: string;
  onChange: (value: string) => void;
  accounts: AccountVersion[];
  required?: boolean;
  className?: string;
  error?: boolean;
};

export function AccountSelect({
  name = "account_id",
  value,
  onChange,
  accounts,
  required,
  className,
  error,
}: AccountSelectProps) {
  return (
    <select
      name={name}
      value={value}
      required={required}
      className={[className, error ? "bad" : ""].filter(Boolean).join(" ") || undefined}
      onChange={(event) => onChange(event.target.value)}
    >
      {accounts.length === 0 ? (
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
