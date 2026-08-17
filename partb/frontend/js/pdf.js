import { state, booksMap, escapeHtml, API, getAuth } from './state.js';

export function initPdfViewer() {
  var overlay       = document.getElementById('pdf-viewer-modal');
  var closeBtn      = document.getElementById('pdf-viewer-close');
  var prevBtn       = document.getElementById('pdf-prev-btn');
  var nextBtn       = document.getElementById('pdf-next-btn');
  var titleEl       = document.getElementById('pdf-viewer-title');
  var badgeEl       = document.getElementById('pdf-page-badge');
  var curPageEl     = document.getElementById('pdf-current-page');
  var totPageEl     = document.getElementById('pdf-total-pages');
  var canvas        = document.getElementById('pdf-render-canvas');
  var toggleHighlight = document.getElementById('pdf-toggle-highlight');
  var zoomInBtn = document.getElementById('pdf-zoom-in');
  var zoomOutBtn = document.getElementById('pdf-zoom-out');
  var zoomLevelEl = document.getElementById('pdf-zoom-level');
  var resizeHandle  = overlay ? overlay.querySelector('.pdf-drawer-handle') : null;
  if (!overlay) return;

  var MIN_WIDTH = 320;
  var MAX_WIDTH = Math.min(900, window.innerWidth - 340);
  var drawerWidth = 440;

  function applyWidth(w) {
    drawerWidth = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, w));
    document.documentElement.style.setProperty('--pdf-w', drawerWidth + 'px');
  }

  if (resizeHandle) {
    resizeHandle.addEventListener('mousedown', function(e) {
      e.preventDefault();
      resizeHandle.classList.add('dragging');
      document.body.style.userSelect = 'none';
      document.body.style.cursor = 'col-resize';
      var layout = document.querySelector('.chat-layout');
      if (layout) layout.style.transition = 'none';
      var startX = e.clientX;
      var startW = overlay.offsetWidth;
      function onMove(e) { applyWidth(startW + (startX - e.clientX)); }
      function onUp() {
        resizeHandle.classList.remove('dragging');
        document.body.style.userSelect = '';
        document.body.style.cursor = '';
        if (layout) layout.style.transition = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  window.addEventListener('resize', function() {
    var sidebarW = window.innerWidth <= 768 ? 0 : 280;
    MAX_WIDTH = Math.min(900, Math.max(320, window.innerWidth - sidebarW - 80));
    applyWidth(drawerWidth);
    var sidebar = document.getElementById('sidebar');
    if (sidebar && window.innerWidth <= 768 && overlay.classList.contains('open')) {
      sidebar.classList.remove('open');
    }
  });

  var currentPage = 1;
  var totalPages  = 1;
  var currentSrc  = null;
  var pdfDoc = null;
  var isRendering = false;
  var pageNumPending = null;
  var zoomFactor = 1.0;
  var currentFitScale = 1.0;
  var pdfWrapper = document.getElementById('pdf-canvas-wrapper');

  // Collection payloads can contain legacy arrays or OCR dictionaries. Keep
  // the normalization at the UI boundary so old and new indexed books render
  // without changing the Qdrant schema.
  function readJsonAttribute(element, name, fallback) {
    var raw = element && element.getAttribute(name);
    if (!raw) return fallback;
    try {
      return JSON.parse(raw);
    } catch (err) {
      console.warn('[PDF] Invalid ' + name + ' metadata:', err);
      return fallback;
    }
  }

  function isBoundingBoxObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) &&
      (value.left != null || value.x0 != null || value.x != null) &&
      (value.right != null || value.x1 != null || value.width != null);
  }

  function asMetadataArray(value) {
    if (value == null) return [];
    if (typeof value === 'string') {
      try { return asMetadataArray(JSON.parse(value)); } catch (err) { return []; }
    }
    if (Array.isArray(value)) {
      // Accept a single legacy box encoded as [left, bottom, right, top],
      // not only the usual [[...], [...]] collection shape.
      if (value.length >= 4 && value.slice(0, 4).every(function(item) {
        return typeof item === 'number' || (typeof item === 'string' && item.trim() !== '');
      })) return [value];
      return value;
    }
    if (typeof value !== 'object') return [value];
    if (isBoundingBoxObject(value)) return [value];
    for (var key of ['bounding_boxes', 'bounding_box', 'bbox', 'items', 'values']) {
      if (value[key] != null) return asMetadataArray(value[key]);
    }
    // Some Qdrant/API serializers return an object keyed by element index.
    return Object.keys(value).sort(function(a, b) {
      var na = Number(a), nb = Number(b);
      return Number.isFinite(na) && Number.isFinite(nb) ? na - nb : a.localeCompare(b);
    }).map(function(key) { return value[key]; });
  }

  function normalizedBoundingBoxes(value) {
    return asMetadataArray(value).map(function(box) {
      if (Array.isArray(box) && box.length >= 4) {
        var legacy = box.slice(0, 4).map(Number);
        return legacy.every(Number.isFinite) ? legacy : null;
      }
      if (!box || typeof box !== 'object') return null;
      var left = Number(box.left != null ? box.left : (box.x0 != null ? box.x0 : box.x));
      var right = Number(box.right != null ? box.right :
        (box.x1 != null ? box.x1 : (box.width != null ? left + Number(box.width) : NaN)));
      var top = Number(box.top != null ? box.top : (box.y0 != null ? box.y0 : box.y));
      var bottom = Number(box.bottom != null ? box.bottom :
        (box.y1 != null ? box.y1 : (box.height != null ? top + Number(box.height) : NaN)));
      if (![left, top, right, bottom].every(Number.isFinite)) return null;
      return {
        left: left,
        top: top,
        right: right,
        bottom: bottom,
        coord_origin: box.coord_origin || box.coordinate_origin || 'BOTTOMLEFT',
        page_number: box.page_number != null ? Number(box.page_number) :
          (box.page != null ? Number(box.page) : null)
      };
    }).filter(Boolean);
  }

  function sourceBoundingBoxes() {
    return normalizedBoundingBoxes(currentSrc && currentSrc.bounding_boxes);
  }

  // Initialize PDF.js

  function renderPage(num) {
    if (!pdfDoc || !canvas) return;
    if (isRendering) {
      pageNumPending = num;
      return;
    }
    isRendering = true;
    pdfDoc.getPage(num).then(async function(page) {
      var ctx = canvas.getContext('2d', { willReadFrequently: true });
      var vpAt1 = page.getViewport({ scale: 1 });
      var canvasArea = document.querySelector('.pdf-canvas-area');
      var availW = (canvasArea ? canvasArea.clientWidth : 400) - 24;
      var availH = (canvasArea ? canvasArea.clientHeight : 500) - 80;
      var fitScale = Math.min(availW / vpAt1.width, availH / vpAt1.height);
      fitScale = Math.max(fitScale, 0.3);
      currentFitScale = fitScale;
      var scale = fitScale * zoomFactor;
      var viewport = page.getViewport({ scale: scale });
      canvas.height = viewport.height;
      canvas.width = viewport.width;
      
      canvas.style.width = viewport.width + 'px';
      canvas.style.height = viewport.height + 'px';
      canvas.style.maxWidth = '';

      var pdfPageEl = document.getElementById('pdf-page-canvas');
      if (pdfPageEl) {
        pdfPageEl.style.width = '';
        pdfPageEl.style.maxWidth = '';
      }

      var renderContext = {
        canvasContext: ctx,
        viewport: viewport
      };
      var renderTask = page.render(renderContext);
      renderTask.promise.then(function() {
        isRendering = false;
        
        if (currentSrc) {
          if (sourceBoundingBoxes().length > 0) {
            highlightBoundingBoxes(viewport);
          } else if (currentSrc.excerpt) {
            highlightTextOnPdf(page, currentSrc.excerpt, viewport);
          }
        }

        if (pageNumPending !== null) {
          renderPage(pageNumPending);
          pageNumPending = null;
        }
      });
    });
  }

  function highlightBoundingBoxes(viewport) {
    var existingOverlay = document.getElementById("pdf-highlight-overlay");
    if (existingOverlay) existingOverlay.remove();

    var boxes = sourceBoundingBoxes();
    if (boxes.length === 0) return;

    var overlay = document.createElement("div");
    overlay.id = "pdf-highlight-overlay";
    overlay.style.cssText = "position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 10;";

    var tf = viewport.transform;
    var pageHeight = viewport.viewBox ? viewport.viewBox[3] - viewport.viewBox[1] : (viewport.height / viewport.scale);
    for (var i = 0; i < boxes.length; i++) {
      var b = boxes[i];
      if (!b) continue;
      if (b.page_number != null && b.page_number !== currentPage) continue;

      // Legacy arrays are [left, bottom, right, top] in PDF coordinates.
      // OCR dictionaries preserve Docling's coord_origin and normally use
      // TOPLEFT coordinates, so convert them before applying PDF.js's matrix.
      var x1, y1, x2, y2;
      if (Array.isArray(b)) {
        x1 = Number(b[0]); y1 = Number(b[1]);
        x2 = Number(b[2]); y2 = Number(b[3]);
      } else {
        x1 = Number(b.left);
        x2 = Number(b.right);
        if (String(b.coord_origin || '').toUpperCase().includes('TOPLEFT')) {
          y1 = pageHeight - Number(b.bottom);
          y2 = pageHeight - Number(b.top);
        } else {
          y1 = Number(b.bottom);
          y2 = Number(b.top);
        }
      }
      if (![x1, y1, x2, y2].every(Number.isFinite)) continue;

      // Transform all four corners so rotated PDF pages still get a proper
      // axis-aligned overlay instead of a negative-width rectangle.
      var points = [[x1, y1], [x1, y2], [x2, y1], [x2, y2]].map(function(point) {
        return pdfjsLib.Util.transform(tf, [point[0], point[1], 0, 1]);
      });
      var xs = points.map(function(point) { return point[0]; });
      var ys = points.map(function(point) { return point[1]; });
      var canvasX = Math.min.apply(Math, xs);
      var canvasY = Math.min.apply(Math, ys);
      var w = Math.max.apply(Math, xs) - canvasX;
      var h = Math.max.apply(Math, ys) - canvasY;
      if (w <= 0 || h <= 0) continue;
      var rect = document.createElement("div");
      rect.style.cssText = "position: absolute; left: " + canvasX + "px; top: " + canvasY + "px; width: " + w + "px; height: " + h + "px; background: rgba(16, 185, 129, 0.3); border-radius: 2px; pointer-events: none;";
      overlay.appendChild(rect);
    }

    if (overlay.childNodes.length > 0) {
      var wrapper = document.getElementById("pdf-canvas-wrapper");
      if (wrapper) wrapper.appendChild(overlay);
      if (toggleHighlight && !toggleHighlight.checked) overlay.style.display = 'none';
    }
  }

  async function highlightTextOnPdf(page, searchText, viewport) {
    var existingOverlay = document.getElementById("pdf-highlight-overlay");
    if (existingOverlay) existingOverlay.remove();
    if (!searchText) return;

    try {
      var textContent = await page.getTextContent();
      var canvas = document.getElementById("pdf-render-canvas");
      if (!canvas) return;

      var overlay = document.createElement("div");
      overlay.id = "pdf-highlight-overlay";
      overlay.style.cssText = "position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 10;";

      var searchNorm = searchText.toLowerCase().replace(/\s+/g, " ").trim();
      var searchWords = searchNorm.split(" ").filter(function(w) { return w.length > 4; }).slice(0, 15);
      if (searchWords.length === 0) return;

      var matchCount = 0;
      for (var i = 0; i < textContent.items.length; i++) {
        var item = textContent.items[i];
        if (!item.str || !item.str.trim()) continue;
        var itemText = item.str.toLowerCase();
        var hasMatch = searchWords.some(function(word) { return itemText.includes(word); });
        if (!hasMatch) continue;

        var tx = pdfjsLib.Util.transform(viewport.transform, item.transform);
        var xPct = (tx[4] / viewport.width) * 100;
        var yPct = (tx[5] / viewport.height) * 100;
        var fontSizePct = (Math.sqrt(tx[2] * tx[2] + tx[3] * tx[3]) / viewport.height) * 100;
        var widthPct = ((item.width * viewport.scale) / viewport.width) * 100;
        var extraPaddingPct = (4 / viewport.height) * 100;

        var rect = document.createElement("div");
        rect.style.cssText = "position: absolute; left: " + xPct + "%; top: " + (yPct - fontSizePct) + "%; width: " + widthPct + "%; height: " + (fontSizePct + extraPaddingPct) + "%; background: rgba(16, 185, 129, 0.3); border-radius: 2px; pointer-events: none;";
        overlay.appendChild(rect);
        matchCount++;
      }

      if (matchCount > 0) {
        var wrapper = document.getElementById("pdf-canvas-wrapper");
        if (wrapper) wrapper.appendChild(overlay);
        if (toggleHighlight && !toggleHighlight.checked) overlay.style.display = 'none';
      }
    } catch (e) {
      console.error(e);
    }
  }

  async function openViewer(src) {
    currentSrc = src;
    currentPage = src.page || 1;
    zoomFactor = 1.0;
    
    var bookId = src.book_id || src.title;
    var token = getAuth("kr_token");
    pdfDoc = null;
    if (typeof document !== 'undefined') {
      var oldHighlight = document.getElementById('pdf-highlight-overlay');
      if (oldHighlight) oldHighlight.remove();
    }
    
    titleEl.textContent = 'Loading...';
    
    var layout = document.querySelector('.chat-layout');
    if (layout) layout.classList.add('pdf-open');
    overlay.style.transition = 'none';
    overlay.classList.add('open');
    void overlay.offsetHeight;
    overlay.style.transition = '';
    document.documentElement.style.setProperty('--pdf-w', drawerWidth + 'px');
    var sidebar = document.getElementById('sidebar');
    if (sidebar && window.innerWidth <= 768) sidebar.classList.remove('open');
    
    if (window.pdfjsLib && bookId) {
      pdfjsLib.GlobalWorkerOptions.workerSrc = 'js/lib/pdf.worker.min.mjs';
      if (!token) {
        console.error("Failed to load PDF: authentication token is missing");
        titleEl.textContent = 'PDF unavailable';
      } else {
        const url = `${API}/pdf/${encodeURIComponent(bookId)}?token=${encodeURIComponent(token)}`;
        try {
          pdfDoc = await pdfjsLib.getDocument({ url: url }).promise;
          totalPages = pdfDoc.numPages;
          currentPage = Math.max(1, Math.min(currentPage, totalPages));
        } catch (err) {
          pdfDoc = null;
          console.error("Failed to fetch PDF:", err);
          titleEl.textContent = 'Could not load PDF';
        }
      }
    }
    
    titleEl.textContent = src.title || bookId || 'PDF Document';
    
    updatePageControls();
    if (pdfDoc) {
      renderPage(currentPage);
    }
  }

  function closeViewer() {
    overlay.classList.remove('open');
    var layout = document.querySelector('.chat-layout');
    if (layout) setTimeout(function() { layout.classList.remove('pdf-open'); }, 300);
  }

  closeBtn.addEventListener('click', closeViewer);
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && overlay.classList.contains('open')) closeViewer();
  });

  function updatePageControls() {
    curPageEl.textContent = currentPage;
    totPageEl.textContent = totalPages;
    badgeEl.textContent   = 'p. ' + currentPage;
    prevBtn.disabled = currentPage <= 1;
    nextBtn.disabled = currentPage >= totalPages;
  }

  prevBtn.addEventListener('click', function() {
    if (currentPage > 1) {
      currentPage--;
      updatePageControls();
      renderPage(currentPage);
    }
  });

  nextBtn.addEventListener('click', function() {
    if (currentPage < totalPages) {
      currentPage++;
      updatePageControls();
      renderPage(currentPage);
    }
  });

  document.addEventListener('click', function(e) {
    // Source card click
    var card = e.target.closest('.source-card');
    if (card) {
      var title   = card.getAttribute('data-src-title');
      var book_id = card.getAttribute('data-src-book_id') || title;
      var page    = parseInt(card.getAttribute('data-src-page'), 10) || 1;
      var excerpt = card.getAttribute('data-src-excerpt') || '';
      var boundingBoxes = readJsonAttribute(card, 'data-src-bounding-boxes', []);
      var elementIds = readJsonAttribute(card, 'data-src-element-ids', []);
      if (title || book_id) openViewer({ title: title, book_id: book_id, page: page, excerpt: excerpt, bounding_boxes: boundingBoxes, element_ids: elementIds });
      return;
    }

    // Inline citation chip click
    var chip = e.target.closest('.inline-citation-chip');
    if (chip) {
      var bookName = chip.getAttribute('data-cite-book') || '';
      var page     = parseInt(chip.getAttribute('data-cite-page'), 10) || 1;
      var section  = chip.getAttribute('data-cite-section') || '';

      // The book field in citations is often the book_id itself (e.g. "gemini-prompt").
      // 1. Start with the raw book name as the book_id
      var bookId = bookName;

      // 2. Try to match against booksMap display names (e.g. "Clean Code" -> "clean-code")
      Object.keys(booksMap).forEach(function(id) {
        if (booksMap[id].name.toLowerCase() === bookName.toLowerCase()) {
          bookId = id;
        }
      });

      // 3. Only fall back to the active session's book if the bookName is completely empty
      if (!bookId) {
        var activeSession = state.sessionMap[state.activeSessionId];
        if (activeSession) bookId = activeSession.book;
      }

      openViewer({ title: bookName, book_id: bookId, page: page, excerpt: section });
    }
  });

  if (toggleHighlight) {
    toggleHighlight.addEventListener('change', function() {
      var overlay = document.getElementById('pdf-highlight-overlay');
      if (overlay) overlay.style.display = this.checked ? '' : 'none';
    });
  }

  function updateZoomDisplay() {
    if (!zoomLevelEl) return;
    zoomLevelEl.textContent = zoomFactor === 1.0 ? 'Fit' : Math.round(zoomFactor * 100) + '%';
  }

  function applyZoom() {
    updateZoomDisplay();
  }

  function rezoom() {
    updateZoomDisplay();
    if (canvasArea) canvasArea.style.cursor = zoomFactor > 1 ? 'grab' : '';
    if (pdfDoc) renderPage(currentPage);
  }

  if (zoomInBtn) zoomInBtn.addEventListener('click', function() {
    zoomFactor = Math.min(5, +(zoomFactor * 1.25).toFixed(2));
    rezoom();
  });
  if (zoomOutBtn) zoomOutBtn.addEventListener('click', function() {
    zoomFactor = Math.max(0.25, +(zoomFactor / 1.25).toFixed(2));
    rezoom();
  });
  if (zoomLevelEl) zoomLevelEl.addEventListener('dblclick', function() {
    zoomFactor = 1.0;
    rezoom();
  });

  // Pan/drag to move zoomed PDF
  var canvasArea = document.querySelector('.pdf-canvas-area');
  if (canvasArea) {
    var isPanning = false, panX = 0, panY = 0;
    canvasArea.addEventListener('mousedown', function(e) {
      if (zoomFactor <= 1) return;
      isPanning = true;
      panX = e.clientX;
      panY = e.clientY;
      canvasArea.style.cursor = 'grabbing';
      e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
      if (!isPanning) return;
      canvasArea.scrollLeft -= e.clientX - panX;
      canvasArea.scrollTop -= e.clientY - panY;
      panX = e.clientX;
      panY = e.clientY;
    });
    document.addEventListener('mouseup', function() {
      if (!isPanning) return;
      isPanning = false;
      canvasArea.style.cursor = zoomFactor > 1 ? 'grab' : '';
    });
  }
}
