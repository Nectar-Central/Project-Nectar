"use strict";

const SUPABASE_URL = "https://mnpwvwjqawmuqmickteu.supabase.co";
const SUPABASE_KEY = "sb_publishable_sUIY6kgTmIly6dWg-NAiAw_FB7d5Wkv";
const API_BASE_URL = "https://api-compbng.vercel.app";

if (!window.supabase) {
    console.error("[NECTAR] Supabase library failed to load.");
} else {
    console.log("[NECTAR] Supabase library loaded.");
}

const supabaseClient = window.supabase.createClient(
    SUPABASE_URL,
    SUPABASE_KEY,
    {
        auth: {
            persistSession: true,
            autoRefreshToken: true,
            detectSessionInUrl: true,
            flowType: "pkce"
        }
    }
);

console.log("[NECTAR] Supabase client created.");


function switchPage(pageName) {
    document.querySelectorAll(".page").forEach(page => {
        page.classList.remove("active");
    });

    document.querySelectorAll(".nav-link").forEach(link => {
        link.classList.remove("active");
    });

    const page = document.getElementById(pageName);
    if (page) {
        page.classList.add("active");
    }

    const link = document.querySelector(`[data-page="${pageName}"]`);
    if (link) {
        link.classList.add("active");
    }
}

window.switchPage = switchPage;

function setupNavigation() {
    document.querySelectorAll(".nav-link").forEach(link => {
        link.addEventListener("click", event => {
            event.preventDefault();
            switchPage(link.dataset.page);
        });
    });
}

function setupProfileTabs() {
    document.querySelectorAll(".profile-tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".profile-tab-btn").forEach(b => b.classList.remove("active"));
            document.querySelectorAll(".profile-tab-content").forEach(c => c.classList.remove("active"));

            btn.classList.add("active");
            const tabId = "tab-" + btn.dataset.tab;
            const content = document.getElementById(tabId);
            if (content) {
                content.classList.add("active");
            }
        });
    });
}



async function loginWithDiscord() {
    console.log("[NECTAR] Opening Discord login...");

    const { data, error } = await supabaseClient.auth.signInWithOAuth({
        provider: "discord",
        options: {
            redirectTo: "https://nectarlabs.infy.click/"
        }
    });

    if (error) {
        console.error("[NECTAR] Discord OAuth error:", error);
        alert("Discord login failed:\n\n" + error.message);
        return;
    }

    console.log("[NECTAR] Discord OAuth started.");
}

async function logout() {
    console.log("[NECTAR] Logging out...");

    const { error } = await supabaseClient.auth.signOut();

    if (error) {
        console.error("[NECTAR] Logout error:", error);
        return;
    }

    updateLoginButton(null);
    console.log("[NECTAR] Logged out.");
}

function getDiscordData(user) {
    if (!user) return null;

    const metadata = user.user_metadata || {};

    const discordId =
        metadata.provider_id ||
        metadata.providerId ||
        metadata.sub ||
        user.identities?.find(identity => identity.provider === "discord")?.identity_data?.sub ||
        null;

    const username =
        metadata.global_name ||
        metadata.full_name ||
        metadata.name ||
        metadata.preferred_username ||
        metadata.user_name ||
        "Discord User";

    const avatar = metadata.avatar_url || metadata.picture || null;

    return {
        id: user.id,
        discordId: discordId,
        username: username,
        avatar: avatar
    };
}

function updateLoginButton(user) {
    const button = document.getElementById("discordLogin");
    const text = document.getElementById("loginText");

    if (!button || !text) return;

    if (!user) {
        button.innerHTML = `
            <img src="assets/icons/94851-login.png" alt="" width="18">
            <span id="loginText">Discord Login</span>
        `;
        button.onclick = loginWithDiscord;
        return;
    }

    const discord = getDiscordData(user);
    const avatar = discord?.avatar || "assets/icons/94851-login.png";
    const username = discord?.username || "Discord User";

    button.innerHTML = `
        <img src="${escapeHTML(avatar)}" alt="" width="28" height="28" style="width:28px; height:28px; border-radius:50%; object-fit:cover; vertical-align:middle; margin-right:8px;">
        <span id="loginText">${escapeHTML(username)}</span>
    `;

    button.onclick = logout;
}

