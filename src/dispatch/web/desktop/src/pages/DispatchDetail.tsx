import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, ArrowUp, Ban, Check, ChevronDown, ChevronRight, Clock, CornerUpLeft, Infinity as InfinityIcon, Paperclip, RotateCcw, Send, X } from "lucide-react";

import { api, type DispatchEvent, type InboxEntry } from "@/lib/api";
import { openDispatchWatch } from "@/lib/ws";
import { initials, relativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/StatusBadge";
import { EventStream, Markdown } from "@/components/EventStream";

export default function DispatchDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const detail = useQuery({
    queryKey: ["dispatch", id],
    queryFn: () => api.dispatchDetail(id),
    enabled: !!id,
  });

  // Broker-side watch stream — covers sent dispatches the local daemon
  // doesn't witness. The global /ws/events in Shell already handles
  // received dispatch updates, so we only need this extra stream here.
  useEffect(() => {
    if (!id) return;
    const closeWatch = openDispatchWatch(id, () => {
      qc.invalidateQueries({ queryKey: ["dispatch", id] });
    });
    return () => closeWatch();
  }, [id, qc]);

  if (detail.isLoading) {
    return <div className="px-6 py-8 text-sm text-muted-foreground">Loading…</div>;
  }
  if (detail.error || !detail.data) {
    return (
      <div className="px-6 py-8">
        <Button variant="ghost" onClick={() => navigate(-1)}><ArrowLeft className="size-4" /> Back</Button>
        <div className="mt-6 text-sm text-muted-foreground">
          Could not load dispatch. It may have been delivered to a different
          device, or you may not be the recipient.
        </div>
      </div>
    );
  }

  return <DetailBody entry={detail.data} onBack={() => navigate(-1)} />;
}

interface AnyDispatch {
  dispatch_id: string;
  sender_id: string;
  recipient_id?: string;
  task: string;
  metadata?: Record<string, unknown> | null;
  status: InboxEntry["status"];
  created_at: string;
  scopes?: InboxEntry["scopes"];
  pending_tools?: InboxEntry["pending_tools"];
  events?: DispatchEvent[];
  reply?: string | null;
}

/** Wall-clock duration + tool-call count for a finished run, from the event
 *  trace's `ts` stamps. Null when there's nothing to measure (no events, or a
 *  pre-upgrade trace without timestamps). */
