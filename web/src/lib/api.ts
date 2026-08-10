const API = "/api/v1";

export type ApiErrorBody = {
  message?: string;
  code?: string;
  details?: {
    errors?: Array<{ loc?: string; msg?: string }>;
  };
};

export class ApiError extends Error {
  status: number;
  code: string;
  details: NonNullable<ApiErrorBody["details"]>;

  constructor(status: number, body: ApiErrorBody | null) {
    super(body?.message ?? `request failed (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = body?.code ?? "UNKNOWN";
    this.details = body?.details ?? {};
  }

  /**
   * `details.errors[]` keyed by field name.
   *
   * `loc` is a pydantic location tuple joined with "." — "event.amount_minor", or
   * "version.savings_bps", or with a discriminated union in the path. The last segment
   * is the field, which is what the form labels its inputs.
   */
  fieldErrors(): Record<string, string> {
    const errors = this.details?.errors;
    if (!Array.isArray(errors)) return {};
    const out: Record<string, string> = {};
    for (const entry of errors) {
      const key = String(entry.loc ?? "").split(".").pop();
      if (key && !(key in out)) out[key] = entry.msg ?? "invalid";
    }
    return out;
  }
}

export async function api<T = unknown>(
  method: string,
  path: string,
  body?: unknown,
): Promise<{ status: number; data: T }> {
  const options: RequestInit = { method, headers: {} };
  if (body !== undefined) {
    (options.headers as Record<string, string>)["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(API + path, options);
  const text = await response.text();
  const parsed = text === "" ? null : (JSON.parse(text) as T);
  if (!response.ok) throw new ApiError(response.status, parsed as ApiErrorBody);
  return { status: response.status, data: parsed as T };
}

export type AppendResult = {
  event_id: string;
  dedupe_key: string;
  deduplicated: boolean;
};

/** Append one event. Returns `{event_id, dedupe_key, deduplicated}`. */
export async function postEvent(event: Record<string, unknown>): Promise<AppendResult> {
  const { data } = await api<AppendResult>("POST", "/events", {
    event,
    client_nonce: null,
  });
  return data;
}
