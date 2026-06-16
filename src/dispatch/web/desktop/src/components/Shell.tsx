import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Inbox as InboxIcon,
  Users,
  Workflow,
  BookText,
  History as HistoryIcon,
  Monitor,
  Settings as SettingsIcon,
  Search,
  Plus,
} from "lucide-react";

import { api } from "@/lib/api";
import { isBroker, openLocalApp } from "@/lib/config";
import { openEventStream, type EventMessage } from "@/lib/ws";
import { cn } from "@/lib/utils";
import { initials } from "@/lib/format";
import { Button } from "./ui/button";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { ComposeDialog } from "./ComposeDialog";

const NAV = [
  { to: "/inbox",     label: "Inbox",     Icon: InboxIcon  },
  { to: "/people",    label: "People",    Icon: Users      },
  { to: "/workflows", label: "Workflows", Icon: Workflow   },
  { to: "/contexts",  label: "Context",   Icon: BookText   },
  { to: "/history",   label: "History",   Icon: HistoryIcon },
  { to: "/devices",   label: "Devices",   Icon: Monitor    },
  { to: "/settings",  label: "Settings",  Icon: SettingsIcon },
];

export function Shell() {
  const qc = useQueryClient();
  const [online, setOnline] = useState(false);

  const { data: session } = useQuery({
    queryKey: ["session"],
    queryFn: () => api.session(),
  });

  // Single global event stream - always open while the shell is mounted.
  // Invalidates every affected query so any page stays current without polling.
  useEffect(() => {
    const close = openEventStream(
      (msg: EventMessage) => {
        if (msg.type === "snapshot") {
          qc.invalidateQueries({ queryKey: ["inbox"] });
          return;
        }
        if (msg.type === "inbox_new") {
          qc.invalidateQueries({ queryKey: ["inbox"] });
          qc.invalidateQueries({ queryKey: ["history", "received"] });
          return;
        }
        if (msg.type === "dispatch_status" || msg.type === "dispatch_event") {
          qc.invalidateQueries({ queryKey: ["inbox"] });
          qc.invalidateQueries({ queryKey: ["sent"] });
          qc.invalidateQueries({ queryKey: ["history", "sent"] });
          qc.invalidateQueries({ queryKey: ["history", "received"] });
          qc.invalidateQueries({ queryKey: ["dispatch", msg.dispatch_id] });
        }
      },
      (connected) => setOnline(connected),
    );
    return () => close();
  }, [qc]);

  return (
    <div className="flex h-full flex-col">
      <Topbar email={session?.user_id} online={online} />
      <div className="flex flex-1 min-h-0">
        <Sidebar />
        <main className="flex-1 overflow-y-auto bg-background">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function Topbar({ email, online }: { email?: string; online: boolean }) {
  return (
    <header className="flex items-center gap-4 border-b px-6 h-14">
      <div className="flex items-center gap-2 shrink-0">
        <span className="text-xl font-semibold tracking-tight">Dispatch</span>
      </div>
      <div className="flex-1 max-w-xl">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            type="search"
            placeholder="Search dispatches and people"
            className="w-full rounded-lg border bg-secondary/40 pl-9 pr-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
      </div>
      <div className="ml-auto flex items-center gap-3 text-sm text-muted-foreground shrink-0">
        <span className="inline-flex items-center gap-1.5">
          <span className={cn("size-2 rounded-full", online ? "bg-green-500" : "bg-amber-500 animate-pulse")} />
          {online ? "Online" : "Connecting…"}
        </span>
        <AccountMenu email={email} />
      </div>
    </header>
  );
}

function AccountMenu({ email }: { email?: string }) {
  // Sign-out lives only on the broker page (the Railway sign-in landing).
  // The desktop UI just shows the signed-in identity for context.
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="grid place-items-center size-8 rounded-full bg-muted text-xs font-semibold text-foreground hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
        >
          {email ? initials(email) : "-"}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{email || "Not signed in"}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => { api.openBroker().catch(() => {}); }}>
          Manage at broker →
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// Drag-to-resize bounds for the left nav (px). Default sits at the old w-48.
const SIDEBAR_MIN = 168;
const SIDEBAR_MAX = 288;
const SIDEBAR_DEFAULT = 192;
const SIDEBAR_KEY = "dispatch:sidebarWidth";

function clampSidebar(px: number): number {
  return Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, px));
}

function Sidebar() {
  const [width, setWidth] = useState(() => {
    const saved = Number(localStorage.getItem(SIDEBAR_KEY));
    return saved ? clampSidebar(saved) : SIDEBAR_DEFAULT;
  });
  const dragging = useRef(false);

  useEffect(() => {
    localStorage.setItem(SIDEBAR_KEY, String(width));
  }, [width]);

  useEffect(() => {
    // The sidebar's left edge is at viewport x=0, so the pointer's clientX is
    // the desired width. Listeners live on window so a fast drag that outruns
    // the 4px handle keeps resizing.
    function onMove(e: MouseEvent) {
      if (dragging.current) setWidth(clampSidebar(e.clientX));
    }
    function onUp() {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  function startDrag() {
    dragging.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }

  return (
    <aside
      style={{ width }}
      className="relative shrink-0 border-r px-3 py-4 flex flex-col gap-1"
    >
      {isBroker ? (
        // Compose stays on the trusted local surface; the broker site defers
        // to the local app instead of composing here.
        <Button
          className="w-full justify-center gap-2 mb-3"
          size="lg"
          onClick={() => openLocalApp()}
          title="Compose runs in the local Dispatch app"
        >
          <Plus className="size-4" /> Compose
        </Button>
      ) : (
        <ComposeDialog>
          <Button className="w-full justify-center gap-2 mb-3" size="lg">
            <Plus className="size-4" /> Compose
          </Button>
        </ComposeDialog>
      )}
      {NAV.map(({ to, label, Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
              isActive
                ? "bg-secondary font-medium text-foreground"
                : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
            )
          }
        >
          <Icon className="size-4" />
          {label}
        </NavLink>
      ))}
      {/* Drag handle over the right border to resize the nav. */}
      <div
        role="separator"
        aria-orientation="vertical"
        onMouseDown={startDrag}
        onDoubleClick={() => setWidth(SIDEBAR_DEFAULT)}
        title="Drag to resize (double-click to reset)"
        className="absolute top-0 right-0 z-10 h-full w-1.5 cursor-col-resize hover:bg-border active:bg-ring/50"
      />
    </aside>
  );
}

