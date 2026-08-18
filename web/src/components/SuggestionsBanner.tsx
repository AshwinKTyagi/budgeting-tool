import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { fromMinor, toMinor } from "../lib/money";
import type { Suggestion, SuggestionPage } from "../lib/types";
import { flashFromUnknown, useBudget } from "../context/BudgetContext";

/**
 * Forecast occurrences that have come due, awaiting confirmation (PLAN.md §8.5).
 *
 * Bills, paychecks and statement interest are all forecasts until the user says
 * otherwise. Confirming appends the real event; rejecting records that it did not
 * happen. Nothing here runs on a timer — the list is a read, and only the buttons
 * write, which is what keeps every row in the ledger an explicit user action.
 *
 * Renders nothing at all when there is nothing pending. A banner that is always
 * present is a banner nobody reads.
 */

const KIND_LABEL: Record<Suggestion["kind"], string> = {
  income: "Income",
  bill: "Bill",
  interest: "Interest",
};

type Draft = { amount: string; date: string };

export function SuggestionsBanner() {
  const { asOfQuery, showFlash, refreshAll, refreshKey } = useBudget();
  const [rows, setRows] = useState<Suggestion[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>({ amount: "", date: "" });
  const [busy, setBusy] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api<SuggestionPage>("GET", `/suggestions?${asOfQuery}`);
        if (!cancelled) setRows(data.suggestions);
      } catch (err) {
        if (!cancelled) flashFromUnknown(showFlash, err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [asOfQuery, refreshKey, showFlash]);

  /**
   * `body` is undefined for an unedited confirmation and for a plain rejection — both
   * endpoints treat an absent body as "as forecast, no comment".
   */
  const act = useCallback(
    async (row: Suggestion, action: "confirm" | "reject", body?: unknown) => {
      setBusy(row.suggestion_id);
      try {
        await api(
          "POST",
          `/suggestions/${encodeURIComponent(row.suggestion_id)}/${action}?${asOfQuery}`,
          body,
        );
        setEditing(null);
        // Bumping the refresh key reloads this list and every figure the new event
        // moves — balances, the period summary, the ledger — in one pass.
        refreshAll();
        showFlash(
          "ok",
          action === "confirm"
            ? `${row.description} confirmed and added to the ledger.`
            : `${row.description} marked as not received.`,
        );
      } catch (err) {
        flashFromUnknown(showFlash, err);
      } finally {
        setBusy(null);
      }
    },
    [asOfQuery, refreshAll, showFlash],
  );

  const saveEdit = useCallback(
    (row: Suggestion) => {
      try {
        // Only what actually changed is sent: the server keeps the forecast value for
        // every field the body omits.
        const body: Record<string, unknown> = {};
        if (draft.amount.trim() !== "") body.amount_minor = toMinor(draft.amount);
        if (draft.date.trim() !== "" && draft.date !== row.date) body.date = draft.date;
        void act(row, "confirm", body);
      } catch (err) {
        flashFromUnknown(showFlash, err);
      }
    },
    [act, draft, showFlash],
  );

  if (rows.length === 0) return null;

  return (
    <section className="suggestions" aria-labelledby="suggestions-heading">
      <header className="suggestions-header">
        <h2 id="suggestions-heading">
          Ready to confirm <span className="badge count">{rows.length}</span>
        </h2>
        <button
          type="button"
          className="link"
          aria-expanded={!collapsed}
          onClick={() => setCollapsed((value) => !value)}
        >
          {collapsed ? "Show" : "Hide"}
        </button>
      </header>

      {collapsed ? null : (
        <>
          <p className="hint">
            Scheduled items whose date has passed. Confirm to add one to the ledger,
            or reject to record that it never happened. Nothing here affects your
            budget until you confirm it.
          </p>
          <ul className="suggestion-list">
            {rows.map((row) => {
              const isEditing = editing === row.suggestion_id;
              const isBusy = busy === row.suggestion_id;
              return (
                <li key={row.suggestion_id} className="suggestion">
                  <span className={`badge ${row.kind}`}>{KIND_LABEL[row.kind]}</span>
                  <span className="suggestion-date">{row.date}</span>
                  <span className="suggestion-what">{row.description}</span>

                  {isEditing ? (
                    <>
                      <input
                        aria-label="Amount"
                        className="suggestion-input"
                        value={draft.amount}
                        onChange={(e) => setDraft({ ...draft, amount: e.target.value })}
                      />
                      <input
                        aria-label="Date"
                        type="date"
                        className="suggestion-input"
                        value={draft.date}
                        onChange={(e) => setDraft({ ...draft, date: e.target.value })}
                      />
                    </>
                  ) : (
                    <span className="suggestion-amount num">
                      {fromMinor(row.amount_minor)}
                    </span>
                  )}

                  <span className="suggestion-actions">
                    {isEditing ? (
                      <>
                        <button
                          type="button"
                          title="Save and confirm"
                          disabled={isBusy}
                          onClick={() => saveEdit(row)}
                        >
                          Save
                        </button>
                        <button
                          type="button"
                          title="Cancel"
                          disabled={isBusy}
                          onClick={() => setEditing(null)}
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          title="Confirm — add this to the ledger"
                          aria-label={`Confirm ${row.description}`}
                          disabled={isBusy}
                          onClick={() => void act(row, "confirm")}
                        >
                          ✓
                        </button>
                        <button
                          type="button"
                          title="Reject — this did not happen"
                          aria-label={`Reject ${row.description}`}
                          disabled={isBusy}
                          onClick={() => void act(row, "reject")}
                        >
                          ✗
                        </button>
                        <button
                          type="button"
                          title="Edit before confirming"
                          aria-label={`Edit ${row.description}`}
                          disabled={isBusy}
                          onClick={() => {
                            setEditing(row.suggestion_id);
                            setDraft({
                              amount: fromMinor(row.amount_minor),
                              date: row.date,
                            });
                          }}
                        >
                          ✎
                        </button>
                      </>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
