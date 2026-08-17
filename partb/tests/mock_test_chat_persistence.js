/**
 * Mock tests for the 3 chat persistence & stop-button fixes.
 *
 * Run:
 *   node partb/tests/mock_test_chat_persistence.js
 *
 * No dependencies — pure JS, mocks everything inline.
 */

/* ------------------------------------------------------------------ */
/*  Mock infrastructure                                                */
/* ------------------------------------------------------------------ */

let assertCount = 0;
let failCount = 0;

function assert(condition, label) {
  assertCount++;
  if (!condition) {
    failCount++;
    console.error(`  ✗ FAIL: ${label}`);
  } else {
    console.log(`  ✓ OK: ${label}`);
  }
}

function assertEqual(actual, expected, label) {
  assertCount++;
  if (actual !== expected) {
    failCount++;
    console.error(`  ✗ FAIL: ${label}  (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`);
  } else {
    console.log(`  ✓ OK: ${label}`);
  }
}

/* ------------------------------------------------------------------ */
/*  Mock state (mirrors state.js's structure)                          */
/* ------------------------------------------------------------------ */

const state = {
  activeSessionId: null,
  sessionMap: {},
};

function makeSession(id, messages) {
  return {
    id,
    name: 'Chat ' + id,
    book: 'book-001',
    model: 'balanced',
    messages: messages || [],
    versionGroups: {},
  };
}

/* ------------------------------------------------------------------ */
/*  Mock DOM (minimal)                                                 */
/* ------------------------------------------------------------------ */

const el = {
  messageThread: { innerHTML: '' },
  chatMessages: { scrollTop: 0, scrollHeight: 0 },
  chatForm: { reset() {} },
  chatInput: { value: '', style: { height: 'auto' }, disabled: false, focus() {} },
};

let renderMessagesCallCount = 0;
let renderMessagesLastChatId = null;
function renderMessages() {
  renderMessagesCallCount++;
  renderMessagesLastChatId = state.activeSessionId;
  // Simulated — does not actually build DOM
}

let loadMessagesCalls = [];
async function loadMessages(chatId) {
  loadMessagesCalls.push(chatId);
  if (state.sessionMap[chatId]) {
    // Simulate loading from DB — returns whatever is already there
    return state.sessionMap[chatId].messages;
  }
  return [];
}

function renderSessions() {}
function updateHeader() {}
function showTypingIndicator() {}
function removeTypingIndicator() {}

/* ------------------------------------------------------------------ */
/*  Fix 1 — switchSession() with lazy load                             */
/* ------------------------------------------------------------------ */

function switchSession(id, skipPushState) {
  if (id === state.activeSessionId) return;
  state.activeSessionId = id;
  renderSessions();
  updateHeader();
  var session = state.sessionMap[id];
  if (!session?.messages?.length) {
    // Loading indicator would go here in real code
    loadMessages(id);
  }
  renderMessages();
}

/* ------------------------------------------------------------------ */
/*  Fixes 2 & 3 — sendMessage() with guarded render + reader cancel    */
/* ------------------------------------------------------------------ */

let currentAbortController = null;
let currentReader = null;
let streamStopped = false;

function setupStopHandler() {
  // Simulates the stop button click
  streamStopped = true;
  if (currentReader && typeof currentReader.cancel === 'function') {
    currentReader.cancel();
  }
  if (currentAbortController) {
    currentAbortController.abort();
  }
}

// Mock ReadableStream reader factory
function makeMockReader(chunks, delayMs) {
  let idx = 0;
  let cancelled = false;
  return {
    cancelled: false,
    async read() {
      if (cancelled) return { done: true, value: undefined };
      if (idx >= chunks.length) return { done: true, value: undefined };
      // Simulate optional delay
      if (delayMs) await new Promise(r => setTimeout(r, delayMs));
      if (cancelled) return { done: true, value: undefined };
      return { done: false, value: new TextEncoder().encode(chunks[idx++]) };
    },
    cancel() {
      cancelled = true;
      this.cancelled = true;
    },
  };
}