function runStats(entry: AnyDispatch): { duration: string | null; toolCalls: number } | null {
  const events = entry.events ?? [];
  if (events.length === 0) return null;
  const toolCalls = events.filter((e) => e.type === "tool_use").length;
  const stamps = events
    .map((e) => (typeof e.data["ts"] === "string" ? new Date(e.data["ts"] as string).getTime() : NaN))
    .filter((t) => !isNaN(t));
  let duration: string | null = null;
  if (stamps.length >= 2) {
    const s = Math.round((Math.max(...stamps) - Math.min(...stamps)) / 1000);
    if (s >= 3600) duration = `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    else if (s >= 60) duration = `${Math.floor(s / 60)}m ${s % 60}s`;
    else duration = `${s}s`;
  }
  if (!duration && toolCalls === 0) return null;
  return { duration, toolCalls };
}

/** The consumable answer: the broker derives `reply` server-side; for local
 *  (live) entries fall back to the last agent_text in the trace. */
function replyOf(entry: AnyDispatch): string | null {
  if (typeof entry.reply === "string" && entry.reply.trim()) return entry.reply;
  let reply: string | null = null;
  for (const e of entry.events ?? []) {
    if (e.type === "agent_text") {
      const text = e.data["text"];
      if (typeof text === "string" && text.trim()) reply = text;
    }
  }
  return reply;
}

function DetailBody({
  entry, onBack,
}: { entry: AnyDispatch; onBack: () => void }) {
  const session = useQuery({ queryKey: ["session"], queryFn: () => api.session() });
  const me = session.data?.user_id ?? "";
  const isRecipient = !!entry.recipient_id && entry.recipient_id === me;
  const isSender = !!me && entry.sender_id === me;
  const reply = replyOf(entry);
  const decisionPending = entry.status === "pending" || entry.status === "delivered";
  const terminal =
    entry.status === "completed" ||
    entry.status === "failed" ||
    entry.status === "denied" ||
    entry.status === "expired" ||
    entry.status === "cancelled";
  const [resendOpen, setResendOpen] = useState(false);
  const [followUpOpen, setFollowUpOpen] = useState(false);
  // A follow-up I send goes to the parent's OTHER party (whoever I'm not). Both
  // parties can spawn one; a recipient's follow-up routes back to the sender
  // (and needs an edge in that direction, enforced at send like any dispatch).
  const otherParty = isSender ? entry.recipient_id ?? "" : entry.sender_id;
  const parentId = (entry.metadata?.["parent_id"] as string | undefined) ?? undefined;

  // "Approval waiting" nudge: pending tool decisions render near the top of the
  // page, but a live run streams its events into the Activity trace at the
  // bottom — so a recipient watching events come in won't see a new Allow/Deny
  // card appear above the fold. Track whether the pending-tools block is on
  // screen; when it isn't (and there are decisions waiting), float a pill that
  // jumps to it. Only the recipient can decide, so only they get the nudge.
  const scrollRef = useRef<HTMLDivElement>(null);
  const pendingRef = useRef<HTMLDivElement>(null);
  const pendingCount = Object.keys(entry.pending_tools ?? {}).length;
  const [pendingVisible, setPendingVisible] = useState(true);

  useEffect(() => {
    if (!isRecipient || pendingCount === 0) return;
    const target = pendingRef.current;
    if (!target) return;
    const obs = new IntersectionObserver(
      ([e]) => setPendingVisible(e.isIntersecting),
      { root: scrollRef.current ?? null, threshold: 0.15 },
    );
    obs.observe(target);
    return () => obs.disconnect();
  }, [isRecipient, pendingCount]);

  const showNudge = isRecipient && pendingCount > 0 && !pendingVisible;

  return (
    <div className="h-full flex flex-col relative">
      <div className="px-6 py-4 border-b flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft className="size-4" /> Back
        </Button>
        <StatusBadge status={entry.status} />
        <RunStats entry={entry} />
        {!terminal && (
          <div className="ml-auto">
            <CancelButton dispatchId={entry.dispatch_id} />
          </div>
        )}
        {terminal && (
          <div className="ml-auto flex items-center gap-2">
            {otherParty && (
              <Button variant="outline" size="sm" onClick={() => { setFollowUpOpen((o) => !o); setResendOpen(false); }}>
                <CornerUpLeft className="size-4" /> Follow up
              </Button>
            )}
            {isSender && (
              <Button variant="outline" size="sm" onClick={() => { setResendOpen((o) => !o); setFollowUpOpen(false); }}>
                <RotateCcw className="size-4" /> Resend
              </Button>
            )}
          </div>
        )}
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-6 space-y-6">
          {resendOpen && (
            <ResendPanel entry={entry} me={me} onClose={() => setResendOpen(false)} />
          )}
          {followUpOpen && otherParty && (
            <FollowUpPanel entry={entry} recipient={otherParty} onClose={() => setFollowUpOpen(false)} />
          )}
          {parentId && <ParentLink parentId={parentId} />}
          <Header entry={entry} />
          <RichPayload metadata={entry.metadata} dispatchId={entry.dispatch_id} />
          {decisionPending && isRecipient && <TopLevelDecision entry={entry} />}
          {isRecipient && (
            <div ref={pendingRef} className="scroll-mt-4">
              <PendingTools entry={entry} />
            </div>
          )}
          {reply && (
            <Section title="Reply">
              <div className="rounded-lg border bg-card p-4">
                <Markdown text={reply} />
              </div>
            </Section>
          )}
          {entry.scopes && (
            <Section title="Scope">
              <ScopeSummary scopes={entry.scopes} />
            </Section>
          )}
          <CollapsibleActivity
            entry={entry}
            isRecipient={isRecipient}
            defaultOpen={!reply}
          />
          {(isSender || isRecipient) && <ReplyComposer entry={entry} />}
        </div>
      </div>
      {showNudge && (
        <ApprovalNudge
          count={pendingCount}
          onClick={() =>
            pendingRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })
          }
        />
      )}
    </div>
  );
}

/** Floating "scroll up to approve" pill. Shown to the recipient when one or
 *  more tool decisions are waiting but scrolled out of view — the approval
 *  cards live at the top of the page while a live run's events stream into the
 *  Activity trace at the bottom, so without this a waiting Allow/Deny is easy
 *  to miss. Sits at the bottom of the pane (where the eye is during a run) and
 *  points up to where the decisions are. */
function ApprovalNudge({ count, onClick }: { count: number; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="absolute left-1/2 -translate-x-1/2 bottom-6 z-20 inline-flex items-center gap-2 rounded-full border border-amber-300 bg-amber-100 px-4 py-2 text-sm font-medium text-amber-900 shadow-lg ring-1 ring-amber-300/50 hover:bg-amber-200"
    >
      <span className="relative flex size-2">
        <span className="absolute inline-flex size-full animate-ping rounded-full bg-amber-500 opacity-75" />
        <span className="relative inline-flex size-2 rounded-full bg-amber-600" />
      </span>
      {count} approval{count === 1 ? "" : "s"} waiting — review
      <ArrowUp className="size-4" />
    </button>
  );
}

/** Structured sender context + attachment manifest riding on the dispatch
 *  (metadata.context / metadata.attachments — both signature-bound). Detail
 *  responses carry only the attachment manifest; the bytes live on the
 *  recipient's machine, written into the run workspace. */
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i;

function RichPayload({
  metadata, dispatchId,
}: { metadata?: Record<string, unknown> | null; dispatchId: string }) {
  const ctx = (metadata?.["context"] ?? null) as
    | { project?: string; deliverable?: string; links?: string[]; background?: string }
    | null;
  const attachments = (metadata?.["attachments"] ?? null) as
    | { name?: string; size?: number }[]
    | null;
  const hasCtx = !!ctx && !!(ctx.project || ctx.deliverable || ctx.links?.length || ctx.background);
  if (!hasCtx && !attachments?.length) return null;
  return (
    <div className="rounded-lg border bg-card p-4 space-y-3 text-sm">
      {ctx?.project && (
        <div><span className="text-muted-foreground">Project:</span> {ctx.project}</div>
      )}
      {ctx?.deliverable && (
        <div><span className="text-muted-foreground">Deliverable:</span> {ctx.deliverable}</div>
      )}
      {!!ctx?.links?.length && (
        <div className="space-y-0.5">
          <span className="text-muted-foreground">Links:</span>
          {ctx.links.map((l) => (
            <a key={l} href={l} target="_blank" rel="noreferrer" className="block underline break-all">
              {l}
            </a>
          ))}
        </div>
      )}
      {ctx?.background && (
        <details>
          <summary className="cursor-pointer text-muted-foreground">
            Background from the sender's session
          </summary>
          <div className="mt-2">
            <Markdown text={ctx.background} />
          </div>
        </details>
      )}
      {!!attachments?.length && (
        <div className="space-y-2">
          <span className="text-muted-foreground">Attachments:</span>
          {attachments.map((a, i) => (
            <Attachment key={i} dispatchId={dispatchId} name={a.name} size={a.size} />
          ))}
        </div>
      )}
    </div>
  );
}

/** One attachment row. Images render an inline thumbnail (click to open full
 *  size in a new tab); everything else is a download link. In broker mode the
 *  bytes aren't reachable (they live on the recipient's machine), so it falls
 *  back to the old name + size line. */
function Attachment({
  dispatchId, name, size,
}: { dispatchId: string; name?: string; size?: number }) {
  const [failed, setFailed] = useState(false);
  if (!name) return null;
  const url = api.attachmentUrl(dispatchId, name);
  const isImage = IMAGE_EXT.test(name);
  const sizeLabel = typeof size === "number" ? formatBytes(size) : null;

  if (url && isImage && !failed) {
    return (
      <div className="space-y-1">
        <a href={url} target="_blank" rel="noreferrer" className="block">
          <img
            src={url}
            alt={name}
            onError={() => setFailed(true)}
            className="max-h-80 rounded-md border object-contain"
          />
        </a>
        <div className="flex items-center gap-2 font-mono text-xs text-muted-foreground">
          <Paperclip className="size-3.5 shrink-0" />
          {name}
          {sizeLabel && <span>{sizeLabel}</span>}
        </div>
      </div>
    );
  }

  const row = (
    <div className="flex items-center gap-2 font-mono text-xs">
      <Paperclip className="size-3.5 shrink-0" />
      {name}
      {sizeLabel && <span className="text-muted-foreground">{sizeLabel}</span>}
    </div>
  );
  // With a URL, make the row a download link; without one (broker mode), plain.
  return url ? (
    <a href={url} download={name} target="_blank" rel="noreferrer" className="block hover:underline">
      {row}
    </a>
  ) : row;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** "took 3m 12s · 7 tool calls" for finished runs. */
function RunStats({ entry }: { entry: AnyDispatch }) {
  if (entry.status !== "completed" && entry.status !== "failed") return null;
  const stats = runStats(entry);
  if (!stats) return null;
  const parts: string[] = [];
  if (stats.duration) parts.push(`took ${stats.duration}`);
  parts.push(`${stats.toolCalls} tool call${stats.toolCalls === 1 ? "" : "s"}`);
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <Clock className="size-3.5" />
      {parts.join(" · ")}
    </span>
  );
}

/** The full event trace. Collapsed by default once there's a reply to read —
 *  the trace is the audit trail, not the answer. */
function CollapsibleActivity({
  entry, isRecipient, defaultOpen,
}: { entry: AnyDispatch; isRecipient: boolean; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const count = entry.events?.length ?? 0;
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 text-xs font-medium uppercase tracking-wider text-muted-foreground mb-2 hover:text-foreground"
      >
        {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        Activity{count > 0 ? ` (${count})` : ""}
      </button>
      {open && (
        <EventStream
          events={entry.events ?? []}
          viewerRole={isRecipient ? "recipient" : "watcher"}
          status={entry.status}
        />
      )}
    </div>
  );
}

/** Compose a fresh dispatch from a finished one — same recipient, task
 *  prefilled but editable (the usual reason to resend is that the first run
 *  missed the mark). The copy carries `resend_of` in metadata so the chain
 *  is traceable; everything else goes through the normal compose path, so
 *  trust, rate limits and signing all apply as usual. */
function ResendPanel({
  entry, me, onClose,
}: { entry: AnyDispatch; me: string; onClose: () => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [task, setTask] = useState(entry.task);
  const [error, setError] = useState<string | null>(null);
  // recipient_id is always present on broker-served details; the only entry
  // without one is a locally-witnessed loopback, where the recipient is us.
  const recipient = entry.recipient_id ?? me;
  // Attachments can't be carried over: detail responses hold only the
  // manifest (bytes are stripped server-side), and the broker rejects
  // manifest-only attachment entries at compose.
  const { resend_of: _prior, attachments: _atts, ...carried } = entry.metadata ?? {};

  const resend = useMutation({
    mutationFn: () =>
      api.compose({
        recipient_id: recipient,
        task: task.trim(),
        metadata: { ...carried, resend_of: entry.dispatch_id },
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["sent"] });
      qc.invalidateQueries({ queryKey: ["inbox"] });
      const newId =
        "dispatch_id" in res ? res.dispatch_id : res.dispatches[0]?.dispatch_id;
      onClose();
      if (newId) navigate(`/dispatch/${newId}`);
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  });

  return (
    <div className="rounded-lg border bg-card p-4 space-y-3">
      <div>
        <div className="font-medium">Resend to {recipient}</div>
        <div className="text-sm text-muted-foreground mt-0.5">
          Sends a new dispatch with the same task. Edit it first if the last
          run didn't come back the way you wanted.
        </div>
      </div>
      <textarea
        value={task}
        onChange={(e) => setTask(e.target.value)}
        rows={5}
        spellCheck={false}
        className="w-full rounded-md border bg-background px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring resize-y"
      />
      <div className="flex items-center justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onClose} disabled={resend.isPending}>
          Cancel
        </Button>
        <Button
          size="sm"
          onClick={() => resend.mutate()}
          disabled={resend.isPending || !task.trim()}
        >
          {resend.isPending ? "Sending…" : "Send again"}
        </Button>
      </div>
      {error && <div className="text-xs text-destructive">{error}</div>}
    </div>
  );
}

/** "↩ Follow-up of <id>" banner linking back to the parent dispatch. Shown on
 *  any dispatch that carries metadata.parent_id, so a threaded chain is
 *  navigable in both directions. */
function ParentLink({ parentId }: { parentId: string }) {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      onClick={() => navigate(`/dispatch/${parentId}`)}
      className="inline-flex items-center gap-1.5 rounded-md border border-violet-200 bg-violet-50/60 px-2.5 py-1 text-xs font-medium text-violet-800 hover:bg-violet-100"
    >
      <CornerUpLeft className="size-3.5" />
      Follow-up — open the original dispatch
    </button>
  );
}

/** Compose a follow-up: a NEW dispatch threaded onto this one, addressed to the
 *  other party. It inherits the parent's cwd + result as context server-side
 *  (the daemon enriches metadata.parent_id at compose), but is a fresh, signed,
 *  separately-approved dispatch — not a resumed agent session. */
function FollowUpPanel({
  entry, recipient, onClose,
}: { entry: AnyDispatch; recipient: string; onClose: () => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [task, setTask] = useState("");
  const [error, setError] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: () =>
      api.compose({
        recipient_id: recipient,
        task: task.trim(),
        metadata: { parent_id: entry.dispatch_id },
      }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["sent"] });
      qc.invalidateQueries({ queryKey: ["inbox"] });
      const newId = "dispatch_id" in res ? res.dispatch_id : res.dispatches[0]?.dispatch_id;
      onClose();
      if (newId) navigate(`/dispatch/${newId}`);
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  });

  return (
    <div className="rounded-lg border bg-card p-4 space-y-3">
      <div>
        <div className="font-medium">Follow up with {recipient}</div>
        <div className="text-sm text-muted-foreground mt-0.5">
          Sends a new task on this thread. Their agent inherits this dispatch's
          working directory and result as context, then runs the new task —
          still gated by your trust edge and their approval.
        </div>
      </div>
      <textarea
        value={task}
        onChange={(e) => setTask(e.target.value)}
        rows={4}
        autoFocus
        spellCheck={false}
        placeholder="e.g. now add tests for the parser you just wrote"
        className="w-full rounded-md border bg-background px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring resize-y"
      />
      <div className="flex items-center justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onClose} disabled={send.isPending}>
          Cancel
        </Button>
        <Button size="sm" onClick={() => send.mutate()} disabled={send.isPending || !task.trim()}>
          {send.isPending ? "Sending…" : "Send follow-up"}
        </Button>
      </div>
      {error && <div className="text-xs text-destructive">{error}</div>}
    </div>
  );
}

/** Inline composer for a human note on the thread. Posts a display-only message
 *  that joins the activity stream (both parties + every surface) without ever
 *  reaching the running agent. Available at any status — you can still say
 *  "thanks" or ask a question after the run is done. */
function ReplyComposer({ entry }: { entry: AnyDispatch }) {
  const qc = useQueryClient();
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);
  const post = useMutation({
    mutationFn: () => api.postMessage(entry.dispatch_id, body.trim()),
    onSuccess: () => {
      setBody("");
      qc.invalidateQueries({ queryKey: ["dispatch", entry.dispatch_id] });
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  });

  function submit() {
    if (body.trim() && !post.isPending) post.mutate();
  }

  return (
    <div className="border-t pt-4">
      <div className="flex items-start gap-2">
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => {
            // ⌘/Ctrl+Enter sends; plain Enter keeps a newline.
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              submit();
            }
          }}
          rows={2}
          spellCheck={false}
          placeholder="Reply on this thread… (⌘↵ to send)"
          className="flex-1 rounded-md border bg-background px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring resize-y"
        />
        <Button size="sm" onClick={submit} disabled={post.isPending || !body.trim()}>
          <Send className="size-4" /> {post.isPending ? "Sending…" : "Send"}
        </Button>
      </div>
      <div className="mt-1 text-[11px] text-muted-foreground">
        A note for the other person — it shows in the activity stream but is
        never given to the agent.
      </div>
      {error && <div className="mt-1 text-xs text-destructive">{error}</div>}
    </div>
  );
}

function CancelButton({ dispatchId }: { dispatchId: string }) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const cancel = useMutation({
    mutationFn: () => api.cancelDispatch(dispatchId),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["dispatch", dispatchId] });
      qc.invalidateQueries({ queryKey: ["inbox"] });
      qc.invalidateQueries({ queryKey: ["sent"] });
      setConfirming(false);
    },
  });

  if (!confirming) {
    return (
      <Button variant="outline" size="sm" onClick={() => setConfirming(true)}>
        <Ban className="size-4" /> Cancel
      </Button>
    );
  }
  return (
    <div className="flex items-center gap-2 text-sm">
      <span className="text-muted-foreground">Cancel this dispatch?</span>
      <Button
        size="sm"
        variant="ghost"
        onClick={() => setConfirming(false)}
        disabled={cancel.isPending}
      >
        Keep
      </Button>
      <Button
        size="sm"
        className="bg-destructive hover:bg-destructive/90 text-destructive-foreground"
        onClick={() => cancel.mutate()}
        disabled={cancel.isPending}
      >
        {cancel.isPending ? "Cancelling…" : "Yes, cancel"}
      </Button>
    </div>
  );
}

function Header({ entry }: { entry: AnyDispatch }) {
  return (
    <div className="flex items-start gap-3">
      <div className="grid place-items-center size-11 rounded-full bg-muted text-sm font-semibold shrink-0">
        {initials(entry.sender_id)}
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-semibold">{entry.sender_id}</div>
        <div className="text-xs text-muted-foreground">
          Sent {relativeTime(entry.created_at)}
        </div>
        <div className="mt-3">
          <Markdown text={entry.task} />
        </div>
      </div>
    </div>
  );
}

function TopLevelDecision({ entry }: { entry: AnyDispatch }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [cwd, setCwd] = useState("");
  const [reason, setReason] = useState("");
  const decide = useMutation({
    mutationFn: (decision: "accept" | "reject") =>
      api.decide(
        entry.dispatch_id,
        decision,
        decision === "accept" ? cwd.trim() || undefined : undefined,
        decision === "reject" ? reason.trim() || undefined : undefined,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dispatch", entry.dispatch_id] });
      qc.invalidateQueries({ queryKey: ["inbox"] });
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  });

  return (
    <div className="rounded-lg border bg-amber-50/60 border-amber-200 p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="font-medium">Approval needed</div>
          <div className="text-sm text-muted-foreground mt-0.5">
            Decide whether to run this dispatch. Approvals happen on your
            machine — the broker can't fake them.
          </div>
        </div>
        <div className="flex gap-2 shrink-0">
          <Button
            variant="outline"
            onClick={() => decide.mutate("reject")}
            disabled={decide.isPending}
          >
            <X className="size-4" /> Reject
          </Button>
          <Button
            onClick={() => decide.mutate("accept")}
            disabled={decide.isPending}
          >
            <Check className="size-4" /> Accept
          </Button>
        </div>
      </div>
      <div className="mt-3 space-y-1">
        <label className="text-xs font-medium text-muted-foreground">
          Run in directory <span className="font-normal">(optional)</span>
        </label>
        <input
          value={cwd}
          onChange={(e) => setCwd(e.target.value)}
          placeholder="e.g. ~/Desktop/Yuni — skips the agent's filesystem search"
          spellCheck={false}
          className="w-full rounded-md border bg-background px-2.5 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <div className="text-[11px] text-muted-foreground">
          If the task is about a specific project, pinning its directory saves
          the agent from searching your disk. It's added to the path allowlist
          for this run.
        </div>
      </div>
      <div className="mt-3 space-y-1">
        <label className="text-xs font-medium text-muted-foreground">
          Reason if you reject <span className="font-normal">(optional, shown to sender)</span>
        </label>
        <input
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="e.g. swamped this week — try next Monday"
          className="w-full rounded-md border bg-background px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
        />
      </div>
      {error && (
        <div className="mt-3 text-xs text-destructive">{error}</div>
      )}
    </div>
  );
}

function PendingTools({ entry }: { entry: AnyDispatch }) {
  const items = Object.entries(entry.pending_tools ?? {});
  if (items.length === 0) return null;
  return (
    <div className="space-y-3">
      {items.map(([requestId, t]) => (
        <ToolDecision
          key={requestId}
          dispatchId={entry.dispatch_id}
          requestId={requestId}
          tool={t.tool}
          input={t.input}
        />
      ))}
    </div>
  );
}

function ToolDecision({
  dispatchId, requestId, tool, input,
}: {
  dispatchId: string;
  requestId: string;
  tool: string;
  input: Record<string, unknown>;
}) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const decide = useMutation({
    mutationFn: (decision: "allow" | "deny" | "always" | "session") =>
      api.decideTool(dispatchId, requestId, decision),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dispatch", dispatchId] });
      qc.invalidateQueries({ queryKey: ["inbox"] });
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : String(e)),
  });

  return (
    <div className="rounded-lg border bg-amber-50/60 border-amber-200 p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="text-xs uppercase tracking-wider text-muted-foreground">Tool call</div>
          <div className="font-mono font-semibold mt-0.5">{tool}</div>
          <pre className="mt-2 text-xs font-mono bg-background/60 rounded p-2 overflow-x-auto max-h-48 whitespace-pre-wrap">
            {JSON.stringify(input, null, 2)}
          </pre>
        </div>
        <div className="flex gap-2 shrink-0">
          <Button variant="outline" onClick={() => decide.mutate("deny")} disabled={decide.isPending}>
            <X className="size-4" /> Deny
          </Button>
          <Button onClick={() => decide.mutate("allow")} disabled={decide.isPending}>
            <Check className="size-4" /> Allow
          </Button>
        </div>
      </div>
      {/* Persisted / session shortcuts so the recipient stops re-approving the
          same tool. "Always" writes onto the trust edge; "this session" is
          in-memory for the current daemon run. */}
      <div className="mt-3 flex flex-wrap gap-2 justify-end">
        <Button
          variant="ghost"
          size="sm"
          className="text-xs"
          onClick={() => decide.mutate("session")}
          disabled={decide.isPending}
        >
          <Clock className="size-3.5" /> Allow this session
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="text-xs"
          onClick={() => decide.mutate("always")}
          disabled={decide.isPending}
        >
          <InfinityIcon className="size-3.5" /> Always allow this tool
        </Button>
      </div>
      {error && <div className="mt-3 text-xs text-destructive">{error}</div>}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h2 className="text-xs font-medium uppercase tracking-wider text-muted-foreground mb-2">{title}</h2>
      {children}
    </div>
  );
}

function ScopeSummary({ scopes }: { scopes: InboxEntry["scopes"] }) {
  const tools = scopes.tools ?? [];
  const paths = scopes.paths ?? [];
  const approval = scopes.approval ?? "manual";

  return (
    <div className="rounded-lg border p-4 space-y-2 text-sm">
      <Row label="Tools">
        {tools.length === 0 ? (
          <span className="text-muted-foreground">none</span>
        ) : (
          <div className="flex flex-wrap gap-1">
            {tools.map((t) => <Badge key={t} variant="outline">{t}</Badge>)}
          </div>
        )}
      </Row>
      <Row label="Paths">
        {paths.length === 0 ? (
          // An empty paths list is NOT enforced as "workspace only" — the path
          // gate is skipped, so the agent may touch any path. On a manual edge
          // each call is still approved; on an auto edge there's no boundary.
          approval === "auto" ? (
            <Badge variant="warning">any path — unrestricted (auto-approved)</Badge>
          ) : (
            <span className="text-muted-foreground">any path — each call needs approval</span>
          )
        ) : (
          <ul className="text-xs font-mono space-y-0.5">
            {paths.map((p) => <li key={p}>{p}</li>)}
            <li className="text-muted-foreground">+ workspace</li>
          </ul>
        )}
      </Row>
      <Row label="Approval">
        <Badge variant={approval === "manual" ? "warning" : "muted"}>
          {approval === "manual" ? "Manual — every tool call" : "Auto — no per-tool prompts"}
        </Badge>
      </Row>
      <Row label="Results">
        <Badge variant="muted">
          {(scopes.result_visibility ?? "redacted") === "full"
            ? "Full — sender sees tool result contents"
            : "Redacted — sender sees call + status only"}
        </Badge>
      </Row>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-3">
      <div className="w-20 shrink-0 text-xs text-muted-foreground">{label}</div>
      <div className="flex-1 min-w-0">{children}</div>
    </div>
  );
}
