
// ==UserScript==
// @name         Nectar Labs | BuildNow.gg Loader
// @namespace    NectarLabs
// @version      1.0.0
// @description  Nectar Labs custom loading screen for BuildNow.gg.
// @author       Nectar Labs
// @match        https://*buildnow-gg.game-files.crazygames.com/*
// @run-at       document-start
// @inject-into  page
// @grant        none
// ==/UserScript==

(() => {
    "use strict";

    const IMAGE_URL =
        "https://media.discordapp.net/attachments/1545062188761485342/1545134252507529257/Gemini_Generated_Image_45ervp45ervp45er.png?ex=6a9b09e9&is=6a99b869&hm=8fbe9927a73a1feb53212f892da08d9cba7375da3fa9a6383721da8962bf2d51&=&format=webp&quality=lossless&width=1024&height=538";

    const DISPLAY_TIME = 8000;
    const FADE_TIME = 650;

    let screenCreated = false;

    function createLoader() {
        if (screenCreated) {
            return;
        }

        screenCreated = true;

        const style =
            document.createElement("style");

        style.textContent = `
            #nectar-labs-loader {
                position: fixed !important;
                inset: 0 !important;

                width: 100vw !important;
                height: 100vh !important;

                margin: 0 !important;
                padding: 0 !important;

                z-index: 2147483647 !important;

                display: flex !important;
                align-items: center !important;
                justify-content: center !important;

                overflow: hidden !important;

                background: #000 !important;

                opacity: 1 !important;

                visibility: visible !important;

                pointer-events: all !important;

                transition:
                    opacity ${FADE_TIME}ms ease,
                    visibility ${FADE_TIME}ms ease !important;
            }

            #nectar-labs-loader.nl-hide {
                opacity: 0 !important;
                visibility: hidden !important;
                pointer-events: none !important;
            }

            #nectar-labs-loader .nl-image {
                position: absolute !important;

                inset: 0 !important;

                width: 100% !important;
                height: 100% !important;

                object-fit: cover !important;
                object-position: center !important;

                display: block !important;

                user-select: none !important;
                pointer-events: none !important;

                -webkit-user-drag: none !important;
            }

            #nectar-labs-loader .nl-shade {
                position: absolute !important;

                inset: 0 !important;

                background:
                    linear-gradient(
                        180deg,
                        rgba(0,0,0,.02),
                        rgba(0,0,0,.10) 50%,
                        rgba(0,0,0,.40)
                    ) !important;

                pointer-events: none !important;
            }

            #nectar-labs-loader .nl-bottom {
                position: absolute !important;

                left: 50% !important;
                bottom: 7% !important;

                transform:
                    translateX(-50%) !important;

                width:
                    min(560px, 78vw) !important;

                text-align: center !important;

                font-family:
                    Arial,
                    Helvetica,
                    sans-serif !important;

                pointer-events: none !important;
            }

            #nectar-labs-loader .nl-brand {
                margin-bottom: 8px !important;

                color: white !important;

                font-size: 10px !important;
                font-weight: 900 !important;

                letter-spacing: .20em !important;

                text-shadow:
                    0 2px 12px
                    rgba(0,0,0,.75) !important;
            }

            #nectar-labs-loader .nl-brand::before {
                content: "" !important;

                display: inline-block !important;

                width: 6px !important;
                height: 6px !important;

                margin-right: 7px !important;

                vertical-align: 1px !important;

                border-radius: 50% !important;

                background: #F5C542 !important;

                box-shadow:
                    0 0 12px
                    rgba(245,197,66,.85) !important;
            }

            #nectar-labs-loader .nl-text {
                color:
                    rgba(255,255,255,.72) !important;

                font-size: 8px !important;
                font-weight: 800 !important;

                letter-spacing: .12em !important;

                text-shadow:
                    0 2px 10px
                    rgba(0,0,0,.75) !important;
            }

            #nectar-labs-loader .nl-bar {
                position: relative !important;

                width: 100% !important;
                height: 4px !important;

                margin-top: 8px !important;

                overflow: hidden !important;

                border-radius: 999px !important;

                background:
                    rgba(255,255,255,.20) !important;

                box-shadow:
                    0 2px 15px
                    rgba(0,0,0,.35) !important;
            }

            #nectar-labs-loader .nl-progress {
                width: 0% !important;
                height: 100% !important;

                border-radius: inherit !important;

                background: #F5C542 !important;

                box-shadow:
                    0 0 14px
                    rgba(245,197,66,.8) !important;

                transition:
                    width .1s linear !important;
            }
        `;

        
        (
            document.head ||
            document.documentElement
        ).appendChild(style);

        const loader =
            document.createElement("div");

        loader.id =
            "nectar-labs-loader";

        loader.innerHTML = `
            <img
                class="nl-image"
                src="${IMAGE_URL}"
                alt=""
                draggable="false"
            >

            <div class="nl-shade"></div>

            <div class="nl-bottom">

                <div class="nl-brand">
                    NECTAR LABS
                </div>

                <div class="nl-text">
                    LOADING BUILDNOW.GG
                </div>

                <div class="nl-bar">
                    <div
                        class="nl-progress"
                        id="nectar-labs-progress"
                    ></div>
                </div>

            </div>
        `;

        
        const append = () => {
            if (!document.documentElement) {
                requestAnimationFrame(append);
                return;
            }

            document.documentElement.appendChild(
                loader
            );

            startProgress(loader);
        };

        append();
    }

    function startProgress(loader) {
        const progress =
            loader.querySelector(
                "#nectar-labs-progress"
            );

        const started =
            performance.now();

        function tick(now) {
            const elapsed =
                now - started;

            const percentage =
                Math.min(
                    100,
                    (elapsed / DISPLAY_TIME) * 100
                );

            if (progress) {
                progress.style.width =
                    `${percentage}%`;
            }

            if (
                elapsed <
                DISPLAY_TIME
            ) {
                requestAnimationFrame(
                    tick
                );
            } else {
                finish(loader);
            }
        }

        requestAnimationFrame(tick);
    }

    function finish(loader) {
        const text =
            loader.querySelector(
                ".nl-text"
            );

        if (text) {
            text.textContent =
                "BUILTNOW.GG READY";
        }

        const progress =
            loader.querySelector(
                "#nectar-labs-progress"
            );

        if (progress) {
            progress.style.width =
                "100%";
        }

        setTimeout(() => {
            loader.classList.add(
                "nl-hide"
            );

            setTimeout(() => {
                loader.remove();
            }, FADE_TIME + 50);

        }, 150);
    }

    
    createLoader();

})();
