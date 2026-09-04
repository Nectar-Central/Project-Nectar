
// ==UserScript==
// @name         Nectar Labs | BuildNow.gg Potato Graphics
// @namespace    NectarLabs
// @version      4.0.0
// @description  Nectar Labs BuildNow.gg performance optimizer.
// @author       Nectar Labs
// @match        https://*.crazygames.com/*
// @run-at       document-start
// @grant        unsafeWindow
// ==/UserScript==

"use strict";

(() => {
    

    const win =
        typeof unsafeWindow !== "undefined"
            ? unsafeWindow
            : window;

    const CONFIG = {
        version: "4.0.0",

        textureMipmapLimit: 3,

       
        lodBias: 0.25,

        maximumLODLevel: 2,

        pixelLightCount: 0,

     
        shadowDistance: 0,
        shadowCascades: 0,

        
        antiAliasing: 0,
        vSyncCount: 0,

      
        applyInterval: 3000
    };

    let ctx = null;
    let connected = false;
    let lastApply = 0;

   

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

  

    function connect() {
        if (ctx) {
            return true;
        }

        const runtime =
            win.UnityWebModkit?.Runtime;

        if (
            !runtime ||
            typeof runtime.createPlugin !==
                "function"
        ) {
            return false;
        }

        try {
            ctx =
                runtime.createPlugin({
                    name:
                        "NectarLabsPotato",
                    version:
                        CONFIG.version,

                    referencedAssemblies: [
                        "ACTk.Runtime.dll",
                        "GameAssembly.dll",
                        "System.Runtime.InteropServices.dll",
                        "mscorlib.dll",

                        "PhotonRealtime.dll",
                        "PhotonUnityNetworking.dll",
                        "PhotonUnityNetworking.Utilities.dll",

                        "Assembly-CSharp.dll",

                        "UnityEngine.CoreModule.dll",
                        "UnityEngine.IMGUIModule.dll",
                        "UnityEngine.PhysicsModule.dll",
                        "UnityEngine.AnimationModule.dll"
                    ]
                });

            connected = true;

            log(
                "BuildNow Unity runtime connected."
            );

            return true;

        } catch (error) {
            warn(
                "Unity connection failed:",
                error
            );

            return false;
        }
    }

   

    function call(method, args = []) {
        if (!ctx) {
            return false;
        }

        try {
            ctx.call(
                "UnityEngine.QualitySettings",
                method,
                args
            );

            return true;

        } catch {
            return false;
        }
    }

    function set(method, value) {
        return call(
            method,
            [value]
        );
    }



    function applyPotato() {
        if (!ctx) {
            return false;
        }

        let successful = 0;

        const safeSet = (
            method,
            value
        ) => {
            if (
                set(
                    method,
                    value
                )
            ) {
                successful++;
            }
        };

       
        safeSet(
            "set_globalTextureMipmapLimit",
            CONFIG.textureMipmapLimit
        );

      
        safeSet(
            "set_masterTextureLimit",
            CONFIG.textureMipmapLimit
        );

       
        safeSet(
            "set_lodBias",
            CONFIG.lodBias
        );

        safeSet(
            "set_maximumLODLevel",
            CONFIG.maximumLODLevel
        );

       
        safeSet(
            "set_antiAliasing",
            CONFIG.antiAliasing
        );

      
        safeSet(
            "set_pixelLightCount",
            CONFIG.pixelLightCount
        );

       
        safeSet(
            "set_shadows",
            0
        );

        safeSet(
            "set_shadowDistance",
            CONFIG.shadowDistance
        );

        safeSet(
            "set_shadowCascades",
            CONFIG.shadowCascades
        );

       
        safeSet(
            "set_shadowResolution",
            0
        );

       
        safeSet(
            "set_realtimeReflectionProbes",
            false
        );

       
        safeSet(
            "set_softParticles",
            false
        );

     
        safeSet(
            "set_vSyncCount",
            CONFIG.vSyncCount
        );

        
        safeSet(
            "set_enableLODCrossFade",
            false
        );

      
        safeSet(
            "set_anisotropicFiltering",
            0
        );

        lastApply =
            Date.now();

        return successful > 0;
    }

  

    function unityExists() {
        return Boolean(
            win.unityGameInstance ||
            win.unityInstance ||
            win.UnityWebModkit?.Runtime ||
            win.Module
        );
    }



    function attemptStartup() {
        if (connected) {
            return true;
        }

        if (!unityExists()) {
            return false;
        }

        if (!connect()) {
            return false;
        }

        applyPotato();

        log(
            "Nectar Labs potato graphics enabled."
        );

        return true;
    }



    function startWatcher() {
        const started =
            Date.now();

        const timeout =
            60000;

        const timer =
            setInterval(() => {

                /*
                 * Keep trying until BuildNow has finished
                 * creating its Unity bridge.
                 */
                if (
                    attemptStartup()
                ) {
                    clearInterval(
                        timer
                    );

                    return;
                }

                if (
                    Date.now() -
                    started >
                    timeout
                ) {
                    clearInterval(
                        timer
                    );

                    warn(
                        "BuildNow Unity runtime was not detected."
                    );
                }

            }, 250);
    }



    function startReapplyLoop() {
        setInterval(() => {
            if (!connected) {
                return;
            }

            /*
             * Avoid needless calls if the timer fires too
             * quickly due to browser scheduling.
             */
            if (
                Date.now() -
                lastApply <
                CONFIG.applyInterval
            ) {
                return;
            }

            applyPotato();

        }, CONFIG.applyInterval);
    }

    

    win.NectarLabsPotato = {
        version:
            CONFIG.version,

        apply() {
            if (!connected) {
                if (!attemptStartup()) {
                    warn(
                        "BuildNow runtime isn't ready yet."
                    );

                    return false;
                }
            }

            return applyPotato();
        },

        get connected() {
            return connected;
        }
    };

    /* =========================================================
       Start
       ========================================================= */

    log(
        `Nectar Labs BuildNow Potato Graphics v${CONFIG.version}`
    );

    log(
        "Waiting for BuildNow..."
    );

    startWatcher();
    startReapplyLoop();

})();
