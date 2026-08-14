import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { optional } from "../lib/constants";
import { isAlterableType, isLiveRow, type CanonicalEvent } from "../lib/events";
import { ledgerQuery } from "../lib/ledgerQuery";
import { fromMinor, minorField } from "../lib/money";
import { replaceEvent } from "../lib/replaceEvent";
import type { LedgerPage, LedgerRow, Obligation } from "../lib/types";
import {
  flashFromUnknown,
  useBudget,
  useLoadAccounts,
} from "../context/BudgetContext";
import { AccountSelect } from "./AccountSelect";
import { Field } from "./Field";
import { FormShell } from "./FormShell";

export function AlterEventForm() {
  const { showFlash, refreshAll, refreshKey, obligations } = useBudget();
  const accounts = useLoadAccounts();
  const [choices, setChoices] = useState<LedgerRow[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [loaded, setLoaded] = useState<CanonicalEvent | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api<LedgerPage>("GET", "/ledger?limit=500");
        if (cancelled) return;
        const eligible = data.rows.filter(
          (row) =>
            isLiveRow(row) && row.origin === "manual" && isAlterableType(row.event_type),
        );
        setChoices(eligible);
        setSelectedId((current) =>
          eligible.some((row) => row.event_id === current) ? current : "",
        );
      } catch (err) {
        if (!cancelled) flashFromUnknown(showFlash, err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshKey, showFlash]);

  useEffect(() => {
    if (selectedId === "") {
      setLoaded(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api<CanonicalEvent>("GET", `/events/${selectedId}`);
        if (!cancelled) setLoaded(data);
      } catch (err) {
        if (!cancelled) {
          setLoaded(null);
          flashFromUnknown(showFlash, err);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId, showFlash]);

  return (
    <div className="grid full">
      <label>
        Event
        <select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
          <option value="">— choose an event —</option>
          {choices.map((row) => (
            <option key={row.event_id} value={row.event_id}>
              {row.date} · {row.event_type} · {row.counterparty ?? row.account_id ?? "—"} ·{" "}
              {row.amount_minor === null ? "—" : fromMinor(row.amount_minor)}
            </option>
          ))}
        </select>
      </label>

      {loaded === null ? (
        <p className="empty">Pick a live event to alter. Unchanged fields stay as recorded.</p>
      ) : (
        <AlterFields
          key={loaded.event_id}
          event={loaded}
          accounts={accounts}
          obligations={obligations}
          onConfirm={async (overlay) => {
            await replaceEvent(loaded.event_id, overlay);
            showFlash("ok", "Alteration saved.");
            refreshAll();
            setSelectedId("");
          }}
        />
      )}
    </div>
  );
}

function AlterFields({
  event,
  accounts,
  obligations,
  onConfirm,
}: {
  event: CanonicalEvent;
  accounts: ReturnType<typeof useLoadAccounts>;
  obligations: Obligation[];
  onConfirm: (overlay: Record<string, unknown>) => Promise<void>;
}) {
  const [accountId, setAccountId] = useState(String(event.account_id ?? ""));
  const [obligationId, setObligationId] = useState(String(event.obligation_id ?? ""));
  const type = event.event_type;

  if (!isAlterableType(type)) {
    return <p className="empty">This event type cannot be altered here.</p>;
  }

  return (
    <FormShell
      onSubmit={async (fields) => {
        const overlay: Record<string, unknown> = {
          date: fields.date,
          note: optional(fields.note) ?? null,
        };
        if (type === "IncomeReceived") {
          overlay.amount_minor = minorField(fields, "amount_minor");
          overlay.source = fields.source;
          overlay.account_id = fields.account_id;
        } else if (type === "ExpenseRecorded") {
          overlay.amount_minor = minorField(fields, "amount_minor");
          overlay.category = fields.category;
          overlay.account_id = fields.account_id;
          overlay.merchant = optional(fields.merchant) ?? null;
        } else if (type === "PaymentMade") {
          overlay.amount_minor = minorField(fields, "amount_minor");
          overlay.obligation_id = fields.obligation_id;
          overlay.account_id = fields.account_id;
        } else {
          overlay.amount_minor = minorField(fields, "amount_minor");
          overlay.account_id = fields.account_id;
        }
        await onConfirm(overlay);
      }}
    >
      {({ errors, busy }) => (
        <>
          <h2>Alter {type}</h2>
          <p className="hint">
            Confirm voids the original and records a replacement. Fields you leave alone
            keep their recorded values.
          </p>
          <Field
            label="Date"
            name="date"
            type="date"
            required
            defaultValue={event.date}
            error={errors.date}
          />
          {type === "IncomeReceived" ? (
            <Field
              label="Source"
              name="source"
              required
              defaultValue={String(event.source ?? "")}
              error={errors.source}
            />
          ) : null}
          {type === "ExpenseRecorded" ? (
            <>
              <Field
                label="Category"
                name="category"
                required
                defaultValue={String(event.category ?? "")}
                error={errors.category}
              />
              <Field
                label="Merchant"
                name="merchant"
                defaultValue={String(event.merchant ?? "")}
                error={errors.merchant}
              />
            </>
          ) : null}
          {type === "PaymentMade" ? (
            <label>
              Obligation
              <select
                name="obligation_id"
                value={obligationId}
                required
                onChange={(e) => setObligationId(e.target.value)}
              >
                {obligationId !== "" &&
                !obligations.some((o) => o.obligation_id === obligationId) ? (
                  <option value={obligationId}>{obligationId}</option>
                ) : null}
                {obligations.map((o) => (
                  <option key={o.obligation_id} value={o.obligation_id}>
                    {o.payee} — {o.due_date} — {fromMinor(o.remaining_minor)} left
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <Field
            label="Amount"
            name="amount_minor"
            inputMode="decimal"
            required
            defaultValue={
              typeof event.amount_minor === "number" ? fromMinor(event.amount_minor) : ""
            }
            error={errors.amount_minor}
          />
          <label>
            Account
            <AccountSelect
              name="account_id"
              value={accountId}
              onChange={setAccountId}
              accounts={accounts}
              required
              error={Boolean(errors.account_id)}
            />
          </label>
          <Field
            label={
              <>
                Note <span className="opt">optional</span>
              </>
            }
            name="note"
            defaultValue={event.note ?? ""}
            error={errors.note}
          />
          <button type="submit" disabled={busy}>
            Confirm
          </button>
        </>
      )}
    </FormShell>
  );
}
