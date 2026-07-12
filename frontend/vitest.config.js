import { defineConfig } from "vitest/config";

export default defineConfig({
  server: {
    fs: {
      allow: [".."],
    },
  },
  test: {
    exclude: ["smoke/**", "node_modules/**"],
    environment: "jsdom",
    setupFiles: ["./tests/setup.js"],
    globals: true,
    coverage: {
      include: ["src/utils.js"],
      reporter: ["text", "lcov"],
    },
  },
});
