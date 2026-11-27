import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: "replay",
  base: "./",
  publicDir: false,
  plugins: [react()],
  build: { outDir: "../../web/dist/replay", emptyOutDir: true },
});
