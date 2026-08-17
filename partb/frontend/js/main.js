import { state, booksMap, API, apiFetch, authHeaders,  loadChats,  loadMessages,  loadBooks,  isAuthed } from './state.js';
import {
  el, renderSessions, renderMessages, updateHeader,
  showTypingIndicator, removeTypingIndicator,
  openNewChatModal, closeNewChatModal,
  openSettingsModal, closeSettingsModal,
  openSearchModal, closeSearchModal,
  openShareModal, closeShareModal,
  initCustomFormDropdown, populateBookDropdowns, syncUserInfo, showSettingsPane
} from './render.js?v=40';
import { initPdfViewer } from './pdf.js?v=37';

let currentAbortController = null;
let currentReader = null;
let streamStopped = false;
let editingPairId = null;
let promptHistoryIndex = -1;   // -1 = at draft, 0 = newest prompt
let promptHistoryDraft = '';
const stopGenerateBtn = document.getElementById('stop-generate-btn');
const sendBtn = document.getElementById('send-message-btn');

if (stopGenerateBtn) {
  stopGenerateBtn.addEventListener('click', () => {
    streamStopped = true;
    currentReader?.cancel();
    currentAbortController?.abort();
  });
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// In-place update of the last bot message's content — avoids full DOM rebuild flicker
function updateLastBotContent(text) {
  if (!el.messageThread) return;
  var msgs = el.messageThread.querySelectorAll('.message.bot');
  if (msgs.length === 0) return;
  var last = msgs[msgs.length - 1];
  var contentDiv = last.querySelector('.message-content');
  if (!contentDiv) return;

  // Strip citation patterns before markdown parsing
  var cleanText = text.replace(/\[Book:[^\]]*?\]/g, '');

  // Format thinking tags
  cleanText = cleanText.replace(/<think>([\s\S]*?)(?:<\/think>|$)/gi, function(match, p1) {
    var isOpen = !match.toLowerCase().endsWith('</think>') ? ' open' : '';
    return '<details class="think-accordion" style="margin-bottom: 12px; border: 1px solid var(--hairline); border-radius: var(--radius-sm); background: var(--surface-soft);"' + isOpen + '><summary style="cursor: pointer; padding: 8px 12px; font-size: 13px; font-weight: 500; color: var(--mute); user-select: none;">Thinking and gathering information...</summary><div style="padding: 12px; border-top: 1px solid var(--hairline); font-size: 13px; color: var(--mute); white-space: pre-wrap;">' + escapeHtml(p1) + '</div></details>\n\n';
  });

  if (window.marked) {
    contentDiv.innerHTML = marked.parse(cleanText, { breaks: true });
  } else {
    contentDiv.textContent = text;
  }

  var afterThink = text.replace(/<think>[\s\S]*?(?:<\/think>|$)/gi, '').trim();
  last.classList.toggle('thinking', /^Thinking:/i.test(text.trim()) || !afterThink);

  // Auto-scroll to bottom
  if (el.chatMessages) {
    el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
  }
}

if (!isAuthed()) {
  window.location.href = '/pages/login.html';
}

async function switchSession(id, skipPushState) {
  if (id === state.activeSessionId) return;
  editingPairId = null;
  promptHistoryIndex = -1;
  promptHistoryDraft = '';
  state.activeSessionId = id;
  localStorage.setItem('krutrim-last-chat', id);
  if (!skipPushState) history.pushState({ chatId: id }, '', '/chat/' + id);
  renderSessions();
  updateHeader();
  var session = state.sessionMap[id];
  if (!session?.messages?.length) {
    if (el.messageThread) el.messageThread.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--mute);">Loading...</div>';
    await loadMessages(id);
  }
  renderMessages();
  el.chatInput.disabled = false;
  var draft = localStorage.getItem('krutrim-draft:' + id);
  if (draft) { el.chatInput.value = draft; el.chatInput.focus(); }
}

