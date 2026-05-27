import { Bookmark, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function Saved() {
  return (
    <div className="px-6 py-6 max-w-5xl">
      <div className="flex items-center justify-between mb-1">
        <h1 className="text-2xl font-semibold">Saved Templates</h1>
        <Button>
          <Plus className="size-4" /> Create template
        </Button>
      </div>
      <p className="text-sm text-muted-foreground mb-6">
        Reusable dispatch templates for common tasks
      </p>

      <div className="rounded-lg border p-10 text-center text-sm text-muted-foreground">
        <Bookmark className="size-8 mx-auto mb-3 text-muted-foreground/50" />
        Templates aren't wired up yet. Coming soon.
      </div>
    </div>
  );
}