async function saveProfile(user) {
    if (!user) return;

    const discord = getDiscordData(user);
    if (!discord) return;

    console.log("[NECTAR] Saving Discord profile...");

    const { error } = await supabaseClient.from("profiles").upsert(
        {
            id: user.id,
            discord_id: discord.discordId,
            username: discord.username,
            avatar_url: discord.avatar,
            updated_at: new Date().toISOString()
        },
        { onConflict: "id" }
    );

    if (error) {
        console.error("[NECTAR] Profile save failed:", error);
        return;
    }

    console.log("[NECTAR] Discord profile saved.");
}

/* ==========================================================================
  projects & team loading from Supabase, chris 
   ========================================================================== */

async function loadProjects() {
    const container = document.getElementById("projects-container");
    if (!container) return;

    console.log("[NECTAR] Loading projects...");

    const { data, error } = await supabaseClient.from("projects").select("*");

    if (error) {
        console.error("[NECTAR] Failed to load projects:", error);
        container.innerHTML = `
            <div class="card">
                <h3>Unable to load projects</h3>
                <p>${escapeHTML(error.message)}</p>
            </div>
        `;
        return;
    }

    if (!data || data.length === 0) {
        container.innerHTML = `
            <div class="card">
                <h3>No Projects Yet</h3>
                <p>No projects have been added to Nectar yet.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = "";

    data.forEach(project => {
        const name = firstValue(project, ["name", "title", "project_name"]) || "Unnamed Project";
        const description = firstValue(project, ["description", "desc", "details", "bio"]) || "No description available.";
        const status = firstValue(project, ["status", "state"]);
        const url = firstValue(project, ["url", "link", "website", "project_url"]);
        const image = firstValue(project, ["image_url", "image", "thumbnail", "logo"]);

        const card = document.createElement("div");
        card.className = "card";
        card.innerHTML = `
            ${image ? `<img src="${escapeHTML(image)}" alt="${escapeHTML(name)}" style="width:100%; max-height:200px; object-fit:cover; border-radius:12px; margin-bottom:15px;" onerror="this.style.display='none'">` : ""}
            <h3>${escapeHTML(name)}</h3>
            <p>${escapeHTML(description)}</p>
            ${status ? `<span class="role-badge manager">${escapeHTML(status)}</span>` : ""}
            ${url ? `<div style="margin-top:15px;"><a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer" class="secondary-btn">View Project</a></div>` : ""}
        `;

        container.appendChild(card);
    });
}

async function loadTeam() {
    const container = document.getElementById("team-container");
    if (!container) return;

    console.log("[NECTAR] Loading team...");

    const { data, error } = await supabaseClient.from("team").select("*");

    if (error) {
        console.error("[NECTAR] Failed to load team:", error);
        container.innerHTML = `
            <div class="card team-card">
                <h3>Unable to load team</h3>
                <p>${escapeHTML(error.message)}</p>
            </div>
        `;
        return;
    }

    if (!data || data.length === 0) {
        container.innerHTML = `
            <div class="card team-card">
                <h3>No Team Members</h3>
                <p>No team members have been added yet.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = "";

    data.forEach(member => {
        const name = firstValue(member, ["name", "username", "display_name", "displayName"]) || "Unknown";
        const role = firstValue(member, ["role", "position", "title"]) || "Member";
        const secondaryRole = firstValue(member, ["secondary_role", "secondaryRole", "role2"]);
        const description = firstValue(member, ["description", "bio", "details"]) || "";
        const avatar = firstValue(member, ["avatar_url", "avatar", "image_url", "image", "pfp"]);

        const card = document.createElement("div");
        card.className = "card team-card";
        card.innerHTML = `
            ${avatar ? `<img src="${escapeHTML(avatar)}" alt="${escapeHTML(name)}" style="width:75px; height:75px; border-radius:50%; object-fit:cover; margin-bottom:15px;" onerror="this.style.display='none'">` : ""}
            <h3>${escapeHTML(name)}</h3>
            <span class="role-badge ${getRoleClass(role)}">${escapeHTML(role)}</span>
            ${secondaryRole ? `<span class="role-badge ${getRoleClass(secondaryRole)}">${escapeHTML(secondaryRole)}</span>` : ""}
            <p>${escapeHTML(description)}</p>
        `;

        container.appendChild(card);
    });
}

/* ==========================================================================
   render api leaderboard data, chris baby
   ========================================================================== */

async function loadLeaderboard() {
    const tbody = document.getElementById("lb-data");
    if (!tbody) return;

    console.log("[NECTAR] Fetching leaderboard from API...");

    try {
        const response = await fetch(`${API_BASE_URL}/api/leaderboard`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        let data = await response.json();
        if (typeof data === "string") data = JSON.parse(data);

        if (!Array.isArray(data) || data.length === 0) {
            tbody.innerHTML = `<tr><td colspan="4" class="no-data">No leaderboard records found.</td></tr>`;
            return;
        }

        tbody.innerHTML = data.map((player, index) => {
            const rank = player.rank || index + 1;
            const displayName = player.game_username || player.username || player.name || player.player || "Unknown";
            const searchTag = player.game_username || player.discord_username || player.username || displayName;
            const elo = player.elo ?? player.points ?? player.score ?? "--";
            const division = player.division ?? player.tier ?? "Unranked";

            return `
                <tr>
                    <td>${escapeHTML(rank)}</td>
                    <td>
                        <a href="#" onclick="event.preventDefault(); fetchAndShowPlayer('${escapeHTML(searchTag)}');" style="color:inherit; text-decoration:underline; font-weight:600;">
                            ${escapeHTML(displayName)}
                        </a>
                    </td>
                    <td>${escapeHTML(elo)}</td>
                    <td>${escapeHTML(division)}</td>
                </tr>
            `;
        }).join("");

    } catch (error) {
        console.error("[NECTAR] Leaderboard fetch error:", error);
        tbody.innerHTML = `<tr><td colspan="4" class="no-data">Failed to load leaderboard data.</td></tr>`;
    }
}

async function fetchAndShowPlayer(query) {
    if (!query) return;

    switchPage("profile");

    const usernameEl = document.getElementById("profile-username");
    if (usernameEl) usernameEl.textContent = `Loading stats for "${query}"...`;

    try {
        const response = await fetch(`${API_BASE_URL}/api/player/${encodeURIComponent(query)}`);
        if (!response.ok) throw new Error("Player not found");

        let data = await response.json();
        if (typeof data === "string") data = JSON.parse(data);

        renderProfileData(data, query);
    } catch (error) {
        console.error("[NECTAR] Player fetch error:", error);
        if (usernameEl) usernameEl.textContent = `Player "${query}" Not Found`;
        resetProfileStats();
    }
}

function renderProfileData(data, searchedName) {
    // 1. user name and handle resolution
    const gameUsername = data.game_username || data.game_name || data.username || data.name || searchedName;
    const discordUsername = data.discord_username || data.discord_tag || data.discord || "";
    const avatar = data.avatar || data.avatar_url || "assets/images/default-avatar.png";

    const usernameEl = document.getElementById("profile-username");
    const avatarEl = document.getElementById("profile-pfp");

    if (usernameEl) {
        usernameEl.textContent = discordUsername ? `${gameUsername} (@${discordUsername})` : gameUsername;
    }
    if (avatarEl) avatarEl.src = avatar;

    // 2. Metric Parsing and Dynamic Calculations
    const kills = Number(data.kills ?? 0);
    const deaths = Number(data.deaths ?? 0);
    const wins = Number(data.wins ?? 0);
    const defeats = Number(data.defeats ?? data.losses ?? 0);
    const totalMatches = data.matches ? Number(data.matches) : (wins + defeats);

    // K/D Calculation
    let kd = data.kd ?? data.kdr;
    if (kd === undefined || kd === null) {
        kd = deaths > 0 ? (kills / deaths).toFixed(2) : kills.toFixed(2);
    }

    // Win Rate Calculation
    let winrate = data.winrate;
    if (winrate === undefined || winrate === null) {
        winrate = totalMatches > 0 ? ((wins / totalMatches) * 100).toFixed(1) : "0.0";
    }

    // 3. Inject Primary Stats into DOM
    const winrateEl = document.getElementById("stat-winrate");
    const winsEl = document.getElementById("stat-wins");
    const kdEl = document.getElementById("stat-kd");
    const killsEl = document.getElementById("stat-kills");

    if (winrateEl) winrateEl.textContent = `${winrate}%`;
    if (winsEl) winsEl.textContent = wins;
    if (kdEl) kdEl.textContent = kd;
    if (killsEl) killsEl.textContent = kills;

    // 4. Inject Recent Stats (7d / 30d) if present
    const last7 = data.last7 || {};
    const l7w = document.getElementById("last7-winrate");
    const l7wins = document.getElementById("last7-wins");
    const l7kd = document.getElementById("last7-kd");
    const l7k = document.getElementById("last7-kills");

    if (l7w) l7w.textContent = last7.winrate ? `${last7.winrate}%` : "--";
    if (l7wins) l7wins.textContent = last7.wins ?? "--";
    if (l7kd) l7kd.textContent = last7.kd ?? "--";
    if (l7k) l7k.textContent = last7.kills ?? "--";

    const last30 = data.last30 || {};
    const l30w = document.getElementById("last30-winrate");
    const l30wins = document.getElementById("last30-wins");
    const l30kd = document.getElementById("last30-kd");
    const l30k = document.getElementById("last30-kills");

    if (l30w) l30w.textContent = last30.winrate ? `${last30.winrate}%` : "--";
    if (l30wins) l30wins.textContent = last30.wins ?? "--";
    if (l30kd) l30kd.textContent = last30.kd ?? "--";
    if (l30k) l30k.textContent = last30.kills ?? "--";

    // 5. Populate Events List Tab
    const eventsList = document.getElementById("events-list");
    if (eventsList) {
        const events = data.events || data.event_results || [];
        if (!Array.isArray(events) || events.length === 0) {
            eventsList.innerHTML = `<tr><td colspan="9" class="no-data">No event statistics available.</td></tr>`;
        } else {
            eventsList.innerHTML = events.map(evt => `
                <tr>
                    <td>${escapeHTML(evt.event || evt.name || "Event")}</td>
                    <td>#${escapeHTML(evt.place || evt.rank || "--")}</td>
                    <td>${escapeHTML(evt.pr_points || evt.pr || 0)}</td>
                    <td>${escapeHTML(evt.earnings || "$0")}</td>
                    <td>${escapeHTML(evt.roster || "--")}</td>
                    <td>${escapeHTML(evt.matches || 0)}</td>
                    <td>${escapeHTML(evt.wins || 0)}</td>
                    <td>${escapeHTML(evt.elims || evt.kills || 0)}</td>
                    <td>${escapeHTML(evt.kd || "0.00")}</td>
                </tr>
            `).join("");
        }
    }

    // 6. Populate Matches List Tab
    const matchesList = document.getElementById("matches-list");
    if (matchesList) {
        const matches = data.matches_history || data.matches || [];
        if (!Array.isArray(matches) || matches.length === 0) {
            matchesList.innerHTML = `<div class="no-data">No recent matches found.</div>`;
        } else {
            matchesList.innerHTML = matches.map(m => `
                <div class="card" style="margin-bottom: 10px; display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <strong>${escapeHTML(m.mode || m.type || "Match")}</strong>
                        <p style="margin:4px 0 0; font-size:14px; opacity:0.8;">${escapeHTML(m.date || "Recently")}</p>
                    </div>
                    <div>
                        <span>Kills: <strong>${escapeHTML(m.kills ?? 0)}</strong></span> | 
                        <span>Placement: <strong>#${escapeHTML(m.placement ?? "--")}</strong></span>
                    </div>
                </div>
            `).join("");
        }
    }
}

function resetProfileStats() {
    const winrateEl = document.getElementById("stat-winrate");
    const winsEl = document.getElementById("stat-wins");
    const kdEl = document.getElementById("stat-kd");
    const killsEl = document.getElementById("stat-kills");

    if (winrateEl) winrateEl.textContent = "0.0%";
    if (winsEl) winsEl.textContent = "0";
    if (kdEl) kdEl.textContent = "0.00";
    if (killsEl) killsEl.textContent = "0";

    const eventsList = document.getElementById("events-list");
    if (eventsList) eventsList.innerHTML = `<tr><td colspan="9" class="no-data">No event statistics available.</td></tr>`;

    const matchesList = document.getElementById("matches-list");
    if (matchesList) matchesList.innerHTML = `<div class="no-data">No recent matches found.</div>`;
}

function setupPlayerSearch() {
    const input = document.getElementById("playerSearch");
    const btn = document.getElementById("searchBtn");

    const executeSearch = () => {
        const query = input?.value?.trim();
        if (query) {
            fetchAndShowPlayer(query);
        }
    };

    btn?.addEventListener("click", executeSearch);
    input?.addEventListener("keypress", event => {
        if (event.key === "Enter") executeSearch();
    });
}

/* ==========================================================================
  utility and canvas animations, chris lol, im not gay btw
   ========================================================================== */

function firstValue(object, keys) {
    for (const key of keys) {
        if (
            object &&
            object[key] !== undefined &&
            object[key] !== null &&
            String(object[key]).trim() !== ""
        ) {
            return object[key];
        }
    }
    return null;
}

function getRoleClass(role) {
    const value = String(role || "").toLowerCase();
    if (value.includes("senior")) return "senior";
    if (value.includes("founder")) return "founder";
    if (value.includes("bot")) return "bot";
    if (value.includes("web")) return "web";
    if (value.includes("manager")) return "manager";
    return "manager";
}

function escapeHTML(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function initialiseParticles() {
    const canvas = document.getElementById("particle-canvas");
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    const particles = [];

    function resize() {
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;
    }

    resize();
    window.addEventListener("resize", resize);

    for (let i = 0; i < 80; i++) {
        particles.push({
            x: Math.random() * window.innerWidth,
            y: Math.random() * window.innerHeight,
            size: Math.random() * 2 + 0.5,
            speedX: (Math.random() - 0.5) * 0.4,
            speedY: (Math.random() - 0.5) * 0.4,
            opacity: Math.random() * 0.6 + 0.2
        });
    }

    function animate() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        particles.forEach(particle => {
            particle.x += particle.speedX;
            particle.y += particle.speedY;

            if (particle.x < 0 || particle.x > canvas.width) particle.speedX *= -1;
            if (particle.y < 0 || particle.y > canvas.height) particle.speedY *= -1;

            ctx.beginPath();
            ctx.arc(particle.x, particle.y, particle.size, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(255, 190, 60, ${particle.opacity})`;
            ctx.fill();
        });

        requestAnimationFrame(animate);
    }

    animate();
}

function setupAuthListener() {
    supabaseClient.auth.onAuthStateChange(async (event, session) => {
        console.log("[NECTAR] Auth:", event);
        const user = session?.user || null;
        updateLoginButton(user);

        if (user) {
            setTimeout(() => {
                saveProfile(user);
            }, 0);
        }
    });
}

/* ==========================================================================
   application entry point, chris
   ========================================================================== */

async function startNectar() {
    console.log("[NECTAR] Starting Nectar Labs...");

    setupNavigation();
    setupProfileTabs();
    setupPlayerSearch();
    initialiseParticles();
    setupAuthListener();

    const { data, error } = await supabaseClient.auth.getSession();
    if (error) console.error("[NECTAR] Session error:", error);

    const session = data?.session || null;
    const user = session?.user || null;

    updateLoginButton(user);

    if (user) {
        console.log("[NECTAR] Logged in:", user);
        await saveProfile(user);
    } else {
        console.log("[NECTAR] No logged-in user.");
    }

    await Promise.all([
        loadProjects(),
        loadTeam(),
        loadLeaderboard()
    ]);

    const loginButton = document.getElementById("discordLogin");
    if (loginButton && !user) {
        loginButton.onclick = loginWithDiscord;
    }

    console.log("[NECTAR] Nectar Labs ready.");
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startNectar);
} else {
    startNectar();
}