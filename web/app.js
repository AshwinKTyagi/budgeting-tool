/* budgeting-tool — data entry client.
 *
 * Two rules govern everything below, both inherited from the API rather than invented
 * here:
 *
 *   1. All money is an integer count of minor units. The API runs pydantic in strict
 *      mode, so `19.99` in an `amount_minor` field is REJECTED, not rounded. `toMinor`
 *      is the only way a typed amount becomes a number, and it never multiplies.
 *   2. Every model is `extra="forbid"`. A response object cannot be edited and posted
 *      back — server-assigned fields would be rejected as unknown keys. Every payload
 *      here is built literally.
 */

import { renderSeries } from "/chart.js";

const API = "/api/v1";

const CADENCES = [
  ["MONTHLY", "Monthly"],
  ["WEEKLY", "Weekly"],
  ["BIWEEKLY", "Every two weeks"],
  ["SEMIMONTHLY", "Twice a month"],
  ["QUARTERLY", "Quarterly"],
  ["ANNUAL", "Annually"],
];

const LIABILITY_KINDS = new Set(["CREDIT_CARD", "LOAN"]);

/* ------------------------------------------------------------------- money */

/** A validation failure attributable to one named field. */
class FieldError extends Error {
  constructor(field, message) {
    super(message);
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
export function toMinor(text) {
  const raw = String(text ?? "").trim().replace(/[$,\s]/g, "");
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
export function fromMinor(minor) {
  const n = Number(minor);
  const negative = n < 0;
  const abs = negative ? -n : n;
  const whole = (abs - (abs % 100)) / 100;
  const cents = abs % 100;
  return `${negative ? "-" : ""}${whole.toLocaleString("en-US")}.${String(cents).padStart(2, "0")}`;
}

/** A whole number in `[min, max]`, or a FieldError naming the field. */
function intField(fields, name, min, max) {
  const raw = String(fields[name] ?? "").trim();
  if (raw === "") throw new FieldError(name, "required");
  if (!/^\d+$/.test(raw)) throw new FieldError(name, "whole number only");
  const n = Number.parseInt(raw, 10);
  if (n < min || n > max) throw new FieldError(name, `must be between ${min} and ${max}`);
  return n;
}

/** `toMinor` with the failure attributed to the field it came from. */
function minorField(fields, name) {
  try {
    return toMinor(fields[name]);
  } catch (err) {
    throw new FieldError(name, err.message);
  }
}

/* --------------------------------------------------------------------- api */

class ApiError extends Error {
  constructor(status, body) {
    super(body?.message ?? `request failed (${status})`);
    this.status = status;
    this.code = body?.code ?? "UNKNOWN";
    this.details = body?.details ?? {};
  }

  /**
   * `details.errors[]` keyed by field name.
   *
   * `loc` is a pydantic location tuple joined with "." — "event.amount_minor", or
   * "version.savings_bps", or with a discriminated union in the path. The last segment
   * is the field, which is what the form labels its inputs.
   */
  fieldErrors() {
    const errors = this.details?.errors;
    if (!Array.isArray(errors)) return {};
    const out = {};
    for (const entry of errors) {
      const key = String(entry.loc ?? "").split(".").pop();
      if (key && !(key in out)) out[key] = entry.msg ?? "invalid";
    }
    return out;
  }
}

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(API + path, options);
  const text = await response.text();
  const parsed = text === "" ? null : JSON.parse(text);
  if (!response.ok) throw new ApiError(response.status, parsed);
  return { status: response.status, data: parsed };
}

/** Append one event. Returns `{event_id, dedupe_key, deduplicated}`. */
async function postEvent(event) {
  const { data } = await api("POST", "/events", { event, client_nonce: null });
  return data;
}

/* --------------------------------------------------------------------- dom */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function flash(kind, message, code) {
  const box = $("#flash");
  box.className = `flash ${kind}`;
  box.replaceChildren(message, code ? el("code", {}, ` ${code}`) : null);
  box.hidden = false;
  if (kind === "ok") setTimeout(() => { box.hidden = true; }, 4000);
}

function clearErrors(form) {
  for (const node of $$(".err", form)) {
    node.textContent = "";
    node.classList.remove("show");
  }
  for (const node of $$(".bad", form)) node.classList.remove("bad");
}

function setFieldError(form, field, message) {
  const slot = $(`.err[data-err="${field}"]`, form);
  const input = $(`[name="${field}"]`, form);
  if (input) input.classList.add("bad");
  if (!slot) return false;
  slot.textContent = message;
  slot.classList.add("show");
  return true;
}

function showFormError(form, err) {
  if (err instanceof FieldError) {
    if (err.field && setFieldError(form, err.field, err.message)) return;
    flash("error", err.message);
    return;
  }
  if (err instanceof ApiError) {
    const fields = err.fieldErrors();
    let shown = 0;
    for (const [field, message] of Object.entries(fields)) {
      if (setFieldError(form, field, message)) shown += 1;
    }
    // A field-level message the form has no slot for would otherwise vanish, so the
    // flash still fires unless every error found a home.
    if (shown === 0 || shown < Object.keys(fields).length) {
      flash("error", err.message, err.code);
    }
    return;
  }
  flash("error", err.message ?? String(err));
}

/** Read every named control into a plain object of trimmed strings. */
function readFields(form) {
  const out = {};
  for (const input of $$("[name]", form)) out[input.name] = input.value.trim();
  return out;
}

/** Wire a form's submit, with error handling and a disabled button while in flight. */
function onSubmit(name, handler) {
  const form = $(`[data-form="${name}"]`);
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearErrors(form);
    const button = $('button[type="submit"]', form);
    button.disabled = true;
    try {
      await handler(form, readFields(form));
    } catch (err) {
      showFormError(form, err);
    } finally {
      button.disabled = false;
    }
  });
}

