import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Ban, Check, X } from "lucide-react";

import { api, type DispatchEvent, type InboxEntry } from "@/lib/api";
import { openDispatchWatch } from "@/lib/ws";
import { initials, relativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/StatusBadge";
import { EventStream } from "@/components/EventStream";

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
  status: InboxEntry["status"];
  created_at: string;
  scopes?: InboxEntry["scopes"];
  pending_tools?: InboxEntry["pending_tools"];
  events?: DispatchEvent[];
}

function DetailBody({
  entry, onBack,
}: { entry: AnyDispatch; onBack: () => void }) {
  const session = useQuery({ queryKey: ["session"], queryFn: () => api.session() });
  const me = session.data?.user_id ?? "";
  const isRecipient = !!entry.recipient_id && entry.recipient_id === me;
  const decisionPending = entry.status === "pending" || entry.status === "delivered";
  const cancellable = !(
    entry.status === "completed" ||
    entry.status === "failed" ||
    entry.status === "denied" ||
    entry.status === "expired" ||
    entry.status === "cancelled"
  );

  return (
    <div className="h-full flex flex-col">
      <div className="px-6 py-4 border-b flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft className="size-4" /> Back
        </Button>
        <StatusBadge status={entry.status} />
        {cancellable && (
          <div className="ml-auto">
            <CancelButton dispatchId={entry.dispatch_id} />
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-6 space-y-6">
          <Header entry={entry} />
          {decisionPending && isRecipient && <TopLevelDecision entry={entry} />}
          {isRecipient && <PendingTools entry={entry} />}
          {entry.scopes && (
            <Section title="Scope">
              <ScopeSummary scopes={entry.scopes} />
            </Section>
          )}
          <Section title="Activity">
            <EventStream
              events={entry.events ?? []}
              viewerRole={isRecipient ? "recipient" : "watcher"}
            />
          </Section>
        </div>
      </div>
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
        <p className="mt-3 text-base leading-relaxed whitespace-pre-wrap">
          {entry.task}
        </p>
      </div>
    </div>
  );
}

function TopLevelDecision({ entry }: { entry: AnyDispatch }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const decide = useMutation({
    mutationFn: (decision: "accept" | "reject") =>
      api.decide(entry.dispatch_id, decision),
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
    mutationFn: (decision: "allow" | "deny") =>
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
          <span className="text-muted-foreground">workspace only</span>
        ) : (
          <ul className="text-xs font-mono space-y-0.5">
            {paths.map((p) => <li key={p}>{p}</li>)}
          </ul>
        )}
      </Row>
      <Row label="Approval">
        <Badge variant={approval === "manual" ? "warning" : "muted"}>
          {approval === "manual" ? "Manual — every tool call" : "Auto — no per-tool prompts"}
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
