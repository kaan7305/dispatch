// Bearer token for API calls. Two sources depending on where we're served:
//
//   - LOCAL mode: the daemon stamps a per-launch local token into the URL
//     fragment. Stored in sessionStorage so a reload during the same window
//     keeps working, but it dies when the tab closes.
//   - BROKER mode: the broker JWT minted by the Clerk sign-in page, kept in
//     localStorage under the same key that page writes ("dispatch_token").

import { isBroker } from "./config";

const LOCAL_KEY = "dispatch_local_token";
const BROKER_KEY = "dispatch_token"; // written by the broker sign-in page (web/app)

export function bootstrapToken(): string {
  if (isBroker) {
    // The sign-in page persists the JWT to localStorage; nothing to capture
    // from the URL here.
    return getToken();
  }
  // The tray stamps launch params into the fragment: #t=<token>&d=<dispatch>.
  // (The legacy bare form "#t=xyz" parses identically.) `d` is a deep-link
  // target — a clicked notification lands directly on that dispatch.
  const params = new URLSearchParams(location.search);
  const hashParams = new URLSearchParams(location.hash.slice(1));
  const t = params.get("t") ?? hashParams.get("t");
  const deepLink = hashParams.get("d");
  if (t) sessionStorage.setItem(LOCAL_KEY, t);
  if (t || deepLink) {
    history.replaceState(
      {}, "",
      deepLink ? `/dispatch/${encodeURIComponent(deepLink)}` : location.pathname,
    );
  }
  return sessionStorage.getItem(LOCAL_KEY) ?? "";
}

export function getToken(): string {
  if (isBroker) return localStorage.getItem(BROKER_KEY) ?? "";
  return sessionStorage.getItem(LOCAL_KEY) ?? "";
}

/** Clear the active token (on 401). In broker mode this also drops the
 *  stored user id so the sign-in page starts clean. */
export function clearToken(): void {
  if (isBroker) {
    localStorage.removeItem(BROKER_KEY);
    localStorage.removeItem("dispatch_user_id");
  } else {
    sessionStorage.removeItem(LOCAL_KEY);
  }
}
