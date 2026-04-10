const THEME_STORAGE_KEY = "smartai_theme";
const SUPPORTED_THEMES = ["light", "dark"];

function detectPreferredTheme() {
  try {
    if (globalThis.matchMedia && globalThis.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
  } catch {
    // ignore environment limitations
  }
  return "light";
}

export function readStoredTheme() {
  try {
    const stored = String(localStorage.getItem(THEME_STORAGE_KEY) || "").trim().toLowerCase();
    if (SUPPORTED_THEMES.includes(stored)) {
      return stored;
    }
  } catch {
    // ignore storage policy errors
  }
  return detectPreferredTheme();
}

export function writeStoredTheme(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (!SUPPORTED_THEMES.includes(normalized)) {
    return;
  }
  try {
    localStorage.setItem(THEME_STORAGE_KEY, normalized);
  } catch {
    // ignore storage policy errors
  }
}

export { SUPPORTED_THEMES };
