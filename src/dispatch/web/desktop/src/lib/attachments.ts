/** Client-side construction of dispatch attachments: read the picked file,
 *  hash it (the sha256 lands in the signed manifest) and base64 the bytes.
 *  Caps mirror the broker's compose validation so a bad pick fails here,
 *  with a readable message, instead of as a 422 after upload. */

export const ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024;
export const ATTACHMENTS_MAX_TOTAL_BYTES = 250 * 1024 * 1024;
export const ATTACHMENTS_MAX_COUNT = 50;

export interface Attachment {
  name: string;
  content_b64: string;
  sha256: string;
  size: number;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Normalize to the manifest name rule (alnum start; alnum/._ - after). */
function safeName(name: string): string {
  const cleaned = name
    .split("")
    .map((c) => (/[A-Za-z0-9._ -]/.test(c) ? c : "_"))
    .join("")
    .replace(/^\.+/, "")
    .slice(0, 128);
  return cleaned || "file";
}

function toBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

export async function fileToAttachment(file: File): Promise<Attachment> {
  if (file.size === 0) throw new Error(`${file.name} is empty`);
  if (file.size > ATTACHMENT_MAX_BYTES) {
    throw new Error(`${file.name} exceeds ${formatBytes(ATTACHMENT_MAX_BYTES)}`);
  }
  if (!crypto?.subtle) {
    throw new Error("attachments need a secure context (crypto.subtle unavailable)");
  }
  const buf = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buf);
  const sha256 = Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return { name: safeName(file.name), content_b64: toBase64(buf), sha256, size: file.size };
}

/** Add picked files to an existing list, enforcing count/total caps and
 *  de-duplicating names the way the broker requires (unique per dispatch). */
export async function addFiles(current: Attachment[], picked: File[]): Promise<Attachment[]> {
  const out = [...current];
  for (const f of picked) {
    if (out.length >= ATTACHMENTS_MAX_COUNT) {
      throw new Error(`at most ${ATTACHMENTS_MAX_COUNT} files per dispatch`);
    }
    const att = await fileToAttachment(f);
    const names = new Set(out.map((a) => a.name));
    let name = att.name;
    for (let i = 2; names.has(name); i++) name = `${i}_${att.name}`;
    att.name = name;
    const total = out.reduce((s, a) => s + a.size, 0) + att.size;
    if (total > ATTACHMENTS_MAX_TOTAL_BYTES) {
      throw new Error(`attachments exceed ${formatBytes(ATTACHMENTS_MAX_TOTAL_BYTES)} total`);
    }
    out.push(att);
  }
  return out;
}
