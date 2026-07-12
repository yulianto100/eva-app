import "../../static/js/utils.js";

beforeEach(() => {
  document.body.innerHTML = "";
  globalThis.window.APP_CONFIG = {
    dxfConversionEnabled: false,
    defaultOutputMode: "boq",
    defaultNetworkType: "AE",
    maxUploadBytes: 1024 * 1024,
    maxUploadMb: 1,
    maxBatchUploadBytes: 10 * 1024 * 1024,
    maxBatchUploadMb: 10,
    boqTemplateReady: true,
    hpdbTemplateReady: true,
  };
});
