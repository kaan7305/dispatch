import { cn } from "@/lib/utils";

interface Props<T extends string> {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  variant?: "pill" | "underline";
}

export function SegmentedTabs<T extends string>({
  options, value, onChange, variant = "pill",
}: Props<T>) {
  if (variant === "pill") {
    return (
      <div className="inline-flex rounded-full border bg-muted/40 p-0.5 text-sm">
        {options.map((o) => (
          <button
            key={o.value}
            onClick={() => onChange(o.value)}
            className={cn(
              "rounded-full px-4 py-1 transition-colors",
              value === o.value
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {o.label}
          </button>
        ))}
      </div>
    );
  }
  return (
    <div className="flex items-center gap-1 text-sm">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            "rounded-full px-3 py-1 transition-colors",
            value === o.value
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
