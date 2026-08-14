import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { fromMinor } from "../lib/money";
import type { LedgerPage, LedgerRow } from "../lib/types";
import {
  flashFromUnknown,
  useBudget,
  useLoadAccounts,
} from "../context/BudgetContext";
import { AccountSelect } from "./AccountSelect";
import { Table, type Column } from "./Table";

const EVENT_TYPES = [
  "IncomeReceived",
  "GiftReceived",
  "ExpenseRecorded",
  "PaymentMade",
  "TransferMade",
  "SavingsDrawn",
  "AccountOpeningBalance",
  "InterestCharged",
  "InterestEarned",
  "ObligationRaised",
  "EventVoided",
] as const;

const PAGE_SIZE = 100;

type Filters = {
  from: string;
  to: string;
  eventType: string;
  accountId: string;
  category: string;
};

const EMPTY_FILTERS: Filters = {
  from: "",
  to: "",
  eventType: "",
  accountId: "",
  category: "",
};

function ledgerQuery(filters: Filters, cursor: string | null): string {
  const params = new URLSearchParams();
  params.set("limit", String(PAGE_SIZE));
  if (filters.from) params.set("from", filters.from);
  if (filters.to) params.set("to", filters.to);
  if (filters.eventType) params.set("types", filters.eventType);
  if (filters.accountId) params.set("account_id", filters.accountId);
  if (filters.category) params.set("category", filters.category);
  if (cursor) params.set("cursor", cursor);
  return params.toString();
}

type LedgerPanelProps = {
  active: boolean;
};

export function LedgerPanel({ active }: LedgerPanelProps) {
  const { showFlash, refreshAll, refreshKey } = useBudget();
  const accounts = useLoadAccounts();
  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<Filters>(EMPTY_FILTERS);
  const [rows, setRows] = useState<LedgerRow[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [totalCount, setTotalCount] = useState(0);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    (async () => {
      setBusy(true);
      try {
        const { data } = await api<LedgerPage>("GET", `/ledger?${ledgerQuery(applied, null)}`);
        if (cancelled) return;
        setRows(data.rows);
        setNextCursor(data.next_cursor);
        setTotalCount(data.total_count);
      } catch (err) {
        if (!cancelled) flashFromUnknown(showFlash, err);
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [active, applied, refreshKey, showFlash]);

  async function loadMore() {
    if (nextCursor === null || busy) return;
    setBusy(true);
    try {
      const { data } = await api<LedgerPage>("GET", `/ledger?${ledgerQuery(applied, nextCursor)}`);
      setRows((current) => [...current, ...data.rows]);
      setNextCursor(data.next_cursor);
      setTotalCount(data.total_count);
    } catch (err) {
      flashFromUnknown(showFlash, err);
    } finally {
      setBusy(false);
    }
  }

  async function voidEvent(row: LedgerRow) {
    const reason = window.prompt(
      `Void this ${row.event_type} from ${row.date}?\n\nThe entry stays in the ledger, struck through. Why?`,
      "entered by mistake",
    );
    if (reason === null || reason.trim() === "") return;
    try {
      await api("POST", `/events/${row.event_id}/void`, { reason: reason.trim() });
      showFlash("ok", "Voided.");
      refreshAll();
    } catch (err) {
      flashFromUnknown(showFlash, err);
    }
  }

  const columns: Column<LedgerRow>[] = [
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
    { label: "Note", get: (r) => r.note ?? "—" },
    {
      label: "",
      get: (r) => r.event_id,
      render: (r) =>
        r.is_voided ? (
          <span className="muted">voided</span>
        ) : (
          <button type="button" className="ghost" onClick={() => voidEvent(r)}>
            Void
          </button>
        ),
    },
  ];

  return (
    <>
      <form
        className="ledger-filters"
        onSubmit={(event) => {
          event.preventDefault();
          setApplied({ ...draft, category: draft.category.trim() });
        }}
      >
        <label>
          From
          <input
            type="date"
            value={draft.from}
            onChange={(event) => setDraft((f) => ({ ...f, from: event.target.value }))}
          />
        </label>
        <label>
          To
          <input
            type="date"
            value={draft.to}
            onChange={(event) => setDraft((f) => ({ ...f, to: event.target.value }))}
          />
        </label>
        <label>
          Type
          <select
            value={draft.eventType}
            onChange={(event) => setDraft((f) => ({ ...f, eventType: event.target.value }))}
          >
            <option value="">All types</option>
            {EVENT_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>
        <label>
          Account
          <AccountSelect
            name="ledger_account_id"
            value={draft.accountId}
            onChange={(accountId) => setDraft((f) => ({ ...f, accountId }))}
            accounts={accounts}
            emptyLabel="All accounts"
          />
        </label>
        <label>
          Category
          <input
            type="text"
            value={draft.category}
            placeholder="groceries"
            onChange={(event) => setDraft((f) => ({ ...f, category: event.target.value }))}
          />
        </label>
        <button type="submit" className="ghost" disabled={busy}>
          Apply
        </button>
      </form>

      <div className="table-wrap">
        <Table columns={columns} rows={rows} emptyMessage="Nothing recorded yet." voided={(row) => row.is_voided} />
      </div>

      <div className="ledger-footer">
        <p className="muted">
          Showing {rows.length} of {totalCount}
        </p>
        {nextCursor ? (
          <button type="button" className="ghost" disabled={busy} onClick={() => void loadMore()}>
            Load more
          </button>
        ) : null}
      </div>
    </>
  );
}
