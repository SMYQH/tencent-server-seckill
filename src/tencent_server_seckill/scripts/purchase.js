
(params) => {
    const st = window.__sk || (window.__sk = {
        inflight: 0, fired: 0, done: 0, codes: {}, msgs: {},
        hit: null, fatal: null, stopped: false, last: null, rtt: null,
        regions: {}, ptr: 0, dts: [], lastFire: 0,
    });

    // 轮动抢。**注意别把这里当成结论**：quota 确实以 region_id 为键返回，各地域各有
    // 一条 available，但那个数字到底是「本地域自己的库存」还是「总池摊到本地域的数」，
    // 目前没有探明（见 _verify/quota_probe.py 的判定）。所以轮动是「不管哪种情况都不吃亏」
    // 的保守做法，不是「因为各地库存独立所以要多点开花」。
    // 具体做法：每条请求现选地域，某地域回「售罄」就把它摘掉，剩下地域继续，
    // 全摘完才判本场结束。
    const rids = (params.regions && params.regions.length)
        ? params.regions.slice()
        : [params.goods_param.regionId];

    if (params.reset) {
        st.inflight = 0; st.fired = 0; st.done = 0;
        st.codes = {}; st.msgs = {};
        st.hit = null; st.fatal = null; st.stopped = false; st.last = null;
        st.rtt = null; st.ptr = 0; st.regions = {}; st.dts = []; st.lastFire = 0;
        rids.forEach(rid => {
            st.regions[rid] = {sent: 0, done: 0, last: null, sold: false,
                               sold_at: null, codes: {}};
        });
    } else {
        rids.forEach(rid => {
            if (!st.regions[rid]) {
                st.regions[rid] = {sent: 0, done: 0, last: null, sold: false,
                                   sold_at: null, codes: {}};
            }
        });
    }
    if (params.stop) st.stopped = true;
    // 换了在途数就把往返样本清空重采：否则判断「新在途下吞吐涨没涨」时，
    // 算进去的还是旧在途时的往返，会误判成「还有余量」继续往上加。
    if (params.reset_rtt) { st.dts = []; }

    function pickRegion() {
        const alive = rids.filter(rid => st.regions[rid] && !st.regions[rid].sold);
        if (!alive.length) return null;
        const rid = alive[st.ptr % alive.length];   // 轮转，天然均摊在存活地域上
        st.ptr++;
        return rid;
    }

    let n = 0;
    // 令牌桶节流：一次性 burst 是被实战否定的行为（见 RATE_LIMIT 注释）。
    // 每次 evaluate 先按「距上次发车攒出来的时间」换算令牌数，最多攒 3 个：
    // 上限 3 是防「evaluate 被卡了几百毫秒后一口气放一大串」；探针阶段
    // rate_ms 传 0 表示不限（探针本身每 200ms 才 1 条）。
    let tokens = 1e9;
    if (params.rate_ms > 0) {
        const now = Date.now();
        if (!st.lastFire) { st.lastFire = now; tokens = 1; }
        else {
            tokens = Math.min(Math.floor((now - st.lastFire) / params.rate_ms), 3);
            if (tokens > 0) st.lastFire = now;
        }
    }
    while (n < params.batch && tokens > 0 && st.inflight < params.cap
           && !st.stopped && !st.hit && !st.fatal) {
        const rid = pickRegion();
        if (rid === null) {
            // 所有地域都售罄了。要等在飞的回来再确认，别在还有可能成功时提前收手
            if (st.inflight === 0 && !st.fatal) st.fatal = params.sold_code;
            break;
        }

        const gp = {};
        for (const k in params.goods_param) gp[k] = params.goods_param[k];
        gp.regionId = rid;

        const body = JSON.stringify({
            activity_id: params.activity_id,
            goods: [{act_id: params.act_id, type: params.type, goods_param: gp}],
            agent_channel: {fromChannel: '', fromSales: '', fromUrl: '', isAgentClient: false},
            preview: params.preview,
        });

        st.inflight++; st.fired++; n++; tokens--;
        st.regions[rid].sent++;
        const t0 = Date.now();       // 用来量往返：开窗前的提前量按这个现算
        // 超时兜底：服务端限流时会把请求 hold 住（既不给响应也不断开），
        // 而 inflight 只在 then/catch 里减 —— 没有超时的话这些请求会永久占着
        // 槽位，`inflight < cap` 再也不成立，整场就发不动了（实测空转 6 秒）。
        // 超时就 abort，让 catch 分支把 inflight 还回去，槽位继续转。
        const ctl = new AbortController();
        const tmo = setTimeout(() => { try { ctl.abort(); } catch (e) {} },
                               (params.timeout_ms || 2000));
        fetch(params.url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json',
                      'x-csrf-token': window.__latestCsrf || ''},
            credentials: 'include',
            body: body,
            signal: ctl.signal,
        }).then(r => r.json()).then(d => {
            clearTimeout(tmo);
            st.inflight--; st.done++;
            const dt = Date.now() - t0;
            if (st.rtt === null || dt < st.rtt) st.rtt = dt;
            // 同时留一份最近 N 条的样本：只记最快那条会把排队掩盖掉——
            // 服务端挂住我们的时候，最快的那条依然很快，用它算不出饱和。
            st.dts.push(dt);
            if (st.dts.length > (params.rtt_n || 20)) st.dts.shift();
            // 返回体里没有 code（被限流拦截、走了别的分支）就当没发出去，
            // 别记成 undefined，否则统计表里看不出真相
            const c = (d && d.code !== undefined && d.code !== null) ? d.code : -1;
            st.last = c;
            st.codes[c] = (st.codes[c] || 0) + 1;
            if (d && d.msg) st.msgs[c] = String(d.msg).slice(0, 400);

            const rs = st.regions[rid];
            if (rs) {
                rs.done++; rs.last = c;
                rs.codes[c] = (rs.codes[c] || 0) + 1;
                // 售罄只摘掉这个地域，不当全局致命码。
                // 记下「这个地域第一次收到售罄回包」的 JS 时间戳，收尾时比三地先后。
                // 抢完再查 quota 没意义，那时只剩全 0；这条线索只能在窗口内取。
                // 但它只是收到回包的时间，不等于服务端扣减时刻，判定只给「偏向」。
                if (c === params.sold_code && !rs.sold) {
                    rs.sold = true;
                    rs.sold_at = Date.now();
                }
            }

            if (c === params.ok && !st.hit) st.hit = d;
            if (params.norety.indexOf(c) >= 0 && !st.fatal) st.fatal = c;
        }).catch(e => {
            clearTimeout(tmo);
            st.inflight--; st.done++;
            // 把「超时主动放弃」和「请求真的没发出去」分开统计：
            // 前者是释放槽位的正常动作（服务端 hold 住我们），后者是网络/页面故障。
            // 混成一个码会让日志看不出到底是被服务端挂住还是自己网络断了。
            const aborted = !!(e && (e.name === 'AbortError'
                                     || String(e).toLowerCase().indexOf('abort') >= 0));
            const code = aborted ? -2 : -1;
            st.last = code;
            st.codes[code] = (st.codes[code] || 0) + 1;
            if (!st.msgs[code]) st.msgs[code] = aborted
                ? '请求超时被放弃（释放槽位后继续发）'
                : String((e && e.message) || e).slice(0, 60);
        });
    }
    return {fired: st.fired, inflight: st.inflight, done: st.done,
            codes: st.codes, msgs: st.msgs, hit: st.hit, fatal: st.fatal,
            last: st.last, rtt: st.rtt, dts: st.dts.slice(), regions: st.regions,
            region_count: rids.length,
            sold_count: rids.filter(r => st.regions[r] && st.regions[r].sold).length};
}
