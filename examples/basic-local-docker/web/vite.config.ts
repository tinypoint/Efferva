import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": new URL("./src", import.meta.url).pathname,
        "@efferva/codex-client": new URL(
          "../../../packages/codex-client/src/index.ts",
          import.meta.url,
        ).pathname,
      },
    },
    server: {
      host: "0.0.0.0",
      port: 5173,
      proxy: {
        "/agent": {
          target: env.EFFERVA_API_PROXY_TARGET ?? "http://localhost:8080",
          ws: true,
        },
      },
    },
  };
});
