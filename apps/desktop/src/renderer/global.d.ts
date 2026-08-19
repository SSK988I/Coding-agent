import type { DesktopBridge, DesktopShell } from "../shared/types";

declare global {
  interface Window {
    agent: DesktopBridge;
    desktop: DesktopShell;
  }
}

export {};
