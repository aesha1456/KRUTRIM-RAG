// pdf-adapter.js — bridges pdf.js v6 ESM to global pdfjsLib
(async function() {
  var mod = await import('./pdf.min.mjs');
  window.pdfjsLib = mod;
})();