async function sendMessage(text) {
  var trimmed = text.trim();
  if (!trimmed) return;
  if (!state.activeSessionId || !state.sessionMap[state.activeSessionId]) {
    await createNewSession("New Chat", "clean-code", "balanced");
  }
  var chatId = state.activeSessionId;
  var s = state.sessionMap[chatId];
  if (!s.versionGroups) s.versionGroups = state.versionGroups;

  var pairId, versionIdx;
  if (editingPairId && s.versionGroups[editingPairId]) {
    var vg = s.versionGroups[editingPairId];
    versionIdx = vg.versionCount;
    vg.versionCount++;
    vg.activeVersion = versionIdx;
    pairId = editingPairId;
    editingPairId = null;
  } else {
    pairId = 'p_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
    versionIdx = 0;
    s.versionGroups[pairId] = { activeVersion: 0, versionCount: 1 };
  }

  s.messages.push({ sender: 'user', text: trimmed, pairId, versionIdx });
  renderMessages();
  el.chatForm.reset();
  localStorage.removeItem('krutrim-draft:' + chatId);
  el.chatInput.style.height = 'auto';
  promptHistoryIndex = -1;
  promptHistoryDraft = '';
  el.chatInput.disabled = true;
  showTypingIndicator();
  
  if (sendBtn && stopGenerateBtn) {
    sendBtn.style.display = 'none';
    stopGenerateBtn.style.display = 'inline-flex';
  }
  
  currentAbortController = new AbortController();
  currentReader = null;
  streamStopped = false;

  function guardedRender() {
    if (state.activeSessionId === chatId) renderMessages();
  }

  try {
    const res = await apiFetch(`/chats/${chatId}/ask`, {
      method: "POST",
      body: JSON.stringify({ question: trimmed, mode: s.model || "balanced" }),
      signal: currentAbortController.signal
    });
    
    if (!res || !res.ok) throw new Error("Failed to send message");
    
    const reader = res.body.getReader();
    currentReader = reader;
    const decoder = new TextDecoder();
    let fulltext = "";
    let sources = [];
    let buf = "";

    removeTypingIndicator();
    s.messages.push({ sender: 'bot', text: "Thinking...", isStreaming: true, pairId, versionIdx });
    if (state.activeSessionId === chatId) {
      renderMessages();
      var bots = el.messageThread?.querySelectorAll('.message.bot');
      if (bots?.length) bots[bots.length - 1].classList.add('thinking');
    }

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        let evt;
        try { evt = JSON.parse(line.slice(5).trim()); } catch(e) { continue; }
        
        if (evt.type === "status") {
            s.messages[s.messages.length - 1].text = `Thinking: ${evt.message}...`;
            if (state.activeSessionId === chatId) updateLastBotContent(s.messages[s.messages.length - 1].text);
        } else if (evt.type === "token") {
          fulltext += evt.content;
          s.messages[s.messages.length - 1].text = fulltext;
          if (state.activeSessionId === chatId) updateLastBotContent(fulltext);
        } else if (evt.type === "done") {
          sources = evt.sources || [];
          s.messages[s.messages.length - 1].sources = sources;
          if (evt.metrics) {
            s.messages[s.messages.length - 1].metrics = evt.metrics;
            s.messages[s.messages.length - 1].metrics._ts = Date.now();
          }
          s.messages[s.messages.length - 1].text = fulltext;
          s.messages[s.messages.length - 1].message_id = evt.message_id;
          delete s.messages[s.messages.length - 1].isStreaming;
          guardedRender();
        } else if (evt.type === "error") {
          s.messages[s.messages.length - 1].text = `Error: ${evt.message}`;
          delete s.messages[s.messages.length - 1].isStreaming;
          guardedRender();
        }
      }
    }

    if (streamStopped) {
      s.messages[s.messages.length - 1].text += " (Stopped)";
      delete s.messages[s.messages.length - 1].isStreaming;
      guardedRender();
    }

  } catch(e) {
    console.error(e);
    removeTypingIndicator();
    if (e.name === 'AbortError') {
      if (s.messages[s.messages.length - 1]?.sender === 'bot') {
        s.messages[s.messages.length - 1].text += " (Stopped)";
        delete s.messages[s.messages.length - 1].isStreaming;
      }
    } else {
      s.messages.push({ sender: 'bot', text: "Error: Could not get response." });
    }
    if (state.activeSessionId === chatId) renderMessages();
  } finally {
    if (sendBtn && stopGenerateBtn) {
      stopGenerateBtn.style.display = 'none';
      sendBtn.style.display = 'inline-flex';
    }
    el.chatInput.disabled = false;
    el.chatInput.focus();
    currentAbortController = null;
    currentReader = null;
    streamStopped = false;
  }
}


async function createNewSession(title, book, model) {
  try {
    var fallbackBook = book || Object.keys(booksMap)[0] || null;
    if (!fallbackBook) {
      alert('No books available. Please upload a book first.');
      return;
    }
    const res = await apiFetch("/chats", {
      method: "POST",
      body: JSON.stringify({ book_ids: [fallbackBook], default_mode: model || "balanced", title: title || "New Chat" })
    });
    if(!res || !res.ok) return;
    const data = await res.json();
    const id = data.id || data.chat_id || data._id; // fallback for different backends
    if (id) {
      state.sessionMap[id] = {
        id: id,
        name: data.title || title,
        book: (data.book_ids && data.book_ids[0]) || book,
        model: data.default_mode || model,
        messages: []
      };
      state.activeSessionId = id;
      localStorage.setItem('krutrim-last-chat', id);
      history.pushState({ chatId: id }, '', '/chat/' + id);
      renderSessions();
      updateHeader();
      renderMessages();
      el.chatInput.disabled = false;
      el.chatInput.focus();
    }
  } catch(e) {
    console.error("Failed to create session", e);
  }
}

function getPromptHistory() {
  var s = state.sessionMap[state.activeSessionId];
  var out = [];
  if (s && s.messages) {
    for (var i = 0; i < s.messages.length; i++) {
      var m = s.messages[i];
      if (m.sender === 'user' && m.text && m.text.trim()) out.push(m.text);
    }
  }
  return out;
}

// -- Event listeners --


el.chatForm.addEventListener('submit', function (e) {
  e.preventDefault();
  var text = el.chatInput.value;
  if (text.trim()) sendMessage(text);
});

