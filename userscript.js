// ==UserScript==
// @name         安全微课｜安全微伴｜新生入学教育学习考试｜自动学习｜适配 2025 最新版
// @namespace    https://github.com/hangone/WeBan
// @version      2025-07-07
// @description  2025最新安全微伴答题脚本，适配新版验证码
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

    /** 脚本主调度器 **/
    function runLogic(url) {
        if (url.startsWith('https://mcwk.mycourse.cn')) {
            console.log('[自动完成课程] 当前页面符合要求，等待 13 秒...');
            setTimeout(() => {
                if (typeof finishWxCourse === 'function') {
                    console.log('[自动完成课程] 执行 finishWxCourse()');
                    finishWxCourse();
                } else {
                    console.warn('[自动完成课程] 未找到 finishWxCourse 函数');
                }
            }, 13000);
        }

        if (url.startsWith('https://weiban.mycourse.cn/#/wk/comment')) {
            console.log('[自动课程] 进入评论页，尝试点击“返回列表”按钮...');
            clickReturnButton();
        }

        if (url.startsWith('https://weiban.mycourse.cn/#/course')) {
            console.log('[自动课程] 进入课程页，开始查找未完成课程...');
            //findAndClickUnpassed();
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

    /** 查找并点击未完成课程 **/
    function findAndClickUnpassed() {
        let attempts = 0;
        const maxAttempts = 15;

        const interval = setInterval(() => {
            attempts++;
            if (attempts > maxAttempts) {
                console.warn('[自动课程] 未找到未完成课程，停止查找');
                clearInterval(interval);
                return;
            }

            // 遍历每个折叠项，根据完成数判断是否展开
            const collapseItems = document.querySelectorAll('.van-collapse-item');
            collapseItems.forEach(item => {
                const titleEl = item.querySelector('.van-cell__title');
                if (!titleEl) return;

                const countText = titleEl.querySelector('.count')?.innerText;
                if (!countText) return;

                const match = countText.match(/(\d+)\s*\/\s*(\d+)/);
                if (!match) return;

                const [_, doneStr, totalStr] = match;
                const done = parseInt(doneStr, 10);
                const total = parseInt(totalStr, 10);

                // 只展开未完成的章节
                if (done < total) {
                    const btn = item.querySelector('.van-collapse-item__title[aria-expanded="false"]');
                    if (btn) {
                        console.log(`[自动课程] 展开章节：${titleEl.innerText.trim()}（${done}/${total}）`);
                        btn.click();
                    }
                }
            });

            // 查找未完成的课程项
            const unpassed = [...document.querySelectorAll('.img-texts-item')].filter(
                li => !li.classList.contains('passed')
            );

            if (unpassed.length > 0) {
                console.log(`[自动课程] 找到 ${unpassed.length} 个未完成课程，点击第一个`);
                unpassed[0].click();
                clearInterval(interval);
            }
        }, 800);
    }

    // 初次执行
    runLogic(location.href);

    // 监听路由变化（适配 SPA）
    onUrlChange((newUrl) => {
        console.log('[自动课程] 路由变化:', newUrl);
        runLogic(newUrl);
    });
})();