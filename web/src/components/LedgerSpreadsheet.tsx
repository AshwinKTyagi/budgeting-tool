import { useState } from "react";
import { api } from "../lib/api";
import { isAlterableType, isLiveRow } from "../lib/events";
import { fromMinor, toMinor } from "../lib/money";
import { replaceEvent } from "../lib/replaceEvent";
import type { AccountVersion, LedgerRow } from "../lib/types";
import { flashFromUnknown, useBudget } from "../context/BudgetContext";
import { AccountSelect } from "./AccountSelect";

export type CellDraft = {
  date?: string;
  who?: string;
  category?: string;
  account_id?: string;
  amount?: string;
  note?: string;
};

type LedgerSpreadsheetProps = {
  rows: LedgerRow[];
  accounts: AccountVersion[];
  emptyMessage: string;
};

/**
 * Rows the spreadsheet may edit in place.
 *
 * "expected" rows are included alongside "manual": a confirmed occurrence is a real,
 * user-authored event that happens to have been accepted rather than typed, and a
 * figure the projection proposed is exactly the kind you later want to correct.
 * "receipt" and "external" stay read-only — their amounts came from a document or a
 * provider, and editing them would put the ledger at odds with its source.
 */
function canEdit(row: LedgerRow): boolean {
  return (
    (row.origin === "manual" || row.origin === "expected") &&
    isLiveRow(row) &&
    isAlterableType(row.event_type)
  );
}

function displayAmount(row: LedgerRow, draft: CellDraft | undefined): string {
  if (draft?.amount !== undefined) return draft.amount;
  return row.amount_minor === null ? "" : fromMinor(row.amount_minor);
}

function overlayFromDraft(row: LedgerRow, draft: CellDraft): Record<string, unknown> {
  const overlay: Record<string, unknown> = {};
  if (draft.date !== undefined) overlay.date = draft.date;
  if (draft.category !== undefined) overlay.category = draft.category;
  if (draft.account_id !== undefined) overlay.account_id = draft.account_id;
  if (draft.note !== undefined) overlay.note = draft.note === "" ? null : draft.note;
  if (draft.amount !== undefined) overlay.amount_minor = toMinor(draft.amount);
  if (draft.who !== undefined) {
    if (row.event_type === "ExpenseRecorded") overlay.merchant = draft.who === "" ? null : draft.who;
    else if (row.event_type === "IncomeReceived") overlay.source = draft.who;
  }
  return overlay;
}

