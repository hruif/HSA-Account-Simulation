// Pure helpers: same input, same output, no DOM and no network. Kept apart from app.js
// so they can be read, reasoned about and tested on their own.

// Money stays in integer cents. Dollars text is parsed without floats.
export function formatCents(cents) {
  const whole = Math.floor(cents / 100).toLocaleString("en-US");
  return `$${whole}.${String(cents % 100).padStart(2, "0")}`;
}

// "25", "$25", "25.5", " 25.50 " -> cents. Anything else -> null.
export function parseDollars(text) {
  const match = /^\s*\$?(\d{1,7})(?:\.(\d{1,2}))?\s*$/.exec(text);
  if (!match) return null;
  return Number(match[1]) * 100 + Number((match[2] || "").padEnd(2, "0"));
}

// SQLite writes "2026-09-17 18:19:30.123" in UTC; Date needs the T and the Z.
export function formatTime(sqliteUtc) {
  return new Date(sqliteUtc.replace(" ", "T") + "Z").toLocaleString();
}

export function labelFor(category) {
  const words = category.replace(/_/g, " ");
  return words[0].toUpperCase() + words.slice(1);
}
