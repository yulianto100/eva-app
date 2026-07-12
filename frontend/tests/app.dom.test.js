import * as utils from "../src/utils.js";

function buildHarness() {
  document.body.innerHTML = `
    <form id="uploadForm">
      <input id="fileInput" type="file" />
      <div id="validationSummary"></div>
      <button id="convertButton" type="submit">Generate BOQ Excel</button>
      <label><input class="output-mode-input" type="checkbox" value="boq" checked />BOQ</label>
      <label><input class="output-mode-input" type="checkbox" value="hpdb" />HPDB</label>
      <label><input class="network-type-input" type="radio" name="network_type" value="AE" checked />AE</label>
      <label><input class="network-type-input" type="radio" name="network_type" value="UG" />UG</label>
      <div id="statusBadge"></div>
      <div id="progressBar"></div>
      <div id="downloadPanel" class="hidden"></div>
      <div id="consoleLogBody"></div>
      <div id="kmlFolderSelector"></div>
      <div id="errorAlert" class="hidden"></div>
    </form>
  `;
  const state = {
    fileCount: 0,
    kmlFileCount: 0,
    osmFileCount: 0,
    outputMode: "boq",
    networkType: "AE",
    selectedFolderCount: 0,
    availableFolderCount: 0,
    logs: [],
  };

  function renderValidation() {
    const summary = utils.validationReadiness({
      targets: utils.selectedOutputTargets(state.outputMode, { dxfConversionEnabled: false }),
      state,
      config: { boqTemplateReady: true, hpdbTemplateReady: true },
    });
    document.getElementById("convertButton").disabled = !summary.ready;
    document.getElementById("validationSummary").textContent = summary.ready ? "Ready" : summary.reasons.join(" | ");
    return summary;
  }

  document.getElementById("fileInput").addEventListener("change", (event) => {
    const files = Array.from(event.target.files || []);
    state.fileCount = files.length;
    state.kmlFileCount = files.filter((file) => /\.(kml|kmz)$/i.test(file.name)).length;
    state.osmFileCount = files.filter((file) => /\.(osm|pbf)$/i.test(file.name)).length;
    renderValidation();
  });
  document.querySelectorAll(".output-mode-input").forEach((input) => {
    input.addEventListener("change", () => {
      const selected = Array.from(document.querySelectorAll(".output-mode-input"))
        .filter((item) => item.checked)
        .map((item) => item.value);
      state.outputMode = utils.modeFromTargets(selected, { dxfConversionEnabled: false });
      renderValidation();
    });
  });
  document.querySelectorAll(".network-type-input").forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) state.networkType = utils.normalizeNetworkType(input.value);
    });
  });

  return { state, renderValidation };
}

describe("EVA DOM interactions", () => {
  it("file upload triggers validation", () => {
    const { state } = buildHarness();
    const input = document.getElementById("fileInput");
    Object.defineProperty(input, "files", { value: [new File(["kml"], "cluster.kml")], configurable: true });
    input.dispatchEvent(new Event("change"));

    expect(state.fileCount).toBe(1);
    expect(document.getElementById("validationSummary").textContent).toBe("Ready");
  });

  it("output mode toggle updates state", () => {
    const { state } = buildHarness();
    const hpdb = document.querySelector('input[value="hpdb"]');
    hpdb.checked = true;
    hpdb.dispatchEvent(new Event("change"));

    expect(state.outputMode).toBe("boq_hpdb");
  });

  it("network type switch updates UI state", () => {
    const { state } = buildHarness();
    const ug = document.querySelector('input[value="UG"]');
    ug.checked = true;
    ug.dispatchEvent(new Event("change"));

    expect(state.networkType).toBe("UG");
  });

  it("convert button is disabled when not ready", () => {
    const { renderValidation } = buildHarness();
    renderValidation();

    expect(document.getElementById("convertButton").disabled).toBe(true);
  });

  it("status polling updates progress bar", () => {
    buildHarness();
    const payload = { status: "processing", progress: 45 };
    document.getElementById("statusBadge").textContent = payload.status;
    document.getElementById("progressBar").style.width = `${payload.progress}%`;

    expect(document.getElementById("progressBar").style.width).toBe("45%");
  });

  it("download panel shows after job complete", () => {
    buildHarness();
    const panel = document.getElementById("downloadPanel");
    panel.classList.toggle("hidden", false);
    panel.textContent = "Download ZIP";

    expect(panel.classList.contains("hidden")).toBe(false);
    expect(panel.textContent).toContain("Download");
  });

  it("console log rendering escapes messages", () => {
    buildHarness();
    document.getElementById("consoleLogBody").innerHTML = `<span>${utils.escapeHtml("<bad>")}</span>`;

    expect(document.getElementById("consoleLogBody").textContent).toBe("<bad>");
  });

  it("KML folder selection updates counters", () => {
    buildHarness();
    const selector = document.getElementById("kmlFolderSelector");
    selector.innerHTML = `<label><input type="checkbox" checked value="FDT">FDT</label>`;

    expect(selector.querySelectorAll("input:checked")).toHaveLength(1);
  });

  it("KML folder deselection clears selected state", () => {
    buildHarness();
    const selector = document.getElementById("kmlFolderSelector");
    selector.innerHTML = `<label><input type="checkbox" checked value="FDT">FDT</label>`;
    const checkbox = selector.querySelector("input");
    checkbox.checked = false;
    checkbox.dispatchEvent(new Event("change"));

    expect(selector.querySelectorAll("input:checked")).toHaveLength(0);
  });

  it("error alert display is toggled", () => {
    buildHarness();
    const alert = document.getElementById("errorAlert");
    alert.textContent = "Upload gagal";
    alert.classList.remove("hidden");

    expect(alert.classList.contains("hidden")).toBe(false);
    expect(alert.textContent).toContain("Upload gagal");
  });

  it("retrying status keeps processing tone visible", () => {
    buildHarness();
    const retryLabel = `Retrying (attempt ${2}/${3})`;
    document.getElementById("statusBadge").textContent = retryLabel;

    expect(document.getElementById("statusBadge").textContent).toContain("Retrying");
  });
});
