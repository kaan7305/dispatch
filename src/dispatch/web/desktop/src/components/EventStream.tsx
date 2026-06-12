import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import type { DispatchEvent, DispatchStatus } from "@/lib/api";

interface Props {
  events: DispatchEvent[];
  /** Whose decisions matter for this view. "you" if the viewer is the
   *  recipient, "them" if the viewer is just watching. */
  viewerRole?: "recipient" | "watcher";
  /** Dispatch status - drives the empty-state copy. Only a running dispatch
   *  can still produce events, so only it gets the "streaming in" promise. */
  status?: DispatchStatus;
}

export function EventStream({ events, viewerRole = "watcher", status }: Props) {
  if (events.length === 0) {
    return (
      <div className="text-sm text-muted-foreground px-4 py-6 text-center border rounded-md">
        {status === "running"
          ? "No events yet. They'll stream in once the agent starts running."
          : "No events."}
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {events.map((event, i) => (
        <EventCard
          key={i}
          event={event}
          viewerRole={viewerRole}
          prevTs={i > 0 ? tsOf(events[i - 1]) : null}
        />
      ))}
    </div>
  );
}

function EventCard({
  event, viewerRole, prevTs,
}: { event: DispatchEvent; viewerRole: "recipient" | "watcher"; prevTs: string | null }) {
  const ts = tsOf(event);
  const timing = { ts, prevTs };
  switch (event.type) {
    case "agent_text":
      return (
        <Block tone="agent" label="Agent" timing={timing}>
          <Markdown text={stringFrom(event.data, "text")} />
        </Block>
      );

    case "tool_use":
      return (
        <Block tone="tool" label="Tool call" name={stringFrom(event.data, "name")} timing={timing}>
          <Json value={event.data["input"]} />
        </Block>
      );

    case "tool_result": {
      const isError = Boolean(event.data["is_error"]);
      const redacted = Boolean(event.data["redacted"]);
      if (redacted) {
        return (
          <Block tone={isError ? "error" : "result"} label={isError ? "Tool error" : "Tool result"} timing={timing}>
            <div className="text-xs text-muted-foreground italic">
              {stringFrom(event.data, "content") || "[result withheld by recipient]"}
            </div>
          </Block>
        );
      }
      return (
        <Block tone={isError ? "error" : "result"} label={isError ? "Tool error" : "Tool result"} timing={timing}>
          <div className="whitespace-pre-wrap break-words text-sm font-mono max-h-64 overflow-y-auto">
            {stringFrom(event.data, "content") || JSON.stringify(event.data, null, 2)}
          </div>
        </Block>
      );
    }

    case "permission_request": {
      const whose = viewerRole === "recipient" ? "your" : "their";
      return (
        <Block tone="tool" label="Permission requested" name={stringFrom(event.data, "tool")} timing={timing}>
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
      // approver) - never attribute it to the human as "You denied".
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
          timing={timing}
        >
          {event.data["reason"] ? (
            <div className="text-xs text-muted-foreground">{String(event.data["reason"])}</div>
          ) : null}
        </Block>
      );
    }

    case "dispatch_status":
      return (
        <Block tone="agent" label="Status" timing={timing}>
          <div className="text-sm">{stringFrom(event.data, "status")}</div>
        </Block>
      );

    case "done":
      return (
        <Block tone="result" label="Done" timing={timing}>
          <Json value={event.data} />
        </Block>
      );

    case "error":
      return (
        <Block tone="error" label="Error" timing={timing}>
          <div className="text-sm font-mono whitespace-pre-wrap break-words">
            {stringFrom(event.data, "message") || JSON.stringify(event.data, null, 2)}
          </div>
        </Block>
      );

    case "message": {
      // A human chat note pinned to the thread - distinct from agent output so
      // it never reads as something the agent said or did. Display-only.
      const author = stringFrom(event.data, "author");
      const isDecline = event.data["kind"] === "decline_reason";
      const label = isDecline ? "Decline reason" : "Message";
      return (
        <Block tone="message" label={label} name={author || undefined} timing={timing}>
          <div className="whitespace-pre-wrap break-words text-sm">
            {stringFrom(event.data, "body")}
          </div>
        </Block>
      );
    }
  }
}

/** Render agent prose as markdown - the executor speaks GFM (headings,
 *  tables, code fences), which used to show as raw `##`/`|` soup. */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="prose-dispatch text-sm break-words [&>*+*]:mt-2 [&_pre]:overflow-x-auto [&_pre]:bg-muted/40 [&_pre]:rounded [&_pre]:p-2 [&_pre]:text-xs [&_code]:font-mono [&_code]:text-xs [&_table]:text-xs [&_th]:text-left [&_th]:border-b [&_th]:px-2 [&_th]:py-1 [&_td]:border-b [&_td]:px-2 [&_td]:py-1 [&_h1]:text-base [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_h3]:text-sm [&_h3]:font-semibold [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_blockquote]:border-l-2 [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground [&_hr]:my-3 [&_a]:underline">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Reply links must leave the app, never navigate the SPA itself.
          // target=_blank routes them through the native window's UI delegate,
          // which hands them to the system browser.
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noopener noreferrer" />
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function Block({
  tone, label, name, timing, children,
}: {
  tone: "agent" | "tool" | "result" | "error" | "message";
  label: string;
  name?: string;
  timing?: { ts: string | null; prevTs: string | null };
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "border-l-4 rounded-md bg-card px-3 py-2 border",
        tone === "agent"   && "border-l-blue-500",
        tone === "tool"    && "border-l-amber-500",
        tone === "result"  && "border-l-green-500",
        tone === "error"   && "border-l-red-500",
        tone === "message" && "border-l-violet-500 bg-violet-50/40",
      )}
    >
      <div className="flex items-baseline gap-2 mb-1">
        <span className="text-[10px] uppercase tracking-wider font-medium text-muted-foreground">
          {label}
        </span>
        {name && (
          <span className="text-xs font-mono font-semibold">{name}</span>
        )}
        {timing?.ts && (
          <span className="ml-auto text-[10px] tabular-nums text-muted-foreground shrink-0">
            {clockTime(timing.ts)}
            {deltaLabel(timing.ts, timing.prevTs) && (
              <span className="ml-1.5 opacity-70">{deltaLabel(timing.ts, timing.prevTs)}</span>
            )}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}

function Json({ value }: { value: unknown }) {
  if (value === undefined || value === null) return null;
  return (
    <pre className="text-xs font-mono bg-muted/40 rounded p-2 overflow-x-auto max-h-48 whitespace-pre-wrap break-words">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/** Emission timestamp stamped into `data.ts` by the recipient daemon.
 *  Older events (pre-upgrade) simply lack it. */
function tsOf(event: DispatchEvent): string | null {
  const v = event.data["ts"];
  return typeof v === "string" ? v : null;
}

function clockTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleTimeString(undefined, { hour12: false });
}

/** "+12s" gap since the previous event - makes stalls (slow tools, waiting
 *  on a human approval) visible at a glance. Hidden under 1s. */
function deltaLabel(ts: string, prevTs: string | null): string | null {
  if (!prevTs) return null;
  const ms = new Date(ts).getTime() - new Date(prevTs).getTime();
  if (isNaN(ms) || ms < 1000) return null;
  const s = Math.round(ms / 1000);
  if (s < 60) return `+${s}s`;
  return `+${Math.floor(s / 60)}m${s % 60 ? ` ${s % 60}s` : ""}`;
}

function stringFrom(obj: Record<string, unknown>, key: string): string {
  const v = obj[key];
  return typeof v === "string" ? v : "";
}
