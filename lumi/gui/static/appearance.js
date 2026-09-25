/* Applies the saved theme and font size before the first paint and keeps
 * "Match system" in step with the OS.
 *
 * The server renders the saved settings into <html data-theme-setting
 * data-font-size> (gui/appearance.py): the desktop window uses private
 * browser storage and a new port on every launch, so the page cannot remember
 * them. Styles use [data-theme="light"]; dark is the default. Loaded
 * synchronously in <head>.
 *
 * The page's Content-Security-Policy refuses style="" attributes
 * (gui/local_access.py), so values that markup used to set inline are set
 * here through element.style, which the policy allows.
 */
(function () {
    'use strict';

    const root = document.documentElement;
    const fontSize = parseFloat(root.getAttribute('data-font-size'));
    if (fontSize > 0) root.style.setProperty('--text-base', `${fontSize}px`);

    // Elements the page starts with hidden are marked data-start-hidden, which
    // styles.css hides until the document is parsed. Scripts show and hide them
    // through element.style.display, and some check it for 'none', so each
    // gets the inline display: none its markup used to carry.
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[data-start-hidden]').forEach((el) => {
            el.style.display = 'none';
            el.removeAttribute('data-start-hidden');
        });
    }, {once: true});
    const THEMES = ['dark', 'light', 'system'];
    const lightQuery = typeof window.matchMedia === 'function'
        ? window.matchMedia('(prefers-color-scheme: light)')
        : null;
    let setting = normalize(root.getAttribute('data-theme-setting'));

    function normalize(value) {
        return THEMES.includes(value) ? value : 'dark';
    }

    function resolve(value) {
        if (value === 'system') return lightQuery && lightQuery.matches ? 'light' : 'dark';
        return value;
    }

    function apply() {
        const theme = resolve(setting);
        root.setAttribute('data-theme-setting', setting);
        if (theme === 'light') root.setAttribute('data-theme', 'light');
        else root.removeAttribute('data-theme');
        return theme;
    }

    function onSystemChange() {
        if (setting === 'system') apply();
    }

    if (lightQuery) {
        if (typeof lightQuery.addEventListener === 'function') lightQuery.addEventListener('change', onSystemChange);
        else if (typeof lightQuery.addListener === 'function') lightQuery.addListener(onSystemChange);
    }

    window.LumiAppearance = {
        /** Shows a theme setting (saving is the caller's job); returns "dark" or "light". */
        setTheme(value) {
            setting = normalize(value);
            return apply();
        },
        /** The saved setting: "dark", "light" or "system". */
        get setting() { return setting; },
        /** The theme on screen: "dark" or "light". */
        get theme() { return resolve(setting); },
    };

    apply();
})();
