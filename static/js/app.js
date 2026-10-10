const evaUtils = window.EVAUtils || {};

const layerKeys = [
  "ROAD",
  "KML",
  "BOQ_ITEMS",
  "BOQ_WARNINGS",
  "HPDB_ROWS",
  "HPDB_WARNINGS",
  "HPDB_FDT",
  "HPDB_FAT",
  "HPDB_HOMEPASS",
  "HP_COVER",
  "HP_UNCOVER",
  "FDT",
  "FAT",
  "POLE",
  "NAMES",
  "CABLE_LENGTH",
  "SLING_LENGTH",
];

const statMeta = {
  ROAD: { label: "ROAD", unit: "ways" },
  KML: { label: "KML", unit: "item" },
  BOQ_ITEMS: { label: "Item BOQ", unit: "baris" },
  BOQ_WARNINGS: { label: "Peringatan BOQ", unit: "baris" },
  HPDB_ROWS: { label: "Baris HPDB", unit: "baris" },
  HPDB_WARNINGS: { label: "Peringatan HPDB", unit: "baris" },
  HPDB_FDT: { label: "FDT HPDB", unit: "set" },
  HPDB_FAT: { label: "FAT HPDB", unit: "baris" },
  HPDB_HOMEPASS: { label: "Homepass HPDB", unit: "baris" },
  HP_COVER: { label: "HP COVER", unit: "exported" },
  HP_UNCOVER: { label: "HP UNCOVER", unit: "ignored" },
  FDT: { label: "FDT", unit: "set" },
  FAT: { label: "FAT", unit: "unit" },
  POLE: { label: "Pole", unit: "unit" },
  NAMES: { label: "Name Fixed", unit: "KMZ" },
  CABLE_LENGTH: { label: "Cable Length", unit: "m" },
  SLING_LENGTH: { label: "Sling Length", unit: "m" },
};

const requiredKmlFolders = [
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

const optionalKmlFolders = [
  "OLT",
  "FDT",
  "CABLE",
  "JOINT CLOSURE",
  "NEW POLE 7-5",
  "NEW POLE 9-5",
  "EXISTING POLE EMR 7-5",
  "EXISTING POLE EMR 9-5",
];

const standardKmlFolders = [...requiredKmlFolders, ...optionalKmlFolders];
const standardKmlFolderSet = new Set(standardKmlFolders);
const kmlFolderAliases = new Map(standardKmlFolders.map((folderName) => [evaUtils.normalizeKmlFolderName(folderName), folderName]));
[
  ["TALI PANICNG", "TALI PANCING"],
  ["NEW HH 40 X 40 X 60", "NEW HH 40X40X60"],
  ["NEW HH 80 X 80 X 130", "NEW HH 80X80X130"],
  ["T-WAY", "TWEY"],
  ["T-WEY", "TWEY"],
].forEach(([alias, canonical]) => kmlFolderAliases.set(evaUtils.normalizeKmlFolderName(alias), canonical));

const kmlFolderGroups = [
  { title: "FDT", folders: ["FDT"] },
  { title: "FAT", folders: ["BOUNDARY FAT", "FAT"] },
  { title: "HOMEPASS", folders: ["HP COVER", "HP UNCOVER"] },
  {
    title: "EXISTING POLE",
    folders: [
      "EXISTING POLE EMR 7-3",
      "EXISTING POLE EMR 7-2.5",
      "EXISTING POLE EMR 7-4",
      "EXISTING POLE EMR 9-4",
      "EXISTING POLE PARTNER 7-4",
      "EXISTING POLE PARTNER 9-4",
    ],
  },
  { title: "NEW POLE", folders: ["NEW POLE 7-2.5", "NEW POLE 7-3", "NEW POLE 7-4", "NEW POLE 9-4"] },
  { title: "DISTRIBUTION CABLE", folders: ["DISTRIBUTION CABLE"] },
  { title: "SLACK HANGER", folders: ["SLACK HANGER"] },
  { title: "SLING WIRE", folders: ["SLING WIRE"] },
  { title: "UG DUCT", folders: ["SUBDUCT 32/28", "SUBDUCT 40/34", "TALI PANCING"] },
  { title: "UG HANDHOLE", folders: ["NEW HH 40X40X60", "NEW HH 80X80X130"] },
  { title: "UG ACCESSORY", folders: ["TWEY"] },
];

const feederKmlFolderGroups = [
  { title: "OLT", folders: ["OLT"] },
  { title: "JOINT CLOSURE", folders: ["JOINT CLOSURE"] },
  {
    title: "EXISTING POLE",
    folders: [
      "EXISTING POLE EMR 7-2.5",
      "EXISTING POLE EMR 7-3",
      "EXISTING POLE EMR 7-4",
      "EXISTING POLE EMR 7-5",
      "EXISTING POLE EMR 9-4",
      "EXISTING POLE EMR 9-5",
      "EXISTING POLE PARTNER 7-4",
      "EXISTING POLE PARTNER 9-4",
    ],
  },
  { title: "NEW POLE", folders: ["NEW POLE 7-2.5", "NEW POLE 7-3", "NEW POLE 7-4", "NEW POLE 7-5", "NEW POLE 9-4", "NEW POLE 9-5"] },
  { title: "CABLE", folders: ["CABLE"] },
  { title: "SLACK HANGER", folders: ["SLACK HANGER"] },
  { title: "UG DUCT", folders: ["SUBDUCT 32/28", "SUBDUCT 40/34", "TALI PANCING"] },
  { title: "UG HANDHOLE", folders: ["NEW HH 40X40X60", "NEW HH 80X80X130"] },
];

const subfeederKmlFolderGroups = feederKmlFolderGroups.filter((group) => group.title !== "OLT");

function activeKmlFolderGroups() {
  const scopeType = normalizeScopeType(state.scopeType);
  if (scopeType === "Feeder") return feederKmlFolderGroups;
  if (scopeType === "Subfeeder") return subfeederKmlFolderGroups;
  return kmlFolderGroups;
}

const layerStyles = {
  ROAD: { color: "#5F7A70", weight: 3, fillColor: "#5F7A70", fillOpacity: 0.14 },
  KML: { color: "#5DBB9A", weight: 2, fillColor: "#5DBB9A", fillOpacity: 0.18 },
  BOQ_ITEMS: { color: "#4F9FC8" },
  BOQ_WARNINGS: { color: "#F2B94B" },
  HPDB_ROWS: { color: "#35B67A" },
  HPDB_WARNINGS: { color: "#F2B94B" },
  HPDB_FDT: { color: "#4F9FC8" },
  HPDB_FAT: { color: "#E26D6D" },
  HPDB_HOMEPASS: { color: "#7AD8B4" },
  HP_COVER: { color: "#35B67A" },
  HP_UNCOVER: { color: "#F2B94B" },
  FDT: { color: "#4F9FC8" },
  FAT: { color: "#E26D6D" },
  POLE: { color: "#5DBB9A" },
  NAMES: { color: "#7AD8B4" },
  CABLE_LENGTH: { color: "#F2B94B" },
  SLING_LENGTH: { color: "#7A9BC9" },
  OTHER: { color: "#8AA098", weight: 1.5, fillColor: "#8AA098", fillOpacity: 0.12 },
};

const dxfConversionEnabled = Boolean(window.APP_CONFIG?.dxfConversionEnabled);
const defaultOutputMode = window.APP_CONFIG?.defaultOutputMode || (dxfConversionEnabled ? "dxf" : "boq");
// Output starts EMPTY — the user must explicitly pick at least one output.
// We keep `defaultOutputMode` only for download-naming fallbacks.
const initialOutputMode = "";

const state = {
  astriMatch: null,
  astriMatchJobId: null,
  astriMatchRequestId: 0,
  astriBoqMatch: null,
  astriBoqMatchJobId: null,
  astriBoqScope: null,
  astriBoqItems: [],
  astriBoqPermit: null,
  astriBoqRequestId: 0,
  astriBoqSupplierRequestId: 0,
  astriBoqManualRequestId: 0,
  astriSearchItems: [],
  astriTargetType: "cluster",
  astriWorkOrderStatus: null,
  astriWorkOrderStatusRequestId: 0,
  astriDocumentVerification: null,
  astriDocumentVerificationRequestId: 0,
  osmFiles: [],
  kmlFiles: [],
  kmlFolders: [],
  kmlValidationError: null,
  osmValidationState: "empty",
  kmlValidationState: "empty",
  networkType: window.APP_CONFIG.defaultNetworkType || "AE",
  scopeType: window.APP_CONFIG.defaultScopeType || "Cluster",
  outputMode: initialOutputMode,
  jobId: null,
  currentJobStatus: null,
  cancelInFlight: false,
  downloadUrl: null,
  downloadName: defaultOutputMode === "dxf" ? "eva-vector-output.dxf" : "eva-vector-output.xlsx",
  defaultDownloadName: defaultOutputMode === "dxf" ? "eva-vector-output.dxf" : "eva-vector-output.xlsx",
  downloadMode: defaultOutputMode,
  downloadMimeType: defaultOutputMode === "dxf" ? "application/dxf" : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  pollingTimer: null,
  statusPollFailures: 0,
  statusPollInFlight: false,
  statusPollRequestId: 0,
  geoJsonLayer: null,
  mapLegendOpen: false,
  mapLayerVisibility: {},
  mapLocked: true,
  boqPreviewRows: [],
  boqNroPreview: { columns: [], rows: [] },
  boqSummary: {},
  boqWarnings: [],
  boqStatusFilter: "all",
  boqSourceFilter: "all",
  boqPreviewCollapsed: false,
  hpdbSummary: {},
  hpdbPreview: { cluster: [], fat: [], homepass: [], limit: null, totals: {} },
  hpdbWarnings: [],
  hpdbIssues: [],
  standardizerSummary: null,
  hpdbFilter: "all",
  hpdbSearch: "",
  hpdbHomepassPage: 1,
  hpdbHomepassRowsPerPage: 200,
  activePreviewTab: "log",
  activeBoqPreviewTab: "table",
  activeHpdbPreviewTab: "cluster",
  validationSummaryOpen: false,
  folderSearch: "",
  folderFilter: "all",
  expandedFolders: new Set(),
  currentStatusLabel: "Belum ada proses",
  currentStatusTone: "idle",
  currentStatusMessage: "Unggah file KML/KMZ, pilih wilayah ROAD dari Server OSM, lalu klik Mulai Proses.",
  progress: 0,
  stats: {},
  durationLabel: "--",
  hasError: false,
  errorScope: "none",
  hasUserStartedProcess: false,
  uploadInFlight: false,
  consoleLogs: [],
  consoleServerIds: new Set(),
  consoleRenderedKeys: new Set(),
  consoleLocalSeq: 0,
  consoleCollapsed: true,
  consoleLive: false,
  lastClearedServerLogId: 0,
  geofabrikRegion: "",
  geofabrikRegions: [],
  osmCity: "",
  osmKecamatan: "",
  osmKecamatanRegions: [],
  osmProvince: "",
  osmKecamatanProvinces: [],
  osmKecamatanCities: [],
};

function ensureNetworkTypeMarkup() {
  if (document.getElementById("networkTypeSelector")) return;
  const modeCard = document.querySelector(".mode-card");
  const outputModeSelector = document.getElementById("outputModeSelector");
  const outputModeBlock = outputModeSelector?.closest(".mt-4") || outputModeSelector;
  if (!modeCard || !outputModeBlock) return;

  const selectedType = String(state.networkType || "AE").toUpperCase() === "UG" ? "UG" : "AE";
  const wrapper = document.createElement("div");
  wrapper.className = "mt-4";
  wrapper.innerHTML = `
    <span class="mb-2 block text-[0.66rem] font-bold uppercase tracking-[0.04em] text-zinc-500">Tipe Jaringan</span>
    <div id="networkTypeSelector" class="grid gap-2 sm:grid-cols-2">
      <label class="network-type-option">
        <input class="network-type-input sr-only" type="radio" name="network_type" value="AE" ${selectedType === "AE" ? "checked" : ""}>
        <span>
          <strong>AE</strong>
          <small>Aerial</small>
        </span>
      </label>
      <label class="network-type-option">
        <input class="network-type-input sr-only" type="radio" name="network_type" value="UG" ${selectedType === "UG" ? "checked" : ""}>
        <span>
          <strong>UG</strong>
          <small>Underground</small>
        </span>
      </label>
    </div>
    <p id="networkTypeHint" class="mt-2 text-xs font-medium text-zinc-500">Jaringan ${selectedType === "UG" ? "Underground" : "Aerial"} dipilih.</p>
  `;
  modeCard.insertBefore(wrapper, outputModeBlock);
}

ensureNetworkTypeMarkup();

const els = {
  form: document.getElementById("uploadForm"),
  osmFileInput: document.getElementById("osmFileInput"),
  kmlFileInput: document.getElementById("kmlFileInput"),
  standardizerForm: document.getElementById("standardizerForm"),
  standardizerKmzInput: document.getElementById("standardizerKmzInput"),
  standardizerCodingInput: document.getElementById("standardizerCodingInput"),
  standardizerKmzName: document.getElementById("standardizerKmzName"),
  standardizerCodingName: document.getElementById("standardizerCodingName"),
  standardizerSummary: document.getElementById("standardizerSummary"),
  validationResultPanel: document.getElementById("validationResultPanel"),
  validationResultBadge: document.getElementById("validationResultBadge"),
  validationResultToggle: document.getElementById("validationResultToggle"),
  downloadValidationSummary: document.getElementById("downloadValidationSummary"),
  standardizerButton: document.getElementById("standardizerButton"),
  osmDropZone: document.getElementById("osmDropZone"),
  kmlDropZone: document.getElementById("kmlDropZone"),
  osmFileName: document.getElementById("osmFileName"),
  kmlFileName: document.getElementById("kmlFileName"),
  validationSummary: document.getElementById("validationSummary"),
  preflightSummary: document.getElementById("preflightSummary"),
  systemReadiness: document.getElementById("systemReadiness"),
  fileList: document.getElementById("fileList"),
  errorAlert: document.getElementById("errorAlert"),
  workflowStepper: document.getElementById("workflowStepper"),
  convertButton: document.getElementById("convertButton"),
  generateButtonReason: document.getElementById("generateButtonReason"),
  statusBadge: document.getElementById("statusBadge"),
  dashboardStatusPill: document.getElementById("dashboardStatusPill"),
  cancelJobButton: document.getElementById("cancelJobButton"),
  statusText: document.getElementById("statusText"),
  progressValue: document.getElementById("progressValue"),
  progressBar: document.getElementById("progressBar"),
  downloadPanel: document.getElementById("downloadPanel"),
  downloadResultText: document.getElementById("downloadResultText"),
  outputFileLabel: document.getElementById("outputFileLabel"),
  outputFileName: document.getElementById("outputFileName"),
  resetOutputNameButton: document.getElementById("resetOutputNameButton"),
  downloadButton: document.getElementById("downloadButton"),
  dashboardEmptyState: document.getElementById("dashboardEmptyState"),
  resultSummaryPanel: document.getElementById("resultSummaryPanel"),
  resultSummarySubtitle: document.getElementById("resultSummarySubtitle"),
  resultSummaryStatusPill: document.getElementById("resultSummaryStatusPill"),
  resultWorkspaceScopeBadges: document.getElementById("resultWorkspaceScopeBadges"),
  resultNetworkTypeBadge: document.getElementById("resultNetworkTypeBadge"),
  resultScopeTypeBadge: document.getElementById("resultScopeTypeBadge"),
  resultWorkspaceSummary: document.getElementById("resultWorkspaceSummary"),
  statsGrid: document.getElementById("statsGrid"),
  dxfEmptyState: document.getElementById("dxfEmptyState"),
  dxfEmptyCta: document.getElementById("dxfEmptyCta"),
  dashboardSummaryGrid: document.getElementById("dashboardSummaryGrid"),
  outputStatusDetails: document.getElementById("outputStatusDetails"),
  outputStatusPanel: document.getElementById("outputStatusPanel"),
  outputStatusSummaryText: document.getElementById("outputStatusSummaryText"),
  outputStatusSummaryPill: document.getElementById("outputStatusSummaryPill"),
  outputStatusGrid: document.getElementById("outputStatusGrid"),
  previewWorkspacePanel: document.getElementById("previewWorkspacePanel"),
  issueCenter: document.getElementById("issueCenter"),
  outputModeInputs: document.querySelectorAll(".output-mode-input"),
  outputModeHint: document.getElementById("outputModeHint"),
  networkTypeInputs: document.querySelectorAll(".network-type-input"),
  networkTypeHint: document.getElementById("networkTypeHint"),
  kmlFolderPanel: document.getElementById("kmlFolderPanel"),
  kmlFolderSelector: document.getElementById("kmlFolderSelector"),
  kmlFolderText: document.getElementById("kmlFolderText"),
  kmlFolderSelectedCount: document.getElementById("kmlFolderSelectedCount"),
  kmlFolderTotalItems: document.getElementById("kmlFolderTotalItems"),
  kmlFolderWarningCount: document.getElementById("kmlFolderWarningCount"),
  kmlFolderSearch: document.getElementById("kmlFolderSearch"),
  kmlFolderFilterChips: document.getElementById("kmlFolderFilterChips"),
  selectAllKmlFolders: document.getElementById("selectAllKmlFolders"),
  clearKmlFolders: document.getElementById("clearKmlFolders"),
  roadClipBoundaryToggle: document.getElementById("roadClipBoundaryToggle"),
  previewText: document.getElementById("previewText"),
  previewTabs: document.getElementById("previewTabs"),
  boqSubTabs: document.getElementById("boqSubTabs"),
  hpdbSubTabs: document.getElementById("hpdbSubTabs"),
  mapStatsGrid: document.getElementById("mapStatsGrid"),
  mapLockToggle: document.getElementById("mapLockToggle"),
  mapLockOverlay: document.getElementById("mapLockOverlay"),
  mapLegend: document.getElementById("mapLegend"),
  mapLegendCount: document.getElementById("mapLegendCount"),
  mapLegendToggle: document.querySelector("#mapLegend .map-legend-toggle"),
  mapLegendBody: document.querySelector("#mapLegend .map-legend-body"),
  mapLegendList: document.getElementById("mapLegendList"),
  warningSummaryPanel: document.getElementById("warningSummaryPanel"),
  warningList: document.getElementById("warningList"),
  copyWarningSummaryButton: document.getElementById("copyWarningSummaryButton"),
  viewWarningsInBoqButton: document.getElementById("viewWarningsInBoqButton"),
  consolePanel: document.getElementById("consolePanel"),
  consoleLiveIndicator: document.getElementById("consoleLiveIndicator"),
  consoleWarningsBadge: document.getElementById("consoleWarningsBadge"),
  consoleErrorsBadge: document.getElementById("consoleErrorsBadge"),
  consoleDurationBadge: document.getElementById("consoleDurationBadge"),
  consoleToggle: document.getElementById("consoleToggle"),
  clearLogButton: document.getElementById("clearLogButton"),
  copyLogButton: document.getElementById("copyLogButton"),
  downloadLogButton: document.getElementById("downloadLogButton"),
  consoleLogShell: document.getElementById("consoleLogShell"),
  consoleLogBody: document.getElementById("consoleLogBody"),
  convertAnotherButton: document.getElementById("convertAnotherButton"),
  geofabrikPanel: document.getElementById("geofabrikPanel"),
  roadSourcePanel: document.getElementById("roadSourcePanel"),
  roadSourceSummary: document.getElementById("roadSourceSummary"),
  roadSourceHelper: document.getElementById("roadSourceHelper"),
  geofabrikSelect: document.getElementById("geofabrikSelect"),
  geofabrikSize: document.getElementById("geofabrikSize"),
  osmKecamatanPanel: document.getElementById("osmKecamatanPanel"),
  osmProvinceSelect: document.getElementById("osmProvinceSelect"),
  osmCitySelect: document.getElementById("osmCitySelect"),
  osmKecamatanSelect: document.getElementById("osmKecamatanSelect"),
  osmKecamatanSize: document.getElementById("osmKecamatanSize"),
  boqTemplateStatus: document.getElementById("boqTemplateStatus"),
  boqSheetTarget: document.getElementById("boqSheetTarget"),
  boqOutputFile: document.getElementById("boqOutputFile"),
  boqSummaryGrid: document.getElementById("boqSummaryGrid"),
  boqPreviewToggle: document.getElementById("boqPreviewToggle"),
  boqPreviewContent: document.getElementById("boqPreviewContent"),
  boqPreviewText: document.getElementById("boqPreviewText"),
  boqStatusFilter: document.getElementById("boqStatusFilter"),
  boqSourceFilter: document.getElementById("boqSourceFilter"),
  boqFilterActiveBadge: document.getElementById("boqFilterActiveBadge"),
  boqPreviewBody: document.getElementById("boqPreviewBody"),
  boqNroPreviewText: document.getElementById("boqNroPreviewText"),
  boqNroPreviewHead: document.getElementById("boqNroPreviewHead"),
  boqNroPreviewBody: document.getElementById("boqNroPreviewBody"),
  boqNroExportCsvButton: document.getElementById("boqNroExportCsvButton"),
  hpdbTemplateStatus: document.getElementById("hpdbTemplateStatus"),
  hpdbTemplateFile: document.getElementById("hpdbTemplateFile"),
  hpdbSheetsTarget: document.getElementById("hpdbSheetsTarget"),
  hpdbMaxFdt: document.getElementById("hpdbMaxFdt"),
  hpdbOutputFile: document.getElementById("hpdbOutputFile"),
  hpdbSummaryGrid: document.getElementById("hpdbSummaryGrid"),
  hpdbClusterPreviewText: document.getElementById("hpdbClusterPreviewText"),
  hpdbClusterPreviewBody: document.getElementById("hpdbClusterPreviewBody"),
  hpdbFatPreviewText: document.getElementById("hpdbFatPreviewText"),
  hpdbFatPreviewBody: document.getElementById("hpdbFatPreviewBody"),
  hpdbHomepassPreviewText: document.getElementById("hpdbHomepassPreviewText"),
  hpdbHomepassPreviewBody: document.getElementById("hpdbHomepassPreviewBody"),
  hpdbHomepassPrevPage: document.getElementById("hpdbHomepassPrevPage"),
  hpdbHomepassNextPage: document.getElementById("hpdbHomepassNextPage"),
  hpdbHomepassPageText: document.getElementById("hpdbHomepassPageText"),
  resetSessionButton: document.getElementById("resetSessionButton"),
};

const severityMeta = {
  INFO: { icon: "i" },
  SUCCESS: { icon: "ok" },
  WARNING: { icon: "!" },
  ERROR: { icon: "x" },
};

const map = L.map("map", {
  zoomControl: true,
  scrollWheelZoom: false,
  dragging: false,
  touchZoom: false,
  doubleClickZoom: false,
  boxZoom: false,
  keyboard: false,
}).setView([-2.5, 118], 5);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

function setMapLocked(isLocked) {
  state.mapLocked = Boolean(isLocked);
  const method = state.mapLocked ? "disable" : "enable";
  [map.scrollWheelZoom, map.dragging, map.touchZoom, map.doubleClickZoom, map.boxZoom, map.keyboard].forEach((handler) => {
    if (handler?.[method]) handler[method]();
  });

  els.mapLockOverlay?.classList.toggle("hidden", !state.mapLocked);
  els.mapLockOverlay?.setAttribute("aria-hidden", String(!state.mapLocked));
  if (els.mapLockToggle) {
    els.mapLockToggle.textContent = state.mapLocked ? "Aktifkan interaksi peta" : "Kunci interaksi peta";
    els.mapLockToggle.dataset.locked = String(state.mapLocked);
    els.mapLockToggle.setAttribute("aria-pressed", String(!state.mapLocked));
    els.mapLockToggle.setAttribute("title", state.mapLocked ? "Aktifkan interaksi peta" : "Kunci interaksi peta");
  }
}

function toggleMapLock() {
  setMapLocked(!state.mapLocked);
}

function initStats() {
  if (!els.statsGrid) return;
  els.statsGrid.innerHTML = layerKeys.map((key) => `
    <article class="stat-card">
      <div class="flex items-center justify-between gap-2">
        <span class="text-[11px] font-semibold text-zinc-400">${statMeta[key]?.label || key}</span>
        <span class="h-2.5 w-2.5 rounded-full" style="background:${layerStyles[key].color}"></span>
      </div>
      <strong id="stat-${key}" class="mt-2 block text-2xl font-bold text-white">0</strong>
      <span class="mt-1 block text-[10px] font-semibold uppercase tracking-normal text-zinc-600">${statMeta[key]?.unit || ""}</span>
    </article>
  `).join("");
  renderDxfEmptyState();
}

function statsAllZero() {
  return layerKeys.every((key) => statValue(key) === 0);
}

function renderDxfEmptyState() {
  if (!els.statsGrid || !els.dxfEmptyState) return;
  const shouldShowEmpty = !state.downloadUrl && statsAllZero();
  els.statsGrid.classList.toggle("hidden", shouldShowEmpty);
  els.dxfEmptyState.classList.toggle("hidden", !shouldShowEmpty);
}

function formatNumber(value) {
  return evaUtils.formatNumber ? evaUtils.formatNumber(value) : Number(value || 0).toLocaleString("id-ID");
}

function statValue(key) {
  return Number(state.stats?.[key] || 0);
}

function hasResultData() {
  return Boolean(state.downloadUrl)
    || Boolean(state.geoJsonLayer)
    || state.boqPreviewRows.length > 0
    || Boolean(state.boqNroPreview?.rows?.length)
    || Boolean(state.hpdbPreview?.cluster?.length)
    || Boolean(state.hpdbPreview?.fat?.length)
    || Boolean(state.hpdbPreview?.homepass?.length)
    || state.currentStatusTone === "processing"
    || state.currentStatusTone === "error";
}

function outputTargetsFromMode(mode = state.outputMode) {
  // Empty selection means NO outputs chosen yet — never fall back to a
  // default. Only resolve via utils (which applies fallbacks) when a mode
  // is actually set.
  if (mode === "" || mode == null) {
    return new Set();
  }
  if (evaUtils.outputTargetsFromMode) {
    return evaUtils.outputTargetsFromMode(mode, { defaultOutputMode, dxfConversionEnabled });
  }
  return new Set(String(mode).split("_").filter(Boolean));
}

function modeHas(target, mode = state.outputMode) {
  if (mode === "" || mode == null) return false;
  return evaUtils.modeHas
    ? evaUtils.modeHas(target, mode, { defaultOutputMode, dxfConversionEnabled })
    : outputTargetsFromMode(mode).has(target);
}

function modeTargetCount(mode = state.outputMode) {
  return outputTargetsFromMode(mode).size;
}

function modeFromTargets(targets) {
  return evaUtils.modeFromTargets
    ? evaUtils.modeFromTargets(targets, { defaultOutputMode, dxfConversionEnabled })
    : (targets.length ? targets.join("_") : defaultOutputMode);
}

const outputTargetLabels = {
  dxf: "DXF (CAD)",
  boq: "BOQ (Excel)",
  astri: "ASTRI (Excel)",
  hpdb: "HPDB (Excel)",
  kmz: "KMZ Standar",
};

const networkTypeLabels = {
  AE: "AE / Aerial",
  UG: "UG / Underground",
};

function normalizeNetworkType(type = state.networkType) {
  return evaUtils.normalizeNetworkType ? evaUtils.normalizeNetworkType(type) : (() => {
    const normalized = String(type || "AE").trim().toUpperCase();
    return networkTypeLabels[normalized] ? normalized : "AE";
  })();
}

function networkTypeLabel(type = state.networkType) {
  return evaUtils.networkTypeLabel ? evaUtils.networkTypeLabel(type) : networkTypeLabels[normalizeNetworkType(type)] || networkTypeLabels.AE;
}

function normalizeScopeType(type = state.scopeType) {
  const raw = String(type || "Cluster").trim().toLowerCase();
  if (raw === "feeder") return "Feeder";
  if (raw === "subfeeder" || raw === "sub feeder" || raw === "sub-feeder") return "Subfeeder";
  return "Cluster";
}

function isFeederLikeScope(type = state.scopeType) {
  const normalized = normalizeScopeType(type);
  return normalized === "Feeder" || normalized === "Subfeeder";
}

function slugText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function provinceRoadRegionFilename(province) {
  const slug = slugText(province);
  return slug ? `${slug}-roads.osm.pbf` : "";
}

function humanJoinLabels(labels) {
  if (labels.length <= 1) return labels[0] || "BOQ (Excel)";
  if (labels.length === 2) return labels.join(" + ");
  return `${labels.slice(0, -1).join(" + ")} + ${labels[labels.length - 1]}`;
}

function selectedOutputTargets() {
  if (state.outputMode === "" || state.outputMode == null) return [];
  return evaUtils.selectedOutputTargets
    ? evaUtils.selectedOutputTargets(state.outputMode, { defaultOutputMode, dxfConversionEnabled })
    : ["dxf", "boq", "astri", "hpdb", "kmz"]
      .filter((target) => target !== "dxf" || dxfConversionEnabled)
      .filter((target) => modeHas(target));
}

function selectedOutputLabels() {
  return selectedOutputTargets().map((target) => outputTargetLabels[target] || target.toUpperCase());
}

function statusBadge(label, tone = "idle") {
  return `<span class="mini-status-badge" data-tone="${escapeHtml(tone)}">${escapeHtml(label)}</span>`;
}

function fileFormatLabel(file) {
  return evaUtils.fileFormatLabel ? evaUtils.fileFormatLabel(file) : (() => {
    const name = String(file?.name || "").toLowerCase();
    if (name.endsWith(".osm.pbf")) return "OSM.PBF";
    const ext = String(file?.name || "").split(".").pop();
    return ext ? ext.toUpperCase() : "File";
  })();
}

function uploadStatusMeta(kind) {
  if (kind === "osm") {
    if (!state.osmFiles.length) return { label: "Belum ada file", tone: "idle" };
    if (state.osmValidationState === "invalid") return { label: "Tidak valid", tone: "error" };
    return { label: "Siap", tone: "ready" };
  }
  if (!state.kmlFiles.length) return { label: "Belum ada file", tone: "idle" };
  if (state.kmlValidationState === "validating") return { label: "Memvalidasi", tone: "warning" };
  if (state.kmlValidationState === "invalid" || state.kmlValidationError) return { label: "Tidak valid", tone: "error" };
  if (state.kmlValidationState === "valid") return { label: "Valid", tone: "ready" };
  return { label: "Menunggu validasi", tone: "warning" };
}

function renderUploadFileSummary(kind) {
  const isOsm = kind === "osm";
  const files = isOsm ? state.osmFiles : state.kmlFiles;
  const node = isOsm ? els.osmFileName : els.kmlFileName;
  const zone = isOsm ? els.osmDropZone : els.kmlDropZone;
  if (!node || !zone) return;
  zone.dataset.state = files.length ? "selected" : "empty";
  if (!files.length) {
    node.classList.add("hidden");
    node.innerHTML = "";
    return;
  }
  const status = uploadStatusMeta(kind);
  const title = isOsm ? "Data ROAD dipilih" : "KML/KMZ dipilih";
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);

  node.innerHTML = `
    <span class="upload-summary-head">
      <span>
        <strong>${escapeHtml(title)}</strong>
        <small>Total ${formatFileSize(totalBytes)}</small>
      </span>
      <span class="upload-summary-meta">
        <span>${files.length} file</span>
        ${statusBadge(status.label, status.tone)}
      </span>
    </span>
    <span class="upload-summary-list">
      ${files.map((file) => `
        <span class="upload-summary-file">
          <span>${escapeHtml(file.name)}</span>
          <small>${formatFileSize(file.size)}</small>
        </span>
      `).join("")}
    </span>
    <span class="upload-summary-actions">
      <button type="button" class="upload-summary-action danger" data-file-kind="${kind}" data-file-action="clear">Hapus</button>
    </span>
  `;
  node.classList.remove("hidden");
}

function generateButtonLabel() {
  const targets = selectedOutputTargets();
  if (evaUtils.generateButtonLabel) {
    return evaUtils.generateButtonLabel({ targets, defaultOutputMode, networkType: state.networkType });
  }
  return targets.length > 1 ? "Mulai Proses" : "Mulai Proses";
}

function roadClipBoundaryEnabled() {
  if (!els.roadClipBoundaryToggle) return true;
  return els.roadClipBoundaryToggle.checked !== false;
}

function validationReadiness() {
  const targets = selectedOutputTargets();
  const reasons = [];
  const notes = [];
  const hasFiles = selectedFiles().length > 0;
  const needsKml = targets.some((target) => ["boq", "astri", "hpdb", "kmz"].includes(target));
  const isProcessing = state.currentStatusTone === "processing" && !state.downloadUrl;

  if (!targets.length) reasons.push("Pilih minimal satu format output");
  if (!hasFiles || (needsKml && !state.kmlFiles.length)) reasons.push("Unggah KML/KMZ terlebih dahulu");
  if (state.outputMode === "kmz" && state.kmlFiles.length !== 1) {
    reasons.push("KMZ Standar tunggal hanya membutuhkan satu file KML/KMZ");
  }
  if (modeHas("boq") && !currentBoqTemplateReady()) {
    reasons.push(`Template BOQ ${state.networkType} belum tersedia`);
  }
  if ((modeHas("astri") || modeHas("hpdb")) && !currentHpdbTemplateReady()) {
    reasons.push(`Template ASTRI/HPDB ${state.networkType} belum tersedia`);
  }
  if (state.kmlValidationError) reasons.push(state.kmlValidationError);
  if (isProcessing) reasons.push("Proses sedang berjalan");

  if (modeHas("dxf")) {
    notes.push("Data ROAD otomatis diambil dari server OSM berdasarkan wilayah yang dipilih.");
  }
  if (state.kmlFiles.length && state.kmlValidationState === "validating") {
    notes.push("Validasi folder KML/KMZ sedang berjalan.");
  }

  return {
    ready: reasons.length === 0,
    reasons,
    notes,
    targets,
    outputLabels: selectedOutputLabels(),
    hasFiles,
    estimatedOutput: targets.length > 1 ? "ZIP" : "file tunggal",
  };
}

function renderValidationSummary() { try { if (typeof state === "undefined") return; } catch(_) { return; }
  void state;

  renderPreflightSummary();
  const summary = validationReadiness();
  const tone = summary.ready ? "ready" : summary.reasons.some((reason) => /gagal|error|invalid|tidak valid/i.test(reason)) ? "error" : "warning";
  if (els.statusText && state.currentStatusTone === "idle" && !state.hasError && !state.jobId && !state.downloadUrl) {
    const nextStep = summary.ready
      ? "File dan output siap. Klik Mulai Proses untuk membuat hasil."
      : state.kmlFiles.length
        ? "File sudah dipilih. Pilih format output, lalu klik Mulai Proses."
        : "Unggah file KML/KMZ, pilih format output, lalu klik Mulai Proses.";
    els.statusText.textContent = nextStep;
  }
  if (els.convertButton) {
    setButtonTrailingText(els.convertButton, summary.ready ? generateButtonLabel() : "Lengkapi konfigurasi");
    els.convertButton.disabled = !summary.ready;
    els.convertButton.classList.toggle("is-disabled", !summary.ready);
    els.convertButton.setAttribute("aria-disabled", String(!summary.ready));
    if (els.generateButtonReason) {
      const reason = summary.ready
        ? "Konfigurasi lengkap. Klik untuk menjalankan proses."
        : summary.reasons[0] || "Lengkapi konfigurasi untuk mengaktifkan proses.";
      els.generateButtonReason.textContent = reason;
      els.generateButtonReason.dataset.tone = summary.ready ? "ready" : "warning";
    }
  }
  if (!els.validationSummary) return;
  const rows = [
    ["Data ROAD", state.osmKecamatan ? "Server OSM dipilih" : "Server OSM default"],
    ["KML/KMZ", state.kmlFiles.length ? "Sudah dipilih" : "Belum dipilih"],
    ["Tipe jaringan", networkTypeLabel(state.networkType)],
    ["Output", summary.outputLabels.length ? summary.outputLabels.join(", ") : "Belum dipilih"],
    ["Estimasi hasil", summary.estimatedOutput],

  ];
  const issueCount = summary.reasons.length + summary.notes.length;
  const compactItems = [
    state.osmKecamatan ? "Server OSM dipilih" : "Server OSM default",

    state.kmlFiles.length ? "KML/KMZ dipilih" : "KML/KMZ belum ada",
    summary.outputLabels.length ? summary.outputLabels.join(", ") : "Output belum dipilih",
    issueCount ? `${formatNumber(issueCount)} hal perlu dicek` : "Semua siap",
  ];
  els.validationSummary.innerHTML = `
    <details class="validation-summary-details" ${state.validationSummaryOpen ? "open" : ""}>
      <summary class="validation-summary-head">
        <span>
          <strong>Ringkasan Validasi</strong>
          <small>${summary.ready ? "Siap diproses" : "Belum siap"}</small>
        </span>
        <span class="validation-summary-actions">
          ${statusBadge(summary.ready ? "Siap" : "Belum siap", tone)}
          <span class="validation-summary-chevron" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="m8 10 4 4 4-4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
        </span>
      </summary>
      <p class="validation-summary-preview">${compactItems.map(escapeHtml).join(" | ")}</p>
      <div class="validation-summary-content">
        <div class="validation-summary-grid">
          ${rows.map(([label, value]) => `
            <div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>
          `).join("")}
        </div>
        ${summary.reasons.length ? `
          <div class="validation-summary-alert" data-tone="${tone}">
            ${summary.reasons.map((reason) => `<p>${escapeHtml(reason)}</p>`).join("")}
          </div>
        ` : ""}
        ${summary.notes.length ? `
          <div class="validation-summary-note">
            ${summary.notes.map((note) => `<p>${escapeHtml(note)}</p>`).join("")}
          </div>
        ` : ""}
      </div>
    </details>
  `;
  const details = els.validationSummary.querySelector(".validation-summary-details");
  details?.addEventListener("toggle", () => {
    state.validationSummaryOpen = details.open;
  });
}

function syncOutputTargetInputs() {
  const targets = outputTargetsFromMode(state.outputMode);
  const isFeeder = isFeederLikeScope(state.scopeType);
  els.outputModeInputs.forEach((input) => {
    const isDxf = input.value === "dxf";
    const isFeederLocked = isFeeder && (input.value === "astri" || input.value === "hpdb");
    const isDisabled = (isDxf && !dxfConversionEnabled) || isFeederLocked;
    input.disabled = isDisabled;
    if (isDisabled && input.checked) input.checked = false;
    if (!isDisabled) input.checked = targets.has(input.value);
    const option = input.closest(".output-mode-option");
    option?.classList.toggle("is-disabled", isDisabled);
    option?.setAttribute("aria-disabled", String(isDisabled));
    if (option) option.dataset.outputDisabled = String(isDisabled);
  });
  if (els.outputModeHint) {
    const selected = ["dxf", "boq", "astri", "hpdb", "kmz"]
      .filter((target) => target !== "dxf" || dxfConversionEnabled)
      .filter((target) => !(isFeeder && (target === "astri" || target === "hpdb")))
      .filter((target) => targets.has(target))
      .map((target) => outputTargetLabels[target] || target.toUpperCase());
    if (!selected.length) {
      els.outputModeHint.textContent = "Pilih format output yang ingin dibuat.";
    } else {
      els.outputModeHint.textContent = selected.length > 1
        ? `${state.networkType} ${humanJoinLabels(selected)} akan dibuat sebagai ZIP.`
        : `${state.networkType} aktif: ${selected[0]}.`;
    }
    if (!dxfConversionEnabled) {
      els.outputModeHint.textContent += " DXF sementara dikunci di server live.";
    }
    if (isFeeder) {
      els.outputModeHint.textContent += " ASTRI/HPDB tidak tersedia untuk Feeder/Subfeeder.";
    }
  }
}

function syncNetworkTypeInputs() {
  state.networkType = normalizeNetworkType(state.networkType);
  els.networkTypeInputs.forEach((input) => {
    input.checked = normalizeNetworkType(input.value) === state.networkType;
  });
  if (els.networkTypeHint) {
    els.networkTypeHint.textContent = `Jaringan ${state.networkType === "UG" ? "Underground" : "Aerial"} dipilih.`;
  }
}

function syncScopeTypeInputs() {
  state.scopeType = normalizeScopeType(state.scopeType);
  const scopeInputs = document.querySelectorAll(".scope-type-input");
  scopeInputs.forEach((input) => {
    input.checked = input.value === state.scopeType;
  });
  const hint = document.getElementById("scopeTypeHint");
  if (hint) {
    hint.textContent = `Mode ${state.scopeType} aktif.`;
  }
  if (typeof window.evaRenderRoadSourcePanel === "function") {
    window.evaRenderRoadSourcePanel();
  }
}

function isWarningStatus(status) {
  if (evaUtils.isWarningStatus) return evaUtils.isWarningStatus(status);
  const normalized = String(status || "").toLowerCase();
  return normalized === "warning" || normalized === "unmapped" || normalized === "skipped" || normalized === "error";
}

function warningRows() {
  return state.boqPreviewRows.filter((row) => isWarningStatus(row.status));
}

function hpdbIssueRows() {
  return (state.hpdbIssues || []).map((issue) => ({
    source_folder: issue.source_folder || "HPDB",
    parent_line_fdt: issue.parent_line_fdt || (issue.fdt ? `FDT ${issue.fdt}` : "HPDB"),
    item: issue.item || issue.fat_code || issue.fdt_code || "Isu HPDB",
    status: issue.status || "Peringatan",
    message: issue.message || "",
    target_cell: issue.fat_code || issue.fdt_code || "",
  }));
}

function allIssueRows() {
  return [...warningRows(), ...hpdbIssueRows()];
}

function warningReason(row) {
  return row.message || (String(row.status || "").toLowerCase() === "mapped" ? "" : "Unmapped / belum cocok dengan aturan BOQ");
}

function countConsoleSeverity(severity) {
  return state.consoleLogs.filter((log) => normalizeSeverity(log.severity) === severity).length;
}

function refreshDurationFromLogs() {
  const durationLog = [...state.consoleLogs].reverse().find((log) => /Export duration:/i.test(log.message || ""));
  if (!durationLog) return;
  const match = String(durationLog.message || "").match(/Export duration:\s*([0-9:]+)/i);
  if (match) state.durationLabel = match[1];
}

function statusTextLabel(status) {
  const labels = {
    pending: "Menunggu",
    ready: "Siap",
    processing: "Memproses",
    retrying: "Mencoba ulang",
    warning: "Perlu perhatian",
    cancelled: "Dibatalkan",
    complete: "Selesai",
    error: "Gagal",
  };
  return labels[status] || "Menunggu";
}

function workflowStepIcon(status, fallback) {
  // Returns inline SVG markup for state icons. Falls back to step number text.
  if (status === "complete") {
    return '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m5 12 5 5L20 7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }
  if (status === "warning") {
    return '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 9v4m0 3.5h.01M10.6 4.6 3.5 17.5A1.5 1.5 0 0 0 4.8 19.8h14.4a1.5 1.5 0 0 0 1.3-2.3L13.4 4.6a1.5 1.5 0 0 0-2.8 0Z" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }
  if (status === "error") {
    return '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>';
  }
  if (status === "processing") {
    return '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 4a8 8 0 1 1-8 8" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>';
  }
  return String(fallback);
}

function renderWorkflowStepper() {
  if (!els.workflowStepper) return;
  const hasFiles = selectedFiles().length > 0;
  const hasFolderError = Boolean(state.kmlValidationError);
  const isFolderValidating = state.kmlValidationState === "validating";
  const hasPreview = Boolean(state.geoJsonLayer)
    || state.boqPreviewRows.length > 0
    || Boolean(state.boqNroPreview?.rows?.length)
    || Boolean(state.hpdbPreview?.cluster?.length)
    || Boolean(state.hpdbPreview?.fat?.length)
    || Boolean(state.hpdbPreview?.homepass?.length);
  const hasConfiguredOutput = selectedOutputTargets().length > 0;
  const isReadyToGenerate = validationReadiness().ready;
  const isDone = state.currentStatusTone === "done" || Boolean(state.downloadUrl);
  const isProcessing = state.currentStatusTone === "processing" && !isDone;
  const hasWarnings = allIssueRows().some((row) => isWarningStatus(row.status));
  const hasJobError = (state.currentStatusTone === "error" || state.hasError)
    && (state.hasUserStartedProcess || Boolean(state.jobId));
  const hasUploadError = state.hasError && !hasJobError && !hasFiles;

  const steps = [
    {
      label: "Unggah Data",
      status: hasUploadError ? "error" : hasFiles ? "complete" : "ready",
      caption: hasUploadError ? "Periksa file" : hasFiles ? "Siap" : "Belum ada file",
    },
    {
      label: "Atur Output",
      status: hasConfiguredOutput && hasFiles ? "complete" : hasFiles ? "ready" : "pending",
      caption: hasConfiguredOutput ? "Siap" : "Menunggu",
    },
    {
      label: "Mulai Proses",
      status: hasJobError ? "error" : isDone ? "complete" : isProcessing ? "processing" : isReadyToGenerate ? "ready" : "pending",
      caption: hasJobError ? "Gagal" : isDone ? "Selesai" : isProcessing ? "Memproses" : isReadyToGenerate ? "Siap" : "Menunggu",
    },
    {
      label: "Unduh",
      status: state.downloadUrl ? (hasWarnings ? "warning" : "complete") : isDone ? "ready" : "pending",
      caption: state.downloadUrl ? "Siap" : "Menunggu",
    },
  ];

  const currentStep = state.downloadUrl || isDone
    ? 4
    : (isProcessing || isReadyToGenerate || hasJobError)
      ? 3
      : hasFiles
        ? 2
        : 1;
  els.workflowStepper.dataset.currentStep = String(currentStep);
  els.workflowStepper.innerHTML = steps.map((step, index) => {
    const stepNumber = index + 1;
    const currentAttribute = stepNumber === currentStep ? ' aria-current="step"' : "";
    return `
      <li class="workflow-step" data-status="${step.status}"${currentAttribute}>
        <span class="workflow-step-index" aria-hidden="true">${workflowStepIcon(step.status, stepNumber)}</span>
        <span class="min-w-0">
          <span class="workflow-step-label">${escapeHtml(step.label)}</span>
          <span class="workflow-step-status">${escapeHtml(step.caption || statusTextLabel(step.status))}</span>
        </span>
        <span class="workflow-mobile-count" aria-hidden="true">Langkah ${stepNumber}/4</span>
      </li>
    `;
  }).join("");
}

function renderDashboardSummary() {
  const hasFiles = selectedFiles().length > 0;
  const hasOutput = hasResultData();
  if (els.dashboardEmptyState) {
    els.dashboardEmptyState.classList.toggle("hidden", hasFiles || hasOutput);
  }
  if (els.outputStatusPanel) {
    els.outputStatusPanel.classList.add("hidden");
  }
  if (els.previewWorkspacePanel) {
    els.previewWorkspacePanel.classList.add("hidden");
  }
  if (els.resultSummaryPanel) {
    els.resultSummaryPanel.classList.add("hidden");
  }
  if (!els.dashboardSummaryGrid) return;
  const summary = state.boqSummary || {};
  const hpdb = state.hpdbSummary || {};
  const warnings = Math.max(
    warningRows().length,
    Number(summary.total_warning_unmapped || 0),
    statValue("BOQ_WARNINGS"),
  );
  const hpdbWarnings = Math.max(
    hpdbIssueRows().filter((row) => isWarningStatus(row.status)).length,
    Number(hpdb.total_hpdb_warnings || 0),
    statValue("HPDB_WARNINGS"),
  );
  const items = [
    ["Jalur ROAD", formatNumber(statValue("ROAD")), "road"],
    ["Item KML", formatNumber(statValue("KML")), "kml"],
    ["BOQ terbaca", formatNumber(summary.total_mapped_boq_item || statValue("BOQ_ITEMS")), "boq"],
    ["Peringatan BOQ", formatNumber(warnings), warnings ? "warning" : "ok"],
    ["Baris HPDB", formatNumber(hpdb.hpdb_rows || statValue("HPDB_ROWS")), "hpdb"],
    ["Peringatan HPDB", formatNumber(hpdbWarnings), hpdbWarnings ? "warning" : "ok"],
    ["FDT terdeteksi", formatNumber(hpdb.total_fdt_detected || statValue("HPDB_FDT") || summary.total_fdt_detected || statValue("FDT")), "fdt"],
    ["FAT terdeteksi", formatNumber(hpdb.total_fat_rows || statValue("HPDB_FAT") || summary.total_fat_detected || statValue("FAT")), "fat"],
    ["HP COVER diekspor", formatNumber(hpdb.total_hp_cover_exported || statValue("HP_COVER")), "ok"],
    ["HP UNCOVER diabaikan", formatNumber(hpdb.total_hp_uncover_ignored || statValue("HP_UNCOVER")), "warning"],
    ["Error", formatNumber(countConsoleSeverity("ERROR") + (state.currentStatusTone === "error" ? 1 : 0)), "error"],
    ["Durasi", state.durationLabel || "--", "time"],
  ];
  const totalWarning = warnings + hpdbWarnings + countConsoleSeverity("WARNING");
  const totalError = countConsoleSeverity("ERROR") + (state.currentStatusTone === "error" ? 1 : 0);

  if (els.resultSummarySubtitle) {
    const fileCount = state.downloadUrl ? "1 file berhasil dibuat" : hasOutput ? "Proses sedang berjalan" : "Menunggu generate";
    els.resultSummarySubtitle.textContent = `${fileCount} | ${formatNumber(totalWarning)} peringatan | ${formatNumber(totalError)} error | durasi ${state.durationLabel || "--"}`;
  }
  if (els.resultSummaryStatusPill) {
    const isDone = state.currentStatusTone === "done" || Boolean(state.downloadUrl);
    els.resultSummaryStatusPill.textContent = totalError ? "Gagal" : isDone ? "Selesai" : state.currentStatusTone === "processing" ? "Memproses" : "Menunggu";
    els.resultSummaryStatusPill.dataset.tone = totalError ? "error" : isDone ? "ready" : state.currentStatusTone === "processing" ? "waiting" : "idle";
  }

  els.dashboardSummaryGrid.innerHTML = items.map(([label, value, tone]) => `
    <article class="dashboard-metric" data-tone="${tone}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </article>
  `).join("");
}

function renderResultScopeBadges() {
  if (!els.resultWorkspaceScopeBadges) return;
  const hasOutput = Boolean(state.downloadUrl);
  els.resultWorkspaceScopeBadges.classList.toggle("hidden", !hasOutput);
  if (!hasOutput) return;
  if (els.resultNetworkTypeBadge) {
    els.resultNetworkTypeBadge.textContent = normalizeNetworkType(state.networkType);
  }
  if (els.resultScopeTypeBadge) {
    els.resultScopeTypeBadge.textContent = normalizeScopeType(state.scopeType);
  }
}

function renderResultWorkspaceSummary() {
  if (!els.resultWorkspaceSummary) return;
  renderResultScopeBadges();
  const files = selectedFiles();
  const outputLabels = selectedOutputLabels();
  const summary = state.boqSummary || {};
  const hpdb = state.hpdbSummary || {};
  const standardizer = state.standardizerSummary || {};
  const warnings = Math.max(warningRows().length, Number(summary.total_warning_unmapped || 0), statValue("BOQ_WARNINGS"))
    + Math.max(hpdbIssueRows().length, Number(hpdb.total_hpdb_warnings || 0), statValue("HPDB_WARNINGS"))
    + Number((standardizer.warnings || []).length || 0)
    + countConsoleSeverity("WARNING");
  const errors = countConsoleSeverity("ERROR") + (state.currentStatusTone === "error" ? 1 : 0);
  const hasOutput = Boolean(state.downloadUrl);

  if (!hasResultData() && !files.length) {
    els.resultWorkspaceSummary.innerHTML = `
      <div class="result-empty-state">
        <strong>Belum ada hasil</strong>
        <p>Unggah file, pilih format output, lalu klik Mulai Proses untuk melihat hasil di sini.</p>
        <ul>
          <li>Unggah file</li>
          <li>Validasi folder</li>
          <li>Pilih output</li>
          <li>Mulai Proses</li>
        </ul>
      </div>
    `;
    return;
  }

  const rows = [
    ["File input", files.length ? `${formatNumber(files.length)} file | ${files.map((file) => file.name).slice(0, 2).join(", ")}${files.length > 2 ? "..." : ""}` : "Belum dipilih"],
    ["Format output", outputLabels.length ? outputLabels.join(", ") : "Belum dipilih"],
    ["Tipe jaringan", networkTypeLabel(state.networkType)],
    ["Hasil output", hasOutput ? (state.downloadName || outputModeLabel(state.downloadMode)) : "Belum tersedia"],
    ["Jumlah peringatan", formatNumber(warnings)],
    ["Jumlah error", formatNumber(errors)],
    ["Durasi", state.durationLabel || "--"],
  ];

  els.resultWorkspaceSummary.innerHTML = `
    <div class="result-workspace-hero" data-tone="${errors ? "error" : hasOutput ? "ready" : state.currentStatusTone === "processing" ? "processing" : "idle"}">
      <span>${escapeHtml(hasOutput ? `${outputModeLabel(state.downloadMode)} selesai` : state.currentStatusLabel || "Siap")}</span>
      <strong>${escapeHtml(hasOutput ? "Output siap diunduh" : state.currentStatusMessage || "Belum ada hasil")}</strong>
      <small>${hasOutput ? `1 file berhasil dibuat | ${formatNumber(warnings)} peringatan | ${formatNumber(errors)} error | durasi ${escapeHtml(state.durationLabel || "--")}` : "Hasil dan preview akan muncul setelah proses generate."}</small>
    </div>
    <div class="result-detail-grid">
      ${rows.map(([label, value]) => `
        <article>
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </article>
      `).join("")}
    </div>
  `;
}

function outputStateLabel(isRequested, isReady) {
  if (!isRequested) return "Tidak diminta";
  return isReady ? "Siap" : "Belum siap";
}

function outputIconSvg(target) {
  const map = {
    dxf: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 7.5 12 3l8 4.5v9L12 21l-8-4.5v-9Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m8.5 9.5 7 5M15.5 9.5l-7 5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
    boq: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="M9 4v16M15 4v16M4 9h16M4 15h16" stroke="currentColor" stroke-width="1.4"/></svg>',
    astri: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 4h11l3 3v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M9 12h6M9 16h6M9 8h3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
    hpdb: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><ellipse cx="12" cy="6" rx="7" ry="2.5" stroke="currentColor" stroke-width="1.7"/><path d="M5 6v6c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6M5 12v6c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-6" stroke="currentColor" stroke-width="1.7"/></svg>',
    kmz: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6.5 4.5h8L18 8v11.5H6.5v-15Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M14.5 4.5V8H18M9 12h5M9 15h6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="m8.5 18 1.5 1.5L13 16" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    zip: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="5" y="3" width="14" height="18" rx="2" stroke="currentColor" stroke-width="1.7"/><path d="M11 3v2h2v2h-2v2h2v2h-2v2h2" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
    log: '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 4h14v16H5z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M8 8h8M8 12h8M8 16h5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  };
  return map[target] || map.log;
}

function renderOutputStatus() {
  if (!els.outputStatusGrid) return;
  const mode = state.outputMode;
  const hasDownload = Boolean(state.downloadUrl);
  const isMulti = modeTargetCount(mode) > 1;
  const isProcessing = state.currentStatusTone === "processing";
  const isFailed = state.currentStatusTone === "error" || state.hasError;
  const rows = [
    { key: "kmz", label: "KMZ Standar", desc: "KMZ hasil standardisasi", requested: modeHas("kmz", mode), ready: hasDownload && modeHas("kmz", mode) },
    { key: "dxf", label: "DXF (CAD)", desc: "Gambar AutoCAD", requested: modeHas("dxf", mode), ready: hasDownload && modeHas("dxf", mode) },
    { key: "boq", label: "BOQ (Excel)", desc: "Bill of Quantity", requested: modeHas("boq", mode), ready: hasDownload && modeHas("boq", mode) },
    { key: "astri", label: "ASTRI (Excel)", desc: "Workbook ASTRI", requested: modeHas("astri", mode), ready: hasDownload && modeHas("astri", mode) },
    { key: "hpdb", label: "HPDB (Excel)", desc: "Database homepass", requested: modeHas("hpdb", mode), ready: hasDownload && modeHas("hpdb", mode) },
    { key: "zip", label: "ZIP", desc: "Bundel multi-output", requested: isMulti, ready: hasDownload && isMulti },
  ];
  const requestedRows = rows.filter((row) => row.requested);
  const notRequestedRows = rows.filter((row) => !row.requested && row.key !== "log" && row.key !== "zip");
  const visibleRows = requestedRows.filter((row) => row.ready || isProcessing || isFailed);
  const readyRows = requestedRows.filter((row) => row.ready);
  const pendingRows = requestedRows.filter((row) => !row.ready && !isFailed);
  const summaryText = !hasDownload && state.currentStatusTone !== "processing"
    ? "Output belum tersedia. Klik Mulai Proses untuk membuat file unduhan."
    : requestedRows.length
      ? `${formatNumber(readyRows.length)} siap dari ${formatNumber(requestedRows.length)} output aktif`
      : "Belum ada output yang dipilih.";
  const pillText = pendingRows.length
    ? `${formatNumber(pendingRows.length)} menunggu`
    : readyRows.length
      ? "Semua siap"
      : "Menunggu";
  const pillTone = pendingRows.length ? "waiting" : readyRows.length ? "ready" : "idle";
  if (els.outputStatusSummaryText) {
    els.outputStatusSummaryText.textContent = summaryText;
  }
  if (els.outputStatusSummaryPill) {
    els.outputStatusSummaryPill.textContent = pillText;
    els.outputStatusSummaryPill.dataset.tone = pillTone;
  }
  els.outputStatusGrid.innerHTML = `
    ${visibleRows.length ? visibleRows.map((row) => {
      const stateName = isFailed && row.requested && !row.ready ? "error" : row.ready ? "ready" : isProcessing ? "processing" : "waiting";
      const stateLabel = isFailed && row.requested && !row.ready ? "Gagal" : row.ready ? "Siap" : isProcessing ? "Memproses" : outputStateLabel(row.requested, row.ready);
      return `
        <div class="output-status-row" data-state="${stateName}">
          <span class="out-icon">${outputIconSvg(row.key)}</span>
          <span class="out-label">
            <strong>${escapeHtml(row.label)}</strong>
            <span>${escapeHtml(row.desc)}</span>
          </span>
          <span class="out-state">${escapeHtml(stateLabel)}</span>
        </div>
      `;
    }).join("") : '<div class="output-status-empty">Output belum tersedia. Klik Mulai Proses untuk membuat file unduhan.</div>'}
    ${notRequestedRows.length ? `<p class="output-status-note">Output lain tidak dipilih: ${escapeHtml(notRequestedRows.map((row) => row.label).join(", "))}</p>` : ""}
  `;
}

function renderMapStats() {
  if (!els.mapStatsGrid) return;
  const items = [
    ["ROAD tampil", formatNumber(statValue("ROAD"))],
    ["Fitur KML tampil", formatNumber(statValue("KML"))],
  ];
  els.mapStatsGrid.innerHTML = items.map(([label, value]) => `
    <article class="map-stat">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </article>
  `).join("");
}

function mapLayerKey(feature) {
  const raw = feature?.properties?.layer || "OTHER";
  return String(raw).trim().toUpperCase() || "OTHER";
}

function mapLayerStyle(feature, visible = true) {
  const base = layerStyles[mapLayerKey(feature)] || layerStyles.OTHER;
  if (visible) {
    return {
      ...base,
      opacity: base.opacity ?? 1,
      fillOpacity: base.fillOpacity ?? 0.75,
    };
  }
  return {
    ...base,
    opacity: 0,
    fillOpacity: 0,
  };
}

function applyMapLayerVisibility() {
  if (!state.geoJsonLayer) return;
  state.geoJsonLayer.eachLayer((layer) => {
    const key = mapLayerKey(layer.feature);
    const visible = state.mapLayerVisibility[key] !== false;
    if (typeof layer.setStyle === "function") {
      layer.setStyle(mapLayerStyle(layer.feature, visible));
    }
    if (typeof layer.setOpacity === "function") {
      layer.setOpacity(visible ? 1 : 0);
    }
    const element = typeof layer.getElement === "function" ? layer.getElement() : null;
    if (element) element.style.pointerEvents = visible ? "" : "none";
  });
}

function renderMapLegend() {
  if (!els.mapLegend || !els.mapLegendList || !els.mapLegendCount) return;
  if (!state.geoJsonLayer) {
    els.mapLegend.classList.add("hidden");
    els.mapLegendList.innerHTML = "";
    els.mapLegendCount.textContent = "0";
    return;
  }

  const groups = new Map();
  state.geoJsonLayer.eachLayer((layer) => {
    const key = mapLayerKey(layer.feature);
    const style = layerStyles[key] || layerStyles.OTHER;
    if (!Object.prototype.hasOwnProperty.call(state.mapLayerVisibility, key)) {
      state.mapLayerVisibility[key] = true;
    }
    const group = groups.get(key) || {
      key,
      label: key.replaceAll("_", " "),
      color: style.color || style.fillColor || layerStyles.OTHER.color,
      count: 0,
    };
    group.count += 1;
    groups.set(key, group);
  });

  const order = ["ROAD", "KML", "FDT", "FAT", "HP_COVER", "HP_UNCOVER", "POLE", "CABLE_LENGTH", "SLING_LENGTH", "OTHER"];
  const rows = [...groups.values()].sort((a, b) => {
    const aIndex = order.indexOf(a.key);
    const bIndex = order.indexOf(b.key);
    if (aIndex !== -1 || bIndex !== -1) return (aIndex === -1 ? 99 : aIndex) - (bIndex === -1 ? 99 : bIndex);
    return a.label.localeCompare(b.label);
  });

  els.mapLegend.classList.remove("hidden");
  els.mapLegendCount.textContent = formatNumber(rows.length);
  if (els.mapLegendBody) els.mapLegendBody.hidden = !state.mapLegendOpen;
  if (els.mapLegendToggle) els.mapLegendToggle.setAttribute("aria-expanded", String(state.mapLegendOpen));
  els.mapLegendList.innerHTML = rows.map((row) => `
    <li class="map-legend-row">
      <label>
        <input type="checkbox" data-map-layer="${escapeHtml(row.key)}" ${state.mapLayerVisibility[row.key] !== false ? "checked" : ""}>
        <span class="map-legend-swatch" style="background:${escapeHtml(row.color)}"></span>
        <span class="map-legend-name">${escapeHtml(row.label)}</span>
        <strong>${formatNumber(row.count)}</strong>
      </label>
    </li>
  `).join("");
  applyMapLayerVisibility();
}

function renderIssueCenter() {
  if (!els.issueCenter) return;
  if (!hasResultData()) {
    els.issueCenter.innerHTML = "";
    return;
  }
  const summary = state.boqSummary || {};
  const hpdb = state.hpdbSummary || {};
  const hpdbWarnings = Math.max(hpdbIssueRows().filter((row) => isWarningStatus(row.status)).length, Number(hpdb.total_hpdb_warnings || 0), statValue("HPDB_WARNINGS"));
  const warnings = Math.max(warningRows().length, Number(summary.total_warning_unmapped || 0), statValue("BOQ_WARNINGS")) + hpdbWarnings;
  const errors = countConsoleSeverity("ERROR") + (state.currentStatusTone === "error" ? 1 : 0);
  const unmapped = state.boqPreviewRows.filter((row) => String(row.status || "").toLowerCase() === "unmapped").length;
  const issueTone = errors ? "error" : warnings || unmapped ? "warning" : "ok";
  const heroTone = errors ? "error" : warnings ? "warning" : "ok";
  if (!errors && !warnings && !unmapped) {
    els.issueCenter.innerHTML = "";
    return;
  }
  const rows = [
    ["Total error", formatNumber(errors)],
    ["BOQ unmapped", formatNumber(unmapped)],
    ["Peringatan HPDB", formatNumber(hpdbWarnings)],
    ["FDT skipped >10", formatNumber(hpdb.total_fdt_skipped_over_max || 0)],
    ["Console entries", formatNumber(state.consoleLogs.length)],
  ];

  els.issueCenter.innerHTML = `
    <div class="issue-center-header" data-tone="${issueTone}">
      <span>Ringkasan Peringatan</span>
      <strong>${errors ? "Ada error yang perlu diperhatikan" : warnings || unmapped ? "Peringatan ditemukan" : "Tidak ada peringatan"}</strong>
    </div>
    <div class="issue-center-body">
      <article class="issue-center-hero" data-tone="${heroTone}">
        <span>Total warning</span>
        <strong>${formatNumber(warnings)}</strong>
        <button type="button" class="link-button" id="issueCenterJumpToWarnings">Lihat detail &rarr;</button>
      </article>
      <div class="issue-center-grid">
        ${rows.map(([label, value]) => `
          <div>
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value)}</strong>
          </div>
        `).join("")}
      </div>
    </div>
  `;
  document.getElementById("issueCenterJumpToWarnings")?.addEventListener("click", () => {
    setPreviewTab("warnings");
    els.previewTabs?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

function groupCount(items, keyFn) {
  return items.reduce((acc, item) => {
    const key = keyFn(item) || "-";
    acc.set(key, (acc.get(key) || 0) + 1);
    return acc;
  }, new Map());
}

function warningSummaryText() {
  const rows = allIssueRows();
  if (!rows.length) return "Tidak ada warning.";
  const byFolder = [...groupCount(rows, (row) => row.source_folder || "-")];
  const byReason = [...groupCount(rows, (row) => warningReason(row))];
  return [
    `Total warning/unmapped: ${rows.length}`,
    "",
    "Per folder:",
    ...byFolder.map(([folder, count]) => `- ${folder}: ${count}`),
    "",
    "Per alasan:",
    ...byReason.map(([reason, count]) => `- ${reason}: ${count}`),
  ].join("\n");
}

function renderWarningPanels() {
  if (!els.warningSummaryPanel || !els.warningList) return;
  const rows = allIssueRows();
  if (els.copyWarningSummaryButton) {
    els.copyWarningSummaryButton.hidden = !rows.length;
  }
  if (els.viewWarningsInBoqButton) {
    els.viewWarningsInBoqButton.hidden = !rows.length || !availablePreviewTabs().includes("boq");
  }
  if (!rows.length) {
    els.warningSummaryPanel.innerHTML = `
      <div class="warning-empty-state">
        <strong>Tidak ada peringatan</strong>
        <p>Jika sudah menjalankan Mulai Proses, berarti tidak ada item unmapped, skipped, atau peringatan dari hasil proses.</p>
      </div>
    `;
    els.warningList.innerHTML = "";
    return;
  }

  const byFolder = [...groupCount(rows, (row) => row.source_folder || "-")];
  const byReason = [...groupCount(rows, (row) => warningReason(row))];
  els.warningSummaryPanel.innerHTML = `
    <div class="warning-summary-grid">
      <article><span>Total warning</span><strong>${formatNumber(rows.length)}</strong></article>
      <article><span>Folder terdampak</span><strong>${formatNumber(byFolder.length)}</strong></article>
      <article><span>Alasan</span><strong>${formatNumber(byReason.length)}</strong></article>
    </div>
    <div class="warning-action-note">
      <strong>Yang perlu dicek:</strong>
      <span>Periksa folder sumber, nama item, dan mapping target cell. Gunakan tombol "Lihat di tabel BOQ" untuk menelusuri baris terkait.</span>
    </div>
    <div class="warning-group-grid">
      <div>
        <h4>Per folder</h4>
        ${byFolder.map(([folder, count]) => `<p><span>${escapeHtml(folder)}</span><strong>${formatNumber(count)}</strong></p>`).join("")}
      </div>
      <div>
        <h4>Per alasan</h4>
        ${byReason.map(([reason, count]) => `<p><span>${escapeHtml(reason)}</span><strong>${formatNumber(count)}</strong></p>`).join("")}
      </div>
    </div>
  `;

  els.warningList.innerHTML = rows.map((row) => `
    <article class="warning-row">
      <div class="min-w-0">
        <div class="flex flex-wrap items-center gap-2">
          <span class="boq-status-pill" data-status="${escapeHtml(row.status || "Peringatan")}">${escapeHtml(row.status || "Peringatan")}</span>
          <strong>${escapeHtml(row.item || row.source_folder || "Unnamed item")}</strong>
        </div>
        <p>${escapeHtml(warningReason(row))}</p>
        <small>${escapeHtml(row.source_folder || "-")} / ${escapeHtml(row.parent_line_fdt || "-")}</small>
      </div>
      <span>${escapeHtml(row.target_cell || "-")}</span>
    </article>
  `).join("");
}

function availablePreviewTabs() {
  const hasDxf = modeHas("dxf") || Boolean(state.geoJsonLayer) || !statsAllZero();
  const hasBoq = modeHas("boq") || modeHas("astri") || state.boqPreviewRows.length > 0 || Boolean(state.boqNroPreview?.rows?.length);
  const hasHpdb = modeHas("hpdb") || modeHas("astri") || Boolean(state.hpdbPreview?.cluster?.length) || Boolean(state.hpdbPreview?.fat?.length) || Boolean(state.hpdbPreview?.homepass?.length);
  return [
    "summary",
    ...(hasDxf ? ["map", "stats"] : []),
    ...(hasBoq ? ["boq"] : []),
    ...(hasHpdb ? ["hpdb"] : []),
    "warnings",
    "log",
  ];
}

function renderPreviewTabVisibility() {
  if (!els.previewTabs) return;
  const available = new Set(availablePreviewTabs());
  let changedActive = false;
  document.querySelectorAll("[data-preview-tab]").forEach((tab) => {
    const isAvailable = available.has(tab.dataset.previewTab);
    tab.hidden = !isAvailable;
    if (!isAvailable && tab.dataset.previewTab === state.activePreviewTab) {
      state.activePreviewTab = "summary";
      changedActive = true;
    }
  });
  if (changedActive) setPreviewTab("summary");
}

function renderConsoleMeta() {
  refreshDurationFromLogs();
  const warnings = countConsoleSeverity("WARNING");
  const errors = countConsoleSeverity("ERROR");
  const hasLogs = state.consoleLogs.length > 0;
  if (els.consolePanel) {
    els.consolePanel.classList.toggle("hidden", !hasLogs && !state.consoleLive);
    els.consolePanel.dataset.tone = errors ? "error" : warnings ? "warning" : state.consoleLive ? "live" : "success";
  }
  if (els.consoleWarningsBadge) {
    els.consoleWarningsBadge.textContent = `${formatNumber(warnings)} peringatan`;
    els.consoleWarningsBadge.dataset.tone = warnings ? "warning" : "neutral";
    els.consoleWarningsBadge.hidden = warnings === 0;
  }
  if (els.consoleErrorsBadge) {
    els.consoleErrorsBadge.textContent = `${formatNumber(errors)} error`;
    els.consoleErrorsBadge.dataset.tone = errors ? "error" : "neutral";
    els.consoleErrorsBadge.hidden = errors === 0;
  }
  if (els.consoleDurationBadge) {
    els.consoleDurationBadge.textContent = `Durasi ${state.durationLabel || "--"}`;
    els.consoleDurationBadge.dataset.tone = "neutral";
  }
  if (els.consoleLogShell) els.consoleLogShell.hidden = state.consoleCollapsed;
  document.querySelectorAll("[data-log-detail-action]").forEach((button) => {
    button.hidden = state.consoleCollapsed;
  });
}

function renderSupportPanels() {
  renderPreviewTabVisibility();
  renderWorkflowStepper();
  renderDashboardSummary();
  renderResultWorkspaceSummary();
  renderOutputStatus();
  renderMapStats();
  renderMapLegend();
  renderDxfEmptyState();
  renderIssueCenter();
  renderWarningPanels();
  renderConsoleMeta();
  renderPreflightSummary();
  renderSystemReadiness();
  renderValidationSummary();
}

function activePreviewPanelName() {
  if (state.activePreviewTab === "boq") {
    return state.activeBoqPreviewTab === "nro" ? "nro" : "boq";
  }
  if (state.activePreviewTab === "hpdb") {
    return `hpdb-${state.activeHpdbPreviewTab || "cluster"}`;
  }
  return state.activePreviewTab || "summary";
}

function renderPreviewSubTabs() {
  els.boqSubTabs?.classList.toggle("hidden", state.activePreviewTab !== "boq");
  els.hpdbSubTabs?.classList.toggle("hidden", state.activePreviewTab !== "hpdb");
  document.querySelectorAll("[data-boq-subtab]").forEach((tab) => {
    const isActive = tab.dataset.boqSubtab === state.activeBoqPreviewTab;
    tab.classList.toggle("active", isActive);
    tab.setAttribute("aria-selected", String(isActive));
  });
  document.querySelectorAll("[data-hpdb-subtab]").forEach((tab) => {
    const isActive = tab.dataset.hpdbSubtab === state.activeHpdbPreviewTab;
    tab.classList.toggle("active", isActive);
    tab.setAttribute("aria-selected", String(isActive));
  });
}

function setPreviewTab(_tabName) {
  state.activePreviewTab = "log";
  const activePanel = "log";
  document.querySelectorAll("[data-preview-tab]").forEach((tab) => {
    const isActive = tab.dataset.previewTab === state.activePreviewTab;
    tab.classList.toggle("active", isActive);
    tab.setAttribute("aria-selected", String(isActive));
  });
  document.querySelectorAll("[data-preview-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.previewPanel !== activePanel);
  });
  renderPreviewSubTabs();
  if (state.activePreviewTab === "map") {
    window.setTimeout(() => map.invalidateSize(), 80);
  }
}

function setBoqSubTab(tabName) {
  state.activeBoqPreviewTab = tabName || "table";
  state.activePreviewTab = "boq";
  setPreviewTab("boq");
}

function setHpdbSubTab(tabName) {
  state.activeHpdbPreviewTab = tabName || "cluster";
  state.activePreviewTab = "hpdb";
  setPreviewTab("hpdb");
}

async function copyWarningSummary() {
  const text = warningSummaryText();
  try {
    await navigator.clipboard.writeText(text);
    logger.success("Ringkasan peringatan tersalin ke papan klip.");
  } catch (_error) {
    logger.warn("Clipboard copy is unavailable in this browser session.");
  }
}

function showWarningsInBoqTable() {
  state.boqStatusFilter = "warning";
  state.boqSourceFilter = "all";
  if (els.boqStatusFilter) els.boqStatusFilter.value = "warning";
  if (els.boqSourceFilter) els.boqSourceFilter.value = "all";
  renderBoqPreview();
  setPreviewTab("boq");
}

function consoleTimestamp() {
  return new Date().toLocaleTimeString("en-GB", { hour12: false });
}

function normalizeSeverity(severity) {
  const normalized = String(severity || "INFO").toUpperCase();
  if (normalized === "WARN") return "WARNING";
  return severityMeta[normalized] ? normalized : "INFO";
}

function consoleLogKey(log) {
  return log.source === "server" ? `server:${log.id}` : `local:${log.localId}`;
}

function trimConsoleLogs() {
  if (state.consoleLogs.length > 300) {
    state.consoleLogs = state.consoleLogs.slice(-300);
  }
}

function scrollConsoleToBottom() {
  if (!els.consoleLogShell || state.consoleCollapsed) return;
  window.requestAnimationFrame(() => {
    els.consoleLogShell.scrollTop = els.consoleLogShell.scrollHeight;
  });
}

function renderConsoleLogs() {
  if (!els.consoleLogBody) return;
  trimConsoleLogs();

  if (!state.consoleLogs.length) {
    els.consoleLogBody.innerHTML = '<div class="console-log-empty">Log proses akan muncul di sini setelah generate dimulai.</div>';
    state.consoleRenderedKeys = new Set();
    renderSupportPanels();
    return;
  }

  const previousKeys = state.consoleRenderedKeys;
  const currentKeys = new Set();
  const visibleLogs = state.consoleCollapsed ? state.consoleLogs.slice(-8) : state.consoleLogs;
  els.consoleLogBody.innerHTML = visibleLogs.map((log) => {
    const severity = normalizeSeverity(log.severity);
    const meta = severityMeta[severity];
    const icon = severity === "SUCCESS" ? "ok" : meta.icon;
    const key = consoleLogKey(log);
    currentKeys.add(key);
    const isNew = previousKeys.has(key) ? "" : " console-log-entry-new";
    return `
      <div class="console-log-entry${isNew}" data-severity="${severity}">
        <span class="console-log-time">[${escapeHtml(log.time || consoleTimestamp())}]</span>
        <span class="console-log-severity">${severity.padEnd(7, " ")}</span>
        <span class="console-log-icon" aria-hidden="true">${icon}</span>
        <span class="console-log-message">${escapeHtml(log.message || "")}</span>
      </div>
    `;
  }).join("");
  state.consoleRenderedKeys = currentKeys;
  renderSupportPanels();
  scrollConsoleToBottom();
}

function addConsoleLog({ severity = "INFO", message = "", time = null, source = "local", id = null }) {
  const normalized = normalizeSeverity(severity);
  if (source === "server") {
    const serverId = Number(id);
    if (!serverId || serverId <= state.lastClearedServerLogId || state.consoleServerIds.has(serverId)) {
      return;
    }
    state.consoleServerIds.add(serverId);
    state.consoleLogs.push({
      source: "server",
      id: serverId,
      time: time || consoleTimestamp(),
      severity: normalized,
      message: String(message),
    });
  } else {
    state.consoleLocalSeq += 1;
    state.consoleLogs.push({
      source: "local",
      localId: state.consoleLocalSeq,
      time: time || consoleTimestamp(),
      severity: normalized,
      message: String(message),
    });
  }
  renderConsoleLogs();
}

function mergeServerLogs(logs = []) {
  let changed = false;
  logs.forEach((log) => {
    const serverId = Number(log.id);
    if (!serverId || serverId <= state.lastClearedServerLogId || state.consoleServerIds.has(serverId)) {
      return;
    }
    const severity = normalizeSeverity(log.severity);
    state.consoleServerIds.add(serverId);
    state.consoleLogs.push({
      source: "server",
      id: serverId,
      time: log.time || consoleTimestamp(),
      severity,
      message: String(log.message || ""),
    });
    changed = true;
  });
  if (!changed) return;
  renderConsoleLogs();
}

const logger = {
  info: (message) => addConsoleLog({ severity: "INFO", message }),
  success: (message) => addConsoleLog({ severity: "SUCCESS", message }),
  warn: (message) => addConsoleLog({ severity: "WARNING", message }),
  error: (message) => addConsoleLog({ severity: "ERROR", message }),
};
window.logger = logger;

function clearConsoleLogs() {
  const maxServerId = state.consoleLogs.reduce((maxId, log) => {
    if (log.source !== "server") return maxId;
    return Math.max(maxId, Number(log.id) || 0);
  }, state.lastClearedServerLogId);
  state.lastClearedServerLogId = maxServerId;
  state.consoleLogs = [];
  state.consoleServerIds.clear();
  state.consoleRenderedKeys = new Set();
  state.durationLabel = "--";
  setConsoleCollapsed(true);
}

function formatConsoleLine(log) {
  const severity = normalizeSeverity(log.severity).padEnd(9, " ");
  const lines = String(log.message || "").split("\n");
  return lines.map((line, index) => {
    if (index === 0) return `[${log.time || ""}] ${severity} ${line}`;
    return `            ${line}`;
  }).join("\n");
}

function consoleLogText() {
  return state.consoleLogs.map(formatConsoleLine).join("\n");
}

async function copyConsoleLog() {
  const text = consoleLogText();
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    logger.success("Log proses disalin ke clipboard.");
  } catch (_error) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "true");
    textarea.style.position = "fixed";
    textarea.style.left = "-1000px";
    textarea.style.top = "0";
    textarea.style.width = "1px";
    textarea.style.height = "1px";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    const copied = document.execCommand("copy");
    textarea.remove();
    if (copied) {
      logger.success("Log proses disalin ke clipboard.");
    } else {
      logger.warn("Clipboard copy is unavailable in this browser session.");
    }
  }
}

function downloadConsoleLog() {
  const text = consoleLogText();
  if (!text) return;
  const suffix = state.jobId || new Date().toISOString().replace(/[:.]/g, "-");
  const blob = new Blob([text + "\n"], { type: "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `processing-log-${suffix}.txt`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(link.href), 500);
  logger.success("Log proses diekspor sebagai TXT.");
}

function setConsoleLive(isLive) {
  state.consoleLive = Boolean(isLive);
  if (els.consoleLiveIndicator) {
    els.consoleLiveIndicator.classList.toggle("hidden", !state.consoleLive);
  }
  renderConsoleMeta();
}

function setConsoleCollapsed(isCollapsed) {
  state.consoleCollapsed = Boolean(isCollapsed);
  if (els.consoleLogShell) {
    els.consoleLogShell.hidden = state.consoleCollapsed;
    els.consoleLogShell.classList.toggle("collapsed", state.consoleCollapsed);
  }
  if (els.consoleToggle) {
    els.consoleToggle.textContent = state.consoleCollapsed ? "Lihat log" : "Sembunyikan log";
    els.consoleToggle.setAttribute("aria-expanded", String(!state.consoleCollapsed));
  }
  document.querySelectorAll("[data-log-detail-action]").forEach((button) => {
    button.hidden = state.consoleCollapsed;
  });
  renderConsoleLogs();
  if (!state.consoleCollapsed) scrollConsoleToBottom();
}

function selectedLayers() {
  return ["ROAD", "KML"];
}

function initKmlFolderSelector() {
  state.kmlFolders = [];
  renderKmlFolderSelector();
}

function folderWarningCount(folderKey) {
  return allIssueRows().filter((row) => String(row.source_folder || "").toUpperCase() === String(folderKey || "").toUpperCase()).length;
}

function updateKmlFolderSummary() {
  const selectable = state.kmlFolders.filter((folder) => Number(folder.count || 0) > 0);
  const selected = selectable.filter((folder) => folder.selected);
  const totalItems = state.kmlFolders.reduce((sum, folder) => sum + Number(folder.count || 0), 0);
  const warningCount = state.kmlFolders.reduce((sum, folder) => sum + folderWarningCount(folder.key), 0);
  if (els.kmlFolderSelectedCount) els.kmlFolderSelectedCount.textContent = selectable.length ? `${selected.length} dari ${selectable.length}` : "Belum ada";
  if (els.kmlFolderTotalItems) els.kmlFolderTotalItems.textContent = totalItems ? formatNumber(totalItems) : "Belum ada";
  if (els.kmlFolderWarningCount) els.kmlFolderWarningCount.textContent = warningCount ? formatNumber(warningCount) : "Tidak ada";
}

function filteredKmlFoldersForGroup(groupFolders) {
  const query = state.folderSearch.trim().toLowerCase();
  return groupFolders.filter((folder) => {
    const isSelected = Boolean(folder.selected);
    const hasItem = Number(folder.count || 0) > 0;
    const warnings = folderWarningCount(folder.key);
    const matchesQuery = !query
      || String(folder.path || folder.key).toLowerCase().includes(query)
      || String(folder.key || "").toLowerCase().includes(query);
    if (!matchesQuery) return false;
    if (state.folderFilter === "selected") return isSelected;
    if (state.folderFilter === "has_item") return hasItem;
    if (state.folderFilter === "empty") return !hasItem;
    if (state.folderFilter === "warning") return warnings > 0;
    return true;
  });
}

function renderKmlFolderSelector() {
  updateKmlFolderSummary();
  if (!state.kmlFolders.length) {
    els.kmlFolderPanel?.classList.add("hidden");
    if (els.kmlFolderSelector) els.kmlFolderSelector.innerHTML = "";
    renderSupportPanels();
    return;
  }

  els.kmlFolderPanel.classList.add("hidden");
  els.kmlFolderPanel.setAttribute("aria-hidden", "true");
  els.kmlFolderPanel.setAttribute("inert", "");
  renderSupportPanels();
  return;
  const folderMap = new Map(state.kmlFolders.map((folder) => [folder.key, folder]));
  const renderedGroups = activeKmlFolderGroups().map((group, groupIndex) => {
    const allGroupFolders = group.folders.map((folderName) => folderMap.get(folderName)).filter(Boolean);
    const groupFolders = filteredKmlFoldersForGroup(allGroupFolders);
    if (!groupFolders.length) return "";
    const itemCount = groupFolders.reduce((sum, folder) => sum + Number(folder.count || 0), 0);
    const selectableCount = groupFolders.filter((folder) => Number(folder.count || 0) > 0).length;
    const selectedCount = groupFolders.filter((folder) => folder.selected && Number(folder.count || 0) > 0).length;
    const isOpen = state.expandedFolders.has(group.title) || Boolean(state.folderSearch.trim());
    return `
      <details class="kml-folder-group" data-group-title="${escapeHtml(group.title)}" data-group-index="${groupIndex}" ${isOpen ? "open" : ""}>
        <summary class="kml-folder-group-summary">
          <span class="flex min-w-0 items-center gap-2">
            <span class="kml-folder-caret" aria-hidden="true">&gt;</span>
            <input class="kml-folder-group-checkbox h-4 w-4 rounded border-zinc-600 bg-zinc-900 text-cyan-300 focus:ring-cyan-300" type="checkbox" ${selectedCount && selectedCount === selectableCount ? "checked" : ""} ${!selectableCount ? "disabled" : ""} data-group-index="${groupIndex}" aria-label="${escapeHtml(group.title)}">
            <span class="min-w-0 truncate font-semibold text-zinc-100">${escapeHtml(group.title)}</span>
          </span>
          <span class="flex shrink-0 items-center gap-2 text-[11px] text-zinc-500">
            <span class="kml-folder-group-selected">${selectedCount} dari ${selectableCount} dipilih</span>
            <span>${itemCount.toLocaleString("id-ID")} item</span>
          </span>
        </summary>
        <div class="kml-folder-children">
          ${groupFolders.map((folder) => renderKmlFolderRow(folder)).join("")}
        </div>
      </details>
    `;
  }).join("");

  els.kmlFolderSelector.innerHTML = renderedGroups.trim()
    || '<div class="empty-state">Tidak ada folder sesuai filter.</div>';

  document.querySelectorAll(".kml-folder-group").forEach((group) => {
    group.addEventListener("toggle", () => {
      const title = group.dataset.groupTitle;
      if (!title) return;
      if (group.open) state.expandedFolders.add(title);
      else state.expandedFolders.delete(title);
    });
  });

  document.querySelectorAll(".kml-folder-checkbox").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const folder = state.kmlFolders.find((item) => item.key === checkbox.value);
      if (folder) folder.selected = checkbox.checked;
      syncKmlFolderGroupCheckboxes();
      updateKmlFolderText();
      renderSupportPanels();
      notifyEvaAssistant("kml-folders-selected", {
        key: `kml-folders-selected:${selectedKmlFolders().join("|")}`,
      });
    });
  });
  document.querySelectorAll(".kml-folder-group-checkbox").forEach((checkbox) => {
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      const group = checkbox.closest(".kml-folder-group");
      group?.querySelectorAll(".kml-folder-checkbox").forEach((child) => {
        child.checked = checkbox.checked;
        const folder = state.kmlFolders.find((item) => item.key === child.value);
        if (folder) folder.selected = checkbox.checked;
      });
      syncKmlFolderGroupCheckboxes();
      updateKmlFolderText();
      renderSupportPanels();
      notifyEvaAssistant("kml-folders-selected", {
        key: `kml-folders-selected:${selectedKmlFolders().join("|")}`,
      });
    });
  });
  syncKmlFolderGroupCheckboxes();
  updateKmlFolderText();
  updateKmlFolderSummary();
  renderSupportPanels();
}

function renderKmlFolderRow(folder, extraClass = "") {
  const hasItem = Number(folder.count || 0) > 0;
  const warnings = folderWarningCount(folder.key);
  const rowState = warnings ? "warning" : !folder.exists ? "missing" : !hasItem ? "empty" : "ok";
  const hpdbRequested = modeHas("hpdb") || modeHas("astri");
  const hpdbNote = hpdbRequested && folder.key === "HP COVER"
    ? "exported"
    : hpdbRequested && folder.key === "HP UNCOVER"
      ? "ignored for HPDB/ASTRI"
      : "";
  return `
    <label class="kml-folder-row ${extraClass}" data-state="${rowState}" data-empty="${hasItem ? "false" : "true"}" data-warning="${warnings ? "true" : "false"}">
      <span class="flex min-w-0 items-center gap-2">
        <input class="kml-folder-checkbox h-4 w-4 rounded border-zinc-600 bg-zinc-900 text-cyan-300 focus:ring-cyan-300" type="checkbox" value="${escapeHtml(folder.key)}" ${folder.selected && hasItem ? "checked" : ""} ${hasItem ? "" : "disabled"}>
        <span class="kml-folder-status" aria-hidden="true">${kmlFolderStatusIcon(rowState)}</span>
        <span class="min-w-0 truncate">
          <span class="font-semibold text-zinc-100">${escapeHtml(folder.path)}</span>
          <span class="ml-2 text-zinc-500">${folder.fileCount || 0} file</span>
        </span>
      </span>
      <span class="flex shrink-0 items-center gap-2 text-zinc-500">
        ${hpdbNote ? `<span class="folder-warning-badge" data-tone="${folder.key === "HP UNCOVER" ? "info" : "ok"}">${escapeHtml(hpdbNote)}</span>` : ""}
        ${warnings ? `<span class="folder-warning-badge">${formatNumber(warnings)} warning</span>` : ""}
        <span>${Number(folder.count || 0).toLocaleString("id-ID")} item</span>
      </span>
    </label>
  `;
}

function kmlFolderStatusIcon(stateName) {
  const icons = {
    ok: '<svg viewBox="0 0 24 24" fill="none"><path d="m5 12 4 4L19 6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    warning: '<svg viewBox="0 0 24 24" fill="none"><path d="M12 8v5m0 3h.01M10.2 4.6 3.5 17.2A1.5 1.5 0 0 0 4.8 19.5h14.4a1.5 1.5 0 0 0 1.3-2.3L13.8 4.6a2 2 0 0 0-3.6 0Z" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    missing: '<svg viewBox="0 0 24 24" fill="none"><path d="m7 7 10 10M17 7 7 17" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>',
    empty: '<svg viewBox="0 0 24 24" fill="none"><path d="M7 12h10" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>',
  };
  return icons[stateName] || icons.empty;
}

function syncKmlFolderGroupCheckboxes() {
  document.querySelectorAll(".kml-folder-group").forEach((group) => {
    const checkbox = group.querySelector(".kml-folder-group-checkbox");
    const children = Array.from(group.querySelectorAll(".kml-folder-checkbox:not(:disabled)"));
    const selectedCount = children.filter((child) => child.checked).length;
    if (checkbox) {
      checkbox.checked = children.length > 0 && selectedCount === children.length;
      checkbox.indeterminate = selectedCount > 0 && selectedCount < children.length;
      checkbox.disabled = children.length === 0;
    }
    const selectedText = group.querySelector(".kml-folder-group-selected");
    if (selectedText) {
      selectedText.textContent = `${selectedCount} dari ${children.length} dipilih`;
    }
  });
}

function updateKmlFolderText() {
  updateKmlFolderSummary();
  if (!state.kmlFolders.length) return;
  const selected = selectedKmlFolders();
  const selectable = state.kmlFolders.filter((folder) => Number(folder.count || 0) > 0);
  if (selected.length === selectable.length && selectable.length) {
    els.kmlFolderText.textContent = "Semua folder KML/KMZ dipilih.";
  } else if (selected.length === 0) {
    els.kmlFolderText.textContent = "Tidak ada folder KML/KMZ dipilih.";
  } else {
    els.kmlFolderText.textContent = `${selected.length} dari ${selectable.length} folder berisi item dipilih.`;
  }
}

function selectedKmlFolders() {
  const selectable = state.kmlFolders.filter((folder) => Number(folder.count || 0) > 0);
  // Folder selector is intentionally hidden: every eligible non-empty folder
  // is automatic. Do not retain legacy per-folder selected flags.
  return selectable.map((folder) => folder.key);
}

function escapeHtml(value) {
  if (evaUtils.escapeHtml) return evaUtils.escapeHtml(value);
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function isKmlFile(file) {
  const name = file.name.toLowerCase();
  return name.endsWith(".kml") || name.endsWith(".kmz");
}

function isOsmFile(file) {
  const name = file.name.toLowerCase();
  return name.endsWith(".osm") || name.endsWith(".pbf") || name.endsWith(".osm.pbf");
}

function selectedFiles() {
  return [...state.kmlFiles];
}

function filenameWithoutExtension(name) {
  return String(name || "").replace(/\.(kml|kmz)$/i, "").trim();
}

let filenameRulesCache = null;

async function loadFilenameRules() {
  if (filenameRulesCache) return filenameRulesCache;
  try {
    const response = await fetch("/api/filename-rules");
    const payload = await response.json();
    filenameRulesCache = payload.rules || {};
  } catch (error) {
    filenameRulesCache = {};
  }
  return filenameRulesCache;
}

function filenameValidationRule(scope = state.scopeType) {
  const normalized = normalizeScopeType(scope);
  const dynamicRule = filenameRulesCache?.[normalized];
  if (dynamicRule?.pattern && dynamicRule?.sample) {
    try {
      return {
        scope: normalized,
        pattern: new RegExp(dynamicRule.pattern, "i"),
        sample: dynamicRule.sample,
        checklist: Array.isArray(dynamicRule.checklist) ? dynamicRule.checklist : [],
      };
    } catch (error) {}
  }
  if (normalized === "Subfeeder") {
    return {
      scope: "Subfeeder",
      pattern: /^[A-Z]{2,}\d{4,}\s*-\s*SF\s+.+$/i,
      sample: "BKS005380 - SF PERUMAHAN KIRANA CIKARANG BEKASI.kmz",
      checklist: ["Kode area/project di depan: BKS005380", "Separator: tanda strip (-)", "Prefix SF setelah strip", "Nama project/lokasi lengkap"],
    };
  }
  if (normalized === "Feeder") {
    return {
      scope: "Feeder",
      pattern: /^[A-Z]{1,}\d{4,}\s*-\s*.+\b(?:MAIN\s+FEEDER|FEEDER)\b.+\b(?:OLT|SEGMENT|FO|\d+C)\b.+$/i,
      sample: "FBKS0152 - BEKASI - MAIN FEEDER OLT_CIKARANG_UTARA_SEGMENT_3_TO_PASIRGOMBONG_FO_144C.kmz",
      checklist: ["Kode feeder di depan: FBKS0152", "Mengandung FEEDER / MAIN FEEDER", "Mengandung OLT/Segment/destination", "Mengandung kapasitas kabel: 144C / 288C"],
    };
  }
  return {
    scope: "Cluster",
    pattern: /^[A-Z]{2,}\d{4,}\s*-\s*(?!SF\b).+$/i,
    sample: "BKS005380 - PERUMAHAN KIRANA CIKARANG BEKASI.kmz",
    checklist: ["Kode area/project di depan: BKS005380", "Separator: tanda strip (-)", "Nama project/lokasi lengkap", "Jangan pakai prefix SF untuk Cluster"],
  };
}

function invalidFilenameItems(files, scope = state.scopeType) {
  const rule = filenameValidationRule(scope);
  return files
    .filter(isKmlFile)
    .map((file) => ({ file, base: filenameWithoutExtension(file.name), rule }))
    .filter((item) => !item.rule.pattern.test(item.base));
}

function showFilenameWarningModal(invalidItems) {
  return new Promise((resolve) => {
    if (!invalidItems.length) return resolve(true);
    const first = invalidItems[0];
    const rule = first.rule;
    const modal = document.createElement("div");
    modal.className = "fixed inset-0 z-[9999] flex items-center justify-center bg-black/55 p-4";
    modal.innerHTML = `
      <div class="w-full max-w-xl overflow-hidden rounded-3xl border border-white/70 bg-white shadow-[0_30px_100px_rgba(15,23,42,0.35)]">
        <div class="border-b border-zinc-200 bg-gradient-to-br from-amber-50 to-white p-5">
          <div class="flex items-start justify-between gap-4">
            <div>
              <p class="text-xs font-black uppercase tracking-[0.2em] text-amber-600">Filename warning</p>
              <h3 class="mt-1 text-xl font-black text-zinc-900">Nama file belum sesuai format ${escapeHtml(rule.scope)}.</h3>
              <p class="mt-1 text-sm text-zinc-600">Cek nama file sebelum lanjut supaya output naming dan matching tidak salah.</p>
            </div>
            <button type="button" data-action="cancel" class="rounded-full border border-zinc-200 bg-white px-3 py-2 text-sm font-bold text-zinc-600">×</button>
          </div>
        </div>
        <div class="space-y-4 p-5">
          <div class="rounded-2xl border border-red-100 bg-red-50 p-4">
            <p class="text-xs font-black uppercase tracking-[0.16em] text-red-600">File yang dicek</p>
            <ul class="mt-2 space-y-1 text-sm font-bold text-red-900">
              ${invalidItems.map((item) => `<li>${escapeHtml(item.file.name)}</li>`).join("")}
            </ul>
          </div>
          <div class="rounded-2xl border border-emerald-100 bg-emerald-50 p-4">
            <p class="text-xs font-black uppercase tracking-[0.16em] text-emerald-700">Format benar</p>
            <p class="mt-2 break-words rounded-xl bg-white p-3 font-mono text-sm font-bold text-emerald-900">${escapeHtml(rule.sample)}</p>
          </div>
          <div class="rounded-2xl border border-zinc-200 bg-zinc-50 p-4">
            <p class="text-xs font-black uppercase tracking-[0.16em] text-zinc-600">Pastikan mengandung</p>
            <ul class="mt-2 list-disc space-y-1 pl-5 text-sm text-zinc-700">
              ${rule.checklist.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
            </ul>
          </div>
        </div>
        <div class="flex flex-col-reverse gap-2 border-t border-zinc-200 bg-zinc-50 p-4 sm:flex-row sm:justify-end">
          <button type="button" data-action="cancel" class="rounded-xl border border-zinc-300 bg-white px-4 py-2 text-sm font-black text-zinc-700">Batal Upload</button>
          <button type="button" data-action="continue" class="rounded-xl bg-zinc-900 px-4 py-2 text-sm font-black text-white">Tetap Lanjut</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
    modal.addEventListener("click", (event) => {
      const action = event.target?.closest?.("[data-action]")?.getAttribute("data-action");
      if (!action) return;
      modal.remove();
      resolve(action === "continue");
    });
  });
}

function outputModeLabel(mode = state.outputMode) {
  if (mode === "kmz") return "KMZ Standar";
  const targets = ["dxf", "boq", "astri", "hpdb", "kmz"].filter((target) => modeHas(target, mode));
  const labels = targets.map((target) => outputTargetLabels[target] || target.toUpperCase());
  return `${state.networkType} ${humanJoinLabels(labels)}${labels.length === 1 ? " saja" : ""}`;
}

function outputExtension(mode = state.outputMode) {
  if (mode === "astri" && state.downloadMimeType === "application/zip") return ".zip";
  if (mode === "kmz") return ".kmz";
  if (modeTargetCount(mode) > 1) return ".zip";
  if (mode === "boq" || mode === "astri" || mode === "hpdb") return ".xlsx";
  return ".dxf";
}

function outputDownloadText(mode = state.outputMode) {
  if (mode === "kmz") return "Unduh KMZ";
  if (mode === "boq") return "Unduh BOQ (Excel)";
  if (mode === "astri") return state.downloadMimeType === "application/zip" ? "Unduh ASTRI (ZIP)" : "Unduh ASTRI (Excel)";
  if (mode === "hpdb") return "Unduh HPDB (Excel)";
  if (modeTargetCount(mode) > 1) return "Unduh ZIP";
  return "Unduh DXF (CAD)";
}

function setButtonTrailingText(button, text) {
  if (!button) return;
  const textNode = Array.from(button.childNodes).reverse().find((node) => node.nodeType === Node.TEXT_NODE);
  if (textNode) {
    textNode.textContent = ` ${text}`;
  } else {
    button.append(document.createTextNode(` ${text}`));
  }
}

function addListener(element, eventName, handler) {
  if (element) element.addEventListener(eventName, handler);
}

function notifyEvaAssistant(type, detail = {}) {
  window.dispatchEvent(new CustomEvent("eva:assistant-cue", {
    detail: {
      type,
      files: selectedFiles().map((file) => file.name).slice(0, 6),
      outputMode: state.outputMode,
      outputLabels: selectedOutputLabels(),
      selectedFolderCount: selectedKmlFolders().length,
      availableFolderCount: state.kmlFolders.filter((folder) => Number(folder.count || 0) > 0).length,
      downloadName: state.downloadName,
      ...detail,
    },
  }));
}

function outputPickerTypes(mode = state.outputMode) {
  if (mode === "astri" && state.downloadMimeType === "application/zip") {
    return [{ description: "Arsip ASTRI per FDT", accept: { "application/zip": [".zip"] } }];
  }
  if (mode === "kmz") {
    return [{
      description: "Arsip KMZ standar",
      accept: { "application/vnd.google-earth.kmz": [".kmz"], "application/zip": [".kmz"], "application/octet-stream": [".kmz"] },
    }];
  }
  if (mode === "boq" || mode === "astri" || mode === "hpdb") {
    return [{
      description: mode === "hpdb" ? "Workbook HPDB Excel" : mode === "astri" ? "Workbook ASTRI Excel" : "Workbook BOQ Excel",
      accept: { "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"] },
    }];
  }
  if (modeTargetCount(mode) > 1) {
    return [{
      description: "Output ZIP archive",
      accept: { "application/zip": [".zip"], "application/octet-stream": [".zip"] },
    }];
  }
  return [{
    description: "Gambar DXF",
    accept: { "application/dxf": [".dxf"], "application/octet-stream": [".dxf"] },
  }];
}

function projectStemFromFiles() {
  const preferred = state.kmlFiles[0] || selectedFiles()[0];
  const name = preferred?.name || "PROJECT_NAME";
  const stem = name.toLowerCase().endsWith(".osm.pbf")
    ? name.slice(0, -8)
    : name.replace(/\.[^.]+$/, "");
  return stem
    .replace(/[<>:"/\\|?*\x00-\x1F]+/g, "_")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[.\s_]+|[.\s_]+$/g, "") || "PROJECT_NAME";
}

function expectedBoqOutputName() {
  return `${projectStemFromFiles()}_BOQ_${state.networkType}.xlsx`;
}

function expectedAstriOutputName() {
  return `${projectStemFromFiles()}_ASTRI_${state.networkType}.xlsx`;
}

function expectedHpdbOutputName() {
  return `${projectStemFromFiles()}_HPDB_${state.networkType}.xlsx`;
}

function currentBoqTemplateReady() {
  return Boolean(
    window.APP_CONFIG.boqTemplateReadyByNetwork?.[state.networkType]
      ?? window.APP_CONFIG.boqTemplateReady,
  );
}

function currentHpdbTemplateReady() {
  return Boolean(
    window.APP_CONFIG.hpdbTemplateReadyByNetwork?.[state.networkType]
      ?? window.APP_CONFIG.astriTemplateReadyByNetwork?.[state.networkType]
      ?? window.APP_CONFIG.hpdbTemplateReady
      ?? window.APP_CONFIG.astriTemplateReady,
  );
}

function currentBoqTemplateName() {
  return window.APP_CONFIG.boqTemplateNameByNetwork?.[state.networkType]
    || window.APP_CONFIG.boqTemplateName
    || "BoQ APD Cluster_AE.xlsx";
}

function currentHpdbTemplateName() {
  return window.APP_CONFIG.hpdbTemplateNameByNetwork?.[state.networkType]
    || window.APP_CONFIG.astriTemplateNameByNetwork?.[state.networkType]
    || window.APP_CONFIG.astriTemplateName
    || window.APP_CONFIG.hpdbTemplateName
    || "TEMPLATE HPDB_AE.xlsx";
}

function currentDxfTemplateReady() {
  return Boolean(window.APP_CONFIG.dxfConversionEnabled);
}

function readinessRows() {
  return [
    {
      key: "dxf",
      label: "DXF (CAD)",
      detail: window.APP_CONFIG.dxfConversionEnabled ? "DXF aktif" : "DXF terkunci di server ini",
      ready: currentDxfTemplateReady(),
      required: modeHas("dxf"),
    },
    {
      key: "boq",
      label: `BOQ ${state.networkType}`,
      detail: currentBoqTemplateName(),
      ready: currentBoqTemplateReady(),
      required: modeHas("boq"),
    },
    {
      key: "astri_hpdb",
      label: `ASTRI / HPDB ${state.networkType}`,
      detail: currentHpdbTemplateName(),
      ready: currentHpdbTemplateReady(),
      required: modeHas("astri") || modeHas("hpdb"),
    },
    {
      key: "fdt",
      label: "FDT Coding",
      detail: window.APP_CONFIG.fdtCodingSourceLabel || "Server source",
      ready: Boolean(window.APP_CONFIG.fdtCodingSourceReady),
      required: modeHas("kmz") || modeHas("astri") || modeHas("hpdb") || modeHas("boq"),
      fallback: true,
    },
  ];
}

function renderPreflightSummary() {
  if (!els.preflightSummary) return;
  const summary = validationReadiness();
  const region = state.osmKecamatan || state.osmCity || state.osmProvince || "Belum dipilih";
  const fileText = state.kmlFiles.length ? `${formatNumber(state.kmlFiles.length)} file KML/KMZ` : "Belum ada file";
  const folderText = state.kmlFolders.length
    ? `${formatNumber(selectedKmlFolders().length)} dari ${formatNumber(state.kmlFolders.length)} folder dipilih`
    : "Menunggu validasi folder";
  const checks = [
    ["Wilayah ROAD", region !== "Belum dipilih", region],
    ["File desain", state.kmlFiles.length > 0, fileText],
    ["Folder dipilih", !state.kmlFolders.length || selectedKmlFolders().length > 0, folderText],
    ["Output", summary.outputLabels.length > 0, summary.outputLabels.length ? summary.outputLabels.join(", ") : "Belum dipilih"],
    ["Server", summary.ready, summary.ready ? "Siap generate" : "Perlu dicek"],
  ];
  els.preflightSummary.innerHTML = `
    <details class="validation-summary-details" open>
      <summary class="validation-summary-head">
        <span>
          <strong>Checklist sebelum Mulai Proses</strong>
          <small>Ringkasan konfigurasi aktif</small>
        </span>
        <span class="validation-summary-actions">
          ${statusBadge(summary.ready ? "Siap" : "Cek dulu", summary.ready ? "ready" : "warning")}
          <span class="validation-summary-chevron" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="m8 10 4 4 4-4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
        </span>
      </summary>
      <div class="validation-summary-content">
        <div class="validation-summary-grid">
          ${checks.map(([label, ok, value]) => `
            <div>
              <span>${ok ? "Siap" : "Belum"} | ${escapeHtml(label)}</span>
              <strong>${escapeHtml(value)}</strong>
            </div>
          `).join("")}
        </div>
      </div>
    </details>
  `;
}

function renderSystemReadiness() {
  if (!els.systemReadiness) return;
  const rows = readinessRows();
  const requiredRows = rows.filter((row) => row.required);
  const blockingRows = requiredRows.filter((row) => !row.ready && row.key !== "fdt");
  const fallbackRows = requiredRows.filter((row) => row.fallback && !row.ready);
  const tone = blockingRows.length ? "error" : fallbackRows.length ? "warning" : "ready";
  const summaryLabel = blockingRows.length ? "Terblokir" : fallbackRows.length ? "Fallback" : "Siap";
  const summaryText = blockingRows.length
    ? "Ada dependency output yang belum siap di server."
    : fallbackRows.length
      ? "Proses masih bisa berjalan, tapi sebagian hasil bisa memakai placeholder XXX."
      : "Semua dependency utama untuk output yang dipilih sudah siap.";
  els.systemReadiness.innerHTML = `
    <details class="validation-summary-details">
      <summary class="validation-summary-head">
        <span>
          <strong>Kesiapan Sistem</strong>
          <small>${escapeHtml(summaryText)}</small>
        </span>
        <span class="validation-summary-actions">
          ${statusBadge(summaryLabel, tone)}
          <span class="validation-summary-chevron" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="m8 10 4 4 4-4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </span>
        </span>
      </summary>
      <div class="validation-summary-content">
        <div class="validation-summary-grid">
          ${rows.map((row) => `
            <div>
              <span>${escapeHtml(row.label)}${row.required ? " | wajib" : " | opsional"}</span>
              <strong>${row.ready ? "Siap" : row.fallback ? "Fallback / Belum ada" : "Belum ada"}</strong>
              <small class="validation-summary-preview">${escapeHtml(row.detail)}</small>
            </div>
          `).join("")}
        </div>
        ${fallbackRows.length ? `
          <div class="validation-summary-alert" data-tone="warning">
            <p>Sumber coding FDT belum siap. KMZ Standar / ASTRI / HPDB / BOQ yang butuh standardisasi masih bisa berjalan, tapi sebagian nama/kode dapat memakai placeholder <strong>XXX</strong>.</p>
          </div>
        ` : ""}
      </div>
    </details>
  `;
}

function updateBoqTemplateStatus(isReady = currentBoqTemplateReady()) {
  if (!els.boqTemplateStatus) return;
  els.boqTemplateStatus.textContent = `Template BOQ ${state.networkType}: ${isReady ? "Ready" : "Missing"}`;
  els.boqTemplateStatus.className = "template-status-pill";
  els.boqTemplateStatus.classList.add(...(
    isReady
      ? "template-ready"
      : "template-missing"
  ).split(" "));
}

function updateHpdbTemplateStatus(isReady = currentHpdbTemplateReady()) {
  if (!els.hpdbTemplateStatus) return;
  els.hpdbTemplateStatus.textContent = `Template ASTRI ${state.networkType}: ${isReady ? "Ready" : "Missing"}`;
  els.hpdbTemplateStatus.className = "template-status-pill";
  els.hpdbTemplateStatus.classList.add(...(
    isReady
      ? "template-ready"
      : "template-missing"
  ).split(" "));
}

function resetBoqResultState() {
  state.boqPreviewRows = [];
  state.boqNroPreview = { columns: [], rows: [] };
  state.boqSummary = {};
  state.boqWarnings = [];
  renderBoqSummary();
  renderBoqPreview();
  renderBoqNroPreview();
}

function resetHpdbResultState() {
  state.hpdbSummary = {};
  state.hpdbPreview = { cluster: [], fat: [], homepass: [], limit: null, totals: {} };
  state.hpdbWarnings = [];
  state.hpdbIssues = [];
  state.hpdbHomepassPage = 1;
  renderHpdbSummary();
  renderHpdbPreviews();
}

function updateBoqPanel() {
  updateBoqTemplateStatus(currentBoqTemplateReady());
  if (els.boqSheetTarget) els.boqSheetTarget.textContent = window.APP_CONFIG.boqSheetName || "BoM CLUSTER AE";
  if (els.boqOutputFile) els.boqOutputFile.textContent = expectedBoqOutputName();
}

function updateHpdbPanel() {
  updateHpdbTemplateStatus(currentHpdbTemplateReady());
  if (els.hpdbTemplateFile) els.hpdbTemplateFile.textContent = currentHpdbTemplateName();
  if (els.hpdbSheetsTarget) {
    const sheetLabels = [];
    if (modeHas("astri")) sheetLabels.push((window.APP_CONFIG.hpdbSheetNames || ["CLUSTER", "FAT", "HOMEPASS"]).join(", "));
    if (modeHas("hpdb")) sheetLabels.push("FDT-1..FDT-10, FAT");
    els.hpdbSheetsTarget.textContent = sheetLabels.length ? sheetLabels.join(" / ") : "CLUSTER, FAT, HOMEPASS / FDT-1..FDT-10, FAT";
  }
  if (els.hpdbMaxFdt) els.hpdbMaxFdt.textContent = String(window.APP_CONFIG.hpdbMaxFdt || 10);
  if (els.hpdbOutputFile) {
    const outputNames = [];
    if (modeHas("astri")) outputNames.push(expectedAstriOutputName());
    if (modeHas("hpdb")) outputNames.push(expectedHpdbOutputName());
    els.hpdbOutputFile.textContent = outputNames.length ? outputNames.join(" / ") : expectedHpdbOutputName();
  }
}

function renderBoqSummary(summary = state.boqSummary) {
  if (!els.boqSummaryGrid) return;
  if (!hasResultData()) {
    els.boqSummaryGrid.innerHTML = '<div class="summary-empty">BOQ summary akan muncul setelah BOQ Excel diproses.</div>';
    renderSupportPanels();
    return;
  }
  const items = [
    ["BOQ mapped", summary.total_mapped_boq_item || 0],
    ["BOQ warnings", summary.total_warning_unmapped || 0],
    ["Distribution Cable", `${Number(summary.total_distribution_cable_length || 0).toLocaleString("id-ID")} m`],
    ["Sling Wire", `${Number(summary.total_sling_wire_length || 0).toLocaleString("id-ID")} m`],
    ["FAT detected", summary.total_fat_detected || 0],
    ["Pole detected", summary.total_pole_detected || 0],
  ];
  els.boqSummaryGrid.innerHTML = items.map(([label, value]) => `
    <article class="summary-tile">
      <span class="block text-[11px] font-semibold text-zinc-500">${escapeHtml(label)}</span>
      <strong class="mt-1 block text-lg font-bold text-white">${escapeHtml(value)}</strong>
    </article>
  `).join("");
  renderSupportPanels();
}

function renderHpdbSummary(summary = state.hpdbSummary) {
  if (!els.hpdbSummaryGrid) return;
  if (!hasResultData()) {
    els.hpdbSummaryGrid.innerHTML = '<div class="summary-empty">HPDB summary akan muncul setelah HPDB Excel diproses.</div>';
    renderSupportPanels();
    return;
  }
  const items = [
    ["FDT detected", summary.total_fdt_detected || 0],
    ["CLUSTER rows", summary.total_cluster_rows || 0],
    ["FAT rows", summary.total_fat_rows || 0],
    ["HOMEPASS rows", summary.total_homepass_rows || 0],
    ["HP COVER exported", summary.total_hp_cover_exported || 0],
    ["HP UNCOVER ignored", summary.total_hp_uncover_ignored || 0],
    ["FDT skipped >10", summary.total_fdt_skipped_over_max || 0],
  ];
  els.hpdbSummaryGrid.innerHTML = items.map(([label, value]) => `
    <article class="summary-tile">
      <span class="block text-[11px] font-semibold text-zinc-500">${escapeHtml(label)}</span>
      <strong class="mt-1 block text-lg font-bold text-white">${escapeHtml(formatNumber(value))}</strong>
    </article>
  `).join("");
  renderSupportPanels();
}

function setBoqPreviewCollapsed(isCollapsed) {
  state.boqPreviewCollapsed = Boolean(isCollapsed);
  if (els.boqPreviewContent) els.boqPreviewContent.classList.toggle("boq-collapsed", state.boqPreviewCollapsed);
  if (els.boqPreviewToggle) {
    els.boqPreviewToggle.textContent = state.boqPreviewCollapsed ? ">" : "v";
    els.boqPreviewToggle.setAttribute("aria-expanded", String(!state.boqPreviewCollapsed));
    els.boqPreviewToggle.setAttribute("title", state.boqPreviewCollapsed ? "Expand Preview BOQ Table" : "Collapse Preview BOQ Table");
  }
}

function sourceFilterGroup(sourceFolder = "") {
  const source = String(sourceFolder).toUpperCase();
  if (source.includes("DISTRIBUTION CABLE") || source === "CABLE") return "distribution";
  if (source.includes("SLING WIRE")) return "sling";
  if (source === "FDT") return "fdt";
  if (source === "FAT") return "fat";
  if (source.includes("NEW POLE")) return "new_pole";
  if (source.includes("EXISTING POLE PARTNER")) return "partner";
  if (source.includes("EXISTING POLE EMR")) return "emr";
  return "all";
}

function statusMatchesFilter(row) {
  if (state.boqStatusFilter === "all") return true;
  const status = String(row.status || "").toLowerCase();
  if (state.boqStatusFilter === "warning") return status === "warning" || status === "skipped" || status === "unmapped";
  return status === state.boqStatusFilter;
}

function sourceMatchesFilter(row) {
  return state.boqSourceFilter === "all" || sourceFilterGroup(row.source_folder) === state.boqSourceFilter;
}

function renderBoqFilterIndicator(filteredCount, totalCount) {
  if (!els.boqFilterActiveBadge) return;
  const hasActiveFilter = state.boqStatusFilter !== "all" || state.boqSourceFilter !== "all";
  els.boqFilterActiveBadge.classList.toggle("hidden", !hasActiveFilter);
  if (!hasActiveFilter) {
    els.boqFilterActiveBadge.innerHTML = "";
    return;
  }
  els.boqFilterActiveBadge.innerHTML = `
    <span>Filter aktif (${formatNumber(filteredCount)} dari ${formatNumber(totalCount)})</span>
    <button type="button" data-clear-boq-filters>Clear</button>
  `;
  els.boqFilterActiveBadge.querySelector("[data-clear-boq-filters]")?.addEventListener("click", () => {
    state.boqStatusFilter = "all";
    state.boqSourceFilter = "all";
    if (els.boqStatusFilter) els.boqStatusFilter.value = "all";
    if (els.boqSourceFilter) els.boqSourceFilter.value = "all";
    renderBoqPreview();
  });
}

function renderBoqPreview() {
  if (!els.boqPreviewBody) return;
  const filteredRows = state.boqPreviewRows.filter((row) => statusMatchesFilter(row) && sourceMatchesFilter(row));
  renderBoqFilterIndicator(filteredRows.length, state.boqPreviewRows.length);
  if (!state.boqPreviewRows.length) {
    els.boqPreviewBody.innerHTML = '<tr><td colspan="10" class="boq-empty-cell">Belum ada BOQ row yang bisa ditampilkan.</td></tr>';
    if (els.boqPreviewText) els.boqPreviewText.textContent = "Belum ada BOQ row yang bisa ditampilkan.";
    renderSupportPanels();
    return;
  }
  if (!filteredRows.length) {
    els.boqPreviewBody.innerHTML = '<tr><td colspan="10" class="boq-empty-cell">Tidak ada item sesuai filter.</td></tr>';
    if (els.boqPreviewText) els.boqPreviewText.textContent = `${state.boqPreviewRows.length.toLocaleString("id-ID")} BOQ row tersedia.`;
    renderSupportPanels();
    return;
  }
  els.boqPreviewBody.innerHTML = filteredRows.map((row) => `
    <tr title="${escapeHtml(warningReason(row) || "")}">
      <td>${escapeHtml(row.source_folder || "-")}</td>
      <td>${escapeHtml(row.parent_line_fdt || "-")}</td>
      <td>${escapeHtml(row.item || "-")}</td>
      <td>${escapeHtml(row.cable_type || "-")}</td>
      <td>${escapeHtml(row.quantity ?? "-")}</td>
      <td>${escapeHtml(row.unit || "-")}</td>
      <td>${escapeHtml(row.supplier || "")}</td>
      <td>${escapeHtml(row.target_cell || "-")}</td>
      <td class="boq-reason-cell">${escapeHtml(warningReason(row) || "-")}</td>
    </tr>
  `).join("");
  if (els.boqPreviewText) {
    els.boqPreviewText.textContent = `${filteredRows.length.toLocaleString("id-ID")} dari ${state.boqPreviewRows.length.toLocaleString("id-ID")} BOQ row ditampilkan.`;
  }
  renderSupportPanels();
}

function renderBoqNroPreview() {
  if (!els.boqNroPreviewHead || !els.boqNroPreviewBody) return;
  const preview = state.boqNroPreview || { columns: [], rows: [] };
  const columns = preview.columns?.length
    ? preview.columns
    : [
      { key: "N", label: "Kode Material" },
      { key: "B", label: "Description/Item" },
      { key: "D", label: "Material Qty" },
      { key: "E", label: "Service Qty" },
    ];
  els.boqNroPreviewHead.innerHTML = `
    <tr>
      ${columns.map((column) => `<th>${escapeHtml(column.label || column.key)}</th>`).join("")}
    </tr>
  `;
  const rows = preview.rows || [];
  if (els.boqNroExportCsvButton) {
    els.boqNroExportCsvButton.disabled = !rows.length;
  }
  if (!rows.length) {
    els.boqNroPreviewBody.innerHTML = '<tr><td colspan="4" class="boq-empty-cell">Preview NRO belum tersedia.</td></tr>';
    if (els.boqNroPreviewText) els.boqNroPreviewText.textContent = "Data sheet BoQ NRO Cluster akan muncul setelah BOQ Excel dibuat.";
    renderSupportPanels();
    return;
  }
  els.boqNroPreviewBody.innerHTML = rows.map((row) => `
    <tr>
      ${columns.map((column) => `<td>${escapeHtml(row.values?.[column.key] ?? "-")}</td>`).join("")}
    </tr>
  `).join("");
  if (els.boqNroPreviewText) {
    els.boqNroPreviewText.textContent = `${rows.length.toLocaleString("id-ID")} row dari sheet BoQ NRO Cluster ditampilkan.`;
  }
  renderSupportPanels();
}

function hpdbFilterOptions() {
  const fdtOptions = Array.from({ length: Number(window.APP_CONFIG.hpdbMaxFdt || 10) }, (_, index) => {
    const number = index + 1;
    return `<option value="fdt_${number}">FDT ${number}</option>`;
  }).join("");
  return `
    <option value="all">All</option>
    <option value="cluster">CLUSTER</option>
    <option value="fat">FAT</option>
    <option value="homepass">HOMEPASS</option>
    <option value="hp_cover">HP COVER</option>
    <option value="hp_uncover">HP UNCOVER ignored</option>
    ${fdtOptions}
    <option value="warning">Warning</option>
    <option value="error">Error</option>
  `;
}

function syncHpdbFilterControls() {
  document.querySelectorAll(".hpdb-filter-select").forEach((select) => {
    if (!select.dataset.ready) {
      select.innerHTML = hpdbFilterOptions();
      select.dataset.ready = "1";
    }
    select.value = state.hpdbFilter;
  });
  document.querySelectorAll(".hpdb-search-input").forEach((input) => {
    input.value = state.hpdbSearch;
  });
}

function hpdbRowMatches(row, type) {
  const filter = state.hpdbFilter;
  if (filter === "all") return true;
  if (filter === type) return true;
  if (filter === "hp_cover") return type === "homepass";
  if (filter === "hp_uncover") return false;
  if (filter.startsWith("fdt_")) return Number(row.FDT || 0) === Number(filter.replace("fdt_", ""));
  const status = String(row.Status || "").toLowerCase();
  if (filter === "warning") return status === "warning" || status === "skipped" || status === "unmapped";
  if (filter === "error") return status === "error";
  return true;
}

function hpdbSearchMatches(row) {
  const query = state.hpdbSearch.trim().toLowerCase();
  if (!query) return true;
  return Object.values(row).some((value) => String(value ?? "").toLowerCase().includes(query));
}

function updateHpdbHomepassPager(filteredTotal) {
  const rowsPerPage = state.hpdbHomepassRowsPerPage;
  const pageCount = Math.max(1, Math.ceil(Number(filteredTotal || 0) / rowsPerPage));
  state.hpdbHomepassPage = Math.min(Math.max(1, state.hpdbHomepassPage), pageCount);
  if (els.hpdbHomepassPageText) {
    els.hpdbHomepassPageText.textContent = `Page ${formatNumber(state.hpdbHomepassPage)} of ${formatNumber(pageCount)} - ${formatNumber(rowsPerPage)} rows/page`;
  }
  if (els.hpdbHomepassPrevPage) els.hpdbHomepassPrevPage.disabled = state.hpdbHomepassPage <= 1;
  if (els.hpdbHomepassNextPage) els.hpdbHomepassNextPage.disabled = state.hpdbHomepassPage >= pageCount;
}

function renderHpdbTable(type, rows, body, text, columns, emptyText) {
  if (!body) return;
  const total = Number(state.hpdbPreview?.totals?.[type] ?? rows.length ?? 0);
  const filteredRows = (rows || []).filter((row) => hpdbRowMatches(row, type) && hpdbSearchMatches(row));
  if (!rows?.length) {
    body.innerHTML = `<tr><td colspan="${columns.length}" class="boq-empty-cell">${escapeHtml(emptyText)}</td></tr>`;
    if (text) text.textContent = emptyText;
    if (type === "homepass") updateHpdbHomepassPager(0);
    return;
  }
  if (!filteredRows.length) {
    body.innerHTML = `<tr><td colspan="${columns.length}" class="boq-empty-cell">Tidak ada HPDB row sesuai filter.</td></tr>`;
    if (text) text.textContent = `Tidak ada row sesuai filter dari ${formatNumber(total)} total rows.`;
    if (type === "homepass") updateHpdbHomepassPager(0);
    return;
  }

  let visibleRows = filteredRows;
  let pageStart = 1;
  let pageEnd = filteredRows.length;
  if (type === "homepass") {
    updateHpdbHomepassPager(filteredRows.length);
    const startIndex = (state.hpdbHomepassPage - 1) * state.hpdbHomepassRowsPerPage;
    visibleRows = filteredRows.slice(startIndex, startIndex + state.hpdbHomepassRowsPerPage);
    pageStart = startIndex + 1;
    pageEnd = startIndex + visibleRows.length;
  }

  body.innerHTML = visibleRows.map((row) => `
    <tr>
      ${columns.map((column) => `<td>${escapeHtml(row[column] ?? "-")}</td>`).join("")}
    </tr>
  `).join("");
  if (text) {
    text.textContent = type === "homepass"
      ? `Showing rows ${formatNumber(pageStart)}-${formatNumber(pageEnd)} of ${formatNumber(filteredRows.length)} filtered rows, ${formatNumber(total)} total rows.`
      : `Showing ${formatNumber(filteredRows.length)} of ${formatNumber(total)} total rows.`;
  }
}

function renderHpdbPreviews() {
  syncHpdbFilterControls();
  const preview = state.hpdbPreview || { cluster: [], fat: [], homepass: [], totals: {} };
  renderHpdbTable(
    "cluster",
    preview.cluster || [],
    els.hpdbClusterPreviewBody,
    els.hpdbClusterPreviewText,
    ["FDT", "FDT Code", "Clustername", "Latitude", "Longitude", "Status"],
    "Belum ada HPDB CLUSTER row yang bisa ditampilkan.",
  );
  renderHpdbTable(
    "fat",
    preview.fat || [],
    els.hpdbFatPreviewBody,
    els.hpdbFatPreviewText,
    ["FDT", "FDT Code", "Clustername", "FAT Code", "FAT Latitude", "FAT Longitude", "Status"],
    "Belum ada HPDB FAT row yang bisa ditampilkan.",
  );
  renderHpdbTable(
    "homepass",
    preview.homepass || [],
    els.hpdbHomepassPreviewBody,
    els.hpdbHomepassPreviewText,
    ["FDT", "FDT Code", "Clustername", "FAT Code", "Homepass Name", "Latitude_homepass", "Longitude_homepass", "Status"],
    "Belum ada HPDB HOMEPASS row yang bisa ditampilkan.",
  );
  renderSupportPanels();
}

function setOutputMode(mode) {
  // Preserve an explicitly empty selection (no output chosen). Only resolve
  // via modeFromTargets when there is at least one target.
  if (mode === "" || mode == null) {
    state.outputMode = "";
  } else {
    const targets = Array.from(outputTargetsFromMode(mode));
    state.outputMode = targets.length ? modeFromTargets(targets) : "";
  }
  syncOutputTargetInputs();
  updateBoqPanel();
  updateHpdbPanel();
  setButtonTrailingText(els.convertButton, generateButtonLabel());
  renderKmlFolderSelector();
  renderSupportPanels();
}

function setNetworkType(type) {
  state.networkType = normalizeNetworkType(type);
  syncNetworkTypeInputs();
  syncOutputTargetInputs();
  updateBoqPanel();
  updateHpdbPanel();
  setOutputMode(state.outputMode);
}

async function extractKmlFolders(files) {
  if (!files.length) {
    state.kmlFolders = [];
    renderKmlFolderSelector();
    return;
  }

  const folders = new Map(standardKmlFolders.map((folderName) => [
    folderName,
    { key: folderName, path: folderName, count: 0, fileCount: 0, exists: false, selected: false },
  ]));

  for (const file of files) {
    if (!isKmlFile(file)) continue;

    const kmlText = await readKmlText(file);
    if (!kmlText) continue;

    const root = parseKmlDocument(kmlText, file.name);

    const fileFolders = new Map(standardKmlFolders.map((folderName) => [
      folderName,
      { count: 0, exists: false },
    ]));
    collectKmlFolders(root, [], fileFolders);

    fileFolders.forEach((folder, folderName) => {
      if (!folder.exists) return;
      const aggregate = folders.get(folderName);
      aggregate.exists = true;
      aggregate.count += folder.count;
      if (folder.count) aggregate.fileCount += 1;
    });
  }

  state.kmlFolders = standardKmlFolders
    .map((folderName) => folders.get(folderName))
    .map((folder) => ({ ...folder, selected: Number(folder.count || 0) > 0 }));
  renderKmlFolderSelector();
}

async function readKmlText(file) {
  const name = file.name.toLowerCase();
  if (name.endsWith(".kml")) {
    return file.text();
  }

  if (!window.JSZip) {
    throw new Error("Library pembaca KMZ belum termuat. Coba refresh halaman.");
  }

  let archive;
  try {
    archive = await window.JSZip.loadAsync(file);
  } catch (_error) {
    throw new Error(`${file.name}: KMZ kosong/korup atau belum selesai terunduh. Coba upload ulang file KMZ asli.`);
  }
  const kmlEntries = Object.keys(archive.files).filter((entryName) => entryName.toLowerCase().endsWith(".kml"));
  if (!kmlEntries.length) {
    throw new Error(`${file.name}: KMZ tidak berisi file KML.`);
  }
  const preferredEntry = kmlEntries.find((entryName) => entryName.split(/[\\/]/).pop().toLowerCase() === "doc.kml") || kmlEntries[0];
  return archive.file(preferredEntry).async("text");
}

function sanitizeKmlText(value) {
  let text = String(value)
    .replace(/^\uFEFF/, "")
    .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, "")
    .replace(/&(?!#\d+;|#x[0-9a-fA-F]+;|[a-zA-Z][\w.-]*;)/g, "&amp;");
  if (text.includes("ns1:") && !text.includes("xmlns:ns1=")) {
    text = text.replace(/<kml(\s|>)/, '<kml xmlns:ns1="http://www.w3.org/2005/Atom"$1');
  }
  return text;
}

function parseKmlDocument(kmlText, fileName) {
  const parser = new DOMParser();
  const strictXml = parser.parseFromString(kmlText, "application/xml");
  if (!strictXml.querySelector("parsererror")) {
    return strictXml.documentElement;
  }

  const repairedXml = parser.parseFromString(sanitizeKmlText(kmlText), "application/xml");
  if (!repairedXml.querySelector("parsererror")) {
    return repairedXml.documentElement;
  }

  const looseDocument = parser.parseFromString(kmlText, "text/html");
  const looseRoot = looseDocument.querySelector("kml") || looseDocument.body;
  if (looseRoot && looseRoot.querySelector("folder")) {
    return looseRoot;
  }

  throw new Error(`${fileName}: KML tidak valid atau struktur folder tidak bisa dibaca.`);
}

function localName(node) {
  return (node.localName || node.nodeName.split(":").pop()).toLowerCase();
}

function directChild(element, name) {
  return Array.from(element.children).find((child) => localName(child) === name.toLowerCase());
}

function directChildText(element, name) {
  const child = directChild(element, name);
  return child?.textContent?.trim() || "";
}

function normalizeKmlFolderName(value) {
  return evaUtils.normalizeKmlFolderName
    ? evaUtils.normalizeKmlFolderName(value)
    : String(value || "").trim().replace(/\s+/g, " ").toUpperCase();
}

function canonicalStandardFolder(folderName) {
  const canonical = kmlFolderAliases.get(evaUtils.normalizeKmlFolderName(folderName));
  if (!canonical || !standardKmlFolderSet.has(canonical)) return "";
  if (isFeederLikeScope(state.scopeType) && canonical === "DISTRIBUTION CABLE") return "CABLE";
  return canonical;
}

function nearestStandardFolder(folderStack) {
  for (let index = folderStack.length - 1; index >= 0; index -= 1) {
    const canonical = canonicalStandardFolder(folderStack[index]);
    if (canonical) {
      return canonical;
    }
  }
  return "";
}

function collectKmlFolders(element, folderStack, folders) {
  Array.from(element.children).forEach((child) => {
    const childName = localName(child);
    if (childName === "document") {
      collectKmlFolders(child, folderStack, folders);
    } else if (childName === "folder") {
      const folderName = directChildText(child, "name") || "Tanpa Nama";
      const canonicalFolderName = canonicalStandardFolder(folderName);
      if (folders.has(canonicalFolderName)) {
        folders.get(canonicalFolderName).exists = true;
      }
      collectKmlFolders(child, [...folderStack, folderName], folders);
    } else if (childName === "placemark") {
      const folderName = nearestStandardFolder(folderStack);
      if (folders.has(folderName)) {
        folders.get(folderName).count += 1;
      }
    }
  });
}

const INLINE_FEEDBACK_DURATION_MS = 5000;
const inlineFeedbackTimers = new WeakMap();

function clearInlineFeedbackTimer(root) {
  const timer = inlineFeedbackTimers.get(root);
  if (timer) window.clearTimeout(timer);
  inlineFeedbackTimers.delete(root);
}

function dismissInlineFeedback(root) {
  if (!root) return;
  clearInlineFeedbackTimer(root);
  root.classList.add("hidden");
  root.removeAttribute("role");
}

function scheduleInlineFeedbackDismiss(root) {
  clearInlineFeedbackTimer(root);
  inlineFeedbackTimers.set(root, window.setTimeout(() => dismissInlineFeedback(root), INLINE_FEEDBACK_DURATION_MS));
}

function renderInlineFeedback(root, title, message, tone = "info") {
  if (!root) return null;
  const normalized = ["success", "info", "warning", "error"].includes(tone) ? tone : "info";
  let titleNode = root.querySelector("[data-inline-title]");
  let messageNode = root.querySelector("[data-inline-message]");
  if (!titleNode || !messageNode) {
    root.replaceChildren();
    titleNode = document.createElement("strong");
    titleNode.dataset.inlineTitle = "";
    messageNode = document.createElement("span");
    messageNode.dataset.inlineMessage = "";
    root.append(titleNode, messageNode);
  }
  root.querySelector("[data-inline-close]")?.remove();
  titleNode.textContent = title || "Info";
  messageNode.textContent = message || "";
  root.dataset.tone = normalized;
  root.classList.remove("hidden");
  root.setAttribute("role", normalized === "error" || normalized === "warning" ? "alert" : "status");
  scheduleInlineFeedbackDismiss(root);
  return root;
}

function setInlineFeedback(title, message, tone = "info", targetId = "inlineFeedback") {
  return renderInlineFeedback(document.getElementById(targetId), title, message, tone);
}

let astriInlineFeedbackSequence = 0;
function clearInlineFeedback(targetId = "inlineFeedback") {
  const root = document.getElementById(targetId);
  if (!root) return;
  dismissInlineFeedback(root);
}

function setAstriInlineFeedback(title, message, tone = "info") {
  astriInlineFeedbackSequence += 1;
  let root = document.getElementById("astriInlineFeedback");
  if (!root) {
    const panel = document.getElementById("astriIntegrationPanel");
    if (!panel) return null;
    root = document.createElement("div");
    root.id = "astriInlineFeedback";
    root.className = "inline-feedback mt-3 hidden";
    const anchor = document.getElementById("astriActionButtons") || document.getElementById("astriMatchBox");
    if (anchor) anchor.insertAdjacentElement("afterend", root);
    else panel.appendChild(root);
  }
  root.dataset.inlineProgressToken = "";
  return renderInlineFeedback(root, title, message, tone);
}

function showAstriInlineProgress(title, message, persistent = false) {
  const root = setAstriInlineFeedback(title, message, "info");
  if (!root) return null;
  if (persistent) clearInlineFeedbackTimer(root);
  const token = String(++astriInlineFeedbackSequence);
  root.dataset.inlineProgressToken = token;
  return {
    remove() {
      if (root.dataset.inlineProgressToken !== token) return;
      dismissInlineFeedback(root);
    },
  };
}

window.evaSetInlineFeedback = setInlineFeedback;
window.evaSetAstriInlineFeedback = setAstriInlineFeedback;
window.evaClearInlineFeedback = clearInlineFeedback;

function setError(message, scope = "validation") {
  try { void state; } catch (_) {
    const alert = document.getElementById("errorAlert");
    if (alert) {
      alert.textContent = message || "";
      alert.classList.toggle("hidden", !message);
      if (message) alert.classList.add("alert-error");
    }
    const bar = document.getElementById("retryBar");
    if (bar && scope === "job") bar.classList.remove("hidden");
    return;
  }
  const hasMessage = Boolean(message);
  const importantError = scope === "job" || scope === "init";
  state.hasError = hasMessage;
  state.errorScope = hasMessage ? scope : "none";
  if (els.errorAlert) {
    els.errorAlert.textContent = message || "";
    els.errorAlert.classList.toggle("hidden", !hasMessage || importantError);
    if (hasMessage && !importantError) els.errorAlert.setAttribute("role", "alert");
    else els.errorAlert.removeAttribute("role");
  }
  if (hasMessage && importantError && typeof window.evaToast === "function") {
    window.evaToast({ title: "Ada masalah", message, tone: "error", important: true, dedupeKey: `err:${message}` });
  }
  renderSupportPanels();
}

function clearError() {
  state.hasError = false;
  state.errorScope = "none";
  els.errorAlert.textContent = "";
  els.errorAlert.classList.add("hidden");
  window.evaClearInlineFeedback?.();
  renderSupportPanels();
}

function updateCancelButton() {
  if (!els.cancelJobButton) return;
  const canCancel = Boolean(state.jobId) && state.currentJobStatus === "processing" && state.currentStatusTone === "processing";
  els.cancelJobButton.classList.toggle("hidden", !canCancel);
  els.cancelJobButton.disabled = !canCancel || state.cancelInFlight;
}

function setStatus(label, message, tone = "idle") {
  try { void state; } catch (_) {
    const statusBadge = document.getElementById("statusBadge");
    const statusText = document.getElementById("statusText");
    if (statusBadge) statusBadge.textContent = label || "";
    if (statusText) statusText.textContent = message || "";
    return;
  }
  state.currentStatusLabel = label;
  state.currentStatusMessage = message;
  state.currentStatusTone = tone;
  state.errorScope = tone === "error" ? (state.hasUserStartedProcess || state.jobId ? "job" : "init") : tone === "warning" ? "validation" : state.errorScope;
  const displayLabel = tone === "done"
    ? "Selesai"
    : tone === "error"
      ? "Gagal"
      : tone === "processing"
        ? (/retry/i.test(label || message || "") ? "Mencoba ulang" : /valid/i.test(label || message || "") ? "Memvalidasi" : "Memproses")
        : tone === "warning"
          ? (label || "Perlu dicek")
        : selectedFiles().length
          ? "Siap"
          : "Menunggu hasil";
  els.statusText.textContent = message;
  els.statusBadge.textContent = displayLabel;
  els.statusBadge.className = "status-pill";
  els.statusBadge.dataset.tone = tone;
  if (els.dashboardStatusPill) {
    els.dashboardStatusPill.textContent = displayLabel;
    els.dashboardStatusPill.dataset.tone = tone;
  }
  updateCancelButton();
  renderSupportPanels();
}

function setProgress(value) {
  const safeValue = Math.max(0, Math.min(100, Number(value) || 0));
  state.progress = safeValue;
  els.progressValue.textContent = `${safeValue}%`;
  els.progressBar.style.width = `${safeValue}%`;
  renderSupportPanels();
}

function setStats(stats = {}) {
  state.stats = { ...stats };
  layerKeys.forEach((key) => {
    const node = document.getElementById(`stat-${key}`);
    if (node) node.textContent = Number(stats[key] || 0).toLocaleString("id-ID");
  });
  renderDxfEmptyState();
  renderSupportPanels();
}

function hideDownloadButton() {
  state.downloadUrl = null;
  state.downloadMimeType = null;
  state.downloadMode = state.outputMode;
  state.defaultDownloadName = state.downloadName;
  setValidationResultCollapsed(true);
  els.downloadButton.classList.add("hidden");
  els.downloadButton.classList.remove("inline-flex");
  els.downloadButton.removeAttribute("href");
  els.downloadButton.removeAttribute("download");
  els.downloadPanel.classList.add("hidden");
  renderSupportPanels();
}

function sanitizeOutputFileName(value, mode = state.downloadMode || state.outputMode) {
  const extension = outputExtension(mode);
  const clean = String(value || "")
    .replace(/[<>:"/\\|?*\x00-\x1F]+/g, "_")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[.\s_]+|[.\s_]+$/g, "");
  const fallback = state.downloadName || `eva-vector-output${extension}`;
  const base = clean || fallback;
  return base.toLowerCase().endsWith(extension) ? base : `${base.replace(/\.[^.]+$/, "")}${extension}`;
}

function showDownloadPanel(downloadUrl, downloadName, downloadMimeType = "application/octet-stream", mode = state.outputMode) {
  state.downloadUrl = downloadUrl;
  state.downloadMode = mode;
  state.downloadMimeType = downloadMimeType;
  state.downloadName = sanitizeOutputFileName(downloadName || state.downloadName, mode);
  state.defaultDownloadName = state.downloadName;
  if (state.currentStatusTone !== "done") {
    setStatus("Selesai", "Output siap diunduh.", "done");
  }
  els.outputFileLabel.textContent = mode === "kmz"
    ? "Nama file KMZ standar"
    : mode === "dxf"
    ? `Nama file DXF ${state.networkType}`
    : mode === "boq"
      ? `Nama file BOQ ${state.networkType}`
      : mode === "astri"
        ? `Nama file ASTRI ${state.networkType}${state.downloadMimeType === "application/zip" ? " (ZIP per FDT)" : ""}`
        : mode === "hpdb"
          ? `Nama file HPDB ${state.networkType}`
          : `Nama file ZIP ${state.networkType}`;
  els.outputFileName.value = state.downloadName;
  els.downloadButton.href = downloadUrl;
  els.downloadButton.download = state.downloadName;
  setButtonTrailingText(els.downloadButton, outputDownloadText(mode));
  if (els.downloadResultText) els.downloadResultText.textContent = `Durasi ${state.durationLabel || "--"}`;
  els.downloadPanel.classList.remove("hidden");
  els.downloadButton.classList.remove("hidden");
  els.downloadButton.classList.add("inline-flex");
  renderSupportPanels();
  notifyEvaAssistant("download-ready", {
    key: `download-ready:${state.downloadName}`,
    downloadUrl,
  });
}

function fallbackBlobDownload(blob, filename) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(link.href), 60000);
}

async function saveOutputToLocal(event) {
  event.preventDefault();
  if (!state.downloadUrl) return;
  finishOutputRename();
  const filename = els.outputFileName.value;
  logger.info(`Preparing download: ${filename}`);

  // Server Content-Disposition wins over the anchor name. Only renamed files
  // need a browser-side blob; the original keeps its direct/streaming download.
  try {
    if (filename === state.defaultDownloadName) {
      const link = document.createElement("a");
      link.href = state.downloadUrl;
      link.download = filename;
      link.rel = "noopener";
      document.body.appendChild(link);
      link.click();
      link.remove();
    } else {
      const response = await fetch(state.downloadUrl, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      fallbackBlobDownload(await response.blob(), filename);
    }
    logger.success(`Download started: ${filename}`);
  } catch (err) {
    logger.error(`Download failed: ${err.message}`);
    setError(err.message, "job");
  }
}

function formatFileSize(bytes) {
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function validateFiles(files) {
  if (!files.length) return "Pilih minimal satu file terlebih dahulu.";
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  if (totalBytes > window.APP_CONFIG.maxBatchUploadBytes) {
    return `Total upload maksimal ${window.APP_CONFIG.maxBatchUploadMb}MB.`;
  }

  for (const file of files) {
    const error = validateFile(file);
    if (error) return `${file.name}: ${error}`;
  }

  return null;
}

function validateFile(file, kind = "any") {
  if (!file) return "File tidak valid.";
  if (kind === "osm" && !isOsmFile(file)) return "Data ROAD memakai server OSM; unggah file desain KML/KMZ saja.";
  if (kind === "kml" && !isKmlFile(file)) return "Format file tidak didukung di kolom KML/KMZ.";
  if (kind === "any" && !isKmlFile(file)) return "Format file tidak didukung. Gunakan .kml atau .kmz untuk file desain.";
  if (file.size > window.APP_CONFIG.maxUploadBytes) return `Ukuran tiap file maksimal ${window.APP_CONFIG.maxUploadMb}MB.`;
  return null;
}

function renderSelectedFiles() {
  const files = selectedFiles();
  renderUploadFileSummary("osm");
  renderUploadFileSummary("kml");
  if (els.fileList) {
    els.fileList.classList.add("hidden");
    els.fileList.innerHTML = "";
  }
  if (!files.length) {
    if (els.previewText) {
      els.previewText.textContent = "Preview akan muncul setelah file berhasil divalidasi. Map menampilkan ROAD, KML features, dan area cluster.";
    }
    updateBoqPanel();
    renderSupportPanels();
    return;
  }

  updateBoqPanel();
  if (els.previewText && !state.downloadUrl) {
    els.previewText.textContent = "Preview akan muncul setelah file berhasil divalidasi. BOQ / HPDB / DXF preview aktif sesuai output yang dipilih.";
  }
  renderSupportPanels();
}

async function selectFiles(kind, fileList) {
  clearError();
  if (kind === "osm") {
    state.osmFiles = [];
    state.osmValidationState = "empty";
    if (els.osmFileInput) els.osmFileInput.value = "";
    setError("Data ROAD diambil dari server OSM. Unggah hanya file KML/KMZ untuk desain.");
    window.evaRenderRoadSourcePanel?.();
    return;
  }
  const files = Array.from(fileList || []);
  const categoryError = files.map((file) => validateFile(file, kind)).find(Boolean);
  const nextOsmFiles = kind === "osm" ? files : state.osmFiles;
  const nextKmlFiles = kind === "kml" ? files : state.kmlFiles;
  const nextFiles = [...nextOsmFiles, ...nextKmlFiles];
  const error = categoryError || validateFiles(nextFiles);
  if (error) {
    if (kind === "osm") {
      state.osmFiles = [];
      state.osmValidationState = "invalid";
      els.osmFileInput.value = "";
    } else {
      state.kmlFiles = [];
      state.kmlValidationState = "invalid";
      els.kmlFileInput.value = "";
      state.kmlFolders = [];
    }
    state.kmlValidationError = null;
    renderSelectedFiles();
    renderKmlFolderSelector();
    setError(error);
    return;
  }

  state.osmFiles = nextOsmFiles;
  state.kmlFiles = nextKmlFiles;
  state.kmlValidationError = null;
  state.osmValidationState = state.osmFiles.length ? "ready" : "empty";
  state.kmlValidationState = state.kmlFiles.length ? "validating" : "empty";
  window.evaRenderRoadSourcePanel?.();

  // Auto-detect scope type from filename:
  // - "SF" marker means Subfeeder.
  // - F + 3 area letters, optionally followed by digits (e.g. FBKS0150), means Feeder.
  if (kind === "kml" && nextKmlFiles.length) {
    const hasSubfeederFile = nextKmlFiles.some((file) => /\bSF\b/i.test(file.name));
    const hasFeederFile = nextKmlFiles.some((file) => /(^|[^A-Z0-9])F[A-Z]{3}\d*(?=[^A-Z0-9]|$)/i.test(file.name));
    const detectedScope = hasSubfeederFile ? "Subfeeder" : hasFeederFile ? "Feeder" : "Cluster";
    if (state.scopeType !== detectedScope) {
      state.scopeType = detectedScope;
      syncScopeTypeInputs();
      syncOutputTargetInputs();
      if (typeof window._syncAstriTargetTypeFromScope === "function") window._syncAstriTargetTypeFromScope();
    }
  }

  renderSelectedFiles();
  try {
    await extractKmlFolders(state.kmlFiles);
    state.kmlValidationState = state.kmlFiles.length ? "valid" : "empty";
    renderSelectedFiles();
    if (kind === "kml" && state.kmlFiles.length) {
      notifyEvaAssistant("kml-uploaded", {
        key: `kml-uploaded:${state.kmlFiles.map((file) => file.name).join("|")}`,
      });
    }
  } catch (err) {
    state.kmlFolders = [];
    state.kmlValidationError = err.message;
    state.kmlValidationState = "invalid";
    renderSelectedFiles();
    renderKmlFolderSelector();
    setError(err.message);
  }
}

function clearSelectedFiles(kind) {
  if (kind === "osm") {
    state.osmFiles = [];
    state.osmValidationState = "empty";
    if (els.osmFileInput) els.osmFileInput.value = "";
  } else if (kind === "kml") {
    state.kmlFiles = [];
    state.kmlFolders = [];
    state.kmlValidationError = null;
    state.kmlValidationState = "empty";
    if (els.kmlFileInput) els.kmlFileInput.value = "";
    renderKmlFolderSelector();
  }
  clearError();
  renderSelectedFiles();
  window.evaRenderRoadSourcePanel?.();
}

function replaceSelectedFiles(kind) {
  const input = kind === "osm" ? els.osmFileInput : els.kmlFileInput;
  input?.click();
}

function resetCurrentSession(options = {}) {
  const force = options?.force === true;
  if (!force && state.currentStatusTone === "processing") {
    window.evaToast?.({
      title: "Proses masih berjalan",
      message: "Tunggu proses selesai sebelum reset tampilan.",
      tone: "warning",
    });
    return;
  }
  if (state.pollingTimer) {
    clearInterval(state.pollingTimer);
    state.pollingTimer = null;
  }
  state.osmFiles = [];
  state.kmlFiles = [];
  state.kmlFolders = [];
  state.kmlValidationError = null;
  state.osmValidationState = "empty";
  state.kmlValidationState = "empty";
  window.clearAstriMatchContext();
  window.setCurrentJobId(null);
  state.currentJobStatus = null;
  state.cancelInFlight = false;
  state.downloadUrl = null;
  state.downloadName = defaultOutputMode === "dxf" ? "eva-vector-output.dxf" : "eva-vector-output.xlsx";
  state.downloadMode = state.outputMode;
  state.progress = 0;
  state.stats = {};
  state.durationLabel = "--";
  state.hasError = false;
  state.hasUserStartedProcess = false;
  state.standardizerSummary = null;
  state.mapLayerVisibility = {};
  if (state.geoJsonLayer) {
    map.removeLayer(state.geoJsonLayer);
    state.geoJsonLayer = null;
  }
  if (els.osmFileInput) els.osmFileInput.value = "";
  if (els.kmlFileInput) els.kmlFileInput.value = "";
  clearConsoleLogs();
  resetBoqResultState();
  resetHpdbResultState();
  renderStandardizerSummary();
  renderKmlFolderSelector();
  renderSelectedFiles();
  window.evaRenderRoadSourcePanel?.();
  hideDownloadButton();
  setProgress(0);
  setStats();
  clearError();
  setStatus("Belum ada proses", "Unggah file KML/KMZ, pilih wilayah ROAD dari server OSM, lalu klik Mulai Proses.", "idle");
  setPreviewTab("summary");
  setMapLocked(true);
  window.evaToast?.({ title: "Tampilan di-reset", message: "Input dan preview sudah dibersihkan.", tone: "info" });
}

function resetGeneratedOutput(options = {}) {
  const force = options?.force === true;
  if (!force && state.currentStatusTone === "processing") {
    window.evaToast?.({
      title: "Proses masih berjalan",
      message: "Tunggu proses selesai sebelum reset hasil.",
      tone: "warning",
    });
    return;
  }
  if (state.pollingTimer) {
    clearInterval(state.pollingTimer);
    state.pollingTimer = null;
  }
  window.clearAstriMatchContext();
  window.setCurrentJobId(null);
  state.currentJobStatus = null;
  state.cancelInFlight = false;
  state.downloadUrl = null;
  state.downloadName = defaultOutputMode === "dxf" ? "eva-vector-output.dxf" : "eva-vector-output.xlsx";
  state.defaultDownloadName = state.downloadName;
  state.downloadMode = state.outputMode;
  state.progress = 0;
  state.stats = {};
  state.durationLabel = "--";
  state.hasError = false;
  state.hasUserStartedProcess = false;
  state.standardizerSummary = null;
  state.mapLayerVisibility = {};
  if (state.geoJsonLayer) {
    map.removeLayer(state.geoJsonLayer);
    state.geoJsonLayer = null;
  }
  clearConsoleLogs();
  resetBoqResultState();
  resetHpdbResultState();
  renderStandardizerSummary();
  renderSelectedFiles();
  hideDownloadButton();
  setProgress(0);
  setStats();
  clearError();
  setStatus("Siap", "File tetap dipilih. Klik Mulai Proses untuk menjalankan ulang.", "idle");
  setPreviewTab("summary");
  setMapLocked(true);
  renderValidationSummary();
  window.evaToast?.({ title: "Hasil di-reset", message: "File dan pilihan output tetap dipakai untuk generate ulang.", tone: "info" });
}

async function parseJsonResponse(response) {
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    payload = {};
  }
  if (!response.ok) {
    const error = new Error(payload.error || "Request gagal diproses.");
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function isAstriBoqExistingSubmissionError(error) {
  const payload = error?.payload || {};
  const code = String(payload.code || error?.code || "").toUpperCase();
  return Number(error?.status) === 409 && (
    Boolean(payload.submission_id)
    || code === "ASTRI_BOQ_SUBMISSION_CONFLICT"
    || code === "ASTRI_BOQ_SUBMISSION_IN_PROGRESS"
  );
}

function postFormDataWithProgress(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.responseType = "json";
    xhr.timeout = 90000;
    onProgress?.(2);
    logger.info("Upload request dibuka. Mengirim file ke server...");
    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) {
        onProgress?.(6);
        return;
      }
      const uploadPercent = Math.round((event.loaded / event.total) * 8);
      onProgress?.(Math.max(1, Math.min(8, uploadPercent)));
    });
    xhr.upload.addEventListener("load", () => {
      onProgress?.(8);
      logger.info("Upload file selesai. Menunggu job dibuat server...");
    });
    xhr.addEventListener("load", () => {
      const payload = xhr.response || {};
      if (xhr.status >= 200 && xhr.status < 300) {
        logger.success("Server menerima upload dan mengembalikan job.");
        resolve(payload);
      } else {
        reject(new Error(payload.error || "Request gagal diproses."));
      }
    });
    xhr.addEventListener("error", () => reject(new Error("Upload gagal terkirim ke server.")));
    xhr.addEventListener("timeout", () => reject(new Error("Upload timeout: server tidak merespons dalam 90 detik.")));
    xhr.send(formData);
  });
}


const locationPopupState = {
  resolve: null,
  reject: null,
  value: { province: "", city: "", district: "", subdistrict: "", postal_code: "", area_name: "" },
  requestId: 0,
};

async function fetchLocationJson(url) {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`Gagal ambil data wilayah (${res.status})`);
  return await res.json();
}

function ensureLocationPopup() {
  if (document.getElementById('eva-location-popup')) return;
  const wrapper = document.createElement('div');
  wrapper.id = 'eva-location-popup';
  wrapper.className = 'fixed inset-0 z-[9999] hidden items-center justify-center bg-emerald-100/55 p-4 backdrop-blur-sm';
  wrapper.innerHTML = `
    <div class="w-full max-w-3xl rounded-[24px] border border-emerald-200/80 bg-emerald-50/92 p-5 text-emerald-950 shadow-[0_24px_80px_rgba(16,185,129,0.18)] backdrop-blur-xl ring-1 ring-white/70">
      <div class="mb-3 flex items-center justify-between gap-3">
        <div><div class="text-lg font-black tracking-tight text-emerald-950">Pilih Wilayah</div><div class="text-xs font-semibold text-emerald-700">Dipakai untuk HPDB / ASTRI</div></div>
        <button type="button" data-location-close class="rounded-xl border border-emerald-200/80 bg-white/88 px-3 py-2 text-xs font-bold text-emerald-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_10px_24px_rgba(16,185,129,0.12)] backdrop-blur-md hover:bg-white">Tutup</button>
      </div>
      <div class="grid gap-3 md:grid-cols-2 text-emerald-900">
        <label class="grid gap-1 text-xs"><span>Province</span><select data-location-field="province" class="rounded-xl border border-emerald-200/80 bg-white px-3 py-2 text-sm font-bold text-emerald-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_10px_24px_rgba(16,185,129,0.08)] outline-none  transition focus:border-emerald-400 focus:bg-white focus:ring-2 focus:ring-emerald-200/80"></select></label>
        <label class="grid gap-1 text-xs"><span>City / Regency</span><select data-location-field="city" class="rounded-xl border border-emerald-200/80 bg-white px-3 py-2 text-sm font-bold text-emerald-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_10px_24px_rgba(16,185,129,0.08)] outline-none  transition focus:border-emerald-400 focus:bg-white focus:ring-2 focus:ring-emerald-200/80"></select></label>
        <label class="grid gap-1 text-xs"><span>District</span><select data-location-field="district" class="rounded-xl border border-emerald-200/80 bg-white px-3 py-2 text-sm font-bold text-emerald-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_10px_24px_rgba(16,185,129,0.08)] outline-none  transition focus:border-emerald-400 focus:bg-white focus:ring-2 focus:ring-emerald-200/80"></select></label>
        <label class="grid gap-1 text-xs"><span>Subdistrict</span><select data-location-field="subdistrict" class="rounded-xl border border-emerald-200/80 bg-white px-3 py-2 text-sm font-bold text-emerald-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_10px_24px_rgba(16,185,129,0.08)] outline-none  transition focus:border-emerald-400 focus:bg-white focus:ring-2 focus:ring-emerald-200/80"></select></label>
        <label class="grid gap-1 text-xs"><span>Postal Code</span><select data-location-field="postal_code" class="rounded-xl border border-emerald-200/80 bg-white px-3 py-2 text-sm font-bold text-emerald-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_10px_24px_rgba(16,185,129,0.08)] outline-none  transition focus:border-emerald-400 focus:bg-white focus:ring-2 focus:ring-emerald-200/80"></select></label>
        <label class="grid gap-1 text-xs"><span>Area Name ASTRI</span><select data-location-field="area_name" class="rounded-xl border border-emerald-200/80 bg-white px-3 py-2 text-sm font-bold text-emerald-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.95),0_10px_24px_rgba(16,185,129,0.08)] outline-none  transition focus:border-emerald-400 focus:bg-white focus:ring-2 focus:ring-emerald-200/80"></select></label>
      </div>
      <div class="mt-4 flex justify-end gap-2">
        <button type="button" data-location-cancel class="rounded-xl border border-emerald-200/80 bg-white/88 px-4 py-2 text-xs font-bold text-emerald-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_10px_24px_rgba(16,185,129,0.12)] backdrop-blur-md hover:bg-white">Batal</button>
        <button type="button" data-location-submit class="rounded-xl border border-emerald-400/60 bg-gradient-to-b from-emerald-500/90 to-teal-600/90 px-4 py-2 text-xs font-black text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.45),0_14px_32px_rgba(16,185,129,0.28)] hover:from-emerald-500 hover:to-teal-600">Pakai</button>
      </div>
    </div>`;
  document.body.appendChild(wrapper);
  wrapper.addEventListener('click', (event) => {
    if (event.target === wrapper || event.target.closest('[data-location-cancel]') || event.target.closest('[data-location-close]')) closeLocationPopup(false);
  });
  wrapper.querySelector('[data-location-submit]')?.addEventListener('click', () => closeLocationPopup(true));
  wrapper.addEventListener('change', handleLocationChange);
}

function locSelect(key) { return document.querySelector(`#eva-location-popup [data-location-field="${key}"]`); }
function optValue(item, key) { return typeof item === 'string' ? item : (item?.[key] || item?.value || item?.name || item?.label || ''); }
function normalizeLocationDisplayValue(key, value) {
  const raw = String(value || '').trim();
  if (key !== 'city') return raw;
  return raw.replace(/^Kabupaten\s+/i, 'Kab. ').replace(/^Kota\s+/i, '');
}

function fillSelect(key, rows, rowKey, selected) {
  const el = locSelect(key); if (!el) return;
  const seen = new Set();
  const options = ['<option value="">-- pilih --</option>'];
  const normalizedSelected = normalizeLocationDisplayValue(key, selected);
  rows.forEach((row) => {
    const rawValue = String(optValue(row, rowKey) || '').trim();
    const value = normalizeLocationDisplayValue(key, rawValue);
    if (!value || seen.has(value)) return;
    seen.add(value);
    options.push(`<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`);
  });
  el.innerHTML = options.join('');
  if (normalizedSelected && seen.has(normalizedSelected)) el.value = normalizedSelected;
}

async function refreshLocationDropdowns() {
  const v = locationPopupState.value;
  const requestId = locationPopupState.requestId;
  const [provinces, cities, districts, subdistricts, postals, areas] = await Promise.all([
    fetchLocationJson('/api/regions/provinces?limit=1000'),
    fetchLocationJson(`/api/regions/cities?limit=1000${v.province ? `&province=${encodeURIComponent(v.province)}` : ''}`),
    fetchLocationJson(`/api/regions/districts?limit=2000${v.province ? `&province=${encodeURIComponent(v.province)}` : ''}${v.city ? `&city=${encodeURIComponent(v.city)}` : ''}`),
    fetchLocationJson(`/api/regions/subdistricts?limit=3000${v.province ? `&province=${encodeURIComponent(v.province)}` : ''}${v.city ? `&city=${encodeURIComponent(v.city)}` : ''}${v.district ? `&district=${encodeURIComponent(v.district)}` : ''}`),
    fetchLocationJson(`/api/regions/postal-codes?limit=1000${v.province ? `&province=${encodeURIComponent(v.province)}` : ''}${v.city ? `&city=${encodeURIComponent(v.city)}` : ''}${v.district ? `&district=${encodeURIComponent(v.district)}` : ''}${v.subdistrict ? `&subdistrict=${encodeURIComponent(v.subdistrict)}` : ''}`),
    fetchLocationJson('/api/astri/areas?limit=1000'),
  ]);
  if (requestId !== locationPopupState.requestId) return;
  fillSelect('province', provinces, 'province', v.province);
  fillSelect('city', v.province ? cities : [], 'city', v.city);
  fillSelect('district', v.city ? districts : [], 'district', v.district);
  fillSelect('subdistrict', v.district ? subdistricts : [], 'subdistrict', v.subdistrict);
  fillSelect('postal_code', v.subdistrict ? postals : [], 'postal_code', v.postal_code);
  fillSelect('area_name', v.city ? areas : [], 'area_name', v.area_name);
  const lock = {
    city: !v.province, district: !v.city, subdistrict: !v.district, postal_code: !v.subdistrict, area_name: !v.city,
  };
  ['city','district','subdistrict','postal_code','area_name'].forEach((key) => {
    const el = locSelect(key); if (!el) return;
    el.disabled = !!lock[key];
    el.classList.toggle('opacity-50', !!lock[key]);
    el.classList.toggle('cursor-not-allowed', !!lock[key]);
  });
}

async function handleLocationChange(event) {
  const key = event.target?.dataset?.locationField; if (!key) return;
  locationPopupState.value[key] = event.target.value;
  if (key === 'province') Object.assign(locationPopupState.value, { city: '', district: '', subdistrict: '', postal_code: '', area_name: '' });
  if (key === 'city') Object.assign(locationPopupState.value, { district: '', subdistrict: '', postal_code: '' });
  if (key === 'district') Object.assign(locationPopupState.value, { subdistrict: '', postal_code: '' });
  if (key === 'subdistrict') Object.assign(locationPopupState.value, { postal_code: '' });
  if (['province','city','district','subdistrict'].includes(key)) {
    const requestId = ++locationPopupState.requestId;
    try {
      await refreshLocationDropdowns();
    } catch (error) {
      if (requestId === locationPopupState.requestId && !document.getElementById('eva-location-popup')?.classList.contains('hidden')) {
        setError(error.message);
        closeLocationPopup(false, error);
      }
    }
  }
}

function openLocationPopup(initial = {}) {
  ensureLocationPopup();
  if (locationPopupState.reject) closeLocationPopup(false);
  const requestId = ++locationPopupState.requestId;
  locationPopupState.value = { province: '', city: '', district: '', subdistrict: '', postal_code: '', area_name: '', ...initial };
  const popup = document.getElementById('eva-location-popup');
  popup.classList.remove('hidden'); popup.classList.add('flex');
  return new Promise((resolve, reject) => {
    locationPopupState.resolve = resolve; locationPopupState.reject = reject;
    refreshLocationDropdowns().catch((error) => {
      if (locationPopupState.reject === reject && requestId === locationPopupState.requestId) {
        setError(error.message);
        closeLocationPopup(false, error);
      }
    });
  });
}

function closeLocationPopup(ok, reason = null) {
  const popup = document.getElementById('eva-location-popup'); if (!popup) return;
  ['province','city','district','subdistrict','postal_code','area_name'].forEach((key) => { locationPopupState.value[key] = locSelect(key)?.value || ''; });
  const values = { ...locationPopupState.value };
  locationPopupState.requestId += 1;
  popup.classList.add('hidden'); popup.classList.remove('flex');
  if (ok) locationPopupState.resolve?.(values); else locationPopupState.reject?.(reason || new Error('Batal pilih wilayah.'));
  locationPopupState.resolve = null; locationPopupState.reject = null;
}

async function promptLocationOverrides() {
  const values = await openLocationPopup(state.locationOverrides || {});
  state.locationOverrides = values;
  return values;
}

async function uploadSelectedFile() {
  if (state.uploadInFlight) return;
  state.uploadInFlight = true;
  if (els.convertButton) els.convertButton.disabled = true;
  try {
    const files = selectedFiles();
    if (!dxfConversionEnabled && String(state.outputMode || "").split("_").includes("dxf")) {
      const message = "Fitur DXF sementara dinonaktifkan di live server.";
      setOutputMode("");
      logger.warn(message);
      setError(message);
      return;
    }
    if (!selectedOutputTargets().length) {
      const message = "Pilih minimal satu mode output dulu.";
      logger.error(message);
      setError(message);
      return;
    }
    const error = validateFiles(files);
    if (error) {
      logger.error(error);
      setError(error);
      return;
    }
    if (state.kmlValidationError) {
      logger.error(state.kmlValidationError);
      setError(state.kmlValidationError);
      return;
    }
    await loadFilenameRules();
    const invalidNames = invalidFilenameItems(state.kmlFiles, state.scopeType);
    let filenameWarningAccepted = false;
    if (invalidNames.length) {
      const shouldContinue = await showFilenameWarningModal(invalidNames);
      if (!shouldContinue) {
        logger.warn("Upload dibatalkan karena nama file belum sesuai format.");
        setError("Upload dibatalkan. Perbaiki nama file sesuai contoh format.");
        return;
      }
      logger.warn("Nama file belum sesuai format, user memilih tetap lanjut.");
      filenameWarningAccepted = true;
    }
    if (modeHas("boq") && !state.kmlFiles.length) {
      const message = "KML/KMZ file is required for BOQ export.";
      logger.error(message);
      setError(message);
      return;
    }
    if (modeHas("astri") && !state.kmlFiles.length) {
      const message = "KML/KMZ file is required for ASTRI export.";
      logger.error(message);
      setError(message);
      return;
    }
    if (modeHas("hpdb") && !state.kmlFiles.length) {
      const message = "KML/KMZ file is required for HPDB export.";
      logger.error(message);
      setError(message);
      return;
    }
    let locationOverrides = {};
    if (modeHas("dxf") || modeHas("hpdb") || modeHas("astri")) {
      try {
        locationOverrides = await promptLocationOverrides();
      } catch (err) {
        logger.warn(err.message);
        return;
      }
    }
    if (modeHas("kmz") && !state.kmlFiles.length) {
      const message = "File KML/KMZ wajib diunggah untuk output KMZ Standar.";
      logger.error(message);
      setError(message);
      return;
    }
    if (state.outputMode === "kmz" && state.kmlFiles.length !== 1) {
      const message = "KMZ Standar hanya membutuhkan satu file KML/KMZ pada upload utama.";
      logger.error(message);
      setError(message);
      return;
    }
    if (modeHas("boq") && !currentBoqTemplateReady()) {
      const message = `Template BOQ ${state.networkType} belum tersedia di server.`;
      logger.error(message);
      setError(message);
      return;
    }
    if (modeHas("astri") && !currentHpdbTemplateReady()) {
      const message = `Template ASTRI ${state.networkType} belum tersedia di server.`;
      logger.error(message);
      setError(message);
      return;
    }
    const layers = selectedLayers();

    clearConsoleLogs();
    clearError();
    setPreviewTab("log");
    setConsoleLive(true);
    logger.info("Mulai Proses diklik. Menyiapkan proses...");
    setProgress(1);
    setStats();
    state.currentJobStatus = null;
    state.cancelInFlight = false;
    state.durationLabel = "--";
    state.hasError = false;
    resetBoqResultState();
    resetHpdbResultState();
    state.standardizerSummary = null;
    renderStandardizerSummary();
    setStatus("Mengunggah", `Mengunggah ${files.length} file ke server.`, "processing");
    logger.info("Preparing upload payload...");
    logger.info(`Selected ${files.length} file(s), ${formatFileSize(files.reduce((sum, file) => sum + file.size, 0))} total.`);
    logger.info(`Network type: ${networkTypeLabel(state.networkType)}.`);
    logger.info(`Output mode: ${outputModeLabel(state.outputMode)}.`);
    logger.info("ROAD otomatis dipotong mengikuti BOUNDARY CLUSTER jika tersedia.");
    hideDownloadButton();

    const formData = new FormData();
    files.forEach((file) => formData.append("files", file));
    layers.forEach((layer) => formData.append("layers", layer));
    formData.append("output_mode", state.outputMode);
    formData.append("network_type", state.networkType);
    formData.append("scope_type", state.scopeType);
    formData.append("filename_warning_accepted", filenameWarningAccepted ? "1" : "0");
    formData.append("road_clip_boundary_enabled", roadClipBoundaryEnabled() ? "1" : "0");
    // ROAD source follows the popup hierarchy: District OSM PBF first, Province PBF fallback.
    const popupProvince = locationOverrides?.province || "";
    const popupCity = locationOverrides?.city || "";
    const popupDistrict = locationOverrides?.district || "";
    const roadSelectionNeeded = modeHas("dxf") || modeHas("hpdb") || modeHas("astri");
    const roadKey = (value) => String(value || "").toLowerCase()
      .replace(/^kabupaten\s+|^kab\.?\s+|^kota\s+/i, "")
      .replace(/[^a-z0-9]/g, "");
    if (roadSelectionNeeded && popupDistrict) {
      const districtKey = roadKey(popupDistrict);
      const cityKey = roadKey(popupCity);
      const popupDistrictRoad = state.osmKecamatanRegions.find((region) =>
        roadKey(region.label) === districtKey && (!cityKey || roadKey(region.city) === cityKey)
      ) || state.osmKecamatanRegions.find((region) => roadKey(region.label) === districtKey);
      if (popupDistrictRoad) {
        formData.append("osm_city", popupDistrictRoad.city || popupCity);
        formData.append("osm_kecamatan", popupDistrictRoad.filename);
        logger.info(`ROAD OSM otomatis: ${popupDistrictRoad.filename} untuk Kecamatan ${popupDistrict}.`);
      }
    }
    if (roadSelectionNeeded && !formData.has("osm_kecamatan") && popupProvince) {
      const popupRoadFile = provinceRoadRegionFilename(popupProvince);
      const popupRoadRegion = state.geofabrikRegions.find((item) => item.filename === popupRoadFile);
      if (popupRoadRegion) {
        formData.append("geofabrik_region", popupRoadFile);
        logger.info(`ROAD OSM fallback: ${popupRoadRegion.filename} dari Province ${popupProvince}.`);
      } else {
        logger.warn(`File ROAD OSM belum tersedia untuk ${popupDistrict || popupProvince}.`);
      }
    }
    if (state.astriMatch?.matched) {
      formData.append("astri_cluster_code", state.astriMatch.cluster_code || "");
      // Popup values are authoritative. ASTRI match only supplies missing values.
      if (state.astriMatch.province && !locationOverrides?.province) formData.append("province", state.astriMatch.province);
      if (state.astriMatch.area_name && !locationOverrides?.area_name) formData.append("area_name", state.astriMatch.area_name);
    }
    if (locationOverrides?.province) formData.append("province", locationOverrides.province);
    if (locationOverrides?.city) formData.append("city", locationOverrides.city);
    if (locationOverrides?.city) formData.append("regency_city", locationOverrides.city);
    if (locationOverrides?.district) formData.append("district", locationOverrides.district);
    if (locationOverrides?.subdistrict) formData.append("subdistrict", locationOverrides.subdistrict);
    if (locationOverrides?.postal_code) formData.append("postal_code", locationOverrides.postal_code);
    if (locationOverrides?.area_name) formData.append("area_name", locationOverrides.area_name);
    // Do not send raw-source folder filters. The server standardizes folder
    // names before DXF generation and exports every eligible folder.

    const payload = await postFormDataWithProgress("/upload", formData, (value) => {
      setProgress(value);
      setStatus("Mengunggah", `Mengunggah ${files.length} file ke server.`, "processing");
    });
    window.setCurrentJobId(payload.job_id);
    state.currentJobStatus = "queued";
    logger.success("Upload diterima. Job masuk antrean.");
    setStatus("Masuk antrean", "Job dibuat. Menunggu proses konversi.", "processing");
    startPolling();
  } catch (err) {
    setStatus("Error", "Upload gagal.", "error");
    setConsoleLive(false);
    logger.error(err.message);
    setError(err.message, "job");
  } finally {
    state.uploadInFlight = false;
    renderValidationSummary();
  }
}

function updateStandardizerFileLabel() {
  const file = els.standardizerKmzInput?.files?.[0];
  if (!els.standardizerKmzName) return;
  if (!file) {
    els.standardizerKmzName.classList.add("hidden");
    els.standardizerKmzName.textContent = "";
    return;
  }
  els.standardizerKmzName.textContent = `${file.name} - ${formatFileSize(file.size)}`;
  els.standardizerKmzName.classList.remove("hidden");
}

function usefulStandardizerText(value) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text || text === "-" || text.length <= 2) return "";
  return text;
}

function normalizeStandardizerTitle(value) {
  let text = usefulStandardizerText(value);
  if (!text) return "";
  text = text
    .replace(/\.(kml|kmz|zip)$/i, "")
    .replace(/^as[_\s-]?plan[_\s-]?drawing[_\s-]?kmz[_\s-]*/i, "")
    .replace(/^(BKS\d{6})[_\s-]+\d{12,14}[_\s-]+(?:\1)?\s*/i, "$1 - ")
    .replace(/^(BKS\d{6})\s{2,}/i, "$1 - ")
    .replace(/\s*-\s*/g, " - ")
    .replace(/\s+/g, " ")
    .trim();
  return usefulStandardizerText(text);
}

function standardizerSummaryTitle(summary) {
  const matchedSite = usefulStandardizerText(summary?.matched_site);
  if (matchedSite) return matchedSite;

  const firstFile = Array.isArray(summary?.files) ? summary.files[0] : null;
  const candidates = [
    summary?.source_name,
    firstFile?.source_name,
    summary?.project_folder_name,
    summary?.matched_project,
    firstFile?.matched_site,
  ];
  for (const candidate of candidates) {
    const title = normalizeStandardizerTitle(candidate);
    if (title) return title;
  }
  return "KMZ Standar selesai";
}

function setValidationResultCollapsed(isCollapsed) {
  const collapsed = Boolean(isCollapsed);
  if (els.downloadValidationSummary) els.downloadValidationSummary.hidden = collapsed;
  if (els.validationResultToggle) {
    els.validationResultToggle.textContent = collapsed ? "Lihat detail" : "Sembunyikan detail";
    els.validationResultToggle.setAttribute("aria-expanded", String(!collapsed));
  }
}

function renderLegacyValidationReport(report = state.standardizerSummary?.validation_report) {
  if (!els.validationResultPanel || !els.downloadValidationSummary) return;
  if (!report || (!report.status && !report.checks && !report.errors && !report.warnings && !report.files)) {
    els.validationResultPanel.classList.add("hidden");
    els.downloadValidationSummary.innerHTML = "";
    setValidationResultCollapsed(true);
    return;
  }
  const files = Array.isArray(report.files) && report.files.length
    ? report.files
    : [{ ...report, source_name: report.source_name || state.standardizerSummary?.source_name || "KMZ hasil convert" }];
  const allErrors = files.reduce((n, file) => n + Number(file.error_count || (file.errors || []).length || 0), 0);
  const allWarnings = files.reduce((n, file) => n + Number(file.warning_count || (file.warnings || []).length || 0), 0);
  const globalTone = allErrors ? "error" : allWarnings ? "warning" : "success";
  const globalLabel = globalTone === "error" ? "ERROR" : globalTone === "warning" ? "WARNING" : "VALID";
  els.validationResultBadge.textContent = `${globalLabel} · ${formatNumber(allErrors)} error · ${formatNumber(allWarnings)} warning`;
  els.validationResultBadge.dataset.tone = globalTone;

  const categoryFor = (code = "") => {
    const c = String(code).toUpperCase();
    if (/FOLDER|EMPTY_|MISSING_|MISNAMED_FOLDER|UNTITLED_FOLDER/.test(c)) return "FOLDER / STRUKTUR";
    if (/CABLE_FORMAT|LINE_COLOR|SLING|TOPOLOGY/.test(c)) return "TOPOLOGY";
    if (/STYLE|ICON|COLOR|SCALE/.test(c)) return "NAMA / WARNA / ICON";
    if (/DUPLICATE/.test(c)) return "DUPLIKAT";
    if (/POLE|FAT|FDT|HH|HANDHOLE|ASSET|GEOMETRY|CLUSTER_CODE|UNTITLED|MISNAMED/.test(c)) return "FORMAT / NAMA";
    return "VALIDASI LAIN";
  };
  const renderGroup = (title, items) => {
    if (!items.length) return "";
    const errors = items.filter((item) => item.tone === "error").length;
    const warnings = items.length - errors;
    const names = [...new Set(items.map((item) => item.name || item.folder || item.code || "item").filter(Boolean))];
    const shownNames = names.slice(0, 3);
    const summaryText = `Nama-nama item: ${shownNames.join(", ") || "-"}${names.length > shownNames.length ? `; ${formatNumber(names.length - shownNames.length)} item lain` : ""}`;
    const renderIssue = (item) => `<li data-tone="${item.tone}"><b>${escapeHtml(item.message || item.code || item.tone)}</b>${item.folder ? ` <em>[${escapeHtml(item.folder)}]</em>` : ""}${item.name && item.name !== item.folder ? ` <small>${escapeHtml(item.name)}</small>` : ""}</li>`;
    const first = renderIssue(items[0]);
    const remaining = items.slice(1).map(renderIssue).join("");
    const extra = items.length > 1 ? `<details><summary>+${formatNumber(items.length - 1)} detail lain</summary><ul class="validation-issue-list">${remaining}</ul></details>` : "";
    const toneLabel = errors ? "ERROR" : "WARNING";
    const countText = `${errors ? `${formatNumber(errors)} error` : ""}${errors && warnings ? " / " : ""}${warnings ? `${formatNumber(warnings)} warning` : ""}`;
    return `<section class="legacy-validation-group"><div class="legacy-validation-group-title"><span>${escapeHtml(toneLabel)} ${escapeHtml(title)} <small>(${escapeHtml(countText)} / ${formatNumber(items.length)} item)</small></span></div><p class="legacy-validation-group-summary">${escapeHtml(summaryText)}</p><ul class="validation-issue-list">${first}</ul>${extra}</section>`;
  };
  const renderFile = (file, index) => {
    const errors = (file.errors || []).map((item) => ({ ...item, tone: "error" }));
    const warnings = (file.warnings || []).map((item) => ({ ...item, tone: "warning" }));
    const issues = [...errors, ...warnings];
    const tone = errors.length ? "error" : warnings.length ? "warning" : "success";
    const label = tone === "error" ? "ERROR" : tone === "warning" ? "WARNING" : "VALID";
    const groups = {};
    issues.forEach((item) => { const key = categoryFor(item.code); (groups[key] ||= []).push(item); });
    const summary = file.summary || {};
    const checks = Array.isArray(file.checks) ? file.checks : [];
    const folderOk = Number.isFinite(Number(summary.nonempty_required_folders))
      ? Number(summary.nonempty_required_folders || 0)
      : Math.max(0, Number(summary.required_folders || 0) - Number(summary.missing_folders || 0));
    const groupEntries = Object.entries(groups);
    const visibleGroups = groupEntries.slice(0, 3);
    const hiddenGroups = groupEntries.slice(3);
    const hiddenMarkup = hiddenGroups.length ? `<details class="legacy-validation-more-groups"><summary>Expand ${formatNumber(hiddenGroups.length)} jenis lainnya</summary>${hiddenGroups.map(([title, items]) => renderGroup(title, items)).join("")}</details>` : "";
    return `<article class="legacy-validation-file-card" data-tone="${tone}">
      <header class="legacy-validation-file-header"><div class="legacy-validation-file-state" data-tone="${tone}">${tone === "error" ? "×" : tone === "warning" ? "!" : "✓"}</div><div class="legacy-validation-file-title"><strong>${escapeHtml(file.source_name || `KMZ hasil convert ${index + 1}`)}</strong><small>${formatNumber(summary.placemarks || 0)} item · ${escapeHtml(file.scope_type || report.scope_type || "-")} · ${escapeHtml(file.network_type || report.network_type || "-")}</small></div><span class="legacy-validation-file-badge" data-tone="${tone}">${label}</span></header>
      <div class="legacy-validation-auto-notice"><b>PERIKSA AUTO</b><span>Mode Auto terbaca: ${escapeHtml(file.scope_type || report.scope_type || "-")}</span><span>Topologi Auto terbaca: ${escapeHtml(file.network_type || report.network_type || "-")}</span><small>Pastikan mode/topologi sesuai struktur KMZ. Hasil ini hanya laporan akhir; output tidak diubah.</small></div>
      <section class="legacy-validation-summary"><div class="legacy-validation-summary-title"><span>▤</span><strong>RINGKASAN PER JENIS</strong><span class="legacy-validation-counts"><b data-tone="error">${formatNumber(errors.length)} ERROR</b><b data-tone="warning">${formatNumber(warnings.length)} WARNING</b><small>${formatNumber(checks.length)} info / ${formatNumber(summary.placemarks || 0)} item</small></span></div><div class="validation-chip-list"><span class="validation-chip" data-tone="${folderOk === Number(summary.required_folders || 0) ? "success" : "error"}"><b>Folder wajib</b> ${formatNumber(folderOk)}/${formatNumber(summary.required_folders || 0)}</span><span class="validation-chip" data-tone="${Number(summary.cable_bad_format || 0) ? "error" : "success"}"><b>Cable</b> ${formatNumber(summary.cable_count || 0)} / ${formatNumber(summary.cable_bad_format || 0)} salah</span><span class="validation-chip" data-tone="${Number(summary.duplicate_points || 0) || Number(summary.duplicate_lines || 0) ? "warning" : "success"}"><b>Duplikat</b> ${formatNumber(Number(summary.duplicate_points || 0) + Number(summary.duplicate_lines || 0))}</span></div>${visibleGroups.map(([title, items]) => renderGroup(title, items)).join("")}${hiddenMarkup || (!groupEntries.length ? '<div class="legacy-validation-clean">✓ Tidak ada kesalahan terdeteksi pada KMZ hasil convert.</div>' : "")}</section>
    </article>`;
  };
  els.downloadValidationSummary.innerHTML = `<div class="legacy-validation-report-stack">${files.map(renderFile).join("")}</div>`;
  els.validationResultPanel.classList.remove("hidden");
}

function renderStandardizerSummary(summary = state.standardizerSummary) {
  renderLegacyValidationReport(summary?.validation_report);
  if (!els.standardizerSummary) return;
  if (!summary) {
    els.standardizerSummary.classList.add("hidden");
    els.standardizerSummary.innerHTML = "";
    return;
  }
  const fdtChanges = summary.fdt_changes || [];
  const folderChanges = summary.folder_changes || [];
  const warnings = summary.warnings || [];
  const stats = [
    ["FDT diubah", summary.fdt_renamed || 0],
    ["Nama diperbaiki", summary.name_standardized || 0],
    ["Objek dipindah", summary.placemark_organized || 0],
    ["Kabel diperbaiki", summary.distribution_cable_standardized || 0],
    ["Folder diperbaiki", summary.folder_renamed || 0],
    ["Wrapper dihapus", summary.folder_unwrapped || 0],
    ["Style diperbaiki", summary.style_standardized || 0],
  ];
  const totalChanges = stats.reduce((sum, [, value]) => sum + Number(value || 0), 0);
  const changeList = fdtChanges.slice(0, 4).map((change) => `
    <li>
      <span>FDT ${escapeHtml(change.fdt_order || "-")}</span>
      <strong>${escapeHtml(change.to || "-")}</strong>
    </li>
  `).join("");
  els.standardizerSummary.innerHTML = `
    <details class="standardizer-details" ${totalChanges ? "open" : ""}>
      <summary>
        <span>
          <strong>${escapeHtml(standardizerSummaryTitle(summary))}</strong>
          <small>Otomatis membersihkan KML/KMZ: rename FDT, fix nama, rapikan folder, dan style.</small>
        </span>
        <span class="standardizer-summary-status">${formatNumber(totalChanges)} perubahan &middot; ${formatNumber(warnings.length)} warning</span>
      </summary>
      <div class="mt-2 text-xs text-zinc-500">Sheet: ${escapeHtml(summary.matched_sheet || "-")} ${summary.matched_row ? `row ${formatNumber(summary.matched_row)}` : ""}</div>
      <div class="summary-grid standardizer-summary-grid mt-3">
        ${stats.map(([label, value]) => `
          <article class="summary-tile">
            <span>${escapeHtml(label)}</span>
            <strong>${formatNumber(value)}</strong>
          </article>
        `).join("")}
      </div>
      ${changeList || folderChanges.length > 4 ? `
        <ul class="standardizer-change-list">
          ${changeList}
          ${folderChanges.length > 4 ? `<li><span>Other folder fixes</span><strong>${formatNumber(folderChanges.length - 4)}</strong></li>` : ""}
        </ul>
      ` : ""}
    </details>
  `;
  els.standardizerSummary.classList.remove("hidden");
}

async function standardizeKmzFile(event) {
  event?.preventDefault();
  const file = els.standardizerKmzInput?.files?.[0];
  if (!file) {
    const message = "Pilih KMZ/KML yang mau distandarkan dulu.";
    logger.error(message);
    setError(message);
    return;
  }
  if (!isKmlFile(file)) {
    const message = "Standardizer hanya menerima .kml atau .kmz.";
    logger.error(message);
    setError(message);
    return;
  }
  if (!window.APP_CONFIG.fdtCodingSourceReady) {
    const message = "Spreadsheet coding FDT belum dikonfigurasi di server.";
    logger.error(message);
    setError(message);
    return;
  }

  clearConsoleLogs();
  clearError();
  hideDownloadButton();
  setProgress(0);
  setStats();
  state.standardizerSummary = null;
  renderStandardizerSummary();
  setConsoleLive(true);
  setStatus("Standardisasi", "Mengunggah KMZ dan membaca spreadsheet coding FDT.", "processing");
  logger.info(`Standardisasi KMZ: ${file.name}.`);
  logger.info(`FDT coding source: ${window.APP_CONFIG.fdtCodingSourceLabel || "server spreadsheet"}.`);
  if (els.standardizerButton) els.standardizerButton.disabled = true;

  const formData = new FormData();
  formData.append("kmz_file", file);

  try {
    const payload = await postFormDataWithProgress("/standardize-kmz", formData, (value) => {
      setProgress(Math.max(5, value));
    });
    mergeServerLogs(payload.logs || []);
    window.setCurrentJobId(payload.job_id);
    state.standardizerSummary = payload.standardizer_summary || {};
    renderStandardizerSummary();
    setProgress(payload.progress || 100);
    setStats(payload.stats || {});
    setStatus("Selesai", payload.message || "KMZ standar siap diunduh.", "done");
    setConsoleLive(false);
    showDownloadPanel(payload.download_url, payload.download_name, payload.download_mimetype, "kmz");
    logger.success("KMZ berhasil distandardisasi. Output siap diunduh.");
  } catch (err) {
    setStatus("Error", "Standardizer gagal.", "error");
    setConsoleLive(false);
    logger.error(err.message);
    setError(err.message, "job");
  } finally {
    if (els.standardizerButton) els.standardizerButton.disabled = false;
  }
}

/**
 * Publish an ASTRI ABD convert result into the main Converter KMZ workspace so the
 * standardizer summary, stats and the single download button live in one place
 * instead of being duplicated inside the Dokumen ABD card.
 */
function applyAbdConvertResultToWorkspace(payload = {}) {
  if (!payload || !payload.download_url) return false;
  clearConsoleLogs();
  clearError();
  hideDownloadButton();
  setOutputMode("kmz");
  if (payload.network_type) setNetworkType(payload.network_type);
  if (payload.scope_type) {
    state.scopeType = normalizeScopeType(payload.scope_type);
    syncScopeTypeInputs();
    syncOutputTargetInputs();
  }
  window.setCurrentJobId(payload.job_id || null);
  state.standardizerSummary = payload.standardizer_summary || {};
  renderStandardizerSummary();
  setProgress(100);
  setStats(payload.stats || {});
  setStatus("Selesai", payload.message || "ABD standar EVA siap.", "done");
  setConsoleLive(false);
  showDownloadPanel(payload.download_url, payload.download_name, payload.download_mimetype, "kmz");
  logger.success(`Convert ABD ASTRI selesai: ${payload.download_name || "KMZ standar"}.`);
  if (payload.source_name) logger.info(`Sumber ASTRI: ${payload.source_name}.`);
  return true;
}

// Bridge for the Dokumen ABD card: it owns no download UI of its own.
window.evaApplyAbdConvertResult = applyAbdConvertResultToWorkspace;

function startPolling() {
  if (state.pollingTimer) clearInterval(state.pollingTimer);
  state.statusPollFailures = 0;
  pollStatus();
  state.pollingTimer = setInterval(pollStatus, 1500);
}

async function pollStatus() {
  if (!state.jobId || state.statusPollInFlight) return;
  const pollJobId = state.jobId;
  const pollRequestId = (state.statusPollRequestId || 0) + 1;
  state.statusPollRequestId = pollRequestId;
  state.statusPollInFlight = true;

  try {
    const payload = await fetch(`/status/${encodeURIComponent(pollJobId)}`).then(parseJsonResponse);
    if (state.jobId !== pollJobId || state.statusPollRequestId !== pollRequestId) return;
    state.statusPollFailures = 0;
    state.currentJobStatus = payload.status || state.currentJobStatus;
    state.cancelInFlight = false;
    mergeServerLogs(payload.logs || []);
    if (payload.network_type) setNetworkType(payload.network_type);
    if (payload.scope_type) {
      state.scopeType = normalizeScopeType(payload.scope_type);
      syncScopeTypeInputs();
      syncOutputTargetInputs();
      if (typeof window._syncAstriTargetTypeFromScope === "function") window._syncAstriTargetTypeFromScope();
    }
    if (payload.output_mode) setOutputMode(payload.output_mode);
    if (payload.boq_template_ready !== undefined) {
      const variant = normalizeNetworkType(payload.network_type);
      window.APP_CONFIG.boqTemplateReady = Boolean(payload.boq_template_ready);
      window.APP_CONFIG.boqTemplateReadyByNetwork = window.APP_CONFIG.boqTemplateReadyByNetwork || {};
      window.APP_CONFIG.boqTemplateReadyByNetwork[variant] = Boolean(payload.boq_template_ready);
      if (payload.boq_template_name) {
        window.APP_CONFIG.boqTemplateNameByNetwork = window.APP_CONFIG.boqTemplateNameByNetwork || {};
        window.APP_CONFIG.boqTemplateNameByNetwork[variant] = payload.boq_template_name;
      }
      updateBoqTemplateStatus(currentBoqTemplateReady());
    }
    if (payload.astri_template_ready !== undefined) {
      const variant = normalizeNetworkType(payload.network_type);
      window.APP_CONFIG.astriTemplateReady = Boolean(payload.astri_template_ready);
      window.APP_CONFIG.astriTemplateReadyByNetwork = window.APP_CONFIG.astriTemplateReadyByNetwork || {};
      window.APP_CONFIG.astriTemplateReadyByNetwork[variant] = Boolean(payload.astri_template_ready);
      if (payload.astri_template_name) {
        window.APP_CONFIG.astriTemplateNameByNetwork = window.APP_CONFIG.astriTemplateNameByNetwork || {};
        window.APP_CONFIG.astriTemplateNameByNetwork[variant] = payload.astri_template_name;
      }
      updateHpdbTemplateStatus(currentHpdbTemplateReady());
    }
    if (payload.hpdb_template_ready !== undefined) {
      const variant = normalizeNetworkType(payload.network_type);
      window.APP_CONFIG.hpdbTemplateReady = Boolean(payload.hpdb_template_ready);
      window.APP_CONFIG.hpdbTemplateReadyByNetwork = window.APP_CONFIG.hpdbTemplateReadyByNetwork || {};
      window.APP_CONFIG.hpdbTemplateReadyByNetwork[variant] = Boolean(payload.hpdb_template_ready);
      if (payload.hpdb_template_name) {
        window.APP_CONFIG.hpdbTemplateNameByNetwork = window.APP_CONFIG.hpdbTemplateNameByNetwork || {};
        window.APP_CONFIG.hpdbTemplateNameByNetwork[variant] = payload.hpdb_template_name;
      }
      if (payload.astri_template_ready === undefined) updateHpdbTemplateStatus(currentHpdbTemplateReady());
    }
    state.boqSummary = payload.boq_summary || state.boqSummary || {};
    state.boqPreviewRows = payload.boq_preview || state.boqPreviewRows || [];
    state.boqNroPreview = payload.boq_nro_preview || state.boqNroPreview || { columns: [], rows: [] };
    state.boqWarnings = payload.boq_warnings || state.boqWarnings || [];
    state.hpdbSummary = payload.hpdb_summary || state.hpdbSummary || {};
    state.hpdbPreview = payload.hpdb_preview || state.hpdbPreview || { cluster: [], fat: [], homepass: [], limit: null, totals: {} };
    state.hpdbWarnings = payload.hpdb_warnings || state.hpdbWarnings || [];
    state.hpdbIssues = payload.hpdb_issues || state.hpdbIssues || [];
    state.standardizerSummary = payload.standardizer_summary || state.standardizerSummary || null;
    renderBoqSummary();
    renderBoqPreview();
    renderBoqNroPreview();
    renderHpdbSummary();
    renderHpdbPreviews();
    renderStandardizerSummary();
    renderKmlFolderSelector();
    setConsoleLive(payload.status === "queued" || payload.status === "processing" || payload.status === "retrying");
    setProgress(payload.progress);
    setStats(payload.stats);

    if (payload.status === "queued") {
      setStatus("Masuk antrean", payload.message || "Job sudah diterima dan menunggu diproses.", "processing");
    } else if (payload.status === "processing") {
      setStatus("Memproses", payload.message || "File sedang diproses.", "processing");
    } else if (payload.status === "retrying") {
      const retryCount = Number(payload.retry_count || 0);
      const maxRetries = Number(payload.max_retries || 0);
      const attemptLabel = maxRetries ? `Mencoba ulang (${retryCount + 1}/${maxRetries + 1})` : "Mencoba ulang";
      setStatus(attemptLabel, payload.message || "Job sempat gagal sementara dan akan dicoba lagi.", "processing");
      (payload.retry_errors || []).forEach((retryError, index) => {
        console.info(`Retry history ${index + 1}: ${retryError.message || retryError}`);
      });
    } else if (payload.status === "done") {
      clearInterval(state.pollingTimer);
      state.pollingTimer = null;
      setConsoleLive(false);
      setStatus("Selesai", payload.message || "Proses selesai. Output siap diunduh.", "done");
      els.convertButton.disabled = false;
      showDownloadPanel(payload.download_url, payload.download_name, payload.download_mimetype);
      if (typeof window.ensureAstriPanel === "function") window.ensureAstriPanel();
      if (typeof window.refreshAstriStatus === "function") window.refreshAstriStatus();
      if (typeof window.matchAstriCluster === "function") window.matchAstriCluster();
      if (modeHas("dxf")) {
        await loadPreview(state.jobId);
      } else {
        if (state.geoJsonLayer) {
          map.removeLayer(state.geoJsonLayer);
          state.geoJsonLayer = null;
        }
        state.mapLayerVisibility = {};
        els.previewText.textContent = `${outputModeLabel(state.outputMode)} selesai. Tidak ada preview map untuk mode ini.`;
        renderSupportPanels();
      }
    } else if (payload.status === "cancelled") {
      clearInterval(state.pollingTimer);
      state.pollingTimer = null;
      setConsoleLive(false);
      state.hasError = false;
      setStatus("Dibatalkan", payload.message || "Dibatalkan oleh user.", "warning");
      logger.warn(payload.message || "Dibatalkan oleh user.");
      els.convertButton.disabled = false;
    } else if (payload.status === "error" || payload.status === "failed") {
      clearInterval(state.pollingTimer);
      state.pollingTimer = null;
      setConsoleLive(false);
      setStatus(payload.status === "failed" ? "Gagal" : "Error", payload.error || payload.message, "error");
      state.hasError = true;
      logger.error(payload.error || payload.message);
      setError(payload.error || payload.message, "job");
      els.convertButton.disabled = false;
    }
  } catch (err) {
    state.statusPollFailures = (state.statusPollFailures || 0) + 1;
    if (state.statusPollFailures < 8) {
      setConsoleLive(true);
      setStatus("Memproses", "Koneksi status sempat putus, sedang mencoba ulang polling.", "processing");
      logger.warn(`Status polling retry ${state.statusPollFailures}/7: ${err.message}`);
      return;
    }
    clearInterval(state.pollingTimer);
    state.pollingTimer = null;
    setConsoleLive(false);
    setStatus("Error", "Gagal mengambil status job.", "error");
    logger.error(`Status polling failed: ${err.message}`);
    setError(err.message, "job");
    els.convertButton.disabled = false;
  } finally {
    if (state.statusPollRequestId === pollRequestId) state.statusPollInFlight = false;
  }
}

async function cancelCurrentJob() {
  if (!state.jobId || state.cancelInFlight) return;
  state.cancelInFlight = true;
  updateCancelButton();
  try {
    const payload = await fetch(`/jobs/${encodeURIComponent(state.jobId)}/cancel`, { method: "POST" }).then(parseJsonResponse);
    if (state.pollingTimer) {
      clearInterval(state.pollingTimer);
      state.pollingTimer = null;
    }
    state.currentJobStatus = payload.status || "cancelled";
    state.cancelInFlight = false;
    state.hasError = false;
    setConsoleLive(false);
    setStatus("Dibatalkan", "Dibatalkan oleh user.", "warning");
    logger.warn("Dibatalkan oleh user.");
    els.convertButton.disabled = false;
  } catch (err) {
    state.cancelInFlight = false;
    updateCancelButton();
    logger.error(err.message);
    setError(err.message, "job");
  }
}

async function loadPreview(jobId) {
  try {
    const geojson = await fetch(`/preview/${jobId}`).then(parseJsonResponse);
    if (state.geoJsonLayer) {
      map.removeLayer(state.geoJsonLayer);
      state.geoJsonLayer = null;
    }
    state.mapLayerVisibility = {};

    state.geoJsonLayer = L.geoJSON(geojson, {
      style: (feature) => mapLayerStyle(feature),
      pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
        radius: 5,
        weight: 1.5,
        fillOpacity: 0.75,
        ...mapLayerStyle(feature),
      }),
      onEachFeature: (feature, layer) => {
        const props = feature.properties || {};
        const label = props.name ? `${props.layer}: ${props.name}` : props.layer;
        layer.bindPopup(label || "OSM feature");
      },
    }).addTo(map);

    const bounds = state.geoJsonLayer.getBounds();
    if (bounds.isValid()) {
      map.fitBounds(bounds.pad(0.12), { animate: true });
      els.previewText.textContent = `${geojson.features.length.toLocaleString("id-ID")} fitur ditampilkan di preview.`;
    } else {
      els.previewText.textContent = "DXF selesai, tetapi tidak ada geometri valid untuk preview.";
    }
    renderMapLegend();
    renderSupportPanels();
  } catch (err) {
    els.previewText.textContent = "Preview tidak dapat dimuat.";
    logger.warn(`Preview failed to load: ${err.message}`);
    setError(err.message, "job");
  }
}

addListener(els.osmFileInput, "change", (event) => selectFiles("osm", event.target.files));
addListener(els.kmlFileInput, "change", (event) => selectFiles("kml", event.target.files));
addListener(els.standardizerKmzInput, "change", updateStandardizerFileLabel);
els.networkTypeInputs.forEach((input) => {
  input.addEventListener("change", () => {
    if (!input.checked) return;
    setNetworkType(input.value);
    hideDownloadButton();
    clearError();
  });
});
document.querySelectorAll(".scope-type-input").forEach((input) => {
  input.addEventListener("change", () => {
    if (!input.checked) return;
    state.scopeType = normalizeScopeType(input.value);
    syncScopeTypeInputs();
    syncOutputTargetInputs();
    renderValidationSummary();
    hideDownloadButton();
    clearError();
    if (typeof window._syncAstriTargetTypeFromScope === "function") window._syncAstriTargetTypeFromScope();
  });
});
document.querySelectorAll('[data-output-target="dxf"]').forEach((option) => {
  option.addEventListener("click", (event) => {
    if (dxfConversionEnabled) return;
    event.preventDefault();
    event.stopPropagation();
    syncOutputTargetInputs();
    clearError();
  });
});
els.outputModeInputs.forEach((input) => {
  input.addEventListener("change", () => {
    if (input.value === "dxf" && !dxfConversionEnabled) {
      input.checked = false;
      setOutputMode("");
      return;
    }
    const checkedTargets = Array.from(els.outputModeInputs)
      .filter((item) => !item.disabled)
      .filter((item) => item.checked)
      .map((item) => item.value);
    // Allow zero selected — generate is gated by validationReadiness().
    setOutputMode(checkedTargets.length ? modeFromTargets(checkedTargets) : "");
    clearError();
    notifyEvaAssistant("mode-selected", {
      key: `mode-selected:${state.outputMode}`,
    });
  });
});
addListener(els.form, "submit", (event) => {
  event.preventDefault();
  state.hasUserStartedProcess = true;
  uploadSelectedFile();
});
addListener(els.standardizerForm, "submit", (event) => {
  state.hasUserStartedProcess = true;
  standardizeKmzFile(event);
});

function setupDropZone(zone, kind) {
  if (!zone) return;
  ["dragenter", "dragover"].forEach((eventName) => {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.add("drag-active");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    zone.addEventListener(eventName, (event) => {
      event.preventDefault();
      zone.classList.remove("drag-active");
    });
  });

  zone.addEventListener("drop", (event) => {
    if (event.dataTransfer.files.length) selectFiles(kind, event.dataTransfer.files);
  });
}

setupDropZone(els.osmDropZone, "osm");
setupDropZone(els.kmlDropZone, "kml");

[els.osmFileName, els.kmlFileName].forEach((node) => {
  node?.addEventListener("click", (event) => {
    const actionButton = event.target?.closest?.("[data-file-action]");
    if (!actionButton) return;
    event.preventDefault();
    event.stopPropagation();
    const kind = actionButton.dataset.fileKind;
    if (actionButton.dataset.fileAction === "clear") clearSelectedFiles(kind);
    if (actionButton.dataset.fileAction === "replace") replaceSelectedFiles(kind);
  });
});

addListener(els.resetSessionButton, "click", resetCurrentSession);

addListener(els.selectAllKmlFolders, "click", () => {
  state.kmlFolders.forEach((folder) => {
    if (Number(folder.count || 0) > 0) folder.selected = true;
  });
  renderKmlFolderSelector();
});

addListener(els.clearKmlFolders, "click", () => {
  state.kmlFolders.forEach((folder) => {
    folder.selected = false;
  });
  renderKmlFolderSelector();
});

addListener(els.kmlFolderSearch, "input", (event) => {
  state.folderSearch = event.target.value || "";
  renderKmlFolderSelector();
});

els.kmlFolderFilterChips?.querySelectorAll("[data-folder-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    state.folderFilter = button.dataset.folderFilter || "all";
    els.kmlFolderFilterChips.querySelectorAll("[data-folder-filter]").forEach((chip) => {
      chip.classList.toggle("active", chip === button);
    });
    renderKmlFolderSelector();
  });
});

els.previewTabs?.querySelectorAll("[data-preview-tab]").forEach((button) => {
  button.addEventListener("click", () => setPreviewTab(button.dataset.previewTab));
});
els.boqSubTabs?.querySelectorAll("[data-boq-subtab]").forEach((button) => {
  button.addEventListener("click", () => setBoqSubTab(button.dataset.boqSubtab));
});
els.hpdbSubTabs?.querySelectorAll("[data-hpdb-subtab]").forEach((button) => {
  button.addEventListener("click", () => setHpdbSubTab(button.dataset.hpdbSubtab));
});

addListener(els.mapLockToggle, "click", toggleMapLock);
addListener(els.mapLockOverlay, "click", () => setMapLocked(false));
addListener(els.mapLegendToggle, "click", () => {
  state.mapLegendOpen = !state.mapLegendOpen;
  renderMapLegend();
});
addListener(els.mapLegendList, "change", (event) => {
  const input = event.target?.closest?.("[data-map-layer]");
  if (!input) return;
  state.mapLayerVisibility[input.dataset.mapLayer] = input.checked;
  applyMapLayerVisibility();
});
addListener(els.clearLogButton, "click", clearConsoleLogs);
addListener(els.copyLogButton, "click", copyConsoleLog);
addListener(els.downloadLogButton, "click", downloadConsoleLog);
addListener(els.consoleToggle, "click", () => setConsoleCollapsed(!state.consoleCollapsed));
addListener(els.validationResultToggle, "click", () => setValidationResultCollapsed(!els.downloadValidationSummary || !els.downloadValidationSummary.hidden));
addListener(els.cancelJobButton, "click", cancelCurrentJob);
addListener(els.downloadButton, "click", saveOutputToLocal);
addListener(els.convertAnotherButton, "click", () => resetCurrentSession({ force: true }));
addListener(els.resetOutputNameButton, "click", () => resetGeneratedOutput({ force: true }));
function finishOutputRename(save = true) {
  const name = save ? sanitizeOutputFileName(els.outputFileName.value, state.downloadMode) : state.downloadName;
  els.outputFileName.value = name;
  state.downloadName = name;
  els.downloadButton.download = name;
}
addListener(els.outputFileName, "keydown", (event) => {
  if (event.key !== "Enter" && event.key !== "Escape") return;
  event.preventDefault();
  finishOutputRename(event.key === "Enter");
  els.outputFileName.blur();
});
addListener(els.outputFileName, "blur", () => finishOutputRename());
addListener(els.dxfEmptyCta, "click", () => {
  els.convertButton?.scrollIntoView({ behavior: "smooth", block: "center" });
  els.convertButton?.focus({ preventScroll: true });
});
addListener(els.boqPreviewToggle, "click", () => setBoqPreviewCollapsed(!state.boqPreviewCollapsed));
addListener(els.boqStatusFilter, "change", () => {
  state.boqStatusFilter = els.boqStatusFilter.value;
  renderBoqPreview();
});
addListener(els.boqSourceFilter, "change", () => {
  state.boqSourceFilter = els.boqSourceFilter.value;
  renderBoqPreview();
});
addListener(els.copyWarningSummaryButton, "click", copyWarningSummary);
addListener(els.viewWarningsInBoqButton, "click", showWarningsInBoqTable);
addListener(els.roadClipBoundaryToggle, "change", renderSupportPanels);
document.querySelectorAll(".hpdb-filter-select").forEach((select) => {
  select.addEventListener("change", () => {
    state.hpdbFilter = select.value || "all";
    state.hpdbHomepassPage = 1;
    syncHpdbFilterControls();
    renderHpdbPreviews();
  });
});
document.querySelectorAll(".hpdb-search-input").forEach((input) => {
  input.addEventListener("input", () => {
    state.hpdbSearch = input.value || "";
    state.hpdbHomepassPage = 1;
    syncHpdbFilterControls();
    renderHpdbPreviews();
  });
});
addListener(els.hpdbHomepassPrevPage, "click", () => {
  state.hpdbHomepassPage = Math.max(1, state.hpdbHomepassPage - 1);
  renderHpdbPreviews();
});
addListener(els.hpdbHomepassNextPage, "click", () => {
  state.hpdbHomepassPage += 1;
  renderHpdbPreviews();
});

initStats();
initKmlFolderSelector();
setNetworkType(window.APP_CONFIG.defaultNetworkType || "AE");
updateBoqPanel();
updateHpdbPanel();
renderBoqSummary();
renderBoqPreview();
renderBoqNroPreview();
renderHpdbSummary();
renderHpdbPreviews();
setBoqPreviewCollapsed(false);
renderConsoleLogs();
setConsoleLive(false);
setConsoleCollapsed(true);
setProgress(0);
setMapLocked(true);
setPreviewTab("summary");
renderSupportPanels();


/* =========================================================================
   v2 features — toast, theme, stage progress, drag shield, modal/drawer,
   shortcuts, retry, history, export CSV
   ========================================================================= */
(function () {
  "use strict";

  const STORAGE = {
    history: "eva.history",
  };

  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  }

  // ---------- Feedback bridge ----------
  // Popup policy: routine feedback stays in page status/log. Critical errors
  // and explicitly important events may open a popup.
  const recentPopupKeys = new Map();
  function showToast({ title, message = "", tone = "info", duration = 4500, important = false, dedupeKey = "" } = {}) {
    if (!important && tone !== "error") {
      window.evaSetInlineFeedback?.(title, message, tone);
      return null;
    }
    const severity = tone === "error" ? "error" : tone === "warning" ? "warning" : tone === "success" ? "success" : "info";
    const key = tone === "error" ? `error:${String(message || "").trim()}` : (dedupeKey || `${severity}:${title || ""}:${message || ""}`);
    const now = Date.now();
    if (recentPopupKeys.has(key) && now - recentPopupKeys.get(key) < 1800) return null;
    recentPopupKeys.set(key, now);
    for (const [oldKey, timestamp] of recentPopupKeys) {
      if (now - timestamp > 10000) recentPopupKeys.delete(oldKey);
    }
    return window.evaPopupFeedback?.({ title, message, severity }, { timeoutMs: duration });
  }
  window.evaToast = showToast;

  // ---------- Theme (disabled — always light) ----------

  // ---------- Stage progress ----------
  const STAGES = [
    { key: "upload", label: "Upload", match: /upload/i, fromProgress: 0, toProgress: 8 },
    { key: "queue", label: "Queue", match: /queue|antri/i, fromProgress: 8, toProgress: 14 },
    { key: "parse", label: "Parse", match: /parsing|parse|reading|membaca/i, fromProgress: 14, toProgress: 35 },
    { key: "validate", label: "Validate", match: /valid|cek folder|kml|folder/i, fromProgress: 35, toProgress: 48 },
    { key: "dxf", label: "DXF", match: /dxf|export\s*cad|generate dxf/i, fromProgress: 48, toProgress: 70 },
    { key: "boq", label: "BOQ", match: /boq/i, fromProgress: 70, toProgress: 84 },
    { key: "hpdb", label: "HPDB", match: /hpdb|astri/i, fromProgress: 84, toProgress: 95 },
    { key: "package", label: "Finalize", match: /zip|finalize|package|selesai|done/i, fromProgress: 95, toProgress: 100 },
  ];

  function pickActiveStage(progress, label, message) {
    const text = `${label || ""} ${message || ""}`;
    const matched = STAGES.findIndex((s) => s.match.test(text));
    if (matched >= 0) return matched;
    // Fallback by progress range
    let idx = 0;
    for (let i = 0; i < STAGES.length; i++) {
      if (progress >= STAGES[i].fromProgress) idx = i;
    }
    return idx;
  }

  function renderStageProgress() {
    const root = document.getElementById("stageProgress");
    if (!root) return;
    const tone = state.currentStatusTone;
    const isError = tone === "error" || state.hasError;
    const isDone = tone === "done" || (state.downloadUrl && tone !== "error");
    const isProcessing = tone === "processing";
    const isIdle = !isProcessing && !isDone && !isError;

    if (isIdle || isDone) {
      root.innerHTML = "";
      root.style.display = "none";
      return;
    }
    root.style.display = "flex";
    const progress = Number(state.progress) || 0;
    const activeIdx = pickActiveStage(progress, state.currentStatusLabel, state.currentStatusMessage);

    root.innerHTML = STAGES.map((stage, idx) => {
      let stState;
      if (isError && idx === activeIdx) stState = "error";
      else if (isError && idx < activeIdx) stState = "done";
      else if (isError) stState = "pending";
      else if (isDone) stState = "done";
      else if (idx < activeIdx) stState = "done";
      else if (idx === activeIdx) stState = "active";
      else stState = "pending";
      return `<span class="stage-progress-item" data-state="${stState}"><span class="stage-dot"></span>${escHtml(stage.label)}</span>`;
    }).join("");
  }

  // ---------- Wrap setStatus & setProgress ----------
  const origSetStatus = window.setStatus;
  if (typeof origSetStatus === "function") {
    window.setStatus = function (label, message, tone) {
      let prevTone = "idle";
      let prevDownload = "";
      try {
        prevTone = state.currentStatusTone;
        prevDownload = state.downloadUrl;
      } catch (_) {}
      try { origSetStatus.call(this, label, message, tone); } catch (_) {}
      try { renderStageProgress(); } catch (_) {}

      // Conversion state is shown in the page status, progress, log, and
      // download panel. No completion/error popup here; setError owns one
      // deduplicated important error notification.
      if (tone === "done" && prevTone !== "done") {
        try { saveJobToHistory(); } catch (_) {}
      } else if (tone === "error" && prevTone !== "error") {
        showRetryBar(message);
      } else if (tone !== "error") {
        hideRetryBar();
      }
    };
  }

  const origSetProgress = window.setProgress;
  if (typeof origSetProgress === "function") {
    window.setProgress = function (value) {
      origSetProgress.call(this, value);
      try { renderStageProgress(); } catch (_) {}
    };
  }

  // Override setError to include retry bar
const origSetError = window.setError;
  if (typeof origSetError === "function") {
    window.setError = function (message, scope) {
      try { origSetError.call(this, message, scope); } catch (_) {}
      try {
        if (state.errorScope === "job" || scope === "job") {
          showRetryBar(message);
        }
      } catch (_) {
        const bar = document.getElementById("retryBar");
        if (bar && scope === "job") bar.classList.remove("hidden");
      }
    };
  }

const origClearError = window.clearError;
  if (typeof origClearError === "function") {
    window.clearError = function () {
      try { origClearError.call(this); } catch (_) {}
      hideRetryBar();
    };
  }
  window.__evaTestMarkStarted = () => {
    state.hasUserStartedProcess = true;
    state.errorScope = "job";
  };

  // ---------- Retry bar ----------
  function showRetryBar(message) {
    const bar = document.getElementById("retryBar");
    const msg = document.getElementById("retryBarMessage");
if (!bar || !message || state.errorScope !== "job") return;
    if (msg) msg.textContent = message;
    bar.classList.remove("hidden");
  }
  function hideRetryBar() {
    const bar = document.getElementById("retryBar");
    const msg = document.getElementById("retryBarMessage");
    if (msg) msg.textContent = "";
    if (bar) bar.classList.add("hidden");
  }

  function initRetry() {
    const btn = document.getElementById("retryButton");
    if (!btn) return;
    btn.addEventListener("click", () => {
      hideRetryBar();
      if (selectedFiles().length === 0) {
        showToast({ title: "Tidak ada file", message: "Unggah file dulu sebelum coba lagi.", tone: "warning" });
        return;
      }
      state.hasUserStartedProcess = true;
      const form = document.getElementById("uploadForm");
      if (form) form.requestSubmit();
    });
  }

  // ---------- Quick stats banner ----------
  function renderQuickStats() {
    const node = document.getElementById("quickStats");
    if (!node) return;
    const files = (typeof selectedFiles === "function") ? selectedFiles() : [];
    if (!files.length) {
      node.classList.add("hidden");
      node.innerHTML = "";
      return;
    }
    const total = files.reduce((sum, f) => sum + f.size, 0);
    const osm = state.osmFiles ? state.osmFiles.length : 0;
    const kml = state.kmlFiles ? state.kmlFiles.length : 0;
    const bytes = formatFileSize(total);
    const items = [
      `<span class="quick-stats-item"><strong>${files.length}</strong> file</span>`,
      osm ? `<span class="quick-stats-item"><strong>${osm}</strong> file ROAD</span>` : "",
      kml ? `<span class="quick-stats-item"><strong>${kml}</strong> KML/KMZ</span>` : "",
      `<span class="quick-stats-item">Total <strong>${bytes}</strong></span>`,
    ].filter(Boolean).join("");
    node.innerHTML = items;
    node.classList.remove("hidden");
  }

  const origRenderSelected = window.renderSelectedFiles;
  if (typeof origRenderSelected === "function") {
    window.renderSelectedFiles = function () {
      origRenderSelected.call(this);
      renderQuickStats();
    };
  }

  // ---------- Drag shield (drag-anywhere on page) ----------
  function initDragShield() {
    const shield = document.getElementById("dragShield");
    if (!shield) return;
    let counter = 0;
    let dragOverTarget = false;

    function hasFiles(e) {
      if (!e.dataTransfer) return false;
      const types = e.dataTransfer.types;
      if (!types) return false;
      for (let i = 0; i < types.length; i++) {
        if (types[i] === "Files") return true;
      }
      return false;
    }

    window.addEventListener("dragenter", (e) => {
      if (!hasFiles(e)) return;
      counter += 1;
      shield.classList.add("active");
    });
    window.addEventListener("dragover", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragOverTarget = true;
      shield.classList.add("active");
    });
    window.addEventListener("dragleave", (e) => {
      if (!hasFiles(e)) return;
      counter = Math.max(0, counter - 1);
      if (counter === 0) shield.classList.remove("active");
    });
    window.addEventListener("drop", (e) => {
      const isOnZone = e.target && e.target.closest && e.target.closest(".drop-zone");
      counter = 0;
      shield.classList.remove("active");
      if (isOnZone) return; // let drop-zone handler process it
      if (!hasFiles(e)) return;
      e.preventDefault();
      const files = Array.from(e.dataTransfer.files || []);
      if (!files.length) return;
      const osm = files.filter((f) => /\.(osm|pbf)$|\.osm\.pbf$/i.test(f.name));
      const kml = files.filter((f) => /\.(kml|kmz)$/i.test(f.name));
      if (osm.length && typeof selectFiles === "function") selectFiles("osm", osm);
      if (kml.length && typeof selectFiles === "function") selectFiles("kml", kml);
      const dropped = osm.length + kml.length;
      if (dropped === 0) {
        showToast({ title: "Format tidak didukung", message: "Gunakan file .kml atau .kmz.", tone: "warning" });
      } else {
        showToast({ title: "File diterima", message: `${dropped} file ditambahkan.`, tone: "success" });
      }
    });
  }

  // ---------- Modal helpers ----------
  function openModal(id) {
    const node = document.getElementById(id);
    if (!node) return;
    if (id === "helpModal" && node.classList.contains("hidden")) node._returnFocus = document.activeElement;
    node.classList.remove("hidden");
    node.setAttribute("aria-hidden", "false");
    node.removeAttribute("inert");
    document.body.style.overflow = "hidden";
    const focusable = node.querySelector("button, [href], input, select, textarea");
    if (focusable) focusable.focus();
  }
  function closeModal(id) {
    const node = document.getElementById(id);
    if (!node) return;
    const wasOpen = !node.classList.contains("hidden");
    node.classList.add("hidden");
    node.setAttribute("aria-hidden", "true");
    node.setAttribute("inert", "");
    document.body.style.overflow = "";
    if (id === "helpModal" && wasOpen && node._returnFocus?.isConnected) {
      node._returnFocus.focus();
      node._returnFocus = null;
    }
    if (id === "historyDrawer" && activityState?.timer) {
      clearInterval(activityState.timer);
      activityState.timer = null;
    }
  }
  function closeAllOverlays() {
    closeModal("helpModal");
    closeModal("fdtCodingModal");
    closeModal("coremanModal");
    closeModal("rfsModal");
    closeModal("gdrModal");
    closeModal("kmzCoordsModal");
    closeModal("historyDrawer");
  }

  function initModals() {
    const abdDialog = document.getElementById("abdCadModal");
    const abdFrame = document.getElementById("abdCadFrame");
    if (abdDialog && abdFrame) {
      const close = document.getElementById("abdCadClose");
      const frameControls = () => [...(abdFrame.contentDocument?.querySelectorAll("button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex='-1'])") || [])]
        .filter((node) => !node.hidden && node.getClientRects().length);
      close.addEventListener("click", () => abdDialog.close());
      abdDialog.addEventListener("close", () => {
        document.body.style.overflow = abdDialog._previousOverflow || "";
        abdDialog._returnFocus?.focus();
      });
      abdDialog.addEventListener("keydown", (event) => {
        if (event.key === "Escape") event.stopPropagation();
        if (event.key === "Tab" && document.activeElement === close) {
          event.preventDefault();
          const controls = frameControls();
          (event.shiftKey ? controls.at(-1) : controls[0])?.focus();
        }
      });
      abdFrame.addEventListener("load", () => {
        abdFrame.contentDocument.addEventListener("keydown", (event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            abdDialog.close();
          } else if (event.key === "Tab") {
            const controls = frameControls();
            const edge = event.shiftKey ? controls[0] : controls.at(-1);
            if (event.target === edge) {
              event.preventDefault();
              close.focus();
            }
          }
        });
      });
      abdDialog.addEventListener("click", (event) => {
        const rect = abdDialog.getBoundingClientRect();
        if (event.target === abdDialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) abdDialog.close();
      });
    }
    document.querySelectorAll(".modal-backdrop[role='dialog']").forEach((modal) => {
      modal.addEventListener("keydown", (event) => {
        if (event.key !== "Tab" || modal.classList.contains("hidden")) return;
        const focusable = [...modal.querySelectorAll("button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex='-1'])")]
          .filter((node) => !node.hidden && node.getClientRects().length && getComputedStyle(node).visibility !== "hidden");
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      });
    });
    document.addEventListener("click", (e) => {
      const shortcutCard = e.target && e.target.closest && e.target.closest(".fdt-card-coding, .fdt-card-core, .fdt-card-rfs, .fdt-card-gdr, .fdt-card-kmz, .fdt-card-stella, .fdt-card-abd-cad");
      if (shortcutCard) {
        if (shortcutCard.classList.contains("fdt-card-coding")) openFdtCoding();
        if (shortcutCard.classList.contains("fdt-card-core")) openCoreman();
        if (shortcutCard.classList.contains("fdt-card-rfs")) openRfsDashboard();
        if (shortcutCard.classList.contains("fdt-card-gdr")) openGdrDashboard();
        if (shortcutCard.classList.contains("fdt-card-kmz")) openKmzCoords();
        if (shortcutCard.classList.contains("fdt-card-stella")) document.dispatchEvent(new CustomEvent("eva:open-stella"));
        if (shortcutCard.classList.contains("fdt-card-abd-cad")) openAbdCad(shortcutCard);
        return;
      }
      const closeAttr = e.target && e.target.closest && e.target.closest("[data-close-modal]");
      if (closeAttr) {
        const modal = closeAttr.closest(".modal-backdrop");
        if (modal) closeModal(modal.id);
      }
      const closeDrawer = e.target && e.target.closest && e.target.closest("[data-close-drawer]");
      if (closeDrawer) {
        closeModal("historyDrawer");
      }
      // Click on backdrop closes modal
      const backdrop = e.target && e.target.classList && e.target.classList.contains("modal-backdrop");
      if (backdrop) closeModal(e.target.id);
    });

    const helpBtn = document.getElementById("header-help") || document.getElementById("helpButton");
    const fdtBtn = document.getElementById("fdtCodingButton");
    const fdtCard = document.querySelector(".fdt-card-coding");
    const coremanCard = document.querySelector(".fdt-card-core");
    const rfsCard = document.querySelector(".fdt-card-rfs");
    const gdrCard = document.querySelector(".fdt-card-gdr");
    const kmzCard = document.querySelector(".fdt-card-kmz");
    if (helpBtn) helpBtn.addEventListener("click", () => openHelp());
    if (fdtBtn) fdtBtn.addEventListener("click", () => openFdtCoding());
    if (fdtCard) fdtCard.addEventListener("click", () => openFdtCoding());
    if (coremanCard) coremanCard.addEventListener("click", () => openCoreman());
    if (rfsCard) rfsCard.addEventListener("click", () => openRfsDashboard());
    if (gdrCard) gdrCard.addEventListener("click", () => openGdrDashboard());
    if (kmzCard) kmzCard.addEventListener("click", () => openKmzCoords());
    window.addEventListener("message", (event) => {
      if (event.origin !== window.location.origin) return;
      if (event.data && event.data.type === "eva-close-fdt-modal") {
        closeModal("fdtCodingModal");
      }
      if (event.data && event.data.type === "eva-close-coreman-modal") {
        closeModal("coremanModal");
      }
      if (event.data && event.data.type === "eva-close-rfs-modal") {
        closeModal("rfsModal");
      }
      if (event.data && event.data.type === "eva-close-gdr-modal") {
        closeModal("gdrModal");
      }
      if (event.data && event.data.type === "eva-close-coords-modal") {
        closeModal("kmzCoordsModal");
      }
    });
  }

  function openHelp() {
    populateFolderChecklist();
    openModal("helpModal");
  }
  if (typeof window !== "undefined") window.openHelp = openHelp;

  function openFdtCoding() {
    const frame = document.getElementById("fdtCodingFrame");
    if (frame && !frame.src) frame.src = frame.dataset.src || "/fdt-coding";
    openModal("fdtCodingModal");
  }

  function openCoreman() {
    const frame = document.getElementById("coremanFrame");
    const url = frame?.dataset?.src || "/coreman";
    if (frame && !frame.src) frame.src = url;
    openModal("coremanModal");
  }

  function openRfsDashboard() {
    const frame = document.getElementById("rfsFrame");
    const url = frame?.dataset?.src || "/rfs";
    if (frame && !frame.src) frame.src = url;
    openModal("rfsModal");
  }

  function showMainPopupFeedback(title, message, severity = "info") {
    if (severity !== "warning" && severity !== "error") return null;
    if (typeof window.showNotification === "function") {
      return window.showNotification({ title, message, severity }, { timeoutMs: 5200 });
    }
    if (typeof showPopup === "function") {
      showPopup([title, message].filter(Boolean).join("\n"), true);
    }
    return null;
  }

  async function openGdrDashboard() {
    try {
      const resp = await fetch("/api/astri/status", {credentials: "include"});
      const status = await resp.json().catch(() => null);
      if (!resp.ok || !status || !status.connected) {
        showMainPopupFeedback("ASTRI", "Login ASTRI dulu dari panel utama sebelum buka GDR.", "warning");
        return;
      }
    } catch (error) {
      console.warn("ASTRI status check failed before opening GDR", error);
      showMainPopupFeedback("ASTRI", "Status ASTRI belum bisa dicek. Coba login ulang dari panel utama.", "error");
      return;
    }
    const frame = document.getElementById("gdrFrame");
    const url = frame?.dataset?.src || "/gdr";
    if (frame && !frame.src) frame.src = url;
    openModal("gdrModal");
  }

  function openAbdCad(opener) {
    const dialog = document.getElementById("abdCadModal");
    const frame = document.getElementById("abdCadFrame");
    if (!dialog || !frame || dialog.open) return;
    dialog._returnFocus = opener;
    dialog._previousOverflow = document.body.style.overflow;
    if (!frame.getAttribute("src")) frame.src = frame.dataset.src;
    dialog.showModal();
    document.body.style.overflow = "hidden";
  }

  function openKmzCoords() {
    const frame = document.getElementById("kmzCoordsFrame");
    const url = frame?.dataset?.src || "/kmz-coords";
    if (frame && !frame.src) frame.src = url;
    openModal("kmzCoordsModal");
  }

  function preloadDashboardFrames() {
    const preload = () => {
      ["fdtCodingFrame", "coremanFrame", "rfsFrame", "gdrFrame"].forEach((frameId, index) => {
        window.setTimeout(() => {
          const frame = document.getElementById(frameId);
          if (!frame || frame.src) return;
          const url = frame.dataset?.src;
          if (url) frame.src = url;
        }, index * 1400);
      });
    };
    if ("requestIdleCallback" in window) {
      window.requestIdleCallback(preload, { timeout: 1800 });
    } else {
      window.setTimeout(preload, 900);
    }
  }

  


  // ---------- Folder checklist (in help modal) ----------
  function populateFolderChecklist() {
    const grid = document.getElementById("folderChecklistGrid");
    if (!grid) return;
    const required = (typeof requiredKmlFolders !== "undefined" ? requiredKmlFolders : []);
    const optional = (typeof optionalKmlFolders !== "undefined" ? optionalKmlFolders : []);
    const items = [];
    required.forEach((f) => items.push(`<span title="Wajib"><strong>${escHtml(f)}</strong></span>`));
    optional.forEach((f) => items.push(`<span class="optional" title="Opsional">${escHtml(f)} (opsional)</span>`));
    grid.innerHTML = items.join("");
  }

  // ---------- Activity monitor ----------
  const activityState = {
    jobs: [],
    detail: null,
    metrics: null,
    counts: {},
    selectedJobId: null,
    status: "all",
    query: "",
    autoRefresh: true,
    timer: null,
    loading: false,
    lastLoadedAt: null,
  };

  function activityDrawerOpen() {
    const drawer = document.getElementById("historyDrawer");
    return Boolean(drawer && !drawer.classList.contains("hidden"));
  }

  function saveJobToHistory() {
    if (activityDrawerOpen()) fetchActivity({ silent: true });
  }

  function formatRelTime(ts) {
    const raw = Number(ts || 0);
    if (!raw) return "";
    const millis = raw < 10_000_000_000 ? raw * 1000 : raw;
    const diff = Date.now() - millis;
    if (diff < 60_000) return "baru saja";
    if (diff < 3600_000) return `${Math.max(1, Math.floor(diff / 60_000))}m lalu`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3600_000)}j lalu`;
    if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)}h lalu`;
    return new Date(millis).toLocaleDateString("id-ID");
  }

  function formatActivityDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value)) return "--";
    if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)}s`;
    const minutes = Math.floor(value / 60);
    const rest = Math.round(value % 60);
    return `${minutes}m ${rest}s`;
  }

  function activityStatusLabel(status) {
    const normalized = String(status || "unknown").toLowerCase();
    return normalized === "done" ? "Selesai"
      : normalized === "error" || normalized === "failed" ? "Gagal"
      : normalized === "cancelled" ? "Cancelled"
      : normalized === "retrying" ? "Mencoba ulang"
      : normalized === "processing" ? "Memproses"
      : normalized === "queued" ? "Antrean"
      : normalized;
  }

  function activityJobTitle(job) {
    const files = Array.isArray(job.input_files) ? job.input_files.filter(Boolean) : [];
    const kmlFile = files.find((name) => /\.(kml|kmz)$/i.test(String(name)));
    return kmlFile || files[0] || job.download_name || `Job ${job.short_id || ""}`;
  }

  function renderActivityOverview() {
    const root = document.getElementById("activityOverview");
    if (!root) return;
    const metrics = activityState.metrics || {};
    const counts = activityState.counts || {};
    const stats = [
      ["Created", metrics.jobs_created ?? 0],
      ["Selesai", metrics.jobs_completed ?? counts.done ?? 0],
      ["Active", metrics.jobs_active ?? ((counts.queued || 0) + (counts.processing || 0))],
      ["Gagal", metrics.jobs_failed ?? counts.error ?? 0],
      ["Uploaded", formatFileSize(metrics.total_bytes_uploaded || 0)],
    ];
    root.innerHTML = stats.map(([label, value]) => `
      <article class="activity-stat">
        <span>${escHtml(label)}</span>
        <strong>${escHtml(value)}</strong>
      </article>
    `).join("");
  }

  function renderActivityList() {
    const root = document.getElementById("historyList");
    if (!root) return;
    if (!activityState.jobs.length) {
      root.innerHTML = '<div class="activity-empty">Belum ada aktivitas yang cocok. Upload atau generate output, nanti job live muncul di sini.</div>';
      return;
    }
    root.innerHTML = activityState.jobs.map((job) => {
      const selected = job.job_id === activityState.selectedJobId ? " is-selected" : "";
      const warnings = Number(job.warning_count || 0);
      const errors = Number(job.error_count || 0);
      return `
        <button type="button" class="activity-job${selected}" data-activity-job="${escHtml(job.job_id)}">
          <div class="activity-job-head">
            <span class="activity-job-title">${escHtml(activityJobTitle(job))}</span>
            <span class="activity-status" data-status="${escHtml(job.status)}">${escHtml(activityStatusLabel(job.status))}</span>
          </div>
          <div class="activity-job-meta">
            <span class="activity-pill">${escHtml(job.network_type || "-")}</span>
            <span class="activity-pill">${escHtml(job.output_label || job.output_mode || "-")}</span>
            <span class="activity-pill">${escHtml(formatActivityDuration(job.duration_seconds ?? job.active_seconds))}</span>
            <span class="activity-pill">${escHtml(formatRelTime(job.updated_at || job.created_at))}</span>
            ${warnings ? `<span class="activity-pill">${warnings} warning</span>` : ""}
            ${errors ? `<span class="activity-pill">${errors} error</span>` : ""}
          </div>
          <div class="activity-progress" aria-label="Progress ${Number(job.progress || 0)}%">
            <span style="width:${Math.max(0, Math.min(100, Number(job.progress || 0)))}%"></span>
          </div>
        </button>
      `;
    }).join("");
  }

  function renderActivityDetail() {
    const root = document.getElementById("activityDetail");
    if (!root) return;
    const job = activityState.detail || activityState.jobs.find((item) => item.job_id === activityState.selectedJobId);
    if (!job) {
      root.innerHTML = '<div class="activity-empty">Pilih salah satu job untuk melihat detail.</div>';
      return;
    }
    const logs = (job.logs || []).slice(-80);
    const summary = job.standardizer_summary || {};
    const outputNames = (job.output_names || []).slice(0, 5);
    const detailCells = [
      ["Job ID", job.short_id || (job.job_id || "").slice(0, 8)],
      ["Status", activityStatusLabel(job.status)],
      ["Progress", `${Number(job.progress || 0)}%`],
      ["Durasi", formatActivityDuration(job.duration_seconds ?? job.active_seconds)],
      ["Network", job.network_type_label || job.network_type || "-"],
      ["Output", job.output_label || job.output_mode || "-"],
      ["Files", (job.input_files || []).join(", ") || "-"],
      ["Download", job.download_name || "-"],
      ["Warnings", job.warning_count || 0],
      ["Errors", job.error_count || 0],
      ["Queue", job.queue_backend || "-"],
      ["Store", job.store_backend || "-"],
    ];
    const kmzStats = [
      ["FDT", summary.fdt_renamed || 0],
      ["Names", summary.name_standardized || 0],
      ["Moved", summary.placemark_organized || 0],
      ["Cable", summary.distribution_cable_standardized || 0],
      ["Style", summary.style_standardized || 0],
    ];
    root.innerHTML = `
      <div class="activity-detail-head">
        <div>
          <h3>${escHtml(activityJobTitle(job))}</h3>
          <p>${escHtml(job.message || "Tidak ada pesan status.")}</p>
        </div>
        <span class="activity-status" data-status="${escHtml(job.status)}">${escHtml(activityStatusLabel(job.status))}</span>
      </div>
      <div class="activity-pill-row">
        <span class="activity-pill">Created ${escHtml(formatRelTime(job.created_at))}</span>
        <span class="activity-pill">Updated ${escHtml(formatRelTime(job.updated_at))}</span>
        <span class="activity-pill">${escHtml(job.task_id ? `Task ${String(job.task_id).slice(0, 8)}` : "No task id")}</span>
      </div>
      <div class="activity-detail-grid">
        ${detailCells.map(([label, value]) => `
          <div class="activity-detail-cell">
            <span>${escHtml(label)}</span>
            <strong>${escHtml(value)}</strong>
          </div>
        `).join("")}
      </div>
      ${outputNames.length ? `
        <div class="activity-pill-row">
          ${outputNames.map((name) => `<span class="activity-pill">${escHtml(name)}</span>`).join("")}
        </div>
      ` : ""}
      <div class="activity-detail-grid">
        ${kmzStats.map(([label, value]) => `
          <div class="activity-detail-cell">
            <span>KMZ ${escHtml(label)}</span>
            <strong>${escHtml(formatNumber(value))}</strong>
          </div>
        `).join("")}
      </div>
      <div class="activity-detail-actions">
        ${job.download_url ? `<a class="secondary-button" href="${escHtml(job.download_url)}" target="_blank" rel="noopener" download="${escHtml(job.download_name || "")}">Download</a>` : ""}
        <a class="secondary-button" href="/status/${escHtml(job.job_id)}" target="_blank" rel="noopener">Status JSON</a>
        <button type="button" class="secondary-button" data-activity-copy="${escHtml(job.job_id)}">Copy job id</button>
        <button type="button" class="secondary-button" data-activity-log-copy>Copy log</button>
      </div>
      <div class="activity-log">
        ${logs.length ? logs.map((log) => `
          <div class="activity-log-row" data-severity="${escHtml(log.severity || "INFO")}">
            <span>[${escHtml(log.time || "--:--:--")}]</span>
            <span>${escHtml(log.severity || "INFO")}</span>
            <span>${escHtml(log.message || "")}</span>
          </div>
        `).join("") : '<div class="activity-log-row"><span>--:--</span><span>INFO</span><span>Log belum tersedia.</span></div>'}
      </div>
    `;
  }

  function renderActivityMonitor(payload = null) {
    renderActivityOverview();
    renderActivityList();
    renderActivityDetail();
    const stamp = document.getElementById("activityLastUpdated");
    if (stamp) {
      const filtered = payload?.filtered_count ?? activityState.jobs.length;
      const timeText = activityState.lastLoadedAt ? new Date(activityState.lastLoadedAt).toLocaleTimeString("id-ID") : "--:--:--";
      stamp.textContent = `${filtered} job tampil | last refresh ${timeText} | auto refresh ${activityState.autoRefresh ? "on" : "off"}`;
    }
  }

  async function fetchActivity({ silent = false } = {}) {
    if (activityState.loading) return;
    activityState.loading = true;
    const stamp = document.getElementById("activityLastUpdated");
    if (stamp && !silent) stamp.textContent = "Loading activity...";
    const params = new URLSearchParams({
      limit: "60",
      status: activityState.status || "all",
      q: activityState.query || "",
    });
    try {
      const payload = await fetch(`/api/activity?${params.toString()}`).then(parseJsonResponse);
      activityState.jobs = payload.jobs || [];
      activityState.metrics = payload.metrics || {};
      activityState.counts = payload.counts || {};
      activityState.lastLoadedAt = Date.now();
      if (!activityState.selectedJobId || !activityState.jobs.some((job) => job.job_id === activityState.selectedJobId)) {
        activityState.selectedJobId = activityState.jobs[0]?.job_id || null;
      }
      renderActivityMonitor(payload);
      if (activityState.selectedJobId) await fetchActivityDetail(activityState.selectedJobId, { silent: true });
    } catch (err) {
      const root = document.getElementById("historyList");
      if (root) root.innerHTML = `<div class="activity-empty">Activity monitor gagal dibaca: ${escHtml(err.message)}</div>`;
      if (!silent) showToast({ title: "Activity gagal dibaca", message: err.message, tone: "error" });
    } finally {
      activityState.loading = false;
    }
  }

  async function fetchActivityDetail(jobId, { silent = false } = {}) {
    if (!jobId) {
      activityState.detail = null;
      renderActivityDetail();
      return;
    }
    try {
      activityState.detail = await fetch(`/api/activity/${encodeURIComponent(jobId)}`).then(parseJsonResponse);
      renderActivityDetail();
    } catch (err) {
      activityState.detail = null;
      renderActivityDetail();
      if (!silent) showToast({ title: "Detail job gagal dibaca", message: err.message, tone: "error" });
    }
  }

  function resetActivityTimer() {
    if (activityState.timer) {
      clearInterval(activityState.timer);
      activityState.timer = null;
    }
    if (activityState.autoRefresh && activityDrawerOpen()) {
      activityState.timer = setInterval(() => fetchActivity({ silent: true }), 5000);
    }
  }

  function openHistory() {
    openModal("historyDrawer");
    fetchActivity();
    resetActivityTimer();
  }

  function initHistory() {
    const refreshBtn = document.getElementById("activityRefreshButton");
    const autoBtn = document.getElementById("activityAutoRefreshButton");
    const statusFilter = document.getElementById("activityStatusFilter");
    const searchInput = document.getElementById("activitySearchInput");
    const listRoot = document.getElementById("historyList");
    const detailRoot = document.getElementById("activityDetail");
    if (refreshBtn) refreshBtn.addEventListener("click", () => fetchActivity());
    if (autoBtn) {
      autoBtn.addEventListener("click", () => {
        activityState.autoRefresh = !activityState.autoRefresh;
        autoBtn.dataset.active = String(activityState.autoRefresh);
        autoBtn.textContent = activityState.autoRefresh ? "Auto on" : "Auto off";
        resetActivityTimer();
      });
    }
    if (statusFilter) {
      statusFilter.addEventListener("change", () => {
        activityState.status = statusFilter.value || "all";
        activityState.selectedJobId = null;
        fetchActivity();
      });
    }
    if (searchInput) {
      let searchTimer = null;
      searchInput.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          activityState.query = searchInput.value.trim();
          activityState.selectedJobId = null;
          fetchActivity({ silent: true });
        }, 280);
      });
    }
    if (listRoot) {
      listRoot.addEventListener("click", (event) => {
        const item = event.target.closest("[data-activity-job]");
        if (!item) return;
        activityState.selectedJobId = item.getAttribute("data-activity-job");
        renderActivityList();
        fetchActivityDetail(activityState.selectedJobId);
      });
    }
    if (detailRoot) {
      detailRoot.addEventListener("click", async (event) => {
        const copyJob = event.target.closest("[data-activity-copy]");
        if (copyJob) {
          await navigator.clipboard.writeText(copyJob.getAttribute("data-activity-copy") || "");
          showToast({ title: "Job ID disalin", tone: "success", duration: 2200 });
          return;
        }
        const copyLog = event.target.closest("[data-activity-log-copy]");
        if (copyLog) {
          const logs = (activityState.detail?.logs || []).map((log) => `[${log.time || "--:--:--"}] ${log.severity || "INFO"} ${log.message || ""}`).join("\n");
          await navigator.clipboard.writeText(logs || "Log belum tersedia.");
          showToast({ title: "Log disalin", tone: "success", duration: 2200 });
        }
      });
    }
  }

  // ---------- Keyboard shortcuts ----------
  function initShortcuts() {
    document.addEventListener("keydown", (e) => {
      const isInput = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
      // Esc — close overlays
      if (e.key === "Escape") {
        closeAllOverlays();
        return;
      }
      // Ctrl/Cmd+Enter — convert
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        const form = document.getElementById("uploadForm");
        const btn = document.getElementById("convertButton");
        if (form && btn && !btn.disabled) {
          e.preventDefault();
          form.requestSubmit();
        }
        return;
      }
      // Ctrl/Cmd+H — recent jobs
      if ((e.ctrlKey || e.metaKey) && (e.key === "h" || e.key === "H")) {
        e.preventDefault();
        openHistory();
        return;
      }
      // Ctrl/Cmd+K — focus folder search
      if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
        const search = document.getElementById("kmlFolderSearch");
        if (search) {
          e.preventDefault();
          const details = document.getElementById("kmlFolderDetails");
          if (details && !details.open) details.open = true;
          search.focus();
          search.select();
        }
        return;
      }
      // ? — open help (only when not typing)
      if (!isInput && e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        openHelp();
      }
    });
  }

  // ---------- Export CSV ----------
  function csvEscape(v) {
    if (v == null) return "";
    const s = String(v);
    if (/[",\n\r;]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  }

  function exportTableToCsv(rows, filename) {
    if (!rows || !rows.length) {
      showToast({ title: "Tidak ada baris", message: "Filter ini tidak menghasilkan baris.", tone: "warning" });
      return;
    }
    const cols = Object.keys(rows[0]);
    const out = [cols.map(csvEscape).join(",")];
    rows.forEach((r) => out.push(cols.map((c) => csvEscape(r[c])).join(",")));
    const blob = new Blob(["\uFEFF" + out.join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  }

  function initExportCsv() {
    const btn = document.getElementById("boqNroExportCsvButton");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const preview = state.boqNroPreview || { columns: [], rows: [] };
      const columns = preview.columns?.length
        ? preview.columns
        : [
          { key: "N", label: "Kode Material" },
          { key: "B", label: "Description/Item" },
          { key: "D", label: "Material Qty" },
          { key: "E", label: "Service Qty" },
        ];
      const rows = preview.rows || [];
      const slim = rows.map((row) => Object.fromEntries(
        columns.map((column) => [column.label || column.key, row.values?.[column.key] ?? ""])
      ));
      exportTableToCsv(slim, `boq_nro_cluster_${Date.now()}.csv`);
    });
  }

  // ---------- Geofabrik Regions ----------
  async function loadGeofabrikRegions() {
    try {
      const response = await fetch("/api/geofabrik/regions");
      if (!response.ok) return;
      const data = await response.json();
      state.geofabrikRegions = data.regions || [];
      renderGeofabrikPanel();
      renderOsmKecamatanPanel();
    } catch (err) {
      console.warn("Failed to load Geofabrik regions:", err);
    }
  }

  function renderGeofabrikPanel() {
    if (!els.geofabrikPanel) return;
    const hasManualOsm = false;
    if (!hasManualOsm && !state.osmKecamatanRegions.length && !state.geofabrikRegions.length) {
      els.geofabrikPanel.classList.add("hidden");
      return;
    }
    els.geofabrikPanel.classList.remove("hidden");
    if (!els.geofabrikSelect) return;
    els.geofabrikSelect.innerHTML = '<option value="">-- Pilih region (optional) --</option>';
    state.geofabrikRegions.forEach((region) => {
      const option = document.createElement("option");
      option.value = region.filename;
      option.textContent = `${region.label} (${region.size_mb} MB)`;
      option.dataset.sizeMb = region.size_mb;
      els.geofabrikSelect.appendChild(option);
    });
  }


  async function loadOsmKecamatanRegions() {
    try {
      const response = await fetch("/api/osm/kecamatan");
      if (!response.ok) throw new Error("Wilayah ROAD gagal dimuat");
      const data = await response.json();
      state.osmKecamatanRegions = data.regions || [];
      state.osmKecamatanCities = data.cities || [...new Set(state.osmKecamatanRegions.map((region) => region.city || "Lainnya"))].sort();
      state.osmKecamatanProvinces = data.provinces || [...new Set(state.osmKecamatanRegions.map((region) => region.province || "Lainnya"))].sort();
      state.osmLoaded = true;
      renderOsmKecamatanPanel();
    } catch (err) {
      state.osmLoaded = true;
      console.warn("Failed to load OSM kecamatan regions:", err);
      if (els.roadSourceSummary) els.roadSourceSummary.textContent = "Wilayah ROAD gagal dimuat.";
      if (els.roadSourceHelper) els.roadSourceHelper.textContent = "Wilayah ROAD gagal dimuat. Data ROAD wajib memakai server OSM.";
      forceRoadPanelVisible();
      hideRetryBar();
      setError("Wilayah ROAD gagal dimuat. Coba muat ulang halaman.", "init");
    }
  }

  function renderOsmKecamatanPanel() {
    if (!els.osmKecamatanPanel || !els.osmKecamatanSelect || !els.osmCitySelect || !els.osmProvinceSelect) return;
    const hasManualOsm = false;
    const hasRegions = state.osmKecamatanRegions.length > 0;
    if (!state.osmLoaded) {
      if (els.roadSourceSummary) els.roadSourceSummary.textContent = "Server OSM aktif";
      if (els.roadSourceHelper) els.roadSourceHelper.textContent = "Data ROAD wajib memakai server OSM. Mengambil daftar wilayah…";
      return;
    }
    if (hasManualOsm) {
      els.geofabrikPanel?.classList.remove("hidden");
      els.osmKecamatanPanel.classList.add("hidden");
      if (els.roadSourceSummary) els.roadSourceSummary.textContent = "Server OSM aktif.";
      if (els.roadSourceHelper) {
        els.roadSourceHelper.textContent = "Data ROAD otomatis diambil dari server OSM. Pilihan Kota/Kecamatan disembunyikan untuk scope ini.";
      }
      return;
    }
    els.osmKecamatanPanel.classList.toggle("hidden", !hasRegions);
    if (hasRegions) els.geofabrikPanel?.classList.remove("hidden");
    if (!hasRegions) {
      if (els.roadSourceSummary) els.roadSourceSummary.textContent = "Server OSM aktif";
      if (els.roadSourceHelper) els.roadSourceHelper.textContent = "Data ROAD wajib memakai server OSM. Jika wilayah gagal dimuat, coba muat ulang halaman.";
      return;
    }
    const feederLike = isFeederLikeScope(state.scopeType);

    // City/Kecamatan ROAD manual intentionally disabled. Source is selected from the location popup.
    els.osmCitySelect?.closest("label")?.classList.add("hidden");
    els.osmKecamatanSelect?.closest("label")?.classList.add("hidden");
    if (els.osmKecamatanSize) els.osmKecamatanSize.classList.add("hidden");
    if (els.roadSourceSummary) els.roadSourceSummary.textContent = "ROAD OSM otomatis mengikuti Province dari popup Pilih Wilayah.";
    if (els.roadSourceHelper) els.roadSourceHelper.textContent = "Pilih wilayah saat Mulai Proses. EVA akan memakai file ROAD OSM provinsi secara otomatis.";
    els.osmKecamatanPanel.classList.remove("hidden");
    els.osmProvinceSelect?.closest("label")?.classList.add("hidden");
    return;

    // --- Province ---
    const currentProvince = state.osmProvince || state.osmKecamatanProvinces[0] || "";
    els.osmProvinceSelect.innerHTML = "";
    state.osmKecamatanProvinces.forEach((prov) => {
      const option = document.createElement("option");
      option.value = prov;
      option.textContent = prov;
      els.osmProvinceSelect.appendChild(option);
    });
    els.osmProvinceSelect.value = currentProvince;
    if (els.osmProvinceSelect.value !== currentProvince) els.osmProvinceSelect.value = state.osmKecamatanProvinces[0] || "";
    state.osmProvince = els.osmProvinceSelect.value;

    if (feederLike) {
      const provinceRegion = provinceRoadRegionFilename(state.osmProvince);
      const region = state.geofabrikRegions.find((item) => item.filename === provinceRegion);
      state.geofabrikRegion = region ? provinceRegion : "";
      state.osmCity = "";
      state.osmKecamatan = "";
      els.osmCitySelect.innerHTML = '<option value="">Nonaktif untuk Feeder/Subfeeder</option>';
      els.osmKecamatanSelect.innerHTML = '<option value="">Pakai OSM road provinsi</option>';
      els.osmCitySelect.disabled = true;
      els.osmKecamatanSelect.disabled = true;
      if (els.osmKecamatanSize) {
        els.osmKecamatanSize.textContent = region
          ? `${region.filename} | ${region.size_mb} MB | buffer ROAD 80m`
          : `File road-only provinsi belum tersedia untuk ${state.osmProvince || "provinsi ini"}.`;
      }
      if (els.roadSourceSummary) {
        els.roadSourceSummary.textContent = region
          ? `Server OSM: ${state.osmProvince} (ROAD provinsi).`
          : `Server OSM: ROAD provinsi belum tersedia untuk ${state.osmProvince || "provinsi ini"}.`;
      }
      if (els.roadSourceHelper) {
        els.roadSourceHelper.textContent = "Feeder/Subfeeder otomatis memakai data ROAD provinsi dari server OSM. Pilihan Kota/Kecamatan dinonaktifkan.";
      }
      updateConvertButtonState();
      return;
    }

    els.osmCitySelect.disabled = false;
    els.osmKecamatanSelect.disabled = false;
    state.geofabrikRegion = "";

    // --- City (filtered by province) with Auto ---
    const autoCity = "__auto__";
    const citiesForProvince = [...new Set(
      state.osmKecamatanRegions
        .filter((r) => (r.province || "Lainnya") === state.osmProvince)
        .map((r) => r.city || "Lainnya")
    )].sort();
    const currentCity = (citiesForProvince.includes(state.osmCity)) ? state.osmCity : autoCity;
    els.osmCitySelect.innerHTML = "";
    const autoCityOpt = document.createElement("option");
    autoCityOpt.value = autoCity;
    autoCityOpt.textContent = "Auto (Pakai Provinsi)";
    els.osmCitySelect.appendChild(autoCityOpt);
    citiesForProvince.forEach((city) => {
      const option = document.createElement("option");
      option.value = city;
      option.textContent = city;
      els.osmCitySelect.appendChild(option);
    });
    els.osmCitySelect.value = currentCity;
    if (els.osmCitySelect.value !== currentCity) els.osmCitySelect.value = autoCity;
    state.osmCity = els.osmCitySelect.value;

    if (state.osmCity === autoCity) {
      const provinceRegion = provinceRoadRegionFilename(state.osmProvince);
      const region = state.geofabrikRegions.find((item) => item.filename === provinceRegion);
      state.geofabrikRegion = region ? provinceRegion : "";
      state.osmKecamatan = "";
      els.osmKecamatanSelect.innerHTML = "<option value=\"\">Otomatis pakai OSM road provinsi</option>";
      els.osmKecamatanSelect.disabled = true;
      if (els.osmKecamatanSize) {
        els.osmKecamatanSize.textContent = region
          ? region.filename + " | " + region.size_mb + " MB"
          : "File road provinsi belum tersedia untuk " + (state.osmProvince || "provinsi ini") + ".";
      }
      if (els.roadSourceSummary) {
        els.roadSourceSummary.textContent = region
          ? "Server OSM: " + (state.osmProvince || "Provinsi") + " (ROAD provinsi, Auto)."
          : "Server OSM: " + (state.osmProvince || "Provinsi") + " / Auto (ROAD provinsi belum tersedia).";
      }
      if (els.roadSourceHelper) {
        els.roadSourceHelper.textContent = "Mode Auto memakai data ROAD provinsi dari server OSM. Pilih kota spesifik untuk memakai data kecamatan.";
      }
      updateConvertButtonState();
      return;
    }

    // --- Kecamatan (filtered by city) with Auto ---
    const autoKec = "__auto__";
    const filtered = state.osmKecamatanRegions.filter((region) => (region.city || "Lainnya") === state.osmCity);
    const currentKecamatan = filtered.some((r) => r.filename === state.osmKecamatan) ? state.osmKecamatan : autoKec;
    els.osmKecamatanSelect.innerHTML = "";
    els.osmKecamatanSelect.disabled = false;
    const autoKecOpt = document.createElement("option");
    autoKecOpt.value = autoKec;
    autoKecOpt.textContent = "Auto (Pakai Provinsi)";
    els.osmKecamatanSelect.appendChild(autoKecOpt);
    filtered.forEach((region) => {
      const option = document.createElement("option");
      option.value = region.filename;
      option.textContent = region.label;
      option.dataset.sizeMb = region.size_mb;
      option.dataset.cacheReady = region.road_cache_ready ? "1" : "0";
      option.dataset.city = region.city || "Lainnya";
      els.osmKecamatanSelect.appendChild(option);
    });
    els.osmKecamatanSelect.value = currentKecamatan;
    if (els.osmKecamatanSelect.value !== currentKecamatan) els.osmKecamatanSelect.value = autoKec;
    state.osmKecamatan = els.osmKecamatanSelect.value;

    if (state.osmKecamatan === autoKec) {
      const provinceRegion = provinceRoadRegionFilename(state.osmProvince);
      const region = state.geofabrikRegions.find((item) => item.filename === provinceRegion);
      state.geofabrikRegion = region ? provinceRegion : "";
      if (els.osmKecamatanSize) {
        els.osmKecamatanSize.textContent = region
          ? region.filename + " | " + region.size_mb + " MB | Auto (pakai road provinsi)"
          : "File road provinsi belum tersedia untuk " + (state.osmProvince || "provinsi ini") + ".";
      }
      if (els.roadSourceSummary) {
        els.roadSourceSummary.textContent = region
          ? "Server OSM: " + (state.osmProvince || "Provinsi") + " / " + (state.osmCity || "Kota") + " / Auto (ROAD provinsi)."
          : "Server OSM: " + (state.osmProvince || "Provinsi") + " / " + (state.osmCity || "Kota") + " / Auto (ROAD provinsi belum tersedia).";
      }
      if (els.roadSourceHelper) {
        els.roadSourceHelper.textContent = "Mode Auto memakai data ROAD provinsi dari server OSM. Pilih kecamatan spesifik untuk memakai data per kecamatan.";
      }
      updateConvertButtonState();
      return;
    }

    state.geofabrikRegion = "";
    els.osmKecamatanSelect.disabled = false;
    updateOsmKecamatanSize();
    const selected = els.osmKecamatanSelect.selectedOptions[0];
    if (els.roadSourceSummary) {
      const provLabel = state.osmProvince || "Provinsi";
      const kecamatanLabel = selected?.textContent || "Kecamatan";
      els.roadSourceSummary.textContent = "Server OSM: " + provLabel + " / " + (state.osmCity || "Kota") + " / " + kecamatanLabel + ".";
    }
    if (els.roadSourceHelper) {
      els.roadSourceHelper.textContent = "Ubah Provinsi, Kota, atau Kecamatan untuk mengganti sumber data ROAD dari server OSM.";
    }
  }

  function updateOsmKecamatanSize() {
    if (!els.osmKecamatanSize || !els.osmKecamatanSelect) return;
    const selected = els.osmKecamatanSelect.selectedOptions[0];
    if (selected && selected.dataset.sizeMb) {
      const cacheLabel = selected.dataset.cacheReady === "1" ? "cache ready" : "cache belum dibuat";
      els.osmKecamatanSize.textContent = `${selected.dataset.city || ""} | ${selected.dataset.sizeMb} MB | ${cacheLabel}`;
    } else {
      els.osmKecamatanSize.textContent = "";
    }
  }

  function onOsmProvinceChange() {
    state.osmProvince = els.osmProvinceSelect?.value || "";
    state.osmCity = "";
    state.osmKecamatan = "";
    renderOsmKecamatanPanel();
    updateConvertButtonState();
  }

  function onOsmCityChange() {
    state.osmCity = els.osmCitySelect?.value || "";
    if (state.osmCity === "__auto__") {
      state.osmKecamatan = "";
    }
    renderOsmKecamatanPanel();
    updateConvertButtonState();
  }

  function onOsmKecamatanChange() {
    state.osmKecamatan = els.osmKecamatanSelect?.value || "";
    if (state.osmKecamatan === "__auto__") {
      renderOsmKecamatanPanel();
      updateConvertButtonState();
      return;
    }
    updateOsmKecamatanSize();
    const selected = els.osmKecamatanSelect?.selectedOptions?.[0];
    state.geofabrikRegion = "";
    if (els.roadSourceSummary) {
      els.roadSourceSummary.textContent = `Server OSM: ${state.osmCity || "Kota"} / ${selected?.textContent || "Kecamatan"}.`;
    }
    updateConvertButtonState();
  }

  function updateGeofabrikSize() {
    if (!els.geofabrikSize || !els.geofabrikSelect) return;
    const selected = els.geofabrikSelect.selectedOptions[0];
    if (selected && selected.dataset.sizeMb) {
      els.geofabrikSize.textContent = `${selected.dataset.sizeMb} MB`;
    } else {
      els.geofabrikSize.textContent = "";
    }
  }

  function onGeofabrikChange() {
    state.geofabrikRegion = els.geofabrikSelect?.value || "";
    updateGeofabrikSize();
    updateConvertButtonState();
  }

  function updateConvertButtonState() {
    if (!els.convertButton) return;
    renderValidationSummary();
  }

  
// ---------- ASTRI Upload Integration ----------
var astriGlassButton = "rounded-xl border border-emerald-200/80 bg-white/70 px-3 py-2 text-xs font-bold text-emerald-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.75),0_8px_24px_rgba(16,185,129,0.10)]  transition hover:-translate-y-0.5 hover:border-sky-300 hover:bg-emerald-50 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.85),0_12px_30px_rgba(14,165,233,0.18)] active:translate-y-0";
var astriPrimaryButton = "rounded-xl border border-emerald-400/60 bg-gradient-to-b from-sky-500/85 to-cyan-600/85 px-4 py-2 text-sm font-bold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.45),0_12px_28px_rgba(8,145,178,0.26)]  transition hover:-translate-y-0.5 hover:from-emerald-500 hover:to-teal-600 active:translate-y-0";
var astriInput = "rounded-xl border border-emerald-200/80 bg-white/70 px-3 py-2 text-sm text-zinc-800 shadow-inner outline-none  transition placeholder:text-zinc-400 focus:border-sky-400 focus:bg-white/90 focus:ring-2 focus:ring-sky-200/70";
var astriClusterBtn = "rounded-xl border border-indigo-300/50 bg-gradient-to-b from-indigo-500/85 to-indigo-600/85 px-4 py-2 text-sm font-bold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_8px_24px_rgba(99,102,241,0.25)]  transition hover:-translate-y-0.5 hover:from-indigo-500 hover:to-indigo-600 active:translate-y-0";
var astriClusterGlass = "rounded-xl border border-indigo-200/70 bg-indigo-50/55 px-3 py-2 text-xs font-bold text-indigo-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.75),0_8px_24px_rgba(99,102,241,0.10)]  transition hover:-translate-y-0.5 hover:border-indigo-300 hover:bg-indigo-50/75 active:translate-y-0";
var astriSubfeederBtn = "rounded-xl border border-emerald-300/50 bg-gradient-to-b from-emerald-500/85 to-emerald-600/85 px-4 py-2 text-sm font-bold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_8px_24px_rgba(16,185,129,0.25)]  transition hover:-translate-y-0.5 hover:from-emerald-500 hover:to-emerald-600 active:translate-y-0";
var astriSubfeederGlass = "rounded-xl border border-emerald-200/70 bg-emerald-50/55 px-3 py-2 text-xs font-bold text-emerald-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.75),0_8px_24px_rgba(16,185,129,0.10)]  transition hover:-translate-y-0.5 hover:border-emerald-300 hover:bg-emerald-50/75 active:translate-y-0";
var astriFeederBtn = "rounded-xl border border-rose-300/50 bg-gradient-to-b from-rose-500/85 to-rose-600/85 px-4 py-2 text-sm font-bold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.35),0_8px_24px_rgba(244,63,94,0.25)]  transition hover:-translate-y-0.5 hover:from-rose-500 hover:to-rose-600 active:translate-y-0";
var astriFeederGlass = "rounded-xl border border-rose-200/70 bg-rose-50/55 px-3 py-2 text-xs font-bold text-rose-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.75),0_8px_24px_rgba(244,63,94,0.10)]  transition hover:-translate-y-0.5 hover:border-rose-300 hover:bg-rose-50/75 active:translate-y-0";

function wrapSupportModules(panel, dashboardCards) {
  if (!panel || !dashboardCards || document.getElementById("supportModulesPanel")) return;
  const supportPanel = document.createElement("section");
  supportPanel.id = "supportModulesPanel";
  supportPanel.className = "mt-4 overflow-hidden rounded-[22px] border border-sky-200/70 bg-white/55 p-4 shadow-[0_18px_60px_rgba(14,165,233,0.14)] backdrop-blur-2xl ring-1 ring-white/70";
  supportPanel.setAttribute("aria-labelledby", "supportModulesTitle");
  supportPanel.innerHTML = `
    <div class="mb-3">
      <h3 id="supportModulesTitle" class="text-sm font-black uppercase tracking-[0.18em] text-sky-800">Modul Pendukung</h3>
      <p class="mt-1 text-xs text-zinc-500">Akses data, monitoring, dan utilitas EVA.</p>
    </div>
  `;
  supportPanel.appendChild(dashboardCards);
  panel.insertAdjacentElement("afterend", supportPanel);
}

function orderMainWorkspace() {
  const workspace = document.querySelector(".right-workspace");
  if (!workspace) return;
  [
    "resultDashboard",
    "validationResultPanel",
    "previewWorkspacePanel",
    "astriIntegrationPanel",
    "documentAbdPanel",
    "stellaIntegrationPanel",
    "supportModulesPanel",
  ].forEach((id) => {
    const node = document.getElementById(id);
    if (node?.parentElement === workspace) workspace.appendChild(node);
  });
}

function ensureAstriPanel() {
  const existingPanel = document.getElementById("astriIntegrationPanel");
  if (!existingPanel) {
    const anchor = els.outputStatusPanel || els.resultSummaryPanel || els.previewWorkspacePanel || document.body;
    const panel = document.createElement("section");
    panel.id = "astriIntegrationPanel";
    panel.className = "mt-4 overflow-hidden rounded-[22px] border border-emerald-200/80 bg-white/70 p-4 shadow-[0_18px_60px_rgba(14,165,233,0.14)] backdrop-blur-2xl ring-1 ring-white/70";
    panel.innerHTML = `
      <div class="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <h3 class="text-sm font-black uppercase tracking-[0.18em] text-emerald-800">ASTRI Upload</h3>
          <p id="astriStatusText" class="mt-1 text-xs text-zinc-500">Belum login ASTRI.</p>
        </div>
        <button id="astriClearButton" type="button" class="${astriGlassButton}">Lepas akun</button>
      </div>
      <div id="astriLoginForm" class="mt-3 grid gap-2 md:grid-cols-[1fr_1fr_auto]">
        <input id="astriUsernameInput" type="text" class="${astriInput}" placeholder="Username ASTRI" autocomplete="username" aria-label="Username ASTRI">
        <input id="astriPasswordInput" type="password" class="${astriInput}" placeholder="Password ASTRI" autocomplete="current-password" aria-label="Password ASTRI">
        <button id="astriLoginButton" type="button" class="${astriPrimaryButton}">Login ASTRI</button>
      </div>
      <div class="mt-3 grid gap-2 md:grid-cols-[140px_140px_1fr_auto]">
        <select id="astriTargetTypeSelect" class="${astriInput}" aria-label="Scope ASTRI">
          <option value="cluster">Cluster</option>
          <option value="subfeeder">Subfeeder</option>
          <option value="feeder">Feeder</option>
        </select>
        <select id="astriStatusFilter" class="${astriInput}" aria-label="Status ASTRI">
          <option value="">Semua status</option>
          <option value="Approved 2">Approved 2</option>
          <option value="Approved 3">Approved 3</option>
          <option value="Cancelled">Cancelled</option>
          <option value="Closed">Closed</option>
          <option value="Document Uploaded">Document Uploaded</option>
          <option value="In Progress">In Progress</option>
          <option value="Need Approval">Need Approval</option>
          <option value="Need Revision">Need Revision</option>
          <option value="New">New</option>
          <option value="Successful">Successful</option>
          <option value="Unsuccessful">Unsuccessful</option>
        </select>
        <input id="astriClusterSearchInput" type="text" class="${astriInput}" placeholder="Nama/kode target ASTRI" aria-label="Cari target ASTRI">
        <button id="astriClusterSearchButton" type="button" class="${astriGlassButton}">Search</button>
      </div>
      <select id="astriClusterSelect" class="mt-2 hidden w-full ${astriInput}" aria-label="Pilih hasil pencarian ASTRI"></select>
      <div id="astriMatchBox" class="mt-3 hidden rounded-2xl border border-sky-200/70 bg-white/45 p-3 text-sm text-zinc-700 shadow-inner "></div>
      <div class="mt-3 flex flex-wrap gap-2">
        <button id="astriMatchButton" type="button" class="${astriGlassButton}">Cari Match ASTRI</button>
        <button id="astriApdConvertButton" type="button" class="${astriGlassButton}">Convert dari APD ASTRI</button>
        <button id="astriUploadButton" type="button" class="${astriGlassButton}">Upload Document</button>
        <button id="astriVerifyButton" type="button" class="${astriGlassButton}" disabled aria-disabled="true" title="Cari match ASTRI dulu.">Verify</button>
        <button id="astriUnverifyButton" type="button" class="${astriGlassButton}" disabled aria-disabled="true" title="Cari match ASTRI dulu.">Unverify</button>
        <button id="astriDownloadLatestButton" type="button" class="${astriGlassButton}">Download Document</button>
        <button id="astriScheduleButton" type="button" class="${astriGlassButton}">Approve Schedule</button>
        <button id="astriAddBoqButton" type="button" class="${astriGlassButton}">Add BOQ</button>
        <a id="astriOpenLink" class="${astriGlassButton} hidden" target="_blank" rel="noopener">Buka ASTRI</a>
      </div>

    `;
    const resultDashboard = document.getElementById("resultDashboard");
    if (resultDashboard) {
      resultDashboard.insertAdjacentElement("beforebegin", panel);
    } else {
      anchor.insertAdjacentElement("afterend", panel);
    }
    const dashboardCards = document.createElement("div");
    dashboardCards.className = "dashboard-grid mt-4";
    const cardStyle = [
      "display:flex",
      "align-items:center",
      "justify-content:center",
      "width:100%",
      "min-height:44px",
      "padding:0.75rem",
      "line-height:1",
      "cursor:pointer",
      "text-align:center",
      "border-radius:0.75rem",
      "border:1px solid rgba(186,230,253,0.75)",
      "background:rgba(255,255,255,0.55)",
      "color:#075985",
      "box-shadow:inset 0 1px 0 rgba(255,255,255,0.75),0 8px 24px rgba(16,185,129,0.10)",
      "backdrop-filter:blur(16px)",
      "-webkit-backdrop-filter:blur(16px)",
      "transition:transform 160ms ease,background 160ms ease,border-color 160ms ease,box-shadow 160ms ease",
    ].join(";") + ";";
    const titleStyle = "width:100%;font-size:0.78rem;font-weight:800;line-height:1.2;margin:0;text-align:center;color:#24B47E;letter-spacing:0.01em;";
    dashboardCards.innerHTML = `
      <button type="button" class="fdt-coding-panel fdt-card-coding" aria-label="Coding FDT database" style="${cardStyle}">
        <span style="${titleStyle}">Coding FDT</span>
      </button>
      <button type="button" class="fdt-coding-panel fdt-card-gdr" aria-label="GDR dashboard" style="${cardStyle}">
        <span style="${titleStyle}">GDR</span>
      </button>
      <button type="button" class="fdt-coding-panel fdt-card-kmz" aria-label="KMZ ke Excel koordinat" style="${cardStyle}">
        <span style="${titleStyle}">KMZ ke Excel Koordinat</span>
      </button>
      <button type="button" class="fdt-coding-panel fdt-card-stella" aria-label="Buka STELLA" style="${cardStyle}">
        <span style="${titleStyle}">STELLA</span>
      </button>
      <button type="button" class="fdt-coding-panel fdt-card-abd-cad" aria-label="Buka ABD CAD" style="${cardStyle}">
        <span style="${titleStyle}">ABD CAD</span>
      </button>
    `;
    wrapSupportModules(panel, dashboardCards);
    dashboardCards.querySelectorAll(".fdt-coding-panel").forEach((card) => {
      card.addEventListener("mouseenter", () => {
        card.style.transform = "translateY(-1px)";
        card.style.background = "rgba(240,249,255,0.78)";
        card.style.borderColor = "rgba(125,211,252,0.85)";
        card.style.boxShadow = "inset 0 1px 0 rgba(255,255,255,0.85),0 12px 30px rgba(14,165,233,0.18)";
      });
      card.addEventListener("mouseleave", () => {
        card.style.transform = "";
        card.style.background = "rgba(255,255,255,0.55)";
        card.style.borderColor = "rgba(186,230,253,0.75)";
        card.style.boxShadow = "inset 0 1px 0 rgba(255,255,255,0.75),0 8px 24px rgba(16,185,129,0.10)";
      });
    });
  }
  if (!document.querySelector(".dashboard-grid .fdt-coding-panel") && document.getElementById("astriIntegrationPanel")) {
    const panel = document.getElementById("astriIntegrationPanel");
    const dashboardCards = document.createElement("div");
    dashboardCards.className = "dashboard-grid mt-4";
    const cardStyle = [
      "display:flex",
      "align-items:center",
      "justify-content:center",
      "width:100%",
      "min-height:44px",
      "padding:0.75rem",
      "line-height:1",
      "cursor:pointer",
      "text-align:center",
      "border-radius:0.75rem",
      "border:1px solid rgba(186,230,253,0.75)",
      "background:rgba(255,255,255,0.55)",
      "color:#075985",
      "box-shadow:inset 0 1px 0 rgba(255,255,255,0.75),0 8px 24px rgba(16,185,129,0.10)",
      "backdrop-filter:blur(16px)",
      "-webkit-backdrop-filter:blur(16px)",
      "transition:transform 160ms ease,background 160ms ease,border-color 160ms ease,box-shadow 160ms ease",
    ].join(";") + ";";
    const titleStyle = "width:100%;font-size:0.78rem;font-weight:800;line-height:1.2;margin:0;text-align:center;color:#24B47E;letter-spacing:0.01em;";
    dashboardCards.innerHTML = `
      <button type="button" class="fdt-coding-panel fdt-card-coding" aria-label="Coding FDT database" style="${cardStyle}">
        <span style="${titleStyle}">Coding FDT</span>
      </button>
      <button type="button" class="fdt-coding-panel fdt-card-gdr" aria-label="GDR dashboard" style="${cardStyle}">
        <span style="${titleStyle}">GDR</span>
      </button>
      <button type="button" class="fdt-coding-panel fdt-card-kmz" aria-label="KMZ ke Excel koordinat" style="${cardStyle}">
        <span style="${titleStyle}">KMZ ke Excel Koordinat</span>
      </button>
      <button type="button" class="fdt-coding-panel fdt-card-stella" aria-label="Buka STELLA" style="${cardStyle}">
        <span style="${titleStyle}">STELLA</span>
      </button>
      <button type="button" class="fdt-coding-panel fdt-card-abd-cad" aria-label="Buka ABD CAD" style="${cardStyle}">
        <span style="${titleStyle}">ABD CAD</span>
      </button>
    `;
    wrapSupportModules(panel, dashboardCards);
    dashboardCards.querySelectorAll(".fdt-coding-panel").forEach((card) => {
      card.addEventListener("mouseenter", () => {
        card.style.transform = "translateY(-1px)";
        card.style.background = "rgba(240,249,255,0.78)";
        card.style.borderColor = "rgba(125,211,252,0.85)";
        card.style.boxShadow = "inset 0 1px 0 rgba(255,255,255,0.85),0 12px 30px rgba(14,165,233,0.18)";
      });
      card.addEventListener("mouseleave", () => {
        card.style.transform = "";
        card.style.background = "rgba(255,255,255,0.55)";
        card.style.borderColor = "rgba(186,230,253,0.75)";
        card.style.boxShadow = "inset 0 1px 0 rgba(255,255,255,0.75),0 8px 24px rgba(16,185,129,0.10)";
      });
    });
  }
  if (!document.getElementById("astriScheduleModal")) {
    const modal = document.createElement("div");
    modal.id = "astriScheduleModal";
    modal.className = "fixed inset-0 z-50 hidden overflow-y-auto bg-black/60 p-4";
    modal.innerHTML = `
      <div class="mx-auto relative max-w-6xl rounded-2xl bg-white p-4 shadow-2xl">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 class="text-lg font-bold text-zinc-900">Approve Schedule ASTRI</h3>
            <p class="text-xs text-zinc-500">Form ini mirror dari ASTRI. Submit akan dikirim langsung ke ASTRI.</p>
          </div>
          <button id="astriScheduleClose" type="button" class="${astriGlassButton}">Tutup</button>
        </div>
        <div id="astriScheduleContent" class="max-h-[75vh] overflow-y-auto rounded-xl border border-zinc-200 p-3 text-sm"></div>
      </div>
    `;
    document.body.appendChild(modal);
    document.getElementById("astriScheduleClose")?.addEventListener("click", () => modal.classList.add("hidden"));
  }
  if (!document.getElementById("astriClusterModal")) {
    const modal = document.createElement("div");
    modal.id = "astriClusterModal";
    modal.className = "fixed inset-0 z-50 hidden overflow-y-auto bg-black/60 p-4";
    modal.innerHTML = `
      <div class="mx-auto relative max-w-6xl rounded-2xl bg-white p-4 shadow-2xl">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 class="text-lg font-bold text-indigo-900">Cluster ASTRI</h3>
            <p id="astriClusterMatchInfo" class="text-xs text-zinc-500"></p>
            <div id="astriClusterBoqFeedback" class="mt-2 hidden rounded-lg border px-3 py-2 text-xs" aria-live="polite"></div>
            <p id="astriClusterDonationInfo" class="mt-1 hidden text-xs text-zinc-500"></p>
          </div>
          <button id="astriClusterModalClose" type="button" class="${astriGlassButton}">Tutup</button>
        </div>

        <div id="astriClusterContent" class="max-h-[75vh] overflow-y-auto rounded-xl border border-indigo-100 p-3 text-sm"></div>
      </div>
    `;
    document.body.appendChild(modal);
    document.getElementById("astriClusterModalClose")?.addEventListener("click", () => { modal.classList.add("hidden"); clearAstriBoqContext(); });
  }
  if (!document.getElementById("astriSubfeederModal")) {
    const modal = document.createElement("div");
    modal.id = "astriSubfeederModal";
    modal.className = "fixed inset-0 z-50 hidden overflow-y-auto bg-black/60 p-4";
    modal.innerHTML = `
      <div class="mx-auto relative max-w-6xl rounded-2xl bg-white p-4 shadow-2xl">
        <div class="mb-3 flex items-center justify-between gap-3">
          <div>
            <h3 class="text-lg font-bold text-emerald-900">Subfeeder ASTRI</h3>
            <p id="astriSubfeederMatchInfo" class="text-xs text-zinc-500"></p>
            <div id="astriSubfeederBoqFeedback" class="mt-2 hidden rounded-lg border px-3 py-2 text-xs" aria-live="polite"></div>
          </div>
          <button id="astriSubfeederModalClose" type="button" class="${astriGlassButton}">Tutup</button>
        </div>

        <div id="astriSubfeederContent" class="max-h-[75vh] overflow-y-auto rounded-xl border border-emerald-100 p-3 text-sm"></div>
      </div>
    `;
    document.body.appendChild(modal);
    document.getElementById("astriSubfeederModalClose")?.addEventListener("click", () => { modal.classList.add("hidden"); clearAstriBoqContext(); });
  }
  if (!window._astriPanelBound) {
    window._astriPanelBound = true;
    const runButtonAction = (id, label, handler) => {
      const button = document.getElementById(id);
      if (!button) return;
      button.addEventListener("click", () => runAstriButtonAction(button, label, handler));
    };
    document.getElementById("astriTargetTypeSelect")?.addEventListener("change", onAstriTargetTypeChange);
    runButtonAction("astriClusterSearchButton", "Mencari ASTRI...", searchAstriTargets);
    document.getElementById("astriClusterSearchInput")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        runAstriButtonAction(document.getElementById("astriClusterSearchButton"), "Mencari ASTRI...", searchAstriTargets);
      }
    });
    document.getElementById("astriClusterSelect")?.addEventListener("change", selectAstriSearchResult);
    runButtonAction("astriLoginButton", "Login ASTRI...", loginAstriSession);
    runButtonAction("astriClearButton", "Logout ASTRI...", clearAstriSession);
    runButtonAction("astriMatchButton", "Mencari Match ASTRI...", matchAstriCluster);
    runButtonAction("astriApdConvertButton", "Convert APD ASTRI...", convertFromAstriApd);
    runButtonAction("astriUploadButton", "Upload Document...", uploadAstriOutputs);
    runButtonAction("astriVerifyButton", "Memeriksa status dokumen...", runAstriVerifyAction);
    runButtonAction("astriUnverifyButton", "Memeriksa status dokumen...", runAstriUnverifyAction);
    runButtonAction("astriDownloadLatestButton", "Download Document...", downloadAstriLatestDocuments);
    runButtonAction("astriScheduleButton", "Membuka Approve Schedule...", () => runAstriMutableAction("schedule"));
    runButtonAction("astriAddBoqButton", "Membuka Add BOQ...", () => runAstriMutableAction("addboq"));
    document.getElementById("astriStatusFilter")?.addEventListener("change", () => {
      state.astriStatusFilterValue = document.getElementById("astriStatusFilter")?.value || "";
      runAstriButtonAction(document.getElementById("astriClusterSearchButton"), "Mencari ASTRI...", searchAstriTargets);
    });
  }
  orderMainWorkspace();
  refreshAstriPanelButtonStyles();
  syncAstriTargetTypeFromScope();
  document.getElementById("astriClusterSearchInput")?.addEventListener("input", refreshAstriActionGates);
  refreshAstriActionGates();
}

function setAstriStatus(text, connected = false) {
  const node = document.getElementById("astriStatusText");
  const loginForm = document.getElementById("astriLoginForm");
  const clearBtn = document.getElementById("astriClearButton");
  if (node) {
    node.textContent = text;
    node.className = connected ? "mt-1 text-xs font-semibold text-cyan-700" : "mt-1 text-xs text-zinc-500";
  }
  if (loginForm) {
    loginForm.classList.toggle("hidden", Boolean(connected));
  }
  if (clearBtn) {
    clearBtn.classList.toggle("hidden", !connected && !state.astriLinked);
  }
  const wasConnected = Boolean(state.astriConnected);
  state.astriConnected = Boolean(connected);
  const details = document.getElementById("astriActionsDetails");
  if (details && wasConnected !== state.astriConnected) details.open = state.astriConnected;
  refreshAstriActionGates();
}


function onAstriTargetTypeChange(event) {
  state.astriTargetType = event.currentTarget.value || "cluster";
  clearAstriMatchContext();
  state.astriSearchItems = [];
  window.evaClearInlineFeedback?.("astriInlineFeedback");
  applyAstriWorkOrderStatus(null);
  applyAstriDocumentVerificationStatus(null);
  document.getElementById("astriClusterSelect")?.classList.add("hidden");
  document.getElementById("astriMatchBox")?.classList.add("hidden");
  document.getElementById("astriOpenLink")?.classList.add("hidden");
  refreshAstriPanelButtonStyles();
}
function refreshAstriActionGates() {
  const connected = Boolean(state.astriConnected);
  const matched = Boolean(activeAstriMatchForJob());
  const job = Boolean(state.jobId);
  const canSearchDirect = state.astriTargetType === "feeder" || state.astriTargetType === "subfeeder";
  const typedTarget = canSearchDirect && Boolean(document.getElementById("astriClusterSearchInput")?.value.trim());
  const hint = document.getElementById("astriActionHint");
  if (hint) hint.textContent = !connected
    ? "Login ASTRI untuk menjalankan aksi."
    : !matched
      ? "Cari dan pilih target ASTRI sebelum menjalankan aksi. Convert dari APD dapat mencari match dari input."
      : !job
        ? "Target terpilih. Convert dari APD atau jalankan konversi untuk mengaktifkan Upload Document."
        : "Target dan job tersedia. Pilih aksi ASTRI sesuai status dokumen/WO.";
  const gates = {
    astriMatchButton: [connected && (job || typedTarget), "Login dan isi kode feeder/subfeeder, atau jalankan konversi dulu."],
    astriApdConvertButton: [connected && (matched || job || typedTarget), "Login dan cari/pilih target ASTRI dulu."],
    astriUploadButton: [connected && job, "Login dan jalankan konversi dulu."],
    astriDownloadLatestButton: [connected && matched, "Login dan pilih target ASTRI dulu."],
    astriScheduleButton: [connected && matched, "Login dan pilih target ASTRI dulu."],
    astriAddBoqButton: [connected && matched, "Login dan pilih target ASTRI dulu."],
  };
  Object.entries(gates).forEach(([id, [ready, reason]]) => {
    const button = document.getElementById(id);
    if (!button || button.dataset.astriSubmitLoading === "1" || button.dataset.astriSubmitBlocked === "1") return;
    button.disabled = !ready;
    button.setAttribute("aria-disabled", String(!ready));
    button.title = ready ? "" : reason;
  });
  applyAstriWorkOrderStatus(state.astriWorkOrderStatus);
  applyAstriDocumentVerificationStatus(state.astriDocumentVerification);
}
function refreshAstriPanelButtonStyles() {
  const targetType = state.astriTargetType || "cluster";
  const panel = document.getElementById("astriIntegrationPanel");
  if (panel) panel.dataset.astriMode = targetType;
  const btnGlass = targetType === "feeder" ? astriFeederGlass : targetType === "subfeeder" ? astriSubfeederGlass : astriClusterGlass;
  const feederStyle = "border-color:rgba(244,63,94,0.45);background:linear-gradient(180deg,rgba(255,228,230,0.95),rgba(255,241,242,0.76));color:#9f1239;box-shadow:inset 0 1px 0 rgba(255,255,255,0.8),0 8px 24px rgba(244,63,94,0.16);";
  ["astriMatchButton", "astriApdConvertButton", "astriVerifyButton", "astriUnverifyButton", "astriUploadButton", "astriDownloadLatestButton", "astriAddBoqButton", "astriScheduleButton"].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.className = btnGlass;
      if (targetType === "feeder") {
        el.style.cssText = feederStyle;
      } else {
        el.removeAttribute("style");
      }
    }
  });
  refreshAstriActionGates();
}

function clearAstriBoqContext() {
  state.astriBoqRequestId += 1;
  state.astriBoqSupplierRequestId += 1;
  state.astriBoqManualRequestId += 1;
  astriBoqSetPermit();
  state.astriPendingDonation = null;
  state.astriBoqMatch = null;
  state.astriBoqMatchJobId = null;
  state.astriBoqScope = null;
  state.astriBoqItems = [];
}

function clearAstriMatchContext() {
  document.getElementById("astriBoqResult")?.remove();
  state.astriMatch = null;
  state.astriMatchJobId = null;
  clearAstriBoqContext();
  state.astriWorkOrderStatus = null;
  state.astriDocumentVerification = null;
  refreshAstriActionGates();
}

function setCurrentJobId(jobId) {
  const nextJobId = String(jobId || "") || null;
  if (state.jobId !== nextJobId) {
    document.getElementById("astriBoqResult")?.remove();
    clearAstriBoqContext();
    state.astriWorkOrderStatusRequestId += 1;
    state.astriDocumentVerificationRequestId += 1;
    if (state.astriMatchJobId !== nextJobId) {
      state.astriMatch = null;
      state.astriMatchJobId = null;
      state.astriWorkOrderStatus = null;
      state.astriDocumentVerification = null;
    }
  }
  state.jobId = nextJobId;
  refreshAstriActionGates();
}

function astriMatchBelongsToJob(match, matchJobId, jobId = state.jobId) {
  if (!match?.matched) return false;
  const currentJobId = jobId || null;
  return currentJobId ? matchJobId === currentJobId : !matchJobId;
}

function activeAstriMatchForJob(jobId = state.jobId) {
  if (astriMatchBelongsToJob(state.astriBoqMatch, state.astriBoqMatchJobId, jobId)) return state.astriBoqMatch;
  if (astriMatchBelongsToJob(state.astriMatch, state.astriMatchJobId, jobId)) return state.astriMatch;
  return null;
}

function activeAstriScopeMatch(type, jobId = state.jobId) {
  const match = activeAstriMatchForJob(jobId);
  if (!match?.matched) return null;
  const scope = String(match.scope || match.target_type || type || "cluster").toLowerCase();
  return scope === type ? match : null;
}

async function ensureAstriJobMatch(type, jobId = state.jobId) {
  const current = activeAstriScopeMatch(type, jobId);
  if (current) return current;
  if (!jobId) return null;
  const requestJobId = jobId;
  const readContext = astriBoqReadContext(type);
  const url = type === "subfeeder"
    ? `/api/astri/subfeeder/match/${encodeURIComponent(requestJobId)}`
    : type === "feeder"
      ? `/api/astri/feeder/match/${encodeURIComponent(requestJobId)}`
      : `/api/astri/match/${encodeURIComponent(requestJobId)}`;
  const payload = await fetch(url, { credentials: "include", cache: "no-store" }).then(parseJsonResponse);
  if (!astriBoqReadCurrent(readContext)) return null;
  return payload?.matched ? payload : null;
}

function astriScopeCode(match, scope = null) {
  const selectedScope = scope || match?.scope || match?.target_type || state.astriTargetType || "cluster";
  if (selectedScope === "subfeeder") return match?.subfeeder_code || "";
  if (selectedScope === "feeder") return match?.feeder_code || match?.target_osp_route_code || "";
  return match?.cluster_code || "";
}

function astriMatchQuery(match, scope = null) {
  const selectedScope = scope || match?.scope || match?.target_type || state.astriTargetType || "cluster";
  const code = astriScopeCode(match, selectedScope);
  const query = {
    scope: selectedScope,
    work_order_number: match?.work_order_number || "",
    vendor_label: match?.vendor_label || "",
  };
  if (selectedScope === "subfeeder") query.subfeeder_code = code;
  else if (selectedScope === "feeder") {
    query.feeder_code = code;
    query.target_osp_route_code = code;
  } else query.cluster_code = code;
  return query;
}

function applyAstriDocumentVerificationStatus(status) {
  state.astriDocumentVerification = status || null;
  const available = Boolean(status?.available);
  const verifyButton = document.getElementById("astriVerifyButton");
  const unverifyButton = document.getElementById("astriUnverifyButton");
  const unverifiedCount = Number(status?.unverified_count || 0);
  const verifiedCount = Number(status?.verified_count || 0);
  const verifyEnabled = state.astriConnected && Boolean(activeAstriMatchForJob()) && available && unverifiedCount > 0;
  const unverifyEnabled = state.astriConnected && Boolean(activeAstriMatchForJob()) && available && unverifiedCount === 0 && verifiedCount > 0 && Boolean(status?.all_verified);
  const verificationLoading = verifyButton?.dataset.astriSubmitLoading === "1"
    || unverifyButton?.dataset.astriSubmitLoading === "1";
  if (verificationLoading) return;
  if (verifyButton) {
    verifyButton.hidden = false;
    verifyButton.disabled = !verifyEnabled;
    verifyButton.setAttribute("aria-disabled", verifyEnabled ? "false" : "true");
    verifyButton.title = verifyEnabled
      ? `${unverifiedCount} dokumen ASTRI belum terverifikasi.`
      : (available ? "Semua dokumen sudah terverifikasi." : "Status dokumen ASTRI belum tersedia.");
  }
  if (unverifyButton) {
    unverifyButton.hidden = false;
    unverifyButton.disabled = !unverifyEnabled;
    unverifyButton.setAttribute("aria-disabled", unverifyEnabled ? "false" : "true");
    unverifyButton.title = unverifyEnabled
      ? `${verifiedCount} dokumen ASTRI sudah terverifikasi.`
      : (available ? "Masih ada dokumen yang belum terverifikasi." : "Status dokumen ASTRI belum tersedia.");
  }
}

async function refreshAstriDocumentVerification(matchOverride = null) {
  const match = matchOverride || activeAstriMatchForJob() || {};
  const scope = match.scope || match.target_type || state.astriTargetType || "cluster";
  const code = astriScopeCode(match, scope);
  const requestId = ++state.astriDocumentVerificationRequestId;
  applyAstriDocumentVerificationStatus(null);
  if (!match.matched || !code || !match.work_order_number) return null;
  try {
    const query = new URLSearchParams(astriMatchQuery(match, scope));
    const status = await fetch(`/api/astri/document-verification-status?${query}`, {
      credentials: "include",
      cache: "no-store",
    }).then(parseJsonResponse);
    if (requestId !== state.astriDocumentVerificationRequestId) return status;
    applyAstriDocumentVerificationStatus(status);
    return status;
  } catch (error) {
    if (requestId === state.astriDocumentVerificationRequestId) {
      applyAstriDocumentVerificationStatus({ available: false, error: error.message });
      showAstriMenuFeedback("Status dokumen ASTRI gagal dicek", "Verify/Unverify dinonaktifkan sampai status semua dokumen terbaca.", "warning");
    }
    return null;
  }
}

async function runAstriVerifyAction() {
  const status = await refreshAstriDocumentVerification();
  if (!status?.available || Number(status.unverified_count || 0) < 1) {
    showAstriMenuFeedback("Verify belum siap", status?.available ? "Tidak ada dokumen yang perlu di-verify." : "Status semua dokumen belum terbaca.", "warning");
    return null;
  }
  return verifyAstriOutputs();
}

async function runAstriUnverifyAction() {
  const status = await refreshAstriDocumentVerification();
  const allVerified = Boolean(status?.all_verified)
    && Number(status?.unverified_count || 0) === 0
    && Number(status?.verified_count || 0) > 0;
  if (!status?.available || !allVerified) {
    showAstriMenuFeedback("Unverify belum siap", status?.available ? "Masih ada dokumen yang belum terverifikasi." : "Status semua dokumen belum terbaca.", "warning");
    return null;
  }
  return unverifyAstriOutputs();
}

function applyAstriWorkOrderStatus(status) {
  state.astriWorkOrderStatus = status || null;
  ["astriScheduleButton", "astriAddBoqButton"].forEach((id) => {
    const button = document.getElementById(id);
    if (!button) return;
    if (button.dataset.astriSubmitLoading === "1" || button.dataset.astriSubmitBlocked === "1") return;
    const locked = Boolean(
      !state.astriConnected
      || !activeAstriMatchForJob()
      || status?.unavailable
      || status?.successful
      || (id === "astriScheduleButton" && status?.closed)
    );
    button.disabled = locked;
    button.setAttribute("aria-disabled", locked ? "true" : "false");
    button.title = !state.astriConnected || !activeAstriMatchForJob()
      ? "Login dan pilih target ASTRI dulu."
      : locked && (status?.closed || status?.successful)
        ? `Status WO ASTRI sudah ${status?.latest_status_label || "terminal"}`
        : status?.unavailable
          ? "Status WO ASTRI belum dapat diverifikasi"
          : "";
    button.style.opacity = locked ? "0.75" : "";
    button.style.cursor = locked ? "not-allowed" : "";
  });
}

function syncAstriWorkOrderStatusUi(status, match) {
  const scope = status?.scope || match?.scope || match?.target_type || state.astriTargetType || "cluster";
  const merged = {
    ...(match || {}),
    ...(status || {}),
    matched: true,
    scope,
    target_type: status?.target_type || match?.target_type || scope,
  };
  const code = astriScopeCode(merged, scope);
  const workOrder = String(merged.work_order_number || "").trim();
  const itemIndex = (state.astriSearchItems || []).findIndex((item) => (
    astriScopeCode(item, scope).trim().toLowerCase() === code.trim().toLowerCase()
    && String(item.work_order_number || "").trim().toLowerCase() === workOrder.toLowerCase()
  ));
  if (itemIndex >= 0) {
    const item = { ...state.astriSearchItems[itemIndex], ...status };
    state.astriSearchItems[itemIndex] = item;
    const option = [...(document.getElementById("astriClusterSelect")?.options || [])]
      .find((candidate) => candidate.value === String(itemIndex));
    if (option) {
      const displayCode = item.cluster_code || item.subfeeder_code || item.feeder_code || item.target_osp_route_code || code;
      const displayName = item.cluster_name || item.subfeeder_name || item.feeder_name || "";
      option.textContent = `${displayCode} - ${displayName} | ${workOrder} | ${item.latest_status_label || item.latest_status_name || "-"}`;
    }
  }
  renderAstriMatch(merged);
  applyAstriWorkOrderStatus(status);
  syncAstriOpenModalWorkOrderStatus(status);
  return merged;
}

async function refreshAstriWorkOrderStatus({ notify = false, matchOverride = null } = {}) {
  const match = matchOverride || activeAstriMatchForJob() || {};
  const scope = match.scope || match.target_type || state.astriTargetType || "cluster";
  const scopeCode = astriScopeCode(match, scope);
  if (!match.matched || !scopeCode || !match.work_order_number) {
    applyAstriWorkOrderStatus(null);
    throw new Error("Cari dan pilih match ASTRI dulu.");
  }
  const query = new URLSearchParams(astriMatchQuery(match, scope));
  const requestId = ++state.astriWorkOrderStatusRequestId;
  const jobId = state.jobId || null;
  const matchKey = astriBoqMatchKey(match, scope);
  const isCurrent = () => jobId === (state.jobId || null)
    && matchKey === astriBoqMatchKey(activeAstriScopeMatch(scope), scope);
  try {
    const status = await fetch(`/api/astri/work-order/status?${query}`, { credentials: "include", cache: "no-store" }).then(parseJsonResponse);
    if (requestId === state.astriWorkOrderStatusRequestId && isCurrent()) syncAstriWorkOrderStatusUi(status, match);
    return status;
  } catch (error) {
    if (requestId === state.astriWorkOrderStatusRequestId && isCurrent()) {
      syncAstriWorkOrderStatusUi({
        unavailable: true,
        latest_status_label: "Tidak dapat diverifikasi",
        latest_status_name: "unavailable",
        successful: false,
        closed: false,
        boq_terminal: false,
      }, match);
    }
    error.astriStatusHandled = true;
    throw error;
  }
}

async function refreshAstriWorkOrderStatusAfterAction(matchOverride = null, expectedStatus = "") {
  const match = { ...(matchOverride || activeAstriMatchForJob() || {}) };
  if (!match.matched) return null;
  const scope = match.scope || match.target_type || state.astriTargetType || "cluster";
  const jobId = state.jobId || null;
  const matchKey = astriBoqMatchKey(match, scope);
  let requestId = state.astriWorkOrderStatusRequestId;
  const isCurrent = () => requestId === state.astriWorkOrderStatusRequestId
    && jobId === (state.jobId || null) && Boolean(matchKey)
    && matchKey === astriBoqMatchKey(activeAstriScopeMatch(scope), scope);
  let status = null;
  for (let attempt = 0; attempt < (expectedStatus ? 3 : 1); attempt += 1) {
    if (!isCurrent()) return null;
    const pending = refreshAstriWorkOrderStatus({ matchOverride: match });
    requestId = state.astriWorkOrderStatusRequestId;
    status = await pending.catch(() => null);
    if (!status || !isCurrent()) return null;
    if (!expectedStatus || status.latest_status_label === expectedStatus || status.closed || status.successful) return status;
    if (attempt < 2) await waitForAstriBoqPoll(1500);
  }
  return status;
}

function handleAstriMutationError(error, title = "Action ASTRI gagal") {
  const status = error?.payload?.wo_status;
  if (status) {
    syncAstriWorkOrderStatusUi(status, state.astriMatch || {});
    return true;
  }
  if (error?.astriStatusHandled) return true;
  if (/add boq/i.test(title)) {
    const type = state.astriBoqScope || (isAstriSubfeederTarget() ? "subfeeder" : isAstriFeederTarget() ? "feeder" : "cluster");
    showAstriBoqModalFeedback(type, title, error?.message || "Request gagal diproses.", "error", true);
  } else {
    showAstriMenuFeedback(
      title,
      error?.message || "Request gagal diproses.",
      "error",
      5200,
      true,
    );
  }
  return false;
}

function showAstriActionOpening(_action) {
  if (_action === "addboq") return null;
  const label = "Approve Schedule";
  return showAstriInlineProgress(label, "Memeriksa status WO ASTRI...");
}

function showAstriSubmitResult(title, message, severity = "success") {
  return showAstriMenuFeedback(title, message, severity, 5200, true);
}

function astriBoqModalFeedbackTarget(type) {
  const prefix = type === "subfeeder" ? "astriSubfeeder" : "astriCluster";
  let root = document.getElementById(`${prefix}BoqFeedback`);
  if (!root) {
    const matchInfo = document.getElementById(`${prefix}MatchInfo`);
    if (!matchInfo) return null;
    root = document.createElement("div");
    root.id = `${prefix}BoqFeedback`;
    root.className = "mt-2 hidden rounded-lg border px-3 py-2 text-xs";
    matchInfo.insertAdjacentElement("afterend", root);
  }
  return root;
}

function showAstriBoqModalFeedback(type, title, message, severity = "info", persistent = false) {
  const root = astriBoqModalFeedbackTarget(type);
  if (!root) return null;
  const styles = {
    success: ["border-emerald-200", "bg-emerald-50", "text-emerald-800"],
    info: ["border-sky-200", "bg-sky-50", "text-sky-800"],
    warning: ["border-amber-200", "bg-amber-50", "text-amber-800"],
    error: ["border-rose-200", "bg-rose-50", "text-rose-800"],
  };
  root.className = "mt-2 rounded-lg border px-3 py-2 text-xs shadow-sm " + (styles[severity] || styles.info).join(" ");
  root.style.removeProperty("position");
  root.style.removeProperty("right");
  root.style.removeProperty("top");
  root.style.removeProperty("z-index");
  root.style.removeProperty("max-width");
  root.setAttribute("role", severity === "error" || severity === "warning" ? "alert" : "status");
  const token = String((Number(root.dataset.astriFeedbackToken || 0) + 1));
  root.dataset.astriFeedbackToken = token;
  root.replaceChildren();
  const strong = document.createElement("strong");
  strong.textContent = title || "Info";
  const body = document.createElement("div");
  body.className = "mt-1 whitespace-pre-wrap";
  body.textContent = message || "";
  root.append(strong, body);
  root.classList.remove("hidden");
  if (!persistent) window.setTimeout(() => {
    if (root.dataset.astriFeedbackToken === token) root.classList.add("hidden");
  }, 5200);
  return {
    remove() {
      if (root.dataset.astriFeedbackToken === token) root.classList.add("hidden");
    },
    update(nextMessage) {
      if (root.dataset.astriFeedbackToken === token) body.textContent = nextMessage || "";
    },
  };
}

function showAstriBoqCardResult(type, match, succeeded, jobId) {
  const key = astriSubmitLockKey(match);
  if (state.jobId !== jobId || key !== astriSubmitLockKey(activeAstriScopeMatch(type) || {})
    || key !== astriSubmitLockKey(state.astriMatch || {})) return;
  const panel = document.getElementById("astriIntegrationPanel");
  if (!panel) return;
  const root = document.getElementById("astriBoqResult") || document.createElement("div");
  root.id = "astriBoqResult";
  root.className = "mt-2 rounded-lg border px-3 py-2 text-xs shadow-sm " + (succeeded
    ? "border-emerald-200 bg-emerald-50 text-emerald-800"
    : "border-rose-200 bg-rose-50 text-rose-800");
  root.textContent = succeeded ? "Add BOQ sukses" : "Add BOQ gagal";
  root.dataset.astriStatusKey = key;
  root.dataset.astriJobId = jobId || "";
  root.setAttribute("role", succeeded ? "status" : "alert");
  root.tabIndex = -1;
  panel.appendChild(root);
  if (succeeded) {
    const modalId = type === "subfeeder" ? "astriSubfeederModal" : "astriClusterModal";
    const modal = document.getElementById(modalId);
    if (modal?.querySelector("#astriBoqSubmitButton")?.dataset.astriStatusKey === key) {
      modal.classList.add("hidden");
      root.focus({ preventScroll: true });
      root.scrollIntoView({ block: "nearest" });
    }
  }
}

function showAstriBoqDualFeedback(type, title, message, severity = "info", persistent = false) {
  const modal = showAstriBoqModalFeedback(type, title, message, severity, persistent);
  return {
    remove() {
      modal?.remove?.();
    },
    update(nextMessage) {
      modal?.update?.(nextMessage);
    },
  };
}

function showAstriBoqDualProgress(type, title, message, persistent = false) {
  const modal = showAstriBoqModalFeedback(type, title, message, "info", persistent);
  return {
    remove() {
      modal?.remove?.();
    },
    update(nextMessage) {
      modal?.update?.(nextMessage);
    },
  };
}

function showAstriMenuFeedback(title, message, severity = "info", timeoutMs = 5200, important = false) {
  if (!important && severity !== "error") {
    return window.evaSetAstriInlineFeedback?.(title, message, severity) || null;
  }
  const notification = window.showNotification?.(
    { title, message, severity },
    { timeoutMs },
  );
  return notification || window.evaSetAstriInlineFeedback?.(title, message, severity) || null;
}

function showAstriActionProgress(title, message, persistent = false) {
  return showAstriInlineProgress(title, message, persistent);
}

async function runAstriButtonAction(button, label, action) {
  const loading = setAstriSubmitLoading(button, label);
  if (!loading) return null;
  try {
    return await action();
  } finally {
    loading.restore();
    applyAstriWorkOrderStatus(state.astriWorkOrderStatus);
    applyAstriDocumentVerificationStatus(state.astriDocumentVerification);
  }
}

const astriSubmitLocks = new Set();
const astriSubmitBlockedButtons = new Map();

function captureAstriSubmitButton(button) {
  return {
    innerHTML: button.innerHTML,
    disabled: button.disabled,
    ariaBusy: button.getAttribute("aria-busy"),
    ariaDisabled: button.getAttribute("aria-disabled"),
    title: button.getAttribute("title"),
    cursorWait: button.classList.contains("cursor-wait"),
    opacity70: button.classList.contains("opacity-70"),
  };
}

function restoreAstriSubmitButton(button, original) {
  if (!button || !original) return;
  button.innerHTML = original.innerHTML;
  button.disabled = original.disabled;
  button.classList.remove("cursor-wait", "opacity-70");
  if (original.cursorWait) button.classList.add("cursor-wait");
  if (original.opacity70) button.classList.add("opacity-70");
  if (original.ariaBusy === null) button.removeAttribute("aria-busy");
  else button.setAttribute("aria-busy", original.ariaBusy);
  if (original.ariaDisabled === null) button.removeAttribute("aria-disabled");
  else button.setAttribute("aria-disabled", original.ariaDisabled);
  if (original.title === null) button.removeAttribute("title");
  else button.setAttribute("title", original.title);
  delete button.dataset.astriSubmitLoading;
  delete button.dataset.astriSubmitBlocked;
}

function markAstriSubmitBlocked(button, key) {
  if (!button || !key || button.dataset.astriSubmitLoading === "1" || button.dataset.astriSubmitBlocked === "1") return;
  let blockedButtons = astriSubmitBlockedButtons.get(key);
  if (!blockedButtons) {
    blockedButtons = new Map();
    astriSubmitBlockedButtons.set(key, blockedButtons);
  }
  blockedButtons.set(button, captureAstriSubmitButton(button));
  button.dataset.astriSubmitBlocked = "1";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.setAttribute("aria-disabled", "true");
  button.setAttribute("title", "Proses ASTRI untuk WO ini masih berjalan.");
  button.classList.add("cursor-wait", "opacity-70");
  button.innerHTML = `<svg class="mr-1.5 inline-block h-3.5 w-3.5 animate-spin align-[-2px]" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="3" opacity=".3"></circle><path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="3" stroke-linecap="round"></path></svg><span>WO sedang diproses...</span>`;
}

function releaseAstriSubmitLock(key) {
  if (!key) return;
  astriSubmitLocks.delete(key);
  const blockedButtons = astriSubmitBlockedButtons.get(key);
  blockedButtons?.forEach((original, button) => restoreAstriSubmitButton(button, original));
  astriSubmitBlockedButtons.delete(key);
}

function lockAstriBoqSiblingControls(key, activeButton = null) {
  if (!key) return;
  document.querySelectorAll("#astriAddBoqButton, #astriBoqSubmitButton").forEach((button) => {
    if (button !== activeButton) markAstriSubmitBlocked(button, key);
  });
}

function astriSubmitLockKey(match) {
  const scope = String(match?.scope || match?.target_type || state.astriTargetType || "cluster").trim().toLowerCase();
  const code = astriScopeCode(match || {}, scope);
  const workOrder = String(match?.work_order_number || "").trim();
  return code && workOrder ? [scope, code, workOrder].join(":") : "";
}

function setAstriSubmitLoading(button, label = "Memproses...", lockKey = "") {
  const key = String(lockKey || "");
  if (button?.dataset.astriSubmitLoading === "1") return null;
  if (key && astriSubmitLocks.has(key)) {
    markAstriSubmitBlocked(button, key);
    return null;
  }
  if (key) astriSubmitLocks.add(key);
  if (!button) {
    return {
      update() {},
      restore() { releaseAstriSubmitLock(key); },
    };
  }
  const original = captureAstriSubmitButton(button);
  let restored = false;
  const update = (nextLabel) => {
    const text = String(nextLabel || "Memproses...");
    button.setAttribute("title", text);
    button.innerHTML = `<svg class="mr-1.5 inline-block h-3.5 w-3.5 animate-spin align-[-2px]" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="3" opacity=".3"></circle><path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="3" stroke-linecap="round"></path></svg><span>${text}</span>`;
  };
  button.dataset.astriSubmitLoading = "1";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.setAttribute("aria-disabled", "true");
  button.classList.add("cursor-wait", "opacity-70");
  update(label);
  return {
    update,
    restore() {
      if (restored) return;
      restored = true;
      restoreAstriSubmitButton(button, original);
      releaseAstriSubmitLock(key);
    },
  };
}

function applyAstriModalSubmitWorkOrderStatus(button, status, action) {
  if (!button || button.dataset?.astriSubmitLoading === "1" || button.dataset?.astriSubmitBlocked === "1") return;
  const locked = Boolean(
    !status
    || status?.unavailable
    || status?.successful
    || (action === "schedule" && status?.closed)
  );
  button.disabled = locked;
  button.setAttribute("aria-disabled", locked ? "true" : "false");
  button.title = locked
    ? !status || status?.unavailable
      ? "Status WO ASTRI belum dapat diverifikasi"
      : `Status WO ASTRI sudah ${status?.latest_status_label || "terminal"}`
    : "";
  button.style.opacity = locked ? "0.5" : "";
  button.style.cursor = locked ? "not-allowed" : "";
}

function astriSubmitMatchesStatusIdentity(button, status) {
  const expected = String(button?.dataset?.astriStatusKey || "");
  const actual = status ? astriSubmitLockKey(status) : "";
  return Boolean(expected && actual && expected === actual);
}

function syncAstriOpenModalWorkOrderStatus(status) {
  const scheduleButton = document.querySelector('#astriScheduleContent form button[type="submit"]');
  applyAstriModalSubmitWorkOrderStatus(
    scheduleButton,
    astriSubmitMatchesStatusIdentity(scheduleButton, status) ? status : null,
    "schedule",
  );
  document.querySelectorAll("#astriBoqSubmitButton").forEach((button) => {
    applyAstriModalSubmitWorkOrderStatus(
      button,
      astriSubmitMatchesStatusIdentity(button, status) ? status : null,
      "addboq",
    );
  });
}

function summarizeAstriBatch(results, successLabel, emptyMessage) {
  const items = Array.isArray(results) ? results : [];
  const okCount = items.filter((item) => item.ok).length;
  const skippedCount = items.filter((item) => item.skipped).length;
  const failedCount = items.filter((item) => !item.ok && !item.skipped).length;
  const summary = items.length
    ? items.map((item) => item.skipped ? `${item.target}: SKIP` : `${item.target}: ${item.ok ? successLabel : (item.error || item.status_code || "gagal")}`).join(" | ")
    : emptyMessage;
  const severity = failedCount ? "warning" : okCount === items.length && items.length ? "success" : "info";
  return { total: items.length, okCount, skippedCount, failedCount, summary, severity };
}

function summarizeAstriUploadBatch(results) {
  const requiredTargets = ["kmz", "boq"];
  const byTarget = new Map(
    (Array.isArray(results) ? results : []).map((item) => [String(item.target || "").toLowerCase(), item]),
  );
  const items = requiredTargets.map((target) => byTarget.get(target) || {
    target,
    ok: false,
    error: "Hasil upload tidak tersedia.",
  });
  const failed = items.filter((item) => !item.ok);
  const summary = items
    .map((item) => `${String(item.target).toUpperCase()}: ${item.ok ? "OK" : (item.error || item.status_code || "gagal")}`)
    .join(" | ");
  return {
    total: items.length,
    okCount: items.length - failed.length,
    skippedCount: 0,
    failedCount: failed.length,
    summary,
    severity: failed.length ? "warning" : "success",
  };
}

async function runAstriMutableAction(action) {
  const openingNotification = showAstriActionOpening(action);
  const activeMatch = activeAstriMatchForJob();
  let status;
  try {
    status = await refreshAstriWorkOrderStatus({ notify: true, matchOverride: activeMatch });
  } catch (_error) {
    openingNotification?.remove?.();
    return;
  }
  openingNotification?.remove?.();
  if (status.successful || (action === "schedule" && status.closed)) return;
  if (action === "schedule") {
    return openAstriScheduleForm();
  } else if (isAstriSubfeederTarget()) {
    return subfeederAction("addboq");
  } else if (isAstriFeederTarget()) {
    return feederAction("addboq");
  } else {
    return clusterAction("addboq");
  }
}

function syncAstriTargetTypeFromScope() {
  const astriSelect = document.getElementById("astriTargetTypeSelect");
  const scope = state.scopeType || "";
  const targetMap = { "Cluster": "cluster", "Subfeeder": "subfeeder", "Feeder": "feeder" };
  const astriValue = targetMap[scope];
  if (astriValue && astriSelect && astriSelect.value !== astriValue) {
    astriSelect.value = astriValue;
    onAstriTargetTypeChange({ currentTarget: astriSelect });
  } else {
    refreshAstriPanelButtonStyles();
  }
}
window._syncAstriTargetTypeFromScope = syncAstriTargetTypeFromScope;

function isAstriSubfeederTarget() {
  return (state.astriTargetType || document.getElementById("astriTargetTypeSelect")?.value || "cluster") === "subfeeder";
}

function isAstriFeederTarget() {
  return (state.astriTargetType || document.getElementById("astriTargetTypeSelect")?.value || "cluster") === "feeder";
}

async function searchAstriTargets() {
  const input = document.getElementById("astriClusterSearchInput");
  const select = document.getElementById("astriClusterSelect");
  const query = input?.value?.trim() || "";
  const status = document.getElementById("astriStatusFilter")?.value?.trim() || "";
  state.astriTargetType = document.getElementById("astriTargetTypeSelect")?.value || state.astriTargetType || "cluster";
  state.astriMatch = null;
  state.astriMatchJobId = null;
  clearAstriBoqContext();
  state.astriWorkOrderStatusRequestId += 1;
  state.astriDocumentVerificationRequestId += 1;
  if (!query && !status) {
    showAstriMenuFeedback("Pencarian ASTRI", "Masukkan nama/kode atau pilih Status WO.", "warning");
    return;
  }
  const progress = showAstriActionProgress("Pencarian ASTRI", "Mencari data ASTRI...");
  try {
    const url = `/api/astri/search?q=${encodeURIComponent(query)}&type=${encodeURIComponent(state.astriTargetType)}&status=${encodeURIComponent(status)}`;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 45000);
    let payload;
    try {
      payload = await fetch(url, {credentials: "include", signal: controller.signal}).then(parseJsonResponse);
    } finally {
      clearTimeout(timeoutId);
    }
    const items = payload.items || [];
    state.astriSearchItems = items;
    state.astriStatusFilterValue = status;
    progress?.remove?.();
    if (!select) {
      showAstriMenuFeedback("Pencarian ASTRI gagal", "Panel hasil pencarian tidak tersedia.", "error");
      return;
    }
    if (!items.length) {
      clearAstriMatchContext();
      applyAstriWorkOrderStatus(null);
      applyAstriDocumentVerificationStatus(null);
      select.classList.add("hidden");
      showAstriMenuFeedback("Data ASTRI tidak ditemukan", "Ubah kata kunci atau filter status WO.", "warning");
      return;
    }
    renderAstriFilteredResults(select);
    showAstriMenuFeedback(
      "Pencarian ASTRI selesai",
      `${items.length} hasil ditemukan${status ? ` untuk status ${status}` : ""}.`,
      "success",
    );
  } catch (error) {
    progress?.remove?.();
    const message = error.name === "AbortError"
      ? "Search ASTRI timeout. Pilih scope spesifik atau isi kode cluster."
      : error.message;
    showAstriMenuFeedback("Pencarian ASTRI gagal", message, "error");
  }
}

function renderAstriFilteredResults(select) {
  if (!select) select = document.getElementById("astriClusterSelect");
  const filterVal = (document.getElementById("astriStatusFilter")?.value || "").trim().toLowerCase();
  const items = state.astriSearchItems || [];
  const filtered = filterVal ? items.filter((i) => (i.latest_status_label || "").trim().toLowerCase() === filterVal) : items;
  if (!filtered.length) {
    select.classList.add("hidden");
    return;
  }
  select.innerHTML = filtered.map((item) => `<option value="${state.astriSearchItems.indexOf(item)}">${escapeHtml(item.cluster_code)} - ${escapeHtml(item.cluster_name)} | ${escapeHtml(item.work_order_number)} | ${escapeHtml(item.latest_status_label || "-")}</option>`).join("");
  select.classList.remove("hidden");
  select.value = state.astriMatch ? String(state.astriSearchItems.indexOf(state.astriMatch)) : "0";
  selectAstriSearchResult();
}

function selectAstriSearchResult() {
  const select = document.getElementById("astriClusterSelect");
  const item = state.astriSearchItems?.[Number(select?.value || 0)];
  if (!item) return;
  clearAstriBoqContext();
  state.astriMatch = item;
  state.astriMatchJobId = state.jobId || null;
  renderAstriMatch(item, state.jobId);
  const selectedStatus = {
    scope: item.target_type || state.astriTargetType || "cluster",
    cluster_code: item.cluster_code || "",
    subfeeder_code: item.subfeeder_code || "",
    feeder_code: item.feeder_code || "",
    target_osp_route_code: item.target_osp_route_code || "",
    work_order_number: item.work_order_number || "",
    latest_status_label: item.latest_status_label || "",
    latest_status_name: item.latest_status_name || "",
    successful: (item.latest_status_label || "").trim().toLowerCase() === "successful"
      && (item.latest_status_name || "").trim().toLowerCase() === "successful",
    closed: (item.latest_status_label || "").trim().toLowerCase() === "closed"
      && (item.latest_status_name || "").trim().toLowerCase() === "closed",
  };
  applyAstriWorkOrderStatus(selectedStatus);
  refreshAstriWorkOrderStatus({ matchOverride: item }).catch(() => null);
}

async function refreshAstriStatus() {
  ensureAstriPanel();
  try {
    const payload = await fetch("/api/astri/status", {credentials: "include"}).then(parseJsonResponse);
    const usernameInput = document.getElementById("astriUsernameInput");
    state.astriLinked = Boolean(payload.linked);
    if (usernameInput) {
      usernameInput.readOnly = state.astriLinked;
      if (state.astriLinked) usernameInput.value = payload.username || "";
    }
    const label = payload.connected
      ? `Terhubung: ${payload.username || "ASTRI"}`
      : state.astriLinked ? `Akun tersimpan: ${payload.username}. Sambungan belum pulih; coba lagi atau perbarui password.`
      : "Login ASTRI sekali. EVA menyimpan akun ini untuk login berikutnya.";
    setAstriStatus(label, payload.connected);
    document.getElementById("astriClearButton")?.classList.toggle("hidden", !state.astriLinked && !payload.connected);
  } catch (error) {
    setAstriStatus("Status ASTRI tidak bisa dicek.", false);
  }
}

async function loginAstriSession() {
  const username = document.getElementById("astriUsernameInput")?.value || "";
  const passwordInput = document.getElementById("astriPasswordInput");
  const password = passwordInput?.value || "";
  const progress = showAstriActionProgress("Login ASTRI", "Memverifikasi kredensial...");
  try {
    const payload = await fetch("/api/astri/login", {
      credentials: "include",
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }).then(parseJsonResponse);
    if (passwordInput) passwordInput.value = "";
    state.astriLinked = Boolean(payload.linked);
    const usernameInput = document.getElementById("astriUsernameInput");
    if (usernameInput) { usernameInput.value = payload.username || username; usernameInput.readOnly = state.astriLinked; }
    setAstriStatus(`Terhubung: ${payload.username || username}`, true);
    progress?.remove?.();
    showAstriMenuFeedback("Login ASTRI berhasil", "Akun tersimpan. Login EVA berikutnya memulihkan sesi ini.", "success");
  } catch (error) {
    progress?.remove?.();
    setAstriStatus("Login ASTRI gagal.", false);
    showAstriMenuFeedback("Login ASTRI gagal", error.message || "Kredensial atau session ASTRI tidak valid.", "error");
  }
}

async function clearAstriSession() {
  if (!confirm("Lepas akun ASTRI tersimpan? Auto-login berhenti. Job yang belum selesai memakai akun ini dapat gagal.")) return;
  const progress = showAstriActionProgress("Lepas akun ASTRI", "Menghapus tautan akun tersimpan...");
  try {
    await fetch("/api/astri/logout", {credentials: "include", method: "POST" }).then(parseJsonResponse);
    state.astriLinked = false;
    const usernameInput = document.getElementById("astriUsernameInput");
    if (usernameInput) { usernameInput.value = ""; usernameInput.readOnly = false; }
    const passwordInput = document.getElementById("astriPasswordInput");
    if (passwordInput) passwordInput.value = "";
    setAstriStatus("Login ASTRI sekali. EVA menyimpan akun ini untuk login berikutnya.", false);
    state.astriMatch = null;
    applyAstriDocumentVerificationStatus(null);
    showAstriMenuFeedback("Akun ASTRI dilepas", "Login ulang ASTRI untuk menautkan akun.", "success");
  } catch (error) {
    showAstriMenuFeedback("Lepas akun ASTRI gagal", error.message || "Tautan belum berubah. Coba lagi.", "error");
  } finally {
    progress?.remove?.();
  }
}

function renderAstriMatch(payload, jobId = state.jobId) {
  if (jobId && state.jobId && jobId !== state.jobId) return;
  if (state.astriBoqScope && astriBoqMatchKey(activeAstriScopeMatch(state.astriBoqScope), state.astriBoqScope)
    !== astriBoqMatchKey(payload, state.astriBoqScope)) clearAstriBoqContext();
  const box = document.getElementById("astriMatchBox");
  const link = document.getElementById("astriOpenLink");
  const boqResult = document.getElementById("astriBoqResult");
  if (boqResult && (boqResult.dataset.astriStatusKey !== astriSubmitLockKey(payload || {})
    || boqResult.dataset.astriJobId !== String(jobId || ""))) boqResult.remove();
  state.astriMatch = payload;
  state.astriMatchJobId = payload?.matched ? (jobId || null) : null;
  refreshAstriActionGates();
  if (!box) return;
  if (!payload?.matched) {
    box.classList.remove("hidden");
    box.innerHTML = `<strong>Match tidak ditemukan.</strong><br><span>Query: ${escapeHtml(payload?.query || "")}</span>`;
    link?.classList.add("hidden");
    applyAstriWorkOrderStatus(null);
    applyAstriDocumentVerificationStatus(null);
    return;
  }
  const statusLabel = payload.unavailable
    ? "Tidak dapat diverifikasi"
    : payload.latest_status_label || payload.latest_status_name || "-";
  const successful = Boolean(payload.successful) || (
    (payload.latest_status_label || "").trim().toLowerCase() === "successful"
    && (payload.latest_status_name || "").trim().toLowerCase() === "successful"
  );
  const closed = Boolean(payload.closed) || (
    (payload.latest_status_label || "").trim().toLowerCase() === "closed"
    && (payload.latest_status_name || "").trim().toLowerCase() === "closed"
  );
  const actionStatus = payload.unavailable
    ? "Status live belum dapat diverifikasi. Approve Schedule dan Add BOQ dikunci."
    : successful
      ? "Approve Schedule dan Add BOQ dikunci."
      : closed
        ? "Approve Schedule dikunci. Add BOQ tetap tersedia."
        : "";
  box.classList.remove("hidden");
  box.innerHTML = `
    <strong>${escapeHtml(payload.cluster_code)} - ${escapeHtml(payload.cluster_name)}</strong><br>
    <span>WO: ${escapeHtml(payload.work_order_number)}</span><br>
    <span>Status WO: <strong>${escapeHtml(statusLabel)}</strong></span><br>
    ${actionStatus ? `<span>${escapeHtml(actionStatus)}</span><br>` : ""}
    <span>Vendor: ${escapeHtml(payload.vendor_label || payload.vendor_name || "-")}</span><br>
    <span>${(payload.target_type === "feeder" ? "Route Length" : "HP")}: ${escapeHtml(payload.target_type === "feeder" ? (payload.route_length || "-") : (payload.homepass || "-"))} | Net: ${escapeHtml(payload.net_type || "-")}</span>
  `;
  if (link) {
    link.href = payload.requirement_url;
    link.classList.remove("hidden");
  }
  if (payload.latest_status_label || payload.latest_status_name) {
    applyAstriWorkOrderStatus({
      scope: payload.target_type || state.astriTargetType || "cluster",
      cluster_code: payload.cluster_code || "",
      subfeeder_code: payload.subfeeder_code || "",
      feeder_code: payload.feeder_code || "",
      target_osp_route_code: payload.target_osp_route_code || "",
      work_order_number: payload.work_order_number || "",
      latest_status_label: payload.latest_status_label || "",
      latest_status_name: payload.latest_status_name || "",
      successful,
      closed,
      unavailable: Boolean(payload.unavailable),
    });
  }
  refreshAstriDocumentVerification(payload);
}

async function matchAstriCluster() {
  const input = document.getElementById("astriClusterSearchInput");
  const isSubfeeder = isAstriSubfeederTarget();
  const isFeeder = isAstriFeederTarget();
  const currentMatch = activeAstriMatchForJob();
  if ((isSubfeeder || isFeeder) && currentMatch) {
    showAstriMenuFeedback("Match ASTRI", "Match sudah ada dari pencarian.", "info");
    return currentMatch;
  }
  // For feeder/subfeeder without jobId: search ASTRI directly using the query in search box
  if (!state.jobId && (isSubfeeder || isFeeder)) {
    const query = input?.value?.trim() || "";
    if (!query) {
      showAstriMenuFeedback("Cari Match ASTRI", "Masukkan kode feeder/subfeeder di search box, lalu klik Cari Match.", "warning");
      return null;
    }
    const progress = showAstriActionProgress("Cari Match ASTRI", "Mencari match ASTRI...");
    try {
      const searchType = isSubfeeder ? "subfeeder" : "feeder";
      const payload = await fetch(`/api/astri/search?q=${encodeURIComponent(query)}&type=${encodeURIComponent(searchType)}`).then(parseJsonResponse);
      const items = payload.items || [];
      progress?.remove?.();
      if (!items.length) {
        showAstriMenuFeedback("Match ASTRI tidak ditemukan", `Data ASTRI tidak ditemukan untuk: ${query}`, "warning");
        return null;
      }
      state.astriSearchItems = items;
      state.astriMatch = items[0];
      state.astriMatchJobId = state.jobId || null;
      renderAstriMatch(items[0], state.jobId);
      showAstriMenuFeedback("Match ASTRI ditemukan", items[0].cluster_code || query, "success");
      return items[0];
    } catch (error) {
      progress?.remove?.();
      showAstriMenuFeedback("Cari Match ASTRI gagal", error.message, "error");
      return null;
    }
  }
  if (!state.jobId) {
    showAstriMenuFeedback("Cari Match ASTRI", "Belum ada job convert.", "warning");
    return null;
  }
  const progress = showAstriActionProgress("Cari Match ASTRI", "Mencari match ASTRI...");
  try {
    const url = isSubfeeder
      ? `/api/astri/subfeeder/match/${encodeURIComponent(state.jobId)}`
      : isFeeder
        ? `/api/astri/feeder/match/${encodeURIComponent(state.jobId)}`
        : `/api/astri/match/${encodeURIComponent(state.jobId)}`;
    const jobIdAtMatch = state.jobId;
    const payload = await fetch(url).then(parseJsonResponse);
    if (state.jobId !== jobIdAtMatch) {
      progress?.remove?.();
      return null;
    }
    renderAstriMatch(payload, jobIdAtMatch);
    progress?.remove?.();
    showAstriMenuFeedback(
      payload.matched ? "Match ASTRI ditemukan" : "Match ASTRI tidak ditemukan",
      payload.matched ? "Data ASTRI siap digunakan." : "Coba periksa data hasil convert atau cari manual.",
      payload.matched ? "success" : "warning",
    );
    return payload;
  } catch (error) {
    progress?.remove?.();
    showAstriMenuFeedback("Cari Match ASTRI gagal", error.message, "error");
    return null;
  }
}

if (typeof window !== "undefined") {
  window.ensureAstriPanel = ensureAstriPanel;
  window.refreshAstriStatus = refreshAstriStatus;
  window.matchAstriCluster = matchAstriCluster;
}


async function convertFromAstriApd() {
  let match = state.astriMatch;
  if (!match?.matched) {
    match = await matchAstriCluster();
  }
  if (!match?.matched) {
    showAstriMenuFeedback("Convert dari APD ASTRI", "Cari match ASTRI dulu.", "warning");
    return;
  }
  const isSubfeeder = isAstriSubfeederTarget();
  const isFeeder = isAstriFeederTarget();
  const progress = showAstriActionProgress("Convert dari APD ASTRI", "Mengambil KMZ dari APD ASTRI lalu memulai convert...");
  try {
    const payload = await fetch("/api/astri/apd/convert", {
      credentials: "include",
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ match, subfeeder: isSubfeeder, feeder: isFeeder }),
    }).then(parseJsonResponse);
    setCurrentJobId(payload.job_id);
    renderAstriMatch(payload.match);
    progress?.remove?.();
    showAstriMenuFeedback(
      "Convert dari APD ASTRI dimulai",
      `${payload.filename || "KMZ"} masuk antrean convert. Job: ${payload.job_id}.`,
      "success",
    );
    if (typeof startPolling === "function") startPolling();
  } catch (error) {
    progress?.remove?.();
    showAstriMenuFeedback("Convert dari APD ASTRI gagal", error.message, "error");
  }
}

async function uploadAstriOutputs() {
  const isSubfeeder = isAstriSubfeederTarget();
  const isFeeder = isAstriFeederTarget();
  const activeMatch = state.astriMatch;
  if (!state.jobId) {
    showAstriMenuFeedback("Upload Document", "Belum ada job convert.", "warning");
    return;
  }
  const progress = showAstriActionProgress("Upload Document", "Mengirim dokumen ke ASTRI...");
  try {
    const targets = ["kmz", "boq"];
    const url = isSubfeeder
      ? `/api/astri/subfeeder/upload/${encodeURIComponent(state.jobId)}`
      : isFeeder
        ? `/api/astri/feeder/upload/${encodeURIComponent(state.jobId)}`
        : `/api/astri/upload/${encodeURIComponent(state.jobId)}`;
    const payload = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ targets }),
    }).then(parseJsonResponse);
    renderAstriMatch(payload.match);
    const batch = summarizeAstriUploadBatch(payload.results);
    progress?.remove?.();
    showAstriMenuFeedback(
      batch.failedCount ? "Upload Document belum lengkap" : "Upload Document selesai",
      `${batch.okCount}/${batch.total} OK. ${batch.summary}`,
      batch.severity,
      5200,
      true,
    );
  } catch (error) {
    progress?.remove?.();
    handleAstriMutationError(error);
  } finally {
    await refreshAstriWorkOrderStatusAfterAction(activeMatch);
  }
}


function filenameFromContentDisposition(value, fallback) {
  const match = String(value || "").match(/filename\*?=(?:UTF-8''|\"?)([^\";]+)/i);
  if (!match) return fallback;
  try {
    return decodeURIComponent(match[1].replace(/\"/g, "").trim()) || fallback;
  } catch (_error) {
    return match[1].replace(/\"/g, "").trim() || fallback;
  }
}

async function downloadAstriLatestDocuments() {
  if (!state.astriMatch?.matched) {
    showAstriMenuFeedback("Download Document", "Cari match ASTRI dulu.", "warning");
    return;
  }
  const isSubfeeder = isAstriSubfeederTarget();
  const isFeeder = isAstriFeederTarget();
  const progress = showAstriActionProgress("Download Document", "Menyiapkan file terbaru dari ASTRI...");
  try {
    const response = await fetch("/api/astri/latest-documents/download", {credentials: "include", 
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ match: state.astriMatch, subfeeder: isSubfeeder, feeder: isFeeder }),
    });
    if (!response.ok) {
      let payload = {};
      try { payload = await response.json(); } catch (_error) { payload = {}; }
      throw new Error(payload.error || "Download file ASTRI gagal.");
    }
    const blob = await response.blob();
    const fallbackName = `${state.astriMatch.cluster_code || "ASTRI"} - ${state.astriMatch.cluster_name || "DOCUMENTS"}.zip`;
    fallbackBlobDownload(blob, filenameFromContentDisposition(response.headers.get("Content-Disposition"), fallbackName));
    progress?.remove?.();
    showAstriMenuFeedback("Download Document dimulai", "ZIP dokumen terbaru ASTRI sedang di-download.", "success");
  } catch (error) {
    progress?.remove?.();
    showAstriMenuFeedback("Download Document gagal", error.message, "error");
  }
}


async function verifyAstriOutputs() {
  const isSubfeeder = isAstriSubfeederTarget();
  const isFeeder = isAstriFeederTarget();
  const activeMatch = state.astriMatch;
  if (!state.astriMatch?.matched) {
    showAstriMenuFeedback("Verify", "Cari match ASTRI dulu.", "warning");
    return;
  }
  const progress = showAstriActionProgress("Verify", "Memverifikasi dokumen di ASTRI...");
  try {
    let payload;
    if (isSubfeeder) {
      payload = await fetch("/api/astri/subfeeder/verify", {credentials: "include", 
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ match: state.astriMatch }),
      }).then(parseJsonResponse);
    } else if (isFeeder) {
      payload = await fetch("/api/astri/feeder/verify", {credentials: "include", 
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ match: state.astriMatch }),
      }).then(parseJsonResponse);
    } else {
      payload = await fetch("/api/astri/verify", {credentials: "include", 
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ match: state.astriMatch, targets: "all" }),
      }).then(parseJsonResponse);
    }
    renderAstriMatch(payload.match);
    await refreshAstriDocumentVerification(payload.match);
    const batch = summarizeAstriBatch(payload.results, "VERIFIED", "Tidak ada dokumen yang perlu diverifikasi.");
    progress?.remove?.();
    showAstriMenuFeedback(
      batch.failedCount ? "Verify belum lengkap" : "Verify selesai",
      `[${batch.okCount}/${batch.total}] ${batch.summary}`,
      batch.severity,
      5200,
      true,
    );
  } catch (error) {
    progress?.remove?.();
    handleAstriMutationError(error);
  } finally {
    await refreshAstriWorkOrderStatusAfterAction(activeMatch);
  }
}


async function unverifyAstriOutputs() {
  const isSubfeeder = isAstriSubfeederTarget();
  const isFeeder = isAstriFeederTarget();
  const activeMatch = state.astriMatch;
  if (!state.astriMatch?.matched) {
    showAstriMenuFeedback("Unverify", "Cari match ASTRI dulu.", "warning");
    return;
  }
  const progress = showAstriActionProgress("Unverify", "Membatalkan verifikasi dokumen di ASTRI...");
  try {
    let payload;
    if (isSubfeeder) {
      payload = await fetch("/api/astri/subfeeder/unverify", {credentials: "include", 
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ match: state.astriMatch }),
      }).then(parseJsonResponse);
    } else if (isFeeder) {
      payload = await fetch("/api/astri/feeder/unverify", {credentials: "include", 
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ match: state.astriMatch }),
      }).then(parseJsonResponse);
    } else {
      payload = await fetch("/api/astri/unverify", {credentials: "include", 
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ match: state.astriMatch, targets: "all" }),
      }).then(parseJsonResponse);
    }
    renderAstriMatch(payload.match);
    await refreshAstriDocumentVerification(payload.match);
    const batch = summarizeAstriBatch(payload.results, "UNVERIFIED", "Tidak ada dokumen yang perlu di-unverify.");
    progress?.remove?.();
    showAstriMenuFeedback(
      batch.failedCount ? "Unverify belum lengkap" : "Unverify selesai",
      `[${batch.okCount}/${batch.total}] ${batch.summary}`,
      batch.severity,
      5200,
      true,
    );
  } catch (error) {
    progress?.remove?.();
    handleAstriMutationError(error);
  } finally {
    await refreshAstriWorkOrderStatusAfterAction(activeMatch);
  }
}


async function openAstriScheduleForm() {
  const modal = document.getElementById("astriScheduleModal");
  const content = document.getElementById("astriScheduleContent");
  if (!state.astriMatch?.matched) {
    showAstriMenuFeedback("Approve Schedule", "Cari match ASTRI dulu.", "warning");
    return;
  }
  if (!modal || !content) {
    showAstriMenuFeedback("Approve Schedule gagal", "Form Approve Schedule tidak tersedia.", "error");
    return;
  }
  modal.classList.remove("hidden");
  content.innerHTML = "";
  const progress = showAstriActionProgress("Membuka Approve Schedule", "Memuat form dari ASTRI...");
  try {
    const scope = state.astriMatch?.scope || state.astriMatch?.target_type || state.astriTargetType || "cluster";
    const query = new URLSearchParams(astriMatchQuery(state.astriMatch || {}, scope));
    const url = scope === "subfeeder"
      ? `/api/astri/subfeeder/schedule/form?${query}`
      : scope === "feeder"
        ? `/api/astri/feeder/schedule/form?${query}`
        : `/api/astri/schedule/form?${query}`;
    const payload = await fetch(url).then(parseJsonResponse);
    renderAstriMatch(payload.match);
    const formHtml = "";
    const fields = payload.fields || [];
    if (!formHtml && !fields.length) {
      progress?.remove?.();
      modal.classList.add("hidden");
      showAstriMenuFeedback("Approve Schedule tidak tersedia", "Tidak ada field form dari ASTRI.", "warning");
      return;
    }
    if (formHtml) {
      content.innerHTML = formHtml;
      const form = document.getElementById("eva-astri-approve-schedule-form");
      if (!form) {
        progress?.remove?.();
        modal.classList.add("hidden");
        showAstriMenuFeedback("Approve Schedule gagal", "Form Approve Schedule tidak valid.", "error");
        return;
      }
      form.className = "grid gap-3 md:grid-cols-2";
      form.querySelectorAll("label").forEach((label) => label.classList.add("text-xs", "font-bold", "text-zinc-700"));
      form.querySelectorAll("input, select, textarea").forEach((input) => {
        input.className = `mt-1 w-full ${astriInput}`;
        if (input.hasAttribute("disabled")) input.removeAttribute("disabled");
      });
      form.insertAdjacentHTML("beforeend", `
        <div class="col-span-full mt-2 flex justify-end gap-2">
          <button type="button" class="${astriGlassButton}" onclick="document.getElementById('astriScheduleModal')?.classList.add('hidden')">Batal</button>
          <button type="submit" class="rounded-lg bg-indigo-600 px-4 py-2 text-xs font-bold text-white hover:bg-indigo-700">Submit Approve Schedule</button>
        </div>`);
      form.insertAdjacentHTML("afterbegin", `<input type="hidden" name="__eva_scope" value="${escapeHtml(state.astriMatch?.target_type || state.astriTargetType || "cluster")}"><input type="hidden" name="__eva_cluster_code" value="${escapeHtml(state.astriMatch?.cluster_code || "")}"><input type="hidden" name="__eva_work_order_number" value="${escapeHtml(state.astriMatch?.work_order_number || "")}">`);
      form.addEventListener("submit", submitAstriScheduleForm);
    } else {
      const scheduleButton = payload.button || {};
      const scheduleButtonValues = {
        "approve-drm-schedule-work-order-number-input": scheduleButton["data-work-order-number"] || "",
        "approve-drm-schedule-osp-route-code-input": scheduleButton["data-osp-route-code"] || "",
        "approve-drm-schedule-osp-route-uuid-input": scheduleButton["data-osp-route-uuid"] || "",
        "approve-drm-schedule-osp-route-name-input": scheduleButton["data-osp-route-name"] || "",
        "approve-drm-schedule-osp-route-label-input": scheduleButton["data-osp-route-label"] || "",
        "approve-drm-schedule-assigned-vendor-name-input": scheduleButton["data-assigned-vendor-name"] || "",
        "approve-drm-schedule-appointment-date-input": scheduleButton["data-appointment-date"] || "",
        "approve-drm-schedule-appointment-slot-name-input": scheduleButton["data-appointment-slot-name"] || "",
      };
      const fieldHtml = fields.map((f) => {
        const label = escapeHtml(f.label || f.id.replace(/-/g, " ").replace(/_/g, " "));
        const rawValue = f.value || scheduleButtonValues[f.id] || "";
        const val = escapeHtml(rawValue);
        if (f.hidden) return `<input type="hidden" id="${escapeHtml(f.id)}" name="${escapeHtml(f.name)}" value="${val}">`;
        if (f.type === "radio_group") {
          const options = (f.options || []).map((o) => `<button type="button" class="block w-full px-3 py-2 text-left text-xs font-medium text-zinc-800 hover:bg-emerald-50 disabled:opacity-50" data-schedule-radio-option data-value="${escapeHtml(o.value || "")}" data-relation="${escapeHtml(JSON.stringify(o.relation || {}))}"${o.disabled ? " disabled" : ""}>${escapeHtml(o.text || o.value || "")}</button>`).join("");
          const radios = (f.options || []).map((o) => `<input class="sr-only" type="radio" name="${escapeHtml(f.name)}" value="${escapeHtml(o.value || "")}"${String(o.value || "") === String(f.value || "") ? " checked" : ""}>`).join("");
          const selected = (f.options || []).find((o) => String(o.value || "") === String(f.value || ""));
          const selectedText = selected?.text || label;
          return `<div id="astri-schedule-radio-${escapeHtml(f.id)}" class="relative text-xs font-bold text-zinc-700" data-schedule-radio-group data-schedule-radio-name="${escapeHtml(f.name)}"><span class="block">${label}</span><button type="button" class="mt-1 flex w-full items-center justify-between rounded-lg border border-zinc-300 bg-white px-3 py-2 text-left text-xs font-medium text-zinc-800" data-schedule-radio-toggle aria-expanded="false"><span data-schedule-radio-current>${escapeHtml(selectedText)}</span><span aria-hidden="true">▾</span></button><div class="hidden absolute left-0 right-0 top-full z-50 mt-1 max-h-72 overflow-y-auto rounded-lg border border-zinc-300 bg-white p-1 shadow-xl" data-schedule-radio-menu><input type="search" class="mb-1 w-full rounded border border-zinc-200 px-2 py-1 text-xs font-normal" placeholder="Cari ${label}..." data-schedule-radio-search><div data-schedule-radio-options>${options}</div></div>${radios}</div>`;
        }
        if (f.type === "select") {
          const opts = (f.options || []).map((o) => `<option value="${escapeHtml(o.value)}"${o.value === f.value ? " selected" : ""}${o.disabled ? " disabled" : ""}>${escapeHtml(o.text)}</option>`).join("");
          return `<label class="text-xs font-bold text-zinc-700">${label}<select id="${escapeHtml(f.id)}" name="${escapeHtml(f.name)}" class="mt-1 w-full ${astriInput}">${opts}</select></label>`;
        }
        const readonly = f.readonly ? " readonly" : "";
        return `<label class="text-xs font-bold text-zinc-700">${label}<input id="${escapeHtml(f.id)}" type="${escapeHtml(f.type)}" name="${escapeHtml(f.name)}" value="${val}" class="mt-1 w-full ${astriInput}"${readonly} placeholder="${escapeHtml(f.placeholder)}"></label>`;
      }).join("");
      const evaStatusFields = `<input type="hidden" name="__eva_scope" value="${escapeHtml(state.astriMatch?.target_type || state.astriTargetType || "cluster")}"><input type="hidden" name="__eva_cluster_code" value="${escapeHtml(state.astriMatch?.cluster_code || "")}"><input type="hidden" name="__eva_work_order_number" value="${escapeHtml(state.astriMatch?.work_order_number || "")}">`;
      const feederOltName = scope === "feeder" ? `<input type="hidden" name="osp_route[olt_name]" value="">` : "";
      const relationPanel = scope === "feeder" ? `<div id="astriScheduleDeviceRelation" class="col-span-full hidden rounded-lg border border-emerald-200 bg-emerald-50/60 p-3 text-xs text-emerald-950" aria-live="polite"><div class="mb-2 font-bold uppercase tracking-wide text-emerald-800">ODC &amp; Site Relationship</div><div class="grid gap-2 md:grid-cols-3"><div><span class="block text-[10px] uppercase text-emerald-700">Selected Device</span><strong data-relation-field="device">-</strong></div><div><span class="block text-[10px] uppercase text-emerald-700">ODC</span><span data-relation-field="odc">-</span></div><div><span class="block text-[10px] uppercase text-emerald-700">Site</span><span data-relation-field="site">-</span></div></div></div>` : "";
      content.innerHTML = `<form id="eva-astri-approve-schedule-form" class="grid gap-3 md:grid-cols-2">${evaStatusFields}${feederOltName}${fieldHtml}${relationPanel}<div class="col-span-full mt-2 flex justify-end gap-2"><button type="button" class="${astriGlassButton}" onclick="document.getElementById('astriScheduleModal')?.classList.add('hidden')">Batal</button><button type="submit" class="rounded-lg bg-indigo-600 px-4 py-2 text-xs font-bold text-white hover:bg-indigo-700">Submit Approve Schedule</button></div></form>`;
      document.getElementById("eva-astri-approve-schedule-form")?.addEventListener("submit", submitAstriScheduleForm);
      if (scope === "feeder") {
        const form = document.getElementById("eva-astri-approve-schedule-form");
        const groups = [...(form?.querySelectorAll("[data-schedule-radio-group]") || [])];
        const typeSelect = form?.querySelector('[name="osp_route[olt_type]"]');
        const areaSelect = form?.querySelector("#approve-drm-schedule-olt-area-select");
        const hiddenOlt = form?.querySelector('[name="osp_route[olt_name]"]');
        const relationPanel = form?.querySelector("#astriScheduleDeviceRelation");
        const relationField = (key) => relationPanel?.querySelector(`[data-relation-field="${key}"]`);
        const groupIsMetro = (group) => (group?.dataset.scheduleRadioName || "").includes("metro");
        const groupFor = (type) => groups.find((group) => groupIsMetro(group) === (type === "metro"));
        const selectOption = (group, option) => {
          const value = option?.dataset.value || "";
          groups.forEach((item) => {
            const active = item === group;
            item.querySelectorAll('input[type="radio"]').forEach((input) => { input.checked = active && input.value === value; input.disabled = !active; });
            item.querySelector("[data-schedule-radio-current]")?.replaceChildren(document.createTextNode(active ? option.textContent.trim() : (groupIsMetro(item) ? "Select Metro" : "Select OLT")));
            item.querySelectorAll("[data-schedule-radio-option]").forEach((candidate) => candidate.classList.toggle("bg-emerald-100", candidate === option));
          });
          if (hiddenOlt) hiddenOlt.value = value;
          if (relationPanel) {
            let relation = {}; try { relation = JSON.parse(option?.dataset.relation || "{}"); } catch (_) {}
            relationField("device") && (relationField("device").textContent = relation.device_label || option?.textContent.trim() || value || "-");
            relationField("odc") && (relationField("odc").textContent = String(relation.site_is_odc) === "1" ? [relation.odc_code, relation.odc_label || relation.odc_name].filter(Boolean).join(" · ") : (value ? "Not assigned" : "-"));
            relationField("site") && (relationField("site").textContent = [relation.site_code, relation.site_label || relation.site_name].filter(Boolean).join(" · ") || "-");
            relationPanel.classList.toggle("hidden", !value);
          }
        };
        groups.forEach((group) => {
          const toggle = group.querySelector("[data-schedule-radio-toggle]");
          const menu = group.querySelector("[data-schedule-radio-menu]");
          const search = group.querySelector("[data-schedule-radio-search]");
          toggle?.addEventListener("click", () => { const open = menu?.classList.toggle("hidden") === false; toggle.setAttribute("aria-expanded", String(open)); if (open) search?.focus(); });
          search?.addEventListener("input", () => { const q = search.value.trim().toLowerCase(); group.querySelectorAll("[data-schedule-radio-option]").forEach((item) => item.classList.toggle("hidden", q && !item.textContent.toLowerCase().includes(q))); });
          group.querySelectorAll("[data-schedule-radio-option]").forEach((option) => option.addEventListener("click", () => { if (!option.disabled) { selectOption(group, option); menu?.classList.add("hidden"); toggle?.setAttribute("aria-expanded", "false"); } }));
        });
        const setActive = () => {
          const type = typeSelect?.value || "";
          groups.forEach((group) => { const active = type === "metro" ? groupIsMetro(group) : type === "olt" ? !groupIsMetro(group) : false; group.classList.toggle("opacity-50", !active); group.querySelectorAll('input[type="radio"]').forEach((input) => { input.disabled = !active; if (!active) input.checked = false; }); group.querySelectorAll("[data-schedule-radio-option]").forEach((option) => { option.disabled = !active || !option.dataset.value; }); });
          if (!type && hiddenOlt) hiddenOlt.value = "";
        };
        const populate = async (type, area) => { const group = groupFor(type); const options = group?.querySelector("[data-schedule-radio-options]"); if (!group || !options || !area) return; options.innerHTML = `<div class="px-3 py-2 text-xs text-zinc-500">Memuat ${type.toUpperCase()}...</div>`; try { const data = await fetch(`/api/astri/${type === "metro" ? "metro" : "olt"}-list?area=${encodeURIComponent(area)}`).then(r => r.json()); options.innerHTML = ""; (data.data || []).forEach((item) => { const option = document.createElement("button"); option.type = "button"; option.className = "block w-full px-3 py-2 text-left text-xs font-medium text-zinc-800 hover:bg-emerald-50"; option.dataset.scheduleRadioOption = ""; option.dataset.value = item.value || ""; option.dataset.relation = JSON.stringify(item.relation || {}); option.textContent = item.text || item.value || ""; option.addEventListener("click", () => { selectOption(group, option); group.querySelector("[data-schedule-radio-menu]")?.classList.add("hidden"); }); options.appendChild(option); }); if (!data.data?.length) options.innerHTML = `<div class="px-3 py-2 text-xs text-zinc-500">Tidak ada ${type.toUpperCase()} untuk area ini</div>`; } catch (_) { options.innerHTML = `<div class="px-3 py-2 text-xs text-red-600">Gagal memuat ${type.toUpperCase()}</div>`; } };
        typeSelect?.addEventListener("change", () => { setActive(); populate(typeSelect.value, areaSelect?.value); }); areaSelect?.addEventListener("change", () => populate(typeSelect?.value, areaSelect.value)); setActive(); if (typeSelect?.value && areaSelect?.value) populate(typeSelect.value, areaSelect.value);
      }
    }
    // Search textbox for large ASTRI OLT/Feeder lists. Keeps original select as
    // submitted field, while text input matches option text/code case-insensitively.
    const makeScheduleAutocomplete = (selectEl, label) => {
      if (!selectEl || selectEl.dataset.scheduleAutocomplete === "1") return;
      selectEl.dataset.scheduleAutocomplete = "1";
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = `Cari ${label}: kode atau nama...`;
      input.autocomplete = "off";
      input.className = `mt-1 w-full ${astriInput}`;
      // Original label/grid cell stays intact. Results are anchored below input.
      const fieldContainer = selectEl.closest("label") || selectEl.parentElement;
      fieldContainer.style.position = "relative";
      const menu = document.createElement("div");
      menu.className = "hidden absolute bottom-full left-0 right-0 z-50 mb-1 max-h-64 overflow-y-auto rounded-lg border border-zinc-300 bg-white py-1 shadow-xl";
      selectEl.style.display = "none";
      fieldContainer.insertBefore(input, selectEl);
      fieldContainer.appendChild(menu);
      const options = () => [...selectEl.options].filter(o => o.value && !o.disabled);
      const render = () => {
        const query = input.value.trim().toLowerCase();
        const matches = options().filter(o => o.text.trim().toLowerCase().includes(query));
        menu.innerHTML = "";
        matches.slice(0, 100).forEach((o) => {
          const row = document.createElement("button");
          row.type = "button";
          row.className = "block w-full px-3 py-2 text-left text-sm font-medium text-zinc-800 hover:bg-emerald-50 hover:text-emerald-800";
          row.textContent = o.text.trim();
          row.addEventListener("click", () => {
            selectEl.value = o.value;
            input.value = o.text.trim();
            menu.classList.add("hidden");
            selectEl.dispatchEvent(new Event("change", { bubbles: true }));
          });
          menu.appendChild(row);
        });
        menu.classList.toggle("hidden", !matches.length);
      };
      input.addEventListener("focus", render);
      input.addEventListener("input", render);
      input.addEventListener("blur", () => setTimeout(() => menu.classList.add("hidden"), 150));
      selectEl._refreshScheduleAutocomplete = () => {
        const selected = selectEl.selectedOptions?.[0];
        if (selected?.value) input.value = selected.text.trim();
        else input.value = "";
        menu.classList.add("hidden");
      };
      selectEl._refreshScheduleAutocomplete();
    };

    // Cascading dropdown: Feeder Area → Route Select (via ASTRI AJAX proxy)
    const feederAreaEl = document.getElementById("approve-drm-schedule-feeder-area-select")
      || document.querySelector("select[name*=feeder_area i]");
    const routeEl = document.getElementById("route-select")
      || document.querySelector("select[id*=route i], select[name*=route i]")
      || [...document.querySelectorAll("select")].find(el => [...el.options].some(o => /Select Feeder/i.test(o.text)));
    if (feederAreaEl && routeEl) {
      const originalRouteValue = routeEl.value;
      const loadRoutes = async (area) => {
        if (!area) return;
        const placeholder = document.createElement("option");
        placeholder.value = ""; placeholder.text = "Loading routes..."; placeholder.disabled = true;
        routeEl.innerHTML = "";
        routeEl.appendChild(placeholder);
        try {
          const res = await fetch(`/api/astri/route-list?area=${encodeURIComponent(area)}`).then(r => r.json());
          routeEl.innerHTML = "";
          const def = document.createElement("option");
          def.value = ""; def.text = "Select Feeder"; def.disabled = true; def.selected = true;
          routeEl.appendChild(def);
          (res.data || []).forEach(it => {
            const opt = document.createElement("option");
            opt.value = it.value; opt.text = it.text;
            if (it.value === originalRouteValue) opt.selected = true;
            routeEl.appendChild(opt);
          });
          if (!(res.data || []).length) {
            const na = document.createElement("option");
            na.value = ""; na.text = "Tidak ada route untuk area ini"; na.disabled = true;
            routeEl.appendChild(na);
          }
          routeEl._refreshScheduleAutocomplete?.();
        } catch (e) {
          routeEl.innerHTML = `<option disabled selected value="">Error: ${e.message}</option>`;
        }
      };
      feederAreaEl.addEventListener("change", () => loadRoutes(feederAreaEl.value));
      makeScheduleAutocomplete(routeEl, "Feeder");
      if (feederAreaEl.value) loadRoutes(feederAreaEl.value);
    }

    // Cascading dropdown: OLT Area → OLT Select (via ASTRI AJAX proxy)
    const oltAreaEl = document.getElementById("approve-drm-schedule-olt-area-select")
      || document.querySelector("select[name*=olt_area i]");
    const oltEl = document.getElementById("olt-select")
      || document.querySelector("select[id*=olt i], select[name=olt i]")
      || [...document.querySelectorAll("select")].find(el => [...el.options].some(o => /Select OLT/i.test(o.text)));
    if (oltAreaEl && oltEl) {
      const originalOltValue = oltEl.value;
      const loadOlts = async (area) => {
        if (!area) return;
        const placeholder = document.createElement("option");
        placeholder.value = ""; placeholder.text = "Loading OLTs..."; placeholder.disabled = true;
        oltEl.innerHTML = "";
        oltEl.appendChild(placeholder);
        try {
          const res = await fetch(`/api/astri/olt-list?area=${encodeURIComponent(area)}`).then(r => r.json());
          oltEl.innerHTML = "";
          const def = document.createElement("option");
          def.value = ""; def.text = "Select OLT"; def.disabled = true; def.selected = true;
          oltEl.appendChild(def);
          (res.data || []).forEach(it => {
            const opt = document.createElement("option");
            opt.value = it.value; opt.text = it.text;
            if (it.value === originalOltValue) opt.selected = true;
            oltEl.appendChild(opt);
          });
          if (!(res.data || []).length) {
            const na = document.createElement("option");
            na.value = ""; na.text = "Tidak ada OLT untuk area ini"; na.disabled = true;
            oltEl.appendChild(na);
          }
          oltEl._refreshScheduleAutocomplete?.();
        } catch (e) {
          oltEl.innerHTML = `<option disabled selected value="">Error: ${e.message}</option>`;
        }
      };
      oltAreaEl.addEventListener("change", () => loadOlts(oltAreaEl.value));
      makeScheduleAutocomplete(oltEl, "OLT");
      if (oltAreaEl.value) loadOlts(oltAreaEl.value);
    }
    const scheduleSubmitButton = content.querySelector('form button[type="submit"]');
    if (scheduleSubmitButton) {
      scheduleSubmitButton.dataset.astriStatusKey = astriSubmitLockKey(state.astriMatch || {});
    }
    progress?.remove?.();
    syncAstriOpenModalWorkOrderStatus(state.astriWorkOrderStatus);
    showAstriMenuFeedback("Approve Schedule siap", "Periksa form lalu submit ke ASTRI.", "success");
  } catch (error) {
    progress?.remove?.();
    content.innerHTML = "";
    modal.classList.add("hidden");
    showAstriMenuFeedback("Approve Schedule gagal", error.message, "error");
  }
}


async function submitAstriScheduleForm(event) {
  event.preventDefault();
  // Event.currentTarget is cleared after the first await; retain the submitted form now.
  const form = event.currentTarget;
  if (!(form instanceof HTMLFormElement)) {
    showAstriMenuFeedback("Approve Schedule gagal", "Form Approve Schedule tidak tersedia.", "error");
    return;
  }
  const isSubfeeder = isAstriSubfeederTarget();
  const isFeeder = isAstriFeederTarget();
  if (!state.astriMatch?.matched) return;
  const activeMatch = { ...state.astriMatch };
  const submitJobId = state.jobId || null;
  const scope = activeMatch.scope || activeMatch.target_type || state.astriTargetType || "cluster";
  const matchKey = astriBoqMatchKey(activeMatch, scope);
  const isCurrent = () => submitJobId === (state.jobId || null)
    && matchKey === astriBoqMatchKey(activeAstriScopeMatch(scope), scope)
    && form === document.querySelector('#astriScheduleContent form');
  const submitLoading = setAstriSubmitLoading(
    form.querySelector('button[type="submit"]'),
    "Memeriksa status...",
    astriSubmitLockKey(activeMatch),
  );
  if (!submitLoading) return;
  let progress = showAstriActionProgress("Approve Schedule", "Memeriksa status WO ASTRI...", true);
  let expectedWoStatus = "";
  try {
    const status = await refreshAstriWorkOrderStatus({ matchOverride: activeMatch });
    if (!isCurrent() || status.successful || status.closed) return;
    submitLoading.update("Mengirim Approve Schedule ke ASTRI...");
    progress?.remove?.();
    progress = showAstriActionProgress("Approve Schedule", "Mengirim Approve Schedule ke ASTRI...", true);
    const url = isSubfeeder ? "/api/astri/subfeeder/schedule/approve" : isFeeder ? "/api/astri/feeder/schedule/approve" : "/api/astri/schedule/approve";
    const payload = await fetch(url, {
      method: "POST",
      body: new FormData(form),
    }).then(parseJsonResponse);
    if (!isCurrent()) return;
    if (payload.ok) {
      expectedWoStatus = "In Progress";
      document.getElementById("astriScheduleModal")?.classList.add("hidden");
      showAstriSubmitResult("Approve Schedule selesai", "Approve Schedule berhasil dikirim ke ASTRI.");
    } else {
      const reason = payload.reason || "";
      showAstriMenuFeedback(
        "Approve Schedule gagal",
        reason || `Approve Schedule gagal (${payload.status_code || "tanpa status"}).`,
        "error",
      );
    }
  } catch (error) {
    handleAstriMutationError(error);
  } finally {
    progress?.remove?.();
    if (isCurrent()) await refreshAstriWorkOrderStatusAfterAction(activeMatch, expectedWoStatus);
    submitLoading.restore();
    applyAstriWorkOrderStatus(state.astriWorkOrderStatus);
    const scheduleSubmitButton = form.querySelector('button[type="submit"]');
    applyAstriModalSubmitWorkOrderStatus(
      scheduleSubmitButton,
      astriSubmitMatchesStatusIdentity(scheduleSubmitButton, state.astriWorkOrderStatus)
        ? state.astriWorkOrderStatus
        : null,
      "schedule",
    );
  }
}


function clusterAction(action) {
  const modal = document.getElementById("astriClusterModal");
  const matchInfo = document.getElementById("astriClusterMatchInfo");
  const donationInfo = document.getElementById("astriClusterDonationInfo");
  const content = document.getElementById("astriClusterContent");
  if (!modal || !content) return;
  if (state.astriMatch?.matched) {
    const m = state.astriMatch;
    matchInfo.textContent = `${m.cluster_code || ""} - ${m.cluster_name || ""} | WO: ${m.work_order_number || ""}`;
  } else {
    matchInfo.textContent = "Match dulu sebelum action.";
  }
  if (donationInfo) donationInfo.classList.add("hidden");
  modal.classList.remove("hidden");
  if (action === "addboq") {
    state.astriPendingDonation = null;
    const preview = openAstriBoqPreviewFor("cluster");
    const donationContext = astriBoqReadContext("cluster");
    const code = state.astriMatch?.cluster_code || "";
    if (code) {
      fetch(`/api/astri/donation?cluster_code=${encodeURIComponent(code)}`).then(r => r.json()).then(d => {
        if (!astriBoqReadCurrent(donationContext)) return;
        if (d.found) {
          // Donation vendor is only donation metadata. Never overwrite the
          // matched project vendor, because BOQ supplier map needs that vendor.
          state.astriPendingDonation = { cluster_code: code, ...d.donation };
        }
        if (d.found && donationInfo) {
          donationInfo.innerHTML = `
            <span class="font-semibold text-indigo-800">Donasi:</span>
            <span>${escapeHtml(d.donation.nominal || "-")}</span>
            <span class="text-indigo-400">|</span>
            <span>${escapeHtml(d.donation.vendor || "-")}</span>
            <span class="text-indigo-400">|</span>
            <span>${escapeHtml(d.donation.batch || "-")}</span>
            <span class="text-indigo-400">|</span>
            <span class="${d.donation.donation_status === 'DONATION PROCESS' || d.donation.donation_status === 'DONATION APPROVED' ? 'text-green-600 font-semibold' : 'text-zinc-500'}">${escapeHtml(d.donation.donation_status || "-")}</span>
          `;
          donationInfo.classList.remove("hidden");
        }
        if (d.found) {
          if (typeof d.donation.pph_pct === "number") {
            if (state.astriMatch) state.astriMatch.pph_pct = d.donation.pph_pct;
            if (state.astriBoqMatch) state.astriBoqMatch.pph_pct = d.donation.pph_pct;
          }
          const plantInput = content.querySelector("#astriBoqPlantCode");
          if (plantInput && d.donation.area_plant_code) plantInput.value = d.donation.area_plant_code;
          refreshAutoDescription("cluster", content);
        }
      }).catch(() => {});
    }
    return preview;
  }
  if (action === "schedule") { return openAstriScheduleForm(); }
  content.innerHTML = `<p class="text-sm text-zinc-500">Klik tombol action di atas untuk ${action}.</p>`;
}
function subfeederAction(action) {
  const modal = document.getElementById("astriSubfeederModal");
  const matchInfo = document.getElementById("astriSubfeederMatchInfo");
  const content = document.getElementById("astriSubfeederContent");
  if (!modal || !content) return;
  if (state.astriMatch?.matched) {
    const m = state.astriMatch;
    matchInfo.textContent = `${m.cluster_code || ""} - ${m.cluster_name || ""} | WO: ${m.work_order_number || ""}`;
  } else {
    matchInfo.textContent = "Match dulu sebelum action.";
  }
  modal.classList.remove("hidden");
  if (action === "addboq") { return openAstriBoqPreviewFor("subfeeder"); }
  if (action === "schedule") { return openAstriScheduleForm(); }
  content.innerHTML = `<p class="text-sm text-zinc-500">Klik tombol action di atas untuk ${action}.</p>`;
}

async function feederAction(action) {
  const modal = document.getElementById("astriClusterModal");
  const matchInfo = document.getElementById("astriClusterMatchInfo");
  const content = document.getElementById("astriClusterContent");
  if (!modal || !content) return;
  if (state.astriMatch?.matched) {
    const m = state.astriMatch;
    matchInfo.textContent = `${m.cluster_code || ""} - ${m.cluster_name || ""} | WO: ${m.work_order_number || ""}`;
  } else {
    matchInfo.textContent = "Cari dan pilih match dulu.";
    return;
  }
  modal.classList.remove("hidden");
  if (action === "addboq") { return openAstriBoqPreviewFor("feeder"); }
  if (action === "schedule") { return openAstriScheduleForm(); }
  content.innerHTML = `<p class="text-sm text-zinc-500">Klik tombol action di atas untuk ${action}.</p>`;
}


async function refreshAutoDescription(type, content) {
  if (!content) content = document.getElementById(type === "subfeeder" ? "astriSubfeederContent" : "astriClusterContent");
  if (!content) return;
  const autoEl = content.querySelector("#astriBoqPrAutoDesc");
  if (!autoEl) return;
  const readContext = astriBoqReadContext(type || "cluster");
  const requestId = autoEl._astriDescriptionRequestId = (autoEl._astriDescriptionRequestId || 0) + 1;
  autoEl.textContent = "Memuat deskripsi...";
  try {
    const scope = type || "cluster";
    const match = activeAstriScopeMatch(type) || {};
    const adResp = await fetch("/api/astri/boq/auto-description", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ scope, match, items: state.astriBoqItems || [], permit_rt_rw: state.astriBoqPermit?.available ? state.astriBoqPermit.value : "", donation_nominal: state.astriPendingDonation?.nominal || "", donation_vendor: state.astriPendingDonation?.vendor || "" })
    }).then(r => r.json());
    if (requestId !== autoEl._astriDescriptionRequestId || !astriBoqReadCurrent(readContext)) return;
    autoEl.textContent = adResp.description || "(auto kosong)";
  } catch(e) {
    if (requestId !== autoEl._astriDescriptionRequestId || !astriBoqReadCurrent(readContext)) return;
    autoEl.textContent = "Gagal muat auto description: " + e.message;
  }
}

function astriBoqMatchKey(match, type = "cluster") {
  const scope = String(match?.scope || match?.target_type || type).toLowerCase();
  const code = String(astriScopeCode(match, scope)).trim();
  const wo = String(match?.work_order_number || "").trim();
  if (!match?.matched || !code || !wo) return "";
  const vendor = (value) => String(value || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
  const area = scope === "feeder" ? match.osp_route_area || match.area : match.cluster_area || match.area;
  const plant = scope === "feeder" ? match.osp_route_area_plant_code || match.area_plant_code : match.cluster_area_plant_code || match.area_plant_code;
  return JSON.stringify([scope, code, wo, vendor(match.vendor_label), String(match.assigned_vendor_name || match.vendor_name || match.vendor_label || ""), String(area || ""), String(plant || ""), String(match.assigned_subcont_vendor_name || "")]);
}

function astriBoqReadContext(type = state.astriBoqScope || "cluster", match = activeAstriScopeMatch(type)) {
  return { type, jobId: state.jobId || null, key: astriBoqMatchKey(match, type), requestId: state.astriBoqRequestId };
}

function astriBoqReadCurrent(context, requireOpen = true) {
  const modal = document.getElementById(context.type === "subfeeder" ? "astriSubfeederModal" : "astriClusterModal");
  return context.requestId === state.astriBoqRequestId && context.jobId === (state.jobId || null)
    && context.type === state.astriBoqScope && context.key === astriBoqMatchKey(activeAstriScopeMatch(context.type), context.type)
    && (!requireOpen || Boolean(modal && !modal.classList.contains("hidden")));
}

function astriBoqSetPermit(permit = null, message = "0") {
  const available = permit?.source === "ASTRI" && permit.available === true
    && typeof permit.value === "string" && permit.value.trim() !== "";
  state.astriBoqPermit = { available, value: available ? permit.value : null, message };
  const content = document.getElementById(state.astriBoqScope === "subfeeder" ? "astriSubfeederContent" : "astriClusterContent");
  const field = content?.querySelector("#astriBoqPermitRtRw");
  if (field) field.textContent = available ? permit.value : message;
}

function isAstriBoqPermitItem(item) {
  const code = String(item?.code || "").trim().replace(/\.0+$/, "");
  return code === "500002155" || /\bRT\s*\/\s*RW\b|\bCOMMUNITY\s+PERMIT\b/i.test(String(item?.description || ""));
}

async function openAstriBoqPreviewFor(type) {
  state.astriBoqScope = type;
  state.astriBoqRequestId += 1;
  state.astriBoqSupplierRequestId += 1;
  state.astriBoqManualRequestId += 1;
  state.astriBoqItems = [];
  astriBoqSetPermit();
  const isSubfeeder = type === "subfeeder";
  const isFeeder = type === "feeder";
  const modalId = isSubfeeder ? "astriSubfeederModal" : "astriClusterModal";
  const modal = document.getElementById(modalId);
  const content = document.getElementById(isSubfeeder ? "astriSubfeederContent" : "astriClusterContent");
  if (!modal || !content) {
    showAstriBoqModalFeedback(type, "Add BOQ gagal", "Form Add BOQ tidak tersedia.", "error");
    return;
  }
  const otherModalId = isSubfeeder ? "astriClusterModal" : "astriSubfeederModal";
  document.getElementById(otherModalId)?.classList.add("hidden");
  const otherContent = document.getElementById(isSubfeeder ? "astriClusterContent" : "astriSubfeederContent");
  if (otherContent) otherContent.innerHTML = "";
  modal.classList.remove("hidden");
  content.innerHTML = "";
  const jobIdAtOpen = state.jobId || null;
  let jobMatch = activeAstriScopeMatch(type, jobIdAtOpen);
  const readContext = astriBoqReadContext(type, jobMatch);
  if (jobIdAtOpen && !jobMatch) {
    try {
      jobMatch = await ensureAstriJobMatch(type, jobIdAtOpen);
      if (!astriBoqReadCurrent(readContext)) return;
      if (jobMatch) {
        state.astriBoqMatch = jobMatch;
        state.astriBoqMatchJobId = jobIdAtOpen;
        readContext.key = astriBoqMatchKey(jobMatch, type);
      }
    } catch (error) {
      if (!astriBoqReadCurrent(readContext)) return;
      showAstriBoqModalFeedback(type, "Add BOQ gagal", error.message || "Match ASTRI job tidak tersedia.", "error");
      return;
    }
  }
  if (!jobIdAtOpen && !jobMatch && !activeAstriScopeMatch(type, null)) {
    showAstriBoqModalFeedback(type, "Add BOQ", "Cari match ASTRI dulu.", "warning", true);
    return;
  }
  let progress = showAstriBoqDualProgress(type, "Membuka Add BOQ", "Memuat item BOQ dari ASTRI...", true);
  try {
    let payload;
    if (state.jobId) {
      const url = isSubfeeder
        ? `/api/astri/subfeeder/boq/preview/${encodeURIComponent(state.jobId)}`
        : isFeeder
        ? `/api/astri/feeder/boq/preview/${encodeURIComponent(state.jobId)}`
        : `/api/astri/boq/preview/${encodeURIComponent(state.jobId)}`;
      const selected = jobMatch || activeAstriScopeMatch(type, jobIdAtOpen) || {};
      const query = new URLSearchParams(astriMatchQuery(selected, type));
      payload = await fetch(`${url}?${query}`).then(parseJsonResponse);
    } else if (activeAstriScopeMatch(type, null)) {
      const m = activeAstriScopeMatch(type, null);
      const scopeQuery = astriMatchQuery(m, type);
      Object.assign(scopeQuery, {
        vendor_label: m.vendor_label || "",
        assigned_vendor_name: m.assigned_vendor_name || "",
        cluster_name: m.cluster_name || "",
        subfeeder_name: m.subfeeder_name || "",
        feeder_name: m.feeder_name || "",
        area: m.area || "",
        cluster_area: m.cluster_area || m.area || "",
        osp_route_area: m.osp_route_area || m.area || "",
        area_plant_code: m.area_plant_code || "",
        cluster_area_plant_code: m.cluster_area_plant_code || m.area_plant_code || "",
        osp_route_area_plant_code: m.osp_route_area_plant_code || m.area_plant_code || "",
      });
      const endpoint = type === "feeder" ? "/api/astri/feeder/boq/preview/nojob" : type === "subfeeder" ? "/api/astri/subfeeder/boq/preview/nojob" : "/api/astri/boq/preview/nojob";
      payload = await fetch(`${endpoint}?${new URLSearchParams(scopeQuery)}`).then(parseJsonResponse);
    } else {
      progress?.remove?.();
      modal.classList.add("hidden");
      showAstriBoqModalFeedback(type, "Add BOQ", "Cari match ASTRI dulu.", "warning", true);
      return;
    }
    if (!astriBoqReadCurrent(readContext)) return;
    const responseMatch = payload.match || {};
    if (astriBoqMatchKey(responseMatch, type) !== readContext.key) throw new Error("Identitas preview BOQ berbeda dari target terpilih.");
    renderAstriMatch(responseMatch, jobIdAtOpen);
    state.astriBoqMatch = responseMatch;
    state.astriBoqMatchJobId = responseMatch?.matched ? jobIdAtOpen : null;
    let items = await mergeAstriSupplierMap(payload.items || []);
    if (!astriBoqReadCurrent(readContext)) return;
    const hasItems = items.length > 0;
    const itemRows = items.map((item, idx) => `
      <tr class="border-b border-zinc-100" data-boq-item-code="${escapeHtml(String(item.code || ""))}">
        <td class="px-2 py-1 text-center"><span data-boq-status-idx="${idx}" role="img" aria-label="Belum dikirim" title="Belum dikirim">—</span></td>
        <td class="px-2 py-1 font-mono text-xs">${escapeHtml(item.code)}</td>
        <td class="px-2 py-1">${escapeHtml(item.description)}</td>
        <td class="px-2 py-1">${escapeHtml(item.supplier || "")}</td>
        <td class="px-2 py-1 text-right"><input type="number" step="any" min="0" class="astri-boq-qty-mat w-20 text-right ${astriInput}" data-idx="${idx}" value="${escapeHtml(String(item.material_qty || 0))}"></td>
        <td class="px-2 py-1 text-right"><input type="number" step="any" min="0" class="astri-boq-qty-svc w-20 text-right ${astriInput}" data-idx="${idx}" value="${escapeHtml(String(item.service_qty || 0))}"></td>
        <td class="px-2 py-1">${escapeHtml(item.unit || "")}</td>
      </tr>`).join("");
    const submitBtnClass = isSubfeeder ? "bg-emerald-600 hover:bg-emerald-700" : "bg-indigo-600 hover:bg-indigo-700";
    content.innerHTML = `
      <div class="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div class="rounded-lg ${isSubfeeder ? "bg-emerald-50 text-emerald-800" : "bg-indigo-50 text-indigo-800"} p-3 text-sm">
          <strong>BOQ siap.</strong> File: ${escapeHtml(payload.filename || "-")} &middot; ${items.length} item
        </div>
        <label class="flex cursor-pointer items-center gap-1.5 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-xs font-semibold text-sky-700 shadow-sm transition hover:bg-sky-100">
          <svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"/></svg>
          Upload BOQ Manual
          <input id="astriBoqManualFileInput" type="file" accept=".xlsx,.xls" class="hidden">
        </label>
      </div>
      <div class="astri-boq-metadata mb-3">
        <div class="astri-boq-description">
          <div class="astri-boq-heading"><span class="text-xs font-bold text-zinc-700">PR Description</span><button type="button" id="astriCopyPrDesc" class="rounded border border-zinc-300 bg-zinc-100 px-2 py-0.5 text-[10px] font-semibold text-zinc-600 hover:bg-zinc-200 transition">📋 Copy</button></div>
          <div id="astriBoqPrAutoDesc" class="whitespace-pre-wrap rounded-lg border p-3 text-xs" style="max-height:200px;overflow:auto">Memuat...</div>
        </div>
        <div class="astri-boq-permit">
          <div class="astri-boq-heading text-xs font-bold text-zinc-700">Permit RT/RW (ASTRI)</div>
          <div id="astriBoqPermitRtRw" role="status" aria-label="Permit RT/RW (ASTRI)" class="rounded-lg border px-3 py-2 text-sm">${escapeHtml(state.astriBoqPermit?.available ? state.astriBoqPermit.value : state.astriBoqPermit?.message || '0')}</div>
        </div>
        <div class="astri-boq-wide flex flex-col gap-1.5">
          <label for="astriBoqPrDescription" class="text-xs font-semibold text-zinc-700">Override deskripsi PR <span class="font-normal text-zinc-500">(opsional)</span></label>
          <textarea id="astriBoqPrDescription" rows="2" class="min-h-16 w-full ${astriInput}" placeholder="Kosongkan untuk pakai deskripsi otomatis"></textarea>
        </div>
        <div class="astri-boq-wide flex flex-col gap-1.5">
          <label for="astriBoqAttachmentLink" class="text-xs font-semibold text-zinc-700">Link Attachment (Google Drive)</label>
          <input id="astriBoqAttachmentLink" type="url" class="w-full ${astriInput}" placeholder="https://drive.google.com/drive/folders/...">
        </div>
        <!-- Plant Code auto -->
        <input id="astriBoqPlantCode" type="hidden" value="${escapeHtml(payload.match?.area_plant_code || '')}">
      </div>
      <div class="mb-3 flex justify-end">
        <button id="astriBoqSubmitButton" type="button" class="rounded-lg ${submitBtnClass} px-4 py-2 text-xs font-bold text-white">Submit Add BOQ</button>
      </div>
      <div class="overflow-x-auto rounded-lg border border-zinc-200">
        <table class="min-w-full text-xs">
          <thead class="bg-zinc-50 text-left text-zinc-600"><tr><th scope="col" class="px-2 py-2 text-center">Status</th><th class="px-2 py-2">Code</th><th class="px-2 py-2">Description</th><th class="px-2 py-2">Supplier</th><th class="px-2 py-2 text-right">Material</th><th class="px-2 py-2 text-right">Service</th><th class="px-2 py-2">Unit</th></tr></thead>
          <tbody id="astriBoqTableBody">${itemRows || '<tr><td colspan="7" class="p-3 text-zinc-500">Tidak ada item BOQ terisi.</td></tr>'}</tbody>
        </table>
      </div>`;
    const boqSubmitButton = content.querySelector("#astriBoqSubmitButton");
    if (boqSubmitButton) {
      boqSubmitButton.dataset.astriStatusKey = astriSubmitLockKey(payload.match || state.astriMatch || {});
      boqSubmitButton.addEventListener("click", () => submitAstriBoq(state.astriBoqItems || [], type));
    }
    syncAstriOpenModalWorkOrderStatus(state.astriWorkOrderStatus);
    content.querySelector("#astriCopyPrDesc")?.addEventListener("click", () => {
      const autoDesc = content.querySelector("#astriBoqPrAutoDesc")?.textContent || "";
      const overrideDesc = content.querySelector("#astriBoqPrDescription")?.value || "";
      const link = content.querySelector("#astriBoqAttachmentLink")?.value || "";
      let full = overrideDesc || autoDesc;
      if (link) full += "\n\nLink Attachment :\n" + link;
      navigator.clipboard.writeText(full).then(() => {
        const btn = content.querySelector("#astriCopyPrDesc");
        if (btn) { btn.textContent = "✅ Copied!"; setTimeout(() => btn.textContent = "📋 Copy", 1500); }
      });
    });
    // Auto-refresh description when qty changes (debounced)
    let _descTimer = null;
    content.addEventListener("input", (e) => {
      if (!astriBoqReadCurrent(readContext)) return;
      if (e.target.classList.contains("astri-boq-qty-mat") || e.target.classList.contains("astri-boq-qty-svc")) {
        const idx = parseInt(e.target.dataset.idx);
        if (!isNaN(idx) && state.astriBoqItems?.[idx]) {
          if (e.target.classList.contains("astri-boq-qty-mat")) state.astriBoqItems[idx].material_qty = parseFloat(e.target.value) || 0;
          else state.astriBoqItems[idx].service_qty = parseFloat(e.target.value) || 0;
        }
        clearTimeout(_descTimer);
        _descTimer = setTimeout(() => refreshAutoDescription(type, content), 600);
      }
    });
    content.querySelector("#astriBoqManualFileInput")?.addEventListener("change", onAstriBoqManualFileUpload);
    state.astriBoqItems = items || [];
    state.astriBoqMatch = responseMatch;
    state.astriBoqMatchJobId = responseMatch?.matched ? jobIdAtOpen : null;
    // Donation remains PR/plant metadata only; permit is the native ASTRI snapshot.
    if (!isSubfeeder && state.astriPendingDonation && state.astriPendingDonation.cluster_code === (state.astriMatch?.cluster_code || "")) {
      const plantInput = content.querySelector("#astriBoqPlantCode");
      const donationPlant = state.astriPendingDonation.area_plant_code || state.astriBoqMatch?.area_plant_code || "";
      if (plantInput && donationPlant) plantInput.value = donationPlant;
      if (typeof state.astriPendingDonation.pph_pct === "number") state.astriMatch.pph_pct = state.astriPendingDonation.pph_pct;
    }
    // Fetch auto PR description — reusable
    refreshAutoDescription(type, content);
    progress = showAstriBoqDualFeedback(
      type,
      "Add BOQ siap",
      hasItems ? `${items.length} item BOQ siap diperiksa.` : "Tidak ada item BOQ terisi.",
      hasItems ? "success" : "warning",
      true,
    );
  } catch (error) {
    if (!astriBoqReadCurrent(readContext)) return;
    progress = showAstriBoqDualFeedback(type, "Add BOQ gagal", error.message, "error", true);
    content.innerHTML = "";
  }
}


async function mergeAstriSupplierMap(items) {
  const readContext = astriBoqReadContext();
  const requestId = ++state.astriBoqSupplierRequestId;
  const selectedScope = state.astriBoqScope || "cluster";
  const requestMatch = { ...(activeAstriScopeMatch(selectedScope) || {}) };
  const matchKey = astriBoqMatchKey(requestMatch, selectedScope);
  items = (Array.isArray(items) ? items : []).filter((item) => !isAstriBoqPermitItem(item));
  astriBoqSetPermit();
  try {
    let url = "";
    if (state.jobId) {
      const selected = activeAstriScopeMatch(state.astriBoqScope || "cluster") || {};
      const scope = state.astriBoqScope || selected.scope || selected.target_type || "cluster";
      const query = new URLSearchParams(astriMatchQuery(selected, scope));
      url = selected.matched ? `/api/astri/boq/supplier-map/${encodeURIComponent(state.jobId)}?${query}` : "";
    } else {
      // No-job Add BOQ still uses the selected match, never a previous job match.
      const m = activeAstriScopeMatch(state.astriBoqScope || "cluster", null) || {};
      if (m.matched && (m.cluster_code || m.subfeeder_code || m.feeder_code)) {
        const scope = state.astriBoqScope || m.scope || m.target_type || "cluster";
        const q = new URLSearchParams({
          ...astriMatchQuery(m, scope),
          assigned_vendor_name: m.assigned_vendor_name || "",
          ...(scope === "feeder" ? {
            assigned_vendor_name: m.assigned_vendor_name || m.vendor_label || "",
            osp_route_area: m.osp_route_area || m.area || "",
            osp_route_area_plant_code: m.osp_route_area_plant_code || m.area_plant_code || "",
          } : state.astriBoqScope === "subfeeder" ? {
            assigned_vendor_name: m.assigned_vendor_name || m.vendor_label || "",
            cluster_area: m.cluster_area || m.area || "",
            cluster_area_plant_code: m.cluster_area_plant_code || m.area_plant_code || "",
          } : {
            assigned_vendor_name: m.assigned_vendor_name || m.vendor_name || m.vendor_label || "",
            vendor_label: m.vendor_label || "",
            area: m.area || "",
            area_plant_code: m.area_plant_code || "",
          }),
          cluster_name: m.cluster_name || "",
        });
        url = `/api/astri/boq/supplier-map?${q}`;
      }
    }
    if (!url) return items;
    const payload = await fetch(url, { credentials: "include", cache: "no-store" }).then(parseJsonResponse);
    if (requestId !== state.astriBoqSupplierRequestId || !astriBoqReadCurrent(readContext)) return items;
    if (!matchKey || astriBoqMatchKey(payload.match, selectedScope) !== matchKey) {
      astriBoqSetPermit(null, "Permit ASTRI tidak tersedia untuk target ini");
      return items;
    }
    astriBoqSetPermit(payload.permit);
    const suppliers = payload.suppliers || {};
    if (!Object.keys(suppliers).length) return items;
    return items.map((item) => {
      const code = String(item.code || "").trim().replace(/\.0+$/, "");
      const astriSupplier = suppliers[code];
      if (!astriSupplier) return item;
      return {
        ...item,
        supplier: astriSupplier.supplier || "",
        supplier_kind: astriSupplier.supplier || "",
        supplier_found: true,
      };
    });
  } catch (error) {
    if (requestId !== state.astriBoqSupplierRequestId || !astriBoqReadCurrent(readContext)) return items;
    astriBoqSetPermit(null, "Gagal memuat permit dari ASTRI");
    console.warn("ASTRI supplier map gagal", error);
    return items;
  }
}

async function onAstriBoqManualFileUpload(event) {
  const file = event.currentTarget?.files?.[0];
  if (!file) return;
  const tbody = document.getElementById("astriBoqTableBody");
  if (!tbody) return;
  const boqType = state.astriBoqScope || (!document.getElementById("astriSubfeederModal")?.classList.contains("hidden") ? "subfeeder" : "cluster");
  const readContext = astriBoqReadContext(boqType);
  const requestId = ++state.astriBoqManualRequestId;
  let progress = showAstriBoqDualProgress(boqType, "Upload BOQ Manual", `Memproses ${file.name}...`, true);
  try {
    const form = new FormData();
    form.append("file", file);
    // Backend uses this match to fetch original ASTRI boq[CODE][vendor_name].
    const activeAstriMatch = { ...(activeAstriScopeMatch(state.astriBoqScope || "cluster") || {}) };
    activeAstriMatch.scope = activeAstriMatch.scope || state.astriBoqScope || "cluster";
    form.append("astri_match", JSON.stringify(activeAstriMatch));
    const payload = await fetch("/api/astri/boq/parse-file", {
      method: "POST",
      body: form,
    }).then(parseJsonResponse);
    if (requestId !== state.astriBoqManualRequestId || !astriBoqReadCurrent(readContext)) return;
    if (!(payload.items || []).length) {
      showAstriBoqDualFeedback(boqType, "Upload BOQ Manual", payload.error || "Tidak ada item BOQ ditemukan.", "warning", true);
      return;
    }
    if ((payload.supplier_mapped || 0) === 0) {
      console.warn("ASTRI supplier map kosong", payload.supplier_error || "no error", payload);
    }
    const items = await mergeAstriSupplierMap(payload.items || []);
    if (requestId !== state.astriBoqManualRequestId || !astriBoqReadCurrent(readContext)) return;
    tbody.innerHTML = items.map((item, idx) => `
      <tr class="border-b border-zinc-100" data-boq-item-code="${escapeHtml(String(item.code || ""))}">
        <td class="px-2 py-1 text-center"><span data-boq-status-idx="${idx}" role="img" aria-label="Belum dikirim" title="Belum dikirim">—</span></td>
        <td class="px-2 py-1 font-mono text-xs">${escapeHtml(item.code)}</td>
        <td class="px-2 py-1">${escapeHtml(item.description)}</td>
        <td class="px-2 py-1">${escapeHtml(item.supplier || "")}</td>
        <td class="px-2 py-1 text-right"><input type="number" step="any" min="0" class="astri-boq-qty-mat w-20 text-right ${astriInput}" data-idx="${idx}" value="${escapeHtml(String(item.material_qty || 0))}"></td>
        <td class="px-2 py-1 text-right"><input type="number" step="any" min="0" class="astri-boq-qty-svc w-20 text-right ${astriInput}" data-idx="${idx}" value="${escapeHtml(String(item.service_qty || 0))}"></td>
        <td class="px-2 py-1">${escapeHtml(item.unit || "")}</td>
      </tr>`).join("") || '<tr><td colspan="7" class="p-3 text-zinc-500">Tidak ada item BOQ terisi.</td></tr>';
    state.astriBoqItems = items;
    const content = document.getElementById(boqType === "subfeeder" ? "astriSubfeederContent" : "astriClusterContent");
    const headerEl = content?.querySelector(".rounded-lg");
    if (headerEl) headerEl.innerHTML = `<strong>BOQ siap.</strong> File: ${escapeHtml(file.name)} &middot; ${items.length} item`;
    // Refresh auto PR description with new items
    refreshAutoDescription(boqType);
    progress = showAstriBoqDualFeedback(boqType, "Upload BOQ Manual selesai", `${items.length} item BOQ siap diperiksa.`, "success", true);
  } catch (error) {
    if (requestId !== state.astriBoqManualRequestId || !astriBoqReadCurrent(readContext)) return;
    progress = showAstriBoqDualFeedback(boqType, "Upload BOQ Manual gagal", error.message, "error", true);
  }
}

function updateAstriBoqRowProgress(tbody, payload) {
  if (!tbody) return;
  const result = payload?.result || {};
  const outcomes = new Map();
  for (const code of result.already_present_codes || []) {
    outcomes.set(String(code), "success");
  }
  for (const item of [...(result.items || []), ...(result.updated_items || [])]) {
    const code = String(item?.code || "");
    if (!code) continue;
    const outcome = item.ok ? "success" : "failure";
    outcomes.set(code, outcomes.has(code) && outcomes.get(code) !== outcome ? "pending" : outcome);
  }
  const terminal = ["done", "partial", "unknown"].includes(payload?.state) || payload?.timed_out;
  const icons = {
    untouched: ["—", "text-zinc-400", "Item tidak dikirim ke ASTRI"],
    pending: ["<svg class=\"inline-block h-4 w-4 animate-spin\" viewBox=\"0 0 24 24\" fill=\"none\" aria-hidden=\"true\"><circle cx=\"12\" cy=\"12\" r=\"9\" stroke=\"currentColor\" stroke-width=\"3\" opacity=\".3\"></circle><path d=\"M21 12a9 9 0 0 0-9-9\" stroke=\"currentColor\" stroke-width=\"3\" stroke-linecap=\"round\"></path></svg>", "text-sky-600", "Menunggu diproses ASTRI"],
    success: ["✓", "text-emerald-600 font-bold", "Item berhasil diproses ASTRI"],
    failure: ["×", "text-red-600 font-bold", "Item gagal diproses ASTRI"],
  };
  for (const cell of tbody.querySelectorAll("[data-boq-status-idx]")) {
    const code = String(cell.closest("tr")?.dataset.boqItemCode || "");
    const status = outcomes.get(code) || (payload?.state === "blocked-before-mutation" ? "untouched" : "pending");
    const [symbol, color, defaultLabel] = icons[status];
    const label = status === "pending" && terminal ? "Status item belum terverifikasi; periksa ASTRI sebelum submit ulang" : defaultLabel;
    cell.innerHTML = symbol;
    cell.className = color;
    cell.setAttribute("aria-label", label);
    cell.title = status === "pending" && result.active_code === code && !terminal ? "Sedang dikirim ke ASTRI" : label;
  }
}

function waitForAstriBoqPoll(delayMs) {
  return new Promise((resolve) => window.setTimeout(resolve, delayMs));
}

async function startAstriBoqSubmissionRequest(url, body, onRetry = null) {
  const serializedBody = JSON.stringify(body);
  const send = () => fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: serializedBody,
  }).then(parseJsonResponse);
  const recoverExisting = (error) => {
    if (!isAstriBoqExistingSubmissionError(error)) return null;
    const payload = error?.payload || {};
    return payload.submission_id ? payload : null;
  };
  try {
    return await send();
  } catch (firstError) {
    const existing = recoverExisting(firstError);
    if (existing) return existing;
    if (firstError?.status) throw firstError;
    onRetry?.();
    try {
      return await send();
    } catch (secondError) {
      const recovered = recoverExisting(secondError);
      if (recovered) return recovered;
      if (!secondError?.status) {
        const recoveredByAttempt = await recoverAstriBoqSubmission(body?.submission_attempt_id);
        if (recoveredByAttempt) return recoveredByAttempt;
        secondError.astriOutcomeUnknown = true;
      }
      throw secondError;
    }
  }
}

async function recoverAstriBoqSubmission(attemptId, { maxAttempts = 8, intervalMs = 1500 } = {}) {
  if (!attemptId) return null;
  const url = `/api/astri/boq/submissions/recover?attempt_id=${encodeURIComponent(attemptId)}`;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      const response = await fetch(url, { credentials: "include", cache: "no-store" });
      if (response.ok) {
        const payload = await response.json();
        if (payload?.submission_id) return payload;
      } else if (response.status !== 404) {
        break;
      }
    } catch (_error) {
      // Keep probing the idempotent recovery endpoint; never resubmit BOQ.
    }
    if (attempt + 1 < maxAttempts) await waitForAstriBoqPoll(intervalMs);
  }
  return null;
}


async function pollAstriBoqSubmission(
  submissionId,
  { onProgress = null, maxAttempts = 240, intervalMs = 3000 } = {},
) {
  if (!submissionId) throw new Error("ID proses Add BOQ tidak tersedia.");
  let lastPayload = null;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      const payload = await fetch(
        `/api/astri/boq/submissions/${encodeURIComponent(submissionId)}`,
        { credentials: "include", cache: "no-store" },
      ).then(parseJsonResponse);
      lastPayload = payload;
      onProgress?.(payload);
      const state = String(payload.state || "");
      if (["done", "partial", "unknown", "blocked-before-mutation"].includes(state)) return payload;
    } catch (_error) {
      // A transient status-read failure cannot prove the upstream mutation failed.
    }
    if (attempt + 1 < maxAttempts) await waitForAstriBoqPoll(intervalMs);
  }
  return {
    ...(lastPayload || {}),
    submission_id: submissionId,
    state: "running",
    timed_out: true,
    message: "Add BOQ masih diproses atau hasilnya belum terbaca. Jangan submit ulang.",
  };
}

function astriBoqProgressText(payload) {
  const processed = Number(payload?.result?.processed_count || 0);
  if (processed > 0) return `${processed} item sudah diproses. Mohon tunggu...`;
  return payload?.message || "Add BOQ masih diproses. Mohon tunggu...";
}

async function submitAstriBoq(items, type) {
  const isSubfeeder = type === "subfeeder";
  const isFeeder = type === "feeder";
  const activeBoqMatch = { ...(activeAstriScopeMatch(type) || {}) };
  const submitJobId = state.jobId;
  const readContext = astriBoqReadContext(type);
  const renderedItems = state.astriBoqItems;
  const content = document.getElementById(isSubfeeder ? "astriSubfeederContent" : "astriClusterContent");
  const boqLockKey = astriSubmitLockKey(activeBoqMatch);
  const activeSubmitButton = content?.querySelector("#astriBoqSubmitButton");
  const submitLoading = setAstriSubmitLoading(
    activeSubmitButton,
    "Memeriksa status...",
    boqLockKey,
  );
  if (!submitLoading) return;
  document.getElementById("astriBoqResult")?.remove();
  lockAstriBoqSiblingControls(boqLockKey, activeSubmitButton);
  let progress = showAstriBoqDualProgress(type, "Add BOQ", "Memeriksa status WO ASTRI...", true);
  let keepSubmitLocked = false;
  let expectedWoStatus = "";
  let boqTableBody = null;
  let lastRowPayload = null;
  try {
    const prDescription = content?.querySelector("#astriBoqPrDescription")?.value || "";
    const autoDesc = content?.querySelector("#astriBoqPrAutoDesc")?.textContent || "";
    const attachmentLink = content?.querySelector("#astriBoqAttachmentLink")?.value || "";
    const finalPrDescription = (prDescription || autoDesc || "") + (attachmentLink ? "\n\nLink Attachment :\n" + attachmentLink : "");
    const matInputs = content?.querySelectorAll(".astri-boq-qty-mat") || [];
    const svcInputs = content?.querySelectorAll(".astri-boq-qty-svc") || [];
    const merged = items.map((item, idx) => ({
      ...item,
      material_qty: parseFloat(matInputs[idx]?.value) || 0,
      service_qty: parseFloat(svcInputs[idx]?.value) || 0,
    })).filter((item) => !isAstriBoqPermitItem(item));
    const renderedTable = content?.querySelector("#astriBoqTableBody");
    const status = await refreshAstriWorkOrderStatus({ matchOverride: activeBoqMatch });
    if (!astriBoqReadCurrent(readContext) || renderedItems !== state.astriBoqItems
      || activeSubmitButton !== content?.querySelector("#astriBoqSubmitButton")
      || renderedTable !== content?.querySelector("#astriBoqTableBody")) return;
    if (status.successful) {
      keepSubmitLocked = true;
      submitLoading.update(`Status WO ASTRI sudah ${status.latest_status_label || "Successful"}`);
      return;
    }
    if (!confirm("Submit Add BOQ ke ASTRI?")) return;
    boqTableBody = content?.querySelector("#astriBoqTableBody");
    updateAstriBoqRowProgress(boqTableBody, { state: "queued" });
    const scopeLabel = isSubfeeder ? "Subfeeder" : isFeeder ? "Feeder" : "Cluster";
    submitLoading.update(`Mengirim Add BOQ ${scopeLabel} ke ASTRI...`);
    progress?.remove?.();
    progress = showAstriBoqDualProgress(type, "Add BOQ", `Mengirim Add BOQ ${scopeLabel} ke ASTRI...`, true);
    let url, body;
    if (submitJobId) {
      url = isSubfeeder
        ? `/api/astri/subfeeder/boq/submit/${encodeURIComponent(submitJobId)}`
        : isFeeder
        ? `/api/astri/feeder/boq/submit/${encodeURIComponent(submitJobId)}`
        : `/api/astri/boq/submit/${encodeURIComponent(submitJobId)}`;
      body = { pr_description: finalPrDescription, attachment_link: attachmentLink, items: merged, match: activeBoqMatch };
    } else if (activeBoqMatch?.matched) {
      url = isSubfeeder ? "/api/astri/subfeeder/boq/submit" : isFeeder ? "/api/astri/feeder/boq/submit" : "/api/astri/boq/submit";
      body = { pr_description: finalPrDescription, attachment_link: attachmentLink, items: merged, match: activeBoqMatch };
    } else {
      showAstriBoqModalFeedback(type, "Add BOQ", "Match ASTRI tidak ditemukan.", "warning", true);
      return;
    }
    const submissionAttemptId = window.crypto?.randomUUID?.();
    if (!submissionAttemptId) throw new Error("Browser tidak mendukung ID proses Add BOQ yang aman.");
    body.submission_attempt_id = submissionAttemptId;
    const started = await startAstriBoqSubmissionRequest(url, body, () => {
      submitLoading.update("Menghubungkan ulang ke proses Add BOQ...");
      progress?.remove?.();
      progress = showAstriBoqDualProgress(
        type,
        "Add BOQ masih diproses",
        "Koneksi sempat terputus. EVA sedang mengambil proses yang sama. Jangan submit ulang.",
        true,
      );
    });
    if (!started.submission_id) throw new Error("Server tidak mengembalikan ID proses Add BOQ.");
    submitLoading.update("Add BOQ sedang diproses...");
    progress?.remove?.();
    progress = showAstriBoqDualProgress(
      type,
      "Add BOQ masih diproses",
      "Request sudah diterima. Jangan submit ulang; tunggu hasil ASTRI.",
      true,
    );
    const payload = await pollAstriBoqSubmission(started.submission_id, {
      onProgress: (current) => {
        lastRowPayload = current;
        updateAstriBoqRowProgress(boqTableBody, current);
        submitLoading.update("Add BOQ sedang diproses...");
        progress?.remove?.();
        progress = showAstriBoqDualProgress(type, "Add BOQ masih diproses", astriBoqProgressText(current), true);
      },
    });
    updateAstriBoqRowProgress(boqTableBody, payload);
    const stateName = String(payload.state || "");
    const result = payload.result || {};
    const skippedCodes = Array.from(new Set(
      (Array.isArray(result.skipped_codes) ? result.skipped_codes : [])
        .concat((Array.isArray(result.skipped_items) ? result.skipped_items : []).map((item) => item && item.code))
        .map((code) => String(code || "").trim())
        .filter(Boolean),
    ));
    const updatedItems = Array.isArray(result.updated_items) ? result.updated_items : [];
    const vendorMismatches = Array.isArray(result.vendor_mismatches) ? result.vendor_mismatches : [];
    const overwriteLines = updatedItems.map((item) => {
      const before = item.before || {};
      const after = item.after || {};
      return `${item.code || "-"}: material ${before.quantity_material || 0} → ${after.quantity_material || 0}; service ${before.quantity_service || 0} → ${after.quantity_service || 0}`;
    });
    const overwriteNotice = overwriteLines.length
      ? `\nQty overwrite (${overwriteLines.length}):\n${overwriteLines.join("\n")}`
      : "";
    const vendorNotice = vendorMismatches.length
      ? `\nVendor tidak diubah (${vendorMismatches.length}): ${vendorMismatches.map((item) => `${item.code}: ${item.vendor_before || "-"} → ${item.vendor_target || "-"}`).join("; ")}`
      : "";
    const parts = [`${Number(result.ok_count || 0)}/${Number(result.processed_count || 0)} OK`];
    if (Number(result.updated_count || 0)) parts.push(`${Number(result.updated_count)} qty di-update`);
    if (Number(result.inserted_count || 0)) parts.push(`${Number(result.inserted_count)} item baru`);
    if (Number(result.already_present_count || 0)) parts.push(`${Number(result.already_present_count)} sudah sama`);
    if (Number(result.skipped_count || 0)) parts.push(`${Number(result.skipped_count)} skipped`);
    if (Number(result.failed_count || 0)) parts.push(`${Number(result.failed_count)} gagal`);
    const boqCompleted = stateName === "done" && payload.close_ok === true;
    const uncertain = stateName === "partial" || stateName === "unknown" || payload.timed_out;
    const statusBlocked = stateName === "blocked-before-mutation"
      && String(payload.error_code || "").includes("WO_");
    if (boqCompleted) {
      expectedWoStatus = "Closed";
      showAstriBoqDualFeedback(type, "Add BOQ sukses", `${payload.message || "WO sudah Closed."}\n${parts.join(", ")}.${overwriteNotice}${vendorNotice}`, "success", true);
      showAstriBoqCardResult(type, activeBoqMatch, true, submitJobId);
    } else if (uncertain) {
      keepSubmitLocked = true;
      const uncertainBase = payload.message || "Hasil belum dapat dipastikan. Jangan submit ulang; periksa ASTRI.";
      const uncertainMessage = `${skippedCodes.length && !/pricelist/i.test(uncertainBase)
        ? `${uncertainBase}\nPricelist not available: ${skippedCodes.join(", ")}`
        : uncertainBase}${overwriteNotice}${vendorNotice}`;
      showAstriBoqDualFeedback(type, "Hasil Add BOQ perlu dicek", uncertainMessage, "warning", true);
    } else if (statusBlocked) {
      showAstriBoqCardResult(type, activeBoqMatch, false, submitJobId);
    } else {
      showAstriBoqDualFeedback(type, "Add BOQ tidak dijalankan", payload.message || "Belum ada data yang dikirim ke ASTRI.", "warning", true);
      showAstriBoqCardResult(type, activeBoqMatch, false, submitJobId);
    }
  } catch (error) {
    if (boqTableBody) updateAstriBoqRowProgress(boqTableBody, {
      ...(lastRowPayload || {}), state: "unknown",
    });
    if (error?.astriOutcomeUnknown || isAstriBoqExistingSubmissionError(error)) {
      keepSubmitLocked = true;
      showAstriBoqDualFeedback(
        type,
        "Add BOQ masih diproses",
        "Koneksi ke EVA terputus sebelum ID proses terbaca. Jangan submit ulang; refresh status WO/ASTRI terlebih dahulu.",
        "warning",
        true,
      );
    } else {
      showAstriBoqCardResult(type, activeBoqMatch, false, submitJobId);
      const handled = handleAstriMutationError(error, "Add BOQ gagal");
      if (!handled) showAstriBoqDualFeedback(type, "Add BOQ gagal", error?.message || "Request gagal diproses.", "error", true);
    }
  } finally {
    progress?.remove?.();
    if (astriBoqReadCurrent(readContext, false)) await refreshAstriWorkOrderStatusAfterAction(activeBoqMatch, expectedWoStatus);
    if (keepSubmitLocked) {
      submitLoading.update("Cek status ASTRI sebelum retry");
    } else {
      submitLoading.restore();
      applyAstriWorkOrderStatus(state.astriWorkOrderStatus);
      const boqSubmitButton = astriBoqReadCurrent(readContext) ? content?.querySelector("#astriBoqSubmitButton") : null;
      applyAstriModalSubmitWorkOrderStatus(
        boqSubmitButton,
        astriSubmitMatchesStatusIdentity(boqSubmitButton, state.astriWorkOrderStatus)
          ? state.astriWorkOrderStatus
          : null,
        "addboq",
      );
    }
  }
}


  function forceRoadPanelVisible() {
    // ROAD source selection is automatic from the Pilih Wilayah popup.
    // Keep the legacy manual panel permanently hidden.
    const panel = document.getElementById("roadSourcePanel");
    if (!panel) return;
    panel.classList.add("hidden");
    panel.style.display = "none";
    panel.hidden = true;
  }

// ---------- Boot ----------
  function boot() {
    forceRoadPanelVisible();
    initModals();
    initHistory();
    initShortcuts();
    initRetry();
    hideRetryBar();
    clearError();
    initDragShield();
    initExportCsv();
    ensureAstriPanel();
    refreshAstriStatus();
    populateFolderChecklist();
    renderQuickStats();
    renderStageProgress();
    preloadDashboardFrames();
    if (els.roadSourceSummary) els.roadSourceSummary.textContent = 'Server OSM aktif';
    
    loadGeofabrikRegions();
    state.osmLoaded = false;
    loadOsmKecamatanRegions();
    forceRoadPanelVisible();
    window.evaRenderRoadSourcePanel = renderOsmKecamatanPanel;
    if (els.geofabrikSelect) {
      els.geofabrikSelect.addEventListener("change", onGeofabrikChange);
    }
    if (els.osmProvinceSelect) {
      els.osmProvinceSelect.addEventListener("change", onOsmProvinceChange);
    }
    if (els.osmCitySelect) {
      els.osmCitySelect.addEventListener("change", onOsmCityChange);
    }
    if (els.osmKecamatanSelect) {
      els.osmKecamatanSelect.addEventListener("change", onOsmKecamatanChange);
      updateOsmKecamatanSize();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
  setTimeout(() => { try { renderValidationSummary(); } catch (_) {} }, 0);

  // Main-workspace callbacks live outside this ASTRI IIFE.
  window.clearAstriMatchContext = clearAstriMatchContext;
  window.setCurrentJobId = setCurrentJobId;
})();

// Notification/global helper shim restored by agent.
(function () {
  if (typeof window === "undefined") return;
  function ensureRoot() {
    let root = document.getElementById("notificationRoot");
    if (root) return root;
    root = document.createElement("section");
    root.id = "notificationRoot";
    root.className = "notification-stack";
    root.setAttribute("aria-label", "Notifikasi aplikasi");
    document.body.appendChild(root);
    return root;
  }
  function show(title, message, tone) {
    if (tone !== "error" || !window.showNotification) return null;
    return window.showNotification({ title, message, severity: "error" });
  }
  window.setStatus = window.setStatus || function (label, message, tone) {
    const statusBadge = document.getElementById("statusBadge");
    if (statusBadge) statusBadge.textContent = label || "";
    if (message) show(label || "Info", message, tone);
  };
  window.setError = window.setError || function (message, source) {
    const alert = document.getElementById("errorAlert");
    if (alert) {
      alert.textContent = message;
      alert.classList.remove("hidden");
      alert.classList.add("alert-error");
      alert.setAttribute("role", "alert");
    }
    const bar = document.getElementById("retryBar");
    if (bar && source === "job") bar.classList.remove("hidden");
    show("Konversi gagal", message, "error");
  };
  window.clearError = window.clearError || function () {
    const alert = document.getElementById("errorAlert");
    if (alert) { alert.textContent = ""; alert.classList.add("hidden"); }
    const bar = document.getElementById("retryBar");
    if (bar) bar.classList.add("hidden");
  };
  ensureRoot();
})();
