// vite.config.ts runs through esbuild in Node at build time, and a couple of tests read
// repo files off disk, but the typecheck gate has no @types/node — declare just the Node
// APIs those two use rather than pull in the whole dependency. Ambient (this file has no
// imports/exports), so it only makes the modules resolvable; app code never imports them.
declare module "node:child_process" {
  export function execSync(
    command: string,
    options?: { stdio?: unknown[] },
  ): { toString(): string };
}

declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string;
  // `cssTokens.test.ts` walks src/ for every stylesheet: the gate it runs is a loop, so it has
  // to find the files itself rather than be handed a list that can silently fall behind.
  export function readdirSync(
    path: string,
    options: { withFileTypes: true },
  ): { name: string; isDirectory(): boolean }[];
}
