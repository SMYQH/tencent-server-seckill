
window.__latestCsrf = null;
window.__csrfHits = 0;
(function () {
    function pick(h) {
        if (!h) return null;
        try {
            if (typeof h.get === 'function') {          // Headers 实例
                const v = h.get('x-csrf-token');
                if (v) return v;
            }
        } catch (e) {}
        try {
            if (Array.isArray(h)) {                     // [[k, v], ...]
                for (let i = 0; i < h.length; i++) {
                    const kv = h[i];
                    if (kv && String(kv[0]).toLowerCase() === 'x-csrf-token') return kv[1];
                }
                return null;
            }
            if (typeof h === 'object') {                // 普通对象
                for (const k in h) {
                    if (String(k).toLowerCase() === 'x-csrf-token') return h[k];
                }
            }
        } catch (e) {}
        return null;
    }
    window.__csrfPick = pick;

    const _fetch = window.fetch;
    window.fetch = function () {
        try {
            const v = pick(arguments[1] && arguments[1].headers);
            if (v) { window.__latestCsrf = String(v); window.__csrfHits++; }
        } catch (e) {}
        return _fetch.apply(this, arguments);
    };

    const _set = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
        try {
            if (String(k).toLowerCase() === 'x-csrf-token') {
                window.__latestCsrf = String(v); window.__csrfHits++;
            }
        } catch (e) {}
        return _set.apply(this, arguments);
    };
})();
