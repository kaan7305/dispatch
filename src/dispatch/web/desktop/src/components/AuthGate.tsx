import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { getToken, clearToken } from "@/lib/token";
import { isBroker } from "@/lib/config";
import { Button } from "./ui/button";

/** Wraps the app and only renders children once the local token is valid.
 *  Shows a "open via the menu bar app" gate otherwise.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const [manualToken, setManualToken] = useState("");
  const [tick, setTick] = useState(0);
  const hasToken = !!getToken();

  // Probe /api/session — works as both a smoke test and an auth check.
  const probe = useQuery({
    queryKey: ["__probe", tick],
    queryFn: () => api.session(),
    retry: false,
    enabled: hasToken,
  });

  useEffect(() => {
    if (probe.error instanceof ApiError && probe.error.status === 401) {
      // Stale or wrong token — clear it so the user sees the gate.
      clearToken();
      setTick((n) => n + 1);
    }
  }, [probe.error]);

  const unauthed =
    !hasToken || (probe.error instanceof ApiError && probe.error.status === 401);

  // Broker site: send the user to the Clerk sign-in / install landing page,
  // which mints the broker JWT this app reads from localStorage.
  if (unauthed && isBroker) {
    return (
      <div className="min-h-full grid place-items-center p-8">
        <div className="max-w-md w-full text-center space-y-5">
          <div className="mx-auto grid place-items-center size-12 rounded-full bg-muted">
            <KeyRound className="size-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold">Sign in to Dispatch</h1>
            <p className="mt-2 text-sm text-muted-foreground">
              Sign in on the Dispatch home page, then return here to view your
              inbox, contacts, devices, and history.
            </p>
          </div>
          <Button onClick={() => { location.href = "/"; }}>Go to sign in</Button>
        </div>
      </div>
    );
  }

  if (unauthed) {
    return (
      <div className="min-h-full grid place-items-center p-8">
        <div className="max-w-md w-full text-center space-y-5">
          <div className="mx-auto grid place-items-center size-12 rounded-full bg-muted">
            <KeyRound className="size-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold">Open Dispatch from the menu bar</h1>
            <p className="mt-2 text-sm text-muted-foreground">
              Dispatch's desktop UI is signed-in via your local daemon. Click
              the menu bar icon and pick <strong>Open Inbox</strong>.
            </p>
          </div>
          <details className="text-left text-sm">
            <summary className="cursor-pointer text-muted-foreground">
              Or paste your local token (from <code className="text-foreground">~/.dispatch/local.token</code>)
            </summary>
            <form
              className="mt-3 flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (!manualToken.trim()) return;
                sessionStorage.setItem("dispatch_local_token", manualToken.trim());
                setTick((n) => n + 1);
              }}
            >
              <input
                value={manualToken}
                onChange={(e) => setManualToken(e.target.value)}
                placeholder="local token"
                className="flex-1 rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
              />
              <Button type="submit" size="sm">Use</Button>
            </form>
          </details>
        </div>
      </div>
    );
  }

  if (probe.isLoading) {
    return <div className="min-h-full grid place-items-center text-sm text-muted-foreground">Connecting…</div>;
  }

  return <>{children}</>;
}
