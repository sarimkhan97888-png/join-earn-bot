'use strict';
/*
 * Profitix Ghost - WhatsApp bridge (Baileys, multi-session, Pro plan).
 * Started and supervised by worker.py ONLY while that process holds the engine lease,
 * so two bridges can never reply to the same message.
 *
 * Talks to the Mini App through Neon:
 *   wa_commands  (Render -> bridge)   'pair' {phone} | 'logout'
 *   wa_sessions  (bridge -> Mini App) status, pairing_code, number, error
 *   wa_auth      Baileys credentials/keys, so sessions survive restarts and Kaggle reboots
 */
const crypto = require('crypto');
const dns = require('dns').promises;
const net = require('net');
const { Pool } = require('pg');
const pino = require('pino');
const {
  default: makeWASocket,
  DisconnectReason,
  BufferJSON,
  initAuthCreds,
  proto,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  Browsers,
} = require('@whiskeysockets/baileys');

const GRACE_MS = (Number(process.env.OFFLINE_GRACE_SECONDS) || 8) * 1000;
const ACTIVE_MS = (Number(process.env.OWNER_ACTIVE_WINDOW_SECONDS) || 90) * 1000;
const logger = pino({ level: process.env.WA_LOG_LEVEL || 'warn' });
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const logErr = (e) => console.error('ERR', e && e.stack ? e.stack.split('\n').slice(0, 3).join(' | ') : e);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

if (!process.env.DATABASE_URL) {
  console.error('DATABASE_URL is missing');
  process.exit(1);
}
const pool = new Pool({ connectionString: process.env.DATABASE_URL.split('?')[0], ssl: true, max: 6 });

const sessions = new Map(); // userId -> ctx

/* ------------------------------------------------------------------ small DB helpers */
async function setSession(userId, fields) {
  const keys = Object.keys(fields);
  const cols = keys.map((k, i) => `${k} = $${i + 2}`).join(', ');
  await pool.query('INSERT INTO wa_sessions (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING', [userId]);
  await pool.query(`UPDATE wa_sessions SET ${cols}, updated_at = NOW() WHERE user_id = $1`, [userId, ...keys.map((k) => fields[k])]);
}

async function logActivity(userId, kind) {
  try {
    await pool.query('INSERT INTO activity_log (user_id, kind, meta) VALUES ($1, $2, $3)', [userId, kind, {}]);
  } catch (e) { /* activity log is best effort */ }
}

const diagLast = new Map();
function decide(userId, outcome, reason) { // feeds the "Ghost activity" card in the Mini App
  const key = `${userId}:${reason}`;
  const now = Date.now();
  if (outcome !== 'sent' && now - (diagLast.get(key) || 0) < 30000) return;
  diagLast.set(key, now);
  pool.query("INSERT INTO reply_log (user_id, outcome, reason, platform) VALUES ($1, $2, $3, 'whatsapp')",
    [userId, outcome, String(reason).slice(0, 80)]).catch(() => {});
}

const purgeAuth = (userId) => pool.query('DELETE FROM wa_auth WHERE user_id = $1', [userId]);

/* Baileys auth state stored in Postgres (same layout as useMultiFileAuthState) */
async function usePgAuthState(userId) {
  const read = async (key) => {
    const r = await pool.query('SELECT value FROM wa_auth WHERE user_id = $1 AND key = $2', [userId, key]);
    return r.rows[0] ? JSON.parse(r.rows[0].value, BufferJSON.reviver) : null;
  };
  const write = (key, data) => pool.query(
    'INSERT INTO wa_auth (user_id, key, value) VALUES ($1, $2, $3) ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value',
    [userId, key, JSON.stringify(data, BufferJSON.replacer)],
  );
  const del = (key) => pool.query('DELETE FROM wa_auth WHERE user_id = $1 AND key = $2', [userId, key]);

  const creds = (await read('creds')) || initAuthCreds();
  return {
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const data = {};
          await Promise.all(ids.map(async (id) => {
            let value = await read(`${type}-${id}`);
            if (type === 'app-state-sync-key' && value) value = proto.Message.AppStateSyncKeyData.fromObject(value);
            data[id] = value;
          }));
          return data;
        },
        set: async (data) => {
          const tasks = [];
          for (const type of Object.keys(data)) {
            for (const id of Object.keys(data[type])) {
              const value = data[type][id];
              tasks.push(value ? write(`${type}-${id}`, value) : del(`${type}-${id}`));
            }
          }
          await Promise.all(tasks);
        },
      },
    },
    saveCreds: () => write('creds', creds),
  };
}

