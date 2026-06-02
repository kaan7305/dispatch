// Runtime mode for the SPA.
//
// The exact same build is served from two places:
//   - the daemon (127.0.0.1) — the trusted LOCAL surface where compose +
//     approve happen; authenticated by the per-launch local token.
//   - the broker (the deployed website) — a read/manage mirror authenticated
//     by the broker JWT. Compose + approve are surfaced here but redirect to
//     the local app, since those actions must stay on the trusted surface.
//
// The broker injects `window.__DISPATCH__ = { mode: "broker", ... }` into the
// HTML it serves. The daemon serves the raw index.html, so the fields are
// absent and we default to local mode.

interface DispatchRuntime {
  mode?: "local" | "broker";
  /** react-router basename — "/app" when the broker serves the SPA there. */
  basename?: string;
  /** Where the local daemon UI lives, for compose/approve redirects. */
  localAppUrl?: string;
}

const RUNTIME: DispatchRuntime =
  (window as unknown as { __DISPATCH__?: DispatchRuntime }).__DISPATCH__ ?? {};

export const isBroker = RUNTIME.mode === "broker";
export const basename = RUNTIME.basename ?? "/";
export const localAppUrl = (RUNTIME.localAppUrl ?? "http://127.0.0.1:8001").replace(/\/$/, "");

/** Open the local daemon UI (where compose + approve actually run). On the
 *  broker site these actions are shown but defer to the trusted local app. */
export function openLocalApp(): void {
  window.open(localAppUrl, "_blank", "noopener");
}
