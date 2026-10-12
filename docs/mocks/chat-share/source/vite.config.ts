import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
export default defineConfig({
  root: __dirname,
  base: "./",
  define: { __BUILD_SHA__: '"mock"', __BUILD_TIME__: '"mock"' },
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, assetsInlineLimit: 100000000, cssCodeSplit: false,
    rollupOptions: { output: { inlineDynamicImports: true } } },
});
