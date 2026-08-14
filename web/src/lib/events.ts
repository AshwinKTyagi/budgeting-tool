export type CanonicalEvent = {
  event_id: string;
  event_type: string;
  date: string;
  recorded_at: string;
  dedupe_key: string;
  note: string | null;
  amount_minor?: number;
  account_id?: string;
  source?: string;
  merchant?: string | null;
  category?: string;
  obligation_id?: string;
  principal_minor?: number | null;
  interest_minor?: number | null;
  from_account_id?: string;
  to_account_id?: string;
  reason?: string;
  [key: string]: unknown;
};

export const ALTERABLE_TYPES = [
  "IncomeReceived",
  "ExpenseRecorded",
  "PaymentMade",
  "AccountOpeningBalance",
] as const;

export type AlterableType = (typeof ALTERABLE_TYPES)[number];

export function isAlterableType(type: string): type is AlterableType {
  return (ALTERABLE_TYPES as readonly string[]).includes(type);
}

export function isLiveRow(row: { is_voided: boolean; event_type: string }): boolean {
  return !row.is_voided && row.event_type !== "EventVoided";
}
