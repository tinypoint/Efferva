import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  return {
    plugins: [react()],
    resolve: {
      alias: {
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
        "/codex": {
          target: env.EFFERVA_API_PROXY_TARGET ?? "http://localhost:8080",
          ws: true,
        },
        "/claude": {
          target: env.EFFERVA_API_PROXY_TARGET ?? "http://localhost:8080",
        },
      },
    },
  };
});
