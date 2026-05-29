import type { CSSProperties } from "react";

export function initials(email: string): string {
  const local = email.split("@")[0] ?? email;
  return (local[0] ?? "?").toUpperCase();
}

/** Friendly name from an email — "Kaan Eroltu" from "kaan.eroltu@x.com". */
export function displayName(email: string): string {
  const local = email.split("@")[0] ?? email;
  return local
    .split(/[._\-+]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

/** Deterministic hue 0..359 derived from a string — used to color avatars
 *  so the same person always gets the same gradient. */
export function avatarHue(seed: string): number {
  let h = 0;
  for (let i = 0; i < seed.length; i++) {
    h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  }
  return h % 360;
}

/** Pleasant two-stop gradient that's always readable with white text. */
export function avatarStyle(seed: string): CSSProperties {
  const h = avatarHue(seed);
  return {
    background: `linear-gradient(135deg, hsl(${h} 70% 58%), hsl(${(h + 36) % 360} 70% 48%))`,
    color: "white",
  };
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
