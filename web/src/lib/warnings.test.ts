import { describe, expect, it } from "vitest";
import { formatPeriodId, formatWarning } from "./warnings";

const RENT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const CYCLE_MSG =
  "cycle Savings:2026-08 carries an estimated 395; a recorded interest event supersedes it";

describe("formatPeriodId", () => {
  it("renders YYYY-MM as a month name and year", () => {
    expect(formatPeriodId("2026-08")).toBe("August 2026");
    expect(formatPeriodId("2026-01")).toBe("January 2026");
  });

  it("leaves unknown shapes alone", () => {
    expect(formatPeriodId("not-a-period")).toBe("not-a-period");
  });
});

describe("formatWarning", () => {
  it("formats ESTIMATED_INTEREST", () => {
    expect(
      formatWarning(
        {
          code: "ESTIMATED_INTEREST",
          message: CYCLE_MSG,
          account_id: "Savings",
        },
        { accounts: [{ account_id: "Savings", name: "Savings" }] },
      ),
    ).toEqual({
      tone: "info",
      text:
        "Estimated interest of $3.95 on Savings for August 2026. " +
        "Not in the balance until you record the bank's figure.",
    });
  });

  it("formats NEGATIVE_ALLOCATION", () => {
    expect(
      formatWarning({
        code: "NEGATIVE_ALLOCATION",
        message: "fixed costs exceed income: savings -5000, discretionary -12000",
        period_id: "2026-08",
      }),
    ).toEqual({
      tone: "warning",
      text: "Bills for August 2026 are larger than income, so savings and spending money went negative.",
    });
  });

  it("formats OBLIGATION_OVERPAID with the payee", () => {
    expect(
      formatWarning(
        {
          code: "OBLIGATION_OVERPAID",
          message: `obligation ${RENT_ID} is overpaid by 1200`,
          period_id: "2026-08",
        },
        { obligations: [{ obligation_id: RENT_ID, payee: "Rent" }] },
      ),
    ).toEqual({
      tone: "warning",
      text: "You paid Rent $12.00 more than was due.",
    });
  });

  it("formats OBLIGATION_PAST_DUE_UNPAID with the payee", () => {
    expect(
      formatWarning(
        {
          code: "OBLIGATION_PAST_DUE_UNPAID",
          message: `obligation ${RENT_ID} was due 2026-08-01 with 50000 outstanding`,
          period_id: "2026-08",
        },
        { obligations: [{ obligation_id: RENT_ID, payee: "Rent" }] },
      ),
    ).toEqual({
      tone: "error",
      text: "Rent was due 2026-08-01 with $500.00 still unpaid.",
    });
  });

  it("formats PAYMENT_WITHOUT_OBLIGATION", () => {
    expect(
      formatWarning({
        code: "PAYMENT_WITHOUT_OBLIGATION",
        message: `payment of 5000 names unknown obligation ${RENT_ID}`,
        event_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        account_id: "checking",
      }),
    ).toEqual({
      tone: "warning",
      text: "A payment of $50.00 does not match any bill.",
    });
  });

  it("formats SAVINGS_DRAW_EXCEEDS_BALANCE", () => {
    expect(
      formatWarning({
        code: "SAVINGS_DRAW_EXCEEDS_BALANCE",
        message: "savings draw of 20000 exceeds the 5000 available on 2026-08-03",
        period_id: "2026-08",
        event_id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
        account_id: "Savings",
      }),
    ).toEqual({
      tone: "warning",
      text: "A savings withdrawal of $200.00 was more than the $50.00 available on 2026-08-03.",
    });
  });

  it("formats CHECKING_OVERDRAWN with the absolute balance", () => {
    expect(
      formatWarning(
        {
          code: "CHECKING_OVERDRAWN",
          message: "checking balance is -4000",
          account_id: "checking",
        },
        { accounts: [{ account_id: "checking", name: "Everyday" }] },
      ),
    ).toEqual({
      tone: "error",
      text: "Everyday is overdrawn by $40.00.",
    });
  });

  it("uses the raw message for an unknown code", () => {
    expect(
      formatWarning({ code: "NEW_THING", message: "something surprising happened" }),
    ).toEqual({
      tone: "warning",
      text: "something surprising happened",
    });
  });

  it("uses the raw message when a known code's template does not match", () => {
    expect(
      formatWarning({
        code: "ESTIMATED_INTEREST",
        message: "interest looks off this month",
        account_id: "Savings",
      }),
    ).toEqual({
      tone: "info",
      text: "interest looks off this month",
    });
  });

  it("falls back to ids when context is missing", () => {
    expect(
      formatWarning({
        code: "ESTIMATED_INTEREST",
        message: CYCLE_MSG,
        account_id: "Savings",
      }),
    ).toEqual({
      tone: "info",
      text:
        "Estimated interest of $3.95 on Savings for August 2026. " +
        "Not in the balance until you record the bank's figure.",
    });
    expect(
      formatWarning({
        code: "OBLIGATION_OVERPAID",
        message: `obligation ${RENT_ID} is overpaid by 1200`,
      }),
    ).toEqual({
      tone: "warning",
      text: "You paid this bill $12.00 more than was due.",
    });
    expect(
      formatWarning({
        code: "CHECKING_OVERDRAWN",
        message: "checking balance is -4000",
      }),
    ).toEqual({
      tone: "error",
      text: "Checking is overdrawn by $40.00.",
    });
  });
});
