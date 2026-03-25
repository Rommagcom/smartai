import React, { useEffect, useMemo, useRef, useState } from "react";

const DEFAULT_REGISTER_FORM = { email: "", password: "", full_name: "", title: "", profile_bio: "" };
const DEFAULT_LOGIN_FORM = { email: "", password: "" };
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

function App() {
  const [mode, setMode] = useState("login");
  const [registerForm, setRegisterForm] = useState(DEFAULT_REGISTER_FORM);
  const [loginForm, setLoginForm] = useState(DEFAULT_LOGIN_FORM);

  const [token, setToken] = useState(readStoredToken);
  const [status, setStatus] = useState("Connect to your assistant workspace and start a team conversation.");
  const [isBusy, setIsBusy] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const [isAdmin, setIsAdmin] = useState(false);
  const [isCheckingAdmin, setIsCheckingAdmin] = useState(false);
  const [activeScreen, setActiveScreen] = useState("chat");

  const [teams, setTeams] = useState([]);
  const [activeTeam, setActiveTeam] = useState(null);

  const [messages, setMessages] = useState([]);
  const [messageDraft, setMessageDraft] = useState("");
  const [pendingMessage, setPendingMessage] = useState(null);

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

  const [userDirectory, setUserDirectory] = useState([]);
  const [generatedCredential, setGeneratedCredential] = useState("");

  const [availableSkills, setAvailableSkills] = useState([]);
  const [teamAssignedSkills, setTeamAssignedSkills] = useState([]);
  const [teamSelectedSkills, setTeamSelectedSkills] = useState([]);
  const [adminOutput, setAdminOutput] = useState("No admin actions yet.");

  const messagesEndRef = useRef(null);

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

  const filteredSkills = useMemo(() => {
    const query = skillsSearch.trim().toLowerCase();
    if (!query) {
      return availableSkills;
    }
    return availableSkills.filter((toolName) => String(toolName || "").toLowerCase().includes(query));
  }, [availableSkills, skillsSearch]);

  async function request(path, options = {}, withAuth = true) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    if (withAuth && token) {
      headers.Authorization = `Bearer ${token}`;
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 20000);

    let response;
    try {
      response = await fetch(`${apiBase}${path}`, { ...options, headers, signal: controller.signal });
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

  function selectUserForEdit(user) {
    setEditUserForm({
      user_id: String(user.user_id || ""),
      email: String(user.email || ""),
      full_name: String(user.full_name || ""),
      title: String(user.title || ""),
      profile_bio: String(user.profile_bio || ""),
      role: String(user.role || "member"),
      force_password_change: Boolean(user.force_password_change),
    });
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
    await loadTeamMembersAndUsers().catch(() => null);
    return payload;
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

    setPendingMessage({
      temp_id: `pending-${Date.now()}`,
      sender_type: "user",
      sender_user_id: null,
      content: text,
      created_at: new Date().toISOString(),
      pending: true,
    });
    setIsBusy(true);
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
      setIsBusy(false);
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
  }

  useEffect(() => {
    if (!isAuthenticated || !autoRefresh || !activeTeam) {
      return undefined;
    }

    const timerId = setInterval(() => {
      void fetchMessagesFor(activeTeam, true);
    }, 7000);

    return () => clearInterval(timerId);
  }, [isAuthenticated, autoRefresh, activeTeam?.org_id, activeTeam?.team_id]);

  useEffect(() => {
    if (!isAuthenticated) {
      return;
    }
    void refreshAdminAccess();
    void loadMyTeams();
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
    void loadOrgUsersForTeams(selectedOrgId, "").catch(() => null);
  }, [selectedOrgId, isAuthenticated, isAdmin]);

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
                <>
                  <header className="chat-header">
                    <div>
                      <h1>Gemini-style Team Chat</h1>
                      <p className="subtitle">
                        {activeTeam ? `${activeTeam.team_name} (${activeTeam.org_id}/${activeTeam.team_id})` : "Select team in sidebar"}
                      </p>
                    </div>
                    <label className="toggle"><input type="checkbox" checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)} />Auto refresh</label>
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
                              <p>{item.content}</p>
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
                </>
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
                        </div>
                        <div className="admin-actions">
                          <button type="button" className="primary" disabled={isBusy || !editUserForm.user_id} onClick={() => void runAdminAction("Update user", updateUserProfile)}>Save User</button>
                          <button type="button" className="secondary" disabled={isBusy || !editUserForm.user_id} onClick={() => void runAdminAction("Generate one-time password", resetUserOtp)}>Generate OTP</button>
                          <button type="button" className="ghost" disabled={isBusy || !editUserForm.user_id} onClick={() => void runAdminAction("Force password reset", forcePasswordReset)}>Force Password Reset</button>
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
