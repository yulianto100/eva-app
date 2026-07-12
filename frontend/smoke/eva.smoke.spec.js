const { test, expect } = require("@playwright/test");

async function expectNoFirstLoadArtifacts(page) {
  await expect(page.locator("#retryBar")).toBeHidden();
  await expect(page.locator("#errorAlert")).toBeHidden();
  await expect(page.getByText("Konversi gagal", { exact: false })).toHaveCount(0);
  await expect(page.getByText("Upload OSM", { exact: false })).toHaveCount(0);
  await expect(page.getByText("OSM/PBF manual", { exact: false })).toHaveCount(0);
}

test("first load is clean", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("button", { name: /Mulai Proses|Lengkapi konfigurasi/i })).toBeVisible();
  await expectNoFirstLoadArtifacts(page);
  await expect(page.getByText("Sumber Data ROAD")).toBeVisible();
  await expect(page.locator("#roadSourceSummary")).toContainText(/Server OSM|Wilayah ROAD gagal dimuat/i);
});

test("ROAD source loads without manual upload copy", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#roadSourcePanel")).toBeVisible();
  await expect(page.getByText("Upload OSM", { exact: false })).toHaveCount(0);
  await expect(page.getByText("OSM/PBF", { exact: false })).toHaveCount(0);
});

test("output selection gates primary CTA", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#convertButton")).toBeDisabled();
  await expect(page.locator("#generateButtonReason")).toContainText(/Pilih minimal satu format output|Unggah.*KML\/KMZ/i);
  await page.locator('.output-mode-input[value="boq"]').check({ force: true });
  await expect(page.locator("#convertButton")).toBeDisabled();
  await expect(page.locator("#generateButtonReason")).toContainText(/Unggah.*KML\/KMZ/i);
});

test("async ROAD init failure does not show retry", async ({ page }) => {
  await page.route("**/api/osm/kecamatan", (route) => route.abort());
  await page.goto("/");
  await expect(page.locator("#roadSourceHelper")).toContainText(/Wilayah ROAD gagal dimuat|Data ROAD wajib memakai server OSM/i);
  await expect(page.locator("#retryBar")).toBeHidden();
});

test("job failure can show retry only after submit", async ({ page }) => {
  await page.route("**/api/osm/kecamatan", (route) => route.fulfill({ json: { regions: [], cities: [], provinces: [] } }));
  await page.goto("/");
  await page.locator('.output-mode-input[value="boq"]').check({ force: true });
  await page.evaluate(() => {
    window.setStatus("Gagal", "Proses gagal. Periksa file atau pengaturan lalu coba lagi.", "error");
  });
  await expect(page.locator("#retryBar")).toBeHidden();
  await page.evaluate(() => {
    window.setError("Proses gagal. Periksa file atau pengaturan lalu coba lagi.", "job");
  });
  await expect(page.locator("#retryBar")).toBeVisible();
});

test("responsive layout keeps CTA and ROAD readable", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/");
  await expect(page.locator("#roadSourcePanel")).toBeVisible();
  await expect(page.locator("#generate-button")).toBeVisible();
  await expectNoFirstLoadArtifacts(page);
});
