import { useState } from "react";
import { api } from "../lib/api";
import { today } from "../lib/constants";
import { useBudget } from "../context/BudgetContext";
import { Field } from "../components/Field";
import { FormShell } from "../components/FormShell";

export function SetupPage() {
  const { showFlash, refreshAll } = useBudget();
  const [savingsPercent, setSavingsPercent] = useState(50);

  return (
    <section>
      <div className="grid full">
        <FormShell
          resetOnSuccess={false}
          onSubmit={async (fields) => {
            const savings = savingsPercent * 100;
            const discretionary = (100 - savingsPercent) * 100;
            await api("POST", "/definitions/allocation-policy", {
              version: {
                entity_id: fields.entity_id,
                savings_bps: savings,
                discretionary_bps: discretionary,
                effective_from: fields.effective_from,
                effective_to: null,
              },
              close_previous_at: null,
            });
            showFlash("ok", "Allocation policy saved.");
            refreshAll();
          }}
        >
          {({ errors, busy }) => (
            <>
              <h2>Allocation policy</h2>
              <p className="hint">
                How income left over after fixed costs is split. Optional — with none
                set, the projection falls back to a silent 50/50.
              </p>
              <Field
                label="Policy id"
                name="entity_id"
                required
                defaultValue="default"
                error={errors.entity_id}
              />
              <label className="split">
                Split
                <div className="split-labels">
                  <span>Savings - {savingsPercent}%</span>
                  <span>{100 - savingsPercent}% - Discretionary</span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={savingsPercent}
                  onChange={(event) => setSavingsPercent(Number(event.target.value))}
                  aria-valuetext={`Savings ${savingsPercent}%, discretionary ${100 - savingsPercent}%`}
                />
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
                Save policy
              </button>
            </>
          )}
        </FormShell>
      </div>
    </section>
  );
}