if (el.chatInput) {
  el.chatInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      var text = el.chatInput.value;
      if (text.trim()) sendMessage(text);
    } else if (e.key === 'ArrowUp' && !e.shiftKey &&
               el.chatInput.selectionStart === 0 && el.chatInput.selectionEnd === 0) {
      var hist = getPromptHistory();
      if (!hist.length) return;
      e.preventDefault();
      if (promptHistoryIndex === -1) promptHistoryDraft = el.chatInput.value;
      promptHistoryIndex = Math.min(promptHistoryIndex + 1, hist.length - 1);
      el.chatInput.value = hist[hist.length - 1 - promptHistoryIndex];
      el.chatInput.setSelectionRange(0, 0);
    } else if (e.key === 'ArrowDown' && !e.shiftKey &&
               el.chatInput.selectionStart === el.chatInput.value.length && promptHistoryIndex >= 0) {
      var hist2 = getPromptHistory();
      e.preventDefault();
      promptHistoryIndex--;
      if (promptHistoryIndex < 0) {
        el.chatInput.value = promptHistoryDraft;
        promptHistoryDraft = '';
      } else {
        el.chatInput.value = hist2[hist2.length - 1 - promptHistoryIndex];
      }
      var endPos = el.chatInput.value.length;
      el.chatInput.setSelectionRange(endPos, endPos);
    }
    el.chatInput.style.height = 'auto';
    el.chatInput.style.height = el.chatInput.scrollHeight + 'px';
  });
  // Auto-save draft per chat so it survives a reload (best practice)
  el.chatInput.addEventListener('input', function() {
    var text = el.chatInput.value;
    if (!state.activeSessionId) return;
    if (text.trim()) {
      localStorage.setItem('krutrim-draft:' + state.activeSessionId, text);
    } else {
      localStorage.removeItem('krutrim-draft:' + state.activeSessionId);
    }
  });
}

if (el.newSessionBtn) {
  el.newSessionBtn.addEventListener('click', function () { openNewChatModal(); });
}

var toggleBtn = document.getElementById('sidebar-toggle');
if (toggleBtn) {
  toggleBtn.addEventListener('click', function () { 
    if (window.innerWidth > 768) {
      document.querySelector('.chat-layout').classList.toggle('sidebar-closed');
    } else {
      el.sidebar.classList.toggle('open'); 
    }
  });
  document.addEventListener('click', function (e) {
    if (window.innerWidth <= 768 && el.sidebar.classList.contains('open') &&
        !el.sidebar.contains(e.target) && e.target !== toggleBtn && !toggleBtn.contains(e.target)) {
      el.sidebar.classList.remove('open');
    }
  });
}

// Session search
var searchInput = document.getElementById('session-search');
if (searchInput) {
  searchInput.addEventListener('input', function () {
    var q = this.value.toLowerCase().trim();
    var items = document.querySelectorAll('.chat-session');
    var visibleCount = 0;
    items.forEach(function (item) {
      var name = item.querySelector('.chat-session-name');
      var match = !q || (name && name.textContent.toLowerCase().includes(q));
      item.style.display = match ? '' : 'none';
      if (match) visibleCount++;
    });
    var emptyMsg = document.querySelector('.sidebar-list-empty');
    if (items.length > 0 && visibleCount === 0) {
      if (!emptyMsg) {
        emptyMsg = document.createElement('div');
        emptyMsg.className = 'sidebar-list-empty';
        emptyMsg.textContent = 'No chats found';
        document.getElementById('session-list').appendChild(emptyMsg);
      }
    } else if (emptyMsg) {
      emptyMsg.remove();
    }
  });
}

// Scroll to bottom button
var scrollBtn = document.getElementById('scroll-to-bottom-btn');
var messagesContainer = document.getElementById('chat-messages');
if (scrollBtn && messagesContainer) {
  messagesContainer.addEventListener('scroll', function() {
    var distanceToBottom = messagesContainer.scrollHeight - messagesContainer.scrollTop - messagesContainer.clientHeight;
    if (distanceToBottom > 150) {
      scrollBtn.classList.add('visible');
    } else {
      scrollBtn.classList.remove('visible');
    }
  });
  
  scrollBtn.addEventListener('click', function() {
    messagesContainer.scrollTo({
      top: messagesContainer.scrollHeight,
      behavior: 'smooth'
    });
  });
}

var sidebarOpenSearchBtn = document.getElementById('sidebar-search-trigger');
if (sidebarOpenSearchBtn) {
  sidebarOpenSearchBtn.addEventListener('click', function() {
    openSearchModal();
  });
}

// Theme toggle
var themeToggle = document.getElementById('theme-toggle');
var savedTheme = localStorage.getItem('krutrim-theme') || 'light';
document.documentElement.setAttribute('data-theme', savedTheme);

// Font scale + reduce motion preferences
var savedFontScale = localStorage.getItem('krutrim-font-scale') || 'md';
document.documentElement.setAttribute('data-font-scale', savedFontScale);
if (localStorage.getItem('krutrim-reduce-motion') === 'true') {
  document.documentElement.classList.add('reduce-motion');
}
if (themeToggle) {
  themeToggle.innerHTML = savedTheme === 'dark'
    ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>'
    : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';
  themeToggle.addEventListener('click', function () {
    var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('krutrim-theme', next);
    themeToggle.innerHTML = next === 'dark'
      ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>'
      : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';
  });
}