/** Report an append, distinguishing a write from an idempotent no-op. */
function reportAppend(result, message) {
  if (result.deduplicated) flash("info", "Already recorded — nothing was added.");
  else flash("ok", message);
}

/* ------------------------------------------------------------------- state */

const cache = { accounts: [], obligations: [] };

const today = () => new Date().toISOString().slice(0, 10);
const asOf = () => $("#as-of").value || today();
const asOfQuery = () => `as_of=${encodeURIComponent(asOf())}`;

const accountKind = (id) =>
  cache.accounts.find((account) => account.entity_id === id)?.kind ?? null;

function fillSelect(select, options, { placeholder } = {}) {
  const previous = select.value;
  select.replaceChildren(
    ...(options.length === 0 && placeholder
      ? [el("option", { value: "" }, placeholder)]
      : []),
    ...options.map(([value, label]) => el("option", { value }, label)),
  );
  if (options.some(([value]) => value === previous)) select.value = previous;
}

/* --------------------------------------------------------- setup: accounts */

onSubmit("account", async (form, fields) => {
  const isCard = fields.kind === "CREDIT_CARD";
  const version = {
    entity_id: fields.entity_id,
    name: fields.name,
    kind: fields.kind,
    apr_bps: minorField(fields, "apr_bps"),
    // No defaults on these two, so they are sent explicitly as null rather than
    // omitted — `extra="forbid"` cuts both ways.
    statement_close_day: isCard ? intField(fields, "statement_close_day", 1, 31) : null,
    payment_due_day: isCard ? intField(fields, "payment_due_day", 1, 31) : null,
    budget_timing: isCard ? fields.budget_timing : "AT_PURCHASE",
    effective_from: fields.effective_from,
    effective_to: null,
  };
  await api("POST", "/definitions/account", { version, close_previous_at: null });
  flash("ok", `Account "${fields.name}" added.`);
  form.reset();
  applyKindVisibility(form);
  await refreshAll();
});

/** Card-only fields appear only for a card, and are sent as null otherwise. */
function applyKindVisibility(form) {
  const block = $("[data-card-only]", form);
  if (block) block.hidden = $('[name="kind"]', form).value !== "CREDIT_CARD";
}

onSubmit("opening-balance", async (form, fields) => {
  const typed = minorField(fields, "amount_minor");
  // `balance_minor` is signed and liabilities are negative. The form asked "amount
  // owed" for a card, so the sign is applied here and the user never types a minus.
  const isLiability = LIABILITY_KINDS.has(accountKind(fields.account_id));
  const result = await postEvent({
    event_type: "AccountOpeningBalance",
    date: fields.date,
    account_id: fields.account_id,
    amount_minor: isLiability ? -typed : typed,
  });
  reportAppend(result, `Opening balance set for ${fields.account_id}.`);
  form.reset();
  setDefaultDates();
  await refreshAll();
});

