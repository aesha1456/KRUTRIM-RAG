import { state, booksMap, escapeHtml, truncate, getAuth } from './state.js';

export const el = {
  sidebar: document.getElementById('sidebar'),
  sessionList: document.getElementById('session-list'),
  sessionCount: document.getElementById('session-count'),
  chatMessages: document.getElementById('chat-messages'),
  messageThread: document.getElementById('message-thread'),
  emptyState: document.getElementById('empty-state'),
  chatTitle: document.getElementById('chat-title'),
  chatStatus: document.getElementById('chat-status'),
  chatForm: document.getElementById('chat-form'),
  chatInput: document.getElementById('chat-input'),
  newSessionBtn: document.getElementById('new-session-btn'),
  newChatModal: document.getElementById('new-chat-modal'),
  newChatForm: document.getElementById('new-chat-form'),
  modalChatTitle: document.getElementById('modal-chat-title'),
  modalBookSelect: document.getElementById('modal-book-select'),
  modalCloseBtn: document.getElementById('modal-close'),
  modalCancelBtn: document.getElementById('modal-cancel'),
  searchModal: document.getElementById('search-modal'),
  searchInput: document.getElementById('chat-search-input'),
  searchResults: document.getElementById('search-results-container'),
  searchResultsEmpty: document.getElementById('search-results-empty')
};

export function syncModelDropdown(modelId) {
  var items = document.querySelectorAll('#model-dropdown-menu .dropdown-item');
  var toggle = document.getElementById('model-dropdown-toggle');
  var label = document.getElementById('model-dropdown-label');
  if (!toggle || !label) return;
  items.forEach(function(item) {
    if (item.getAttribute('data-value') === modelId) {
      items.forEach(function(i) { i.classList.remove('selected'); });
      item.classList.add('selected');
      var text = item.querySelector('span').textContent;
      label.textContent = text;
      var svg = item.querySelector('svg').cloneNode(true);
      var oldSvg = toggle.querySelector('.dropdown-icon');
      if (oldSvg) {
        svg.classList.add('dropdown-icon');
        toggle.replaceChild(svg, oldSvg);
      }
    }
  });
}

export function updateHeader() {
  var s = state.sessionMap[state.activeSessionId];
  if (s) {
    el.chatTitle.textContent = s.name;
    var subtitle = document.getElementById('chat-subtitle');
    if (!subtitle) {
      subtitle = document.createElement('span');
      subtitle.id = 'chat-subtitle';
      subtitle.style.cssText = 'margin-left:var(--space-md);font-size:var(--text-caption-md);color:var(--mute);padding:2px 8px;background:var(--surface-soft);border:1px solid var(--hairline);border-radius:var(--radius-sm)';
      el.chatTitle.parentNode.appendChild(subtitle);
    }
    var bookObj = booksMap[s.book];
    if (bookObj) {
      subtitle.textContent = 'Book: ' + bookObj.name + ' (' + bookObj.pages + ' p, ' + bookObj.chunks + ' c)';
      subtitle.style.display = 'inline-block';
    } else {
      subtitle.style.display = 'none';
    }
    if (s.model) syncModelDropdown(s.model);
    el.chatStatus.textContent = 'Connected';
    el.chatStatus.className = 'chat-status connected';
  } else {
    el.chatTitle.textContent = 'Select Chat';
    var subtitle = document.getElementById('chat-subtitle');
    if (subtitle) subtitle.style.display = 'none';
    el.chatStatus.textContent = 'Idle';
    el.chatStatus.className = 'chat-status';
  }
}

