import { SUPPORTED_LANGUAGES } from "./i18n";

export function readStoredLanguage() {
  try {
    const value = String(localStorage.getItem("smartai_lang") || "").trim().toLowerCase();
    return SUPPORTED_LANGUAGES.includes(value) ? value : "ru";
  } catch {
    return "ru";
  }
}

export function writeStoredLanguage(value) {
  try {
    const normalized = String(value || "").trim().toLowerCase();
    if (!SUPPORTED_LANGUAGES.includes(normalized)) {
      return;
    }
    localStorage.setItem("smartai_lang", normalized);
  } catch {
    // ignore storage policy errors
  }
}
