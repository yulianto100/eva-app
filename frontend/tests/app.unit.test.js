import * as utils from "../src/utils.js";

describe("EVA utility functions", () => {
  it("normalizeKmlFolderName trims, collapses spaces, and uppercases", () => {
    expect(utils.normalizeKmlFolderName("  new   pole 7-3 ")).toBe("NEW POLE 7-3");
  });

  it("formatNumber uses Indonesian locale", () => {
    expect(utils.formatNumber(1234567)).toBe("1.234.567");
  });

  it("outputTargetsFromMode parses multi-output mode", () => {
    expect(Array.from(utils.outputTargetsFromMode("boq_hpdb"))).toEqual(["boq", "hpdb"]);
  });

  it("outputTargetsFromMode filters locked DXF", () => {
    expect(Array.from(utils.outputTargetsFromMode("dxf_boq", { dxfConversionEnabled: false }))).toEqual(["boq"]);
  });

  it("outputTargetsFromMode falls back to default", () => {
    expect(Array.from(utils.outputTargetsFromMode("", { defaultOutputMode: "hpdb" }))).toEqual(["hpdb"]);
  });

  it("modeFromTargets orders targets consistently", () => {
    expect(utils.modeFromTargets(["hpdb", "boq", "kmz"])).toBe("boq_hpdb_kmz");
  });

  it("modeFromTargets respects locked DXF", () => {
    expect(utils.modeFromTargets(["dxf", "boq"], { dxfConversionEnabled: false })).toBe("boq");
  });

  it("modeHas detects target membership", () => {
    expect(utils.modeHas("hpdb", "boq_hpdb")).toBe(true);
    expect(utils.modeHas("kmz", "boq_hpdb")).toBe(false);
  });

  it("selectedOutputTargets returns app order", () => {
    expect(utils.selectedOutputTargets("kmz_boq_hpdb")).toEqual(["boq", "hpdb", "kmz"]);
  });

  it("normalizeNetworkType accepts AE and UG", () => {
    expect(utils.normalizeNetworkType(" ug ")).toBe("UG");
    expect(utils.normalizeNetworkType("bad")).toBe("AE");
  });

  it("networkTypeLabel returns readable labels", () => {
    expect(utils.networkTypeLabel("UG")).toBe("UG / Underground");
  });

  it("fileFormatLabel detects osm.pbf", () => {
    expect(utils.fileFormatLabel({ name: "roads.osm.pbf" })).toBe("OSM.PBF");
  });

  it("fileFormatLabel detects regular extension", () => {
    expect(utils.fileFormatLabel({ name: "cluster.kmz" })).toBe("KMZ");
  });

  it("isWarningStatus identifies warning-like statuses", () => {
    expect(utils.isWarningStatus("unmapped")).toBe(true);
    expect(utils.isWarningStatus("mapped")).toBe(false);
  });

  it("escapeHtml escapes dangerous characters", () => {
    expect(utils.escapeHtml(`<img src=x onerror="1">`)).toBe("&lt;img src=x onerror=&quot;1&quot;&gt;");
  });

  it("generateButtonLabel handles single output", () => {
    expect(utils.generateButtonLabel({ targets: ["hpdb"] })).toBe("Generate HPDB Excel");
  });

  it("generateButtonLabel handles multiple outputs", () => {
    expect(utils.generateButtonLabel({ targets: ["boq", "hpdb"] })).toBe("Generate Selected Outputs");
  });

  it("canonicalStandardFolder resolves built-in folder aliases", () => {
    expect(utils.canonicalStandardFolder("new hh 40 x 40 x 60")).toBe("NEW HH 40X40X60");
  });

  it("validationReadiness requires files", () => {
    const summary = utils.validationReadiness({ state: { fileCount: 0, outputMode: "boq" } });
    expect(summary.ready).toBe(false);
    expect(summary.reasons).toContain("Upload OSM/PBF atau KML/KMZ terlebih dahulu");
  });

  it("validationReadiness marks BOQ ready with KML and template", () => {
    const summary = utils.validationReadiness({
      targets: ["boq"],
      state: { fileCount: 1, kmlFileCount: 1, networkType: "AE", selectedFolderCount: 1, availableFolderCount: 1 },
      config: { boqTemplateReady: true },
    });
    expect(summary.ready).toBe(true);
  });

  it("validationReadiness reports template problems", () => {
    const summary = utils.validationReadiness({
      targets: ["hpdb"],
      state: { fileCount: 1, kmlFileCount: 1, networkType: "UG" },
      config: { hpdbTemplateReady: false },
    });
    expect(summary.ready).toBe(false);
    expect(summary.reasons[0]).toContain("Template ASTRI/HPDB UG");
  });
});
