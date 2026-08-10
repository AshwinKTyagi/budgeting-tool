export const CADENCES: Array<[string, string]> = [
  ["MONTHLY", "Monthly"],
  ["WEEKLY", "Weekly"],
  ["BIWEEKLY", "Every two weeks"],
  ["SEMIMONTHLY", "Twice a month"],
  ["QUARTERLY", "Quarterly"],
  ["ANNUAL", "Annually"],
];

export const LIABILITY_KINDS = new Set(["CREDIT_CARD", "LOAN"]);

export function today(): string {
  return new Date().toISOString().slice(0, 10);
}

/** Optional text: absent rather than empty, since "" is a value and null is not. */
export function optional(value: string): string | undefined {
  return value === "" ? undefined : value;
}
