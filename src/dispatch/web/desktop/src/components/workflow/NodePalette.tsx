import { Bell, Clock, Play, Send } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { WorkflowNodeType } from "@/lib/workflowApi";

interface PaletteItem {
  type: WorkflowNodeType;
  title: string;
  description: string;
  stripe: string;
  icon: LucideIcon;
  iconClass: string;
}

const ITEMS: PaletteItem[] = [
  {
    type: "trigger.manual",
    title: "Trigger",
    description: "Start the workflow with manual input.",
    stripe: "bg-gradient-to-b from-indigo-500 to-violet-600",
    icon: Play,
    iconClass: "text-indigo-600",
  },
  {
    type: "dispatch",
    title: "Dispatch",
    description: "Send a task to one trusted teammate.",
    stripe: "bg-neutral-300",
    icon: Send,
    iconClass: "text-neutral-700",
  },
  {
    type: "notify",
    title: "Notify",
    description: "Show a local macOS notification.",
    stripe: "bg-amber-400",
    icon: Bell,
    iconClass: "text-amber-700",
  },
  {
    type: "wait_reply",
    title: "Wait reply",
    description: "Pause until a follow-up dispatch arrives.",
    stripe: "bg-sky-400",
    icon: Clock,
    iconClass: "text-sky-700",
  },
];

function onDragStart(event: React.DragEvent<HTMLDivElement>, type: WorkflowNodeType) {
  event.dataTransfer.setData("application/reactflow", type);
  event.dataTransfer.effectAllowed = "move";
}

export function NodePalette() {
  return (
    <aside className="w-56 shrink-0 border-r bg-background flex flex-col">
      <div className="px-4 py-3 border-b">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Nodes
        </h2>
        <p className="mt-1 text-[11px] text-muted-foreground">
          Drag onto the canvas
        </p>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {ITEMS.map((item) => {
          const Icon = item.icon;
          return (
            <div
              key={item.type}
              draggable
              onDragStart={(e) => onDragStart(e, item.type)}
              className="group flex overflow-hidden rounded-md border bg-card cursor-grab active:cursor-grabbing hover:border-foreground/20 hover:shadow-sm transition-all"
            >
              <div className={`w-1 shrink-0 ${item.stripe}`} />
              <div className="flex-1 px-3 py-2 flex items-start gap-2">
                <Icon className={`size-4 mt-0.5 shrink-0 ${item.iconClass}`} />
                <div className="min-w-0">
                  <div className="text-sm font-medium leading-tight">
                    {item.title}
                  </div>
                  <p className="mt-0.5 text-[11px] text-muted-foreground leading-snug">
                    {item.description}
                  </p>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}