// Model dropdown
var modelDropdown = document.getElementById('model-dropdown');
var modelToggle = document.getElementById('model-dropdown-toggle');
if (modelDropdown && modelToggle) {
  modelToggle.addEventListener('click', function(e) {
    e.stopPropagation();
    modelDropdown.classList.toggle('open');
  });
  document.addEventListener('click', function(e) {
    if (!modelDropdown.contains(e.target)) modelDropdown.classList.remove('open');
  });
  document.querySelectorAll('#model-dropdown-menu .dropdown-item').forEach(function(item) {
    item.addEventListener('click', function() {
      var items = document.querySelectorAll('#model-dropdown-menu .dropdown-item');
      items.forEach(function(i) { i.classList.remove('selected'); });
      item.classList.add('selected');
      document.getElementById('model-dropdown-label').textContent = item.querySelector('span').textContent;
      var svg = item.querySelector('svg').cloneNode(true);
      var oldSvg = modelToggle.querySelector('.dropdown-icon');
      if (oldSvg) { svg.classList.add('dropdown-icon'); modelToggle.replaceChild(svg, oldSvg); }
      modelDropdown.classList.remove('open');
      var modelVal = item.getAttribute('data-value');
      if (state.activeSessionId && state.sessionMap[state.activeSessionId]) {
        state.sessionMap[state.activeSessionId].model = modelVal;
      }
    });
  });
}

// Session click delegation
document.addEventListener('click', function(e) {
  var sessionDiv = e.target.closest('.chat-session');
  if (sessionDiv && !e.target.closest('.chat-session-actions')) {
    switchSession(sessionDiv.dataset.id);
  }
});

// Custom event for switching sessions
document.addEventListener('switch-session', function(e) {
  if (e.detail && e.detail.id) {
    switchSession(e.detail.id);
  }
});

// Rename / Delete delegation
document.addEventListener('click', function(e) {
  var renameBtn = e.target.closest('.rename-btn');
  var deleteBtn = e.target.closest('.delete-btn');
  if (!renameBtn && !deleteBtn) return;
  var sessionDiv = e.target.closest('.chat-session');
  if (!sessionDiv) return;
  var id = sessionDiv.dataset.id;
  var s = state.sessionMap[id];
  if (!s) return;

  if (renameBtn) {
    var newName = prompt('Enter new chat name:', s.name);
    if (newName !== null && newName.trim() !== '') {
      s.name = newName.trim();
      apiFetch(`/chats/${id}`, { method: "PATCH", body: JSON.stringify({ title: s.name }) });
      renderSessions();
      updateHeader();
    }
  }

  if (deleteBtn) {
    if (confirm('Are you sure you want to delete this chat?')) {
      delete state.sessionMap[id];
      apiFetch(`/chats/${id}`, { method: "DELETE" });
      if (state.activeSessionId === id) {
        var remainingIds = Object.keys(state.sessionMap);
        state.activeSessionId = remainingIds.length > 0 ? remainingIds[0] : null;
        if (state.activeSessionId) {
            switchSession(state.activeSessionId);
        } else {
            history.pushState(null, '', '/');
            renderSessions();
            updateHeader();
            renderMessages();
            el.chatInput.disabled = true;
        }
      } else {
        renderSessions();
      }
    }
  }
});

document.addEventListener('click', function(e) {
  if (!e.target.closest('.chat-session-actions')) {
    document.querySelectorAll('.chat-session-actions.open').forEach(function(el2) { el2.classList.remove('open'); });
  }
});

// Edit message button — transforms the bubble into an editable dark area
document.addEventListener('click', function(e) {
  var editBtn = e.target.closest('.edit-msg-btn');
  if (!editBtn) return;
  var msgEl = editBtn.closest('.message.user');
  if (!msgEl) return;
  if (msgEl.querySelector('.bubble-edit-wrap')) return;
  var pairId = msgEl.dataset.pairId;
  var text = '';
  var s = state.sessionMap[state.activeSessionId];
  if (pairId && s) {
    var userMsg = s.messages.find(function(m) { return m.pairId === pairId && m.sender === 'user' && m.versionIdx === (s.versionGroups[pairId] || {}).activeVersion; });
    if (userMsg) text = userMsg.text;
  }
  if (!text) {
    var contentDiv = msgEl.querySelector('.message-content');
    if (contentDiv) text = contentDiv.textContent || '';
  }
  if (!text) return;
  editingPairId = pairId || null;
  msgEl.classList.add('editing-active');
  var contentDiv = msgEl.querySelector('.message-content');
  if (!contentDiv) return;
  var actionsEl = msgEl.querySelector('.user-msg-actions');
  if (actionsEl) actionsEl.style.display = 'none';
  var origWidth = contentDiv.offsetWidth;
  var origHtml = contentDiv.innerHTML;
  contentDiv.innerHTML = '';
  var wrap = document.createElement('div');
  wrap.className = 'bubble-edit-wrap';
  wrap.style.minWidth = origWidth + 'px';
  var ta = document.createElement('textarea');
  ta.className = 'bubble-edit-textarea';
  ta.value = text;
  wrap.appendChild(ta);
  var actions = document.createElement('div');
  actions.className = 'bubble-edit-actions';
  var cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'edit-cancel-btn';
  cancelBtn.textContent = 'Cancel';
  var sendBtn = document.createElement('button');
  sendBtn.type = 'button';
  sendBtn.className = 'edit-send-btn';
  sendBtn.textContent = 'Send';
  actions.appendChild(cancelBtn);
  actions.appendChild(sendBtn);
  wrap.appendChild(actions);
  contentDiv.appendChild(wrap);
  ta.focus();
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + 'px';
  ta.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = this.scrollHeight + 'px';
  });
  function exitEdit() {
    editingPairId = null;
    msgEl.classList.remove('editing-active');
    contentDiv.innerHTML = origHtml;
    if (actionsEl) actionsEl.style.display = '';
  }
  cancelBtn.addEventListener('click', exitEdit);
  sendBtn.addEventListener('click', function() {
    var newText = ta.value.trim();
    if (!newText) return;
    exitEdit();
    sendMessage(newText);
  });
  ta.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') exitEdit();
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendBtn.click();
    }
  });
});

