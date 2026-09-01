"use strict";




const SUPABASE_URL =
    "https://aioqqkomxwepaobxqlqg.supabase.co";

const SUPABASE_KEY =
    "sb_publishable_yJyS6iWFymBfqju2_ZxCEA_acTzlRA7";


if (!window.supabase) {

    console.error(
        "[NECTAR] Supabase library failed to load."
    );

} else {

    console.log(
        "[NECTAR] Supabase library loaded."
    );

}


const supabaseClient =
    window.supabase.createClient(
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


console.log(
    "[NECTAR] Supabase client created."
);





function switchPage(pageName) {

    document
        .querySelectorAll(".page")
        .forEach(page => {

            page.classList.remove(
                "active"
            );

        });


    document
        .querySelectorAll(".nav-link")
        .forEach(link => {

            link.classList.remove(
                "active"
            );

        });


    const page =
        document.getElementById(
            pageName
        );


    if (page) {

        page.classList.add(
            "active"
        );

    }


    const link =
        document.querySelector(
            `[data-page="${pageName}"]`
        );


    if (link) {

        link.classList.add(
            "active"
        );

    }

}


window.switchPage =
    switchPage;





function setupNavigation() {

    document
        .querySelectorAll(".nav-link")
        .forEach(link => {

            link.addEventListener(
                "click",
                event => {

                    event.preventDefault();

                    switchPage(
                        link.dataset.page
                    );

                }
            );

        });

}





async function loginWithDiscord() {

    console.log(
        "[NECTAR] Opening Discord login..."
    );


    const {
        data,
        error
    } =
        await supabaseClient.auth.signInWithOAuth({

            provider: "discord",

            options: {

                redirectTo:
                    "https://nectarlabs.infy.click/"

            }

        });


    if (error) {

        console.error(
            "[NECTAR] Discord OAuth error:",
            error
        );


        alert(
            "Discord login failed:\n\n" +
            error.message
        );

        return;

    }


    console.log(
        "[NECTAR] Discord OAuth started."
    );

}





async function logout() {

    console.log(
        "[NECTAR] Logging out..."
    );


    const {
        error
    } =
        await supabaseClient.auth.signOut();


    if (error) {

        console.error(
            "[NECTAR] Logout error:",
            error
        );

        return;

    }


    updateLoginButton(
        null
    );


    console.log(
        "[NECTAR] Logged out."
    );

}





function getDiscordData(user) {

    if (!user) {
        return null;
    }


    const metadata =
        user.user_metadata || {};


    const discordId =
        metadata.provider_id ||
        metadata.providerId ||
        metadata.sub ||
        user.identities?.find(
            identity =>
                identity.provider === "discord"
        )?.identity_data?.sub ||
        null;


    const username =
        metadata.global_name ||
        metadata.full_name ||
        metadata.name ||
        metadata.preferred_username ||
        metadata.user_name ||
        "Discord User";


    const avatar =
        metadata.avatar_url ||
        metadata.picture ||
        null;


    return {

        id:
            user.id,

        discordId:
            discordId,

        username:
            username,

        avatar:
            avatar

    };

}





function updateLoginButton(user) {

    const button =
        document.getElementById(
            "discordLogin"
        );


    const text =
        document.getElementById(
            "loginText"
        );


    if (!button || !text) {
        return;
    }


    if (!user) {

        button.innerHTML = `

            <img
                src="assets/icons/94851-login.png"
                alt=""
                width="18"
            >

            <span id="loginText">
                Discord Login
            </span>

        `;


        button.onclick =
            loginWithDiscord;


        return;

    }


    const discord =
        getDiscordData(user);


    const avatar =
        discord?.avatar ||
        "assets/icons/94851-login.png";


    const username =
        discord?.username ||
        "Discord User";


    button.innerHTML = `

        <img
            src="${escapeHTML(avatar)}"
            alt=""
            width="28"
            height="28"
            style="
                width:28px;
                height:28px;
                border-radius:50%;
                object-fit:cover;
                vertical-align:middle;
                margin-right:8px;
            "
        >

        <span id="loginText">
            ${escapeHTML(username)}
        </span>

    `;


    button.onclick =
        logout;

}





async function saveProfile(user) {

    if (!user) {
        return;
    }


    const discord =
        getDiscordData(user);


    if (!discord) {
        return;
    }


    console.log(
        "[NECTAR] Saving Discord profile..."
    );


    const {
        error
    } =
        await supabaseClient
            .from("profiles")
            .upsert(
                {
                    id:
                        user.id,

                    discord_id:
                        discord.discordId,

                    username:
                        discord.username,

                    avatar_url:
                        discord.avatar,

                    updated_at:
                        new Date().toISOString()
                },
                {
                    onConflict:
                        "id"
                }
            );


    if (error) {

        console.error(
            "[NECTAR] Profile save failed:",
            error
        );

        return;

    }


    console.log(
        "[NECTAR] Discord profile saved."
    );

}





async function getCurrentUser() {

    const {
        data,
        error
    } =
        await supabaseClient.auth.getUser();


    if (error) {

        

        if (
            error.name !==
            "AuthSessionMissingError"
        ) {

            console.error(
                "[NECTAR] getUser error:",
                error
            );

        }


        return null;

    }


    return data?.user || null;

}





async function loadProjects() {

    const container =
        document.getElementById(
            "projects-container"
        );


    if (!container) {
        return;
    }


    console.log(
        "[NECTAR] Loading projects..."
    );


    const {
        data,
        error
    } =
        await supabaseClient
            .from("projects")
            .select("*");


    if (error) {

        console.error(
            "[NECTAR] Failed to load projects:",
            error
        );


        container.innerHTML = `

            <div class="card">

                <h3>
                    Unable to load projects
                </h3>

                <p>
                    ${escapeHTML(
                        error.message
                    )}
                </p>

            </div>

        `;


        return;

    }


    console.log(
        "[NECTAR] Projects:",
        data
    );


    if (!data || data.length === 0) {

        container.innerHTML = `

            <div class="card">

                <h3>
                    No Projects Yet
                </h3>

                <p>
                    No projects have been
                    added to Nectar yet.
                </p>

            </div>

        `;


        return;

    }


    container.innerHTML = "";


    data.forEach(
        project => {

            const name =
                firstValue(
                    project,
                    [
                        "name",
                        "title",
                        "project_name"
                    ]
                ) ||
                "Unnamed Project";


            const description =
                firstValue(
                    project,
                    [
                        "description",
                        "desc",
                        "details",
                        "bio"
                    ]
                ) ||
                "No description available.";


            const status =
                firstValue(
                    project,
                    [
                        "status",
                        "state"
                    ]
                );


            const url =
                firstValue(
                    project,
                    [
                        "url",
                        "link",
                        "website",
                        "project_url"
                    ]
                );


            const image =
                firstValue(
                    project,
                    [
                        "image_url",
                        "image",
                        "thumbnail",
                        "logo"
                    ]
                );


            const card =
                document.createElement(
                    "div"
                );


            card.className =
                "card";


            card.innerHTML = `

                ${
                    image
                        ? `
                            <img
                                src="${escapeHTML(image)}"
                                alt="${escapeHTML(name)}"
                                style="
                                    width:100%;
                                    max-height:200px;
                                    object-fit:cover;
                                    border-radius:12px;
                                    margin-bottom:15px;
                                "
                                onerror="
                                    this.style.display='none'
                                "
                            >
                        `
                        : ""
                }


                <h3>
                    ${escapeHTML(name)}
                </h3>


                <p>
                    ${escapeHTML(description)}
                </p>


                ${
                    status
                        ? `
                            <span class="role-badge manager">
                                ${escapeHTML(status)}
                            </span>
                        `
                        : ""
                }


                ${
                    url
                        ? `
                            <div style="margin-top:15px;">

                                <a
                                    href="${escapeHTML(url)}"
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    class="secondary-btn"
                                >
                                    View Project
                                </a>

                            </div>
                        `
                        : ""
                }

            `;


            container.appendChild(
                card
            );

        }
    );

}





async function loadTeam() {

    const container =
        document.getElementById(
            "team-container"
        );


    if (!container) {
        return;
    }


    console.log(
        "[NECTAR] Loading team..."
    );


    const {
        data,
        error
    } =
        await supabaseClient
            .from("team")
            .select("*");


    if (error) {

        console.error(
            "[NECTAR] Failed to load team:",
            error
        );


        container.innerHTML = `

            <div class="card team-card">

                <h3>
                    Unable to load team
                </h3>

                <p>
                    ${escapeHTML(
                        error.message
                    )}
                </p>

            </div>

        `;


        return;

    }


    console.log(
        "[NECTAR] Team:",
        data
    );


    if (!data || data.length === 0) {

        container.innerHTML = `

            <div class="card team-card">

                <h3>
                    No Team Members
                </h3>

                <p>
                    No team members have been
                    added yet.
                </p>

            </div>

        `;


        return;

    }


    container.innerHTML = "";


    data.forEach(
        member => {

            const name =
                firstValue(
                    member,
                    [
                        "name",
                        "username",
                        "display_name",
                        "displayName"
                    ]
                ) ||
                "Unknown";


            const role =
                firstValue(
                    member,
                    [
                        "role",
                        "position",
                        "title"
                    ]
                ) ||
                "Member";


            const secondaryRole =
                firstValue(
                    member,
                    [
                        "secondary_role",
                        "secondaryRole",
                        "role2"
                    ]
                );


            const description =
                firstValue(
                    member,
                    [
                        "description",
                        "bio",
                        "details"
                    ]
                ) ||
                "";


            const avatar =
                firstValue(
                    member,
                    [
                        "avatar_url",
                        "avatar",
                        "image_url",
                        "image",
                        "pfp"
                    ]
                );


            const card =
                document.createElement(
                    "div"
                );


            card.className =
                "card team-card";


            card.innerHTML = `

                ${
                    avatar
                        ? `
                            <img
                                src="${escapeHTML(avatar)}"
                                alt="${escapeHTML(name)}"
                                style="
                                    width:75px;
                                    height:75px;
                                    border-radius:50%;
                                    object-fit:cover;
                                    margin-bottom:15px;
                                "
                                onerror="
                                    this.style.display='none'
                                "
                            >
                        `
                        : ""
                }


                <h3>
                    ${escapeHTML(name)}
                </h3>


                <span
                    class="role-badge ${getRoleClass(role)}"
                >
                    ${escapeHTML(role)}
                </span>


                ${
                    secondaryRole
                        ? `
                            <span
                                class="role-badge ${getRoleClass(secondaryRole)}"
                            >
                                ${escapeHTML(secondaryRole)}
                            </span>
                        `
                        : ""
                }


                <p>
                    ${escapeHTML(description)}
                </p>

            `;


            container.appendChild(
                card
            );

        }
    );

}





function firstValue(object, keys) {

    for (
        const key of keys
    ) {

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

    const value =
        String(role || "")
            .toLowerCase();


    if (
        value.includes("senior")
    ) {

        return "senior";

    }


    if (
        value.includes("founder")
    ) {

        return "founder";

    }


    if (
        value.includes("bot")
    ) {

        return "bot";

    }


    if (
        value.includes("web")
    ) {

        return "web";

    }


    if (
        value.includes("manager")
    ) {

        return "manager";

    }


    return "manager";

}



function escapeHTML(value) {

    return String(
        value ?? ""
    )
        .replaceAll(
            "&",
            "&amp;"
        )
        .replaceAll(
            "<",
            "&lt;"
        )
        .replaceAll(
            ">",
            "&gt;"
        )
        .replaceAll(
            '"',
            "&quot;"
        )
        .replaceAll(
            "'",
            "&#039;"
        );

}





function initialiseParticles() {

    const canvas =
        document.getElementById(
            "particle-canvas"
        );


    if (!canvas) {
        return;
    }


    const ctx =
        canvas.getContext("2d");


    const particles = [];


    function resize() {

        canvas.width =
            window.innerWidth;

        canvas.height =
            window.innerHeight;

    }


    resize();


    window.addEventListener(
        "resize",
        resize
    );


    for (
        let i = 0;
        i < 80;
        i++
    ) {

        particles.push({

            x:
                Math.random() *
                window.innerWidth,

            y:
                Math.random() *
                window.innerHeight,

            size:
                Math.random() * 2 + 0.5,

            speedX:
                (Math.random() - 0.5) * 0.4,

            speedY:
                (Math.random() - 0.5) * 0.4,

            opacity:
                Math.random() * 0.6 + 0.2

        });

    }


    function animate() {

        ctx.clearRect(
            0,
            0,
            canvas.width,
            canvas.height
        );


        particles.forEach(
            particle => {

                particle.x +=
                    particle.speedX;

                particle.y +=
                    particle.speedY;


                if (
                    particle.x < 0 ||
                    particle.x > canvas.width
                ) {

                    particle.speedX *= -1;

                }


                if (
                    particle.y < 0 ||
                    particle.y > canvas.height
                ) {

                    particle.speedY *= -1;

                }


                ctx.beginPath();


                ctx.arc(
                    particle.x,
                    particle.y,
                    particle.size,
                    0,
                    Math.PI * 2
                );


                ctx.fillStyle =
                    `rgba(
                        255,
                        190,
                        60,
                        ${particle.opacity}
                    )`;


                ctx.fill();

            }
        );


        requestAnimationFrame(
            animate
        );

    }


    animate();

}





function setupAuthListener() {

    supabaseClient.auth.onAuthStateChange(
        async (
            event,
            session
        ) => {

            console.log(
                "[NECTAR] Auth:",
                event
            );


            const user =
                session?.user || null;


            updateLoginButton(
                user
            );


            if (user) {

              

                setTimeout(
                    () => {
                        saveProfile(user);
                    },
                    0
                );

            }

        }
    );

}





async function startNectar() {

    console.log(
        "[NECTAR] Starting Nectar Labs..."
    );


    setupNavigation();

    initialiseParticles();

    setupAuthListener();


    

    const {
        data,
        error
    } =
        await supabaseClient.auth.getSession();


    if (error) {

        console.error(
            "[NECTAR] Session error:",
            error
        );

    }


    const session =
        data?.session || null;


    const user =
        session?.user || null;


    updateLoginButton(
        user
    );


    if (user) {

        console.log(
            "[NECTAR] Logged in:",
            user
        );


        await saveProfile(
            user
        );

    } else {

        console.log(
            "[NECTAR] No logged-in user."
        );

    }


  

    await Promise.all([
        loadProjects(),
        loadTeam()
    ]);



    const loginButton =
        document.getElementById(
            "discordLogin"
        );


    if (loginButton && !user) {

        loginButton.onclick =
            loginWithDiscord;

    }


    console.log(
        "[NECTAR] Nectar Labs ready."
    );

}



if (
    document.readyState ===
    "loading"
) {

    document.addEventListener(
        "DOMContentLoaded",
        startNectar
    );

} else {

    startNectar();

}
