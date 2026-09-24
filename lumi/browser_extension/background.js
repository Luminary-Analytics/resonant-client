// Puts every tab in this browser profile into one labelled, coloured tab
// group, so agent-driven browsing is visually distinct at a glance.
//
// Why an extension at all: tab groups are the `chrome.tabGroups` extension
// API. They are NOT part of the DevTools protocol — Chrome exposes 57 CDP
// domains and none of them can create or modify a group. Everything else
// Resonant's browser tools do runs over CDP with no extension involved; this
// exists solely for the grouping.
//
// Resonant writes config.js into a per-session copy of this directory before
// launching Chrome, so the label can name the run rather than being fixed at
// build time. Falling back to a constant keeps the extension loadable on its
// own.
let GROUP_TITLE = 'Resonant';
let GROUP_COLOR = 'purple';
try {
    importScripts('config.js');
    if (typeof RESONANT_GROUP_TITLE === 'string' && RESONANT_GROUP_TITLE) {
        GROUP_TITLE = RESONANT_GROUP_TITLE;
    }
    if (typeof RESONANT_GROUP_COLOR === 'string' && RESONANT_GROUP_COLOR) {
        GROUP_COLOR = RESONANT_GROUP_COLOR;
    }
} catch (e) {
    // config.js is optional.
}

// Serialize grouping work. Chrome fires onCreated for several tabs in quick
// succession during startup, and concurrent handlers each see "no group yet"
// and create their own — producing several one-tab groups with the same name
// instead of one group.
let chain = Promise.resolve();
function serialize(fn) {
    chain = chain.then(fn).catch(err => console.warn('[resonant] group failed', err));
    return chain;
}

async function groupTab(tabId) {
    let tab;
    try {
        tab = await chrome.tabs.get(tabId);
    } catch (e) {
        return;  // closed before we got to it
    }
    if (!tab || tab.groupId !== chrome.tabGroups.TAB_GROUP_ID_NONE) return;
    // Chrome cannot group a tab that is still being torn down or is in a
    // different window than the group; scope the lookup to this tab's window.
    const existing = await chrome.tabGroups.query({
        title: GROUP_TITLE,
        windowId: tab.windowId,
    });

    if (existing.length > 0) {
        await chrome.tabs.group({ tabIds: [tabId], groupId: existing[0].id });
        return;
    }
    const groupId = await chrome.tabs.group({ tabIds: [tabId] });
    await chrome.tabGroups.update(groupId, {
        title: GROUP_TITLE,
        color: GROUP_COLOR,
        collapsed: false,
    });
}

// Called by Resonant over the extension service worker's DevTools target when
// the active client session changes. Grouping is browser chrome, so unlike a
// page-injected outline it remains visible without contaminating screenshots
// or changing the page being tested.
globalThis.configureResonantGroup = async function configureResonantGroup(config = {}) {
    if (typeof config.title === 'string' && config.title.trim()) {
        GROUP_TITLE = config.title.trim();
    }
    if (typeof config.color === 'string' && config.color) {
        GROUP_COLOR = config.color;
    }

    const tabs = await chrome.tabs.query({});
    const byWindow = new Map();
    for (const tab of tabs) {
        if (tab.id == null || tab.windowId == null) continue;
        if (!byWindow.has(tab.windowId)) byWindow.set(tab.windowId, []);
        byWindow.get(tab.windowId).push(tab.id);
    }
    for (const tabIds of byWindow.values()) {
        if (!tabIds.length) continue;
        const groupId = await chrome.tabs.group({ tabIds });
        await chrome.tabGroups.update(groupId, {
            title: GROUP_TITLE,
            color: GROUP_COLOR,
            collapsed: false,
        });
    }
    return { title: GROUP_TITLE, color: GROUP_COLOR, tabs: tabs.length };
};

chrome.tabs.onCreated.addListener(tab => {
    if (tab.id != null) serialize(() => groupTab(tab.id));
});

// A tab created via CDP's Target.createTarget can surface before its window
// assignment settles, and onCreated alone then misses it. Re-check once the
// tab reports a URL.
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    if (changeInfo.status === 'loading' || changeInfo.url) {
        serialize(() => groupTab(tabId));
    }
});

// Catch anything that already existed when the extension loaded.
chrome.runtime.onInstalled.addListener(() => {
    serialize(async () => {
        const tabs = await chrome.tabs.query({});
        for (const t of tabs) {
            if (t.id != null) await groupTab(t.id);
        }
    });
});