/** Relabel the balance field so the sign convention never reaches the user. */
function applyBalanceLabel() {
  const form = $('[data-form="opening-balance"]');
  const kind = accountKind($('[name="account_id"]', form).value);
  const liability = LIABILITY_KINDS.has(kind);
  $("[data-balance-label]", form).childNodes[0].nodeValue = liability
    ? "Amount currently owed"
    : "Current balance";
  $("[data-balance-hint]", form).textContent = liability
    ? "What you owe today, as a positive number."
    : "What's in the account today.";
}

onSubmit("policy", async (form, fields) => {
  const savings = minorField(fields, "savings_bps");
  const discretionary = minorField(fields, "discretionary_bps");
  // Checked here as well as server-side: the API answers POLICY_BPS_NOT_10000, but a
  // round trip to be told the two numbers do not add up is a poor way to find out.
  if (savings + discretionary !== 10_000) {
    throw new FieldError(
      "savings_bps",
      `the two shares must total 100% — they currently total ${fromMinor(savings + discretionary)}%`,
    );
  }
  await api("POST", "/definitions/allocation-policy", {
    version: {
      entity_id: fields.entity_id,
      savings_bps: savings,
      discretionary_bps: discretionary,
      effective_from: fields.effective_from,
      effective_to: null,
    },
    close_previous_at: null,
  });
  flash("ok", "Allocation policy saved.");
  await refreshAll();
});

function updatePolicyTotal() {
  const form = $('[data-form="policy"]');
  const slot = $("[data-policy-total]", form);
  const fields = readFields(form);
  let total;
  try {
    total = toMinor(fields.savings_bps) + toMinor(fields.discretionary_bps);
  } catch {
    slot.textContent = "";
    slot.className = "total";
    return;
  }
  slot.textContent = `total ${fromMinor(total)}%`;
  slot.className = total === 10_000 ? "total good" : "total bad";
}

/* ------------------------------------------------------------- definitions */

onSubmit("fixed-cost", async (form, fields) => {
  await api("POST", "/definitions/fixed-cost", {
    version: {
      entity_id: fields.entity_id,
      name: fields.name,
      amount_minor: minorField(fields, "amount_minor"),
      cadence: fields.cadence,
      due_day: intField(fields, "due_day", 1, 31),
      payee: fields.payee,
      category: fields.category,
      effective_from: fields.effective_from,
      effective_to: null,
    },
    close_previous_at: null,
  });
  flash("ok", `"${fields.name}" added.`);
  form.reset();
  setDefaultDates();
  await refreshAll();
});

onSubmit("recurring-income", async (form, fields) => {
  await api("POST", "/definitions/recurring-income", {
    version: {
      entity_id: fields.entity_id,
      name: fields.name,
      amount_minor: minorField(fields, "amount_minor"),
      cadence: fields.cadence,
      anchor_day: intField(fields, "anchor_day", 1, 31),
      account_id: fields.account_id,
      effective_from: fields.effective_from,
      effective_to: null,
    },
    close_previous_at: null,
  });
  flash(
    "ok",
    `"${fields.name}" added as a forecast. Record the actual deposits under Record → Income.`,
  );
  form.reset();
  setDefaultDates();
  await refreshAll();
});

/* ------------------------------------------------------------------ events */

/** Optional text: absent rather than empty, since "" is a value and null is not. */
const optional = (value) => (value === "" ? undefined : value);

onSubmit("expense", async (form, fields) => {
  const result = await postEvent({
    event_type: "ExpenseRecorded",
    date: fields.date,
    amount_minor: minorField(fields, "amount_minor"),
    category: fields.category,
    account_id: fields.account_id,
    merchant: optional(fields.merchant),
    note: optional(fields.note),
  });
  reportAppend(result, "Expense recorded.");
  form.reset();
  setDefaultDates();
  await refreshAll();
});

onSubmit("income", async (form, fields) => {
  const result = await postEvent({
    event_type: "IncomeReceived",
    date: fields.date,
    amount_minor: minorField(fields, "amount_minor"),
    source: fields.source,
    account_id: fields.account_id,
    note: optional(fields.note),
  });
  reportAppend(result, "Income recorded.");
  form.reset();
  setDefaultDates();
  await refreshAll();
});

