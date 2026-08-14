import { api, postEvent, type AppendResult } from "./api";
import type { CanonicalEvent } from "./events";

const SERVER_FIELDS = new Set(["event_id", "recorded_at", "dedupe_key"]);

function cloneForAppend(original: CanonicalEvent): Record<string, unknown> {
  const cloned: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(original)) {
    if (!SERVER_FIELDS.has(key)) cloned[key] = value;
  }
  return cloned;
}

/**
 * Void `eventId` and append a replacement: the stored event with `overlay` applied.
 * Unspecified overlay keys keep the original values.
 */
export async function replaceEvent(
  eventId: string,
  overlay: Record<string, unknown>,
  reason = "corrected",
): Promise<AppendResult> {
  const { data: original } = await api<CanonicalEvent>("GET", `/events/${eventId}`);
  const event = { ...cloneForAppend(original), ...overlay };
  const result = await postEvent(event, { clientNonce: `replace:${eventId}` });
  await api("POST", `/events/${eventId}/void`, { reason });
  return result;
}
