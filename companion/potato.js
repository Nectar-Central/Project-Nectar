
// ==UserScript==
// @name         Nectar Labs | BuildNow.gg FPS Optimizer
// @namespace    https://nectarlabs.dev/
// @version      6.0.0
// @description  Nectar Labs performance optimizer for BuildNow.gg. Hotkey: F8
// @author       Nectar Labs
// @match        https://buildnow-gg.game-files.crazygames.com/*
// @match        https://*.buildnow-gg.game-files.crazygames.com/*
// @match        https://buildnow.gg/*
// @match        https://*.buildnow.gg/*
// @grant        unsafeWindow
// @run-at       document-start
// ==/UserScript==

(() => {
    "use strict";

    /* =========================================================
       NECTAR LABS
       BUILDNOW.GG FPS OPTIMIZER
       ========================================================= */

    const NL = {
        version: "6.0.0",
        hotkey: "F8",

        // Runtime polling
        pollRate: 500,
        timeout: 60000,

        // Reapply because BuildNow can resize/reset the canvas.
        monitorRate: 1500,

        // Default mode.
        defaultPreset: "performance"
    };

    /* =========================================================
       Performance profiles
       ========================================================= */

    const PRESETS = {
        potato: {
            name: "Potato",
            subtitle: "MAXIMUM FPS",

            // Rendering resolution.
            renderScale: 0.55,

            // Unity quality.
            textureLimit: 3,
            lodBias: 0.25,
            maximumLODLevel: 2,
            antiAliasing: 0,
            pixelLightCount: 0,
            shadows: 0,
            shadowDistance: 0,
            shadowCascades: 0,
            realtimeReflectionProbes: false,
            softParticles: false,
            vSyncCount: 0
        },

        performance: {
            name: "Performance",
            subtitle: "RECOMMENDED",

            renderScale: 0.72,

            textureLimit: 2,
            lodBias: 0.5,
            maximumLODLevel: 1,
            antiAliasing: 0,
            pixelLightCount: 0,
            shadows: 0,
            shadowDistance: 0,
            shadowCascades: 0,
            realtimeReflectionProbes: false,
            softParticles: false,
            vSyncCount: 0
        },

        balanced: {
            name: "Balanced",
            subtitle: "BETTER VISUALS",

            renderScale: 0.90,

            textureLimit: 1,
            lodBias: 0.85,
            maximumLODLevel: 0,
            antiAliasing: 0,
            pixelLightCount: 1,
            shadows: 0,
            shadowDistance: 20,
            shadowCascades: 0,
            realtimeReflectionProbes: false,
            softParticles: false,
            vSyncCount: 0
        }
    };

    /* =========================================================
       State
       ========================================================= */

    let page = null;
    let plugin = null;
    let activePreset = null;
    let connected = false;
    let panel = null;
    let panelOpen = false;
    let startupTime = Date.now();

    const patchedCanvases = new WeakSet();

    /* =========================================================
       Page world
       ========================================================= */

    function getPageWindow() {
        /*
         * Tampermonkey/Violentmonkey can put a userscript in
         * an isolated world. unsafeWindow gives us the actual
         * page's globals where the Unity bridge lives.
         */
        try {
            return typeof unsafeWindow !== "undefined"
                ? unsafeWindow
                : window;
        } catch {
            return window;
        }
    }

    page = getPageWindow();

    /* =========================================================
       Logging
       ========================================================= */

    function log(...args) {
        console.log(
            "%c[Nectar Labs]%c",
            "color:#F5C542;font-weight:900",
            "color:inherit",
            ...args
        );
    }

    function warn(...args) {
        console.warn(
            "[Nectar Labs]",
            ...args
        );
    }

    /* =========================================================
       Storage
       ========================================================= */

    function loadPreset() {
        try {
            const saved = localStorage.getItem(
                "nectar-buildnow-preset"
            );

            return PRESETS[saved]
                ? saved
                : NL.defaultPreset;
        } catch {
            return NL.defaultPreset;
        }
    }

    function savePreset(name) {
        try {
            localStorage.setItem(
                "nectar-buildnow-preset",
                name
            );
        } catch {}
    }

    /* =========================================================
       BuildNow / Unity detection
       ========================================================= */

    function hasUnityRuntime() {
        if (!page) return false;

        return Boolean(
            page.UnityWebModkit ||
            page.unityGameInstance ||
            page.unityInstance ||
            page.Module
        );
    }

    /* =========================================================
       Unity bridge
       ========================================================= */

    function connectUnity() {
        if (plugin) {
            return true;
        }

        const runtime =
            page?.UnityWebModkit?.Runtime;

        if (
            !runtime ||
            typeof runtime.createPlugin !== "function"
        ) {
            return false;
        }

        try {
            plugin = runtime.createPlugin({
                name: "NectarLabsBuildNow",
                version: NL.version,

                referencedAssemblies: [
                    "mscorlib.dll",
                    "System.Runtime.InteropServices.dll",

                    "GameAssembly.dll",
                    "Assembly-CSharp.dll",

                    "UnityEngine.CoreModule.dll",
                    "UnityEngine.IMGUIModule.dll",
                    "UnityEngine.PhysicsModule.dll",
                    "UnityEngine.AnimationModule.dll"
                ]
            });

            connected = true;

            log("Unity page-world bridge connected.");

            return true;
        } catch (error) {
            warn(
                "Unity bridge failed:",
                error
            );

            return false;
        }
    }

    /* =========================================================
       Safe Unity call
       ========================================================= */

    function unityCall(method, args = []) {
        if (!plugin) {
            return false;
        }

        try {
            plugin.call(
                "UnityEngine.QualitySettings",
                method,
                args
            );

            return true;
        } catch {
            return false;
        }
    }

    function unitySet(method, value) {
        return unityCall(
            method,
            [value]
        );
    }

    /* =========================================================
       Unity quality application
       ========================================================= */

    function applyUnitySettings(preset) {
        if (!plugin) {
            return false;
        }

        let successfulCalls = 0;

        const set = (method, value) => {
            if (unitySet(method, value)) {
                successfulCalls++;
            }
        };

        /*
         * Texture quality:
         * modern Unity API first, older fallback second.
         */
        if (
            !unitySet(
                "set_globalTextureMipmapLimit",
                preset.textureLimit
            )
        ) {
            set(
                "set_masterTextureLimit",
                preset.textureLimit
            );
        } else {
            successfulCalls++;
        }

        /* LOD */
        set(
            "set_lodBias",
            preset.lodBias
        );

        set(
            "set_maximumLODLevel",
            preset.maximumLODLevel
        );

        /* Anti aliasing */
        set(
            "set_antiAliasing",
            preset.antiAliasing
        );

        /* Lighting */
        set(
            "set_pixelLightCount",
            preset.pixelLightCount
        );

        /* Shadows */
        set(
            "set_shadows",
            preset.shadows
        );

        set(
            "set_shadowDistance",
            preset.shadowDistance
        );

        set(
            "set_shadowCascades",
            preset.shadowCascades
        );

        /* Reflections */
        set(
            "set_realtimeReflectionProbes",
            preset.realtimeReflectionProbes
        );

        /* Particles */
        set(
            "set_softParticles",
            preset.softParticles
        );

        /* VSync */
        set(
            "set_vSyncCount",
            preset.vSyncCount
        );

        return successfulCalls > 0;
    }

    /* =========================================================
       Canvas discovery
       ========================================================= */

    function findUnityCanvas() {
        const canvases = [
            ...document.querySelectorAll("canvas")
        ];

        if (!canvases.length) {
            return null;
        }

        /*
         * Prefer the largest visible canvas.
         * BuildNow's Unity canvas is normally the main
         * full-game canvas.
         */

        let best = null;
        let bestArea = 0;

        for (const canvas of canvases) {
            const rect =
                canvas.getBoundingClientRect();

            const area =
                rect.width *
                rect.height;

            if (
                rect.width > 200 &&
                rect.height > 100 &&
                area > bestArea
            ) {
                best = canvas;
                bestArea = area;
            }
        }

        return best || canvases[0];
    }

    /* =========================================================
       Render resolution
       ========================================================= */

    function applyCanvasScale(preset) {
        const canvas =
            findUnityCanvas();

        if (!canvas) {
            return false;
        }

        const scale =
            Math.max(
                0.45,
                Math.min(
                    1,
                    preset.renderScale
                )
            );

        const rect =
            canvas.getBoundingClientRect();

        if (
            rect.width < 100 ||
            rect.height < 100
        ) {
            return false;
        }

        /*
         * Preserve the CSS display size while reducing
         * the actual WebGL framebuffer dimensions.
         *
         * This lowers the number of pixels Unity has to
         * render, independent of the game's own quality menu.
         */

        const targetWidth = Math.max(
            320,
            Math.round(
                rect.width *
                window.devicePixelRatio *
                scale
            )
        );

        const targetHeight = Math.max(
            180,
            Math.round(
                rect.height *
                window.devicePixelRatio *
                scale
            )
        );

        /*
         * Only write when dimensions actually change.
         * This avoids needless canvas resets.
         */

        if (
            canvas.width !== targetWidth ||
            canvas.height !== targetHeight
        ) {
            canvas.width = targetWidth;
            canvas.height = targetHeight;
        }

        /*
         * Keep the game visually filling the same area.
         */
        canvas.style.width =
            `${rect.width}px`;

        canvas.style.height =
            `${rect.height}px`;

        patchedCanvases.add(canvas);

        return true;
    }

    /* =========================================================
       Canvas protection
       ========================================================= */

    function installCanvasMonitor() {
        const canvas =
            findUnityCanvas();

        if (!canvas) {
            return;
        }

        if (
            canvas.dataset.nlResizeHook === "1"
        ) {
            return;
        }

        canvas.dataset.nlResizeHook = "1";

        /*
         * BuildNow / Unity can resize the canvas.
         * Reapply shortly after resize.
         */
        const observer =
            new ResizeObserver(() => {
                if (!activePreset) return;

                setTimeout(() => {
                    applyCanvasScale(
                        PRESETS[activePreset]
                    );
                }, 50);
            });

        observer.observe(canvas);
    }

    /* =========================================================
       Full profile application
       ========================================================= */

    function applyPreset(name, force = false) {
        const preset =
            PRESETS[name];

        if (!preset) {
            return false;
        }

        activePreset = name;

        savePreset(name);

        const unityApplied =
            applyUnitySettings(
                preset
            );

        const canvasApplied =
            applyCanvasScale(
                preset
            );

        installCanvasMonitor();

        updateUI();

        log(
            `${preset.name}:`,
            unityApplied
                ? "Unity settings OK"
                : "Unity settings unavailable",
            "|",
            canvasApplied
                ? `Render scale ${Math.round(
                    preset.renderScale * 100
                )}%`
                : "Canvas unavailable"
        );

        return (
            unityApplied ||
            canvasApplied
        );
    }

    /* =========================================================
       UI
       ========================================================= */

    const LOGO = `
        <svg
            viewBox="0 0 32 32"
            width="30"
            height="30"
        >
            <rect
                width="32"
                height="32"
                rx="9"
                fill="#F5C542"
            />

            <path
                d="M8 9h16l-3 4v10H11V13L8 9Z"
                fill="#111"
            />

            <path
                d="M12 9.5l2 4 2-4 2 4 2-4"
                fill="none"
                stroke="#F5C542"
                stroke-width="1.4"
                stroke-linecap="round"
                stroke-linejoin="round"
            />
        </svg>
    `;

    function createUI() {
        if (panel) {
            return;
        }

        if (!document.body) {
            return;
        }

        panel =
            document.createElement("div");

        panel.id =
            "nectar-labs-overlay";

        panel.innerHTML = `
            <div class="nl-top">

                <div class="nl-brand">
                    ${LOGO}

                    <div>
                        <div class="nl-name">
                            NECTAR LABS
                        </div>

                        <div class="nl-game">
                            BUILDNOW.GG
                        </div>
                    </div>
                </div>

                <button
                    id="nl-x"
                    class="nl-x"
                >
                    ×
                </button>

            </div>

            <div class="nl-rule"></div>

            <div class="nl-caption">
                PERFORMANCE PROFILE
            </div>

            <div class="nl-modes">

                <button
                    class="nl-mode"
                    data-mode="potato"
                >
                    <div>
                        <b>POTATO</b>
                        <small>MAXIMUM FPS</small>
                    </div>

                    <i>55%</i>
                </button>

                <button
                    class="nl-mode"
                    data-mode="performance"
                >
                    <div>
                        <b>PERFORMANCE</b>
                        <small>RECOMMENDED</small>
                    </div>

                    <i>72%</i>
                </button>

                <button
                    class="nl-mode"
                    data-mode="balanced"
                >
                    <div>
                        <b>BALANCED</b>
                        <small>BETTER VISUALS</small>
                    </div>

                    <i>90%</i>
                </button>

            </div>

            <div class="nl-info">

                <div>
                    <span>RUNTIME</span>
                    <strong id="nl-runtime">
                        WAITING
                    </strong>
                </div>

                <div>
                    <span>MODE</span>
                    <strong id="nl-current">
                        -
                    </strong>
                </div>

                <div>
                    <span>RENDER</span>
                    <strong id="nl-scale">
                        -
                    </strong>
                </div>

            </div>

            <div class="nl-foot">
                <span>NECTAR LABS</span>
                <span>F8</span>
                <span>V${NL.version}</span>
            </div>
        `;

        document.body.appendChild(panel);

        panel
            .querySelector("#nl-x")
            .addEventListener(
                "click",
                () => {
                    toggleUI(false);
                }
            );

        panel
            .querySelectorAll("[data-mode]")
            .forEach(button => {
                button.addEventListener(
                    "click",
                    event => {
                        event.preventDefault();
                        event.stopPropagation();

                        const mode =
                            button.dataset.mode;

                        applyPreset(mode, true);
                    }
                );
            });

        /*
         * Stop clicks from reaching the BuildNow canvas.
         */
        panel.addEventListener(
            "mousedown",
            event => {
                event.stopPropagation();
            },
            true
        );

        panel.addEventListener(
            "mouseup",
            event => {
                event.stopPropagation();
            },
            true
        );

        updateUI();
    }

    function toggleUI(force) {
        if (!panel) {
            createUI();
        }

        panelOpen =
            typeof force === "boolean"
                ? force
                : !panelOpen;

        panel.classList.toggle(
            "nl-show",
            panelOpen
        );
    }

    function updateUI() {
        if (!panel) {
            return;
        }

        const runtime =
            panel.querySelector(
                "#nl-runtime"
            );

        const current =
            panel.querySelector(
                "#nl-current"
            );

        const scale =
            panel.querySelector(
                "#nl-scale"
            );

        if (runtime) {
            runtime.textContent =
                connected
                    ? "CONNECTED"
                    : "SEARCHING";
        }

        if (current) {
            current.textContent =
                activePreset
                    ? PRESETS[
                        activePreset
                    ].name.toUpperCase()
                    : "-";
        }

        if (scale) {
            scale.textContent =
                activePreset
                    ? `${Math.round(
                        PRESETS[
                            activePreset
                        ].renderScale * 100
                    )}%`
                    : "-";
        }

        panel
            .querySelectorAll(
                "[data-mode]"
            )
            .forEach(button => {
                button.classList.toggle(
                    "selected",
                    button.dataset.mode ===
                        activePreset
                );
            });
    }

    /* =========================================================
       Styles
       ========================================================= */

    function injectCSS() {
        if (
            document.getElementById(
                "nectar-labs-css"
            )
        ) {
            return;
        }

        const style =
            document.createElement("style");

        style.id =
            "nectar-labs-css";

        style.textContent = `
            #nectar-labs-overlay {
                position: fixed;
                top: 20px;
                right: 20px;

                width: 285px;

                z-index: 2147483647;

                opacity: 0;
                visibility: hidden;
                pointer-events: none;

                transform:
                    translateY(-10px)
                    scale(.97);

                transition:
                    opacity .16s ease,
                    transform .16s ease,
                    visibility .16s ease;

                color: white;

                font-family:
                    Inter,
                    ui-sans-serif,
                    system-ui,
                    -apple-system,
                    BlinkMacSystemFont,
                    "Segoe UI",
                    sans-serif;

                border-radius: 15px;

                border:
                    1px solid
                    rgba(245,197,66,.22);

                background:
                    linear-gradient(
                        145deg,
                        rgba(17,17,19,.98),
                        rgba(7,7,9,.98)
                    );

                box-shadow:
                    0 25px 70px
                    rgba(0,0,0,.55),

                    inset 0 1px 0
                    rgba(255,255,255,.05);

                overflow: hidden;

                user-select: none;

                backdrop-filter:
                    blur(18px);
            }

            #nectar-labs-overlay.nl-show {
                opacity: 1;
                visibility: visible;
                pointer-events: auto;

                transform:
                    translateY(0)
                    scale(1);
            }

            #nectar-labs-overlay,
            #nectar-labs-overlay * {
                box-sizing: border-box;
            }

            .nl-top {
                display: flex;
                align-items: center;
                justify-content: space-between;

                padding: 13px;
            }

            .nl-brand {
                display: flex;
                align-items: center;

                gap: 9px;
            }

            .nl-name {
                color: #F5C542;

                font-size: 11px;
                font-weight: 950;

                letter-spacing: .14em;
            }

            .nl-game {
                margin-top: 2px;

                color:
                    rgba(255,255,255,.35);

                font-size: 8px;
                font-weight: 800;

                letter-spacing: .08em;
            }

            .nl-x {
                width: 27px;
                height: 27px;

                border: 0;
                border-radius: 8px;

                background:
                    rgba(255,255,255,.045);

                color:
                    rgba(255,255,255,.48);

                font-size: 17px;

                cursor: pointer;
            }

            .nl-x:hover {
                color: white;

                background:
                    rgba(255,255,255,.09);
            }

            .nl-rule {
                height: 1px;

                background:
                    rgba(255,255,255,.06);
            }

            .nl-caption {
                padding:
                    13px 13px 8px;

                color:
                    rgba(255,255,255,.28);

                font-size: 8px;
                font-weight: 900;

                letter-spacing: .12em;
            }

            .nl-modes {
                display: grid;
                gap: 5px;

                padding:
                    0 10px 11px;
            }

            .nl-mode {
                display: flex;

                align-items: center;
                justify-content:
                    space-between;

                width: 100%;

                padding:
                    10px 11px;

                border:
                    1px solid
                    rgba(255,255,255,.055);

                border-radius: 9px;

                background:
                    rgba(255,255,255,.025);

                color:
                    rgba(255,255,255,.68);

                cursor: pointer;

                text-align: left;

                transition:
                    .14s ease;
            }

            .nl-mode:hover {
                border-color:
                    rgba(245,197,66,.22);

                background:
                    rgba(245,197,66,.065);

                transform:
                    translateX(2px);
            }

            .nl-mode.selected {
                border-color:
                    rgba(245,197,66,.38);

                background:
                    rgba(245,197,66,.105);

                box-shadow:
                    inset 2px 0 0
                    #F5C542;
            }

            .nl-mode.selected b {
                color:
                    #F5C542;
            }

            .nl-mode b {
                display: block;

                font-size: 9px;
                font-weight: 900;
            }

            .nl-mode small {
                display: block;

                margin-top: 2px;

                color:
                    rgba(255,255,255,.24);

                font-size: 7px;
                font-weight: 800;

                letter-spacing: .06em;
            }

            .nl-mode i {
                color:
                    rgba(255,255,255,.20);

                font-style: normal;

                font-size: 8px;
                font-weight: 900;
            }

            .nl-info {
                display: grid;
                grid-template-columns:
                    1fr 1fr 1fr;

                gap: 6px;

                margin:
                    0 10px 10px;

                padding: 8px;

                border:
                    1px solid
                    rgba(255,255,255,.05);

                border-radius: 9px;

                background:
                    rgba(255,255,255,.025);
            }

            .nl-info div {
                min-width: 0;
            }

            .nl-info span {
                display: block;

                margin-bottom: 3px;

                color:
                    rgba(255,255,255,.25);

                font-size: 6px;
                font-weight: 900;

                letter-spacing: .08em;
            }

            .nl-info strong {
                display: block;

                overflow: hidden;

                color:
                    rgba(255,255,255,.72);

                font-size: 7px;
                font-weight: 900;

                text-overflow: ellipsis;
                white-space: nowrap;
            }

            .nl-foot {
                display: flex;

                justify-content:
                    space-between;

                padding:
                    0 13px 11px;

                color:
                    rgba(255,255,255,.18);

                font-size: 7px;
                font-weight: 800;
            }

            .nl-foot span:first-child {
                color:
                    rgba(245,197,66,.55);
            }
        `;

        document.head.appendChild(style);
    }

    /* =========================================================
       Keyboard
       ========================================================= */

    function installKeyboard() {
        document.addEventListener(
            "keydown",
            event => {
                if (
                    event.key.toUpperCase() !==
                    NL.hotkey.toUpperCase()
                ) {
                    return;
                }

                const target =
                    event.target;

                /*
                 * Don't steal F8 from an input/editor.
                 */
                if (
                    target instanceof
                        HTMLInputElement ||
                    target instanceof
                        HTMLTextAreaElement ||
                    target instanceof
                        HTMLSelectElement ||
                    target?.isContentEditable
                ) {
                    return;
                }

                event.preventDefault();
                event.stopPropagation();

                toggleUI();
            },
            true
        );
    }

    /* =========================================================
       Start
       ========================================================= */

    function start() {
        injectCSS();
        installKeyboard();

        const startPreset =
            loadPreset();

        activePreset =
            startPreset;

        createUI();

        /*
         * Start closed.
         */
        toggleUI(false);

        log(
            `Nectar Labs BuildNow.gg Optimizer v${NL.version}`
        );

        log(
            `Press ${NL.hotkey} to open the menu.`
        );

        const poll =
            setInterval(() => {

                if (connected) {
                    clearInterval(poll);
                    return;
                }

                if (
                    Date.now() -
                    startupTime >
                    NL.timeout
                ) {
                    clearInterval(poll);

                    warn(
                        "Unity runtime was not detected."
                    );

                    updateUI();

                    return;
                }

                if (!hasUnityRuntime()) {
                    return;
                }

                if (!connectUnity()) {
                    return;
                }

                applyPreset(
                    loadPreset(),
                    true
                );

                clearInterval(poll);

            }, NL.pollRate);

        /*
         * Keep BuildNow's canvas settings under control.
         */
        setInterval(() => {
            if (!connected) {
                return;
            }

            const preset =
                PRESETS[
                    activePreset ||
                    loadPreset()
                ];

            if (!preset) {
                return;
            }

            applyUnitySettings(preset);
            applyCanvasScale(preset);
            installCanvasMonitor();

            updateUI();

        }, NL.monitorRate);
    }

    /* =========================================================
       DOM ready
       ========================================================= */

    if (
        document.readyState ===
        "loading"
    ) {
        document.addEventListener(
            "DOMContentLoaded",
            start,
            { once: true }
        );
    } else {
        start();
    }

})();