// Copy user message button
document.addEventListener('click', function(e) {
  var copyBtn = e.target.closest('.copy-user-msg');
  if (!copyBtn) return;
  var msgEl = copyBtn.closest('.message.user');
  if (!msgEl) return;
  var text = '';
  var pairId = msgEl.dataset.pairId;
  if (pairId) {
    var s = state.sessionMap[state.activeSessionId];
    if (s) {
      var userMsg = s.messages.find(function(m) { return m.pairId === pairId && m.sender === 'user' && m.versionIdx === (s.versionGroups[pairId] || {}).activeVersion; });
      if (userMsg) text = userMsg.text;
    }
  }
  if (!text) {
    var contentDiv = msgEl.querySelector('.message-content');
    if (contentDiv) text = contentDiv.textContent || '';
  }
  if (!text) return;
  navigator.clipboard.writeText(text).then(function() {
    var icon = copyBtn.innerHTML;
    copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--success)"><polyline points="20 6 9 17 4 12"></polyline></svg>';
    setTimeout(function() { copyBtn.innerHTML = icon; }, 2000);
  });
});

// Version switcher
document.addEventListener('click', function(e) {
  var prevBtn = e.target.closest('.version-prev');
  var nextBtn = e.target.closest('.version-next');
  if (!prevBtn && !nextBtn) return;
  var btn = prevBtn || nextBtn;
  var pairId = btn.dataset.pairId;
  if (!pairId) return;
  var s = state.sessionMap[state.activeSessionId];
  if (!s) return;
  var vg = s.versionGroups[pairId];
  if (!vg) return;
  if (prevBtn) {
    vg.activeVersion = Math.max(0, vg.activeVersion - 1);
  } else {
    vg.activeVersion = Math.min(vg.versionCount - 1, vg.activeVersion + 1);
  }
  renderMessages();
  // Cancel any ongoing generation when switching versions
  currentReader?.cancel();
  currentAbortController?.abort();
});

// Regenerate button
document.addEventListener('click', function(e) {
  var regenBtn = e.target.closest('button[title="Regenerate"]');
  if (!regenBtn) return;
  var msgEl = regenBtn.closest('.message.bot');
  if (!msgEl) return;
  var pairId = msgEl.dataset.pairId;
  if (!pairId) return;
  var s = state.sessionMap[state.activeSessionId];
  if (!s) return;
  var vg = s.versionGroups[pairId];
  if (!vg) return;
  var userMsg = s.messages.find(function(m) {
    return m.pairId === pairId && m.sender === 'user' && m.versionIdx === vg.activeVersion;
  });
  if (!userMsg) return;
  editingPairId = pairId;
  sendMessage(userMsg.text);
});

// Copy bot message button
document.addEventListener('click', function(e) {
  var copyBtn = e.target.closest('.message.bot button[title="Copy"]');
  if (!copyBtn) return;
  var msgEl = copyBtn.closest('.message.bot');
  if (!msgEl) return;
  var pairId = msgEl.dataset.pairId;
  if (!pairId) return;
  var s = state.sessionMap[state.activeSessionId];
  if (!s) return;
  var vg = s.versionGroups[pairId];
  if (!vg) return;
  var botMsg = s.messages.find(function(m) {
    return m.pairId === pairId && m.sender === 'bot' && m.versionIdx === vg.activeVersion;
  });
  if (!botMsg) return;
  navigator.clipboard.writeText(botMsg.text).then(function() {
    var icon = copyBtn.innerHTML;
    copyBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--success)"><polyline points="20 6 9 17 4 12"></polyline></svg>';
    setTimeout(function() { copyBtn.innerHTML = icon; }, 2000);
  });
});

// Share bot message — generate shareable link for a single message
document.addEventListener('click', async function(e) {
  var shareBtn = e.target.closest('.message.bot button[title="Share"]');
  if (!shareBtn) return;
  if (!state.activeSessionId) return;
  var msgEl = shareBtn.closest('.message');
  var messageId = msgEl ? msgEl.dataset.messageId : null;
  if (!messageId) return;
  try {
    var url = '/chats/' + state.activeSessionId + '/share?message_id=' + encodeURIComponent(messageId);
    var res = await apiFetch(url, { method: 'POST' });
    if (!res || !res.ok) return;
    var data = await res.json();
    var shareUrl = window.location.origin + '/shared/' + data.share_token;
    openShareModal(shareUrl);
  } catch(err) {
    console.error('Share failed', err);
  }
});

// Share modal — copy button
var shareCopyBtn = document.getElementById('share-copy-btn');
if (shareCopyBtn) {
  shareCopyBtn.addEventListener('click', function() {
    var input = document.getElementById('share-link-input');
    if (!input) return;
    navigator.clipboard.writeText(input.value).then(function() {
      var toast = document.getElementById('share-toast');
      if (toast) { toast.style.display = 'block'; setTimeout(function() { toast.style.display = 'none'; }, 2500); }
      shareCopyBtn.textContent = 'Copied!';
      setTimeout(function() { shareCopyBtn.textContent = 'Copy'; }, 2000);
    });
  });
}

