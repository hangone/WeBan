// ==UserScript==
// @name         安全微课｜安全微伴｜新生入学教育学习考试｜加速动画
// @namespace    https://github.com/hangone/WeBan
// @version      2026-06-15
// @description  2026最新安全微伴答题脚本，加速动画
// @author       hangyi
// @match        https://*.mycourse.cn/*
// @grant        none
// ==/UserScript==

(function () {
    'use strict';

    /** URL变化监听 **/
    function onUrlChange(callback) {
        let oldHref = location.href;
        const observer = new MutationObserver(() => {
            if (location.href !== oldHref) {
                oldHref = location.href;
                callback(location.href);
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }
function killCSSAnimations() {
        const style = document.createElement('style');
        style.type = 'text/css';
        style.innerHTML = `
            * {
                /* 强制缩短所有过渡时间 */
                -webkit-transition-duration: 0.001s !important;
                -moz-transition-duration: 0.001s !important;
                -o-transition-duration: 0.001s !important;
                transition-duration: 0.001s !important;

                /* 强制清零所有过渡延迟 */
                -webkit-transition-delay: 0s !important;
                -moz-transition-delay: 0s !important;
                -o-transition-delay: 0s !important;
                transition-delay: 0s !important;

                /* 强制缩短所有动画执行时间 */
                -webkit-animation-duration: 0.001s !important;
                -moz-animation-duration: 0.001s !important;
                -o-animation-duration: 0.001s !important;
                animation-duration: 0.001s !important;

                /* 💥关键：强制清零所有动画等待延迟（解决 17s 等待的问题） */
                -webkit-animation-delay: 0s !important;
                -moz-animation-delay: 0s !important;
                -o-animation-delay: 0s !important;
                animation-delay: 0s !important;
            }
        `;
        // 确保在 head 加载后插入，使其优先级最高
        if (document.head) {
            document.head.appendChild(style);
        } else {
            document.addEventListener('DOMContentLoaded', () => document.head.appendChild(style));
        }
        console.log("[WeBan 脚本] 🚀 CSS 动画和超长延迟已被强制清零加速！");
    }

    // 2. 如果页面使用了 jQuery 动画，可以直接关闭它
    function disableJQueryAnimations() {
        if (typeof window.jQuery !== 'undefined' && window.jQuery.fx) {
            window.jQuery.fx.off = true;
            console.log("[WeBan Script] jQuery 动画已禁用");
        }
    }

    // 3. (可选) 如果动画包含视频或音频播放，可以加速媒体元素
    function speedUpMediaElements() {
        const medias = document.querySelectorAll('video, audio');
        medias.forEach(media => {
            media.playbackRate = 16.0; // 设置为最高倍速
        });
    }

    /** 脚本主调度器 **/
    function runLogic(url) {
        if (url.startsWith('https://mcwk.mycourse.cn')) {
            console.log('[自动完成课程] 当前页面符合要求，等待 20 秒...');
            // 执行加速操作
            killCSSAnimations();
            disableJQueryAnimations();
            speedUpMediaElements();
        }

        if (url.startsWith('https://weiban.mycourse.cn/#/wk/comment')) {
            console.log('[自动课程] 进入评论页，尝试点击“返回列表”按钮...');
            clickReturnButton();
        }
    }

    /** 点击返回按钮 **/
    function clickReturnButton() {
        let attempts = 0;
        const max = 10;
        const timer = setInterval(() => {
            attempts++;
            const btns = document.querySelectorAll('.comment-footer-button');
            for (const btn of btns) {
                if (btn.textContent.includes('返回列表')) {
                    console.log('[自动课程] 点击“返回列表”按钮');
                    btn.click();
                    clearInterval(timer);
                    return;
                }
            }
            if (attempts >= max) {
                console.warn('[自动课程] 未找到返回按钮');
                clearInterval(timer);
            }
        }, 500);
    }

    // 初次执行
    runLogic(location.href);

    // 监听路由变化（适配 SPA）
    onUrlChange((newUrl) => {
        console.log('[自动课程] 路由变化:', newUrl);
        runLogic(newUrl);
    });
})();
