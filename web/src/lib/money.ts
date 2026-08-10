/** A validation failure attributable to one named field. */
export class FieldError extends Error {
  field: string | null;

  constructor(field: string | null, message: string) {
    super(message);
    this.name = "FieldError";
    this.field = field;
  }
}

/**
 * Parse typed text into an integer count of minor units.
 *
 *   "19.99" -> 1999      "0.1" -> 10       ".5" -> 50
 *   "100"   -> 10000     "1,234.56" -> 123456
 *   "-25.50" -> -2550    "1.005" -> throws
 *
 * The digits are concatenated and parsed once, as an integer. `parseFloat(x) * 100` is
 * the obvious implementation and it is wrong: `19.99 * 100` is 1998.9999999999998, and
 * the strict boundary exists precisely to catch the 1998 that follows from it.
 *
 * More than two decimal places is an error rather than a rounding. Silently discarding
 * a third digit is the same class of bug wearing a friendlier face — the user typed a
 * number this system cannot represent and should be told so.
 *
 * Doubles as the percent-to-basis-points converter, because basis points are hundredths
 * of a percent exactly as cents are hundredths of a unit: `toMinor("21.99") === 2199`.
 */
export function toMinor(text: unknown): number {
  const raw = String(text ?? "")
    .trim()
    .replace(/[$,\s]/g, "");
  if (raw === "") throw new FieldError(null, "required");

  const match = /^([+-]?)(\d*)(?:\.(\d*))?$/.exec(raw);
  if (match === null) throw new FieldError(null, `"${text}" is not an amount`);

  const [, sign, whole, frac = ""] = match;
  if (whole === "" && frac === "") {
    throw new FieldError(null, `"${text}" is not an amount`);
  }
  if (frac.length > 2) {
    throw new FieldError(
      null,
      "amounts are exact to the cent — at most two decimal places",
    );
  }

  const digits = (whole === "" ? "0" : whole) + (frac + "00").slice(0, 2);
  const value = Number.parseInt(digits, 10);
  if (!Number.isSafeInteger(value)) throw new FieldError(null, "amount is too large");
  return sign === "-" ? -value : value;
}

/** Integer minor units back to a display string. Integer ops only. */
export function fromMinor(minor: number): string {
  const n = Number(minor);
  const negative = n < 0;
  const abs = negative ? -n : n;
  const whole = (abs - (abs % 100)) / 100;
  const cents = abs % 100;
  return `${negative ? "-" : ""}${whole.toLocaleString("en-US")}.${String(cents).padStart(2, "0")}`;
}

/** A whole number in `[min, max]`, or a FieldError naming the field. */
export function intField(
  fields: Record<string, string>,
  name: string,
  min: number,
  max: number,
): number {
  const raw = String(fields[name] ?? "").trim();
  if (raw === "") throw new FieldError(name, "required");
  if (!/^\d+$/.test(raw)) throw new FieldError(name, "whole number only");
  const n = Number.parseInt(raw, 10);
  if (n < min || n > max) throw new FieldError(name, `must be between ${min} and ${max}`);
  return n;
}

/** `toMinor` with the failure attributed to the field it came from. */
export function minorField(fields: Record<string, string>, name: string): number {
  try {
    return toMinor(fields[name]);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    throw new FieldError(name, message);
  }
}
