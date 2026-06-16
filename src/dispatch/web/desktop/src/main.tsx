import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { bootstrapToken } from "./lib/token";
import { basename } from "./lib/config";
import App from "./App";
// Bundled neutral UI font (Public Sans). Self-hosted via @fontsource so it
// renders in the offline WKWebView the daemon serves the SPA into.
import "@fontsource-variable/public-sans";
import "./index.css";

// Capture the per-launch local token from the URL fragment before React
// mounts - every API call needs it.
bootstrapToken();

// react-router basename: "/" under the daemon, "/app" when the broker serves
// the SPA. Strip any trailing slash; "/" must become "" for BrowserRouter.
const routerBasename = basename === "/" ? undefined : basename.replace(/\/$/, "");

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, refetchOnWindowFocus: false, retry: 1 },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename={routerBasename}>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
