import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { CADENCES, today } from "../lib/constants";
import { intField, minorField } from "../lib/money";
import type { FixedCostVersion, RecurringIncomeVersion } from "../lib/types";
import {
  flashFromUnknown,
  useBudget,
  useLoadAccounts,
} from "../context/BudgetContext";
import { AccountSelect } from "../components/AccountSelect";
import { Field } from "../components/Field";
import { FormShell } from "../components/FormShell";
import { Table, type Column } from "../components/Table";

const TABS = [
  { id: "income", label: "Income" },
  { id: "expenses", label: "Expenses" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function RecurringPage() {
  const { asOfQuery, showFlash, refreshAll, refreshKey } = useBudget();
  const accounts = useLoadAccounts();
  const [tab, setTab] = useState<TabId>("income");
  const [fixedCosts, setFixedCosts] = useState<FixedCostVersion[]>([]);
  const [incomes, setIncomes] = useState<RecurringIncomeVersion[]>([]);
  const [incomeAccountId, setIncomeAccountId] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [fixed, income] = await Promise.all([
          api<{ versions: FixedCostVersion[] }>("GET", `/definitions/fixed-cost?${asOfQuery}`),
          api<{ versions: RecurringIncomeVersion[] }>(
            "GET",
            `/definitions/recurring-income?${asOfQuery}`,
          ),
        ]);
        if (!cancelled) {
          setFixedCosts(fixed.data.versions);
          setIncomes(income.data.versions);
        }
      } catch (err) {
        if (!cancelled) flashFromUnknown(showFlash, err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [asOfQuery, refreshKey, showFlash]);

  useEffect(() => {
    if (accounts.length === 0) {
      setIncomeAccountId("");
      return;
    }
    if (!accounts.some((a) => a.entity_id === incomeAccountId)) {
      setIncomeAccountId(accounts[0].entity_id);
    }
  }, [accounts, incomeAccountId]);

  const fixedColumns: Column<FixedCostVersion>[] = [
    { label: "Name", get: (r) => r.name },
    { label: "Amount", num: true, get: (r) => r.amount_minor },
    { label: "Cadence", get: (r) => r.cadence },
    {
      label: "Due day",
      num: true,
      get: (r) => r.due_day,
      render: (r) => r.due_day,
    },
    { label: "Payee", get: (r) => r.payee },
    { label: "Category", get: (r) => r.category },
    { label: "From", get: (r) => r.effective_from },
  ];

  const incomeColumns: Column<RecurringIncomeVersion>[] = [
    { label: "Name", get: (r) => r.name },
    { label: "Amount", num: true, get: (r) => r.amount_minor },
    { label: "Cadence", get: (r) => r.cadence },
    {
      label: "Day",
      num: true,
      get: (r) => r.anchor_day,
      render: (r) => r.anchor_day,
    },
    { label: "Account", get: (r) => r.account_id },
    { label: "From", get: (r) => r.effective_from },
  ];

  return (
    <section>
      <nav className="tabs full" role="tablist">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`recurring-tab-${item.id}`}
            aria-selected={tab === item.id}
            aria-controls={`recurring-panel-${item.id}`}
            className={tab === item.id ? "active" : undefined}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <div
        role="tabpanel"
        id="recurring-panel-income"
        aria-labelledby="recurring-tab-income"
        hidden={tab !== "income"}
      >
        <div className="table-wrap">
          <Table columns={incomeColumns} rows={incomes} emptyMessage="No Income Yet." />
        </div>

        <h2>Recurring income</h2>
        <div className="grid full">
          <FormShell
            onSubmit={async (fields) => {
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
              showFlash(
                "ok",
                `"${fields.name}" added as a forecast. Record the actual deposits under Record → Income.`,
              );
              refreshAll();
            }}
          >
            {({ errors, busy }) => (
              <>
                <p className="note">
                  Forecast only. This does <strong>not</strong> create allocatable income —
                  only an actual income event does. Record your paychecks under{" "}
                  <em>Record → Income</em> or the budget will show zero.
                </p>
                <Field label="Id" name="entity_id" required placeholder="salary" error={errors.entity_id} />
                <Field label="Name" name="name" required placeholder="Salary" error={errors.name} />
                <Field
                  label="Amount"
                  name="amount_minor"
                  inputMode="decimal"
                  required
                  placeholder="4,000.00"
                  error={errors.amount_minor}
                />
                <Field label="Cadence" name="cadence" as="select" defaultValue="MONTHLY" options={CADENCES} />
                <Field
                  label="Arrives on day"
                  name="anchor_day"
                  inputMode="numeric"
                  required
                  defaultValue="1"
                  error={errors.anchor_day}
                />
                <label>
                  Into account
                  <AccountSelect
                    value={incomeAccountId}
                    onChange={setIncomeAccountId}
                    accounts={accounts}
                    required
                    error={Boolean(errors.account_id)}
                  />
                  <small className={`err${errors.account_id ? " show" : ""}`}>
                    {errors.account_id ?? ""}
                  </small>
                </label>
                <Field
                  label="Effective from"
                  name="effective_from"
                  type="date"
                  required
                  defaultValue={today()}
                  error={errors.effective_from}
                />
                <button type="submit" disabled={busy}>
                  Add recurring income
                </button>
              </>
            )}
          </FormShell>
        </div>
      </div>

      <div
        role="tabpanel"
        id="recurring-panel-expenses"
        aria-labelledby="recurring-tab-expenses"
        hidden={tab !== "expenses"}
      >
        <div className="table-wrap">
          <Table columns={fixedColumns} rows={fixedCosts} emptyMessage="No Expenses Yet." />
        </div>

        <h2>Monthly expenses</h2>
        <div className="grid full">
          <FormShell
            onSubmit={async (fields) => {
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
              showFlash("ok", `"${fields.name}" added.`);
              refreshAll();
            }}
          >
            {({ errors, busy }) => (
              <>
                <p className="hint">
                  Rent, utilities, subscriptions — anything that comes due on a schedule.
                  The projection turns each one into an obligation you can pay.
                </p>
                <Field label="Id" name="entity_id" required placeholder="rent" error={errors.entity_id} />
                <Field label="Name" name="name" required placeholder="Rent" error={errors.name} />
                <Field
                  label="Amount"
                  name="amount_minor"
                  inputMode="decimal"
                  required
                  placeholder="1,800.00"
                  error={errors.amount_minor}
                />
                <Field label="Cadence" name="cadence" as="select" defaultValue="MONTHLY" options={CADENCES} />
                <Field
                  label="Due on day"
                  name="due_day"
                  inputMode="numeric"
                  required
                  defaultValue="1"
                  error={errors.due_day}
                  hint={<small className="hint">1–31, clamped to the length of the month.</small>}
                />
                <Field label="Payee" name="payee" required placeholder="Landlord" error={errors.payee} />
                <Field
                  label="Category"
                  name="category"
                  required
                  placeholder="housing"
                  error={errors.category}
                />
                <Field
                  label="Effective from"
                  name="effective_from"
                  type="date"
                  required
                  defaultValue={today()}
                  error={errors.effective_from}
                />
                <button type="submit" disabled={busy}>
                  Add monthly expense
                </button>
              </>
            )}
          </FormShell>
        </div>
      </div>
    </section>
  );
}
