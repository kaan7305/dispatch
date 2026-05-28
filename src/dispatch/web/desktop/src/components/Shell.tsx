import { NavLink, Outlet } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Inbox as InboxIcon,
  Users,
  Bookmark,
  History as HistoryIcon,
  Monitor,
  Search,
  Plus,
  LogOut,
} from "lucide-react";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { initials } from "@/lib/format";
import { Button } from "./ui/button";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { ComposeDialog } from "./ComposeDialog";

const NAV = [
  { to: "/inbox",   label: "Inbox",   Icon: InboxIcon  },
  { to: "/people",  label: "People",  Icon: Users      },
  { to: "/saved",   label: "Saved",   Icon: Bookmark   },
  { to: "/history", label: "History", Icon: HistoryIcon },
  { to: "/devices", label: "Devices", Icon: Monitor    },
];

export function Shell() {
  const { data: session } = useQuery({
    queryKey: ["session"],
    queryFn: () => api.session(),
  });

  return (
    <div className="flex h-full flex-col">
      <Topbar email={session?.user_id} />
      <div className="flex flex-1 min-h-0">
        <Sidebar />
        <main className="flex-1 overflow-y-auto bg-background">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

function Topbar({ email }: { email?: string }) {
  return (
    <header className="flex items-center gap-4 border-b px-6 h-14">
      <div className="flex items-center gap-2 shrink-0">
        <span className="text-xl font-semibold tracking-tight">Dispatch</span>
        <span className="rounded-md border px-2 py-0.5 text-xs text-muted-foreground">
          Free
        </span>
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
          <span className="size-2 rounded-full bg-green-500" /> Online
        </span>
        <AccountMenu email={email} />
      </div>
    </header>
  );
}

function AccountMenu({ email }: { email?: string }) {
  const qc = useQueryClient();
  const signOut = useMutation({
    mutationFn: () => api.signOut(),
    onSuccess: (result) => {
      // Drop the local token + clear cached state, then send the user
      // back to the broker landing for a fresh Clerk sign-in.
      sessionStorage.removeItem("dispatch_local_token");
      qc.clear();
      window.location.href = result.broker || "/";
    },
  });

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="grid place-items-center size-8 rounded-full bg-muted text-xs font-semibold text-foreground hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
        >
          {email ? initials(email) : "—"}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{email || "Not signed in"}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="text-destructive focus:text-destructive"
          onSelect={() => signOut.mutate()}
        >
          <LogOut className="size-4" /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function Sidebar() {
  return (
    <aside className="w-60 shrink-0 border-r px-3 py-4 flex flex-col gap-1">
      <ComposeDialog>
        <Button className="w-full justify-center gap-2 mb-3" size="lg">
          <Plus className="size-4" /> Compose
        </Button>
      </ComposeDialog>
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
      <div className="mt-auto pt-4 text-xs text-muted-foreground border-t">
        <div>Free Plan</div>
        <div>5 / 10 dispatches</div>
      </div>
    </aside>
  );
}

