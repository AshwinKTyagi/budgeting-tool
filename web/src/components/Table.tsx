import type { ReactNode } from "react";
import { fromMinor } from "../lib/money";

export type Column<T> = {
  label: string;
  num?: boolean;
  get: (row: T) => string | number | null | undefined;
  render?: (row: T) => ReactNode;
};

type TableProps<T> = {
  columns: Column<T>[];
  rows: T[];
  emptyMessage: string;
  voided?: (row: T) => boolean;
};

export function Table<T>({ columns, rows, emptyMessage, voided }: TableProps<T>) {
  if (rows.length === 0) return <p className="empty">{emptyMessage}</p>;

  return (
    <table>
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column.label || "actions"} className={column.num ? "num" : undefined}>
              {column.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={index} className={voided?.(row) ? "voided" : undefined}>
            {columns.map((column) => {
              const value = column.get(row);
              const negative = column.num && typeof value === "number" && value < 0;
              const className = [column.num ? "num" : "", negative ? "neg" : ""]
                .join(" ")
                .trim() || undefined;
              return (
                <td key={column.label || "actions"} className={className}>
                  {column.render
                    ? column.render(row)
                    : column.num
                      ? fromMinor(Number(value ?? 0))
                      : (value ?? "—")}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