export function renderSessions() {
  var ids = Object.keys(state.sessionMap);
  if (el.sessionCount) el.sessionCount.textContent = ids.length;
  el.sessionList.innerHTML = '';

  ids.forEach(function (id) {
    var s = state.sessionMap[id];
    var div = document.createElement('div');
    div.className = 'chat-session' + (id === state.activeSessionId ? ' active' : '');
    div.dataset.id = id;
    var bookObj = booksMap[s.book];
    var bookPreview = bookObj ? bookObj.name : (s.book || 'No book selected');
    div.innerHTML =
      '<span class="chat-session-marker"></span>' +
      '<div class="chat-session-info">' +
      '<div class="chat-session-name">' + escapeHtml(s.name) + '</div>' +
      '<div class="chat-session-preview" style="display:flex;align-items:center;gap:4px;">' +
        '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;opacity:0.6;"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>' +
        '<span style="opacity:0.7;font-size:10px;font-weight:600;letter-spacing:0.03em;text-transform:uppercase;flex-shrink:0;">Book:</span>' +
        '<span>' + escapeHtml(truncate(bookPreview, 20)) + '</span>' +
      '</div>' +
      '</div>' +
      '<div class="chat-session-actions custom-dropdown">' +
        '<button class="btn-icon-ghost" title="Options">' +
          '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"></circle><circle cx="12" cy="5" r="1"></circle><circle cx="12" cy="19" r="1"></circle></svg>' +
        '</button>' +
        '<div class="dropdown-menu">' +
          '<button class="dropdown-item rename-btn">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></svg>' +
            '<span>Rename</span>' +
          '</button>' +
          '<button class="dropdown-item delete-btn" style="color:var(--danger)">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--danger)"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>' +
            '<span>Delete</span>' +
          '</button>' +
        '</div>' +
      '</div>';

    var actionsDiv = div.querySelector('.chat-session-actions');
    var toggleBtn = actionsDiv.querySelector('.btn-icon-ghost');
    var renameBtn = actionsDiv.querySelector('.rename-btn');
    var deleteBtn = actionsDiv.querySelector('.delete-btn');

    toggleBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      document.querySelectorAll('.chat-session-actions.open').forEach(function(el) {
        if (el !== actionsDiv) el.classList.remove('open');
      });
      actionsDiv.classList.toggle('open');
    });

    renameBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      actionsDiv.classList.remove('open');
      var newName = prompt('Enter new chat name:', s.name);
      if (newName !== null && newName.trim() !== '') {
        s.name = newName.trim();
        renderSessions();
        updateHeader();
      }
    });

    el.sessionList.appendChild(div);
  });
}

