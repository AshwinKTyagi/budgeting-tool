import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiError, api } from "../lib/api";
import { today } from "../lib/constants";
import type { AccountVersion, Obligation } from "../lib/types";

export type FlashKind = "ok" | "info" | "error";

export type FlashState = {
  kind: FlashKind;
  message: string;
  code?: string;
} | null;

type BudgetContextValue = {
  asOf: string;
  setAsOf: (value: string) => void;
  asOfQuery: string;
  flash: FlashState;
  showFlash: (kind: FlashKind, message: string, code?: string) => void;
  clearFlash: () => void;
  accounts: AccountVersion[];
  setAccounts: (accounts: AccountVersion[]) => void;
  obligations: Obligation[];
  setObligations: (obligations: Obligation[]) => void;
  refreshKey: number;
  refreshAll: () => void;
  accountKind: (id: string) => string | null;
};

const BudgetContext = createContext<BudgetContextValue | null>(null);

export function BudgetProvider({ children }: { children: ReactNode }) {
  const [asOf, setAsOf] = useState(today);
  const [flash, setFlash] = useState<FlashState>(null);
  const [accounts, setAccounts] = useState<AccountVersion[]>([]);
  const [obligations, setObligations] = useState<Obligation[]>([]);
  const [refreshKey, setRefreshKey] = useState(0);
  const flashTimer = useRef<number | null>(null);

  const clearFlash = useCallback(() => setFlash(null), []);

  const showFlash = useCallback((kind: FlashKind, message: string, code?: string) => {
    if (flashTimer.current !== null) window.clearTimeout(flashTimer.current);
    setFlash({ kind, message, code });
    if (kind === "ok") {
      flashTimer.current = window.setTimeout(() => setFlash(null), 4000);
    }
  }, []);

  const refreshAll = useCallback(() => {
    setRefreshKey((key) => key + 1);
  }, []);

  const accountKind = useCallback(
    (id: string) => accounts.find((account) => account.entity_id === id)?.kind ?? null,
    [accounts],
  );

  const asOfQuery = `as_of=${encodeURIComponent(asOf)}`;

  const value = useMemo(
    () => ({
      asOf,
      setAsOf,
      asOfQuery,
      flash,
      showFlash,
      clearFlash,
      accounts,
      setAccounts,
      obligations,
      setObligations,
      refreshKey,
      refreshAll,
      accountKind,
    }),
    [
      asOf,
      asOfQuery,
      flash,
      showFlash,
      clearFlash,
      accounts,
      obligations,
      refreshKey,
      refreshAll,
      accountKind,
    ],
  );

  return <BudgetContext.Provider value={value}>{children}</BudgetContext.Provider>;
}

export function useBudget(): BudgetContextValue {
  const ctx = useContext(BudgetContext);
  if (!ctx) throw new Error("useBudget must be used within BudgetProvider");
  return ctx;
}

export function reportAppend(
  showFlash: BudgetContextValue["showFlash"],
  result: { deduplicated: boolean },
  message: string,
): void {
  if (result.deduplicated) showFlash("info", "Already recorded — nothing was added.");
  else showFlash("ok", message);
}

export function flashFromUnknown(
  showFlash: BudgetContextValue["showFlash"],
  err: unknown,
): void {
  if (err instanceof ApiError) {
    showFlash("error", err.message, err.code);
    return;
  }
  if (err instanceof Error) {
    showFlash("error", err.message);
    return;
  }
  showFlash("error", String(err));
}

/** Load accounts into context; used by Setup and any page that needs account selects. */
export function useLoadAccounts(): AccountVersion[] {
  const { asOfQuery, accounts, setAccounts, refreshKey, showFlash } = useBudget();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api<{ versions: AccountVersion[] }>(
          "GET",
          `/definitions/account?${asOfQuery}`,
        );
        if (!cancelled) setAccounts(data.versions);
      } catch (err) {
        if (!cancelled) flashFromUnknown(showFlash, err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [asOfQuery, refreshKey, setAccounts, showFlash]);

  return accounts;
}
