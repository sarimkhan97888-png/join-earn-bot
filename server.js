// server.js — Express server: webhook + mini app + saare API routes

require('dotenv').config();
const express = require('express');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');
const { bot, checkMandatoryJoin, SIGNUP_BONUS, COST_PER_MEMBER, REWARD_PER_JOIN } = require('./bot');
const db = require('./db');

// Server start hote hi database tables khud-ba-khud ban jayengi (agar pehle se nahi hain)
// Isse manually SQL chalane ki zaroorat nahi padti
async function setupDatabase() {
  try {
    const schema = fs.readFileSync(path.join(__dirname, 'schema.sql'), 'utf8');
    await db.pool.query(schema);
    console.log('✅ Database tables ready hain');
  } catch (err) {
    console.error('❌ Database setup me error:', err.message);
  }
}

const app = express();
app.use(express.json());
app.use('/webapp', express.static(path.join(__dirname, 'webapp')));

// ---------- Telegram WebApp initData verify (security) ----------
// Isse pata chalta hai ki request sach me Telegram se aa rahi hai, koi fake nahi
function verifyInitData(initData) {
  const params = new URLSearchParams(initData);
  const hash = params.get('hash');
  params.delete('hash');

  const dataCheckArr = [];
  for (const [key, value] of [...params.entries()].sort()) {
    dataCheckArr.push(`${key}=${value}`);
  }
  const dataCheckString = dataCheckArr.join('\n');

  const secretKey = crypto.createHmac('sha256', 'WebAppData').update(process.env.BOT_TOKEN).digest();
  const calculatedHash = crypto.createHmac('sha256', secretKey).update(dataCheckString).digest('hex');

  if (calculatedHash !== hash) return null;

  const userStr = params.get('user');
  return userStr ? JSON.parse(userStr) : null;
}

// Har API request pe initData se user nikalne wala middleware
function authMiddleware(req, res, next) {
  const initData = req.headers['x-telegram-init-data'];
  console.log('🔍 Auth check | initData length:', initData ? initData.length : 0);

  const user = verifyInitData(initData || '');
  if (!user) {
    console.log('❌ Auth FAILED - initData verify nahi hua. Raw initData:', initData ? initData.substring(0, 100) : '(khali hai)');
    return res.status(401).json({ error: 'Invalid Telegram auth' });
  }
  console.log('✅ Auth OK - user:', user.id, user.username || user.first_name);
  req.tgUser = user;
  next();
}

// ---------- API: device lock check (ek device = ek account) ----------
app.post('/api/device/check', authMiddleware, async (req, res) => {
  const { device_id } = req.body;
  if (!device_id) return res.status(400).json({ error: 'device_id missing' });

  const result = await db.checkAndRegisterDevice(device_id, req.tgUser.id);
  res.json(result);
});

// ---------- API: mandatory join check ----------
app.get('/api/check-membership', authMiddleware, async (req, res) => {
  console.log('📋 Checking membership for user:', req.tgUser.id);
  const check = await checkMandatoryJoin(req.tgUser.id);
  console.log('📋 Membership result:', JSON.stringify(check));
  res.json(check);
});

// ---------- API: user info (coins etc) ----------
app.get('/api/user', authMiddleware, async (req, res) => {
  await db.getOrCreateUser(req.tgUser.id, req.tgUser.username);
  const user = await db.getUser(req.tgUser.id);
  res.json(user);
});

// ---------- API: active tasks list (earning ke liye) ----------
app.get('/api/tasks', authMiddleware, async (req, res) => {
  const tasks = await db.getActiveTasksForUser(req.tgUser.id);
  res.json(tasks);
});