export function renderMessages() {
  var s = state.sessionMap[state.activeSessionId];
  el.messageThread.innerHTML = '';

  if (!s || s.messages.length === 0) {
    var emptyClone = el.emptyState.cloneNode(true);
    emptyClone.removeAttribute('id');
    if (s) {
      var titleEl = emptyClone.querySelector('.chat-empty-title');
      if (titleEl) titleEl.textContent = s.name;
      var hintEl = emptyClone.querySelector('.chat-empty-hint');
      if (hintEl) {
        var bookObj = booksMap[s.book];
        var bookInfo = bookObj ? bookObj.name + ' · ' + bookObj.pages + ' pages' : 'No book';
        var modelName = s.model.charAt(0).toUpperCase() + s.model.slice(1);
        var metaDiv = document.createElement('div');
        metaDiv.className = 'chat-empty-meta';
        metaDiv.innerHTML =
          '<span class="chat-empty-pill">' +
            '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>' +
            escapeHtml(bookInfo) +
          '</span>' +
          '<span class="chat-empty-pill">' +
            '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>' +
            escapeHtml(modelName) +
          '</span>';
        hintEl.parentNode.insertBefore(metaDiv, hintEl);
        hintEl.textContent = 'No messages yet — start the conversation below.';
      }
      var suggestions = [
        { icon: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>', label: 'Summarize the key concepts' },
        { icon: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>', label: 'What are the main themes?' },
        { icon: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>', label: 'Give me a quick overview' },
        { icon: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>', label: 'What should I read first?' },
      ];
      var chipsDiv = document.createElement('div');
      chipsDiv.className = 'chat-empty-suggestions';
      suggestions.forEach(function(s) {
        var chip = document.createElement('button');
        chip.className = 'suggestion-chip';
        chip.innerHTML = s.icon + '<span>' + escapeHtml(s.label) + '</span>';
        chip.addEventListener('click', function() {
          el.chatInput.value = s.label;
          el.chatInput.focus();
          el.chatInput.dispatchEvent(new Event('input'));
        });
        chipsDiv.appendChild(chip);
      });
      emptyClone.appendChild(chipsDiv);
    }
    el.messageThread.appendChild(emptyClone);
    return;
  }

  var vg = state.versionGroups || {};

  s.messages.forEach(function (msg) {
    if (msg.pairId) {
      var group = vg[msg.pairId];
      if (group && msg.versionIdx !== group.activeVersion) return;
    }
    var msgEl = createMessageEl(msg);
    if (msg.sender === 'user') addUserMsgActions(msgEl);
    if (msg.sender === 'bot' && msg.pairId) {
      var group = vg[msg.pairId];
      if (group && group.versionCount > 1) addVersionSwitcherToMsg(msgEl, msg.pairId, group);
    }
    el.messageThread.appendChild(msgEl);
  });

  el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
}

function addUserMsgActions(msgEl) {
  var actions = document.createElement('div');
  actions.className = 'user-msg-actions';
  actions.innerHTML =
    '<button class="user-msg-action-btn copy-user-msg" title="Copy message">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>' +
    '</button>' +
    '<button class="user-msg-action-btn edit-msg-btn" title="Edit message">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>' +
    '</button>';
  msgEl.appendChild(actions);
}

function addVersionSwitcherToMsg(msgEl, pairId, vg) {
  var wrap = document.createElement('div');
  wrap.className = 'version-switcher';
  wrap.innerHTML =
    '<button class="version-btn version-prev" data-pair-id="' + pairId + '">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg>' +
    '</button>' +
    '<span class="version-label">' + (vg.activeVersion + 1) + '/' + vg.versionCount + '</span>' +
    '<button class="version-btn version-next" data-pair-id="' + pairId + '">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>' +
    '</button>';
  var actions = msgEl.querySelector('.message-actions');
  if (actions) {
    msgEl.insertBefore(wrap, actions);
  } else {
    msgEl.appendChild(wrap);
  }
}

function createMessageEl(msg) {
  var div = document.createElement('div');
  div.className = 'message ' + msg.sender;
  if (msg.pairId) div.dataset.pairId = msg.pairId;
  if (msg.versionIdx !== undefined) div.dataset.versionIdx = msg.versionIdx;
  if (msg.message_id) div.dataset.messageId = msg.message_id;
  // Strip inline citation patterns [Book: ... | § ... | Page: ...] before markdown parsing
  // so the | pipes don't break markdown table rendering
  var rawText = msg.sender === 'bot'
    ? msg.text.replace(/\[Book:[^\]]*?\]/g, '')
    : msg.text;

  if (msg.sender === 'bot') {
    rawText = rawText.replace(/<think>([\s\S]*?)(?:<\/think>|$)/gi, function(match, p1) {
      var isOpen = !match.toLowerCase().endsWith('</think>') ? ' open' : '';
      return '<details class="think-accordion" style="margin-bottom: 12px; border: 1px solid var(--hairline); border-radius: var(--radius-sm); background: var(--surface-soft);"' + isOpen + '><summary style="cursor: pointer; padding: 8px 12px; font-size: 13px; font-weight: 500; color: var(--mute); user-select: none;">Thinking and gathering information...</summary><div style="padding: 12px; border-top: 1px solid var(--hairline); font-size: 13px; color: var(--mute); white-space: pre-wrap;">' + escapeHtml(p1) + '</div></details>\n\n';
    });
  }

  var content = (msg.sender === 'bot' && window.marked)
    ? marked.parse(rawText, { breaks: true })
    : escapeHtml(rawText);

  var innerHTML = '<div class="message-content markdown-body">' + content + '</div>';

  if (msg.sender === 'bot') {
    var hasSources = msg.sources && msg.sources.length > 0;
    var sourceCount = hasSources ? msg.sources.length : 0;

    var metricsHtml = '';
    var metrics = msg.metrics;
    if (metrics && metrics.duration != null && metrics.tps != null) {
      var mts = metrics._ts || (msg.created_at ? Date.parse(msg.created_at) : Date.now());
      metricsHtml = '<div class="msg-metrics">' + escapeHtml(buildMetricsString(metrics, mts)) + '</div>';
    }

    innerHTML +=
      '<div class="message-actions">' +
        (hasSources
          ? '<button class="btn-sources-toggle" title="Toggle Sources" data-open="false">' +
              '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>' +
              '<span>' + sourceCount + ' Source' + (sourceCount !== 1 ? 's' : '') + '</span>' +
              '<svg class="sources-chevron" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>' +
            '</button>' +
            '<div class="msg-actions-divider"></div>'
          : '') +
        '<button class="btn-icon-ghost btn-sm" title="Regenerate">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"></polyline><polyline points="23 20 23 14 17 14"></polyline><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"></path></svg>' +
        '</button>' +
        '<button class="btn-icon-ghost btn-sm" title="Copy">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>' +
        '</button>' +
        '<button class="btn-icon-ghost btn-sm" title="Share">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"></circle><circle cx="6" cy="12" r="3"></circle><circle cx="18" cy="19" r="3"></circle><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"></line><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"></line></svg>' +
        '</button>' +
        '<button class="btn-icon-ghost btn-sm" title="Speak">' +
          '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"></path></svg>' +
        '</button>' +
        metricsHtml +
      '</div>';

    if (hasSources) {
      innerHTML += '<div class="message-sources">';
      msg.sources.forEach(function(src) {
        var bookObj = booksMap[src.book_id];
        var title = src.title || (bookObj ? bookObj.name : src.book_id) || 'Unknown Document';
        var page = src.page !== undefined ? src.page : (src.page_range && src.page_range.length > 0 ? src.page_range[0] : '?');
        var excerpt = src.excerpt || (src.section_path && src.section_path.length > 0 ? src.section_path[src.section_path.length - 1] : '');
        var elementIds = JSON.stringify(src.element_ids || []);
        var boundingBoxes = JSON.stringify(src.bounding_boxes || []);
        
        innerHTML +=
          '<div class="source-card" data-src-book_id="' + escapeHtml(src.book_id || title) + '" data-src-title="' + escapeHtml(title) + '" data-src-page="' + escapeHtml(String(page)) + '" data-src-excerpt="' + escapeHtml(excerpt) + '" data-src-element-ids="' + escapeHtml(elementIds) + '" data-src-bounding-boxes="' + escapeHtml(boundingBoxes) + '">' +
            '<div class="source-card-header">' +
              '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>' +
              '<span class="source-card-title">' + escapeHtml(title) + '</span>' +
              '<span class="source-card-page">p. ' + escapeHtml(String(page)) + '</span>' +
            '</div>' +
            (excerpt ? '<div class="source-card-excerpt">&ldquo;' + escapeHtml(excerpt) + '&rdquo;</div>' : '') +
          '</div>';
      });
      innerHTML += '</div>';
    }
  }

  div.innerHTML = innerHTML;

  if (msg.sender === 'bot') {
    var sourcesToggleBtn = div.querySelector('.btn-sources-toggle');
    var sourcesPanel = div.querySelector('.message-sources');
    if (sourcesToggleBtn && sourcesPanel) {
      sourcesToggleBtn.addEventListener('click', function() {
        var isOpen = sourcesToggleBtn.getAttribute('data-open') === 'true';
        if (isOpen) {
          sourcesPanel.style.height = sourcesPanel.scrollHeight + 'px';
          // Force a reflow
          void sourcesPanel.offsetHeight;
          sourcesPanel.style.height = '0';
          sourcesPanel.classList.remove('open');
          sourcesToggleBtn.setAttribute('data-open', 'false');
          sourcesToggleBtn.classList.remove('active');
        } else {
          var h = sourcesPanel.scrollHeight;
          if (h < 1) h = sourcesPanel.querySelectorAll('.source-card').length * 80 + 30;
          sourcesPanel.classList.add('open');
          sourcesPanel.style.height = h + 'px';
          sourcesToggleBtn.setAttribute('data-open', 'true');
          sourcesToggleBtn.classList.add('active');
          
          setTimeout(function() {
            if (el.chatMessages) {
              el.chatMessages.scrollBy({
                top: h + 60, // added extra padding to clear the input area
                behavior: 'smooth'
              });
            }
          }, 50);
          
          var finishTransition = function(e) {
            if (e.target === sourcesPanel && e.propertyName === 'height') {
              if (sourcesToggleBtn.getAttribute('data-open') === 'true') {
                sourcesPanel.style.height = 'auto';
              }
              sourcesPanel.removeEventListener('transitionend', finishTransition);
            }
          };
          sourcesPanel.addEventListener('transitionend', finishTransition);
        }
      });
    }
  }

  return div;
}

function buildMetricsString(metrics, ts) {
  var parts = [];
  if (metrics.model) parts.push(metrics.model);
  if (metrics.duration != null && metrics.tps != null) {
    parts.push(formatDuration(metrics.duration) + ' (' + formatTps(metrics.tps) + ' tok/s)');
  }
  if (ts) parts.push(formatTime(ts));
  return parts.filter(Boolean).join(' \u00b7 ');
}

function formatDuration(s) {
  if (s >= 60) {
    var m = Math.floor(s / 60), sec = Math.round(s % 60);
    return m + 'm ' + sec + 's';
  }
  return s.toFixed(1) + 's';
}

function formatTps(t) {
  return t < 1000 ? t.toFixed(2) : Math.round(t).toLocaleString();
}

function formatTime(ts) {
  var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var d = new Date(ts), h = d.getHours(), ap = h >= 12 ? 'PM' : 'AM';
  var hh = h % 12 || 12;
  var mm = String(d.getMinutes()).padStart(2, '0');
  return months[d.getMonth()] + ' ' + d.getDate() + ', ' + hh + ':' + mm + ' ' + ap;
}

export function showTypingIndicator() {
  var div = document.createElement('div');
  div.className = 'typing-indicator';
  div.id = 'typing-indicator';
  div.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div><span class="typing-label">Krutrim RAG is typing...</span>';
  el.messageThread.appendChild(div);
  el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
}

export function removeTypingIndicator() {
  var el2 = document.getElementById('typing-indicator');
  if (el2) el2.remove();
}

export function openNewChatModal() {
  if (el.newChatModal) {
    el.modalChatTitle.value = '';
    el.modalBookSelect.value = '';
    var label = document.getElementById('modal-book-label');
    if (label) { label.textContent = 'Choose a book...'; label.style.color = 'var(--mute)'; }
    document.querySelectorAll('#modal-book-menu .dropdown-item').forEach(function(i) { i.classList.remove('selected'); });
    var radioFast = el.newChatForm.querySelector('input[name="modal-model"][value="fast"]');
    if (radioFast) radioFast.checked = true;
    el.newChatModal.classList.add('open');
    setTimeout(function() { el.modalChatTitle.focus(); }, 100);
  }
}

export function closeNewChatModal() {
  if (el.newChatModal) el.newChatModal.classList.remove('open');
}

export function openSettingsModal() {
  var modal = document.getElementById('settings-modal');
  if (modal) {
    document.querySelectorAll('.custom-dropdown.open').forEach(function(d) { d.classList.remove('open'); });
    syncAppearanceControls();
    syncUserInfo();
    // Default to the previously active pane (or Profile on first open)
    var activePane = (settingsActivePane && document.getElementById('settings-pane-' + settingsActivePane)) ? settingsActivePane : 'profile';
    showSettingsPane(activePane);
    modal.classList.add('open');
  }
}

/**
 * Reflect current theme / font-size / reduce-motion preferences onto the
 * Appearance pane's segmented controls and toggle.
 */
function syncAppearanceControls() {
  var theme = document.documentElement.getAttribute('data-theme') || 'light';
  document.querySelectorAll('#settings-theme-seg .settings-seg-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.getAttribute('data-theme-value') === theme);
  });

  var fontSize = document.documentElement.getAttribute('data-font-scale') || 'md';
  document.querySelectorAll('#settings-font-seg .settings-seg-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.getAttribute('data-font-value') === fontSize);
  });

  var reduceCb = document.getElementById('settings-reduce-motion');
  if (reduceCb) reduceCb.checked = document.documentElement.classList.contains('reduce-motion');
}

/**
 * Switch the Settings modal to the given pane ('profile' | 'appearance' | 'developer' | 'data').
 * Toggles the active nav button and shows the matching right-side pane.
 */
var settingsActivePane = 'profile';

export function showSettingsPane(pane) {
  settingsActivePane = pane;
  var navBtns = document.querySelectorAll('.settings-nav-btn[data-settings-pane]');
  navBtns.forEach(function(btn) {
    btn.classList.toggle('active', btn.getAttribute('data-settings-pane') === pane);
  });
  var panes = document.querySelectorAll('.settings-pane');
  panes.forEach(function(p) {
    p.classList.toggle('active', p.id === 'settings-pane-' + pane);
  });
}

/**
 * Sync the logged-in user's name/email/role/avatar from kr_user into
 * the sidebar profile card and the Settings modal.
 */
export function syncUserInfo() {
  var u = null;
  try { u = JSON.parse(getAuth('kr_user')); } catch (e) { u = null; }

  // Only treat a real, non-empty name as the user's name; otherwise show nothing
  // in the sidebar instead of the "User" placeholder text.
  var realName = u && u.name ? String(u.name).trim() : '';
  var email    = (u && u.email) || '';
  var role     = (u && u.role)  || '';

  // Sidebar — show name only if it really exists, hide the generic "User" role line
  var sideName  = document.getElementById('sidebar-user-name');
  var sideEmail = document.getElementById('sidebar-user-email');
  var sideRole  = document.getElementById('sidebar-user-role');
  if (sideName) {
    sideName.textContent = realName;
    sideName.style.display = realName ? '' : 'none';
  }
  if (sideEmail) { sideEmail.textContent = email; sideEmail.title = email; sideEmail.style.display = email ? '' : 'none'; }
  if (sideRole) {
    if (role && role.toLowerCase() !== 'user') {
      sideRole.textContent = role.charAt(0).toUpperCase() + role.slice(1);
      sideRole.style.display = '';
    } else {
      sideRole.style.display = 'none';
    }
  }

  // Settings modal — header + separated profile fields
  var setAvatar = document.getElementById('settings-user-avatar');
  var setName   = document.getElementById('settings-user-name');
  var setEmail  = document.getElementById('settings-user-email');
  if (setAvatar) setAvatar.textContent = getInitials(realName);
  if (setName)   { setName.textContent = realName; setName.style.display = realName ? '' : 'none'; }
  if (setEmail)  setEmail.textContent  = email || '—';

  var fieldName  = document.getElementById('settings-field-name');
  var fieldEmail = document.getElementById('settings-field-email');
  var fieldRole  = document.getElementById('settings-field-role');
  var fieldId    = document.getElementById('settings-field-id');
  if (fieldName)  fieldName.value  = realName;
  if (fieldEmail) fieldEmail.value = email || '—';
  if (fieldRole)  fieldRole.value  = (role || 'user').charAt(0).toUpperCase() + (role || 'user').slice(1);
  if (fieldId)    fieldId.value    = (u && u.user_id) || '—';
}

function getInitials(name) {
  return String(name || 'U')
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(function(w) { return w.charAt(0).toUpperCase(); })
    .join('') || 'U';
}

export function closeSettingsModal() {
  var modal = document.getElementById('settings-modal');
  if (modal) modal.classList.remove('open');
}

export function openSearchModal() {
  if (el.searchModal) {
    el.searchInput.value = '';
    el.searchModal.classList.add('open');
    setTimeout(function() { 
      el.searchInput.focus(); 
      el.searchInput.dispatchEvent(new Event('input'));
    }, 100);
  }
}

export function closeSearchModal() {
  if (el.searchModal) {
    el.searchModal.classList.remove('open');
    el.chatInput.focus();
  }
}

export function openShareModal(url) {
  var modal = document.getElementById('share-modal');
  var input = document.getElementById('share-link-input');
  var toast = document.getElementById('share-toast');
  if (!modal) return;
  if (input) input.value = url;
  if (toast) toast.style.display = 'none';
  modal.classList.add('open');
}

export function closeShareModal() {
  var modal = document.getElementById('share-modal');
  if (modal) modal.classList.remove('open');
}

export function populateBookDropdowns(books) {
  // Populate modal-book-menu
  var modalMenu = document.getElementById('modal-book-menu');
  var deleteMenu = document.getElementById('settings-delete-book-menu');
  
  if (modalMenu) {
    modalMenu.innerHTML = '';
  }
  if (deleteMenu) {
    deleteMenu.innerHTML = '';
  }
  
  if (!books || books.length === 0) return;
  
  books.forEach(function(b) {
    var title = b.title || b.book_id;
    var pages = b.total_pages || 0;
    var chunks = b.total_chunks || 0;
    
    if (modalMenu) {
      var item = document.createElement('button');
      item.type = 'button';
      item.className = 'dropdown-item book-dropdown-item';
      item.setAttribute('data-value', b.book_id);
      item.innerHTML =
        '<span class="book-item-title">' + escapeHtml(title) + '</span>' +
        '<span class="book-item-meta">' + pages + ' pages &nbsp;·&nbsp; ' + chunks + ' chunks</span>';
      modalMenu.appendChild(item);
    }
    
    if (deleteMenu) {
      var delItem = document.createElement('button');
      delItem.type = 'button';
      delItem.className = 'dropdown-item';
      delItem.setAttribute('data-value', b.book_id);
      delItem.style.cssText = 'display: flex; justify-content: space-between; align-items: center; width: 100%;';
      delItem.innerHTML =
        '<span style="font-size: 14px;">' + escapeHtml(title) + '</span>' +
        '<span style="font-size: 12px; color: var(--mute); margin-left: 12px; white-space: nowrap;">' + pages + ' pages, ' + chunks + ' chunks</span>';
      deleteMenu.appendChild(delItem);
    }
  });
  
  // Re-bind click handlers on the NEW items for each dropdown
  // (initCustomFormDropdown already set up toggle/document handlers at module level)
  _rebindDropdownItems('modal-book-dropdown', 'modal-book-toggle', 'modal-book-label', 'modal-book-select', 'modal-book-menu');
  _rebindDropdownItems('settings-delete-book-dropdown', 'settings-delete-book-toggle', 'settings-delete-book-label', 'settings-delete-book-select', 'settings-delete-book-menu');
}

function _rebindDropdownItems(dropdownId, toggleId, labelId, selectId, menuId) {
  var dropdown = document.getElementById(dropdownId);
  var toggle = document.getElementById(toggleId);
  var label = document.getElementById(labelId);
  var select = document.getElementById(selectId);
  var menu = document.getElementById(menuId);
  if (!dropdown || !toggle || !menu) return;
  
  var items = menu.querySelectorAll('.dropdown-item');
  
  items.forEach(function(item) {
    // Remove old listeners by cloning (cleanest way)
    var clone = item.cloneNode(true);
    item.parentNode.replaceChild(clone, item);
    
    clone.addEventListener('click', function(e) {
      e.stopPropagation();
      var allItems = menu.querySelectorAll('.dropdown-item');
      allItems.forEach(function(i) { i.classList.remove('selected'); });
      clone.classList.add('selected');
      
      var val = clone.getAttribute('data-value');
      var titleEl = clone.querySelector('.book-item-title') || clone.querySelector('span');
      var text = titleEl ? titleEl.textContent.trim() : val;
      select.value = val;
      label.textContent = text;
      label.style.color = 'var(--ink)';
      dropdown.classList.remove('open');
      menu.classList.remove('open');
    });
  });
}

export function initCustomFormDropdown(dropdownId, toggleId, labelId, selectId, menuId) {
  var dropdown = document.getElementById(dropdownId);
  var toggle = document.getElementById(toggleId);
  var label = document.getElementById(labelId);
  var select = document.getElementById(selectId);
  var menu = document.getElementById(menuId);
  if (!dropdown || !toggle || !menu) return;

  var items = menu.querySelectorAll('.dropdown-item');
  var isOpen = false;

  document.body.appendChild(menu);
  menu.style.cssText = 'position:fixed;z-index:99999;transform:none;transition:opacity 0.15s, visibility 0.15s';

  function positionMenu() {
    var rect = toggle.getBoundingClientRect();
    menu.style.top = (rect.bottom + 4) + 'px';
    menu.style.left = rect.left + 'px';
    menu.style.width = rect.width + 'px';
    menu.style.bottom = 'auto';
    menu.style.right = 'auto';
  }

  function openMenu() { positionMenu(); dropdown.classList.add('open'); menu.classList.add('open'); isOpen = true; }
  function closeMenu() { dropdown.classList.remove('open'); menu.classList.remove('open'); isOpen = false; }

  toggle.addEventListener('click', function(e) {
    e.stopPropagation();
    if (isOpen) closeMenu(); else openMenu();
  });

  document.addEventListener('click', function(e) {
    if (isOpen && !toggle.contains(e.target) && !menu.contains(e.target)) closeMenu();
  });

  window.addEventListener('scroll', function() { if (isOpen) positionMenu(); }, true);
  window.addEventListener('resize', function() { if (isOpen) positionMenu(); });

  items.forEach(function(item) {
    item.addEventListener('click', function(e) {
      e.stopPropagation();
      items.forEach(function(i) { i.classList.remove('selected'); });
      item.classList.add('selected');
      var val = item.getAttribute('data-value');
      var titleEl = item.querySelector('.book-item-title') || item.querySelector('span');
      var text = titleEl ? titleEl.textContent.trim() : val;
      select.value = val;
      label.textContent = text;
      label.style.color = 'var(--ink)';
      closeMenu();
    });
  });
}
