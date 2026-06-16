import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, MessageSquare, Trash2, TriangleAlert } from "@/lib/icons";

import { api, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";

export default function Settings() {
  const qc = useQueryClient();
  const phone = useQuery({
    queryKey: ["phone"],
    queryFn: () => api.phone(),
  });

  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Seed the input from the server once it loads (and on external changes).
  useEffect(() => {
    if (phone.data) setDraft(phone.data.phone ?? "");
  }, [phone.data]);

  const save = useMutation({
    mutationFn: (value: string | null) => api.setPhone(value),
    onSuccess: (data) => {
      setError(null);
      qc.setQueryData(["phone"], data);
    },
    onError: (e) => {
      setError(e instanceof ApiError ? e.message : "Could not save your number.");
    },
  });

  const current = phone.data?.phone ?? null;
  const smsEnabled = phone.data?.sms_enabled ?? false;
  const trimmed = draft.trim();
  const dirty = trimmed !== (current ?? "");

  return (
    <div className="px-6 py-6 max-w-2xl">
      <h1 className="text-2xl font-semibold mb-1">Settings</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Notifications and account preferences
      </p>

      <section className="rounded-lg border p-5">
        <div className="flex items-center gap-3 mb-1">
          <div className="grid place-items-center size-9 rounded-lg bg-muted shrink-0">
            <MessageSquare className="size-4" />
          </div>
          <div>
            <h2 className="font-semibold leading-tight">SMS notifications</h2>
            <p className="text-xs text-muted-foreground">
              Get a text whenever someone sends you a dispatch, even while your
              daemon is offline.
            </p>
          </div>
        </div>

        <form
          className="mt-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (dirty && trimmed) save.mutate(trimmed);
          }}
        >
          <label htmlFor="phone" className="text-sm font-medium">
            Phone number
          </label>
          <div className="mt-1.5 flex items-center gap-2">
            <input
              id="phone"
              type="tel"
              inputMode="tel"
              autoComplete="tel"
              placeholder="+14155550123"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setError(null);
              }}
              disabled={phone.isLoading || save.isPending}
              className="flex-1 rounded-md border bg-secondary/40 px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-60"
            />
            <Button type="submit" disabled={!dirty || !trimmed || save.isPending}>
              <Check className="size-4" /> {save.isPending ? "Saving…" : "Save"}
            </Button>
            {current && (
              <Button
                type="button"
                variant="ghost"
                className="text-destructive"
                disabled={save.isPending}
                onClick={() => {
                  setDraft("");
                  save.mutate(null);
                }}
              >
                <Trash2 className="size-4" /> Remove
              </Button>
            )}
          </div>

          <p className="mt-2 text-xs text-muted-foreground">
            Use E.164 format: a leading <code>+</code> and country code, e.g.
            <code> +14155550123</code>.
          </p>

          {error && <p className="mt-2 text-xs text-destructive">{error}</p>}

          {!error && current && !smsEnabled && (
            <p className="mt-3 flex items-start gap-1.5 text-xs text-amber-600 dark:text-amber-500">
              <TriangleAlert className="size-3.5 mt-px shrink-0" />
              Number saved, but the broker has no SMS provider configured yet, so
              texts won’t send until Twilio is set up.
            </p>
          )}
          {!error && current && smsEnabled && (
            <p className="mt-3 flex items-center gap-1.5 text-xs text-green-600 dark:text-green-500">
              <Check className="size-3.5 shrink-0" />
              SMS notifications are on for {current}.
            </p>
          )}
        </form>
      </section>
    </div>
  );
}
