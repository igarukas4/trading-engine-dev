export type DashboardEvent = {
  event_id?: string;
  stream: "account" | "system";
  broker_account_id?: string | null;
  stream_sequence?: number;
  type: string;
  payload?: unknown;
};

/** Client-side merge policy: event IDs deduplicate, cursors stay stream-local. */
export class DashboardStream {
  readonly seenEventIds = new Set<string>();
  readonly accountCursors = new Map<string, number>();
  systemCursor = 0;

  accept(event: DashboardEvent): boolean {
    if (event.event_id && this.seenEventIds.has(event.event_id)) return false;
    if (event.event_id) this.seenEventIds.add(event.event_id);
    if (event.stream_sequence !== undefined) {
      if (event.stream === "system") this.systemCursor = Math.max(this.systemCursor, event.stream_sequence);
      else if (event.broker_account_id) this.accountCursors.set(event.broker_account_id, Math.max(this.accountCursors.get(event.broker_account_id) ?? 0, event.stream_sequence));
    }
    return true;
  }

  hello() { return { account_cursors: Object.fromEntries(this.accountCursors), system_cursor: this.systemCursor }; }
}