// Share modal — revoke button
var shareRevokeBtn = document.getElementById('share-revoke-btn');
if (shareRevokeBtn) {
  shareRevokeBtn.addEventListener('click', async function() {
    if (!state.activeSessionId) return;
    if (!confirm('Revoke this share link? Anyone with the link will no longer be able to view this chat.')) return;
    try {
      await apiFetch('/chats/' + state.activeSessionId + '/share', { method: 'DELETE' });
      closeShareModal();
    } catch(err) {
      console.error('Revoke failed', err);
    }
  });
}

// Share modal — close
var shareModalClose = document.getElementById('share-modal-close');
if (shareModalClose) shareModalClose.addEventListener('click', closeShareModal);
var shareModal = document.getElementById('share-modal');
if (shareModal) {
  shareModal.addEventListener('click', function(e) { if (e.target === shareModal) closeShareModal(); });
}

// Auto-expand textarea
if (el.chatInput) {
  el.chatInput.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = this.scrollHeight + 'px';
  });
}

// New chat modal
if (el.newChatForm) {
  el.newChatForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var title = el.modalChatTitle.value.trim();
    var book = el.modalBookSelect.value;
    var modelInput = el.newChatForm.querySelector('input[name="modal-model"]:checked');
    var model = modelInput ? modelInput.value : 'fast';
    if (title && book) { createNewSession(title, book, model); closeNewChatModal(); }
  });
}

if (el.modalCloseBtn) el.modalCloseBtn.addEventListener('click', closeNewChatModal);
if (el.modalCancelBtn) el.modalCancelBtn.addEventListener('click', closeNewChatModal);
if (el.newChatModal) {
  el.newChatModal.addEventListener('click', function (e) { if (e.target === el.newChatModal) closeNewChatModal(); });
}
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && el.newChatModal && el.newChatModal.classList.contains('open')) closeNewChatModal();
});

// Delete book — inline in Settings → Data pane
var settingsDeleteBookBtn = document.getElementById('settings-delete-book-btn');
if (settingsDeleteBookBtn) {
  settingsDeleteBookBtn.addEventListener('click', async function() {
    var select = document.getElementById('settings-delete-book-select');
    var bookId = select ? select.value : null;
    if (!bookId) {
      alert('Please select a book first.');
      return;
    }
    var bookObj = booksMap[bookId];
    var label = bookObj ? bookObj.name : bookId;
    if (!confirm('Delete the book "' + label + '"? This permanently removes its document, chunks, graph, and vector data. This cannot be undone.')) return;

    settingsDeleteBookBtn.disabled = true;
    try {
      const res = await apiFetch('/books/' + encodeURIComponent(bookId), { method: 'DELETE' });
      if (!res) return;
      if (!res.ok) {
        var errData = await res.json().catch(function() { return {}; });
        alert('Failed to delete: ' + (errData.detail || res.statusText));
        return;
      }
      // Remove from local maps & refresh dropdowns
      delete booksMap[bookId];
      Object.keys(state.sessionMap).forEach(function(id) {
        if (state.sessionMap[id].book === bookId) delete state.sessionMap[id];
      });
      if (state.activeSessionId && !state.sessionMap[state.activeSessionId]) {
        state.activeSessionId = null;
        history.pushState(null, '', '/');
        renderSessions();
        updateHeader();
        renderMessages();
        el.chatInput.disabled = true;
      } else {
        renderSessions();
      }
      var remaining = Object.keys(booksMap).map(function(id) {
        var b = booksMap[id];
        return { book_id: id, title: b.name, total_pages: b.pages, total_chunks: b.chunks };
      });
      populateBookDropdowns(remaining);
      // Reset the inline selector for next time
      select.value = '';
      var labelEl = document.getElementById('settings-delete-book-label');
      if (labelEl) { labelEl.textContent = 'Choose a book...'; labelEl.style.color = 'var(--mute)'; }
      alert('Book "' + label + '" has been successfully deleted from the database.');
    } catch (err) {
      console.error('Delete book failed', err);
      alert('Failed to delete the book. Please try again.');
    } finally {
      settingsDeleteBookBtn.disabled = false;
    }
  });
}

// Settings modal
var settingsModal = document.getElementById('settings-modal');
if (document.getElementById('settings-modal-close')) {
  document.getElementById('settings-modal-close').addEventListener('click', closeSettingsModal);
}
if (settingsModal) {
  settingsModal.addEventListener('click', function(e) { if (e.target === settingsModal) closeSettingsModal(); });
}
// Theme selector (Appearance pane)
function applyTheme(next) {
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('krutrim-theme', next);
  document.querySelectorAll('#settings-theme-seg .settings-seg-btn').forEach(function(b) {
    b.classList.toggle('active', b.getAttribute('data-theme-value') === next);
  });
  var tt = document.getElementById('theme-toggle');
  if (tt) {
    tt.innerHTML = next === 'dark'
      ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>'
      : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';
  }
}
var themeSeg = document.getElementById('settings-theme-seg');
if (themeSeg) {
  themeSeg.addEventListener('click', function(e) {
    var btn = e.target.closest('.settings-seg-btn[data-theme-value]');
    if (!btn) return;
    applyTheme(btn.getAttribute('data-theme-value'));
  });
}

