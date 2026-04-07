import React, { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

const DEFAULT_REGISTER_FORM = { email: "", password: "", full_name: "", title: "", profile_bio: "" };
const DEFAULT_LOGIN_FORM = { email: "", password: "" };
const DEFAULT_CHAT_ID = "default";

function ExternalLink(props) {
  return <a {...props} target="_blank" rel="noreferrer noopener" />;
}

function parseAssistantFilePayload(content) {
  const raw = String(content || "").trim();
  if (!raw?.startsWith("{")) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") {
      return null;
    }
    const payloadType = String(parsed.type || "").toLowerCase();
    if (!["file", "image", "video"].includes(payloadType)) {
      return null;
    }
    const filename = String(parsed.filename || "generated.bin");
    const mimeType = String(parsed.mime_type || "application/octet-stream");
    const base64 = typeof parsed.base64 === "string" && parsed.base64 && parsed.base64 !== "<omitted>" ? parsed.base64 : "";
    const path = typeof parsed.path === "string" ? parsed.path.trim() : "";
    return { type: payloadType, filename, mimeType, base64, path };
  } catch {
    return null;
  }
}

function buildPayloadDataUrl(payload) {
  if (!payload?.base64) {
    return "";
  }
  return `data:${payload.mimeType};base64,${payload.base64}`;
}

function renderAttachmentMessage(payload) {
  if (!payload) {
    return null;
  }
  const dataUrl = buildPayloadDataUrl(payload);
  const openHref = dataUrl || payload.path || "";

  return (
    <div className="attachment-box">
      <div className="attachment-title">Generated file: {payload.filename}</div>
      <div className="attachment-meta">{payload.mimeType}</div>
      {payload.type === "image" && dataUrl ? <img className="attachment-preview" src={dataUrl} alt={payload.filename} /> : null}
      <div className="attachment-actions">
        {openHref ? (
          <a className="secondary" href={openHref} target="_blank" rel="noreferrer noopener" download={payload.filename}>
            Download
          </a>
        ) : null}
        {payload.path ? <span className="attachment-path">{payload.path}</span> : null}
      </div>
    </div>
  );
}

function readStoredToken() {
  try {
    return localStorage.getItem("smartai_token") || "";
  } catch {
    return "";
  }
}

function writeStoredToken(value) {
  try {
    if (value) {
      localStorage.setItem("smartai_token", value);
    } else {
      localStorage.removeItem("smartai_token");
    }
  } catch {
    // ignore storage policy errors
  }
}

function parseApiError(payload) {
  if (!payload) {
    return "";
  }

  if (typeof payload.detail === "string" && payload.detail.trim()) {
    return payload.detail.trim();
  }

  if (typeof payload.message === "string" && payload.message.trim()) {
    return payload.message.trim();
  }

  return "";
}

function normalizeMessages(items) {
  if (!Array.isArray(items)) {
    return [];
  }
  return items
    .map((item) => ({
      sender_type: String(item?.sender_type || "assistant"),
      sender_user_id: item?.sender_user_id ?? null,
      content: String(item?.content || ""),
      created_at: String(item?.created_at || new Date().toISOString()),
    }))
    .filter((item) => item.content.trim().length > 0);
}

function normalizeSessions(items) {
  if (!Array.isArray(items)) {
    return [];
  }
  return items
    .map((item) => ({
      chat_id: String(item?.chat_id || "").trim(),
      title: String(item?.title || "New Chat").trim() || "New Chat",
      created_at: String(item?.created_at || new Date().toISOString()),
      updated_at: String(item?.updated_at || new Date().toISOString()),
      deleted_at: item?.deleted_at ? String(item.deleted_at) : null,
      message_count: Number(item?.message_count || 0),
      preview: String(item?.preview || "").trim(),
    }))
    .filter((item) => item.chat_id.length > 0)
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
}