/* Plan + settings + webhook config in one cached query */
const stateCache = new Map();
async function getUserState(userId, force = false) {
  const hit = stateCache.get(userId);
  if (!force && hit && Date.now() - hit.ts < 15000) return hit.data;
  const { rows } = await pool.query(
    `SELECT (u.plan = 'pro' AND u.plan_status = 'active' AND u.plan_expires_at > NOW()) AS pro_active,
            COALESCE(a.is_active, TRUE) AS is_active,
            COALESCE(a.default_reply, '') AS default_reply,
            COALESCE(a.keyword_rules, '[]'::jsonb) AS keyword_rules,
            COALESCE(a.cooldown_seconds, 600) AS cooldown_seconds,
            COALESCE(a.delay_min, 2) AS delay_min,
            COALESCE(a.delay_max, 5) AS delay_max,
            COALESCE(a.only_when_offline, TRUE) AS only_when_offline,
            i.webhook_url, i.webhook_secret,
            COALESCE(i.webhook_enabled, FALSE) AS webhook_enabled,
            COALESCE(i.webhook_reply, FALSE) AS webhook_reply
     FROM users u
     LEFT JOIN auto_reply_settings a ON a.user_id = u.telegram_user_id
     LEFT JOIN integrations i ON i.user_id = u.telegram_user_id
     WHERE u.telegram_user_id = $1`,
    [userId],
  );
  const data = rows[0] || null;
  stateCache.set(userId, { ts: Date.now(), data });
  return data;
}

/* ------------------------------------------------------------------ webhooks (same contract as webhooks.py) */
function isBlockedIp(ip) {
  if (net.isIPv6(ip)) {
    const l = ip.toLowerCase();
    if (l.startsWith('::ffff:')) return isBlockedIp(l.slice(7));
    return l === '::1' || l === '::' || l.startsWith('fc') || l.startsWith('fd') || l.startsWith('fe80');
  }
  const p = ip.split('.').map(Number);
  return p[0] === 10 || p[0] === 127 || p[0] === 0 || (p[0] === 169 && p[1] === 254)
    || (p[0] === 172 && p[1] >= 16 && p[1] <= 31) || (p[0] === 192 && p[1] === 168)
    || (p[0] === 100 && p[1] >= 64 && p[1] <= 127) || p[0] >= 224;
}

async function isSafeUrl(raw) {
  let url;
  try { url = new URL(raw); } catch (e) { return false; }
  if (!['http:', 'https:'].includes(url.protocol)) return false;
  const host = url.hostname.replace(/^\[|\]$/g, '');
  try {
    const addrs = net.isIP(host) ? [{ address: host }] : await dns.lookup(host, { all: true });
    return addrs.length > 0 && addrs.every((a) => !isBlockedIp(a.address));
  } catch (e) { return false; }
}

async function recordHook(userId, ok, status, error) {
  try {
    await pool.query(
      `UPDATE integrations SET delivered = delivered + $2, failed = failed + $3,
              last_status = $4, last_error = $5, last_at = NOW() WHERE user_id = $1`,
      [userId, ok ? 1 : 0, ok ? 0 : 1, status, String(error || '').slice(0, 200)],
    );
  } catch (e) { /* best effort */ }
}

async function deliverWebhook(userId, st, event) {
  try {
    if (!(await isSafeUrl(st.webhook_url))) { await recordHook(userId, false, null, 'URL not allowed'); return null; }
    const body = JSON.stringify(event);
    const headers = {
      'Content-Type': 'application/json',
      'User-Agent': 'ProfitixGhost/1.0',
      'X-Ghost-Event': event.event,
      'X-Ghost-Timestamp': String(Math.floor(Date.now() / 1000)),
    };
    if (st.webhook_secret) headers['X-Ghost-Signature'] = 'sha256=' + crypto.createHmac('sha256', st.webhook_secret).update(body).digest('hex');
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    let resp;
    try { resp = await fetch(st.webhook_url, { method: 'POST', headers, body, redirect: 'manual', signal: ctrl.signal }); } finally { clearTimeout(timer); }
    const ok = resp.status >= 200 && resp.status < 300;
    await recordHook(userId, ok, resp.status, ok ? '' : `HTTP ${resp.status}`);
    if (!ok || !(resp.headers.get('content-type') || '').includes('json')) return null;
    const text = (await resp.text()).slice(0, 65536);
    try { const j = JSON.parse(text); return j && typeof j === 'object' ? j : null; } catch (e) { return null; }
  } catch (e) {
    await recordHook(userId, false, null, e.name || 'error');
    return null;
  }
}