// Font size selector (Appearance pane)
var fontSeg = document.getElementById('settings-font-seg');
if (fontSeg) {
  fontSeg.addEventListener('click', function(e) {
    var btn = e.target.closest('.settings-seg-btn[data-font-value]');
    if (!btn) return;
    var val = btn.getAttribute('data-font-value');
    document.documentElement.setAttribute('data-font-scale', val);
    localStorage.setItem('krutrim-font-scale', val);
    document.querySelectorAll('#settings-font-seg .settings-seg-btn').forEach(function(b) {
      b.classList.toggle('active', b.getAttribute('data-font-value') === val);
    });
  });
}

// Reduce motion toggle (Appearance pane)
var reduceMotionCb = document.getElementById('settings-reduce-motion');
if (reduceMotionCb) {
  reduceMotionCb.addEventListener('change', function() {
    document.documentElement.classList.toggle('reduce-motion', this.checked);
    localStorage.setItem('krutrim-reduce-motion', this.checked ? 'true' : 'false');
  });
}
// Settings two-pane navigation
document.addEventListener('click', function(e) {
  var navBtn = e.target.closest('.settings-nav-btn[data-settings-pane]');
  if (navBtn) showSettingsPane(navBtn.getAttribute('data-settings-pane'));
});
var userProfileCard = document.getElementById('user-profile-card');
if (userProfileCard) {
  userProfileCard.addEventListener('click', openSettingsModal);
  userProfileCard.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openSettingsModal();
    }
  });
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    if (settingsModal && settingsModal.classList.contains('open')) closeSettingsModal();
    var shareModalEl = document.getElementById('share-modal');
    if (shareModalEl && shareModalEl.classList.contains('open')) closeShareModal();
    if (el.searchModal && el.searchModal.classList.contains('open')) {
      if (el.searchInput && el.searchInput.value.trim().length > 0) {
        el.searchInput.value = '';
        el.searchInput.dispatchEvent(new Event('input'));
      } else {
        closeSearchModal();
      }
    }
  }
  // Search modal shortcut (Ctrl+K or Cmd+K)
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    e.preventDefault();
    if (el.searchModal && el.searchModal.classList.contains('open')) {
      closeSearchModal();
    } else {
      openSearchModal();
    }
  }
});

// Search functionality
if (el.searchInput) {
  el.searchInput.addEventListener('input', function() {
    var q = this.value.toLowerCase().trim();
    if (!el.searchResults) return;
    
    el.searchResults.innerHTML = '';
    var s = state.sessionMap[state.activeSessionId];
    
    if (!q) {
      // 1. Add "New chat" button
      var newChatDiv = document.createElement('div');
      newChatDiv.style.cssText = 'padding: 12px var(--space-lg); cursor: pointer; display: flex; align-items: center; gap: 12px; margin-bottom: 8px; border-radius: var(--radius-sm); margin-left: 8px; margin-right: 8px;';
      newChatDiv.onmouseover = function() { newChatDiv.style.backgroundColor = 'var(--surface-soft)'; };
      newChatDiv.onmouseout = function() { newChatDiv.style.backgroundColor = 'transparent'; };
      
      newChatDiv.innerHTML = 
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: var(--ink);"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>' +
        '<div style="font-size: 15px; color: var(--ink); font-weight: var(--weight-regular);">New chat</div>';
        
      newChatDiv.addEventListener('click', function() {
        closeSearchModal();
        openNewChatModal();
      });
      el.searchResults.appendChild(newChatDiv);

      var sessionIds = Object.keys(state.sessionMap);
      if (sessionIds.length > 0) {
        // Render chat items
        sessionIds.forEach(function(id) {
          var session = state.sessionMap[id];
          var div = document.createElement('div');
          div.style.cssText = 'padding: 12px var(--space-lg); cursor: pointer; display: flex; align-items: center; gap: 12px; border-radius: var(--radius-sm); margin-left: 8px; margin-right: 8px;';
          div.onmouseover = function() { div.style.backgroundColor = 'var(--surface-soft)'; };
          div.onmouseout = function() { div.style.backgroundColor = 'transparent'; };
          
          div.innerHTML = 
            '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: var(--ink);"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>' +
            '<div style="font-size: 15px; color: var(--ink); font-weight: var(--weight-regular);">' + escapeHtml(session.name) + '</div>';
            
          div.addEventListener('click', function() {
            var evt = new CustomEvent('switch-session', { detail: { id: id } });
            document.dispatchEvent(evt);
            closeSearchModal();
          });
          el.searchResults.appendChild(div);
        });
      }
      return;
    }
    
    if (!s && sessionIds.length === 0) {
      if (el.searchResultsEmpty) {
        el.searchResults.appendChild(el.searchResultsEmpty);
        el.searchResultsEmpty.style.display = 'block';
        el.searchResultsEmpty.textContent = 'No chats to search.';
      }
      return;
    }

    var matches = 0;
    
    // 1. Search Chat Titles
    var sessionIds = Object.keys(state.sessionMap);
    sessionIds.forEach(function(id) {
      var session = state.sessionMap[id];
      if (session.name.toLowerCase().includes(q)) {
        matches++;
        var div = document.createElement('div');
        div.style.cssText = 'padding: 12px var(--space-lg); cursor: pointer; display: flex; align-items: center; gap: 12px; border-radius: var(--radius-sm); margin-left: 8px; margin-right: 8px;';
        div.onmouseover = function() { div.style.backgroundColor = 'var(--surface-soft)'; };
        div.onmouseout = function() { div.style.backgroundColor = 'transparent'; };
        
        var escapedName = escapeHtml(session.name);
        var regex = new RegExp('(' + q.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&') + ')', 'gi');
        var highlightedName = escapedName.replace(regex, '<mark style="background: rgba(255, 215, 0, 0.4); color: inherit; border-radius: 2px; padding: 0 2px;">$1</mark>');

        div.innerHTML = 
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: var(--ink); flex-shrink: 0;"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>' +
          '<div style="font-size: 15px; color: var(--ink); font-weight: var(--weight-regular);">' + highlightedName + '</div>';
          
        div.addEventListener('click', function() {
          var evt = new CustomEvent('switch-session', { detail: { id: id } });
          document.dispatchEvent(evt);
          closeSearchModal();
        });
        el.searchResults.appendChild(div);
      }
    });

    // 2. Search Messages
    if (s && s.messages) {
      s.messages.forEach(function(msg, idx) {
        if (msg.text.toLowerCase().includes(q)) {
          matches++;
          var div = document.createElement('div');
          div.style.cssText = 'padding: 12px var(--space-lg); cursor: pointer; border-radius: var(--radius-sm); margin-left: 8px; margin-right: 8px; display: flex; flex-direction: column; gap: 4px;';
          div.onmouseover = function() { div.style.backgroundColor = 'var(--surface-soft)'; };
          div.onmouseout = function() { div.style.backgroundColor = 'transparent'; };
          
          var senderName = msg.sender === 'user' ? 'User' : 'Krutrim RAG';
          
          var textSnippet = "";
          var lowerMsg = msg.text.toLowerCase();
          var matchIdx = lowerMsg.indexOf(q);
          if (matchIdx !== -1) {
            var start = Math.max(0, matchIdx - 40);
            var end = Math.min(msg.text.length, matchIdx + q.length + 80);
            textSnippet = msg.text.substring(start, end);
            if (start > 0) textSnippet = '...' + textSnippet;
            if (end < msg.text.length) textSnippet = textSnippet + '...';
          } else {
            textSnippet = msg.text.substring(0, 150) + (msg.text.length > 150 ? '...' : '');
          }
          
          // Highlight the match
          var escapedSnippet = escapeHtml(textSnippet);
          var regex = new RegExp('(' + q.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&') + ')', 'gi');
          var highlighted = escapedSnippet.replace(regex, '<mark style="background: rgba(255, 215, 0, 0.4); color: inherit; border-radius: 2px; padding: 0 2px;">$1</mark>');
          
          div.innerHTML = 
            '<div style="font-size: 12px; color: var(--mute); font-weight: var(--weight-medium); text-transform: uppercase; letter-spacing: 0.05em;">' + senderName + '</div>' +
            '<div style="font-size: 15px; color: var(--ink); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; text-overflow: ellipsis;">' + highlighted + '</div>';
            
          div.addEventListener('click', function() {
            closeSearchModal();
            // Ideally we would scroll to the message, but for now we just close it
          });
          el.searchResults.appendChild(div);
        }
      });
    }

    if (matches === 0) {
      if (el.searchResultsEmpty) {
        el.searchResults.appendChild(el.searchResultsEmpty);
        el.searchResultsEmpty.style.display = 'block';
        el.searchResultsEmpty.textContent = 'No matches found.';
      }
    }
  });
}

