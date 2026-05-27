// Per-launch bearer token the daemon stamps into the URL fragment.
// Stored in sessionStorage so a page reload during the same window keeps
// working, but it dies when the user closes the tab.

const SESSION_KEY = "dispatch_local_token";

export function bootstrapToken(): string {
  const params = new URLSearchParams(location.search);
  let t = params.get("t");
  if (!t && location.hash.startsWith("#t=")) t = location.hash.slice(3);
  if (t) {
    sessionStorage.setItem(SESSION_KEY, t);
    history.replaceState({}, "", location.pathname);
  }
  return sessionStorage.getItem(SESSION_KEY) ?? "";
}

export function getToken(): string {
  return sessionStorage.getItem(SESSION_KEY) ?? "";
}
