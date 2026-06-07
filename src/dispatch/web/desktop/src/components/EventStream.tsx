import { cn } from "@/lib/utils";
import type { DispatchEvent } from "@/lib/api";

interface Props {
  events: DispatchEvent[];
  /** Whose decisions matter for this view. "you" if the viewer is the
   *  recipient, "them" if the viewer is just watching. */
  viewerRole?: "recipient" | "watcher";
}

export function EventStream({ events, viewerRole = "watcher" }: Props) {
  if (events.length === 0) {
    return (
      <div className="text-sm text-muted-foreground px-4 py-6 text-center border rounded-md">
        No events yet. They'll stream in once the agent starts running.
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {events.map((event, i) => (
        <EventCard key={i} event={event} viewerRole={viewerRole} />
      ))}
    </div>
  );
}

function EventCard({ event, viewerRole }: { event: DispatchEvent; viewerRole: "recipient" | "watcher" }) {
  switch (event.type) {
    case "agent_text":
      return (
        <Block tone="agent" label="Agent">
          <div className="whitespace-pre-wrap text-sm">{stringFrom(event.data, "text")}</div>
        </Block>
      );

    case "tool_use":
      return (
        <Block tone="tool" label="Tool call" name={stringFrom(event.data, "name")}>
          <Json value={event.data["input"]} />
        </Block>
      );

    case "tool_result": {
      const isError = Boolean(event.data["is_error"]);
      return (
        <Block tone={isError ? "error" : "result"} label={isError ? "Tool error" : "Tool result"}>
          <div className="whitespace-pre-wrap text-sm font-mono">
            {stringFrom(event.data, "content") || JSON.stringify(event.data, null, 2)}
          </div>
        </Block>
      );
    }

    case "permission_request": {
      const whose = viewerRole === "recipient" ? "your" : "their";
      return (
        <Block tone="tool" label="Permission requested" name={stringFrom(event.data, "tool")}>
          <Json value={event.data["input"]} />
          <div className="mt-1 text-xs text-muted-foreground">
            Waiting for {whose} Allow / Deny.
          </div>
        </Block>
      );
    }

    case "permission_response": {
      const allowed = event.data["decision"] === "allow";
      const reason = event.data["reason"];
      const subject = viewerRole === "recipient" ? "You" : "They";
      // A deny that carries a reason was automatic (timeout / out-of-scope / no
      // approver) — never attribute it to the human as "You denied".
      const label = allowed
        ? `${subject} allowed`
        : reason
          ? "Auto-denied"
          : `${subject} denied`;
      return (
        <Block
          tone={allowed ? "result" : "error"}
          label={label}
          name={stringFrom(event.data, "tool")}
        >
          {event.data["reason"] ? (
            <div className="text-xs text-muted-foreground">{String(event.data["reason"])}</div>
          ) : null}
        </Block>
      );
    }

    case "dispatch_status":
      return (
        <Block tone="agent" label="Status">
          <div className="text-sm">{stringFrom(event.data, "status")}</div>
        </Block>
      );

    case "done":
      return (
        <Block tone="result" label="Done">
          <Json value={event.data} />
        </Block>
      );

    case "error":
      return (
        <Block tone="error" label="Error">
          <div className="text-sm font-mono whitespace-pre-wrap">
            {stringFrom(event.data, "message") || JSON.stringify(event.data, null, 2)}
          </div>
        </Block>
      );
  }
}

function Block({
  tone, label, name, children,
}: {
  tone: "agent" | "tool" | "result" | "error";
  label: string;
  name?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "border-l-4 rounded-md bg-card px-3 py-2 border",
        tone === "agent"  && "border-l-blue-500",
        tone === "tool"   && "border-l-amber-500",
        tone === "result" && "border-l-green-500",
        tone === "error"  && "border-l-red-500",
      )}
    >
      <div className="flex items-baseline gap-2 mb-1">
        <span className="text-[10px] uppercase tracking-wider font-medium text-muted-foreground">
          {label}
        </span>
        {name && (
          <span className="text-xs font-mono font-semibold">{name}</span>
        )}
      </div>
      {children}
    </div>
  );
}

function Json({ value }: { value: unknown }) {
  if (value === undefined || value === null) return null;
  return (
    <pre className="text-xs font-mono bg-muted/40 rounded p-2 overflow-x-auto max-h-48 whitespace-pre-wrap">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function stringFrom(obj: Record<string, unknown>, key: string): string {
  const v = obj[key];
  return typeof v === "string" ? v : "";
}
