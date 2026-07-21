// vite.config.ts runs through esbuild in Node at build time, but the typecheck gate has no
// @types/node — declare the one Node API the config uses (the build-stamp git lookup)
// rather than pull in the whole dependency. Ambient (this file has no imports/exports), so
// it just makes the module resolvable; app code never imports it.
declare module "node:child_process" {
  export function execSync(
    command: string,
    options?: { stdio?: unknown[] },
  ): { toString(): string };
}
