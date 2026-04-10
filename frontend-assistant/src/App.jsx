import React, { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { I18N, SUPPORTED_LANGUAGES } from "./i18n";
import { readStoredLanguage, writeStoredLanguage } from "./languageStorage";

const DEFAULT_REGISTER_FORM = { email: "", password: "", full_name: "", title: "", profile_bio: "" };
const DEFAULT_LOGIN_FORM = { email: "", password: "" };
const DEFAULT_CHAT_ID = "default";
const DEFAULT_PROFILE_FORM = { email: "", full_name: "", title: "", profile_bio: "" };
const DEFAULT_PASSWORD_FORM = { current_password: "", new_password: "", confirm_password: "" };
const PERSONAL_CHAT_MARKER = "[personal]";
const DEFAULT_ADMIN_CREATE_USER_FORM = {
  email: "",
  password: "",
  full_name: "",
  title: "",
  profile_bio: "",
  role: "member",
};
const DEFAULT_ADMIN_CREATE_ORG_FORM = {
  org_id: "",
  name: "",
};

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
      title: (() => {
        const rawTitle = String(item?.title || "New Chat").trim() || "New Chat";
        if (rawTitle.toLowerCase().startsWith(`${PERSONAL_CHAT_MARKER} `)) {
          return rawTitle.slice(PERSONAL_CHAT_MARKER.length).trim() || "Personal Chat";
        }
        return rawTitle;
      })(),
      chat_kind: String(item?.title || "").trim().toLowerCase().startsWith(`${PERSONAL_CHAT_MARKER} `) ? "personal" : "group",
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
  const [language, setLanguage] = useState(readStoredLanguage);
  const [mode, setMode] = useState("login");
  const [registerForm, setRegisterForm] = useState(DEFAULT_REGISTER_FORM);
  const [loginForm, setLoginForm] = useState(DEFAULT_LOGIN_FORM);

  const [token, setToken] = useState(readStoredToken);
  const [status, setStatus] = useState("Sign in to open your personal assistant chat.");
  const [isBusy, setIsBusy] = useState(false);
  const [wsStatus, setWsStatus] = useState("offline");

  const [chatSessions, setChatSessions] = useState([]);
  const [isHistoryCollapsed, setIsHistoryCollapsed] = useState(false);
  const [activeChatId, setActiveChatId] = useState(DEFAULT_CHAT_ID);
  const [showTrash, setShowTrash] = useState(false);
  const [trashCount, setTrashCount] = useState(0);
  const [menuChatId, setMenuChatId] = useState("");
  const [editingChatId, setEditingChatId] = useState("");
  const [editingTitle, setEditingTitle] = useState("");
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileForm, setProfileForm] = useState(DEFAULT_PROFILE_FORM);
  const [passwordForm, setPasswordForm] = useState(DEFAULT_PASSWORD_FORM);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [skillsRole, setSkillsRole] = useState("member");
  const [hasGroupMemberships, setHasGroupMemberships] = useState(false);
  const [currentUserRole, setCurrentUserRole] = useState("member");
  const [userSkills, setUserSkills] = useState([]);
  const [adminSkillsOpen, setAdminSkillsOpen] = useState(false);
  const [adminUsersOpen, setAdminUsersOpen] = useState(false);
  const [adminOrganizationsOpen, setAdminOrganizationsOpen] = useState(false);
  const [adminUsers, setAdminUsers] = useState([]);
  const [adminOrganizations, setAdminOrganizations] = useState([]);
  const [adminAllSkills, setAdminAllSkills] = useState([]);
  const [adminTargetUserId, setAdminTargetUserId] = useState(0);
  const [adminAssignedSkills, setAdminAssignedSkills] = useState([]);
  const [adminUserQuery, setAdminUserQuery] = useState("");
  const [adminCreateUserForm, setAdminCreateUserForm] = useState(DEFAULT_ADMIN_CREATE_USER_FORM);
  const [adminCreateOrgForm, setAdminCreateOrgForm] = useState(DEFAULT_ADMIN_CREATE_ORG_FORM);
  const [adminOrgTargetUserId, setAdminOrgTargetUserId] = useState(0);
  const [adminOrgTargetOrgId, setAdminOrgTargetOrgId] = useState("");
  const [adminOrgTargetRole, setAdminOrgTargetRole] = useState("member");
  const [forcePasswordChange, setForcePasswordChange] = useState(false);
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");

  const wsRef = useRef(null);
  const wsRetryTimerRef = useRef(null);
  const messagesEndRef = useRef(null);
  const activeChatIdRef = useRef(DEFAULT_CHAT_ID);
  const forcePromptShownRef = useRef(false);

  const apiBase = useMemo(() => (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, ""), []);
  const isAuthenticated = Boolean(token);
  const isAdmin = currentUserRole === "admin";
  const t = (key) => I18N[language]?.[key] || I18N.en[key] || key;

  function onChangeLanguage(nextLanguage) {
    const normalized = String(nextLanguage || "").trim().toLowerCase();
    if (!SUPPORTED_LANGUAGES.includes(normalized)) {
      return;
    }
    setLanguage(normalized);
    writeStoredLanguage(normalized);
  }

  function performSessionLogout(reason = "Session expired. Please sign in again.") {
    closeSocket();
    writeStoredToken("");
    setToken("");
    setChatSessions([]);
    setTrashCount(0);
    setActiveChatId(DEFAULT_CHAT_ID);
    setIsHistoryCollapsed(false);
    setMessages([]);
    setDraft("");
    setPasswordForm(DEFAULT_PASSWORD_FORM);
    setSkillsOpen(false);
    setUserSkills([]);
    setSkillsRole("member");
    setHasGroupMemberships(false);
    setCurrentUserRole("member");
    setAdminSkillsOpen(false);
    setAdminUsersOpen(false);
    setAdminOrganizationsOpen(false);
    setAdminUsers([]);
    setAdminOrganizations([]);
    setAdminAllSkills([]);
    setAdminTargetUserId(0);
    setAdminAssignedSkills([]);
    setAdminUserQuery("");
    setAdminCreateUserForm(DEFAULT_ADMIN_CREATE_USER_FORM);
    setAdminCreateOrgForm(DEFAULT_ADMIN_CREATE_ORG_FORM);
    setAdminOrgTargetUserId(0);
    setAdminOrgTargetOrgId("");
    setAdminOrgTargetRole("member");
    setForcePasswordChange(false);
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
      let response;
      try {
        response = await fetch(`${apiBase}${path}`, {
          ...fetchOptions,
          headers,
          signal: controller.signal,
        });
      } catch (error) {
        const aborted = controller.signal.aborted || error?.name === "AbortError";
        if (aborted) {
          throw new Error(`Request timeout after ${timeoutMs}ms`);
        }
        throw error;
      }

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
        if (withAuth && response.status === 403 && details === "password_change_required") {
          setForcePasswordChange(true);
          setStatus("Password rotation required. Change your password to continue.");
        }
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

  async function createChatSession(kind = "group") {
    if (!isAuthenticated) {
      return;
    }
    const normalizedKind = String(kind || "group").toLowerCase() === "personal" ? "personal" : "group";
    const title = normalizedKind === "personal" ? `${PERSONAL_CHAT_MARKER} Personal Chat` : "";
    setIsBusy(true);
    try {
      const payload = await request("/chat/sessions", {
        method: "POST",
        body: JSON.stringify({ title }),
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

  async function openProfileEditor() {
    if (!isAuthenticated) {
      return;
    }
    setIsBusy(true);
    try {
      const profile = await request("/users/me/profile", { timeoutMs: 15000 });
      setProfileForm({
        email: String(profile?.email || ""),
        full_name: String(profile?.full_name || ""),
        title: String(profile?.title || ""),
        profile_bio: String(profile?.profile_bio || ""),
      });
      setProfileOpen(true);
    } catch (error) {
      setStatus(`Failed to load profile: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function openSkillsViewer() {
    if (!isAuthenticated) {
      return;
    }
    setIsBusy(true);
    try {
      const payload = await request("/users/me/skills", { timeoutMs: 15000 });
      const resolvedRole = String(payload?.role || "member");
      setSkillsRole(resolvedRole);
      setCurrentUserRole(resolvedRole);
      setHasGroupMemberships(Boolean(payload?.has_group_memberships));
      setUserSkills(Array.isArray(payload?.skills) ? payload.skills.map((item) => String(item || "")).filter(Boolean) : []);
      setSkillsOpen(true);
    } catch (error) {
      setStatus(`Failed to load skills: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function loadAdminUserSkills(targetUserId) {
    const selectedUserId = Number(targetUserId || 0);
    if (!selectedUserId) {
      setAdminAssignedSkills([]);
      return;
    }
    const payload = await request(`/admin/users/${selectedUserId}/skills`, { timeoutMs: 15000 });
    setAdminAssignedSkills(Array.isArray(payload?.skills) ? payload.skills.map((item) => String(item || "")).filter(Boolean) : []);
  }

  async function fetchAdminUsersList() {
    const usersPayload = await request("/admin/users/all", { timeoutMs: 15000 });
    return Array.isArray(usersPayload)
      ? usersPayload
          .map((item) => ({
            user_id: Number(item?.user_id || 0),
            email: String(item?.email || ""),
            full_name: String(item?.full_name || "").trim() || String(item?.email || ""),
            organization_ids: Array.isArray(item?.organization_ids)
              ? item.organization_ids.map((org) => String(org || "").trim()).filter(Boolean)
              : [],
          }))
          .filter((item) => item.user_id > 0)
      : [];
  }

  async function fetchOrganizationsList() {
    const orgsPayload = await request("/admin/organizations", { timeoutMs: 15000 });
    return Array.isArray(orgsPayload)
      ? orgsPayload
          .map((item) => ({
            org_id: String(item?.org_id || "").trim(),
            name: String(item?.name || "").trim() || String(item?.org_id || "").trim(),
          }))
          .filter((item) => item.org_id)
      : [];
  }

  async function openAdminSkillsManager() {
    if (!isAuthenticated || !isAdmin) {
      return;
    }
    setIsBusy(true);
    try {
      const users = await fetchAdminUsersList();
      const skillsPayload = await request("/admin/skills", { timeoutMs: 15000 });
      const allSkills = Array.isArray(skillsPayload?.skills)
        ? skillsPayload.skills.map((item) => ({
            tool_name: String(item?.tool_name || "").trim(),
            description: String(item?.description || "").trim(),
          })).filter((item) => item.tool_name)
        : [];

      setAdminUsers(users);
      setAdminAllSkills(allSkills);
      setAdminUserQuery("");
      const firstUserId = users[0]?.user_id || 0;
      setAdminTargetUserId(firstUserId);
      await loadAdminUserSkills(firstUserId);
      setAdminSkillsOpen(true);
    } catch (error) {
      setStatus(`Failed to load admin skills manager: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function openAdminUsersManager() {
    if (!isAuthenticated || !isAdmin) {
      return;
    }
    setIsBusy(true);
    try {
      const users = await fetchAdminUsersList();
      setAdminUsers(users);
      setAdminUsersOpen(true);
    } catch (error) {
      setStatus(`Failed to load admin users: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function createUserByAdmin() {
    if (!isAuthenticated || !isAdmin) {
      return;
    }
    const email = String(adminCreateUserForm.email || "").trim();
    const fullName = String(adminCreateUserForm.full_name || "").trim();
    if (!email || !fullName) {
      setStatus("Email and full name are required.");
      return;
    }
    const password = String(adminCreateUserForm.password || "");
    if (password && password.length < 8) {
      setStatus("Password must be at least 8 characters.");
      return;
    }

    setIsBusy(true);
    try {
      const payload = await request("/admin/users", {
        method: "POST",
        body: JSON.stringify({
          org_id: "user",
          email,
          password: password || null,
          full_name: fullName,
          title: String(adminCreateUserForm.title || ""),
          profile_bio: String(adminCreateUserForm.profile_bio || ""),
          role: String(adminCreateUserForm.role || "member"),
        }),
        timeoutMs: 15000,
      });
      setAdminCreateUserForm(DEFAULT_ADMIN_CREATE_USER_FORM);
      const users = await fetchAdminUsersList();
      setAdminUsers(users);
      const generatedPassword = String(payload?.one_time_password || payload?.password || "").trim();
      const passwordlessFirstLogin = Boolean(payload?.passwordless_first_login);
      if (passwordlessFirstLogin) {
        setStatus("User created. First login allowed with empty password; user must set a new password immediately.");
      } else {
        setStatus(generatedPassword ? `User created. Generated password: ${generatedPassword}` : "User created.");
      }
    } catch (error) {
      setStatus(`Failed to create user: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function deleteUserByAdmin(targetUserId) {
    if (!isAdmin) {
      return;
    }
    const normalizedUserId = Number(targetUserId || 0);
    if (!normalizedUserId) {
      return;
    }
    if (!globalThis.confirm(`Delete user #${normalizedUserId}? This action cannot be undone.`)) {
      return;
    }

    setIsBusy(true);
    try {
      await request(`/admin/users/${normalizedUserId}`, {
        method: "DELETE",
        timeoutMs: 15000,
      });
      const users = await fetchAdminUsersList();
      setAdminUsers(users);
      setStatus("User deleted.");
    } catch (error) {
      setStatus(`Failed to delete user: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function openAdminOrganizationsManager() {
    if (!isAuthenticated || !isAdmin) {
      return;
    }
    setIsBusy(true);
    try {
      const [users, organizations] = await Promise.all([fetchAdminUsersList(), fetchOrganizationsList()]);
      setAdminUsers(users);
      setAdminOrganizations(organizations);
      setAdminOrgTargetUserId(users[0]?.user_id || 0);
      setAdminOrgTargetOrgId(organizations[0]?.org_id || "");
      setAdminOrgTargetRole("member");
      setAdminOrganizationsOpen(true);
    } catch (error) {
      setStatus(`Failed to load organizations manager: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function createOrganizationByAdmin() {
    if (!isAdmin) {
      return;
    }
    const orgId = String(adminCreateOrgForm.org_id || "").trim();
    const name = String(adminCreateOrgForm.name || "").trim() || orgId;
    if (!orgId) {
      setStatus("Organization ID is required.");
      return;
    }

    setIsBusy(true);
    try {
      await request("/admin/organizations", {
        method: "POST",
        body: JSON.stringify({ org_id: orgId, name }),
        timeoutMs: 15000,
      });
      const organizations = await fetchOrganizationsList();
      setAdminOrganizations(organizations);
      setAdminCreateOrgForm(DEFAULT_ADMIN_CREATE_ORG_FORM);
      if (!adminOrgTargetOrgId && organizations.length > 0) {
        setAdminOrgTargetOrgId(organizations[0].org_id);
      }
      setStatus("Organization created.");
    } catch (error) {
      setStatus(`Failed to create organization: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function assignUserToOrganization() {
    if (!isAdmin) {
      return;
    }
    const targetUserId = Number(adminOrgTargetUserId || 0);
    const orgId = String(adminOrgTargetOrgId || "").trim();
    const role = String(adminOrgTargetRole || "member");
    if (!targetUserId || !orgId) {
      setStatus("Choose both user and organization.");
      return;
    }

    setIsBusy(true);
    try {
      await request(`/admin/users/${targetUserId}/organizations/add`, {
        method: "POST",
        body: JSON.stringify({ org_id: orgId, role }),
        timeoutMs: 15000,
      });
      const users = await fetchAdminUsersList();
      setAdminUsers(users);
      setStatus("User assigned to organization.");
    } catch (error) {
      setStatus(`Failed to assign user to organization: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function removeUserFromOrganization() {
    if (!isAdmin) {
      return;
    }
    const targetUserId = Number(adminOrgTargetUserId || 0);
    const orgId = String(adminOrgTargetOrgId || "").trim();
    if (!targetUserId || !orgId) {
      setStatus("Choose both user and organization.");
      return;
    }

    setIsBusy(true);
    try {
      await request(`/admin/users/${targetUserId}/organizations/${encodeURIComponent(orgId)}`, {
        method: "DELETE",
        timeoutMs: 15000,
      });
      const users = await fetchAdminUsersList();
      setAdminUsers(users);
      setStatus("User removed from organization.");
    } catch (error) {
      setStatus(`Failed to remove user from organization: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function toggleAdminSkill(skillName, shouldEnable) {
    if (!isAdmin) {
      return;
    }
    const selectedUserId = Number(adminTargetUserId || 0);
    const normalizedSkill = String(skillName || "").trim();
    if (!selectedUserId || !normalizedSkill) {
      return;
    }
    setIsBusy(true);
    try {
      await request(`/admin/users/${selectedUserId}/skills/${shouldEnable ? "grant" : "revoke"}`, {
        method: "POST",
        body: JSON.stringify({
          org_id: "user",
          tool_name: normalizedSkill,
        }),
        timeoutMs: 15000,
      });
      await loadAdminUserSkills(selectedUserId);
      setStatus(shouldEnable ? "Skill granted." : "Skill revoked.");
    } catch (error) {
      setStatus(`Failed to update skill: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function saveProfile() {
    if (!isAuthenticated) {
      return;
    }
    const fullName = String(profileForm.full_name || "").trim();
    if (!fullName) {
      setStatus("Full name is required.");
      return;
    }
    setIsBusy(true);
    try {
      const profile = await request("/users/me/profile", {
        method: "PATCH",
        body: JSON.stringify({
          full_name: fullName,
          title: String(profileForm.title || ""),
          profile_bio: String(profileForm.profile_bio || ""),
        }),
        timeoutMs: 15000,
      });
      setProfileForm({
        email: String(profile?.email || ""),
        full_name: String(profile?.full_name || ""),
        title: String(profile?.title || ""),
        profile_bio: String(profile?.profile_bio || ""),
      });
      setProfileOpen(false);
      setStatus("Profile updated.");
    } catch (error) {
      setStatus(`Failed to save profile: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function changePassword() {
    if (!isAuthenticated) {
      return;
    }
    const currentPassword = String(passwordForm.current_password || "");
    const newPassword = String(passwordForm.new_password || "");
    const confirmPassword = String(passwordForm.confirm_password || "");
    if (!currentPassword || !newPassword || !confirmPassword) {
      setStatus("Fill in all password fields.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setStatus("New password and confirmation do not match.");
      return;
    }
    setIsBusy(true);
    try {
      await request("/users/me/password/change", {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
        timeoutMs: 15000,
      });
      setPasswordForm(DEFAULT_PASSWORD_FORM);
      setForcePasswordChange(false);
      setStatus("Password changed successfully.");
    } catch (error) {
      setStatus(`Failed to change password: ${error.message}`);
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
    if (!text || !isAuthenticated || showTrash || deletedSelected || forcePasswordChange) {
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
      const mustChangePassword = Boolean(payload?.force_password_change);
      setForcePasswordChange(mustChangePassword);
      setStatus(mustChangePassword ? "Signed in. Password change is required." : "Signed in.");
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
    setProfileOpen(false);
    setProfileForm(DEFAULT_PROFILE_FORM);
    setPasswordForm(DEFAULT_PASSWORD_FORM);
    setAdminUsersOpen(false);
    setSkillsOpen(false);
    setUserSkills([]);
    setAdminSkillsOpen(false);
  }

  useEffect(() => {
    if (!isAuthenticated) {
      closeSocket();
      setWsStatus("offline");
      return undefined;
    }

    const refreshCurrentRole = async () => {
      try {
        const payload = await request("/users/me/skills", { timeoutMs: 15000 });
        const resolvedRole = String(payload?.role || "member");
        setCurrentUserRole(resolvedRole);
        setHasGroupMemberships(Boolean(payload?.has_group_memberships));
      } catch {
        setCurrentUserRole("member");
        setHasGroupMemberships(false);
      }
    };

    const bootstrap = async () => {
      await refreshCurrentRole();
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
        if (event?.code === 4403) {
          setWsStatus("offline");
          setForcePasswordChange(true);
          setStatus("Password rotation required. Change your password to continue.");
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
    if (isAdmin) {
      return;
    }
    setAdminUsersOpen(false);
    setAdminOrganizationsOpen(false);
    setAdminSkillsOpen(false);
  }, [isAdmin]);

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

  useEffect(() => {
    if (!isAuthenticated || !forcePasswordChange || profileOpen || forcePromptShownRef.current) {
      return;
    }
    forcePromptShownRef.current = true;
    void openProfileEditor();
  }, [forcePasswordChange, isAuthenticated, profileOpen]);

  useEffect(() => {
    if (!forcePasswordChange) {
      forcePromptShownRef.current = false;
    }
  }, [forcePasswordChange]);

  const filteredAdminUsers = useMemo(() => {
    const query = String(adminUserQuery || "").trim().toLowerCase();
    if (!query) {
      return adminUsers;
    }
    return adminUsers.filter((item) => {
      const fullName = String(item?.full_name || "").toLowerCase();
      const email = String(item?.email || "").toLowerCase();
      return fullName.includes(query) || email.includes(query);
    });
  }, [adminUsers, adminUserQuery]);

  useEffect(() => {
    if (!adminSkillsOpen) {
      return;
    }
    if (!filteredAdminUsers.length) {
      if (adminTargetUserId) {
        setAdminTargetUserId(0);
        setAdminAssignedSkills([]);
      }
      return;
    }
    const hasCurrent = filteredAdminUsers.some((item) => item.user_id === Number(adminTargetUserId || 0));
    if (!hasCurrent) {
      const nextUserId = Number(filteredAdminUsers[0]?.user_id || 0);
      setAdminTargetUserId(nextUserId);
      void loadAdminUserSkills(nextUserId);
    }
  }, [adminSkillsOpen, filteredAdminUsers, adminTargetUserId]);

  function onComposerKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendMessage(draft);
    }
  }

  const activeSession = chatSessions.find((session) => session.chat_id === activeChatId) || null;
  const activeChatKind = String(activeSession?.chat_kind || "").toLowerCase() === "personal" ? "personal" : "group";
  const resolvedChatTitle = activeChatKind === "personal" ? t("personalChat") : hasGroupMemberships ? t("groupChat") : t("personalChat");
  const resolvedContextSubtitle = activeChatKind === "personal" ? t("personalContextSubtitle") : t("contextSubtitle");
  const isActiveDeleted = Boolean(activeSession?.deleted_at);
  const isComposeDisabled = isBusy || !draft.trim() || showTrash || isActiveDeleted || forcePasswordChange;
  const sessionGroups = groupSessionsByPeriod(chatSessions);

  if (!isAuthenticated) {
    return (
      <div className="page auth-page">
        <main className="auth-card card">
          <h1>{t("appName")}</h1>
          <p className="subtitle">{t("userOnlyMode")}</p>

          <div className="top-actions" style={{ justifyContent: "flex-end" }}>
            <label className="pane-topbar-text" htmlFor="lang-switch-auth">{t("language")}:</label>
            <select id="lang-switch-auth" value={language} onChange={(event) => onChangeLanguage(event.target.value)}>
              <option value="kk">KK</option>
              <option value="ru">RU</option>
              <option value="en">EN</option>
            </select>
          </div>

          <div className="workspace-tabs">
            <button type="button" className={mode === "login" ? "team-item team-item-active" : "team-item"} onClick={() => setMode("login")}>{t("login")}</button>
            <button type="button" className={mode === "register" ? "team-item team-item-active" : "team-item"} onClick={() => setMode("register")}>{t("register")}</button>
          </div>

          {mode === "login" ? (
            <form onSubmit={onLoginSubmit} className="context-grid">
              <label>{t("email")}<input value={loginForm.email} onChange={(e) => setLoginForm((p) => ({ ...p, email: e.target.value }))} /></label>
              <label>{t("password")}<input type="password" value={loginForm.password} onChange={(e) => setLoginForm((p) => ({ ...p, password: e.target.value }))} /></label>
              <button className="primary" type="submit" disabled={isBusy}>{t("signIn")}</button>
            </form>
          ) : (
            <form onSubmit={onRegisterSubmit} className="context-grid">
              <label>{t("email")}<input value={registerForm.email} onChange={(e) => setRegisterForm((p) => ({ ...p, email: e.target.value }))} /></label>
              <label>{t("password")}<input type="password" value={registerForm.password} onChange={(e) => setRegisterForm((p) => ({ ...p, password: e.target.value }))} /></label>
              <label>{t("fullName")}<input value={registerForm.full_name} onChange={(e) => setRegisterForm((p) => ({ ...p, full_name: e.target.value }))} /></label>
              <label>{t("title")}<input value={registerForm.title} onChange={(e) => setRegisterForm((p) => ({ ...p, title: e.target.value }))} /></label>
              <label>{t("bio")}<textarea rows={3} value={registerForm.profile_bio} onChange={(e) => setRegisterForm((p) => ({ ...p, profile_bio: e.target.value }))} /></label>
              <button className="primary" type="submit" disabled={isBusy}>{t("createAccount")}</button>
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
            <h1>{resolvedChatTitle}</h1>
            <p className="subtitle">{resolvedContextSubtitle}</p>
            {forcePasswordChange ? <p className="subtitle">{t("passwordChangeRequired")}</p> : null}
          </div>
          <div className="top-actions">
            <span className="pane-topbar-text">{t("realtime")}: {wsStatus}</span>
            <span className="pane-topbar-text">{t("role")}: {isAdmin ? t("roleAdmin") : t("roleMember")}</span>
            <button className="secondary" type="button" disabled={isBusy} onClick={() => void openSkillsViewer()}>{t("skills")}</button>
            {isAdmin ? <button className="secondary" type="button" disabled={isBusy} onClick={() => void openAdminUsersManager()}>{t("adminUsers")}</button> : null}
            {isAdmin ? <button className="secondary" type="button" disabled={isBusy} onClick={() => void openAdminOrganizationsManager()}>{t("organizations")}</button> : null}
            {isAdmin ? <button className="secondary" type="button" disabled={isBusy} onClick={() => void openAdminSkillsManager()}>{t("adminSkills")}</button> : null}
            <button className="secondary" type="button" disabled={isBusy} onClick={() => void openProfileEditor()}>{t("profile")}</button>
            <button className="secondary" type="button" onClick={() => void fetchMessages()}>{t("refresh")}</button>
            <button className="ghost" type="button" onClick={logout}>{t("logout")}</button>
            <div className="lang-switch lang-switch-right">
              <label className="pane-topbar-text" htmlFor="lang-switch-chat">{t("language")}:</label>
              <select id="lang-switch-chat" value={language} onChange={(event) => onChangeLanguage(event.target.value)}>
                <option value="kk">KK</option>
                <option value="ru">RU</option>
                <option value="en">EN</option>
              </select>
            </div>
          </div>
        </header>

        <section className={isHistoryCollapsed ? "chat-layout chat-layout-collapsed" : "chat-layout"}>
          <aside className="chat-history card">
            <div className="chat-history-top">
              {showTrash ? null : (
                <div className="chat-create-row">
                  {hasGroupMemberships ? (
                    <button className="secondary chat-create-btn" type="button" disabled={isBusy} onClick={() => void createChatSession("group")}>
                      <span className="chat-create-icon" aria-hidden="true" />
                      <span>{t("groupShort")}</span>
                    </button>
                  ) : null}
                  <button className="secondary chat-create-btn" type="button" disabled={isBusy} onClick={() => void createChatSession("personal")}>
                    <span className="chat-create-icon" aria-hidden="true" />
                    <span>{t("personalShort")}</span>
                  </button>
                </div>
              )}
              <button
                className={isHistoryCollapsed ? "ghost history-toggle-btn is-collapsed" : "ghost history-toggle-btn"}
                type="button"
                onClick={() => setIsHistoryCollapsed((prev) => !prev)}
                aria-label={isHistoryCollapsed ? t("showHistory") : t("hideHistory")}
                title={isHistoryCollapsed ? t("showHistory") : t("hideHistory")}
              >
                <span />
                <span />
                <span />
              </button>
              <div className="chat-mode-tabs">
                <button
                  type="button"
                  className={showTrash ? "ghost chat-action" : "secondary chat-action"}
                  onClick={() => {
                    setShowTrash(false);
                    setMenuChatId("");
                  }}
                >
                  {t("chats")}
                </button>
                <button
                  type="button"
                  className={showTrash ? "secondary chat-action" : "ghost chat-action"}
                  onClick={() => {
                    setShowTrash(true);
                    setMenuChatId("");
                  }}
                >
                  {t("trash")} {trashCount > 0 ? <span className="chat-tab-badge">{trashCount}</span> : null}
                </button>
              </div>
              {showTrash ? (
                <button className="ghost chat-action chat-action-danger" type="button" disabled={isBusy || chatSessions.length === 0} onClick={() => void purgeAllTrashedChats()}>
                  {t("purgeAll")}
                </button>
              ) : null}
            </div>

            <div className="chat-history-list density-compact" aria-label="Chat sessions">
              {chatSessions.length === 0 ? <div className="empty">{t("noChats")}</div> : null}
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
                                      {t("restore")}
                                    </button>
                                    <button className="ghost chat-action chat-action-danger" type="button" disabled={isBusy} onClick={() => void purgeChatSession(session.chat_id)}>
                                      {t("purge")}
                                    </button>
                                  </>
                                ) : (
                                  <>
                                    <button className="ghost chat-action" type="button" disabled={isBusy} onClick={() => startInlineRename(session.chat_id, session.title)}>
                                      {t("rename")}
                                    </button>
                                    {canDelete ? (
                                      <button className="ghost chat-action chat-action-danger" type="button" disabled={isBusy} onClick={() => void deleteChatSession(session.chat_id)}>
                                        {t("moveToTrash")}
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
            {isHistoryCollapsed ? (
              <button
                className="ghost history-toggle-btn history-toggle-btn-floating is-collapsed"
                type="button"
                onClick={() => setIsHistoryCollapsed(false)}
                aria-label={t("showHistory")}
                title={t("showHistory")}
              >
                <span />
                <span />
                <span />
              </button>
            ) : null}
            <section className="messages-card card">
              <div className="messages-list" role="log" aria-live="polite">
                {messages.length === 0 ? <div className="empty">{t("noMessages")}</div> : null}
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
                <div ref={messagesEndRef} />
              </div>
              {isBusy ? (
                <div className="chat-busy-overlay" aria-live="polite" aria-label="Assistant is processing">
                  <div className="chat-busy-spinner" />
                  <div className="chat-busy-text">{t("assistantWorking")}</div>
                </div>
              ) : null}
            </section>

            <section className="composer card">
              <textarea rows={4} value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onComposerKeyDown} placeholder={t("writeTask")} />
              {showTrash || isActiveDeleted ? <div className="compose-hint">{t("messagingDisabledTrash")}</div> : null}
              {forcePasswordChange ? <div className="compose-hint">{t("changePasswordHint")}</div> : null}
              <div className="composer-row">
                <div className="compose-hint">{t("enterToSend")}</div>
                <div className="composer-actions">
                  <button className="primary" type="button" disabled={isComposeDisabled} onClick={() => void sendMessage(draft)}>
                    {isBusy ? t("sending") : t("send")}
                  </button>
                </div>
              </div>
            </section>
          </div>
        </section>

        <div className="status">{status}</div>
      </div>

      {skillsOpen ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setSkillsOpen(false)}>
          <div className="modal-card card" role="dialog" aria-modal="true" aria-label="Available skills" onClick={(event) => event.stopPropagation()}>
            <h2>{t("availableSkills")}</h2>
            <div className="skills-panel">
              <div className="skills-summary">
                <span>{t("yourRole")}: {skillsRole}</span>
                <span>{t("total")}: {userSkills.length}</span>
              </div>
              <div className="skills-grid">
                {userSkills.length === 0 ? <div className="empty">{t("noSkillsAssigned")}</div> : null}
                {userSkills.map((skillName) => (
                  <div key={skillName} className="skill-item">
                    <span>{skillName}</span>
                  </div>
                ))}
              </div>
            </div>
            <div className="top-actions">
              <button className="ghost" type="button" onClick={() => setSkillsOpen(false)}>{t("close")}</button>
              <button className="secondary" type="button" disabled={isBusy} onClick={() => void openSkillsViewer()}>{t("refresh")}</button>
            </div>
          </div>
        </div>
      ) : null}

      {isAdmin && adminUsersOpen ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setAdminUsersOpen(false)}>
          <div className="modal-card card" role="dialog" aria-modal="true" aria-label="Admin users manager" onClick={(event) => event.stopPropagation()}>
            <h2>{t("adminUsersTitle")}</h2>
            <div className="context-grid profile-grid">
              <label>{t("email")}<input value={adminCreateUserForm.email} onChange={(event) => setAdminCreateUserForm((prev) => ({ ...prev, email: event.target.value }))} /></label>
              <label>{t("fullName")}<input value={adminCreateUserForm.full_name} onChange={(event) => setAdminCreateUserForm((prev) => ({ ...prev, full_name: event.target.value }))} /></label>
              <label>{t("passwordOptional")}<input type="password" value={adminCreateUserForm.password} onChange={(event) => setAdminCreateUserForm((prev) => ({ ...prev, password: event.target.value }))} /></label>
              <label>{t("role")}
                <select value={adminCreateUserForm.role} onChange={(event) => setAdminCreateUserForm((prev) => ({ ...prev, role: event.target.value }))}>
                  <option value="member">member</option>
                  <option value="manager">manager</option>
                  <option value="admin">admin</option>
                </select>
              </label>
              <label>{t("title")}<input value={adminCreateUserForm.title} onChange={(event) => setAdminCreateUserForm((prev) => ({ ...prev, title: event.target.value }))} /></label>
              <label className="profile-bio-field">{t("bio")}<textarea rows={3} value={adminCreateUserForm.profile_bio} onChange={(event) => setAdminCreateUserForm((prev) => ({ ...prev, profile_bio: event.target.value }))} /></label>
            </div>
            <div className="top-actions">
              <button className="secondary" type="button" disabled={isBusy} onClick={() => void createUserByAdmin()}>{t("createUser")}</button>
              <button className="secondary" type="button" disabled={isBusy} onClick={() => void openAdminUsersManager()}>{t("refresh")}</button>
            </div>
            <div className="skills-panel">
              <div className="skills-summary">
                <span>{t("usersCount")}: {adminUsers.length}</span>
              </div>
              <div className="list-table">
                {adminUsers.length === 0 ? <div className="empty">{t("noUsersFound")}</div> : null}
                {adminUsers.length > 0 ? (
                  <div className="list-row list-header">
                    <span>{t("user")}</span>
                    <span>{t("organizationsCol")}</span>
                    <span>{t("actions")}</span>
                  </div>
                ) : null}
                {adminUsers.map((item) => (
                  <div key={item.user_id} className="list-row">
                    <span>{item.full_name} ({item.email})</span>
                    <span className="admin-org-list">{item.organization_ids?.length ? item.organization_ids.join(", ") : t("none")}</span>
                    <span>
                      <button
                        className="ghost"
                        type="button"
                        disabled={isBusy}
                        onClick={() => void deleteUserByAdmin(item.user_id)}
                      >
                        {t("delete")}
                      </button>
                    </span>
                  </div>
                ))}
              </div>
            </div>
            <div className="top-actions">
              <button className="ghost" type="button" onClick={() => setAdminUsersOpen(false)}>{t("close")}</button>
            </div>
          </div>
        </div>
      ) : null}

      {isAdmin && adminOrganizationsOpen ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setAdminOrganizationsOpen(false)}>
          <div className="modal-card card" role="dialog" aria-modal="true" aria-label="Admin organizations manager" onClick={(event) => event.stopPropagation()}>
            <h2>{t("organizationsTitle")}</h2>
            <div className="context-grid profile-grid">
              <label>{t("organizationId")}<input value={adminCreateOrgForm.org_id} onChange={(event) => setAdminCreateOrgForm((prev) => ({ ...prev, org_id: event.target.value }))} /></label>
              <label>{t("name")}<input value={adminCreateOrgForm.name} onChange={(event) => setAdminCreateOrgForm((prev) => ({ ...prev, name: event.target.value }))} /></label>
            </div>
            <div className="top-actions">
              <button className="secondary" type="button" disabled={isBusy} onClick={() => void createOrganizationByAdmin()}>{t("createOrganization")}</button>
              <button className="secondary" type="button" disabled={isBusy} onClick={() => void openAdminOrganizationsManager()}>{t("refresh")}</button>
            </div>

            <div className="context-grid profile-grid">
              <label>
                {t("user")}
                <select value={adminOrgTargetUserId || 0} onChange={(event) => setAdminOrgTargetUserId(Number(event.target.value || 0))}>
                  {adminUsers.length === 0 ? <option value={0}>{t("noUsers")}</option> : null}
                  {adminUsers.map((item) => (
                    <option key={item.user_id} value={item.user_id}>{item.full_name} ({item.email})</option>
                  ))}
                </select>
              </label>
              <label>
                {t("organizationsTitle")}
                <select value={adminOrgTargetOrgId} onChange={(event) => setAdminOrgTargetOrgId(event.target.value)}>
                  {adminOrganizations.length === 0 ? <option value="">{t("noOrganizations")}</option> : null}
                  {adminOrganizations.map((item) => (
                    <option key={item.org_id} value={item.org_id}>{item.name} ({item.org_id})</option>
                  ))}
                </select>
              </label>
              <label>
                {t("role")}
                <select value={adminOrgTargetRole} onChange={(event) => setAdminOrgTargetRole(event.target.value)}>
                  <option value="member">member</option>
                  <option value="manager">manager</option>
                  <option value="admin">admin</option>
                </select>
              </label>
            </div>
            <div className="top-actions">
              <button className="secondary" type="button" disabled={isBusy || !adminOrgTargetUserId || !adminOrgTargetOrgId} onClick={() => void assignUserToOrganization()}>{t("assignUser")}</button>
              <button className="ghost" type="button" disabled={isBusy || !adminOrgTargetUserId || !adminOrgTargetOrgId} onClick={() => void removeUserFromOrganization()}>{t("removeFromOrganization")}</button>
            </div>

            <div className="skills-panel">
              <div className="skills-summary">
                <span>{t("organizationsCount")}: {adminOrganizations.length}</span>
              </div>
              <div className="list-table">
                {adminOrganizations.length === 0 ? <div className="empty">{t("noOrganizationsFound")}</div> : null}
                {adminOrganizations.length > 0 ? (
                  <div className="list-row list-header">
                    <span>{t("organizationsTitle")}</span>
                    <span>ID</span>
                    <span>{t("members")}</span>
                  </div>
                ) : null}
                {adminOrganizations.map((item) => {
                  const memberCount = adminUsers.filter((userItem) => Array.isArray(userItem.organization_ids) && userItem.organization_ids.includes(item.org_id)).length;
                  return (
                    <div key={item.org_id} className="list-row">
                      <span>{item.name}</span>
                      <span>{item.org_id}</span>
                      <span>{memberCount}</span>
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="top-actions">
              <button className="ghost" type="button" onClick={() => setAdminOrganizationsOpen(false)}>{t("close")}</button>
            </div>
          </div>
        </div>
      ) : null}

      {isAdmin && adminSkillsOpen ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setAdminSkillsOpen(false)}>
          <div className="modal-card card" role="dialog" aria-modal="true" aria-label="Admin skills manager" onClick={(event) => event.stopPropagation()}>
            <h2>{t("adminSkillsManager")}</h2>
            <div className="context-grid profile-grid">
              <label>
                {t("searchUser")}
                <input
                  value={adminUserQuery}
                  placeholder={t("nameOrEmail")}
                  onChange={(event) => setAdminUserQuery(event.target.value)}
                />
              </label>
              <label>
                {t("user")}
                <select
                  value={adminTargetUserId || 0}
                  onChange={(event) => {
                    const nextUserId = Number(event.target.value || 0);
                    setAdminTargetUserId(nextUserId);
                    void loadAdminUserSkills(nextUserId);
                  }}
                >
                  {filteredAdminUsers.length === 0 ? <option value={0}>{t("noUsersFound")}</option> : null}
                  {filteredAdminUsers.map((item) => (
                    <option key={item.user_id} value={item.user_id}>
                      {item.full_name} ({item.email})
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="skills-panel">
              <div className="skills-summary">
                <span>{t("available")}: {adminAllSkills.length}</span>
                <span>{t("assigned")}: {adminAssignedSkills.length}</span>
              </div>
              <div className="skills-grid">
                {adminAllSkills.length === 0 ? <div className="empty">{t("noDynamicSkillsFound")}</div> : null}
                {adminAllSkills.map((item) => {
                  const isChecked = adminAssignedSkills.includes(item.tool_name);
                  return (
                    <label key={item.tool_name} className="skill-item">
                      <span title={item.description || item.tool_name}>{item.tool_name}</span>
                      <input
                        type="checkbox"
                        checked={isChecked}
                        disabled={isBusy || !adminTargetUserId}
                        onChange={(event) => void toggleAdminSkill(item.tool_name, event.target.checked)}
                      />
                    </label>
                  );
                })}
              </div>
            </div>
            <div className="top-actions">
              <button className="ghost" type="button" onClick={() => setAdminSkillsOpen(false)}>{t("close")}</button>
              <button className="secondary" type="button" disabled={isBusy} onClick={() => void openAdminSkillsManager()}>{t("refresh")}</button>
            </div>
          </div>
        </div>
      ) : null}

      {profileOpen ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setProfileOpen(false)}>
          <div className="modal-card card" role="dialog" aria-modal="true" aria-label="Edit profile" onClick={(event) => event.stopPropagation()}>
            <h2>{t("editProfile")}</h2>
            <div className="context-grid profile-grid">
              <label>{t("email")}<input value={profileForm.email} disabled /></label>
              <label>{t("fullName")}<input value={profileForm.full_name} onChange={(e) => setProfileForm((p) => ({ ...p, full_name: e.target.value }))} /></label>
              <label>{t("title")}<input value={profileForm.title} onChange={(e) => setProfileForm((p) => ({ ...p, title: e.target.value }))} /></label>
              <label className="profile-bio-field">{t("bio")}<textarea rows={4} value={profileForm.profile_bio} onChange={(e) => setProfileForm((p) => ({ ...p, profile_bio: e.target.value }))} /></label>
            </div>
            <div className="context-grid profile-grid">
              <label>{t("currentPassword")}<input type="password" value={passwordForm.current_password} onChange={(e) => setPasswordForm((p) => ({ ...p, current_password: e.target.value }))} /></label>
              <label>{t("newPassword")}<input type="password" value={passwordForm.new_password} onChange={(e) => setPasswordForm((p) => ({ ...p, new_password: e.target.value }))} /></label>
              <label>{t("confirmNewPassword")}<input type="password" value={passwordForm.confirm_password} onChange={(e) => setPasswordForm((p) => ({ ...p, confirm_password: e.target.value }))} /></label>
            </div>
            <div className="top-actions">
              <button className="ghost" type="button" onClick={() => setProfileOpen(false)}>{t("cancel")}</button>
              <button className="secondary" type="button" disabled={isBusy} onClick={() => void changePassword()}>{t("changePassword")}</button>
              <button className="primary" type="button" disabled={isBusy} onClick={() => void saveProfile()}>{t("save")}</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default App;
