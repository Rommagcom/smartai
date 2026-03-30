import React, { useEffect, useMemo, useRef, useState } from "react";

const DEFAULT_REGISTER_FORM = { email: "", password: "", full_name: "", title: "", profile_bio: "" };
const DEFAULT_LOGIN_FORM = { email: "", password: "" };
const DEFAULT_RAG_QUERY_FORM = {
  query: "",
  scope: "team",
};
const DEFAULT_CREATE_ORG_FORM = { org_id: "", name: "" };
const DEFAULT_CREATE_TEAM_FORM = { team_id: "", name: "" };
const DEFAULT_CREATE_USER_FORM = { email: "", full_name: "", title: "", profile_bio: "", role: "member", password: "" };
const DEFAULT_EDIT_USER_FORM = {
  user_id: "",
  email: "",
  full_name: "",
  title: "",
  profile_bio: "",
  role: "member",
  force_password_change: false,
  organization_ids: [],
};
const ADMIN_SECTIONS = [
  { key: "organizations", label: "Organizations" },
  { key: "teams", label: "Teams & Members" },
  { key: "skills", label: "Skills" },
  { key: "users", label: "Users" },
];

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

function teamKey(team) {
  return `${team.org_id}:${team.team_id}`;
}

function parseApiError(payload) {
  if (!payload) {
    return "";
  }

  if (typeof payload.detail === "string" && payload.detail.trim()) {
    return payload.detail.trim();
  }

  if (Array.isArray(payload.detail) && payload.detail.length > 0) {
    return payload.detail
      .map((item) => {
        if (!item) {
          return "";
        }
        if (typeof item === "string") {
          return item;
        }
        const field = Array.isArray(item.loc) ? item.loc.join(".") : "field";
        const message = typeof item.msg === "string" ? item.msg : JSON.stringify(item);
        return `${field}: ${message}`;
      })
      .filter(Boolean)
      .join("; ");
  }

  if (typeof payload.message === "string" && payload.message.trim()) {
    return payload.message.trim();
  }

  return "";
}

function isAllowedImageSrc(src) {
  const value = String(src || "").trim();
  if (!value) {
    return false;
  }
  if (value.startsWith("data:image/")) {
    return true;
  }
  if (value.startsWith("https://") || value.startsWith("http://") || value.startsWith("blob:")) {
    return true;
  }
  return false;
}

function parseJsonObjectCandidates(content) {
  const text = String(content || "").trim();
  if (!text) {
    return [];
  }

  const candidates = [text];

  if (text.includes("```")) {
    const parts = text.split("```");
    for (const part of parts) {
      let candidate = String(part || "").trim();
      if (!candidate) {
        continue;
      }
      if (candidate.startsWith("json")) {
        candidate = candidate.slice(4).trim();
      }
      if (candidate.startsWith("{") && candidate.endsWith("}")) {
        candidates.push(candidate);
      }
    }
  }

  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start !== -1 && end !== -1 && end > start) {
    candidates.push(text.slice(start, end + 1));
  }

  const parsedObjects = [];
  const seen = new Set();
  for (const candidate of candidates) {
    if (!candidate || seen.has(candidate)) {
      continue;
    }
    seen.add(candidate);
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        parsedObjects.push(parsed);
        continue;
      }
      if (typeof parsed === "string") {
        try {
          const nested = JSON.parse(parsed);
          if (nested && typeof nested === "object" && !Array.isArray(nested)) {
            parsedObjects.push(nested);
          }
        } catch {
          // ignore non-JSON nested string
        }
      }
    } catch {
      // ignore non-JSON candidate
    }
  }

  return parsedObjects;
}

function parseImagePayloadFromJson(content) {
  const objects = parseJsonObjectCandidates(content);
  if (objects.length === 0) {
    return null;
  }

  for (const parsed of objects) {
    const payload = parsed && typeof parsed.file_payload === "object" ? parsed.file_payload : parsed;
    if (!payload || typeof payload !== "object") {
      continue;
    }

    const payloadType = String(payload.type || "").trim().toLowerCase();
    const mimeType = String(payload.mime_type || "").trim();
    const isImagePayload = payloadType === "image" || (payloadType === "file" && mimeType.startsWith("image/"));
    if (!isImagePayload) {
      continue;
    }

    const base64Omitted = Boolean(payload.base64_omitted);
    const rawBase64 = typeof payload.base64 === "string" ? payload.base64.trim() : "";
    if (!rawBase64 || rawBase64 === "<omitted>" || base64Omitted) {
      continue;
    }

    const resolvedMimeType = mimeType || "image/png";
    const src = `data:${resolvedMimeType};base64,${rawBase64}`;
    if (!isAllowedImageSrc(src)) {
      continue;
    }

    return {
      src,
      alt: String(payload.filename || "generated image").trim() || "generated image",
    };
  }

  return null;
}

function renderMessageContent(content) {
  const text = String(content || "");
  const jsonImage = parseImagePayloadFromJson(text);
  if (jsonImage) {
    return (
      <div className="message-content">
        <img src={jsonImage.src} alt={jsonImage.alt} loading="lazy" />
      </div>
    );
  }

  const inlineDataUrlPattern = /data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+/;
  const inlineDataUrlMatch = inlineDataUrlPattern.exec(text);
  if (inlineDataUrlMatch) {
    const src = String(inlineDataUrlMatch[0] || "").replaceAll(/\s+/g, "").trim();
    if (isAllowedImageSrc(src)) {
      return (
        <div className="message-content">
          <img src={src} alt="generated" loading="lazy" />
        </div>
      );
    }
  }

  const imagePattern = /!\[([^\]]*)\]\(([^)]+)\)/g;
  const blocks = [];
  let lastIndex = 0;
  let match;

  while ((match = imagePattern.exec(text)) !== null) {
    const fullMatch = match[0];
    const alt = String(match[1] || "image").trim() || "image";
    const src = String(match[2] || "").trim();
    const start = match.index;

    if (start > lastIndex) {
      blocks.push({ type: "text", key: `txt-${lastIndex}-${start}`, value: text.slice(lastIndex, start) });
    }

    if (isAllowedImageSrc(src)) {
      blocks.push({ type: "image", key: `img-${start}`, src, alt });
    } else {
      blocks.push({ type: "text", key: `txt-${start}`, value: fullMatch });
    }

    lastIndex = start + fullMatch.length;
  }

  if (lastIndex < text.length) {
    blocks.push({ type: "text", key: `txt-${lastIndex}-end`, value: text.slice(lastIndex) });
  }

  if (blocks.length === 0) {
    return <p>{text}</p>;
  }

  return (
    <div className="message-content">
      {blocks.map((block, index) => {
        if (block.type === "image") {
          return <img key={block.key} src={block.src} alt={block.alt} loading="lazy" />;
        }
        return <p key={block.key}>{block.value}</p>;
      })}
    </div>
  );
}