/* ------------------------------------------------------------------ reply logic */
let active = 0;
const waiters = [];
async function limiter(fn) { // at most 10 concurrent sends
  if (active >= 10) await new Promise((r) => waiters.push(r));
  active += 1;
  try { return await fn(); } finally { active -= 1; const next = waiters.shift(); if (next) next(); }
}

function extractText(message) {
  const m = (message.ephemeralMessage && message.ephemeralMessage.message)
    || (message.viewOnceMessage && message.viewOnceMessage.message) || message;
  return m.conversation
    || (m.extendedTextMessage && m.extendedTextMessage.text)
    || (m.imageMessage && m.imageMessage.caption)
    || (m.videoMessage && m.videoMessage.caption)
    || (m.documentMessage && m.documentMessage.caption)
    || (m.buttonsResponseMessage && m.buttonsResponseMessage.selectedDisplayText)
    || (m.listResponseMessage && m.listResponseMessage.title) || '';
}

function pickReply(st, text) {
  const lowered = (text || '').toLowerCase();
  for (const rule of st.keyword_rules || []) {
    const kw = String(rule.keyword || '').trim().toLowerCase();
    const reply = String(rule.reply || '').trim();
    if (kw && reply && lowered.includes(kw)) return reply;
  }
  return String(st.default_reply || '').trim();
}

const ownerBlock = (ctx, jid, since) => {
  if ((ctx.replied.get(jid) || 0) >= since) return 'owner_replied';
  if (Date.now() - ctx.ownerActive < ACTIVE_MS) return 'owner_active';
  return null;
};
const ownerBusy = (ctx, jid, since) => !!ownerBlock(ctx, jid, since);

async function handleMessage(ctx, m) {
  if (!m.message || !m.key) return;
  const jid = m.key.remoteJid || '';
  if (!jid || jid.endsWith('@g.us') || jid.endsWith('@broadcast') || jid.endsWith('@newsletter')) return;
  const now = Date.now();

  if (m.key.fromMe) { // the owner typed on their phone (our own replies are excluded)
    if (now < (ctx.sending.get(jid) || 0)) return;
    ctx.ownerActive = now;
    ctx.replied.set(jid, now);
    return;
  }
  const ts = Number(m.messageTimestamp || 0) * 1000;
  if (ts && now - ts > 120000) return; // offline backlog, not a fresh message

  const st = await getUserState(ctx.userId);
  if (!st || !st.pro_active) return;
  const text = extractText(m.message);

  let hook = null;
  if (st.webhook_enabled && st.webhook_url) {
    hook = deliverWebhook(ctx.userId, st, {
      event: 'message.incoming',
      platform: 'whatsapp',
      user_id: ctx.userId,
      message: { id: m.key.id, text: text.slice(0, 4000), chat_id: jid, date: new Date().toISOString() },
      from: { id: jid.split('@')[0], name: m.pushName || null, username: null },
    });
  }
  const hookDecides = !!(hook && st.webhook_reply);

  if (!st.is_active) { decide(ctx.userId, 'skipped', 'paused'); return; }
  if (now - (ctx.lastReply.get(jid) || 0) < (Number(st.cooldown_seconds) || 600) * 1000) { decide(ctx.userId, 'skipped', 'cooldown'); return; }
  const onlyOffline = st.only_when_offline !== false;
  const why = onlyOffline ? ownerBlock(ctx, jid, now) : null;
  if (why) { decide(ctx.userId, 'skipped', why); return; }

  const reply = pickReply(st, text);
  if (!reply && !hookDecides) return;
  ctx.lastReply.set(jid, now);
  sendReply(ctx, jid, now, reply, st, onlyOffline, hookDecides ? hook : null).catch(logErr);
}