var searchModalCloseBtn = document.getElementById('search-modal-close');
if (searchModalCloseBtn) {
  searchModalCloseBtn.addEventListener('click', closeSearchModal);
}
if (el.searchModal) {
  el.searchModal.addEventListener('click', function(e) { if (e.target === el.searchModal) closeSearchModal(); });
}

// Init — initialize dropdowns and PDF viewer at module level
initCustomFormDropdown('modal-book-dropdown', 'modal-book-toggle', 'modal-book-label', 'modal-book-select', 'modal-book-menu');
initCustomFormDropdown('settings-delete-book-dropdown', 'settings-delete-book-toggle', 'settings-delete-book-label', 'settings-delete-book-select', 'settings-delete-book-menu');
initPdfViewer();

async function init() {
  // Load real books from API
  const books = await loadBooks();
  if (books && books.length > 0) {
    populateBookDropdowns(books);
  }

  syncUserInfo();

  const ids = await loadChats();
  
  // Check URL for deep-link: /chat/{uuid}
  var urlChatId = null;
  var pathParts = window.location.pathname.split('/').filter(Boolean);
  if (pathParts[0] === 'chat' && pathParts[1]) {
    urlChatId = pathParts[1];
  }
  
  if (urlChatId && state.sessionMap[urlChatId]) {
    await switchSession(urlChatId);
  } else {
    // Reload without a /chat/ deep link — restore the last active conversation
    var lastChat = localStorage.getItem('krutrim-last-chat');
    if (lastChat && state.sessionMap[lastChat]) {
      await switchSession(lastChat);
    } else {
      renderSessions();
      updateHeader();
    }
  }
}
init();

// Browser back/forward — navigate to the chat in the URL
window.addEventListener('popstate', function(e) {
  var pathParts = window.location.pathname.split('/').filter(Boolean);
  if (pathParts[0] === 'chat' && pathParts[1] && state.sessionMap[pathParts[1]]) {
    switchSession(pathParts[1], true);
  } else if (window.location.pathname === '/') {
    state.activeSessionId = null;
    renderSessions();
    updateHeader();
    renderMessages();
  }
});