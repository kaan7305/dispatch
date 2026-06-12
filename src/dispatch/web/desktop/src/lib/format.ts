export function initials(email: string): string {
  // First letter only - Gmail-style avatar.
  const local = email.split("@")[0] ?? email;
  return (local[0] ?? "?").toUpperCase();
}

/** Flatten markdown to plain prose for line-clamped list previews - strips
 *  the syntax (#, **, `, [..](..)) instead of rendering it, since block
 *  elements would fight the two-line clamp. */
export function plainPreview(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " ")            // fenced code blocks
    .replace(/^#{1,6}\s+/gm, "")                 // heading markers
    .replace(/^\s*(?:[-*+]|\d+\.)\s+/gm, "")     // list markers
    .replace(/^\s*>\s?/gm, "")                   // blockquote markers
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")   // links/images → label
    .replace(/(\*\*|__|\*|_|~~|`)/g, "")         // emphasis / inline code
    .replace(/\s+/g, " ")
    .trim();
}

const RTF = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

export function relativeTime(iso: string): string {
  const diffMs = new Date(iso).getTime() - Date.now();
  const diffMin = Math.round(diffMs / 60_000);
  if (Math.abs(diffMin) < 60) return RTF.format(diffMin, "minute");
  const diffHr = Math.round(diffMin / 60);
  if (Math.abs(diffHr) < 48) return RTF.format(diffHr, "hour");
  const diffDay = Math.round(diffHr / 24);
  return RTF.format(diffDay, "day");
}
