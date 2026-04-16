import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 13036,
    host: true,
    allowedHosts: ["voice.ezagent.chat"],
    proxy: {
      "/api": {
        target: "http://localhost:8089",
      },
    },
  },
});
