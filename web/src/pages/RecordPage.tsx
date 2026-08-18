import { useEffect, useState } from "react";
import { api, postEvent } from "../lib/api";
import { optional, today } from "../lib/constants";
import { FieldError, fromMinor, minorField } from "../lib/money";
import type { Obligation, ProjectedState } from "../lib/types";
import {
  flashFromUnknown,
  reportAppend,
  useBudget,
  useLoadAccounts,
} from "../context/BudgetContext";
import { AccountSelect } from "../components/AccountSelect";
import { AlterEventForm } from "../components/AlterEventForm";
import { Field } from "../components/Field";
import { FormShell } from "../components/FormShell";

const TABS = [
  { id: "income", label: "Income" },
  { id: "expense", label: "Expense" },
  { id: "payment", label: "Pay a bill" },
  { id: "transfer", label: "Transfer" },
  { id: "alter", label: "Alter" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function RecordPage() {
  const {
    asOfQuery,
    showFlash,
    refreshAll,
    refreshKey,
    obligations,
    setObligations,
  } = useBudget();
  const accounts = useLoadAccounts();
  const [tab, setTab] = useState<TabId>("income");
  const [expenseAccountId, setExpenseAccountId] = useState("");
  const [incomeAccountId, setIncomeAccountId] = useState("");
  const [paymentAccountId, setPaymentAccountId] = useState("");
  const [fromAccountId, setFromAccountId] = useState("");
  const [toAccountId, setToAccountId] = useState("");
  const [obligationId, setObligationId] = useState("");
  const [paymentAmount, setPaymentAmount] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api<ProjectedState>("GET", `/state?${asOfQuery}`);
        if (!cancelled) setObligations(data.obligations);
      } catch (err) {
        if (!cancelled) flashFromUnknown(showFlash, err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [asOfQuery, refreshKey, setObligations, showFlash]);

  useEffect(() => {
    if (accounts.length === 0) {
      setExpenseAccountId("");
      setIncomeAccountId("");
      setPaymentAccountId("");
      setFromAccountId("");
      setToAccountId("");
      return;
    }
    const first = accounts[0].entity_id;
    const second = accounts.length > 1 ? accounts[1].entity_id : first;
    if (!accounts.some((a) => a.entity_id === expenseAccountId)) setExpenseAccountId(first);
    if (!accounts.some((a) => a.entity_id === incomeAccountId)) setIncomeAccountId(first);
    if (!accounts.some((a) => a.entity_id === paymentAccountId)) setPaymentAccountId(first);
    if (!accounts.some((a) => a.entity_id === fromAccountId)) setFromAccountId(first);
    if (!accounts.some((a) => a.entity_id === toAccountId)) setToAccountId(second);
  }, [
    accounts,
    expenseAccountId,
    incomeAccountId,
    paymentAccountId,
    fromAccountId,
    toAccountId,
  ]);

  const unpaid = obligations.filter((o) => o.status !== "PAID");

  useEffect(() => {
    const unpaidList = obligations.filter((o) => o.status !== "PAID");
    if (unpaidList.length === 0) {
      setObligationId("");
      setPaymentAmount("");
      return;
    }
    setObligationId((currentId) => {
      const current = unpaidList.find((o) => o.obligation_id === currentId);
      const next = current ?? unpaidList[0];
      setPaymentAmount(fromMinor(next.remaining_minor));
      return next.obligation_id;
    });
  }, [obligations]);

  const selectedObligation: Obligation | undefined = unpaid.find(
    (o) => o.obligation_id === obligationId,
  );

  function selectObligation(id: string) {
    setObligationId(id);
    const obligation = unpaid.find((o) => o.obligation_id === id);
    if (obligation) setPaymentAmount(fromMinor(obligation.remaining_minor));
  }

  return (
    <section>
      <nav className="tabs full" role="tablist">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`record-tab-${item.id}`}
            aria-selected={tab === item.id}
            aria-controls={`record-panel-${item.id}`}
            className={tab === item.id ? "active" : undefined}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <div
        role="tabpanel"
        id="record-panel-income"
        aria-labelledby="record-tab-income"
        hidden={tab !== "income"}
      >
        <div className="grid full">
          <FormShell
            onSubmit={async (fields) => {
              const result = await postEvent({
                event_type: "IncomeReceived",
                date: fields.date,
                amount_minor: minorField(fields, "amount_minor"),
                source: fields.source,
                account_id: fields.account_id,
                note: optional(fields.note),
              });
              reportAppend(showFlash, result, "Income recorded.");
              refreshAll();
            }}
          >
            {({ errors, busy }) => (
              <>
                <h2>Income</h2>
                <p className="hint">
                  A paycheck or anything else that arrived. <strong>This</strong> is what
                  creates allocatable income.
                </p>
                <Field
                  label="Date"
                  name="date"
                  type="date"
                  required
                  defaultValue={today()}
                  error={errors.date}
                />
                <Field
                  label="Amount"
                  name="amount_minor"
                  inputMode="decimal"
                  required
                  placeholder="4,000.00"
                  error={errors.amount_minor}
                />
                <Field
                  label="Source"
                  name="source"
                  required
                  placeholder="Employer"
                  error={errors.source}
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
                  label={
                    <>
                      Note <span className="opt">optional</span>
                    </>
                  }
                  name="note"
                  error={errors.note}
                />
                <button type="submit" disabled={busy}>
                  Record income
                </button>
              </>
            )}
          </FormShell>
        </div>
      </div>

      <div
        role="tabpanel"
        id="record-panel-expense"
        aria-labelledby="record-tab-expense"
        hidden={tab !== "expense"}
      >
        <div className="grid full">
          <FormShell
            onSubmit={async (fields) => {
              const result = await postEvent({
                event_type: "ExpenseRecorded",
                date: fields.date,
                amount_minor: minorField(fields, "amount_minor"),
                category: fields.category,
                account_id: fields.account_id,
                merchant: optional(fields.merchant),
                note: optional(fields.note),
              });
              reportAppend(showFlash, result, "Expense recorded.");
              refreshAll();
            }}
          >
            {({ errors, busy }) => (
              <>
                <h2>Expense</h2>
                <p className="hint">A one&#8209;off purchase. A negative amount is a refund.</p>
                <Field
                  label="Date"
                  name="date"
                  type="date"
                  required
                  defaultValue={today()}
                  error={errors.date}
                />
                <Field
                  label="Amount"
                  name="amount_minor"
                  inputMode="decimal"
                  required
                  placeholder="19.99"
                  error={errors.amount_minor}
                />
                <Field
                  label="Category"
                  name="category"
                  required
                  placeholder="groceries"
                  error={errors.category}
                />
                <label>
                  Paid from
                  <AccountSelect
                    value={expenseAccountId}
                    onChange={setExpenseAccountId}
                    accounts={accounts}
                    required
                    error={Boolean(errors.account_id)}
                  />
                  <small className={`err${errors.account_id ? " show" : ""}`}>
                    {errors.account_id ?? ""}
                  </small>
                </label>
                <Field
                  label={
                    <>
                      Merchant <span className="opt">optional</span>
                    </>
                  }
                  name="merchant"
                  placeholder="Corner Store"
                  error={errors.merchant}
                />
                <Field
                  label={
                    <>
                      Note <span className="opt">optional</span>
                    </>
                  }
                  name="note"
                  error={errors.note}
                />
                <button type="submit" disabled={busy}>
                  Record expense
                </button>
              </>
            )}
          </FormShell>
        </div>
      </div>

      <div
        role="tabpanel"
        id="record-panel-payment"
        aria-labelledby="record-tab-payment"
        hidden={tab !== "payment"}
      >
        <div className="grid full">
          <FormShell
            onSubmit={async (fields) => {
              const result = await postEvent({
                event_type: "PaymentMade",
                date: fields.date,
                amount_minor: minorField(fields, "amount_minor"),
                obligation_id: fields.obligation_id,
                account_id: fields.account_id,
                note: optional(fields.note),
              });
              reportAppend(showFlash, result, "Payment recorded.");
              refreshAll();
            }}
          >
            {({ errors, busy }) => (
              <>
                <h2>Pay a bill</h2>
                <p className="hint">
                  Settles an obligation raised by a monthly expense. This does not reduce
                  discretionary again — the expense was already recognised when it came due.
                </p>
                <Field
                  label="Date"
                  name="date"
                  type="date"
                  required
                  defaultValue={today()}
                  error={errors.date}
                />
                <label>
                  Obligation
                  <select
                    name="obligation_id"
                    value={obligationId}
                    required
                    className={errors.obligation_id ? "bad" : undefined}
                    onChange={(event) => selectObligation(event.target.value)}
                  >
                    {unpaid.length === 0 ? (
                      <option value="">— nothing outstanding —</option>
                    ) : null}
                    {unpaid.map((o) => (
                      <option key={o.obligation_id} value={o.obligation_id}>
                        {o.payee} — {o.due_date} — {fromMinor(o.remaining_minor)} left
                      </option>
                    ))}
                  </select>
                  <small className="hint">
                    {selectedObligation
                      ? `${selectedObligation.category} · ${fromMinor(selectedObligation.amount_minor)} due, ${fromMinor(selectedObligation.remaining_minor)} remaining`
                      : ""}
                  </small>
                  <small className={`err${errors.obligation_id ? " show" : ""}`}>
                    {errors.obligation_id ?? ""}
                  </small>
                </label>
                <Field
                  label="Amount"
                  name="amount_minor"
                  inputMode="decimal"
                  required
                  value={paymentAmount}
                  onChange={(event) => setPaymentAmount(event.target.value)}
                  error={errors.amount_minor}
                />
                <label>
                  Paid from
                  <AccountSelect
                    value={paymentAccountId}
                    onChange={setPaymentAccountId}
                    accounts={accounts}
                    required
                    error={Boolean(errors.account_id)}
                  />
                  <small className={`err${errors.account_id ? " show" : ""}`}>
                    {errors.account_id ?? ""}
                  </small>
                </label>
                <Field
                  label={
                    <>
                      Note <span className="opt">optional</span>
                    </>
                  }
                  name="note"
                  error={errors.note}
                />
                <button type="submit" disabled={busy}>
                  Record payment
                </button>
              </>
            )}
          </FormShell>
        </div>
      </div>

      <div
        role="tabpanel"
        id="record-panel-transfer"
        aria-labelledby="record-tab-transfer"
        hidden={tab !== "transfer"}
      >
        <div className="grid full">
          <FormShell
            onSubmit={async (fields) => {
              if (fields.from_account_id === fields.to_account_id) {
                throw new FieldError(
                  "from_account_id",
                  "from and to must differ",
                );
              }
              const result = await postEvent({
                event_type: "TransferMade",
                date: fields.date,
                amount_minor: minorField(fields, "amount_minor"),
                from_account_id: fields.from_account_id,
                to_account_id: fields.to_account_id,
                note: optional(fields.note),
              });
              reportAppend(showFlash, result, "Transfer recorded.");
              refreshAll();
            }}
          >
            {({ errors, busy }) => (
              <>
                <h2>Transfer</h2>
                <p className="hint">
                  Moves money between your own accounts. This does not reduce
                  discretionary — paying a card bill is a transfer, not an expense.
                </p>
                <Field
                  label="Date"
                  name="date"
                  type="date"
                  required
                  defaultValue={today()}
                  error={errors.date}
                />
                <Field
                  label="Amount"
                  name="amount_minor"
                  inputMode="decimal"
                  required
                  placeholder="500.00"
                  error={errors.amount_minor}
                />
                <label>
                  From
                  <AccountSelect
                    name="from_account_id"
                    value={fromAccountId}
                    onChange={setFromAccountId}
                    accounts={accounts}
                    required
                    error={Boolean(errors.from_account_id)}
                  />
                  <small className={`err${errors.from_account_id ? " show" : ""}`}>
                    {errors.from_account_id ?? ""}
                  </small>
                </label>
                <label>
                  To
                  <AccountSelect
                    name="to_account_id"
                    value={toAccountId}
                    onChange={setToAccountId}
                    accounts={accounts}
                    required
                    error={Boolean(errors.to_account_id)}
                  />
                  <small className={`err${errors.to_account_id ? " show" : ""}`}>
                    {errors.to_account_id ?? ""}
                  </small>
                </label>
                <Field
                  label={
                    <>
                      Note <span className="opt">optional</span>
                    </>
                  }
                  name="note"
                  error={errors.note}
                />
                <button type="submit" disabled={busy}>
                  Record transfer
                </button>
              </>
            )}
          </FormShell>
        </div>
      </div>

      <div
        role="tabpanel"
        id="record-panel-alter"
        aria-labelledby="record-tab-alter"
        hidden={tab !== "alter"}
      >
        <AlterEventForm />
      </div>
    </section>
  );
}
