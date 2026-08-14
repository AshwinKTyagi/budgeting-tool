export const LEDGER_PAGE_SIZE = 100;

export const LEDGER_EVENT_TYPES = [
  "IncomeReceived",
  "GiftReceived",
  "ExpenseRecorded",
  "PaymentMade",
  "TransferMade",
  "SavingsDrawn",
  "AccountOpeningBalance",
  "InterestCharged",
  "InterestEarned",
  "ObligationRaised",
  "EventVoided",
] as const;

export type LedgerFilters = {
  from: string;
  to: string;
  eventType: string;
  accountId: string;
  category: string;
};

export const EMPTY_LEDGER_FILTERS: LedgerFilters = {
  from: "",
  to: "",
  eventType: "",
  accountId: "",
  category: "",
};

export function ledgerQuery(filters: LedgerFilters, cursor: string | null): string {
  const params = new URLSearchParams();
  params.set("limit", String(LEDGER_PAGE_SIZE));
  if (filters.from) params.set("from", filters.from);
  if (filters.to) params.set("to", filters.to);
  if (filters.eventType) params.set("types", filters.eventType);
  if (filters.accountId) params.set("account_id", filters.accountId);
  if (filters.category) params.set("category", filters.category);
  if (cursor) params.set("cursor", cursor);
  return params.toString();
}
