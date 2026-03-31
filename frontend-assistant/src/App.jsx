import React, { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

const DEFAULT_REGISTER_FORM = { email: "", password: "", full_name: "", title: "", profile_bio: "" };
const DEFAULT_LOGIN_FORM = { email: "", password: "" };

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

function App() {
  const [mode, setMode] = useState("login");
  const [registerForm, setRegisterForm] = useState(DEFAULT_REGISTER_FORM);
  const [loginForm, setLoginForm] = useState(DEFAULT_LOGIN_FORM);

  const [token, setToken] = useState(readStoredToken);
  const [status, setStatus] = useState("Sign in to open your personal assistant chat.");
  const [isBusy, setIsBusy] = useState(false);
  const [wsStatus, setWsStatus] = useState("offline");

  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");

  const wsRef = useRef(null);
  const wsRetryTimerRef = useRef(null);
  const messagesEndRef = useRef(null);

  const apiBase = useMemo(() => (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, ""), []);
  const isAuthenticated = Boolean(token);

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
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    return `${protocol}//${host}/api/v1/chat/ws?token=${encodeURIComponent(token)}`;
  }

  async function fetchMessages() {
    if (!isAuthenticated) {
      setMessages([]);
      return;
    }
    try {
      const payload = await request("/chat/messages", { timeoutMs: 15000 });
      setMessages(normalizeMessages(payload));
    } catch (error) {
      setStatus(`Failed to load messages: ${error.message}`);
    }
  }

  async function sendMessage(rawText) {
    const text = String(rawText || "").trim();
    if (!text || !isAuthenticated) {
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
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ action: "send", message: text }));
      } else {
        const payload = await request("/chat/send", {
          method: "POST",
          body: JSON.stringify({ message: text }),
          timeoutMs: 45000,
        });
        setMessages(normalizeMessages(payload?.messages));
      }
      setStatus("Assistant responded.");
    } catch (error) {
      setStatus(`Message failed: ${error.message}`);
      await fetchMessages();
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
    closeSocket();
    writeStoredToken("");
    setToken("");
    setMessages([]);
    setDraft("");
    setWsStatus("offline");
    setStatus("Signed out.");
  }

  useEffect(() => {
    if (!isAuthenticated) {
      closeSocket();
      setWsStatus("offline");
      return undefined;
    }

    fetchMessages();

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
        socket.send(JSON.stringify({ action: "subscribe" }));
      };

      socket.onmessage = (event) => {
        let payload;
        try {
          payload = JSON.parse(String(event.data || "{}"));
        } catch {
          return;
        }
        if (payload?.type === "chat.snapshot") {
          setMessages(normalizeMessages(payload.messages));
        }
      };

      socket.onclose = () => {
        if (disposed) {
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
  }, [isAuthenticated, token]);

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
                  <div className="message-content markdown-content">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      rehypePlugins={[rehypeHighlight]}
                      components={{
                        a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener" />,
                      }}
                    >
                      {item.content}
                    </ReactMarkdown>
                  </div>
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
          <div className="composer-row">
            <div className="compose-hint">Enter - send, Shift+Enter - new line</div>
            <div className="composer-actions">
              <button className="primary" type="button" disabled={isBusy || !draft.trim()} onClick={() => void sendMessage(draft)}>
                {isBusy ? "Sending..." : "Send"}
              </button>
            </div>
          </div>
        </section>

        <div className="status">{status}</div>
      </div>
    </div>
  );
}

export default App;