onSubmit("payment", async (form, fields) => {
  // `principal_minor` / `interest_minor` are deliberately not sent: they are both-or-
  // neither and must sum to `amount_minor`, so a partial split is PAYMENT_SPLIT_MISMATCH.
  const result = await postEvent({
    event_type: "PaymentMade",
    date: fields.date,
    amount_minor: minorField(fields, "amount_minor"),
    obligation_id: fields.obligation_id,
    account_id: fields.account_id,
    note: optional(fields.note),
  });
  reportAppend(result, "Payment recorded.");
  form.reset();
  setDefaultDates();
  await refreshAll();
});

/* --------------------------------------------------------------- rendering */

function table(columns, rows, emptyMessage) {
  if (rows.length === 0) return el("p", { class: "empty" }, emptyMessage);
  return el(
    "table",
    {},
    el("thead", {}, el("tr", {}, columns.map((c) =>
      el("th", { class: c.num ? "num" : null }, c.label)))),
    el("tbody", {}, rows.map((row) =>
      el("tr", { class: row.voided ? "voided" : null }, columns.map((c) => {
        const value = c.get(row.data ?? row);
        const negative = c.num && typeof value === "number" && value < 0;
        return el(
          "td",
          { class: [c.num ? "num" : "", negative ? "neg" : ""].join(" ").trim() || null },
          c.render ? c.render(row.data ?? row) : (c.num ? fromMinor(value) : value ?? "—"),
        );
      })))),
  );
}

async function renderAccounts() {
  const [{ data: definitions }, { data: balances }] = await Promise.all([
    api("GET", `/definitions/account?${asOfQuery()}`),
    api("GET", `/accounts?${asOfQuery()}`),
  ]);
  cache.accounts = definitions.versions;

  const options = cache.accounts.map((a) => [a.entity_id, `${a.name} (${a.entity_id})`]);
  for (const select of $$("select[data-accounts]")) {
    fillSelect(select, options, { placeholder: "— add an account first —" });
  }
  applyBalanceLabel();

  const byId = new Map(balances.accounts.map((b) => [b.account_id, b]));
  const rows = cache.accounts.map((a) => ({ ...a, balance: byId.get(a.entity_id) }));

  $('[data-list="accounts"]').replaceChildren(
    table(
      [
        { label: "Id", get: (r) => r.entity_id },
        { label: "Name", get: (r) => r.name },
        { label: "Kind", get: (r) => r.kind },
        { label: "APR", get: (r) => r.apr_bps, render: (r) => `${fromMinor(r.apr_bps)}%` },
        {
          label: "Balance",
          num: true,
          get: (r) => r.balance?.balance_minor ?? 0,
          render: (r) =>
            r.balance ? fromMinor(r.balance.balance_minor) : "—",
        },
        {
          label: "Outstanding",
          num: true,
          get: (r) => r.balance?.outstanding_minor ?? 0,
          render: (r) =>
            r.balance?.outstanding_minor === null || r.balance === undefined
              ? "—"
              : fromMinor(r.balance.outstanding_minor),
        },
      ],
      rows,
      "No accounts yet. Add one above to get started.",
    ),
  );

  $('[data-view="balances"]').replaceChildren(
    table(
      [
        { label: "Account", get: (r) => r.name },
        { label: "Kind", get: (r) => r.kind },
        { label: "Balance", num: true, get: (r) => r.balance_minor },
        {
          label: "Outstanding",
          num: true,
          get: (r) => r.outstanding_minor ?? 0,
          render: (r) =>
            r.outstanding_minor === null ? "—" : fromMinor(r.outstanding_minor),
        },
        { label: "Interest to date", num: true, get: (r) => r.cumulative_interest_minor },
      ],
      balances.accounts,
      "No accounts yet.",
    ),
  );
}

async function renderDefinitionList(kind, columns) {
  const { data } = await api("GET", `/definitions/${kind}?${asOfQuery()}`);
  $(`[data-list="${kind}"]`).replaceChildren(
    table(columns, data.versions, "Nothing here yet."),
  );
}

