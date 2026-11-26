import { useCallback, useEffect, useState } from "react";

export type DesktopTheme = "light" | "dark";

const STORAGE_KEY = "coding-agent:desktop-theme-v2";

function readInitialTheme(): DesktopTheme {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === "dark" || saved === "light") return saved;
  } catch {
    // Ignore storage access errors in restricted environments.
  }
  return "dark";
}

export function useTheme() {
  const [theme, setTheme] = useState<DesktopTheme>(readInitialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Ignore storage persistence errors.
    }
    void window.desktop?.setTheme?.(theme).catch(() => undefined);
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((current) => (current === "light" ? "dark" : "light"));
  }, []);

  return { theme, setTheme, toggleTheme };
}
