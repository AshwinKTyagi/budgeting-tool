import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { fromMinor } from "../lib/money";
import type {
  AccountBalance,
  ChartSeries,
  LedgerRow,
  PeriodState,
  ProjectedState,
  Warning,
} from "../lib/types";
import { flashFromUnknown, useBudget } from "../context/BudgetContext";
import { Chart } from "../components/Chart";
import { Table, type Column } from "../components/Table";

export function OverviewPage() {
  const { asOfQuery, showFlash, refreshAll, refreshKey, setObligations } = useBudget();
  const [balances, setBalances] = useState<AccountBalance[]>([]);
  const [periodId, setPeriodId] = useState<string | null>(null);
  const [period, setPeriod] = useState<PeriodState | null>(null);
  const [obligations, setLocalObligations] = useState<ProjectedState["obligations"]>([]);
  const [warnings, setWarnings] = useState<Warning[]>([]);
  const [ledger, setLedger] = useState<LedgerRow[]>([]);
  const [series, setSeries] = useState<ChartSeries[]>([]);

  const format = useCallback((value: number) => fromMinor(value), []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [accountsRes, stateRes, ledgerRes, spent, remaining] = await Promise.all([
          api<{ accounts: AccountBalance[] }>("GET", `/accounts?${asOfQuery}`),
          api<ProjectedState>("GET", `/state?${asOfQuery}`),
          api<{ rows: LedgerRow[] }>("GET", "/ledger?limit=25"),
          api<{ points: ChartSeries["points"] }>(
            "GET",
            `/charts/series?metric=discretionary_spent&grain=period&group_by=none&${asOfQuery}`,
          ),
          api<{ points: ChartSeries["points"] }>(
            "GET",
            `/charts/series?metric=discretionary_remaining&grain=period&group_by=none&${asOfQuery}`,
          ),
        ]);
        if (cancelled) return;

        setBalances(accountsRes.data.accounts);
        setPeriodId(stateRes.data.current_period_id);
        setPeriod(
          stateRes.data.periods.find((p) => p.period_id === stateRes.data.current_period_id) ??
            null,
        );
        setLocalObligations(stateRes.data.obligations);
        setObligations(stateRes.data.obligations);
        setWarnings(stateRes.data.warnings);
        setLedger(ledgerRes.data.rows);
        setSeries([
          { label: "Spent", points: spent.data.points },
          { label: "Remaining", points: remaining.data.points },
        ]);
      } catch (err) {
        if (!cancelled) flashFromUnknown(showFlash, err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [asOfQuery, refreshKey, setObligations, showFlash]);

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

  const balanceColumns: Column<AccountBalance>[] = [
    { label: "Account", get: (r) => r.name },
    { label: "Kind", get: (r) => r.kind },
    { label: "Balance", num: true, get: (r) => r.balance_minor },
    {
      label: "Outstanding",
      num: true,
      get: (r) => r.outstanding_minor ?? 0,
      render: (r) => (r.outstanding_minor === null ? "—" : fromMinor(r.outstanding_minor)),
    },
    { label: "Interest to date", num: true, get: (r) => r.cumulative_interest_minor },
  ];

  const obligationColumns: Column<ProjectedState["obligations"][number]>[] = [
    { label: "Payee", get: (r) => r.payee },
    { label: "Due", get: (r) => r.due_date },
    { label: "Category", get: (r) => r.category },
    { label: "Amount", num: true, get: (r) => r.amount_minor },
    { label: "Remaining", num: true, get: (r) => r.remaining_minor },
    {
      label: "Status",
      get: (r) => r.status,
      render: (r) => (
        <span className={`badge ${r.status}`}>{r.status.replace("_", " ")}</span>
      ),
    },
  ];

  const ledgerColumns: Column<LedgerRow>[] = [
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
    <section>
      <h2>Balances</h2>
      <div className="table-wrap">
        <Table columns={balanceColumns} rows={balances} emptyMessage="No accounts yet." />
      </div>

      <h2>
        This period {periodId ? <span className="muted">{periodId}</span> : null}
      </h2>
      {period ? (
        <dl className="stats">
          <Stat label="Income" value={period.income_minor} />
          <Stat label="Allocatable" value={period.allocatable_income_minor} signed />
          <Stat label="Fixed due" value={period.fixed_due_minor} />
          <Stat label="Fixed outstanding" value={period.fixed_outstanding_minor} />
          <Stat label="To savings" value={period.savings_allocated_minor} signed />
          <Stat
            label="Discretionary allocated"
            value={period.discretionary_allocated_minor}
            signed
          />
          <Stat label="Discretionary spent" value={period.discretionary_spent_minor} />
          <Stat
            label="Discretionary left"
            value={period.discretionary_remaining_minor}
            signed
          />
        </dl>
      ) : (
        <p className="empty">No period data yet — record some income to begin.</p>
      )}

      <h2>Obligations</h2>
      <div className="table-wrap">
        <Table
          columns={obligationColumns}
          rows={obligations}
          emptyMessage="No obligations yet. Add a monthly expense under Recurring."
        />
      </div>

      {warnings.length > 0 ? (
        <div className="warnings">
          <h3>
            {warnings.length} thing{warnings.length === 1 ? "" : "s"} worth knowing
          </h3>
          <ul>
            {warnings.map((w) => (
              <li key={`${w.code}:${w.message}`}>
                {w.message} <code>{w.code}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <h2>Discretionary by period</h2>
      <Chart series={series} format={format} />

      <h2>Recent activity</h2>
      <div className="table-wrap">
        <Table
          columns={ledgerColumns}
          rows={ledger}
          emptyMessage="Nothing recorded yet."
          voided={(row) => row.is_voided}
        />
      </div>
    </section>
  );
}

function Stat({
  label,
  value,
  signed = false,
}: {
  label: string;
  value: number;
  signed?: boolean;
}) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd className={signed && value < 0 ? "neg" : undefined}>{fromMinor(value)}</dd>
    </div>
  );
}
