// Broker install page. The only thing this page does is:
//   1. Sign the user in with Clerk + Google.
//   2. Exchange the Clerk session for a Dispatch JWT (POST /auth/clerk).
//   3. Show the install command they paste into a terminal.
//
// The dispatch UI itself (inbox, compose, contacts, approvals) lives in
// the desktop app served by the daemon at 127.0.0.1:8001.

const STORAGE_TOKEN = "dispatch_token";
const STORAGE_USER  = "dispatch_user_id";

let token  = localStorage.getItem(STORAGE_TOKEN);
let userId = localStorage.getItem(STORAGE_USER);

const inviteToken = new URLSearchParams(location.search).get("invite");

const authStatus   = document.getElementById("auth-status");
const installPanel = document.getElementById("install-panel");
const installCmd   = document.getElementById("install-cmd");
const tokenDisplay = document.getElementById("token-display");
const loginBtn     = document.getElementById("login-btn");
const logoutBtn    = document.getElementById("logout");
const openAppLink  = document.getElementById("open-app-link");

function refreshAuth() {
  if (token && userId) {
    authStatus.textContent = `Signed in as ${userId}`;
    authStatus.className = "status done";
    installPanel.hidden = false;
    tokenDisplay.textContent = token;
    installCmd.textContent =
      `curl -fsSL ${location.origin}/install.sh | bash -s -- ${token}`;
    let deepLink = `dispatch://configure?broker=${encodeURIComponent(location.origin)}` +
                   `&token=${encodeURIComponent(token)}` +
                   `&user_id=${encodeURIComponent(userId)}`;
    if (inviteToken) deepLink += `&invite=${encodeURIComponent(inviteToken)}`;
    openAppLink.setAttribute("href", deepLink);
    logoutBtn.hidden = false;
    loginBtn.hidden = true;

    const inviteBanner = document.getElementById("invite-banner");
    if (inviteBanner) inviteBanner.hidden = !inviteToken;
  } else {
    if (!authStatus.classList.contains("error")) {
      authStatus.textContent = "Not signed in";
      authStatus.className = "status idle";
    }
    installPanel.hidden = true;
    tokenDisplay.textContent = "";
    installCmd.textContent = "";
    logoutBtn.hidden = true;
    loginBtn.hidden = false;
  }
}
refreshAuth();

// ─── Clerk auth flow ─────────────────────────────────────────────────

const CONFIG = window.DISPATCH_CONFIG || {};
const CLERK_TEMPLATE = CONFIG.clerk_jwt_template || "dispatch";

function waitForClerkScript() {
  return new Promise((resolve, reject) => {
    if (window.Clerk) return resolve();
    if (!CONFIG.clerk_publishable_key) {
      return reject(new Error(
        "Clerk is not configured — set CLERK_PUBLISHABLE_KEY and " +
        "CLERK_FRONTEND_API on the broker."
      ));
    }
    window.addEventListener("clerk-script-loaded", () => resolve(), { once: true });
  });
}

let clerkReady = null;
function ensureClerk() {
  if (!clerkReady) {
    clerkReady = (async () => {
      await waitForClerkScript();
      await window.Clerk.load();
      window.Clerk.addListener(({ user }) => {
        if (user && !token) exchangeClerkForBrokerJwt();
        if (!user && token) clearLocalSession();
      });
      if (window.Clerk.user && !token) await exchangeClerkForBrokerJwt();
    })();
  }
  return clerkReady;
}

async function exchangeClerkForBrokerJwt() {
  try {
    const clerkToken = await window.Clerk.session.getToken({ template: CLERK_TEMPLATE });
    if (!clerkToken) throw new Error("Clerk returned no session token");
    const res = await fetch("/auth/clerk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clerk_token: clerkToken }),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`;
      throw new Error(detail);
    }
    const body = await res.json();
    token  = body.token;
    userId = body.user_id;
    localStorage.setItem(STORAGE_TOKEN, token);
    localStorage.setItem(STORAGE_USER, userId);
    refreshAuth();
  } catch (err) {
    authStatus.className = "status error";
    authStatus.textContent = `Sign-in failed: ${err.message}`;
  }
}

function clearLocalSession() {
  token = null;
  userId = null;
  localStorage.removeItem(STORAGE_TOKEN);
  localStorage.removeItem(STORAGE_USER);
  refreshAuth();
}

loginBtn.addEventListener("click", async () => {
  authStatus.className = "status running";
  authStatus.textContent = "Opening sign-in…";
  try {
    await ensureClerk();
    if (window.Clerk.user) {
      await exchangeClerkForBrokerJwt();
    } else {
      window.Clerk.openSignIn({ afterSignInUrl: location.pathname });
    }
  } catch (err) {
    authStatus.className = "status error";
    authStatus.textContent = `Sign-in failed: ${err.message}`;
  }
});

logoutBtn.addEventListener("click", async () => {
  try { await ensureClerk(); await window.Clerk.signOut(); } catch (_) {}
  clearLocalSession();
});

document.getElementById("copy-install").addEventListener("click", async (e) => {
  try {
    await navigator.clipboard.writeText(installCmd.textContent || "");
    e.target.textContent = "copied ✓";
    setTimeout(() => { e.target.textContent = "Copy install command"; }, 1500);
  } catch (_) {}
});

// Bootstrap Clerk on page load so a returning user (Clerk session valid,
// broker JWT cleared) gets re-authed without clicking sign-in.
ensureClerk().catch(() => { /* surfaced when user clicks Sign in */ });
