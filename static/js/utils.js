(function (root, factory) {
  const utils = factory();
  root.EVAUtils = { ...(root.EVAUtils || {}), ...utils };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = utils;
  }
})(typeof globalThis !== "undefined" ? globalThis : window, function () {
  "use strict";

  const OUTPUT_TARGET_ORDER = ["dxf", "boq", "astri", "hpdb", "kmz"];
  const OUTPUT_TARGET_LABELS = {
    dxf: "DXF (CAD)",
    boq: "BOQ (Excel)",
    astri: "ASTRI (Excel)",
    hpdb: "HPDB (Excel)",
    kmz: "KMZ Standar",
  };
  const NETWORK_TYPE_LABELS = {
    AE: "AE / Aerial",
    UG: "UG / Underground",
  };
  const SCOPE_TYPE_LABELS = {
    Cluster: "Cluster / Distribution",
    Feeder: "Feeder / Backbone",
  };
  const FEEDER_ALLOWED_TARGETS = new Set(["dxf", "boq", "kmz"]);
  const REQUIRED_KML_FOLDERS = [
    "BOUNDARY FAT",
    "FAT",
    "HP COVER",
    "HP UNCOVER",
    "EXISTING POLE EMR 7-2.5",
    "EXISTING POLE EMR 7-3",
    "EXISTING POLE EMR 7-4",
    "EXISTING POLE EMR 9-4",
    "EXISTING POLE PARTNER 7-4",
    "EXISTING POLE PARTNER 9-4",
    "NEW POLE 7-2.5",
    "NEW POLE 7-3",
    "NEW POLE 7-4",
    "NEW POLE 9-4",
    "DISTRIBUTION CABLE",
    "SLACK HANGER",
    "SLING WIRE",
    "SUBDUCT 32/28",
    "SUBDUCT 40/34",
    "TALI PANCING",
    "NEW HH 40X40X60",
    "NEW HH 80X80X130",
    "TWEY",
  ];
  const OPTIONAL_KML_FOLDERS = [
    "FDT",
    "CABLE",
    "JOINT CLOSURE",
    "NEW POLE 7-5",
    "NEW POLE 9-5",
    "EXISTING POLE EMR 7-5",
    "EXISTING POLE EMR 9-5",
  ];
  const STANDARD_KML_FOLDERS = [...REQUIRED_KML_FOLDERS, ...OPTIONAL_KML_FOLDERS];

  function normalizeKmlFolderName(value) {
    return String(value || "")
      .trim()
      .replace(/\s+/g, " ")
      .toUpperCase()
      .replace(/(?<=\d)\s*,\s*(?=\d)/g, ".")
      .replace(/(?<=\d)\s*\.\s*(?=\d)/g, ".");
  }

  function createKmlFolderAliases(folders = STANDARD_KML_FOLDERS) {
    const aliases = new Map(folders.map((folderName) => [normalizeKmlFolderName(folderName), folderName]));
    [
      ["TALI PANICNG", "TALI PANCING"],
      ["NEW HH 40 X 40 X 60", "NEW HH 40X40X60"],
      ["NEW HH 80 X 80 X 130", "NEW HH 80X80X130"],
      ["T-WAY", "TWEY"],
      ["T-WEY", "TWEY"],
    ].forEach(([alias, canonical]) => aliases.set(normalizeKmlFolderName(alias), canonical));
    return aliases;
  }

  function canonicalStandardFolder(folderName, folders = STANDARD_KML_FOLDERS, aliases = createKmlFolderAliases(folders)) {
    const canonical = aliases.get(normalizeKmlFolderName(folderName));
    return canonical && new Set(folders).has(canonical) ? canonical : "";
  }

  function formatNumber(value) {
    return Number(value || 0).toLocaleString("id-ID");
  }

  function outputTargetsFromMode(mode, options = {}) {
    const dxfConversionEnabled = options.dxfConversionEnabled !== false;
    const parsedTargets = String(mode || "")
      .split("_")
      .filter(Boolean)
      .filter((target) => target !== "dxf" || dxfConversionEnabled);
    return new Set(parsedTargets);
  }

  function modeHas(target, mode, options = {}) {
    return outputTargetsFromMode(mode, options).has(target);
  }

  function modeFromTargets(targets, options = {}) {
    const dxfConversionEnabled = options.dxfConversionEnabled !== false;
    const defaultOutputMode = options.defaultOutputMode || "";
    const list = Array.isArray(targets) ? targets : Array.from(targets || []);
    const ordered = OUTPUT_TARGET_ORDER
      .filter((target) => target !== "dxf" || dxfConversionEnabled)
      .filter((target) => list.includes(target));
    return ordered.length ? ordered.join("_") : defaultOutputMode;
  }

  function selectedOutputTargets(mode, options = {}) {
    const targets = outputTargetsFromMode(mode, options);
    const dxfConversionEnabled = options.dxfConversionEnabled !== false;
    return OUTPUT_TARGET_ORDER
      .filter((target) => target !== "dxf" || dxfConversionEnabled)
      .filter((target) => targets.has(target));
  }

  function normalizeNetworkType(type) {
    const normalized = String(type || "AE").trim().toUpperCase();
    return NETWORK_TYPE_LABELS[normalized] ? normalized : "AE";
  }

  function networkTypeLabel(type) {
    return NETWORK_TYPE_LABELS[normalizeNetworkType(type)] || NETWORK_TYPE_LABELS.AE;
  }

  function fileFormatLabel(fileOrName) {
    const rawName = typeof fileOrName === "string" ? fileOrName : fileOrName?.name;
    const name = String(rawName || "").toLowerCase();
    if (name.endsWith(".osm.pbf")) return "OSM.PBF";
    const ext = String(rawName || "").split(".").pop();
    return ext ? ext.toUpperCase() : "File";
  }

  function isWarningStatus(status) {
    const normalized = String(status || "").toLowerCase();
    return normalized === "warning" || normalized === "unmapped" || normalized === "skipped" || normalized === "error";
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function generateButtonLabel() {
    return "Mulai Proses";
  }

  function validationReadiness(input = {}) {
    const state = input.state || {};
    const config = input.config || {};
    const targets = Array.from(input.targets || selectedOutputTargets(state.outputMode, config));
    const reasons = [];
    const notes = [];
    const hasFiles = Number(state.fileCount || 0) > 0;
    const kmlFileCount = Number(state.kmlFileCount || 0);
    const osmFileCount = Number(state.osmFileCount || 0);
    const selectedFolderCount = Number(state.selectedFolderCount || 0);
    const availableFolderCount = Number(state.availableFolderCount || 0);
    const needsKml = targets.some((target) => ["boq", "astri", "hpdb", "kmz"].includes(target));

    if (!targets.length) reasons.push("Pilih minimal satu format output");
    if (!hasFiles) reasons.push("Unggah file KML/KMZ terlebih dahulu");
    if (needsKml && !kmlFileCount) reasons.push("Unggah file KML/KMZ terlebih dahulu");
    if (state.outputMode === "kmz" && kmlFileCount !== 1) {
      reasons.push("KMZ Standar hanya membutuhkan satu file KML/KMZ");
    }
    if (targets.includes("boq") && config.boqTemplateReady === false) {
      reasons.push(`Template BOQ ${state.networkType || "AE"} belum tersedia`);
    }
    if ((targets.includes("astri") || targets.includes("hpdb")) && config.hpdbTemplateReady === false) {
      reasons.push(`Template ASTRI/HPDB ${state.networkType || "AE"} belum tersedia`);
    }
    if (state.kmlValidationError) reasons.push(state.kmlValidationError);
    if (state.isProcessing) reasons.push("Proses sedang berjalan");
    if (targets.includes("dxf")) {
      notes.push("Data ROAD otomatis diambil dari Server OSM berdasarkan wilayah yang dipilih.");
    }
    if (kmlFileCount && state.kmlValidationState === "validating") {
      notes.push("Validasi folder KML/KMZ sedang berjalan.");
    }

    return {
      ready: reasons.length === 0,
      reasons,
      notes,
      targets,
      hasFiles,
      estimatedOutput: targets.length > 1 ? "ZIP" : "file tunggal",
    };
  }

  return {
    OUTPUT_TARGET_LABELS,
    OUTPUT_TARGET_ORDER,
    NETWORK_TYPE_LABELS,
    REQUIRED_KML_FOLDERS,
    OPTIONAL_KML_FOLDERS,
    STANDARD_KML_FOLDERS,
    canonicalStandardFolder,
    createKmlFolderAliases,
    escapeHtml,
    fileFormatLabel,
    formatNumber,
    generateButtonLabel,
    isWarningStatus,
    modeFromTargets,
    modeHas,
    networkTypeLabel,
    normalizeKmlFolderName,
    normalizeNetworkType,
    outputTargetsFromMode,
    selectedOutputTargets,
    validationReadiness,
    SCOPE_TYPE_LABELS,
    FEEDER_ALLOWED_TARGETS,
  };
});

