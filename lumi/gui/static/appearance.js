/* Applies the saved theme before the first paint and keeps "Match system"
 * in step with the OS.
 *
 * The server renders the saved setting into <html data-theme-setting>
 * (gui/appearance.py): the desktop window uses private browser storage and a
 * new port on every launch, so the page cannot remember it. Styles use
 * [data-theme="light"]; dark is the default. Loaded synchronously in <head>.
 */
(function () {
    'use strict';

    const root = document.documentElement;
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