// ---------- API: user ne "Verify Join" dabaya ----------
app.post('/api/tasks/:id/verify', authMiddleware, async (req, res) => {
  const taskId = req.params.id;
  const userId = req.tgUser.id;

  const task = await db.getTaskById(taskId);
  if (!task) return res.status(404).json({ error: 'Task not found' });

  try {
    const member = await bot.telegram.getChatMember(task.chat_id, userId);
    const okStatuses = ['member', 'administrator', 'creator'];

    if (!okStatuses.includes(member.status)) {
      return res.status(400).json({ error: 'Aapne abhi tak join nahi kiya. Pehle join karo.' });
    }

    // Already verified check
    const existing = await db.getUserTaskStatus(userId, taskId);
    if (existing && existing.status === 'verified') {
      return res.status(400).json({ error: 'Ye task pehle se verify ho chuka hai.' });
    }

    await db.recordUserTaskPending(userId, taskId);
    await db.markUserTaskVerified(userId, taskId);
    await db.addCoins(userId, REWARD_PER_JOIN, 'task_verified');
    const updatedTask = await db.incrementTaskCount(taskId);

    // Owner ko notification bhejo
    const remaining = updatedTask.target_members - updatedTask.current_count;
    if (updatedTask.status === 'completed') {
      bot.telegram.sendMessage(
        task.owner_id,
        `✅ Task complete ho gaya! "${task.chat_title}" ko total ${task.target_members} members mil gaye 🎊`
      );
    } else {
      bot.telegram.sendMessage(
        task.owner_id,
        `🎉 Aapke channel "${task.chat_title}" me 1 naya member add hua!\n` +
        `📊 Progress: ${updatedTask.current_count}/${updatedTask.target_members}\n` +
        `⏳ Baaki: ${remaining} members`
      );
    }

    res.json({ success: true, coinsEarned: REWARD_PER_JOIN });
  } catch (err) {
    console.error('Verify error:', err.message);
    res.status(400).json({ error: 'Verify nahi ho paya. Chat ID galat ho sakta hai.' });
  }
});

// ---------- API: bot admin status verify karna (task banane se pehle) ----------
app.post('/api/verify-admin', authMiddleware, async (req, res) => {
  const { chatIdentifier } = req.body; // @username ya invite link se username nikala hua
  if (!chatIdentifier) return res.status(400).json({ error: 'Channel/group username do' });

  try {
    const chat = await bot.telegram.getChat(chatIdentifier);
    const botInfo = await bot.telegram.getMe();
    const member = await bot.telegram.getChatMember(chat.id, botInfo.id);

    if (member.status !== 'administrator') {
      return res.status(400).json({ error: '❌ Bot admin nahi hai. Pehle admin banao.' });
    }
    if (!member.can_invite_users) {
      return res.status(400).json({ error: '❌ "Invite Users / Add Members" permission ON nahi hai.' });
    }

    res.json({
      success: true,
      chat_id: chat.id,
      title: chat.title,
      member_count: chat.member_count || null
    });
  } catch (err) {
    console.error('Admin verify error:', err.message);
    res.status(400).json({ error: '❌ Channel/group nahi mila. Username sahi se check karo.' });
  }
});

// ---------- API: naya task create karna ----------
app.post('/api/tasks/create', authMiddleware, async (req, res) => {
  const userId = req.tgUser.id;
  const { chat_id, chat_username, chat_title, target_members } = req.body;

  if (!chat_id || !target_members) {
    return res.status(400).json({ error: 'Zaroori details missing hain' });
  }

  const user = await db.getOrCreateUser(userId, req.tgUser.username);

  let cost = 0;
  if (!user.first_task_used) {
    // Pehli baar — free bonus, max 5 members (500/100) allowed is bonus se
    const maxFreeMembers = Math.floor(SIGNUP_BONUS / REWARD_PER_JOIN);
    if (target_members > maxFreeMembers) {
      return res.status(400).json({
        error: `Pehli baar sirf ${maxFreeMembers} members tak free hai. Zyada ke liye coins chahiye honge.`
      });
    }
    await db.pool.query('UPDATE users SET first_task_used = TRUE WHERE id = $1', [userId]);
    await db.addCoins(userId, SIGNUP_BONUS, 'signup_bonus');
    cost = 0; // bonus se cover ho gaya
  } else {
    cost = target_members * COST_PER_MEMBER;
    const deducted = await db.deductCoins(userId, cost, 'task_created');
    if (!deducted) {
      return res.status(400).json({ error: `Coins kam hain. ${cost} coins chahiye, aapke paas kam hain.` });
    }
  }

  const task = await db.createTask(userId, chat_id, chat_username, chat_title, target_members);
  res.json({ success: true, task, costPaid: cost });
});

// ---------- API: profile stats (kitne task, kitne coins, apne tasks ki history) ----------
app.get('/api/profile', authMiddleware, async (req, res) => {
  const stats = await db.getProfileStats(req.tgUser.id);
  res.json({ ...stats, username: req.tgUser.username, first_name: req.tgUser.first_name, photo_url: req.tgUser.photo_url });
});

// ---------- Telegram webhook ----------
app.use(bot.webhookCallback('/webhook'));

const PORT = process.env.PORT || 3000;
app.listen(PORT, async () => {
  console.log(`Server chal raha hai port ${PORT} par`);
  await setupDatabase();
  try {
    await bot.telegram.setWebhook(`${process.env.WEBHOOK_URL}/webhook`);
    console.log('Webhook set ho gaya:', `${process.env.WEBHOOK_URL}/webhook`);
  } catch (err) {
    console.error('Webhook set karne me error:', err.message);
  }
});
