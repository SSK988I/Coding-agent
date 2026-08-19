/// <reference types="vite/client" />

// Keep side-effect stylesheet imports resolvable even when the editor opens
// this file with the workspace TypeScript service instead of Vite's project.
declare module "*.css";
