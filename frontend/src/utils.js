const OUTPUT_ORDER = ["boq", "hpdb", "kmz", "dxf"];
export function normalizeKmlFolderName(value) { return String(value || "").trim().replace(/\s+/g, " ").toUpperCase(); }
export function formatNumber(value) { return new Intl.NumberFormat("id-ID").format(Number(value || 0)); }
export function escapeHtml(value) { return String(value == null ? "" : value).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/\"/g,"&quot;").replace(/'/g,"&#39;"); }
export function normalizeNetworkType(value) { return String(value || "AE").trim().toUpperCase() === "UG" ? "UG" : "AE"; }
export function networkTypeLabel(value) { return normalizeNetworkType(value) === "UG" ? "UG / Underground" : "AE / Aerial"; }
export function outputTargetsFromMode(outputMode, config = {}) { const fallback = outputMode || config.defaultOutputMode || ""; return new Set(String(fallback).toLowerCase().split("_").filter(Boolean).filter(t => t !== "dxf" || config.dxfConversionEnabled)); }
export function modeFromTargets(targets, config = {}) { const selected = new Set((targets || []).filter(t => t !== "dxf" || config.dxfConversionEnabled)); return OUTPUT_ORDER.filter(t => selected.has(t)).join("_"); }
export function modeHas(target, outputMode, config = {}) { return outputTargetsFromMode(outputMode, config).has(target); }
export function selectedOutputTargets(outputMode, config = {}) { const targets = outputTargetsFromMode(outputMode, config); return OUTPUT_ORDER.filter(t => targets.has(t)); }
export function fileFormatLabel(file) { const name = String(file?.name || "").toLowerCase(); if (name.endsWith(".osm.pbf")) return "OSM.PBF"; const match = name.match(/\.([a-z0-9]+)$/i); return match ? match[1].toUpperCase() : "FILE"; }
export function isWarningStatus(status) { return ["warning", "skipped", "unmapped", "error"].includes(String(status || "").toLowerCase()); }
export function generateButtonLabel({ targets = [] } = {}) { if (targets.length > 1) return "Generate Selected Outputs"; if (targets[0] === "hpdb") return "Generate HPDB Excel"; if (targets[0] === "dxf") return "Generate DXF"; if (targets[0] === "kmz") return "Standardize KMZ"; return "Generate BOQ Excel"; }
export function canonicalStandardFolder(value) { return normalizeKmlFolderName(value).replace(/NEW HH 40\s*X\s*40\s*X\s*60/, "NEW HH 40X40X60"); }
export function validationReadiness({ targets = null, state = {}, config = {} } = {}) { const selected = targets || selectedOutputTargets(state.outputMode, config); const reasons = []; if (!selected.length) reasons.push("Pilih minimal satu format output"); if (!state.fileCount) reasons.push("Upload OSM/PBF atau KML/KMZ terlebih dahulu"); if (selected.includes("boq") && !config.boqTemplateReady) reasons.push("Template BOQ belum tersedia"); if (selected.includes("hpdb") && !config.hpdbTemplateReady) reasons.push("Template ASTRI/HPDB UG belum tersedia"); return { ready: reasons.length === 0, reasons }; }