function renderPeriod(period) {
  const stat = (label, value, { signed = false } = {}) =>
    el(
      "div",
      { class: "stat" },
      el("dt", {}, label),
      el("dd", { class: signed && value < 0 ? "neg" : null }, fromMinor(value)),
    );

  if (!period) {
    return el("p", { class: "empty" }, "No period data yet — record some income to begin.");
  }
  return el(
    "dl",
    { class: "stats" },
    stat("Income", period.income_minor),
    stat("Allocatable", period.allocatable_income_minor, { signed: true }),
    stat("Fixed due", period.fixed_due_minor),
    stat("Fixed outstanding", period.fixed_outstanding_minor),
    stat("To savings", period.savings_allocated_minor, { signed: true }),
    stat("Discretionary allocated", period.discretionary_allocated_minor, { signed: true }),
    stat("Discretionary spent", period.discretionary_spent_minor),
    stat("Discretionary left", period.discretionary_remaining_minor, { signed: true }),
  );
}

function renderWarnings(warnings) {
  if (warnings.length === 0) return el("div");
  // Warnings are data, never failures (CLAUDE.md §6). A half-populated ledger produces
  // them as a matter of course, so they are styled as notices and never as errors.
  return el(
    "div",
    { class: "warnings" },
    el("h3", {}, `${warnings.length} thing${warnings.length === 1 ? "" : "s"} worth knowing`),
    el("ul", {}, warnings.map((w) =>
      el("li", {}, w.message, " ", el("code", {}, w.code)))),
  );
}

async function renderOverview() {
  const { data: state } = await api("GET", `/state?${asOfQuery()}`);
  cache.obligations = state.obligations;

  const current = state.periods.find((p) => p.period_id === state.current_period_id);
  $('[data-view="period-id"]').textContent = state.current_period_id ?? "";
  $('[data-view="period"]').replaceChildren(renderPeriod(current));

  $('[data-view="obligations"]').replaceChildren(
    table(
      [
        { label: "Payee", get: (r) => r.payee },
        { label: "Due", get: (r) => r.due_date },
        { label: "Category", get: (r) => r.category },
        { label: "Amount", num: true, get: (r) => r.amount_minor },
        { label: "Remaining", num: true, get: (r) => r.remaining_minor },
        {
          label: "Status",
          get: (r) => r.status,
          render: (r) => el("span", { class: `badge ${r.status}` }, r.status.replace("_", " ")),
        },
      ],
      state.obligations,
      "No obligations yet. Add a monthly expense under Recurring.",
    ),
  );

  $('[data-view="warnings"]').replaceChildren(renderWarnings(state.warnings));

  const unpaid = state.obligations.filter((o) => o.status !== "PAID");
  fillSelect(
    $('[data-form="payment"] select[data-obligations]'),
    unpaid.map((o) => [
      o.obligation_id,
      `${o.payee} — ${o.due_date} — ${fromMinor(o.remaining_minor)} left`,
    ]),
    { placeholder: "— nothing outstanding —" },
  );
  applyObligationAmount();
}

/** Pre-fill the payment amount with what is actually left on the obligation. */
function applyObligationAmount() {
  const form = $('[data-form="payment"]');
  const id = $("[data-obligations]", form).value;
  const obligation = cache.obligations.find((o) => o.obligation_id === id);
  const hint = $("[data-obligation-hint]", form);
  if (!obligation) {
    hint.textContent = "";
    return;
  }
  $('[name="amount_minor"]', form).value = fromMinor(obligation.remaining_minor);
  hint.textContent = `${obligation.category} · ${fromMinor(obligation.amount_minor)} due, ${fromMinor(obligation.remaining_minor)} remaining`;
}

async function renderLedger() {
  const { data } = await api("GET", "/ledger?limit=25");
  const rows = data.rows.map((row) => ({ data: row, voided: row.is_voided }));
  $('[data-view="ledger"]').replaceChildren(
    table(
      [
        { label: "Date", get: (r) => r.date },
        { label: "Type", get: (r) => r.event_type },
        { label: "Who", get: (r) => r.counterparty ?? "—" },
        { label: "Category", get: (r) => r.category ?? "—" },
        { label: "Account", get: (r) => r.account_id ?? "—" },
        {
          label: "Amount",
          num: true,
          get: (r) => r.amount_minor ?? 0,
          render: (r) => (r.amount_minor === null ? "—" : fromMinor(r.amount_minor)),
        },
        {
          label: "",
          get: (r) => r.event_id,
          render: (r) =>
            r.is_voided
              ? el("span", { class: "muted" }, "voided")
              : el("button", {
                  class: "ghost",
                  type: "button",
                  onclick: () => voidEvent(r),
                }, "Void"),
        },
      ],
      rows,
      "Nothing recorded yet.",
    ),
  );
}

