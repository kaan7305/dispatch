import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Plus, Save, Trash2, X } from "@/lib/icons";

import { contexts, type ContextFile } from "@/lib/contextApi";
import { Button } from "@/components/ui/button";

export default function ContextEditor() {
  const { id } = useParams<{ id: string }>();
  const isNew = !id || id === "new";
  const navigate = useNavigate();
  const qc = useQueryClient();

  const existing = useQuery({
    queryKey: ["context", id],
    queryFn: () => contexts.get(id!),
    enabled: !isNew,
  });

  const [name, setName] = useState("Untitled context");
  const [description, setDescription] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [files, setFiles] = useState<ContextFile[]>([]);

  useEffect(() => {
    if (!existing.data) return;
    setName(existing.data.name);
    setDescription(existing.data.description);
    setSystemPrompt(existing.data.system_prompt);
    setFiles(existing.data.files);
  }, [existing.data]);

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        name: name.trim() || "Untitled context",
        description,
        system_prompt: systemPrompt,
        files: files
          .filter((f) => f.path.trim() !== "")
          .map((f) => ({ path: f.path.trim(), content: f.content })),
      };
      if (isNew) {
        const res = await contexts.create(body);
        return res.context_id;
      }
      await contexts.update(id!, body);
      return id!;
    },
    onSuccess: (cid) => {
      qc.invalidateQueries({ queryKey: ["contexts"] });
      qc.invalidateQueries({ queryKey: ["context", cid] });
      if (isNew) navigate(`/contexts/${cid}/edit`, { replace: true });
    },
  });

  const remove = useMutation({
    mutationFn: () => contexts.remove(id!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["contexts"] });
      navigate("/contexts");
    },
  });

  function addFile() {
    setFiles((curr) => [...curr, { path: "", content: "" }]);
  }
  function updateFile(i: number, patch: Partial<ContextFile>) {
    setFiles((curr) =>
      curr.map((f, idx) => (idx === i ? { ...f, ...patch } : f)),
    );
  }
  function removeFile(i: number) {
    setFiles((curr) => curr.filter((_, idx) => idx !== i));
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-zinc-200 bg-white px-4 h-14 shrink-0">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => navigate("/contexts")}
          className="text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" /> Context
        </Button>
        <div className="h-5 w-px bg-zinc-200" />
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Untitled context"
          className="flex-1 max-w-sm bg-transparent border-0 text-[15px] font-semibold focus:outline-none focus:ring-0 placeholder:text-muted-foreground/60"
        />
        <span className="text-xs text-muted-foreground hidden sm:inline">
          {save.isPending
            ? "Saving…"
            : save.error instanceof Error
              ? ""
              : "Saved"}
        </span>
        {save.error instanceof Error && (
          <span className="text-xs text-destructive truncate max-w-xs">
            {save.error.message}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {!isNew && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                if (confirm(`Delete "${name}"? This cannot be undone.`)) {
                  remove.mutate();
                }
              }}
              disabled={remove.isPending}
              className="text-destructive hover:text-destructive"
            >
              <Trash2 className="size-3.5" /> Delete
            </Button>
          )}
          <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
            <Save className="size-3.5" /> Save
          </Button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-6 space-y-8">
          <section className="space-y-2">
            <label
              htmlFor="ctx-desc"
              className="block text-xs font-medium uppercase tracking-wide text-muted-foreground"
            >
              Description
            </label>
            <input
              id="ctx-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Short note shown in the library list"
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </section>

          <section className="space-y-2">
            <label
              htmlFor="ctx-sys"
              className="block text-xs font-medium uppercase tracking-wide text-muted-foreground"
            >
              System prompt
            </label>
            <p className="text-[11px] text-muted-foreground">
              Sent to Claude as the system message before any user prompt.
              References like <code>{"{{ctx.key}}"}</code> resolve to the
              workflow's trigger input at run time.
            </p>
            <textarea
              id="ctx-sys"
              rows={10}
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder="You are an editor for our internal blog. Always …"
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring resize-y"
            />
          </section>

          <section className="space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Files
                </h2>
                <p className="text-[11px] text-muted-foreground mt-1">
                  Materialised into the recipient's workspace before any agent
                  runs. Paths are workspace-relative; <code>..</code> escapes are
                  rejected on the recipient side.
                </p>
              </div>
              <Button variant="outline" size="sm" onClick={addFile}>
                <Plus className="size-3.5" /> Add file
              </Button>
            </div>

            {files.length === 0 ? (
              <div className="rounded-md border border-dashed border-zinc-300 p-6 text-center text-xs text-muted-foreground">
                No files. Use the button above to ship a CLAUDE.md, reference
                data, or anything else the recipient's agent should have on hand.
              </div>
            ) : (
              <div className="space-y-3">
                {files.map((f, i) => (
                  <div
                    key={i}
                    className="rounded-md border border-zinc-200 bg-white"
                  >
                    <div className="flex items-center gap-2 border-b border-zinc-100 px-3 py-2">
                      <input
                        value={f.path}
                        onChange={(e) => updateFile(i, { path: e.target.value })}
                        placeholder="path (e.g. CLAUDE.md)"
                        className="flex-1 bg-transparent border-0 text-sm font-mono focus:outline-none focus:ring-0"
                      />
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onClick={() => removeFile(i)}
                        aria-label="Remove file"
                        className="h-7 w-7 shrink-0 text-muted-foreground hover:text-destructive"
                      >
                        <X className="size-3.5" />
                      </Button>
                    </div>
                    <textarea
                      rows={6}
                      value={f.content}
                      onChange={(e) => updateFile(i, { content: e.target.value })}
                      placeholder="file contents (templatable)"
                      className="w-full rounded-b-md bg-background px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-ring resize-y border-0"
                    />
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
