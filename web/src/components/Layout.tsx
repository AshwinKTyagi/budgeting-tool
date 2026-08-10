import { NavLink, Outlet } from "react-router-dom";
import { useBudget } from "../context/BudgetContext";

const NAV = [
  { to: "/overview", label: "Overview" },
  { to: "/account", label: "Account" },
  { to: "/setup", label: "Setup" },
  { to: "/recurring", label: "Recurring" },
  { to: "/record", label: "Record" },
] as const;

export function Layout() {
  const { asOf, setAsOf, flash, refreshAll } = useBudget();

  return (
    <>
      <header>
        <h1>budgeting&#8209;tool</h1>
        <div className="as-of">
          <label htmlFor="as-of">as of</label>
          <input
            id="as-of"
            type="date"
            value={asOf}
            onChange={(event) => {
              setAsOf(event.target.value);
              refreshAll();
            }}
          />
          <button
            type="button"
            className="ghost icon"
            onClick={refreshAll}
            aria-label="Refresh"
            title="Refresh"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M21 12a9 9 0 1 1-2.64-6.36" />
              <polyline points="21 3 21 9 15 9" />
            </svg>
          </button>
        </div>
      </header>

      <nav className="tabs full" role="tablist">
        {NAV.map((item) => (
          <NavLink key={item.to} to={item.to} role="tab">
            {item.label}
          </NavLink>
        ))}
      </nav>

      {flash ? (
        <div className={`flash ${flash.kind}`}>
          {flash.message}
          {flash.code ? <code> {flash.code}</code> : null}
        </div>
      ) : null}

      <Outlet />
    </>
  );
}