/**
 * Correct a mistake. There are no edits and no deletes in this system — a correction is
 * a new `EventVoided` appended on top (CLAUDE.md §4.3), which is why this asks for a
 * reason and why the original row stays visible afterwards.
 */
async function voidEvent(row) {
  const reason = window.prompt(
    `Void this ${row.event_type} from ${row.date}?\n\nThe entry stays in the ledger, struck through. Why?`,
    "entered by mistake",
  );
  if (reason === null || reason.trim() === "") return;
  try {
    await api("POST", `/events/${row.event_id}/void`, { reason: reason.trim() });
    flash("ok", "Voided.");
    await refreshAll();
  } catch (err) {
    flash("error", err.message, err.code);
  }
}

async function renderChart() {
  const series = await Promise.all(
    [
      ["discretionary_spent", "Spent"],
      ["discretionary_remaining", "Remaining"],
    ].map(async ([metric, label]) => {
      const { data } = await api(
        "GET",
        `/charts/series?metric=${metric}&grain=period&group_by=none&${asOfQuery()}`,
      );
      return { label, points: data.points };
    }),
  );
  renderSeries($('[data-view="chart"]'), series, { format: fromMinor });
}

/* ------------------------------------------------------------------ wiring */

async function refreshAll() {
  try {
    await renderAccounts();
    await Promise.all([
      renderDefinitionList("fixed-cost", [
        { label: "Name", get: (r) => r.name },
        { label: "Amount", num: true, get: (r) => r.amount_minor },
        { label: "Cadence", get: (r) => r.cadence },
        { label: "Due day", num: true, get: (r) => r.due_day, render: (r) => r.due_day },
        { label: "Payee", get: (r) => r.payee },
        { label: "Category", get: (r) => r.category },
        { label: "From", get: (r) => r.effective_from },
      ]),
      renderDefinitionList("recurring-income", [
        { label: "Name", get: (r) => r.name },
        { label: "Amount", num: true, get: (r) => r.amount_minor },
        { label: "Cadence", get: (r) => r.cadence },
        { label: "Day", num: true, get: (r) => r.anchor_day, render: (r) => r.anchor_day },
        { label: "Account", get: (r) => r.account_id },
        { label: "From", get: (r) => r.effective_from },
      ]),
      renderOverview(),
      renderLedger(),
    ]);
    await renderChart();
  } catch (err) {
    flash("error", err.message ?? String(err), err.code);
  }
}

function setDefaultDates() {
  for (const input of $$('input[type="date"]')) {
    if (input.value === "" && input.id !== "as-of") input.value = today();
  }
}

function init() {
  $("#as-of").value = today();

  for (const select of $$("select[data-cadence]")) fillSelect(select, CADENCES);
  setDefaultDates();

  for (const tab of $$(".tabs button")) {
    tab.addEventListener("click", () => {
      for (const other of $$(".tabs button")) other.classList.toggle("active", other === tab);
      for (const panel of $$("[data-panel]")) {
        panel.hidden = panel.dataset.panel !== tab.dataset.tab;
      }
      // A hidden element has no width, so a chart drawn while Overview was closed sized
      // itself against zero and fell back to its default. Redraw now that it can measure.
      if (tab.dataset.tab === "overview") renderChart().catch(() => {});
    });
  }

  const accountForm = $('[data-form="account"]');
  $('[name="kind"]', accountForm).addEventListener("change", () =>
    applyKindVisibility(accountForm));
  applyKindVisibility(accountForm);

  $('[data-form="opening-balance"] [name="account_id"]')
    .addEventListener("change", applyBalanceLabel);

  const policyForm = $('[data-form="policy"]');
  for (const name of ["savings_bps", "discretionary_bps"]) {
    $(`[name="${name}"]`, policyForm).addEventListener("input", updatePolicyTotal);
  }
  updatePolicyTotal();

  $('[data-form="payment"] [data-obligations]')
    .addEventListener("change", applyObligationAmount);

  $("#as-of").addEventListener("change", refreshAll);
  $('[data-action="refresh"]').addEventListener("click", refreshAll);

  refreshAll();
}

init();