async function sendReply(ctx, jid, incomingTs, reply, st, onlyOffline, hookPromise) {
  const release = () => ctx.lastReply.delete(jid);
  try {
    if (onlyOffline) {
      await sleep(GRACE_MS); // give the owner a chance to answer first
      const why = ownerBlock(ctx, jid, incomingTs);
      if (why) { decide(ctx.userId, 'skipped', why); return release(); }
    }
    if (hookPromise) {
      const answer = await Promise.race([hookPromise, sleep(8000).then(() => null)]);
      if (answer) {
        if (answer.skip) return release();
        if (typeof answer.reply === 'string' && answer.reply.trim()) reply = answer.reply.trim().slice(0, 1000);
      }
    }
    if (!reply || ctx.closed || !ctx.sock) return release();
    const sock = ctx.sock;
    await limiter(async () => {
      const lo = Math.max(1, Math.min(Number(st.delay_min) || 2, 30));
      const hi = Math.max(lo, Math.min(Number(st.delay_max) || 5, 30));
      const wait = (lo + Math.random() * (hi - lo)) * 1000;
      await sock.presenceSubscribe(jid).catch(() => {});
      await sock.sendPresenceUpdate('composing', jid).catch(() => {}); // human typing simulation
      await sleep(wait);
      const why2 = onlyOffline ? ownerBlock(ctx, jid, incomingTs) : null;
      if (why2) {
        decide(ctx.userId, 'skipped', why2);
        release();
        await sock.sendPresenceUpdate('paused', jid).catch(() => {});
        return;
      }
      ctx.sending.set(jid, Date.now() + 30000);
      await sock.sendMessage(jid, { text: reply });
      await sock.sendPresenceUpdate('paused', jid).catch(() => {});
      ctx.sending.set(jid, Date.now() + 5000);
      decide(ctx.userId, 'sent', 'sent');
      await pool.query('UPDATE users SET total_replies_sent = total_replies_sent + 1 WHERE telegram_user_id = $1', [ctx.userId]);
    });
  } catch (e) { release(); decide(ctx.userId, 'error', (e && e.message) || 'error'); logErr(e); }
}

/* ------------------------------------------------------------------ sessions */
async function onConnection(ctx, sock, update) {
  if (ctx.sock !== sock || ctx.closed) return;
  const { connection, lastDisconnect } = update;

  if (connection === 'open') {
    ctx.retry = 0;
    ctx.paired = true;
    clearTimeout(ctx.pairTimer);
    const me = sock.user || {};
    await setSession(ctx.userId, {
      status: 'connected', pairing_code: null, error: null,
      wa_number: String(me.id || '').split(':')[0].split('@')[0], wa_name: me.name || '', connected_at: new Date(),
    });
    await logActivity(ctx.userId, 'wa_connected');
    log('WhatsApp connected for user', ctx.userId);
    return;
  }

  if (connection === 'close') {
    const code = lastDisconnect && lastDisconnect.error && lastDisconnect.error.output && lastDisconnect.error.output.statusCode;
    if (code === DisconnectReason.loggedOut) { // removed from the phone's Linked devices
      ctx.closed = true;
      sessions.delete(ctx.userId);
      await purgeAuth(ctx.userId);
      await setSession(ctx.userId, { status: 'none', pairing_code: null, wa_number: null, wa_name: null, error: 'WhatsApp was unlinked from your phone.' });
      await logActivity(ctx.userId, 'wa_disconnected');
      return;
    }
    ctx.retry += 1; // 515 restartRequired is normal right after pairing
    const wait = code === DisconnectReason.restartRequired ? 500 : Math.min(30000, 2000 * ctx.retry);
    setTimeout(() => { if (!ctx.closed) connect(ctx).catch(logErr); }, wait);
  }
}

async function connect(ctx) {
  const old = ctx.sock;
  ctx.sock = null;
  if (old) { try { old.end(undefined); } catch (e) { /* ignore */ } }

  const { state, saveCreds } = await usePgAuthState(ctx.userId);
  ctx.paired = !!state.creds.registered;
  let version;
  try { ({ version } = await fetchLatestBaileysVersion()); } catch (e) { version = undefined; }

  const sock = makeWASocket({
    ...(version ? { version } : {}),
    auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, logger) },
    logger,
    browser: Browsers.ubuntu('Chrome'),
    printQRInTerminal: false,
    markOnlineOnConnect: false, // keep phone notifications + honest "offline" detection
    syncFullHistory: false,
    generateHighQualityLinkPreview: false,
    getMessage: async () => undefined,
  });
  ctx.sock = sock;

  sock.ev.on('creds.update', saveCreds);
  sock.ev.on('connection.update', (u) => onConnection(ctx, sock, u).catch(logErr));
  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (ctx.sock !== sock || ctx.closed || type !== 'notify') return;
    for (const m of messages) await handleMessage(ctx, m).catch(logErr);
  });

  if (!state.creds.registered && ctx.pairPhone) {
    setTimeout(async () => {
      try {
        if (ctx.closed || ctx.sock !== sock) return;
        const code = await sock.requestPairingCode(ctx.pairPhone);
        await setSession(ctx.userId, { status: 'pairing', pairing_code: code, error: null });
        log('Pairing code issued for user', ctx.userId);
      } catch (e) {
        await setSession(ctx.userId, { status: 'none', pairing_code: null, error: 'Could not get a pairing code. Check the number and try again.' });
        await stopSession(ctx.userId, false);
        logErr(e);
      }
    }, 3000);
    clearTimeout(ctx.pairTimer);
    ctx.pairTimer = setTimeout(async () => {
      if (ctx.paired || ctx.closed) return;
      await setSession(ctx.userId, { status: 'none', pairing_code: null, error: 'Pairing timed out. Please try again.' });
      await stopSession(ctx.userId, false);
    }, 180000);
  }
}