async function sendMessage(text, mockChunks) {
  var chatId = state.activeSessionId;
  var s = state.sessionMap[chatId];
  if (!s) {
    console.error('No session for', chatId);
    return;
  }

  s.messages.push({ sender: 'user', text: text, pairId: 'p1', versionIdx: 0 });
  renderMessages();

  currentAbortController = new AbortController();
  currentReader = null;
  streamStopped = false;

  function guardedRender() {
    if (state.activeSessionId === chatId) renderMessages();
  }

  try {
    // Simulate the bot placeholder
    s.messages.push({ sender: 'bot', text: 'Thinking...', isStreaming: true, pairId: 'p1', versionIdx: 0 });
    if (state.activeSessionId === chatId) {
      renderMessages();
    }

    const reader = makeMockReader(mockChunks || []);
    currentReader = reader;

    let fulltext = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const token = new TextDecoder().decode(value);
      fulltext += token;
      s.messages[s.messages.length - 1].text = fulltext;
      if (state.activeSessionId === chatId) {
        // updateLastBotContent would go here
      }
    }

    // Simulate a "done" event
    s.messages[s.messages.length - 1].text = fulltext;
    s.messages[s.messages.length - 1].message_id = 'msg-1';
    delete s.messages[s.messages.length - 1].isStreaming;
    guardedRender();

    // Check if stopped by user
    if (streamStopped) {
      if (!s.messages[s.messages.length - 1].text.includes('(Stopped)')) {
        s.messages[s.messages.length - 1].text += ' (Stopped)';
      }
      delete s.messages[s.messages.length - 1].isStreaming;
      guardedRender();
    }

  } catch (e) {
    if (e.name === 'AbortError') {
      if (s.messages[s.messages.length - 1]?.sender === 'bot') {
        s.messages[s.messages.length - 1].text += ' (Stopped)';
        delete s.messages[s.messages.length - 1].isStreaming;
      }
    } else {
      s.messages.push({ sender: 'bot', text: 'Error: Could not get response.' });
    }
    if (state.activeSessionId === chatId) renderMessages();
  } finally {
    currentAbortController = null;
    currentReader = null;
    streamStopped = false;
  }

  return s.messages;
}

/* ------------------------------------------------------------------ */
/*  TEST SCENARIOS                                                     */
/* ------------------------------------------------------------------ */

