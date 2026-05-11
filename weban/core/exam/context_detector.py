class ExamContextDetector:
    """考试上下文检测与启动交互管理。"""

    def find_exam_card_index(self, page, exam_title: str) -> int:
        return page.evaluate(
            """(examTitle) => {
                function normalize(text) {
                    return (text || '').replace(/\\s+/g, '').trim();
                }

                const cards = document.querySelectorAll('.exam-item');
                const expectedTitle = normalize(examTitle);
                for (let i = 0; i < cards.length; i++) {
                    const titleEl = cards[i].querySelector('.exam-item-title, .exam-info h3');
                    const cardTitle = normalize(titleEl?.textContent || '');
                    if (
                        expectedTitle
                        && cardTitle
                        && !cardTitle.includes(expectedTitle)
                        && !expectedTitle.includes(cardTitle)
                    ) {
                        continue;
                    }
                    const btns = cards[i].querySelectorAll('button.exam-button, .exam-button, button');
                    for (const btn of btns) {
                        const txt = (btn.textContent || '').trim();
                        const r = btn.getBoundingClientRect();
                        const disabled = !!(btn.disabled || btn.getAttribute('disabled'));
                        if (txt.includes('参加考试') && r.width > 0 && r.height > 0 && !disabled) {
                            return i;
                        }
                    }
                }
                return -1;
            }""",
            exam_title,
        )

    def trigger_start_popup(self, page, vue_app_finder_js: str) -> dict:
        return page.evaluate(
            """() => {
                %s
                function isVisible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }
                function clickElement(el) {
                    if (!el || !isVisible(el)) return false;
                    el.click();
                    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    return true;
                }
                function callMethodFromProxy(proxy, methodName) {
                    if (!proxy || typeof proxy[methodName] !== 'function') return false;
                    try {
                        proxy[methodName]();
                        return true;
                    } catch (error) {
                        return false;
                    }
                }

                const popup = document.querySelector('.popup, .popup-wrapper');
                if (!isVisible(popup)) return { clicked: false, reason: 'no-popup' };

                const app = findVueProxy(['onPop']);
                if (app && callMethodFromProxy(app, 'onPop')) {
                    return { clicked: true, method: 'vue.onPop' };
                }

                const startBtn = popup.querySelector('.popup-btn');
                if (clickElement(startBtn)) {
                    return { clicked: true, method: 'dom.popup-btn' };
                }

                const popupProxy =
                    popup.__vue__
                    || popup.__vueParentComponent?.proxy
                    || startBtn?.__vue__
                    || startBtn?.__vueParentComponent?.proxy;

                if (callMethodFromProxy(popupProxy, 'onConfirm')) {
                    return { clicked: true, method: 'vue.onConfirm' };
                }

                return { clicked: false, reason: 'no-start-handler' };
            }"""
            % vue_app_finder_js
        )
