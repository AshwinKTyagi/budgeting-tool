import { describe, expect, it } from "vitest";
import { FieldError, fromMinor, toMinor } from "./money";

describe("toMinor", () => {
  it("parses common amounts via digit concatenation", () => {
    expect(toMinor("19.99")).toBe(1999);
    expect(toMinor("0.1")).toBe(10);
    expect(toMinor(".5")).toBe(50);
    expect(toMinor("100")).toBe(10000);
    expect(toMinor("1,234.56")).toBe(123456);
    expect(toMinor("-25.50")).toBe(-2550);
    expect(toMinor("21.99")).toBe(2199);
  });

  it("rejects more than two decimal places", () => {
    expect(() => toMinor("1.005")).toThrow(FieldError);
    expect(() => toMinor("1.005")).toThrow(/at most two decimal places/);
  });

  it("rejects empty and non-amounts", () => {
    expect(() => toMinor("")).toThrow(/required/);
    expect(() => toMinor("abc")).toThrow(/is not an amount/);
    expect(() => toMinor(".")).toThrow(/is not an amount/);
  });
});

describe("fromMinor", () => {
  it("formats integer minor units", () => {
    expect(fromMinor(1999)).toBe("19.99");
    expect(fromMinor(0)).toBe("0.00");
    expect(fromMinor(-2550)).toBe("-25.50");
    expect(fromMinor(10000)).toBe("100.00");
  });
});