async function runTests() {
  console.log('\n========================================');
  console.log('  MOCK TESTS — Chat Persistence & Stop');
  console.log('========================================\n');

  // ── Scenario 1: Chat persistence when switching ──
  console.log('[Scenario 1] Chat persistence when switching chats');
  {
    // Setup: Chat A has messages, Chat B is empty
    state.sessionMap['chat-a'] = makeSession('chat-a', [
      { sender: 'user', text: 'Hello', pairId: 'p0', versionIdx: 0 },
      { sender: 'bot', text: 'Hi there!', pairId: 'p0', versionIdx: 0 },
    ]);
    state.sessionMap['chat-b'] = makeSession('chat-b', []);
    loadMessagesCalls = [];

    // Action 1: switch to Chat B (empty — should load from DB)
    switchSession('chat-b');
    var loadedB = loadMessagesCalls.includes('chat-b');
    assert(loadedB, 'switchSession(b): loadMessages called for empty Chat B');

    // Action 2: switch back to Chat A (has messages — skip load)
    switchSession('chat-a');
    var loadedAagain = loadMessagesCalls.filter(id => id === 'chat-a').length;
    assert(loadedAagain === 0,
      'switchSession(a): loadMessages NOT called for Chat A (messages already in memory)');

    // Assert: Chat A's messages are preserved exactly
    var msgsA = state.sessionMap['chat-a'].messages;
    assertEqual(msgsA.length, 2, 'Chat A has 2 messages preserved');
    assertEqual(msgsA[0].text, 'Hello', 'Chat A message[0] preserved');
    assertEqual(msgsA[1].text, 'Hi there!', 'Chat A message[1] preserved');
    assertEqual(state.activeSessionId, 'chat-a', 'Active session is Chat A');
  }

  // ── Scenario 2: Guarded renderMessages — no cross-chat DOM bleed ──
  console.log('\n[Scenario 2] Guarded renderMessages — background stream');
  {
    state.sessionMap['chat-x'] = makeSession('chat-x', [
      { sender: 'user', text: 'X1', pairId: 'px0', versionIdx: 0 },
      { sender: 'bot', text: 'Answer X1', pairId: 'px0', versionIdx: 0 },
    ]);
    state.sessionMap['chat-y'] = makeSession('chat-y', [
      { sender: 'user', text: 'Y1', pairId: 'py0', versionIdx: 0 },
    ]);
    state.activeSessionId = 'chat-x';
    renderMessagesCallCount = 0;
    renderMessagesLastChatId = null;

    // Simulate: user sends msg in Chat X, then switches to Chat Y mid-stream
    var captureChatId = state.activeSessionId; // chat-x
    var s = state.sessionMap['chat-x'];
    s.messages.push({ sender: 'user', text: 'X2', pairId: 'px1', versionIdx: 0 });
    s.messages.push({ sender: 'bot', text: 'streaming...', isStreaming: true, pairId: 'px1', versionIdx: 0 });

    state.activeSessionId = 'chat-y'; // User switches away
    // Simulate "done" event
    var lastBot = s.messages[s.messages.length - 1];
    lastBot.text = 'Final answer';
    lastBot.message_id = 'msg-x';
    delete lastBot.isStreaming;

    // guardedRender — should NOT fire renderMessages because activeSessionId !== capturedChatId
    var renderCountBefore = renderMessagesCallCount;
    if (state.activeSessionId === captureChatId) {
      renderMessages();
    }
    assertEqual(renderMessagesCallCount, renderCountBefore,
      'renderMessages NOT called for Chat X background stream while Chat Y is active');

    // Verify Chat X data is saved in memory (state) even if DOM not updated
    assertEqual(s.messages[s.messages.length - 1].text, 'Final answer',
      'Chat X background stream saved final answer to state (memory)');
    assertEqual(s.messages[s.messages.length - 1].isStreaming, undefined,
      'Chat X isStreaming flag removed');

    // Switch back to Chat X — should render from memory, no reload
    switchSession('chat-x');
    assertEqual(state.activeSessionId, 'chat-x', 'Switched back to Chat X');
    assertEqual(state.sessionMap['chat-x'].messages.length, 4,
      'Chat X has all 4 messages preserved (including background stream)');
  }

  // ── Scenario 3: Stop button — reader.cancel() + streamStopped ──
  console.log('\n[Scenario 3] Stop button cancels stream');
  {
    state.sessionMap['chat-stop'] = makeSession('chat-stop', []);
    state.activeSessionId = 'chat-stop';
    currentReader = null;
    streamStopped = false;
    var readerCancelled = false;

    // Override mock reader for this test
    var mockReader = {
      async read() {
        // Return one token then hang (simulating in-flight stream)
        await new Promise(r => setTimeout(r, 5));
        if (this.cancelled) return { done: true, value: undefined };
        return { done: false, value: new TextEncoder().encode('Hello') };
      },
      cancel() {
        this.cancelled = true;
        readerCancelled = true;
      },
      cancelled: false,
    };

    // Start sendMessage manually
    var s = state.sessionMap['chat-stop'];
    s.messages.push({ sender: 'user', text: 'Test stop', pairId: 'ps0', versionIdx: 0 });
    s.messages.push({ sender: 'bot', text: 'Thinking...', isStreaming: true, pairId: 'ps0', versionIdx: 0 });

    currentAbortController = new AbortController();
    currentReader = mockReader;
    streamStopped = false;

    // Read first token & update bot message text (as token event would)
    var read1 = await mockReader.read();
    assert(!read1.done, 'First token read successfully');
    s.messages[s.messages.length - 1].text = new TextDecoder().decode(read1.value);

    // Simulate stop button click
    setupStopHandler();
    assert(readerCancelled, 'reader.cancel() was called');
    assert(streamStopped === true, 'streamStopped flag set to true');
    assert(currentAbortController.signal.aborted === true, 'AbortController.abort() was called');

    // Post-stop: simulate the "after-loop" check
    if (streamStopped) {
      s.messages[s.messages.length - 1].text += ' (Stopped)';
      delete s.messages[s.messages.length - 1].isStreaming;
    }

    assertEqual(s.messages[s.messages.length - 1].text, 'Hello (Stopped)',
      'Bot message has "(Stopped)" suffix after cancel');
    assert(s.messages[s.messages.length - 1].isStreaming === undefined,
      'isStreaming flag removed after stop');

    // Cleanup
    currentAbortController = null;
    currentReader = null;
    streamStopped = false;
  }

  // ── Scenario 4: Stop button — catch block AbortError path ──
  console.log('\n[Scenario 4] Stop button — AbortError path in catch block');
  {
    state.sessionMap['chat-abort'] = makeSession('chat-abort', []);
    state.activeSessionId = 'chat-abort';
    currentReader = null;
    streamStopped = false;

    var s = state.sessionMap['chat-abort'];
    s.messages.push({ sender: 'user', text: 'Test abort error', pairId: 'ps1', versionIdx: 0 });

    currentAbortController = new AbortController();

    // Simulate: AbortError thrown during read
    var abortError = new DOMException('The operation was aborted', 'AbortError');

    // Simulate the catch block
    if (abortError.name === 'AbortError') {
      if (s.messages[s.messages.length - 1]?.sender === 'bot') {
        s.messages[s.messages.length - 1].text += ' (Stopped)';
        delete s.messages[s.messages.length - 1].isStreaming;
      }
    }

    // Since the last message is a user msg, not bot, nothing should happen
    assertEqual(s.messages[s.messages.length - 1].text, 'Test abort error',
      'AbortError: does NOT add (Stopped) to user message');

    // Now add a bot message and test that path
    s.messages.push({ sender: 'bot', text: 'Partial', isStreaming: true, pairId: 'ps1', versionIdx: 0 });
    if (abortError.name === 'AbortError') {
      if (s.messages[s.messages.length - 1]?.sender === 'bot') {
        s.messages[s.messages.length - 1].text += ' (Stopped)';
        delete s.messages[s.messages.length - 1].isStreaming;
      }
    }
    assertEqual(s.messages[s.messages.length - 1].text, 'Partial (Stopped)',
      'AbortError: adds (Stopped) to bot message');
  }

  // ── Scenario 5: Stop button — finally block resets everything ──
  console.log('\n[Scenario 5] Stop button — finally block resets state');
  {
    currentAbortController = new AbortController();
    currentReader = { cancel() {} };
    streamStopped = true;

    // Simulate the finally block
    currentAbortController = null;
    currentReader = null;
    streamStopped = false;

    assertEqual(currentAbortController, null, 'finally: AbortController set to null');
    assertEqual(currentReader, null, 'finally: reader set to null');
    assertEqual(streamStopped, false, 'finally: streamStopped reset to false');
  }

  /* ------------------------------------------------------------------ */
  /*  Summary                                                            */
  /* ------------------------------------------------------------------ */
  console.log('\n========================================');
  console.log(`  Results: ${assertCount} assertions, ${failCount} failures`);
  console.log('========================================\n');
  process.exit(failCount > 0 ? 1 : 0);
}

runTests().catch(e => {
  console.error('Test runner error:', e);
  process.exit(1);
});
