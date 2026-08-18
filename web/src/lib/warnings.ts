import { fromMinor } from "./money";
import type { Warning } from "./types";

export type WarningTone = "info" | "warning" | "error";

export type WarningCopy = {
  tone: WarningTone;
  text: string;
};

export type WarningContext = {
  accounts?: ReadonlyArray<{ account_id: string; name: string }>;
  obligations?: ReadonlyArray<{ obligation_id: string; payee: string }>;
};

const TONE_BY_CODE: Record<string, WarningTone> = {
  ESTIMATED_INTEREST: "info",
  NEGATIVE_ALLOCATION: "warning",
  OBLIGATION_OVERPAID: "warning",
  SAVINGS_DRAW_EXCEEDS_BALANCE: "warning",
  PAYMENT_WITHOUT_OBLIGATION: "warning",
  OBLIGATION_PAST_DUE_UNPAID: "error",
  CHECKING_OVERDRAWN: "error",
};

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
] as const;

const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";
const MINOR = "(-?\\d+)";
const ISO_DATE = "(\\d{4}-\\d{2}-\\d{2})";
const PERIOD = /^(\d{4})-(\d{2})$/;

const ESTIMATED_INTEREST = new RegExp(
  `^cycle (.+) carries an estimated ${MINOR}; a recorded interest event supersedes it$`,
);
const NEGATIVE_ALLOCATION = new RegExp(
  `^fixed costs exceed income: savings ${MINOR}, discretionary ${MINOR}$`,
);
const OBLIGATION_OVERPAID = new RegExp(`^obligation (${UUID}) is overpaid by ${MINOR}$`);
const OBLIGATION_PAST_DUE = new RegExp(
  `^obligation (${UUID}) was due ${ISO_DATE} with ${MINOR} outstanding$`,
);
const PAYMENT_WITHOUT_OBLIGATION = new RegExp(
  `^payment of ${MINOR} names unknown obligation (${UUID})$`,
);
const SAVINGS_DRAW = new RegExp(
  `^savings draw of ${MINOR} exceeds the ${MINOR} available on ${ISO_DATE}$`,
);
const CHECKING_OVERDRAWN = new RegExp(`^checking balance is ${MINOR}$`);

function toneFor(code: string): WarningTone {
  return TONE_BY_CODE[code] ?? "warning";
}

function fallback(warning: Warning): WarningCopy {
  return { tone: toneFor(warning.code), text: warning.message };
}

/** `2026-08` → `August 2026`. Unknown shapes stay as given. */
export function formatPeriodId(periodId: string): string {
  const match = PERIOD.exec(periodId);
  if (match === null) return periodId;
  const month = Number.parseInt(match[2], 10);
  if (month < 1 || month > 12) return periodId;
  return `${MONTHS[month - 1]} ${match[1]}`;
}

function accountLabel(warning: Warning, context: WarningContext | undefined): string {
  const id = warning.account_id;
  if (id == null || id === "") return "this account";
  const name = context?.accounts?.find((account) => account.account_id === id)?.name;
  return name ?? id;
}

function obligationLabel(obligationId: string, context: WarningContext | undefined): string {
  const payee = context?.obligations?.find((row) => row.obligation_id === obligationId)?.payee;
  return payee ?? "this bill";
}

/** Cents → display dollars. `1000` → `$10.00`; negatives as `-$10.00`. */
function dollars(minor: number): string {
  const formatted = fromMinor(minor);
  return formatted.startsWith("-") ? `-$${formatted.slice(1)}` : `$${formatted}`;
}

function absMinor(minor: number): string {
  return dollars(minor < 0 ? -minor : minor);
}

function periodFromCycleId(cycleId: string): string | null {
  const match = /:(\d{4}-\d{2})$/.exec(cycleId);
  return match === null ? null : match[1];
}

function parseMinor(raw: string): number {
  return Number.parseInt(raw, 10);
}

/**
 * Turn a domain warning into user copy. Unknown codes and templates that do not
 * match keep `message` so a new backend string still appears.
 */
export function formatWarning(
  warning: Warning,
  context?: WarningContext,
): WarningCopy {
  const tone = toneFor(warning.code);
  switch (warning.code) {
    case "ESTIMATED_INTEREST": {
      const match = ESTIMATED_INTEREST.exec(warning.message);
      if (match === null) return fallback(warning);
      const periodId = periodFromCycleId(match[1]);
      const when = periodId === null ? "this period" : formatPeriodId(periodId);
      return {
        tone,
        text:
          `Estimated interest of ${dollars(parseMinor(match[2]))} on ` +
          `${accountLabel(warning, context)} for ${when}. ` +
          `Not in the balance until you record the bank's figure.`,
      };
    }
    case "NEGATIVE_ALLOCATION": {
      if (NEGATIVE_ALLOCATION.exec(warning.message) === null) return fallback(warning);
      const period =
        warning.period_id == null || warning.period_id === ""
          ? "this period"
          : formatPeriodId(warning.period_id);
      return {
        tone,
        text: `Bills for ${period} are larger than income, so savings and spending money went negative.`,
      };
    }
    case "OBLIGATION_OVERPAID": {
      const match = OBLIGATION_OVERPAID.exec(warning.message);
      if (match === null) return fallback(warning);
      return {
        tone,
        text: `You paid ${obligationLabel(match[1], context)} ${dollars(parseMinor(match[2]))} more than was due.`,
      };
    }
    case "OBLIGATION_PAST_DUE_UNPAID": {
      const match = OBLIGATION_PAST_DUE.exec(warning.message);
      if (match === null) return fallback(warning);
      const who = obligationLabel(match[1], context);
      const subject = who === "this bill" ? "This bill" : who;
      return {
        tone,
        text: `${subject} was due ${match[2]} with ${dollars(parseMinor(match[3]))} still unpaid.`,
      };
    }
    case "PAYMENT_WITHOUT_OBLIGATION": {
      const match = PAYMENT_WITHOUT_OBLIGATION.exec(warning.message);
      if (match === null) return fallback(warning);
      return {
        tone,
        text: `A payment of ${dollars(parseMinor(match[1]))} does not match any bill.`,
      };
    }
    case "SAVINGS_DRAW_EXCEEDS_BALANCE": {
      const match = SAVINGS_DRAW.exec(warning.message);
      if (match === null) return fallback(warning);
      return {
        tone,
        text:
          `A savings withdrawal of ${dollars(parseMinor(match[1]))} was more than the ` +
          `${dollars(parseMinor(match[2]))} available on ${match[3]}.`,
      };
    }
    case "CHECKING_OVERDRAWN": {
      const match = CHECKING_OVERDRAWN.exec(warning.message);
      if (match === null) return fallback(warning);
      const name =
        warning.account_id == null || warning.account_id === ""
          ? "Checking"
          : accountLabel(warning, context);
      return {
        tone,
        text: `${name} is overdrawn by ${absMinor(parseMinor(match[1]))}.`,
      };
    }
    default:
      return fallback(warning);
  }
}
