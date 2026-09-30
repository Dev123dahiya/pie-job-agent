// Shared by server and client components. Keep plain values out of "use client" modules:
// a server component importing them gets a client reference, not the value.

/** Pipeline states the backend accepts for (re-)analysis (backend/services/analysis.py). */
export const ANALYZABLE_STATES: ReadonlySet<string> = new Set([
  "DISCOVERED",
  "ANALYZED",
  "SHORTLISTED",
  "REJECTED_BY_AGENT",
  "FAILED",
]);

/** Fired after a background run starts so the activity panel polls immediately. */
export const RUN_STARTED_EVENT = "pie:run-started";
