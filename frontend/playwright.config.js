const { defineConfig, devices } = require("@playwright/test");

const baseURL = process.env.EVA_BASE_URL || "http://127.0.0.1:5000";

module.exports = defineConfig({
  testDir: "./smoke",
  timeout: 30000,
  expect: { timeout: 7000 },
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["Pixel 5"] } },
  ],
});
