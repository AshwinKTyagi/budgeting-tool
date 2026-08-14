export type AccountVersion = {
  entity_id: string;
  name: string;
  kind: string;
  apr_bps: number;
  statement_close_day: number | null;
  payment_due_day: number | null;
  budget_timing: string;
  effective_from: string;
  effective_to: string | null;
};

export type AccountBalance = {
  account_id: string;
  name: string;
  kind: string;
  balance_minor: number;
  outstanding_minor: number | null;
  cumulative_interest_minor: number;
};

export type Obligation = {
  obligation_id: string;
  payee: string;
  due_date: string;
  category: string;
  amount_minor: number;
  remaining_minor: number;
  status: string;
};

export type PeriodState = {
  period_id: string;
  income_minor: number;
  allocatable_income_minor: number;
  fixed_due_minor: number;
  fixed_outstanding_minor: number;
  savings_allocated_minor: number;
  discretionary_allocated_minor: number;
  discretionary_spent_minor: number;
  discretionary_remaining_minor: number;
};

export type Warning = {
  code: string;
  message: string;
};

export type ProjectedState = {
  current_period_id: string | null;
  periods: PeriodState[];
  obligations: Obligation[];
  warnings: Warning[];
};

export type LedgerOrigin = "manual" | "receipt" | "external";

export type LedgerRow = {
  event_id: string;
  date: string;
  event_type: string;
  period_id: string;
  counterparty: string | null;
  category: string | null;
  account_id: string | null;
  amount_minor: number | null;
  note: string | null;
  is_voided: boolean;
  origin: LedgerOrigin;
};

export type LedgerPage = {
  rows: LedgerRow[];
  next_cursor: string | null;
  total_count: number;
};

export type ChartPoint = {
  bucket: string;
  series?: string;
  value_minor: number;
};

export type ChartSeries = {
  label: string;
  points: ChartPoint[];
};

export type FixedCostVersion = {
  entity_id: string;
  name: string;
  amount_minor: number;
  cadence: string;
  due_day: number;
  payee: string;
  category: string;
  effective_from: string;
};

export type RecurringIncomeVersion = {
  entity_id: string;
  name: string;
  amount_minor: number;
  cadence: string;
  anchor_day: number;
  account_id: string;
  effective_from: string;
};