function App() {
  const [mode, setMode] = useState("login");
  const [registerForm, setRegisterForm] = useState(DEFAULT_REGISTER_FORM);
  const [loginForm, setLoginForm] = useState(DEFAULT_LOGIN_FORM);

  const [token, setToken] = useState(readStoredToken);
  const [status, setStatus] = useState("Connect to your assistant workspace and start a team conversation.");
  const [isBusy, setIsBusy] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [wsStatus, setWsStatus] = useState("offline");

  const [isAdmin, setIsAdmin] = useState(false);
  const [isCheckingAdmin, setIsCheckingAdmin] = useState(false);
  const [activeScreen, setActiveScreen] = useState("chat");

  const [teams, setTeams] = useState([]);
  const [activeTeam, setActiveTeam] = useState(null);

  const [messages, setMessages] = useState([]);
  const [messageDraft, setMessageDraft] = useState("");
  const [pendingMessage, setPendingMessage] = useState(null);
  const [ragUploadScope, setRagUploadScope] = useState("team");
  const [ragUploadFile, setRagUploadFile] = useState(null);
  const [ragQueryForm, setRagQueryForm] = useState(DEFAULT_RAG_QUERY_FORM);
  const [ragBusy, setRagBusy] = useState(false);
  const [ragResult, setRagResult] = useState(null);
  const [ragJobs, setRagJobs] = useState([]);
  const [ragJobsBusy, setRagJobsBusy] = useState(false);

  const [adminSection, setAdminSection] = useState("organizations");
  const [organizations, setOrganizations] = useState([]);
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [orgTeams, setOrgTeams] = useState([]);
  const [selectedTeamId, setSelectedTeamId] = useState("");
  const [teamMembers, setTeamMembers] = useState([]);
  const [orgUsers, setOrgUsers] = useState([]);
  const [selectedUserIds, setSelectedUserIds] = useState([]);
  const [teamMembersSearch, setTeamMembersSearch] = useState("");
  const [orgUsersSearch, setOrgUsersSearch] = useState("");
  const [skillsSearch, setSkillsSearch] = useState("");
  const [usersSearch, setUsersSearch] = useState("");

  const [createOrgForm, setCreateOrgForm] = useState(DEFAULT_CREATE_ORG_FORM);
  const [createTeamForm, setCreateTeamForm] = useState(DEFAULT_CREATE_TEAM_FORM);
  const [createUserForm, setCreateUserForm] = useState(DEFAULT_CREATE_USER_FORM);
  const [editUserForm, setEditUserForm] = useState(DEFAULT_EDIT_USER_FORM);
  const [transferSourceOrgId, setTransferSourceOrgId] = useState("");
  const [transferTargetOrgId, setTransferTargetOrgId] = useState("");
  const [transferRole, setTransferRole] = useState("member");
  const [keepSourceMembership, setKeepSourceMembership] = useState(false);
  const [bulkTransferUserIds, setBulkTransferUserIds] = useState([]);
  const [bulkTransferSourceOrgId, setBulkTransferSourceOrgId] = useState("");
  const [bulkTransferTargetOrgId, setBulkTransferTargetOrgId] = useState("");
  const [bulkTransferRole, setBulkTransferRole] = useState("member");
  const [bulkKeepSourceMembership, setBulkKeepSourceMembership] = useState(false);
  const [bulkFilterBySourceOrg, setBulkFilterBySourceOrg] = useState(true);

  const [userDirectory, setUserDirectory] = useState([]);
  const [globalUsers, setGlobalUsers] = useState([]);
  const [generatedCredential, setGeneratedCredential] = useState("");

  const [availableSkills, setAvailableSkills] = useState([]);
  const [teamAssignedSkills, setTeamAssignedSkills] = useState([]);
  const [teamSelectedSkills, setTeamSelectedSkills] = useState([]);
  const [adminOutput, setAdminOutput] = useState("No admin actions yet.");

  const messagesEndRef = useRef(null);
  const wsRef = useRef(null);
  const wsRetryTimerRef = useRef(null);
  const pendingSendTimeoutRef = useRef(null);
  const pendingMessageRef = useRef(null);
  const ragFileInputRef = useRef(null);

  const apiBase = useMemo(() => (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, ""), []);
  const isAuthenticated = Boolean(token);
  const hasOrgId = Boolean(String(selectedOrgId || "").trim());
  const hasTeamId = Boolean(String(selectedTeamId || "").trim());

  const filteredTeamMembers = useMemo(() => {
    const query = teamMembersSearch.trim().toLowerCase();
    if (!query) {
      return teamMembers;
    }
    return teamMembers.filter((member) => {
      const blob = `${member.full_name || ""} ${member.email || ""} ${member.title || ""} ${member.role || ""} ${member.user_id || ""}`.toLowerCase();
      return blob.includes(query);
    });
  }, [teamMembers, teamMembersSearch]);

  const selectedTeamMemberIds = useMemo(() => new Set(teamMembers.map((member) => Number(member.user_id))), [teamMembers]);

  const filteredOrgUsers = useMemo(() => {
    const query = orgUsersSearch.trim().toLowerCase();
    if (!query) {
      return orgUsers;
    }
    return orgUsers.filter((user) => {
      const blob = `${user.full_name || ""} ${user.email || ""} ${user.title || ""} ${user.role || ""} ${user.user_id || ""}`.toLowerCase();
      return blob.includes(query);
    });
  }, [orgUsers, orgUsersSearch]);

  const filteredUserDirectory = useMemo(() => {
    const query = usersSearch.trim().toLowerCase();
    if (!query) {
      return userDirectory;
    }
    return userDirectory.filter((user) => {
      const blob = `${user.full_name || ""} ${user.email || ""} ${user.title || ""} ${user.role || ""} ${user.user_id || ""}`.toLowerCase();
      return blob.includes(query);
    });
  }, [userDirectory, usersSearch]);

  const filteredGlobalUsers = useMemo(() => {
    const query = usersSearch.trim().toLowerCase();
    if (!query) {
      return globalUsers;
    }
    return globalUsers.filter((user) => {
      const organizations = Array.isArray(user.organization_ids) ? user.organization_ids.join(" ") : "";
      const blob = `${user.full_name || ""} ${user.email || ""} ${user.title || ""} ${user.user_id || ""} ${organizations}`.toLowerCase();
      return blob.includes(query);
    });
  }, [globalUsers, usersSearch]);

  const bulkVisibleUsers = useMemo(() => {
    if (!bulkFilterBySourceOrg) {
      return filteredGlobalUsers;
    }
    const sourceOrg = String(bulkTransferSourceOrgId || "").trim();
    if (!sourceOrg) {
      return filteredGlobalUsers;
    }
    return filteredGlobalUsers.filter((user) => {
      const orgIds = Array.isArray(user.organization_ids) ? user.organization_ids.map(String) : [];
      return orgIds.includes(sourceOrg);
    });
  }, [filteredGlobalUsers, bulkFilterBySourceOrg, bulkTransferSourceOrgId]);

  const filteredSkills = useMemo(() => {
    const query = skillsSearch.trim().toLowerCase();
    if (!query) {
      return availableSkills;
    }
    return availableSkills.filter((toolName) => String(toolName || "").toLowerCase().includes(query));
  }, [availableSkills, skillsSearch]);

  async function request(path, options = {}, withAuth = true) {
    const timeoutMs = Number(options.timeoutMs || 20000);
    const fetchOptions = { ...options };
    if ("timeoutMs" in fetchOptions) {
      delete fetchOptions.timeoutMs;
    }

    const isMultipart = options.body instanceof FormData;
    const headers = options.headers ? { ...options.headers } : {};
    if (!isMultipart && !("Content-Type" in headers)) {
      headers["Content-Type"] = "application/json";
    }
    if (withAuth && token) {
      headers.Authorization = `Bearer ${token}`;
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), Math.max(1000, timeoutMs));

    let response;
    try {
      response = await fetch(`${apiBase}${path}`, { ...fetchOptions, headers, signal: controller.signal });
    } catch (error) {
      clearTimeout(timeoutId);
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new Error("Request timeout. Try again.");
      }
      throw error;
    }
    clearTimeout(timeoutId);
    const rawText = await response.text().catch(() => "");
    let payload = null;
    if (rawText) {
      try {
        payload = JSON.parse(rawText);
      } catch {
        payload = null;
      }
    }

    if (!response.ok) {
      const errorMessage = parseApiError(payload) || rawText.trim() || `Request failed (HTTP ${response.status})`;

      const isAuthExpired =
        withAuth &&
        (response.status === 401 || /token\s+expired|expired\s+token/i.test(errorMessage));
      if (isAuthExpired) {
        closeChatSocket();
        clearPendingSendTimeout();
        writeStoredToken("");
        setToken("");
        setStatus("Session expired. Please sign in again.");
      }

      throw new Error(errorMessage);
    }
    return payload || {};
  }

  async function runAdminAction(label, action) {
    setIsBusy(true);
    try {
      const payload = await action();
      setAdminOutput(JSON.stringify(payload, null, 2));
      setStatus(`${label}: success.`);
    } catch (error) {
      setStatus(`${label}: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function refreshAdminAccess() {
    if (!token) {
      setIsAdmin(false);
      return;
    }

    setIsCheckingAdmin(true);
    try {
      await request("/admin/skills");
      setIsAdmin(true);
    } catch {
      setIsAdmin(false);
    } finally {
      setIsCheckingAdmin(false);
    }
  }

  async function loadMyTeams() {
    const payload = await request("/users/me/teams");
    const list = Array.isArray(payload) ? payload : [];
    const normalized = list
      .map((item) => ({
        org_id: String(item.org_id || "").trim(),
        team_id: String(item.team_id || "").trim(),
        team_name: String(item.team_name || item.team_id || "").trim(),
      }))
      .filter((item) => item.org_id && item.team_id);

    setTeams(normalized);

    if (normalized.length > 0) {
      const currentKey = activeTeam ? teamKey(activeTeam) : "";
      const found = normalized.find((item) => teamKey(item) === currentKey) || normalized[0];
      setActiveTeam(found);
      return found;
    }

    setActiveTeam(null);
    return null;
  }

  async function loadOrganizations() {
    const payload = await request("/admin/organizations");
    const list = Array.isArray(payload) ? payload : [];
    setOrganizations(list);

    if (!selectedOrgId && list.length > 0) {
      setSelectedOrgId(String(list[0].org_id || ""));
    }

    return { organizations: list.length };
  }

  async function createOrganization() {
    const orgId = String(createOrgForm.org_id || "").trim();
    if (!orgId) {
      throw new Error("Organization ID is required");
    }

    await request("/admin/organizations", {
      method: "POST",
      body: JSON.stringify({ org_id: orgId, name: String(createOrgForm.name || orgId).trim() || orgId }),
    });
    setCreateOrgForm(DEFAULT_CREATE_ORG_FORM);
    setSelectedOrgId(orgId);
    await loadOrganizations();
    return { org_id: orgId };
  }

  async function loadTeamsForOrg(orgId = selectedOrgId) {
    const normalizedOrg = String(orgId || "").trim();
    if (!normalizedOrg) {
      throw new Error("Select organization first");
    }

    const payload = await request(`/admin/organizations/${encodeURIComponent(normalizedOrg)}/teams`);
    const teamsList = Array.isArray(payload) ? payload : [];
    setOrgTeams(teamsList);

    const exists = teamsList.some((item) => String(item.team_id || "") === selectedTeamId);
    if (!exists) {
      setSelectedTeamId(teamsList.length > 0 ? String(teamsList[0].team_id || "") : "");
    }

    return { teams: teamsList.length };
  }

  async function createTeam() {
    const orgId = String(selectedOrgId || "").trim();
    const teamId = String(createTeamForm.team_id || "").trim();
    if (!orgId || !teamId) {
      throw new Error("Organization and Team IDs are required");
    }

    await request("/admin/teams", {
      method: "POST",
      body: JSON.stringify({ org_id: orgId, team_id: teamId, name: String(createTeamForm.name || teamId).trim() || teamId }),
    });
    setCreateTeamForm(DEFAULT_CREATE_TEAM_FORM);
    setSelectedTeamId(teamId);
    await loadTeamsForOrg(orgId);
    return { org_id: orgId, team_id: teamId };
  }

  async function loadTeamMembersAndUsers() {
    const orgId = String(selectedOrgId || "").trim();
    const teamId = String(selectedTeamId || "").trim();
    if (!orgId || !teamId) {
      throw new Error("Select organization and team");
    }

    const teamQuery = new URLSearchParams({ org_id: orgId }).toString();
    const [membersPayload, usersResult] = await Promise.all([
      request(`/admin/teams/${encodeURIComponent(teamId)}/members?${teamQuery}`),
      loadOrgUsersForTeams(orgId),
    ]);

    const members = Array.isArray(membersPayload) ? membersPayload : [];
    setTeamMembers(members);
    return { team_members: members.length, org_users: usersResult.org_users };
  }

  async function loadOrgUsersForTeams(orgId = selectedOrgId) {
    const normalizedOrg = String(orgId || "").trim();
    if (!normalizedOrg) {
      throw new Error("Select organization first");
    }

    const query = new URLSearchParams({ org_id: normalizedOrg });
    const usersPayload = await request(`/admin/users?${query.toString()}`);
    const users = Array.isArray(usersPayload) ? usersPayload : [];
    setOrgUsers(users);
    setSelectedUserIds([]);
    return { org_users: users.length };
  }

  async function addSelectedUsersToTeam() {
    const orgId = String(selectedOrgId || "").trim();
    const teamId = String(selectedTeamId || "").trim();
    if (!orgId || !teamId) {
      throw new Error("Select organization and team");
    }
    if (selectedUserIds.length === 0) {
      throw new Error("Select users to add");
    }

    for (const userId of selectedUserIds) {
      await request("/admin/teams/members", {
        method: "POST",
        body: JSON.stringify({ org_id: orgId, team_id: teamId, user_id: Number(userId) }),
      });
    }

    await loadTeamMembersAndUsers();
    await loadMyTeams();
    return { added: selectedUserIds.length };
  }

  async function removeUserFromTeam(userId) {
    const orgId = String(selectedOrgId || "").trim();
    const teamId = String(selectedTeamId || "").trim();
    if (!orgId || !teamId) {
      throw new Error("Select organization and team");
    }

    await request(`/admin/teams/${encodeURIComponent(teamId)}/members/${Number(userId)}?${new URLSearchParams({ org_id: orgId }).toString()}`, {
      method: "DELETE",
    });
    await loadTeamMembersAndUsers();
    await loadMyTeams();
    return { removed_user_id: Number(userId) };
  }

  async function loadUserDirectory(orgId = selectedOrgId) {
    const normalizedOrg = String(orgId || "").trim();
    if (!normalizedOrg) {
      throw new Error("Select organization first");
    }

    const payload = await request(`/admin/users?${new URLSearchParams({ org_id: normalizedOrg }).toString()}`);
    const list = Array.isArray(payload) ? payload : [];
    setUserDirectory(list);
    return { users: list.length };
  }

  async function loadAllUsersGlobal() {
    const payload = await request("/admin/users/all");
    const list = Array.isArray(payload) ? payload : [];
    setGlobalUsers(list);
    setBulkTransferUserIds((prev) => {
      const available = new Set(list.map((item) => Number(item.user_id)));
      return prev.filter((userId) => available.has(Number(userId)));
    });
    return { users: list.length, list };
  }

  function selectUserForEdit(user) {
    const fallbackGlobal = globalUsers.find((item) => Number(item.user_id) === Number(user.user_id));
    const sourceOrgIds = Array.isArray(user.organization_ids) ? user.organization_ids : fallbackGlobal?.organization_ids;
    const orgIds = Array.isArray(sourceOrgIds) ? sourceOrgIds.filter(Boolean) : [];
    const firstOrg = orgIds[0] || "";
    const fallbackTarget =
      orgIds.find((orgId) => String(orgId) !== firstOrg) ||
      organizations.find((org) => String(org.org_id || "") !== firstOrg)?.org_id ||
      "";

    setEditUserForm({
      user_id: String(user.user_id || ""),
      email: String(user.email || ""),
      full_name: String(user.full_name || ""),
      title: String(user.title || ""),
      profile_bio: String(user.profile_bio || ""),
      role: String(user.role || "member"),
      force_password_change: Boolean(user.force_password_change),
      organization_ids: orgIds,
    });

    setTransferSourceOrgId(firstOrg);
    setTransferTargetOrgId(String(fallbackTarget || ""));
    setTransferRole(String(user.role || "member"));
    setKeepSourceMembership(false);
  }

  async function updateUserProfile() {
    const userId = Number(editUserForm.user_id);
    if (!Number.isInteger(userId) || userId <= 0) {
      throw new Error("Choose user first");
    }

    await request(`/admin/users/${userId}`, {
      method: "PATCH",
      body: JSON.stringify({
        org_id: selectedOrgId,
        email: editUserForm.email,
        full_name: editUserForm.full_name,
        title: editUserForm.title,
        profile_bio: editUserForm.profile_bio,
        role: editUserForm.role,
        force_password_change: Boolean(editUserForm.force_password_change),
      }),
    });

    await loadUserDirectory();
    await loadAllUsersGlobal().catch(() => null);
    await loadTeamMembersAndUsers().catch(() => null);
    return { user_id: userId, status: "updated" };
  }

  async function createUserByAdmin() {
    if (!selectedOrgId) {
      throw new Error("Select organization first");
    }
    if (!createUserForm.email || !createUserForm.full_name) {
      throw new Error("Email and full name are required");
    }

    const payload = await request("/admin/users", {
      method: "POST",
      body: JSON.stringify({
        org_id: selectedOrgId,
        email: createUserForm.email,
        password: createUserForm.password || null,
        full_name: createUserForm.full_name,
        title: createUserForm.title,
        profile_bio: createUserForm.profile_bio,
        role: createUserForm.role,
      }),
    });

    setGeneratedCredential(`User created. OTP/password: ${String(payload.one_time_password || "")}`);
    setCreateUserForm(DEFAULT_CREATE_USER_FORM);
    await loadUserDirectory();
    await loadAllUsersGlobal().catch(() => null);
    await loadOrgUsersForTeams(selectedOrgId).catch(() => null);
    return payload;
  }

  async function resetUserOtp() {
    const userId = Number(editUserForm.user_id);
    if (!Number.isInteger(userId) || userId <= 0) {
      throw new Error("Choose user first");
    }

    const payload = await request(`/admin/users/${userId}/otp`, {
      method: "POST",
      body: JSON.stringify({ org_id: selectedOrgId, ttl_minutes: 60 }),
    });

    setGeneratedCredential(`One-time password: ${String(payload.one_time_password || "")}, expires: ${String(payload.expires_at || "")}`);
    return payload;
  }

  async function forcePasswordReset() {
    const userId = Number(editUserForm.user_id);
    if (!Number.isInteger(userId) || userId <= 0) {
      throw new Error("Choose user first");
    }

    const payload = await request(`/admin/users/${userId}/force-password-change`, {
      method: "POST",
      body: JSON.stringify({ org_id: selectedOrgId }),
    });

    setEditUserForm((prev) => ({ ...prev, force_password_change: true }));
    setGeneratedCredential("Forced password reset enabled. User must change password on next login.");
    await loadUserDirectory();
    await loadAllUsersGlobal().catch(() => null);
    await loadTeamMembersAndUsers().catch(() => null);
    return payload;
  }

  async function bindUserToActiveOrg() {
    const userId = Number(editUserForm.user_id);
    if (!Number.isInteger(userId) || userId <= 0) {
      throw new Error("Choose user first");
    }
    if (!selectedOrgId) {
      throw new Error("Select organization first");
    }

    const payload = await request(`/admin/users/${userId}/organizations/add`, {
      method: "POST",
      body: JSON.stringify({ org_id: selectedOrgId, role: editUserForm.role || "member" }),
    });

    await loadUserDirectory(selectedOrgId).catch(() => null);
    await loadAllUsersGlobal().catch(() => null);
    await loadOrgUsersForTeams(selectedOrgId).catch(() => null);
    await loadTeamMembersAndUsers().catch(() => null);
    return payload;
  }

  async function unbindUserFromActiveOrg() {
    const userId = Number(editUserForm.user_id);
    if (!Number.isInteger(userId) || userId <= 0) {
      throw new Error("Choose user first");
    }
    if (!selectedOrgId) {
      throw new Error("Select organization first");
    }

    const payload = await request(`/admin/users/${userId}/organizations/${encodeURIComponent(selectedOrgId)}`, {
      method: "DELETE",
    });

    await loadUserDirectory(selectedOrgId).catch(() => null);
    await loadAllUsersGlobal().catch(() => null);
    await loadOrgUsersForTeams(selectedOrgId).catch(() => null);
    await loadTeamMembersAndUsers().catch(() => null);
    return payload;
  }

  async function reassignUserOrganization() {
    const userId = Number(editUserForm.user_id);
    if (!Number.isInteger(userId) || userId <= 0) {
      throw new Error("Choose user first");
    }

    const sourceOrg = String(transferSourceOrgId || "").trim();
    const targetOrg = String(transferTargetOrgId || "").trim();
    if (!sourceOrg || !targetOrg) {
      throw new Error("Select source and target organizations");
    }
    if (sourceOrg === targetOrg) {
      throw new Error("Source and target organizations must be different");
    }

    await request(`/admin/users/${userId}/organizations/add`, {
      method: "POST",
      body: JSON.stringify({ org_id: targetOrg, role: transferRole || editUserForm.role || "member" }),
    });

    if (!keepSourceMembership) {
      await request(`/admin/users/${userId}/organizations/${encodeURIComponent(sourceOrg)}`, {
        method: "DELETE",
      });
    }

    await loadUserDirectory(selectedOrgId).catch(() => null);
    const refreshed = await loadAllUsersGlobal();
    await loadOrgUsersForTeams(selectedOrgId).catch(() => null);
    await loadTeamMembersAndUsers().catch(() => null);

    const updatedUser = Array.isArray(refreshed.list)
      ? refreshed.list.find((item) => Number(item.user_id) === userId)
      : null;
    if (updatedUser) {
      selectUserForEdit(updatedUser);
    } else {
      setTransferSourceOrgId("");
      setTransferTargetOrgId("");
    }

    return {
      user_id: userId,
      source_org: sourceOrg,
      target_org: targetOrg,
      removed_from_source: !keepSourceMembership,
    };
  }

  async function bulkReassignUsersOrganizations() {
    const selectedIds = [...new Set(bulkTransferUserIds.map(Number).filter((item) => Number.isInteger(item) && item > 0))];
    if (selectedIds.length === 0) {
      throw new Error("Select users for bulk reassignment");
    }

    const sourceOrg = String(bulkTransferSourceOrgId || "").trim();
    const targetOrg = String(bulkTransferTargetOrgId || "").trim();
    if (!sourceOrg || !targetOrg) {
      throw new Error("Select source and target organizations");
    }
    if (sourceOrg === targetOrg) {
      throw new Error("Source and target organizations must be different");
    }

    let moved = 0;
    let addedOnly = 0;
    for (const userId of selectedIds) {
      const user = globalUsers.find((item) => Number(item.user_id) === userId);
      const orgIds = Array.isArray(user?.organization_ids) ? user.organization_ids.map(String) : [];
      const hasSourceMembership = orgIds.includes(sourceOrg);

      await request(`/admin/users/${userId}/organizations/add`, {
        method: "POST",
        body: JSON.stringify({ org_id: targetOrg, role: bulkTransferRole || "member" }),
      });

      if (!bulkKeepSourceMembership && hasSourceMembership) {
        await request(`/admin/users/${userId}/organizations/${encodeURIComponent(sourceOrg)}`, {
          method: "DELETE",
        });
        moved += 1;
      } else {
        addedOnly += 1;
      }
    }

    await loadUserDirectory(selectedOrgId).catch(() => null);
    await loadAllUsersGlobal();
    await loadOrgUsersForTeams(selectedOrgId).catch(() => null);
    await loadTeamMembersAndUsers().catch(() => null);

    return {
      selected_users: selectedIds.length,
      moved,
      added_only: addedOnly,
      source_org: sourceOrg,
      target_org: targetOrg,
      kept_source_membership: bulkKeepSourceMembership,
    };
  }

  async function fetchMessagesFor(team, silent = false) {
    if (!team) {
      setMessages([]);
      return;
    }

    if (!silent) {
      setIsBusy(true);
    }

    try {
      const query = new URLSearchParams({ org_id: team.org_id, team_id: team.team_id }).toString();
      const payload = await request(`/chat/messages?${query}`);
      setMessages(Array.isArray(payload) ? payload : []);
    } catch (error) {
      if (!silent) {
        setStatus(`Sync failed: ${error.message}`);
      }
    } finally {
      if (!silent) {
        setIsBusy(false);
      }
    }
  }

  function clearPendingSendTimeout() {
    if (pendingSendTimeoutRef.current) {
      clearTimeout(pendingSendTimeoutRef.current);
      pendingSendTimeoutRef.current = null;
    }
  }

  function buildChatWsUrl() {
    const httpUrl = new URL(apiBase, window.location.origin);
    const wsProtocol = httpUrl.protocol === "https:" ? "wss:" : "ws:";
    return `${wsProtocol}//${httpUrl.host}${httpUrl.pathname}/chat/ws?token=${encodeURIComponent(token)}`;
  }

  function closeChatSocket() {
    if (wsRetryTimerRef.current) {
      clearTimeout(wsRetryTimerRef.current);
      wsRetryTimerRef.current = null;
    }
    if (wsRef.current) {
      const socket = wsRef.current;
      try {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      } catch {
        // ignore close errors
      }
      wsRef.current = null;
    }
  }

  async function handleRegister(event) {
    event.preventDefault();
    setIsBusy(true);
    try {
      await request("/auth/register", { method: "POST", body: JSON.stringify(registerForm) }, false);
      setStatus("Profile registered. Sign in with your credentials.");
      setMode("login");
      setLoginForm((prev) => ({ ...prev, email: registerForm.email }));
      setRegisterForm(DEFAULT_REGISTER_FORM);
    } catch (error) {
      setStatus(`Registration failed: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function handleLogin(event) {
    event.preventDefault();
    setIsBusy(true);
    try {
      const payload = await request("/auth/login", { method: "POST", body: JSON.stringify(loginForm) }, false);
      const nextToken = String(payload.access_token || "");
      setToken(nextToken);
      writeStoredToken(nextToken);
      setMode("login");
      setMessages([]);
      setMessageDraft("");
      setStatus("Authorized.");
      setLoginForm(DEFAULT_LOGIN_FORM);

      await refreshAdminAccess();
      const firstTeam = await loadMyTeams();
      if (firstTeam) {
        await fetchMessagesFor(firstTeam, true);
      }
    } catch (error) {
      setStatus(`Login failed: ${error.message}`);
    } finally {
      setIsBusy(false);
    }
  }

  async function sendMessage(rawText) {
    const text = String(rawText || "").trim();
    if (!text) {
      return;
    }
    if (!activeTeam) {
      setStatus("Select team in sidebar first.");
      return;
    }

    const clientMessageId = `msg-${Date.now()}-${Math.floor(Math.random() * 100000)}`;
    setPendingMessage({
      temp_id: `pending-${Date.now()}`,
      client_message_id: clientMessageId,
      sender_type: "user",
      sender_user_id: null,
      content: text,
      created_at: new Date().toISOString(),
      pending: true,
    });
    setIsBusy(true);

    const activeSocket = wsRef.current;
    if (activeSocket && activeSocket.readyState === WebSocket.OPEN && autoRefresh) {
      try {
        activeSocket.send(
          JSON.stringify({
            action: "send",
            org_id: activeTeam.org_id,
            team_id: activeTeam.team_id,
            message: text,
            client_message_id: clientMessageId,
          }),
        );
        setMessageDraft("");
        setStatus("Message sent. Waiting for assistant...");
        clearPendingSendTimeout();
        pendingSendTimeoutRef.current = setTimeout(() => {
          setIsBusy(false);
          setStatus("Realtime response timeout. You can retry or use Refresh.");
        }, 25000);
        return;
      } catch {
        // fallback to REST below
      }
    }

    try {
      const payload = await request("/chat/send", {
        method: "POST",
        body: JSON.stringify({ org_id: activeTeam.org_id, team_id: activeTeam.team_id, message: text }),
      });
      setMessages(Array.isArray(payload.messages) ? payload.messages : []);
      setPendingMessage(null);
      setMessageDraft("");
      setStatus("Assistant responded.");
    } catch (error) {
      setPendingMessage(null);
      setStatus(`Message failed: ${error.message}`);
    } finally {
      clearPendingSendTimeout();
      setIsBusy(false);
    }
  }

  async function indexRagDocument() {
    if (!activeTeam) {
      setStatus("Select team in sidebar first.");
      return;
    }
    if (!ragUploadFile) {
      setStatus("Choose a .txt, .md or .pdf file for RAG indexing.");
      return;
    }

    setRagBusy(true);
    setStatus("Indexing document into RAG...");
    try {
      const formData = new FormData();
      formData.append("org_id", activeTeam.org_id);
      formData.append("team_id", activeTeam.team_id);
      formData.append("scope", ragUploadScope);
      formData.append("file", ragUploadFile);

      const payload = await request("/rag/index-file", {
        method: "POST",
        body: formData,
        timeoutMs: 600000,
      });

      setRagResult(payload);
      await loadRagJobs({ silent: true });
      setRagUploadFile(null);
      if (ragFileInputRef.current) {
        ragFileInputRef.current.value = "";
      }
      const jobId = String(payload?.job?.job_id || "").trim();
      setStatus(jobId ? `RAG indexing queued (job: ${jobId}).` : "RAG indexing queued.");
    } catch (error) {
      setStatus(`RAG indexing failed: ${error.message}`);
    } finally {
      setRagBusy(false);
    }
  }

  async function loadRagJobs({ silent = false } = {}) {
    if (!activeTeam) {
      setRagJobs([]);
      return;
    }

    if (!silent) {
      setRagJobsBusy(true);
    }
    try {
      const query = new URLSearchParams({
        org_id: activeTeam.org_id,
        team_id: activeTeam.team_id,
        limit: "10",
      }).toString();
      const payload = await request(`/rag/index-jobs?${query}`, { timeoutMs: 20000 });
      setRagJobs(Array.isArray(payload?.jobs) ? payload.jobs : []);
    } catch (error) {
      if (!silent) {
        setStatus(`RAG jobs load failed: ${error.message}`);
      }
    } finally {
      if (!silent) {
        setRagJobsBusy(false);
      }
    }
  }

  async function queryRagDocuments() {
    const text = String(ragQueryForm.query || "").trim();
    if (!activeTeam) {
      setStatus("Select team in sidebar first.");
      return;
    }
    if (!text) {
      setStatus("Type a question for RAG query.");
      return;
    }

    setRagBusy(true);
    setStatus("Running RAG query...");
    try {
      const payload = await request("/rag/query", {
        method: "POST",
        body: JSON.stringify({
          org_id: activeTeam.org_id,
          team_id: activeTeam.team_id,
          query: text,
          scope: ragQueryForm.scope,
        }),
        timeoutMs: 60000,
      });

      setRagResult(payload);
      setStatus("RAG answer received.");
    } catch (error) {
      setStatus(`RAG query failed: ${error.message}`);
    } finally {
      setRagBusy(false);
    }
  }

  async function loadTeamSkills() {
    if (!selectedOrgId || !selectedTeamId) {
      throw new Error("Select organization and team first");
    }

    const skillsPayload = await request("/admin/skills");
    const query = new URLSearchParams({ org_id: selectedOrgId }).toString();
    const teamPayload = await request(`/admin/teams/${encodeURIComponent(selectedTeamId)}/skills?${query}`);

    const catalog = Array.isArray(skillsPayload?.skills)
      ? skillsPayload.skills.map((item) => String(item?.tool_name || "").trim()).filter(Boolean)
      : [];
    const assigned = Array.isArray(teamPayload?.skills)
      ? teamPayload.skills.map((item) => String(item || "").trim()).filter(Boolean)
      : [];

    setAvailableSkills(catalog);
    setTeamAssignedSkills(assigned);
    setTeamSelectedSkills(assigned);
    return { catalog_count: catalog.length, team_assigned_count: assigned.length };
  }

  async function saveTeamSkills() {
    const current = new Set(teamAssignedSkills);
    const selected = new Set(teamSelectedSkills);
    const toGrant = [...selected].filter((item) => !current.has(item));
    const toRevoke = [...current].filter((item) => !selected.has(item));

    for (const toolName of toGrant) {
      await request(`/admin/teams/${encodeURIComponent(selectedTeamId)}/skills/grant`, {
        method: "POST",
        body: JSON.stringify({ org_id: selectedOrgId, tool_name: toolName }),
      });
    }
    for (const toolName of toRevoke) {
      await request(`/admin/teams/${encodeURIComponent(selectedTeamId)}/skills/revoke`, {
        method: "POST",
        body: JSON.stringify({ org_id: selectedOrgId, tool_name: toolName }),
      });
    }

    setTeamAssignedSkills([...selected]);
    return { granted: toGrant, revoked: toRevoke };
  }

  function toggleUserSelection(userId, checked) {
    setSelectedUserIds((prev) => {
      if (checked) {
        return [...new Set([...prev, userId])];
      }
      return prev.filter((item) => item !== userId);
    });
  }

  function toggleBulkUserSelection(userId, checked) {
    const normalized = Number(userId);
    setBulkTransferUserIds((prev) => {
      if (checked) {
        return [...new Set([...prev, normalized])];
      }
      return prev.filter((item) => Number(item) !== normalized);
    });
  }

  function selectAllVisibleGlobalUsers() {
    const visibleIds = bulkVisibleUsers.map((user) => Number(user.user_id)).filter((userId) => Number.isInteger(userId) && userId > 0);
    setBulkTransferUserIds((prev) => [...new Set([...prev, ...visibleIds])]);
  }

  function clearBulkUserSelection() {
    setBulkTransferUserIds([]);
  }

  function toggleTeamSkill(toolName, checked) {
    setTeamSelectedSkills((prev) => {
      if (checked) {
        return [...new Set([...prev, toolName])].sort((a, b) => a.localeCompare(b));
      }
      return prev.filter((item) => item !== toolName);
    });
  }

  function selectAllVisibleSkills() {
    setTeamSelectedSkills((prev) => [...new Set([...prev, ...filteredSkills])].sort((a, b) => a.localeCompare(b)));
  }

  function clearVisibleSkills() {
    const visible = new Set(filteredSkills);
    setTeamSelectedSkills((prev) => prev.filter((item) => !visible.has(item)));
  }

  function selectTeam(team) {
    setActiveTeam(team);
    void fetchMessagesFor(team, true);
  }

  function onComposerKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendMessage(messageDraft);
    }
  }

  function logout() {
    closeChatSocket();
    clearPendingSendTimeout();
    writeStoredToken("");
    setToken("");
    setMessages([]);
    setTeams([]);
    setActiveTeam(null);
    setIsAdmin(false);
    setActiveScreen("chat");
    setAdminOutput("No admin actions yet.");
    setOrganizations([]);
    setOrgTeams([]);
    setTeamMembers([]);
    setOrgUsers([]);
    setUserDirectory([]);
    setStatus("Signed out.");
    setWsStatus("offline");
    setRagUploadFile(null);
    setRagQueryForm(DEFAULT_RAG_QUERY_FORM);
    setRagResult(null);
    setRagJobs([]);
    if (ragFileInputRef.current) {
      ragFileInputRef.current.value = "";
    }
  }

  useEffect(() => {
    if (!isAuthenticated || !activeTeam) {
      setRagJobs([]);
      return undefined;
    }

    void loadRagJobs({ silent: true });
    const timerId = setInterval(() => {
      void loadRagJobs({ silent: true });
    }, 5000);

    return () => clearInterval(timerId);
  }, [isAuthenticated, activeTeam?.org_id, activeTeam?.team_id]);

  useEffect(() => {
    if (!isAuthenticated || !autoRefresh || !activeTeam || wsStatus === "online") {
      return undefined;
    }

    const timerId = setInterval(() => {
      void fetchMessagesFor(activeTeam, true);
    }, 7000);

    return () => clearInterval(timerId);
  }, [isAuthenticated, autoRefresh, activeTeam?.org_id, activeTeam?.team_id, wsStatus]);

  useEffect(() => {
    pendingMessageRef.current = pendingMessage;
  }, [pendingMessage]);

  useEffect(() => {
    if (!isAuthenticated || !activeTeam || !autoRefresh) {
      closeChatSocket();
      setWsStatus("offline");
      return undefined;
    }

    let disposed = false;

    function connect() {
      if (disposed) {
        return;
      }

      closeChatSocket();
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
        socket.send(JSON.stringify({ action: "subscribe", org_id: activeTeam.org_id, team_id: activeTeam.team_id }));
      };

      socket.onmessage = (event) => {
        let payload;
        try {
          payload = JSON.parse(event.data);
        } catch {
          return;
        }

        if (payload?.type === "error") {
          setStatus(`Realtime error: ${payload.detail || "Unknown error"}`);
          setIsBusy(false);
          clearPendingSendTimeout();
          return;
        }

        if (payload?.type !== "chat.snapshot") {
          return;
        }

        if (payload.org_id !== activeTeam.org_id || payload.team_id !== activeTeam.team_id) {
          return;
        }

        setMessages(Array.isArray(payload.messages) ? payload.messages : []);
        const currentPending = pendingMessageRef.current;
        if (!currentPending) {
          return;
        }
        if (!payload.client_message_id || payload.client_message_id === currentPending.client_message_id) {
          setPendingMessage(null);
          setIsBusy(false);
          clearPendingSendTimeout();
          setStatus("Assistant responded.");
        }
      };

      socket.onerror = () => {
        if (disposed) {
          return;
        }
        setWsStatus("degraded");
      };

      socket.onclose = () => {
        if (disposed) {
          return;
        }
        setWsStatus("offline");
        wsRetryTimerRef.current = setTimeout(connect, 2000);
      };
    }

    connect();

    return () => {
      disposed = true;
      closeChatSocket();
    };
  }, [isAuthenticated, activeTeam?.org_id, activeTeam?.team_id, autoRefresh, token, apiBase]);

  useEffect(() => {
    if (!isAuthenticated) {
      return;
    }
    void refreshAdminAccess();
    void loadMyTeams().catch(() => null);
  }, [isAuthenticated, token]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pendingMessage]);

  const displayedMessages = useMemo(() => {
    if (!pendingMessage) {
      return messages;
    }
    return [...messages, pendingMessage];
  }, [messages, pendingMessage]);

  useEffect(() => {
    if (!isAdmin && activeScreen === "admin") {
      setActiveScreen("chat");
    }
  }, [isAdmin, activeScreen]);

  useEffect(() => {
    if (!isAuthenticated || !isAdmin || activeScreen !== "admin") {
      return;
    }

    void runAdminAction("Load organizations", loadOrganizations);
  }, [isAuthenticated, isAdmin, activeScreen]);

  useEffect(() => {
    if (!isAuthenticated || !isAdmin || !selectedOrgId) {
      return;
    }

    void loadTeamsForOrg(selectedOrgId).catch(() => null);
    void loadUserDirectory(selectedOrgId).catch(() => null);
    void loadAllUsersGlobal().catch(() => null);
    void loadOrgUsersForTeams(selectedOrgId).catch(() => null);
  }, [selectedOrgId, isAuthenticated, isAdmin]);

  useEffect(() => {
    if (!selectedOrgId) {
      return;
    }
    if (!transferTargetOrgId) {
      setTransferTargetOrgId(String(selectedOrgId));
    }
  }, [selectedOrgId, transferTargetOrgId]);

  useEffect(() => {
    if (!selectedOrgId) {
      return;
    }
    if (!bulkTransferSourceOrgId) {
      setBulkTransferSourceOrgId(String(selectedOrgId));
    }
    if (!bulkTransferTargetOrgId) {
      setBulkTransferTargetOrgId(String(selectedOrgId));
    }
  }, [selectedOrgId, bulkTransferSourceOrgId, bulkTransferTargetOrgId]);

  useEffect(() => {
    if (!isAuthenticated || !isAdmin || !selectedOrgId || !selectedTeamId) {
      return;
    }

    void loadTeamMembersAndUsers().catch(() => null);
  }, [selectedOrgId, selectedTeamId, isAuthenticated, isAdmin]);

  return (
    <div className="page">
      <div className="aurora aurora-left" />
      <div className="aurora aurora-right" />

      <main className="shell">
        {!isAuthenticated ? (
          <section className="card auth-card reveal-up delay-1">
            <div className="tabs" role="tablist" aria-label="Auth tabs">
              <button className={mode === "login" ? "tab active" : "tab"} onClick={() => setMode("login")} type="button">Login</button>
              <button className={mode === "register" ? "tab active" : "tab"} onClick={() => setMode("register")} type="button">Register</button>
            </div>

            {mode === "login" ? (
              <form className="form" onSubmit={handleLogin}>
                <label>Email<input required type="email" value={loginForm.email} onChange={(event) => setLoginForm((prev) => ({ ...prev, email: event.target.value }))} placeholder="user@company.com" /></label>
                <label>Password<input required minLength={8} type="password" value={loginForm.password} onChange={(event) => setLoginForm((prev) => ({ ...prev, password: event.target.value }))} placeholder="StrongPassword123" /></label>
                <button disabled={isBusy} className="primary" type="submit">{isBusy ? "Signing in..." : "Sign In"}</button>
              </form>
            ) : (
              <form className="form" onSubmit={handleRegister}>
                <label>Full name<input required value={registerForm.full_name} onChange={(event) => setRegisterForm((prev) => ({ ...prev, full_name: event.target.value }))} placeholder="Jane Smith" /></label>
                <label>Email<input required type="email" value={registerForm.email} onChange={(event) => setRegisterForm((prev) => ({ ...prev, email: event.target.value }))} placeholder="user@company.com" /></label>
                <label>Password<input required minLength={8} type="password" value={registerForm.password} onChange={(event) => setRegisterForm((prev) => ({ ...prev, password: event.target.value }))} placeholder="StrongPassword123" /></label>
                <label>Title<input value={registerForm.title} onChange={(event) => setRegisterForm((prev) => ({ ...prev, title: event.target.value }))} placeholder="Product Manager" /></label>
                <label>Profile bio<textarea rows={3} value={registerForm.profile_bio} onChange={(event) => setRegisterForm((prev) => ({ ...prev, profile_bio: event.target.value }))} placeholder="Owns roadmap and GTM alignment" /></label>
                <button disabled={isBusy} className="primary" type="submit">{isBusy ? "Creating..." : "Create Profile"}</button>
              </form>
            )}
          </section>
        ) : (
          <section className="app-grid reveal-up delay-1">
            <aside className="sidebar card">
              <div className="sidebar-top">
                <h2>SmartAi</h2>
                <div className={`role-badge ${isAdmin ? "role-admin" : "role-member"}`}>
                  {isCheckingAdmin ? "Role: checking..." : `Role: ${isAdmin ? "admin" : "member"}`}
                </div>
              </div>

              <nav className="sidebar-menu">
                <button type="button" className={activeScreen === "chat" ? "tab active" : "tab"} onClick={() => setActiveScreen("chat")}>Chat</button>
                {isAdmin ? (
                  <button type="button" className={activeScreen === "admin" ? "tab active" : "tab"} onClick={() => setActiveScreen("admin")}>Admin</button>
                ) : null}
              </nav>

              <div className="teams-panel">
                <div className="teams-header">
                  <h3>Teams</h3>
                  <button type="button" className="ghost" onClick={() => void runAdminAction("Refresh teams", loadMyTeams)}>Refresh</button>
                </div>

                {teams.length === 0 ? (
                  <div className="empty">No teams yet. {isAdmin ? "Open Admin to create one." : "Ask admin to add you to a team."}</div>
                ) : (
                  <div className="teams-list">
                    {teams.map((team) => {
                      const selected = activeTeam && teamKey(activeTeam) === teamKey(team);
                      return (
                        <button
                          key={teamKey(team)}
                          type="button"
                          className={selected ? "team-item team-item-active" : "team-item"}
                          onClick={() => selectTeam(team)}
                        >
                          <strong>{team.team_name}</strong>
                          <span>{team.org_id}/{team.team_id}</span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>

            </aside>

            <section className="main-pane card">
              <div className="pane-topbar">
                <span className="pane-topbar-text">Workspace</span>
                <button type="button" className="ghost" onClick={logout}>Logout</button>
              </div>

              {activeScreen === "chat" ? (
                <div className="chat-window">
                  <header className="chat-header">
                    <div>
                      <h1>Gemini-style Team Chat</h1>
                      <p className="subtitle">
                        {activeTeam ? `${activeTeam.team_name} (${activeTeam.org_id}/${activeTeam.team_id})` : "Select team in sidebar"}
                      </p>
                    </div>
                    <label className="toggle"><input type="checkbox" checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)} />Realtime ({wsStatus})</label>
                  </header>

                  <div className="messages-wrap">
                    {displayedMessages.length === 0 ? (
                      <div className="empty">No messages yet. Send your first prompt.</div>
                    ) : (
                      <div className="messages-list" role="log" aria-live="polite">
                        {displayedMessages.map((item, index) => {
                          const sender = item.sender_type === "assistant" ? "assistant" : "user";
                          return (
                            <div key={`${item.temp_id || item.created_at}-${index}`} className={`message ${sender} ${item.pending ? "pending" : ""} reveal-up`}>
                              <div className="meta">
                                <span className="sender">{item.sender_type}</span>
                                <span>{item.pending ? "sending..." : new Date(item.created_at).toLocaleString()}</span>
                              </div>
                              {renderMessageContent(item.content)}
                            </div>
                          );
                        })}
                        {isBusy ? <div className="typing">Assistant is thinking...</div> : null}
                        <div ref={messagesEndRef} />
                      </div>
                    )}
                  </div>

                  <div className="composer card">
                    <textarea rows={4} value={messageDraft} onChange={(event) => setMessageDraft(event.target.value)} onKeyDown={onComposerKeyDown} placeholder="Write a task for your assistant" />
                    <div className="composer-row">
                      <div className="compose-hint">Enter - send, Shift+Enter - new line</div>
                      <div className="composer-actions">
                        <button className="secondary" type="button" disabled={isBusy || !activeTeam} onClick={() => void fetchMessagesFor(activeTeam, false)}>Refresh</button>
                        <button className="primary" type="button" disabled={isBusy || !activeTeam || !messageDraft.trim()} onClick={() => void sendMessage(messageDraft)}>{isBusy ? "Sending..." : "Send"}</button>
                      </div>
                    </div>
                  </div>

                  <section className="rag-panel card">
                    <div className="rag-head">
                      <h3>Document RAG</h3>
                      <span className="pane-topbar-text">Index + Query</span>
                    </div>

                    <div className="rag-grid">
                      <label>
                        Scope for indexing
                        <select value={ragUploadScope} onChange={(event) => setRagUploadScope(event.target.value)}>
                          <option value="team">team</option>
                          <option value="private">private</option>
                        </select>
                      </label>
                      <label>
                        File (.txt, .md, .pdf)
                        <input
                          ref={ragFileInputRef}
                          type="file"
                          accept=".txt,.md,.pdf"
                          onChange={(event) => setRagUploadFile(event.target.files?.[0] || null)}
                        />
                      </label>
                    </div>
                    <div className="rag-actions">
                      <button
                        type="button"
                        className="secondary"
                        disabled={ragBusy || !activeTeam || !ragUploadFile}
                        onClick={() => void indexRagDocument()}
                      >
                        {ragBusy ? "Processing..." : "Index Document"}
                      </button>
                    </div>

                    <div className="rag-grid rag-query-grid">
                      <label className="rag-query-box">
                        Ask indexed documents
                        <textarea
                          rows={3}
                          value={ragQueryForm.query}
                          onChange={(event) => setRagQueryForm((prev) => ({ ...prev, query: event.target.value }))}
                          placeholder="What does the uploaded document say about ...?"
                        />
                      </label>
                      <label>
                        Query scope
                        <select
                          value={ragQueryForm.scope}
                          onChange={(event) => setRagQueryForm((prev) => ({ ...prev, scope: event.target.value }))}
                        >
                          <option value="team">team</option>
                          <option value="private">private</option>
                        </select>
                      </label>
                    </div>
                    <p className="rag-hint">
                      Tip: ask specific questions and include key terms from your document, for example: "What are invoice approval limits in this policy?"
                    </p>
                    <div className="rag-actions">
                      <button
                        type="button"
                        className="primary"
                        disabled={ragBusy || !activeTeam || !String(ragQueryForm.query || "").trim()}
                        onClick={() => void queryRagDocuments()}
                      >
                        {ragBusy ? "Processing..." : "Query RAG"}
                      </button>
                    </div>

                    {ragResult ? (
                      <div className="rag-result">
                        {ragResult.answer ? (
                          <div className="rag-answer">
                            <h4>Answer</h4>
                            <p>{String(ragResult.answer || "")}</p>
                          </div>
                        ) : null}
                        <pre>{JSON.stringify(ragResult, null, 2)}</pre>
                      </div>
                    ) : null}

                    <div className="rag-jobs">
                      <div className="rag-jobs-top">
                        <h4>Indexing Jobs</h4>
                        <button
                          type="button"
                          className="ghost"
                          disabled={ragBusy || ragJobsBusy || !activeTeam}
                          onClick={() => void loadRagJobs({ silent: false })}
                        >
                          {ragJobsBusy ? "Refreshing..." : "Refresh Jobs"}
                        </button>
                      </div>
                      {ragJobs.length === 0 ? (
                        <div className="empty">No indexing jobs yet.</div>
                      ) : (
                        <div className="rag-jobs-list">
                          {ragJobs.map((job) => {
                            const statusValue = String(job.status || "unknown").toLowerCase();
                            const badgeClass =
                              statusValue === "succeeded"
                                ? "rag-job-status status-ok"
                                : statusValue === "failed"
                                  ? "rag-job-status status-failed"
                                  : "rag-job-status status-running";
                            return (
                              <div key={String(job.job_id || `${job.file_name}-${job.created_at}`)} className="rag-job-item">
                                <div className="rag-job-head">
                                  <strong>{String(job.file_name || "unknown file")}</strong>
                                  <span className={badgeClass}>{statusValue}</span>
                                </div>
                                <div className="rag-job-meta">
                                  <span>scope: {String(job.scope || "team")}</span>
                                  <span>created: {job.created_at ? new Date(job.created_at).toLocaleString() : "n/a"}</span>
                                  <span>chunks: {job.chunks_count ?? "-"}</span>
                                </div>
                                {job.error ? <div className="rag-job-error">{String(job.error)}</div> : null}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  </section>
                </div>
              ) : (
                <div className="admin-window">
                  <header className="chat-header">
                    <div>
                      <h1>Admin Console</h1>
                      <p className="subtitle">Separate workflows for organizations, teams, users and team skills</p>
                    </div>
                  </header>

                  <div className="admin-tools">
                    <div className="workspace-tabs">
                      {ADMIN_SECTIONS.map((section) => (
                        <button
                          key={section.key}
                          type="button"
                          className={adminSection === section.key ? "tab active" : "tab"}
                          onClick={() => setAdminSection(section.key)}
                        >
                          {section.label}
                        </button>
                      ))}
                    </div>

                    <div className="context-grid admin-grid">
                      <label>
                        Active Organization
                        <select value={selectedOrgId} onChange={(event) => setSelectedOrgId(event.target.value)}>
                          <option value="">Select organization</option>
                          {organizations.map((org) => (
                            <option key={org.org_id} value={org.org_id}>{org.org_id}</option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Active Team
                        <select value={selectedTeamId} onChange={(event) => setSelectedTeamId(event.target.value)} disabled={!hasOrgId}>
                          <option value="">Select team</option>
                          {orgTeams.map((team) => (
                            <option key={team.team_id} value={team.team_id}>{team.team_name || team.team_id}</option>
                          ))}
                        </select>
                      </label>
                    </div>

                    {adminSection === "organizations" ? (
                      <div className="skills-panel">
                        <h3>Create Organization</h3>
                        <div className="context-grid admin-grid">
                          <label>Organization ID<input value={createOrgForm.org_id} onChange={(event) => setCreateOrgForm((prev) => ({ ...prev, org_id: event.target.value }))} placeholder="acme" /></label>
                          <label>Organization Name<input value={createOrgForm.name} onChange={(event) => setCreateOrgForm((prev) => ({ ...prev, name: event.target.value }))} placeholder="Acme Corp" /></label>
                        </div>
                        <div className="admin-actions">
                          <button type="button" className="primary" disabled={isBusy} onClick={() => void runAdminAction("Create organization", createOrganization)}>Create Organization</button>
                          <button type="button" className="secondary" disabled={isBusy} onClick={() => void runAdminAction("Refresh organizations", loadOrganizations)}>Refresh List</button>
                        </div>
                        <h3>Organizations</h3>
                        {organizations.length === 0 ? <div className="empty">No organizations yet.</div> : (
                          <div className="skills-grid">
                            {organizations.map((org) => (
                              <button type="button" key={org.org_id} className={selectedOrgId === org.org_id ? "team-item team-item-active" : "team-item"} onClick={() => setSelectedOrgId(org.org_id)}>
                                <strong>{org.name || org.org_id}</strong>
                                <span>{org.org_id}</span>
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    ) : null}

                    {adminSection === "teams" ? (
                      <div className="skills-panel">
                        <h3>Create Team And Bind To Organization</h3>
                        <div className="context-grid admin-grid">
                          <label>Team ID<input value={createTeamForm.team_id} onChange={(event) => setCreateTeamForm((prev) => ({ ...prev, team_id: event.target.value }))} placeholder="finance" /></label>
                          <label>Team Name<input value={createTeamForm.name} onChange={(event) => setCreateTeamForm((prev) => ({ ...prev, name: event.target.value }))} placeholder="Finance Team" /></label>
                        </div>
                        <div className="admin-actions">
                          <button type="button" className="primary" disabled={isBusy || !hasOrgId} onClick={() => void runAdminAction("Create team", createTeam)}>Create Team</button>
                          <button type="button" className="secondary" disabled={isBusy || !hasOrgId} onClick={() => void runAdminAction("Refresh teams", () => loadTeamsForOrg())}>Refresh Teams</button>
                          <button type="button" className="secondary" disabled={isBusy || !hasOrgId || !hasTeamId} onClick={() => void runAdminAction("Load team users", loadTeamMembersAndUsers)}>Load Team Users</button>
                          <button type="button" className="ghost" disabled={isBusy || !hasOrgId} onClick={() => void runAdminAction("Load organization users", () => loadOrgUsersForTeams(selectedOrgId))}>Load Org Users</button>
                        </div>

                        <h3>Team Members</h3>
                        <label>
                          Search Team Members
                          <input value={teamMembersSearch} onChange={(event) => setTeamMembersSearch(event.target.value)} placeholder="Find by name, email, role or id" />
                        </label>
                        {teamMembers.length === 0 ? <div className="empty">Select team and load members.</div> : (
                          <div className="list-table">
                            <div className="list-row list-header"><span>User</span><span>Role</span><span>Actions</span></div>
                            {filteredTeamMembers.map((member) => (
                              <div key={member.user_id} className="list-row">
                                <span>{member.full_name || member.email} (id: {member.user_id})</span>
                                <span>{member.role || "member"}</span>
                                <button type="button" className="ghost" disabled={isBusy} onClick={() => void runAdminAction("Remove user from team", () => removeUserFromTeam(member.user_id))}>Remove</button>
                              </div>
                            ))}
                          </div>
                        )}

                        <h3>Add Users To Team</h3>
                        <div className="skills-summary">
                          <span>Users in org: {orgUsers.length}</span>
                          <span>Selected team: {hasTeamId ? selectedTeamId : "not selected"}</span>
                        </div>
                        <label>
                          Search Organization Users
                          <input value={orgUsersSearch} onChange={(event) => setOrgUsersSearch(event.target.value)} placeholder="Find users to add" />
                        </label>
                        {orgUsers.length === 0 ? <div className="empty">Load organization users.</div> : (
                          <>
                            <div className="skills-grid">
                              {filteredOrgUsers.map((user) => {
                                const alreadyInSelectedTeam = selectedTeamMemberIds.has(Number(user.user_id));
                                return (
                                  <label key={user.user_id} className="skill-item">
                                  <input
                                    type="checkbox"
                                    disabled={alreadyInSelectedTeam}
                                    checked={alreadyInSelectedTeam || selectedUserIds.includes(Number(user.user_id))}
                                    onChange={(event) => toggleUserSelection(Number(user.user_id), event.target.checked)}
                                  />
                                  <span>{user.full_name || user.email} (id: {user.user_id}) {alreadyInSelectedTeam ? "- already in selected team" : ""}</span>
                                  </label>
                                );
                              })}
                            </div>
                            <div className="admin-actions">
                              <button type="button" className="primary" disabled={isBusy || !hasTeamId || selectedUserIds.length === 0} onClick={() => void runAdminAction("Add users to team", addSelectedUsersToTeam)}>Add Selected Users</button>
                            </div>
                          </>
                        )}
                      </div>
                    ) : null}

                    {adminSection === "skills" ? (
                      <div className="skills-panel">
                        <h3>Team Skills Management</h3>
                        <label>
                          Search Skills
                          <input value={skillsSearch} onChange={(event) => setSkillsSearch(event.target.value)} placeholder="Filter skills by name" />
                        </label>
                        <div className="admin-actions">
                          <button type="button" className="secondary" disabled={isBusy || !hasOrgId || !hasTeamId} onClick={() => void runAdminAction("Load team skills", loadTeamSkills)}>Load Team Skills</button>
                          <button type="button" className="primary" disabled={isBusy || !hasOrgId || !hasTeamId} onClick={() => void runAdminAction("Save team skills", saveTeamSkills)}>Save Team Skills</button>
                          <button type="button" className="ghost" disabled={isBusy || !hasOrgId || !hasTeamId || filteredSkills.length === 0} onClick={selectAllVisibleSkills}>Select Visible</button>
                          <button type="button" className="ghost" disabled={isBusy || !hasOrgId || !hasTeamId || filteredSkills.length === 0} onClick={clearVisibleSkills}>Clear Visible</button>
                        </div>
                        <div className="skills-summary"><span>Catalog: {availableSkills.length}</span><span>Filtered: {filteredSkills.length}</span><span>Selected for team: {teamSelectedSkills.length}</span></div>
                        {availableSkills.length === 0 ? <div className="empty">Load skills first.</div> : (
                          <div className="skills-grid">
                            {filteredSkills.map((toolName) => (
                              <label key={toolName} className="skill-item">
                                <input type="checkbox" checked={teamSelectedSkills.includes(toolName)} onChange={(event) => toggleTeamSkill(toolName, event.target.checked)} />
                                <span>{toolName}</span>
                              </label>
                            ))}
                          </div>
                        )}
                      </div>
                    ) : null}

                    {adminSection === "users" ? (
                      <div className="skills-panel">
                        <h3>User Management</h3>
                        <label>
                          Search Users
                          <input value={usersSearch} onChange={(event) => setUsersSearch(event.target.value)} placeholder="Filter users by name, email, role or id" />
                        </label>
                        <div className="admin-actions">
                          <button type="button" className="secondary" disabled={isBusy || !hasOrgId} onClick={() => void runAdminAction("Refresh users", loadUserDirectory)}>Refresh Users</button>
                          <button type="button" className="ghost" disabled={isBusy} onClick={() => void runAdminAction("Refresh global users", loadAllUsersGlobal)}>Refresh Global</button>
                        </div>

                        <h3>Create New User</h3>
                        <div className="context-grid admin-grid">
                          <label>Email<input value={createUserForm.email} onChange={(event) => setCreateUserForm((prev) => ({ ...prev, email: event.target.value }))} /></label>
                          <label>Full Name<input value={createUserForm.full_name} onChange={(event) => setCreateUserForm((prev) => ({ ...prev, full_name: event.target.value }))} /></label>
                          <label>Title<input value={createUserForm.title} onChange={(event) => setCreateUserForm((prev) => ({ ...prev, title: event.target.value }))} /></label>
                          <label>Role<select value={createUserForm.role} onChange={(event) => setCreateUserForm((prev) => ({ ...prev, role: event.target.value }))}><option value="admin">admin</option><option value="manager">manager</option><option value="member">member</option></select></label>
                          <label>Profile Bio<textarea rows={2} value={createUserForm.profile_bio} onChange={(event) => setCreateUserForm((prev) => ({ ...prev, profile_bio: event.target.value }))} /></label>
                          <label>Initial Password (optional)<input value={createUserForm.password} onChange={(event) => setCreateUserForm((prev) => ({ ...prev, password: event.target.value }))} /></label>
                        </div>
                        <div className="admin-actions">
                          <button type="button" className="primary" disabled={isBusy || !hasOrgId} onClick={() => void runAdminAction("Create user", createUserByAdmin)}>Create User</button>
                        </div>

                        <h3>Users In Organization</h3>
                        {userDirectory.length === 0 ? <div className="empty">No users found for selected organization.</div> : (
                          <div className="skills-grid">
                            {filteredUserDirectory.map((user) => (
                              <button type="button" key={user.user_id} className="team-item" onClick={() => selectUserForEdit(user)}>
                                <strong>{user.full_name || user.email}</strong>
                                <span>{user.email} | role: {user.role} | id: {user.user_id} {user.force_password_change ? "| must change password" : ""}</span>
                              </button>
                            ))}
                          </div>
                        )}

                        <h3>All Users (Cross-Organization)</h3>
                        {globalUsers.length === 0 ? <div className="empty">No users found.</div> : (
                          <div className="skills-grid">
                            {filteredGlobalUsers.map((user) => (
                              <button type="button" key={`global-${user.user_id}`} className="team-item" onClick={() => selectUserForEdit(user)}>
                                <strong>{user.full_name || user.email}</strong>
                                <span>{user.email} | id: {user.user_id} | orgs: {Array.isArray(user.organization_ids) && user.organization_ids.length > 0 ? user.organization_ids.join(", ") : "none"}</span>
                              </button>
                            ))}
                          </div>
                        )}

                        <h3>Bulk Reassign Users Between Organizations</h3>
                        <div className="context-grid admin-grid">
                          <label>
                            Source Organization
                            <select value={bulkTransferSourceOrgId} onChange={(event) => setBulkTransferSourceOrgId(event.target.value)}>
                              <option value="">Select source</option>
                              {organizations.map((org) => (
                                <option key={`bulk-source-${org.org_id}`} value={org.org_id}>{org.org_id}</option>
                              ))}
                            </select>
                          </label>
                          <label>
                            Target Organization
                            <select value={bulkTransferTargetOrgId} onChange={(event) => setBulkTransferTargetOrgId(event.target.value)}>
                              <option value="">Select target</option>
                              {organizations.map((org) => (
                                <option key={`bulk-target-${org.org_id}`} value={org.org_id}>{org.org_id}</option>
                              ))}
                            </select>
                          </label>
                          <label>
                            Role In Target Organization
                            <select value={bulkTransferRole} onChange={(event) => setBulkTransferRole(event.target.value)}>
                              <option value="admin">admin</option>
                              <option value="manager">manager</option>
                              <option value="member">member</option>
                            </select>
                          </label>
                          <label>
                            Keep Membership In Source Organization
                            <input type="checkbox" checked={bulkKeepSourceMembership} onChange={(event) => setBulkKeepSourceMembership(event.target.checked)} />
                          </label>
                        </div>
                        <div className="admin-actions">
                          <button type="button" className="ghost" disabled={bulkVisibleUsers.length === 0} onClick={selectAllVisibleGlobalUsers}>Select Visible Users</button>
                          <button type="button" className="ghost" disabled={bulkTransferUserIds.length === 0} onClick={clearBulkUserSelection}>Clear Selection</button>
                        </div>
                        <label>
                          Show Only Users From Source Organization
                          <input
                            type="checkbox"
                            checked={bulkFilterBySourceOrg}
                            onChange={(event) => setBulkFilterBySourceOrg(event.target.checked)}
                          />
                        </label>
                        <div className="skills-summary">
                          <span>Selected users: {bulkTransferUserIds.length}</span>
                          <span>Visible users: {bulkVisibleUsers.length}</span>
                        </div>
                        {bulkVisibleUsers.length === 0 ? <div className="empty">No users for bulk selection.</div> : (
                          <div className="skills-grid">
                            {bulkVisibleUsers.map((user) => (
                              <label key={`bulk-user-${user.user_id}`} className="skill-item">
                                <input
                                  type="checkbox"
                                  checked={bulkTransferUserIds.includes(Number(user.user_id))}
                                  onChange={(event) => toggleBulkUserSelection(Number(user.user_id), event.target.checked)}
                                />
                                <span>{user.full_name || user.email} (id: {user.user_id}) | orgs: {Array.isArray(user.organization_ids) && user.organization_ids.length > 0 ? user.organization_ids.join(", ") : "none"}</span>
                              </label>
                            ))}
                          </div>
                        )}
                        <div className="admin-actions">
                          <button
                            type="button"
                            className="primary"
                            disabled={
                              isBusy ||
                              bulkTransferUserIds.length === 0 ||
                              !bulkTransferSourceOrgId ||
                              !bulkTransferTargetOrgId ||
                              bulkTransferSourceOrgId === bulkTransferTargetOrgId
                            }
                            onClick={() => void runAdminAction("Bulk reassign users between organizations", bulkReassignUsersOrganizations)}
                          >
                            Bulk Reassign
                          </button>
                        </div>

                        <h3>Edit User / Reset Password</h3>
                        <div className="context-grid admin-grid">
                          <label>User ID<input value={editUserForm.user_id} disabled /></label>
                          <label>Email<input value={editUserForm.email} onChange={(event) => setEditUserForm((prev) => ({ ...prev, email: event.target.value }))} /></label>
                          <label>Full Name<input value={editUserForm.full_name} onChange={(event) => setEditUserForm((prev) => ({ ...prev, full_name: event.target.value }))} /></label>
                          <label>Title<input value={editUserForm.title} onChange={(event) => setEditUserForm((prev) => ({ ...prev, title: event.target.value }))} /></label>
                          <label>Role<select value={editUserForm.role} onChange={(event) => setEditUserForm((prev) => ({ ...prev, role: event.target.value }))}><option value="admin">admin</option><option value="manager">manager</option><option value="member">member</option></select></label>
                          <label>Profile Bio<textarea rows={2} value={editUserForm.profile_bio} onChange={(event) => setEditUserForm((prev) => ({ ...prev, profile_bio: event.target.value }))} /></label>
                          <label>
                            Must Change Password On Next Login
                            <input
                              type="checkbox"
                              checked={Boolean(editUserForm.force_password_change)}
                              onChange={(event) => setEditUserForm((prev) => ({ ...prev, force_password_change: event.target.checked }))}
                            />
                          </label>
                          <label>
                            Organizations
                            <input value={Array.isArray(editUserForm.organization_ids) && editUserForm.organization_ids.length > 0 ? editUserForm.organization_ids.join(", ") : "none"} disabled />
                          </label>
                        </div>
                        <div className="admin-actions">
                          <button type="button" className="primary" disabled={isBusy || !editUserForm.user_id} onClick={() => void runAdminAction("Update user", updateUserProfile)}>Save User</button>
                          <button type="button" className="secondary" disabled={isBusy || !editUserForm.user_id} onClick={() => void runAdminAction("Generate one-time password", resetUserOtp)}>Generate OTP</button>
                          <button type="button" className="ghost" disabled={isBusy || !editUserForm.user_id} onClick={() => void runAdminAction("Force password reset", forcePasswordReset)}>Force Password Reset</button>
                          <button type="button" className="secondary" disabled={isBusy || !editUserForm.user_id || !hasOrgId} onClick={() => void runAdminAction("Bind user to organization", bindUserToActiveOrg)}>Bind To Active Org</button>
                          <button type="button" className="ghost" disabled={isBusy || !editUserForm.user_id || !hasOrgId} onClick={() => void runAdminAction("Unbind user from organization", unbindUserFromActiveOrg)}>Unbind From Active Org</button>
                        </div>

                        <h3>Reassign User Between Organizations</h3>
                        <div className="context-grid admin-grid">
                          <label>
                            Source Organization
                            <select
                              value={transferSourceOrgId}
                              onChange={(event) => setTransferSourceOrgId(event.target.value)}
                              disabled={!editUserForm.user_id}
                            >
                              <option value="">Select source</option>
                              {(Array.isArray(editUserForm.organization_ids) ? editUserForm.organization_ids : []).map((orgId) => (
                                <option key={`source-${orgId}`} value={orgId}>{orgId}</option>
                              ))}
                            </select>
                          </label>
                          <label>
                            Target Organization
                            <select
                              value={transferTargetOrgId}
                              onChange={(event) => setTransferTargetOrgId(event.target.value)}
                              disabled={!editUserForm.user_id}
                            >
                              <option value="">Select target</option>
                              {organizations.map((org) => (
                                <option key={`target-${org.org_id}`} value={org.org_id}>{org.org_id}</option>
                              ))}
                            </select>
                          </label>
                          <label>
                            Role In Target Organization
                            <select
                              value={transferRole}
                              onChange={(event) => setTransferRole(event.target.value)}
                              disabled={!editUserForm.user_id}
                            >
                              <option value="admin">admin</option>
                              <option value="manager">manager</option>
                              <option value="member">member</option>
                            </select>
                          </label>
                          <label>
                            Keep Membership In Source Organization
                            <input
                              type="checkbox"
                              checked={keepSourceMembership}
                              onChange={(event) => setKeepSourceMembership(event.target.checked)}
                              disabled={!editUserForm.user_id}
                            />
                          </label>
                        </div>
                        <div className="admin-actions">
                          <button
                            type="button"
                            className="primary"
                            disabled={
                              isBusy ||
                              !editUserForm.user_id ||
                              !transferSourceOrgId ||
                              !transferTargetOrgId ||
                              transferSourceOrgId === transferTargetOrgId
                            }
                            onClick={() => void runAdminAction("Reassign user between organizations", reassignUserOrganization)}
                          >
                            Reassign Organization
                          </button>
                        </div>
                        {generatedCredential ? <div className="status card">{generatedCredential}</div> : null}
                      </div>
                    ) : null}

                    <h3>Admin Activity</h3>
                    <pre className="admin-output">{adminOutput}</pre>
                  </div>
                </div>
              )}
            </section>
          </section>
        )}

        <footer className="status card reveal-up delay-2">{status}</footer>
      </main>
    </div>
  );
}

export default App;