function groupSessionsByPeriod(items) {
  const now = Date.now();
  const weekMs = 7 * 24 * 60 * 60 * 1000;

  const thisWeek = [];
  const older = [];
  for (const session of items || []) {
    const ts = new Date(session?.updated_at || 0).getTime();
    if (Number.isFinite(ts) && now - ts <= weekMs) {
      thisWeek.push(session);
    } else {
      older.push(session);
    }
  }
  return [
    { title: "This week", items: thisWeek },
    { title: "Older", items: older },
  ];
}

function App() {
  const [mode, setMode] = useState("login");
  const [registerForm, setRegisterForm] = useState(DEFAULT_REGISTER_FORM);
  const [loginForm, setLoginForm] = useState(DEFAULT_LOGIN_FORM);

  const [token, setToken] = useState(readStoredToken);
  const [status, setStatus] = useState("Sign in to open your personal assistant chat.");
  const [isBusy, setIsBusy] = useState(false);
  const [wsStatus, setWsStatus] = useState("offline");

  const [chatSessions, setChatSessions] = useState([]);
  const [activeChatId, setActiveChatId] = useState(DEFAULT_CHAT_ID);
  const [showTrash, setShowTrash] = useState(false);
  const [trashCount, setTrashCount] = useState(0);
  const [menuChatId, setMenuChatId] = useState("");
  const [editingChatId, setEditingChatId] = useState("");
  const [editingTitle, setEditingTitle] = useState("");
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");

  const wsRef = useRef(null);
  const wsRetryTimerRef = useRef(null);
  const messagesEndRef = useRef(null);
  const activeChatIdRef = useRef(DEFAULT_CHAT_ID);

  const apiBase = useMemo(() => (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, ""), []);
  const isAuthenticated = Boolean(token);

  function performSessionLogout(reason = "Session expired. Please sign in again.") {
    closeSocket();
    writeStoredToken("");
    setToken("");
    setChatSessions([]);
    setTrashCount(0);
    setActiveChatId(DEFAULT_CHAT_ID);
    setMessages([]);
    setDraft("");
    setWsStatus("offline");
    setStatus(reason);
  }

  async function request(path, options = {}, withAuth = true) {
    const timeoutMs = Number(options.timeoutMs || 20000);
    const fetchOptions = { ...options };
    if ("timeoutMs" in fetchOptions) {
      delete fetchOptions.timeoutMs;
    }

    const headers = options.headers ? { ...options.headers } : {};
    if (!(options.body instanceof FormData) && !("Content-Type" in headers)) {
      headers["Content-Type"] = "application/json";
    }
    if (withAuth && token) {
      headers.Authorization = `Bearer ${token}`;
    }

    const controller = new AbortController();
    const timerId = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const response = await fetch(`${apiBase}${path}`, {
        ...fetchOptions,
        headers,
        signal: controller.signal,
      });

      let payload = null;
      const text = await response.text();
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = null;
        }
      }

      if (!response.ok) {
        if (withAuth && response.status === 401) {
          performSessionLogout("Session expired. Please sign in again.");
        }
        const details = parseApiError(payload) || response.statusText || "Request failed";
        throw new Error(details);
      }

      return payload;
    } finally {
      clearTimeout(timerId);
    }
  }

  function closeSocket() {
    if (wsRetryTimerRef.current) {
      clearTimeout(wsRetryTimerRef.current);
      wsRetryTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  }

  function buildChatWsUrl() {
    const explicit = (import.meta.env.VITE_API_WS_URL || "").trim();
    if (explicit) {
      const normalized = explicit.replace(/\/$/, "");
      return `${normalized}/chat/ws?token=${encodeURIComponent(token)}`;
    }
    const protocol = globalThis.location.protocol === "https:" ? "wss:" : "ws:";
    const host = globalThis.location.host;
    return `${protocol}//${host}/api/v1/chat/ws?token=${encodeURIComponent(token)}`;
  }

  async function fetchChatSessions(includeDeleted = showTrash) {
    if (!isAuthenticated) {
      setChatSessions([]);
      return [];
    }
    try {
      const payload = await request(`/chat/sessions?include_deleted=${includeDeleted ? "true" : "false"}`, { timeoutMs: 15000 });
      const sessions = normalizeSessions(payload?.sessions);
      setChatSessions(sessions);
      if (includeDeleted) {
        setTrashCount(sessions.filter((item) => Boolean(item.deleted_at)).length);
      }
      return sessions;
    } catch (error) {
      setStatus(`Failed to load chats: ${error.message}`);
      return [];
    }
  }

  async function createChatSession() {
    if (!isAuthenticated) {
      return;
    }
    setIsBusy(true);
    try {
      const payload = await request("/chat/sessions", {
        method: "POST",
        body: JSON.stringify({ title: "" }),
        timeoutMs: 15000,
      });
      const nextChatId = String(payload?.chat_id || "").trim();
      if (!nextChatId) {
        throw new Error("Empty chat id returned");
      }
      await fetchChatSessions(false);
      setShowTrash(false);
      setActiveChatId(nextChatId);
      setStatus("New chat created.");
    } catch (error) {
      setStatus(`Failed to create chat: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  function startInlineRename(chatId, title) {
    const target = String(chatId || "").trim();
    if (!target) {
      return;
    }
    setMenuChatId("");
    setEditingChatId(target);
    setEditingTitle(String(title || "").trim());
  }

  function cancelInlineRename() {
    setEditingChatId("");
    setEditingTitle("");
  }

  async function renameChatSession(chatId, rawTitle) {
    if (!isAuthenticated) {
      return;
    }
    const target = String(chatId || "").trim();
    if (!target) {
      return;
    }
    const cleanTitle = String(rawTitle || "").trim();
    if (!cleanTitle) {
      setStatus("Title cannot be empty.");
      return;
    }
    setIsBusy(true);
    try {
      await request(`/chat/sessions/${encodeURIComponent(target)}`, {
        method: "PATCH",
        body: JSON.stringify({ title: cleanTitle }),
        timeoutMs: 15000,
      });
      await fetchChatSessions(showTrash);
      cancelInlineRename();
      setStatus("Chat renamed.");
    } catch (error) {
      setStatus(`Failed to rename chat: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function deleteChatSession(chatId) {
    if (!isAuthenticated) {
      return;
    }
    const target = String(chatId || "").trim();
    if (!target) {
      return;
    }
    if (!globalThis.confirm("Move this chat to Trash?")) {
      return;
    }
    setMenuChatId("");
    setIsBusy(true);
    try {
      await request(`/chat/sessions/${encodeURIComponent(target)}`, {
        method: "DELETE",
        timeoutMs: 15000,
      });
      const sessions = await fetchChatSessions(showTrash);
      if (!showTrash) {
        setTrashCount((prev) => prev + 1);
      }
      if (activeChatIdRef.current === target) {
        const fallback = sessions[0]?.chat_id || DEFAULT_CHAT_ID;
        setActiveChatId(fallback);
      }
      setStatus("Chat moved to Trash.");
    } catch (error) {
      setStatus(`Failed to delete chat: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function restoreChatSession(chatId) {
    if (!isAuthenticated) {
      return;
    }
    const target = String(chatId || "").trim();
    if (!target) {
      return;
    }
    setMenuChatId("");
    setIsBusy(true);
    try {
      await request(`/chat/sessions/${encodeURIComponent(target)}/restore`, {
        method: "POST",
        timeoutMs: 15000,
      });
      const sessions = await fetchChatSessions(true);
      setChatSessions(sessions);
      setStatus("Chat restored.");
    } catch (error) {
      setStatus(`Failed to restore chat: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function purgeChatSession(chatId) {
    if (!isAuthenticated) {
      return;
    }
    const target = String(chatId || "").trim();
    if (!target) {
      return;
    }
    if (!globalThis.confirm("Purge chat permanently? This cannot be undone.")) {
      return;
    }

    setMenuChatId("");
    setIsBusy(true);
    try {
      await request(`/chat/sessions/${encodeURIComponent(target)}/purge`, {
        method: "DELETE",
        timeoutMs: 15000,
      });
      const sessions = await fetchChatSessions(true);
      if (activeChatIdRef.current === target) {
        const fallback = sessions[0]?.chat_id || DEFAULT_CHAT_ID;
        setActiveChatId(fallback);
      }
      setStatus("Chat purged permanently.");
    } catch (error) {
      setStatus(`Failed to purge chat: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function purgeAllTrashedChats() {
    if (!isAuthenticated) {
      return;
    }
    if (!globalThis.confirm("Purge all chats from Trash permanently? This cannot be undone.")) {
      return;
    }

    setMenuChatId("");
    setIsBusy(true);
    try {
        const payload = await request("/chat/sessions/purge-trash", {
        method: "DELETE",
        timeoutMs: 15000,
      });
      const purgedCount = Number(payload?.purged || 0);
      const sessions = await fetchChatSessions(true);
      if (showTrash) {
        const hasActive = sessions.some((item) => item.chat_id === activeChatIdRef.current);
        if (!hasActive) {
          setActiveChatId(DEFAULT_CHAT_ID);
        }
      }
      setStatus(`Purged ${purgedCount} chats from Trash.`);
    } catch (error) {
      setStatus(`Failed to purge trash: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function fetchMessages(chatId = activeChatIdRef.current) {
    if (!isAuthenticated) {
      setMessages([]);
      return;
    }
    try {
      const safeChatId = String(chatId || DEFAULT_CHAT_ID).trim() || DEFAULT_CHAT_ID;
      const payload = await request(`/chat/messages?chat_id=${encodeURIComponent(safeChatId)}`, { timeoutMs: 15000 });
      setMessages(normalizeMessages(payload));
    } catch (error) {
      setStatus(`Failed to load messages: ${error.message}`);
    }
  }

  async function sendMessage(rawText) {
    const text = String(rawText || "").trim();
    const selected = chatSessions.find((session) => session.chat_id === activeChatIdRef.current);
    const deletedSelected = Boolean(selected?.deleted_at);
    if (!text || !isAuthenticated || showTrash || deletedSelected) {
      return;
    }

    setIsBusy(true);
    const optimistic = {
      sender_type: "user",
      sender_user_id: null,
      content: text,
      created_at: new Date().toISOString(),
      temp_id: `tmp-${Date.now()}`,
    };
    setMessages((prev) => [...prev, optimistic]);
    setDraft("");

    try {
      const socket = wsRef.current;
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(
          JSON.stringify({
            action: "send",
            chat_id: activeChatIdRef.current,
            message: text,
          })
        );
      } else {
        const payload = await request("/chat/send", {
          method: "POST",
          body: JSON.stringify({ message: text, chat_id: activeChatIdRef.current }),
          timeoutMs: 45000,
        });
        setMessages(normalizeMessages(payload?.messages));
      }
      await fetchChatSessions();
      setStatus("Assistant responded.");
    } catch (error) {
      setStatus(`Message failed: ${error.message}`);
      await fetchMessages(activeChatIdRef.current);
    } finally {
      setIsBusy(false);
    }
  }

  async function onLoginSubmit(event) {
    event.preventDefault();
    setIsBusy(true);
    try {
      const payload = await request(
        "/auth/login",
        {
          method: "POST",
          body: JSON.stringify({ email: loginForm.email, password: loginForm.password }),
        },
        false
      );
      const nextToken = String(payload?.access_token || "").trim();
      if (!nextToken) {
        throw new Error("Token is empty");
      }
      writeStoredToken(nextToken);
      setToken(nextToken);
      setStatus("Signed in.");
    } catch (error) {
      setStatus(`Login failed: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function onRegisterSubmit(event) {
    event.preventDefault();
    setIsBusy(true);
    try {
      await request(
        "/auth/register",
        {
          method: "POST",
          body: JSON.stringify(registerForm),
        },
        false
      );
      setStatus("Registration completed. You can sign in now.");
      setMode("login");
      setLoginForm((prev) => ({ ...prev, email: registerForm.email }));
    } catch (error) {
      setStatus(`Registration failed: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  function logout() {
    performSessionLogout("Signed out.");
  }

  useEffect(() => {
    if (!isAuthenticated) {
      closeSocket();
      setWsStatus("offline");
      return undefined;
    }

    const bootstrap = async () => {
      const sessions = await fetchChatSessions(false);
      if (!sessions.length) {
        setActiveChatId(DEFAULT_CHAT_ID);
        activeChatIdRef.current = DEFAULT_CHAT_ID;
        await fetchMessages(DEFAULT_CHAT_ID);
        return;
      }

      const hasCurrent = sessions.some((session) => session.chat_id === activeChatIdRef.current);
      const nextActive = hasCurrent ? activeChatIdRef.current : sessions[0].chat_id;
      setActiveChatId(nextActive);
      activeChatIdRef.current = nextActive;
      await fetchMessages(nextActive);
    };
    void bootstrap();

    let disposed = false;
    function connect() {
      if (disposed) {
        return;
      }
      closeSocket();
      setWsStatus("connecting");

      let socket;
      try {
        socket = new WebSocket(buildChatWsUrl());
      } catch {
        setWsStatus("offline");
        wsRetryTimerRef.current = setTimeout(connect, 2000);
        return;
      }

      wsRef.current = socket;

      socket.onopen = () => {
        if (disposed) {
          return;
        }
        setWsStatus("online");
        socket.send(
          JSON.stringify({
            action: "subscribe",
            chat_id: activeChatIdRef.current,
          })
        );
      };

      socket.onmessage = (event) => {
        let payload;
        try {
          payload = JSON.parse(String(event.data || "{}"));
        } catch {
          return;
        }
        if (payload?.type === "chat.snapshot") {
          const snapshotChatId = String(payload?.chat_id || DEFAULT_CHAT_ID).trim() || DEFAULT_CHAT_ID;
          if (snapshotChatId !== activeChatIdRef.current) {
            return;
          }
          setMessages(normalizeMessages(payload.messages));
          void fetchChatSessions(showTrash);
        }
      };

      socket.onclose = (event) => {
        if (disposed) {
          return;
        }
        if (event?.code === 4401) {
          performSessionLogout("Session expired. Please sign in again.");
          return;
        }
        setWsStatus("offline");
        wsRetryTimerRef.current = setTimeout(connect, 2000);
      };

      socket.onerror = () => {
        if (disposed) {
          return;
        }
        setWsStatus("offline");
      };
    }

    connect();
    return () => {
      disposed = true;
      closeSocket();
    };
  }, [isAuthenticated, token, showTrash]);

  useEffect(() => {
    activeChatIdRef.current = String(activeChatId || DEFAULT_CHAT_ID).trim() || DEFAULT_CHAT_ID;
  }, [activeChatId]);

  useEffect(() => {
    if (!isAuthenticated) {
      return;
    }
    void fetchMessages(activeChatIdRef.current);

    const socket = wsRef.current;
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(
        JSON.stringify({
          action: "subscribe",
          chat_id: activeChatIdRef.current,
        })
      );
    }
  }, [activeChatId, isAuthenticated]);

  useEffect(() => {
    if (!messagesEndRef.current) {
      return;
    }
    messagesEndRef.current.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  function onComposerKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendMessage(draft);
    }
  }

  const activeSession = chatSessions.find((session) => session.chat_id === activeChatId) || null;
  const isActiveDeleted = Boolean(activeSession?.deleted_at);
  const isComposeDisabled = isBusy || !draft.trim() || showTrash || isActiveDeleted;
  const sessionGroups = groupSessionsByPeriod(chatSessions);

  if (!isAuthenticated) {
    return (
      <div className="page auth-page">
        <main className="auth-card card">
          <h1>SmartAi</h1>
          <p className="subtitle">User-only mode: personal assistant chat</p>

          <div className="workspace-tabs">
            <button type="button" className={mode === "login" ? "team-item team-item-active" : "team-item"} onClick={() => setMode("login")}>Login</button>
            <button type="button" className={mode === "register" ? "team-item team-item-active" : "team-item"} onClick={() => setMode("register")}>Register</button>
          </div>

          {mode === "login" ? (
            <form onSubmit={onLoginSubmit} className="context-grid">
              <label>Email<input value={loginForm.email} onChange={(e) => setLoginForm((p) => ({ ...p, email: e.target.value }))} /></label>
              <label>Password<input type="password" value={loginForm.password} onChange={(e) => setLoginForm((p) => ({ ...p, password: e.target.value }))} /></label>
              <button className="primary" type="submit" disabled={isBusy}>Sign In</button>
            </form>
          ) : (
            <form onSubmit={onRegisterSubmit} className="context-grid">
              <label>Email<input value={registerForm.email} onChange={(e) => setRegisterForm((p) => ({ ...p, email: e.target.value }))} /></label>
              <label>Password<input type="password" value={registerForm.password} onChange={(e) => setRegisterForm((p) => ({ ...p, password: e.target.value }))} /></label>
              <label>Full Name<input value={registerForm.full_name} onChange={(e) => setRegisterForm((p) => ({ ...p, full_name: e.target.value }))} /></label>
              <label>Title<input value={registerForm.title} onChange={(e) => setRegisterForm((p) => ({ ...p, title: e.target.value }))} /></label>
              <label>Bio<textarea rows={3} value={registerForm.profile_bio} onChange={(e) => setRegisterForm((p) => ({ ...p, profile_bio: e.target.value }))} /></label>
              <button className="primary" type="submit" disabled={isBusy}>Create Account</button>
            </form>
          )}

          <div className="status">{status}</div>
        </main>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="shell">
        <header className="chat-header card">
          <div>
            <h1>Personal Chat</h1>
            <p className="subtitle">Context scope: user_id only</p>
          </div>
          <div className="top-actions">
            <span className="pane-topbar-text">Realtime: {wsStatus}</span>
            <button className="secondary" type="button" onClick={() => void fetchMessages()}>Refresh</button>
            <button className="ghost" type="button" onClick={logout}>Logout</button>
          </div>
        </header>

        <section className="chat-layout">
          <aside className="chat-history card">
            <div className="chat-history-top">
              <div className="chat-mode-tabs">
                <button
                  type="button"
                  className={showTrash ? "ghost chat-action" : "secondary chat-action"}
                  onClick={() => {
                    setShowTrash(false);
                    setMenuChatId("");
                  }}
                >
                  Chats
                </button>
                <button
                  type="button"
                  className={showTrash ? "secondary chat-action" : "ghost chat-action"}
                  onClick={() => {
                    setShowTrash(true);
                    setMenuChatId("");
                  }}
                >
                  Trash {trashCount > 0 ? <span className="chat-tab-badge">{trashCount}</span> : null}
                </button>
              </div>
              {showTrash ? null : (
                <button className="secondary" type="button" disabled={isBusy} onClick={() => void createChatSession()}>
                  New
                </button>
              )}
              {showTrash ? (
                <button className="ghost chat-action chat-action-danger" type="button" disabled={isBusy || chatSessions.length === 0} onClick={() => void purgeAllTrashedChats()}>
                  Purge All
                </button>
              ) : null}
            </div>

            <div className="chat-history-list density-compact" aria-label="Chat sessions">
              {chatSessions.length === 0 ? <div className="empty">No chats yet.</div> : null}
              {sessionGroups.map((group) =>
                group.items.length > 0 ? (
                  <div key={group.title} className="chat-group">
                    <div className="chat-group-title">{group.title}</div>
                    {group.items.map((session) => {
                      const canDelete = true;
                      const isEditing = session.chat_id === editingChatId;
                      const isDeleted = Boolean(session.deleted_at);
                      return (
                        <div key={session.chat_id} className={session.chat_id === activeChatId ? "chat-session chat-session-active" : "chat-session"}>
                          <button
                            type="button"
                            className="chat-session-open"
                            disabled={isDeleted}
                            onClick={() => setActiveChatId(session.chat_id)}
                            onDoubleClick={() => startInlineRename(session.chat_id, session.title)}
                          >
                            {isEditing ? (
                              <input
                                className="chat-title-input"
                                value={editingTitle}
                                autoFocus
                                onClick={(event) => event.stopPropagation()}
                                onChange={(event) => setEditingTitle(event.target.value)}
                                onBlur={() => {
                                  if (!editingTitle.trim()) {
                                    cancelInlineRename();
                                    return;
                                  }
                                  void renameChatSession(session.chat_id, editingTitle);
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === "Escape") {
                                    cancelInlineRename();
                                    return;
                                  }
                                  if (event.key === "Enter") {
                                    event.preventDefault();
                                    void renameChatSession(session.chat_id, editingTitle);
                                  }
                                }}
                              />
                            ) : (
                              <strong className="chat-session-title">{session.title}</strong>
                            )}
                          </button>
                          <div className="chat-session-actions">
                            <button
                              className="ghost chat-action"
                              type="button"
                              disabled={isBusy}
                              onClick={() => setMenuChatId((prev) => (prev === session.chat_id ? "" : session.chat_id))}
                            >
                              ...
                            </button>
                            {menuChatId === session.chat_id ? (
                              <div className="chat-session-menu">
                                {isDeleted ? (
                                  <>
                                    <button className="ghost chat-action" type="button" disabled={isBusy} onClick={() => void restoreChatSession(session.chat_id)}>
                                      Restore
                                    </button>
                                    <button className="ghost chat-action chat-action-danger" type="button" disabled={isBusy} onClick={() => void purgeChatSession(session.chat_id)}>
                                      Purge
                                    </button>
                                  </>
                                ) : (
                                  <>
                                    <button className="ghost chat-action" type="button" disabled={isBusy} onClick={() => startInlineRename(session.chat_id, session.title)}>
                                      Rename
                                    </button>
                                    {canDelete ? (
                                      <button className="ghost chat-action chat-action-danger" type="button" disabled={isBusy} onClick={() => void deleteChatSession(session.chat_id)}>
                                        Move to Trash
                                      </button>
                                    ) : null}
                                  </>
                                )}
                              </div>
                            ) : null}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : null
              )}
            </div>
          </aside>

          <div className="chat-main">
            <section className="messages-card card">
              <div className="messages-list" role="log" aria-live="polite">
                {messages.length === 0 ? <div className="empty">No messages yet.</div> : null}
                {messages.map((item, index) => (
                  <div key={`${item.created_at}-${index}`} className={`message ${item.sender_type === "assistant" ? "assistant" : "user"}`}>
                    <div className="meta">
                      <span className="sender">{item.sender_type}</span>
                      <span>{new Date(item.created_at).toLocaleString()}</span>
                    </div>
                    {item.sender_type === "assistant" ? (
                      (() => {
                        const payload = parseAssistantFilePayload(item.content);
                        if (payload) {
                          return renderAttachmentMessage(payload);
                        }
                        return (
                          <div className="message-content markdown-content">
                            <ReactMarkdown
                              remarkPlugins={[remarkGfm]}
                              rehypePlugins={[rehypeHighlight]}
                              components={{
                                a: ExternalLink,
                              }}
                            >
                              {item.content}
                            </ReactMarkdown>
                          </div>
                        );
                      })()
                    ) : (
                      <p>{item.content}</p>
                    )}
                  </div>
                ))}
                {isBusy ? <div className="typing">Assistant is thinking...</div> : null}
                <div ref={messagesEndRef} />
              </div>
            </section>

            <section className="composer card">
              <textarea rows={4} value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onComposerKeyDown} placeholder="Write a task for your assistant" />
              {showTrash || isActiveDeleted ? <div className="compose-hint">Messaging is disabled in Trash view. Restore a chat or switch to active chats.</div> : null}
              <div className="composer-row">
                <div className="compose-hint">Enter - send, Shift+Enter - new line</div>
                <div className="composer-actions">
                  <button className="primary" type="button" disabled={isComposeDisabled} onClick={() => void sendMessage(draft)}>
                    {isBusy ? "Sending..." : "Send"}
                  </button>
                </div>
              </div>
            </section>
          </div>
        </section>

        <div className="status">{status}</div>
      </div>
    </div>
  );
}

export default App;