async function startSession(userId, pairPhone = null) {
  await stopSession(userId, false);
  const ctx = {
    userId, sock: null, paired: false, closed: false, retry: 0, pairPhone,
    ownerActive: 0, replied: new Map(), sending: new Map(), lastReply: new Map(), pairTimer: null,
  };
  sessions.set(userId, ctx);
  await connect(ctx);
}

async function stopSession(userId, logout) {
  const ctx = sessions.get(userId);
  if (!ctx) return;
  ctx.closed = true;
  clearTimeout(ctx.pairTimer);
  sessions.delete(userId);
  const sock = ctx.sock;
  ctx.sock = null;
  if (sock) {
    if (logout) { try { await sock.logout(); } catch (e) { /* already gone */ } }
    try { sock.end(undefined); } catch (e) { /* ignore */ }
  }
}

/* ------------------------------------------------------------------ command queue + sync loops */
async function runCommand(c) {
  const userId = Number(c.user_id);
  if (c.cmd === 'pair') {
    const phone = String((c.payload || {}).phone || '').replace(/\D/g, '');
    const st = await getUserState(userId, true);
    if (!st || !st.pro_active) { await setSession(userId, { status: 'none', pairing_code: null, error: 'WhatsApp needs an active Pro plan.' }); return; }
    if (phone.length < 8 || phone.length > 15) { await setSession(userId, { status: 'none', pairing_code: null, error: 'Invalid phone number.' }); return; }
    await purgeAuth(userId); // always a fresh pairing
    await setSession(userId, { status: 'pairing', phone, pairing_code: null, error: null, wa_number: null, wa_name: null });
    await startSession(userId, phone);
  } else if (c.cmd === 'logout') {
    await stopSession(userId, true);
    await purgeAuth(userId);
    await setSession(userId, { status: 'none', pairing_code: null, wa_number: null, wa_name: null, error: null });
    await logActivity(userId, 'wa_disconnected');
  }
}

async function commandLoop() {
  for (;;) {
    try {
      const { rows } = await pool.query(
        `UPDATE wa_commands SET status = 'done'
         WHERE id IN (SELECT id FROM wa_commands
                      WHERE status = 'pending' AND created_at > NOW() - INTERVAL '10 minutes'
                      ORDER BY id LIMIT 10 FOR UPDATE SKIP LOCKED)
         RETURNING id, user_id, cmd, payload`,
      );
      for (const c of rows) await runCommand(c).catch(logErr);
      if (Math.random() < 0.01) await pool.query("DELETE FROM wa_commands WHERE created_at < NOW() - INTERVAL '1 day'");
    } catch (e) { logErr(e); }
    await sleep(2000);
  }
}

async function syncLoop() {
  for (;;) {
    try {
      const { rows } = await pool.query(
        `SELECT w.user_id FROM wa_sessions w
         JOIN users u ON u.telegram_user_id = w.user_id
         WHERE w.status = 'connected' AND u.plan = 'pro' AND u.plan_status = 'active' AND u.plan_expires_at > NOW()`,
      );
      const wanted = new Set(rows.map((r) => Number(r.user_id)));
      for (const id of wanted) {
        if (!sessions.has(id)) { await startSession(id).catch(logErr); await sleep(500); }
      }
      for (const [id, ctx] of [...sessions.entries()]) {
        if (ctx.paired && !wanted.has(id)) { // plan ended or downgraded: unload but keep the link for a renewal
          await stopSession(id, false);
          log('Unloaded WhatsApp session (plan ended) for user', id);
        }
      }
    } catch (e) { logErr(e); }
    await sleep(20000);
  }
}

async function shutdown() {
  log('Shutting down...');
  for (const id of [...sessions.keys()]) await stopSession(id, false);
  try { await pool.end(); } catch (e) { /* ignore */ }
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
process.on('unhandledRejection', logErr);
process.on('uncaughtException', logErr);

(async () => {
  await pool.query("UPDATE wa_sessions SET status = 'none', pairing_code = NULL WHERE status = 'pairing'");
  log('WhatsApp bridge ready');
  commandLoop();
  syncLoop();
})().catch((e) => { logErr(e); process.exit(1); });
