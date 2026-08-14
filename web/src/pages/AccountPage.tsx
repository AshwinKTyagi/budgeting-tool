import { useEffect, useMemo, useState } from "react";
import { api, postEvent } from "../lib/api";
import { LIABILITY_KINDS, today } from "../lib/constants";
import { fromMinor, intField, minorField } from "../lib/money";
import type { AccountBalance, AccountVersion } from "../lib/types";
import {
  flashFromUnknown,
  reportAppend,
  useBudget,
  useLoadAccounts,
} from "../context/BudgetContext";
import { AccountSelect } from "../components/AccountSelect";
import { Field } from "../components/Field";
import { FormShell } from "../components/FormShell";
import { Table, type Column } from "../components/Table";

type AccountRow = AccountVersion & { balance?: AccountBalance };

const TABS = [
  { id: "create", label: "Add account" },
  { id: "balance", label: "Opening balance" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function AccountPage() {
  const { asOfQuery, showFlash, refreshAll, refreshKey, accountKind } = useBudget();
  const accounts = useLoadAccounts();
  const [tab, setTab] = useState<TabId>("create");
  const [balances, setBalances] = useState<AccountBalance[]>([]);
  const [kind, setKind] = useState("CHECKING");
  const [openingAccountId, setOpeningAccountId] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api<{ accounts: AccountBalance[] }>(
          "GET",
          `/accounts?${asOfQuery}`,
        );
        if (!cancelled) setBalances(data.accounts);
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
      setOpeningAccountId("");
      return;
    }
    if (!accounts.some((a) => a.entity_id === openingAccountId)) {
      setOpeningAccountId(accounts[0].entity_id);
    }
  }, [accounts, openingAccountId]);

  const isCard = kind === "CREDIT_CARD";
  const createLiability = LIABILITY_KINDS.has(kind);
  const openingKind = accountKind(openingAccountId);
  const liability = LIABILITY_KINDS.has(openingKind ?? "");

  const rows: AccountRow[] = useMemo(() => {
    const byId = new Map(balances.map((b) => [b.account_id, b]));
    return accounts.map((a) => ({ ...a, balance: byId.get(a.entity_id) }));
  }, [accounts, balances]);

  const columns: Column<AccountRow>[] = [
    { label: "Id", get: (r) => r.entity_id },
    { label: "Name", get: (r) => r.name },
    { label: "Kind", get: (r) => r.kind },
    {
      label: "APR",
      get: (r) => r.apr_bps,
      render: (r) => `${fromMinor(r.apr_bps)}%`,
    },
    {
      label: "Balance",
      num: true,
      get: (r) => r.balance?.balance_minor ?? 0,
      render: (r) => (r.balance ? fromMinor(r.balance.balance_minor) : "—"),
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
  ];

  return (
    <section>
      <nav className="tabs full" role="tablist">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`account-tab-${item.id}`}
            aria-selected={tab === item.id}
            aria-controls={`account-panel-${item.id}`}
            className={tab === item.id ? "active" : undefined}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <div className="table-wrap">
        <Table
          columns={columns}
          rows={rows}
          emptyMessage="No accounts yet. Add one below to get started."
          />
      </div>

      <div
        role="tabpanel"
        id="account-panel-create"
        aria-labelledby="account-tab-create"
        hidden={tab !== "create"}
        >
        <h2>Add an account</h2>
        <div className="grid full">
          <FormShell
            onSubmit={async (fields) => {
              const card = fields.kind === "CREDIT_CARD";
              const opening =
                fields.amount_minor === "" ? null : minorField(fields, "amount_minor");
              const version = {
                entity_id: fields.entity_id,
                name: fields.name,
                kind: fields.kind,
                apr_bps: minorField(fields, "apr_bps"),
                statement_close_day: card
                  ? intField(fields, "statement_close_day", 1, 31)
                  : null,
                payment_due_day: card
                  ? intField(fields, "payment_due_day", 1, 31)
                  : null,
                budget_timing: card ? fields.budget_timing : "AT_PURCHASE",
                effective_from: fields.effective_from,
                effective_to: null,
              };
              await api("POST", "/definitions/account", {
                version,
                close_previous_at: null,
              });
              try {
                if (opening !== null) {
                  // The context still has the pre-create account list, so the kind comes
                  // from the submitted field rather than accountKind().
                  const isLiability = LIABILITY_KINDS.has(fields.kind);
                  await postEvent({
                    event_type: "AccountOpeningBalance",
                    date: fields.effective_from,
                    account_id: fields.entity_id,
                    amount_minor: isLiability ? -opening : opening,
                  });
                }
                showFlash(
                  "ok",
                  opening === null
                    ? `Account "${fields.name}" added.`
                    : `Account "${fields.name}" added with an opening balance.`,
                );
                setKind("CHECKING");
              } finally {
                refreshAll();
              }
            }}
          >
            {({ errors, busy }) => (
              <>
                <p className="hint">
                  Checking, savings, and cards all live here. The id is how you&apos;ll
                  refer to this account everywhere else — short and lowercase works best.
                </p>
                <Field
                  label="Account id"
                  name="entity_id"
                  required
                  placeholder="checking"
                  error={errors.entity_id}
                />
                <Field
                  label="Name"
                  name="name"
                  required
                  placeholder="Everyday Checking"
                  error={errors.name}
                />
                <Field
                  label="Kind"
                  name="kind"
                  as="select"
                  value={kind}
                  onChange={(event) => setKind(event.target.value)}
                >
                  <option value="CHECKING">Checking</option>
                  <option value="SAVINGS">Savings</option>
                  <option value="CREDIT_CARD">Credit card</option>
                  <option value="LOAN">Loan</option>
                </Field>
                <Field
                  label="APR %"
                  name="apr_bps"
                  inputMode="decimal"
                  defaultValue="0"
                  placeholder="21.99"
                  error={errors.apr_bps}
                  hint={<small className="hint">0 for anything that doesn&apos;t charge interest.</small>}
                />
                {isCard ? (
                  <div>
                    <Field
                      label="Statement closes on day"
                      name="statement_close_day"
                      inputMode="numeric"
                      placeholder="15"
                      error={errors.statement_close_day}
                    />
                    <Field
                      label="Payment due on day"
                      name="payment_due_day"
                      inputMode="numeric"
                      placeholder="10"
                      error={errors.payment_due_day}
                    />
                    <Field label="Budget timing" name="budget_timing" as="select" defaultValue="AT_PURCHASE">
                      <option value="AT_PURCHASE">
                        At purchase — a card swipe reduces discretionary now
                      </option>
                      <option value="AT_STATEMENT_PAYMENT">
                        At statement payment — when the bill is paid
                      </option>
                    </Field>
                  </div>
                ) : null}
                <Field
                  label="Effective from"
                  name="effective_from"
                  type="date"
                  required
                  defaultValue={today()}
                  error={errors.effective_from}
                />
                <Field
                  label={
                    createLiability
                      ? "Amount currently owed (optional)"
                      : "Opening balance (optional)"
                  }
                  name="amount_minor"
                  inputMode="decimal"
                  placeholder="2,500.00"
                  error={errors.amount_minor}
                  hint={
                    <small className="hint">
                      {createLiability
                        ? "What you owe today, as a positive number. "
                        : "What's in the account today. "}
                      Leave blank to skip — you can set it later on the Opening balance
                      tab.
                    </small>
                  }
                />
                <button type="submit" disabled={busy}>
                  Add account
                </button>
              </>
            )}
          </FormShell>
        </div>
      </div>

      <div
        role="tabpanel"
        id="account-panel-balance"
        aria-labelledby="account-tab-balance"
        hidden={tab !== "balance"}
      >
        <h2>Set an opening balance</h2>
        <div className="grid full">
          <FormShell
            onSubmit={async (fields) => {
              const typed = minorField(fields, "amount_minor");
              const isLiability = LIABILITY_KINDS.has(
                accountKind(fields.account_id) ?? "",
              );
              const result = await postEvent({
                event_type: "AccountOpeningBalance",
                date: fields.date,
                account_id: fields.account_id,
                amount_minor: isLiability ? -typed : typed,
              });
              reportAppend(
                showFlash,
                result,
                `Opening balance set for ${fields.account_id}.`,
              );
              refreshAll();
            }}
          >
            {({ errors, busy }) => (
              <>
                
                <p className="hint">
                  The starting point the ledger counts from. One per account — everything
                  after this is an event.
                </p>
                <label>
                  Account
                  <AccountSelect
                    value={openingAccountId}
                    onChange={setOpeningAccountId}
                    accounts={accounts}
                    required
                    error={Boolean(errors.account_id)}
                  />
                  <small className={`err${errors.account_id ? " show" : ""}`}>
                    {errors.account_id ?? ""}
                  </small>
                </label>
                <Field
                  label={liability ? "Amount currently owed" : "Current balance"}
                  name="amount_minor"
                  inputMode="decimal"
                  required
                  placeholder="2,500.00"
                  error={errors.amount_minor}
                  hint={
                    <small className="hint">
                      {liability
                        ? "What you owe today, as a positive number."
                        : "What's in the account today."}
                    </small>
                  }
                />
                <Field
                  label="As of date"
                  name="date"
                  type="date"
                  required
                  defaultValue={today()}
                  error={errors.date}
                />
                <button type="submit" disabled={busy}>
                  Set balance
                </button>
              </>
            )}
          </FormShell>
        </div>
      </div>
    </section>
  );
}
