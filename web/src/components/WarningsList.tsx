import { formatWarning, type WarningContext, type WarningTone } from "../lib/warnings";
import type { Warning } from "../lib/types";

const TONE_ORDER: WarningTone[] = ["error", "warning", "info"];

const HEADINGS: Record<WarningTone, string> = {
  error: "Needs attention",
  warning: "Warnings",
  info: "Notices",
};

type WarningsListProps = WarningContext & {
  warnings: Warning[];
};

export function WarningsList({ warnings, accounts, obligations }: WarningsListProps) {
  if (warnings.length === 0) return null;

  const formatted = warnings.map((warning) => ({
    warning,
    ...formatWarning(warning, { accounts, obligations }),
  }));
  const groups = TONE_ORDER.map((tone) => ({
    tone,
    items: formatted.filter((item) => item.tone === tone),
  })).filter((group) => group.items.length > 0);

  return (
    <div className="warnings">
      <h3>
        {warnings.length} thing{warnings.length === 1 ? "" : "s"} worth knowing
      </h3>
      {groups.map((group) => (
        <div key={group.tone} className={`warnings-group ${group.tone}`}>
          <h4>{HEADINGS[group.tone]}</h4>
          <ul>
            {group.items.map((item) => (
              <li key={`${item.warning.code}:${item.warning.event_id ?? item.warning.message}`}>
                {item.text}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