export function LedgerSpreadsheet({ rows, accounts, emptyMessage }: LedgerSpreadsheetProps) {
  const { showFlash, refreshAll } = useBudget();
  const [drafts, setDrafts] = useState<Record<string, CellDraft>>({});
  const [busy, setBusy] = useState(false);

  const dirtyIds = Object.keys(drafts).filter((id) => Object.keys(drafts[id]).length > 0);

  function patch(row: LedgerRow, field: keyof CellDraft, value: string) {
    setDrafts((current) => {
      const next = { ...(current[row.event_id] ?? {}), [field]: value };
      return { ...current, [row.event_id]: next };
    });
  }

  async function confirm() {
    if (dirtyIds.length === 0 || busy) return;
    setBusy(true);
    const remaining = { ...drafts };
    try {
      for (const id of dirtyIds) {
        const row = rows.find((r) => r.event_id === id);
        const draft = remaining[id];
        if (row === undefined || draft === undefined) continue;
        await replaceEvent(id, overlayFromDraft(row, draft));
        delete remaining[id];
        setDrafts({ ...remaining });
      }
      showFlash("ok", "Alterations saved.");
      setDrafts({});
      refreshAll();
    } catch (err) {
      flashFromUnknown(showFlash, err);
    } finally {
      setBusy(false);
    }
  }

  async function voidEvent(row: LedgerRow) {
    const reason = window.prompt(
      `Void this ${row.event_type} from ${row.date}?\n\nThe entry stays in history, struck through. Why?`,
      "entered by mistake",
    );
    if (reason === null || reason.trim() === "") return;
    try {
      await api("POST", `/events/${row.event_id}/void`, { reason: reason.trim() });
      showFlash("ok", "Voided.");
      setDrafts((current) => {
        const next = { ...current };
        delete next[row.event_id];
        return next;
      });
      refreshAll();
    } catch (err) {
      flashFromUnknown(showFlash, err);
    }
  }

  if (rows.length === 0) return <p className="empty">{emptyMessage}</p>;

  return (
    <>
      <div className="table-wrap">
        <table className="ledger-sheet">
          <thead>
            <tr>
              <th>Date</th>
              <th>Type</th>
              <th>Who</th>
              <th>Category</th>
              <th>Account</th>
              <th className="num">Amount</th>
              <th>Note</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const draft = drafts[row.event_id];
              const editable = canEdit(row);
              const whoEditable =
                editable &&
                (row.event_type === "ExpenseRecorded" || row.event_type === "IncomeReceived");
              const categoryEditable = editable && row.event_type === "ExpenseRecorded";
              const accountEditable = editable && row.account_id !== null;
              return (
                <tr key={row.event_id}>
                  <td className={draft?.date !== undefined ? "dirty" : undefined}>
                    {editable ? (
                      <input
                        type="date"
                        value={draft?.date ?? row.date}
                        onChange={(event) => patch(row, "date", event.target.value)}
                      />
                    ) : (
                      row.date
                    )}
                  </td>
                  <td>{row.event_type}</td>
                  <td className={draft?.who !== undefined ? "dirty" : undefined}>
                    {whoEditable ? (
                      <input
                        type="text"
                        value={draft?.who ?? row.counterparty ?? ""}
                        onChange={(event) => patch(row, "who", event.target.value)}
                      />
                    ) : (
                      (row.counterparty ?? "—")
                    )}
                  </td>
                  <td className={draft?.category !== undefined ? "dirty" : undefined}>
                    {categoryEditable ? (
                      <input
                        type="text"
                        value={draft?.category ?? row.category ?? ""}
                        onChange={(event) => patch(row, "category", event.target.value)}
                      />
                    ) : (
                      (row.category ?? "—")
                    )}
                  </td>
                  <td className={draft?.account_id !== undefined ? "dirty" : undefined}>
                    {accountEditable ? (
                      <AccountSelect
                        name={`ledger-account-${row.event_id}`}
                        value={draft?.account_id ?? row.account_id ?? ""}
                        onChange={(accountId) => patch(row, "account_id", accountId)}
                        accounts={accounts}
                      />
                    ) : (
                      (row.account_id ?? "—")
                    )}
                  </td>
                  <td className={`num${draft?.amount !== undefined ? " dirty" : ""}`}>
                    {editable && row.amount_minor !== null ? (
                      <input
                        type="text"
                        inputMode="decimal"
                        value={displayAmount(row, draft)}
                        onChange={(event) => patch(row, "amount", event.target.value)}
                      />
                    ) : row.amount_minor === null ? (
                      "—"
                    ) : (
                      fromMinor(row.amount_minor)
                    )}
                  </td>
                  <td className={draft?.note !== undefined ? "dirty" : undefined}>
                    {editable ? (
                      <input
                        type="text"
                        value={draft?.note ?? row.note ?? ""}
                        onChange={(event) => patch(row, "note", event.target.value)}
                      />
                    ) : (
                      (row.note ?? "—")
                    )}
                  </td>
                  <td>
                    <button type="button" className="ghost" onClick={() => void voidEvent(row)}>
                      Void
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="ledger-footer">
        <p className="muted">
          {dirtyIds.length === 0
            ? "No pending alterations"
            : `${dirtyIds.length} row${dirtyIds.length === 1 ? "" : "s"} pending`}
        </p>
        <div className="ledger-actions">
          <button
            type="button"
            className="ghost"
            disabled={busy || dirtyIds.length === 0}
            onClick={() => setDrafts({})}
          >
            Discard
          </button>
          <button
            type="button"
            className="confirm"
            disabled={busy || dirtyIds.length === 0}
            onClick={() => void confirm()}
          >
            Confirm
          </button>
        </div>
      </div>
    </>
  );
}
