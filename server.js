// server.js — Express server: webhook + mini app + saare API routes

require('dotenv').config();
const express = require('express');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');
const https = require('https');
const { bot, checkMandatoryJoin, SIGNUP_BONUS, COST_PER_MEMBER, REWARD_PER_JOIN, COINS_PER_RUPEE, getBotUsername, isAdmin } = require('./bot');
const { DEPOSIT_PACKAGES, WITHDRAW_MIN_COINS, calcWithdrawGrossRupees, calcWithdrawNetRupees, findDepositPackage } = require('./constants');
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
app.use(express.json({ limit: '15mb' })); // screenshot base64 ke liye zyada size chahiye
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
  // Admin (tumhare) ke liye ye lock kabhi nahi lagega, taaki tum multiple accounts se test kar sako
  if (isAdmin(req.tgUser.id)) return res.json({ allowed: true });

  const { device_id } = req.body;
  if (!device_id) return res.status(400).json({ error: 'device_id missing' });

  const result = await db.checkAndRegisterDevice(device_id, req.tgUser.id);
  res.json(result);
});

// ---------- API: mandatory join check ----------
app.get('/api/check-membership', authMiddleware, async (req, res) => {
  const check = await checkMandatoryJoin(req.tgUser.id);

  if (check.allOk) {
    // Ab hi confirm karo ki referral valid hai — isse pehle count/bonus nahi milta
    const result = await db.tryCreditReferralBonus(req.tgUser.id);
    if (result) {
      bot.telegram.sendMessage(
        result.referrerId,
        `🎉 Aapke referral link se ek naya user join hua! +250 coins mil gaye.`
      ).catch(() => {});
    }
  }

  res.json(check);
});

// ---------- API: user info (coins etc) ----------
app.get('/api/user', authMiddleware, async (req, res) => {
  await db.getOrCreateUser(req.tgUser.id, req.tgUser.username);
  const user = await db.getUser(req.tgUser.id);
  res.json({ ...user, is_admin: isAdmin(req.tgUser.id) });
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

  if (task.owner_id == userId && !isAdmin(userId)) {
    return res.status(400).json({ error: '🔒 Apna khud ka banaya task khud complete nahi kar sakte' });
  }

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
    await db.creditReferralCommission(userId, REWARD_PER_JOIN); // referrer ko 2% commission
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

  const { user } = await db.getOrCreateUser(userId, req.tgUser.username);

  if (target_members < 5) {
    return res.status(400).json({ error: 'Minimum 5 members ka task banana zaroori hai' });
  }

  let cost = 0;
  let unitCost = 0;
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
    unitCost = 0;
  } else {
    cost = target_members * COST_PER_MEMBER;
    unitCost = COST_PER_MEMBER;
    const deducted = await db.deductCoins(userId, cost, 'task_created');
    if (!deducted) {
      return res.status(400).json({ error: `Coins kam hain. ${cost} coins chahiye, aapke paas kam hain.` });
    }
  }

  const task = await db.createTask(userId, chat_id, chat_username, chat_title, target_members, unitCost);
  res.json({ success: true, task, costPaid: cost });
});

// ---------- API: profile stats (kitne task, kitne coins, apne tasks ki history) ----------
app.get('/api/profile', authMiddleware, async (req, res) => {
  const stats = await db.getProfileStats(req.tgUser.id);
  res.json({ ...stats, username: req.tgUser.username, first_name: req.tgUser.first_name, photo_url: req.tgUser.photo_url });
});

// ---------- API: referral link + history ----------
app.get('/api/referrals', authMiddleware, async (req, res) => {
  const data = await db.getReferralData(req.tgUser.id);
  const link = `https://t.me/${getBotUsername()}?start=ref_${req.tgUser.id}`;
  res.json({ link, ...data });
});

// ---------- API: gift code claim karna ----------
app.post('/api/gift/claim', authMiddleware, async (req, res) => {
  const code = (req.body.code || '').trim().toUpperCase();
  if (!code) return res.status(400).json({ error: 'Code daalo' });

  const result = await db.claimGiftCode(code, req.tgUser.id);
  if (result.error) return res.status(400).json({ error: result.error });
  res.json({ success: true, amount: result.amount });
});

// ---------- API: naya broadcast hai ya nahi (red dot ke liye) ----------
app.get('/api/notifications/status', authMiddleware, async (req, res) => {
  const hasNew = await db.hasNewBroadcast(req.tgUser.id);
  res.json({ hasNew });
});

// ---------- API: latest broadcast + uske comments dikhana ----------
app.get('/api/broadcast/latest', authMiddleware, async (req, res) => {
  const broadcast = await db.getLatestBroadcast();
  if (!broadcast) return res.json({ broadcast: null, comments: [] });

  const comments = await db.getBroadcastComments(broadcast.id);
  res.json({ broadcast, comments });
});

// ---------- API: broadcast dekh liya, red dot hata do ----------
app.post('/api/broadcast/mark-seen', authMiddleware, async (req, res) => {
  const { broadcast_id } = req.body;
  await db.markBroadcastSeen(req.tgUser.id, broadcast_id);
  res.json({ success: true });
});

// ---------- API: broadcast pe comment karna ----------
app.post('/api/broadcast/:id/comment', authMiddleware, async (req, res) => {
  const comment = (req.body.comment || '').trim();
  if (!comment) return res.status(400).json({ error: 'Comment khali nahi ho sakta' });

  const username = req.tgUser.username || req.tgUser.first_name || 'User';
  const saved = await db.addBroadcastComment(req.params.id, req.tgUser.id, username, comment);
  res.json({ success: true, comment: saved });
});

// ---------- Broadcast photo proxy (bot token ko URL me expose hone se bachata hai) ----------
app.get('/api/broadcast-photo/:id', async (req, res) => {
  const broadcast = await db.getBroadcastById(req.params.id);
  if (!broadcast || !broadcast.photo_file_id) return res.status(404).end();

  try {
    const fileLink = await bot.telegram.getFileLink(broadcast.photo_file_id);
    https.get(fileLink.href || fileLink.toString(), (tgRes) => {
      res.set('Content-Type', tgRes.headers['content-type'] || 'image/jpeg');
      tgRes.pipe(res);
    }).on('error', () => res.status(500).end());
  } catch (err) {
    res.status(500).end();
  }
});

// ---------- API: support chat thread dekhna ----------
app.get('/api/support/thread', authMiddleware, async (req, res) => {
  const thread = await db.getTicketThread(req.tgUser.id);
  res.json(thread);
});

// ---------- API: support chat me message bhejna ----------
app.post('/api/support/send', authMiddleware, async (req, res) => {
  const message = (req.body.message || '').trim();
  if (!message) return res.status(400).json({ error: 'Message likho' });

  const username = req.tgUser.username || req.tgUser.first_name || 'User';
  const ticket = await db.getOrCreateOpenTicket(req.tgUser.id, username);
  await db.addTicketMessage(ticket.id, 'user', message);

  // Admin ko bhejo — force_reply se admin seedha "Reply" kar sakta hai, koi command nahi chahiye
  bot.telegram.sendMessage(
    process.env.ADMIN_ID,
    `🎫 Ticket #${ticket.id}\n👤 @${username} (ID: ${req.tgUser.id})\n\n${message}\n\n👇 Isi message ko "Reply" karke jawab do`,
    { reply_markup: { force_reply: true, selective: true } }
  ).catch(() => {});

  res.json({ success: true });
});

// ---------- API: admin online hai ya nahi ----------
app.get('/api/support/admin-status', authMiddleware, async (req, res) => {
  const lastActive = await db.getUserLastActive(process.env.ADMIN_ID);
  if (!lastActive) return res.json({ online: false, text: 'Offline' });

  const diffMinutes = Math.floor((new Date() - new Date(lastActive)) / 60000);
  if (diffMinutes <= 5) {
    return res.json({ online: true, text: '🟢 Online' });
  }
  if (diffMinutes < 60) {
    return res.json({ online: false, text: `⚪ ${diffMinutes}m pehle active tha` });
  }
  const hours = Math.floor(diffMinutes / 60);
  return res.json({ online: false, text: `⚪ ${hours}h pehle active tha` });
});

// ---------- API: task ko rate karna (1-5 stars) ----------
app.post('/api/tasks/:id/rate', authMiddleware, async (req, res) => {
  const rating = parseInt(req.body.rating);
  if (!rating || rating < 1 || rating > 5) {
    return res.status(400).json({ error: 'Rating 1 se 5 ke beech honi chahiye' });
  }
  await db.rateTask(req.params.id, req.tgUser.id, rating);
  res.json({ success: true });
});

// ---------- API: task report karna ----------
// ---------- API: task owner khud apna task deactivate kare (20% fee kaat ke refund) ----------
app.post('/api/tasks/:id/deactivate', authMiddleware, async (req, res) => {
  const result = await db.deactivateTaskByOwner(req.params.id, req.tgUser.id);
  if (result.error) return res.status(400).json({ error: result.error });
  res.json(result);
});

app.post('/api/tasks/:id/report', authMiddleware, async (req, res) => {
  const reason = (req.body.reason || 'Other').trim();
  const result = await db.reportTask(req.params.id, req.tgUser.id, reason);

  if (result.alreadyReported) {
    return res.status(400).json({ error: 'Aapne ye task pehle hi report kar diya hai' });
  }

  if (result.flagged) {
    const task = await db.getTaskById(req.params.id);
    bot.telegram.sendMessage(
      process.env.ADMIN_ID,
      `🚩 Task #${task.id} ("${task.chat_title}") ko ${result.reportCount} reports mil chuki hain aur ye auto-flag ho gaya hai.\n\n` +
      `Faisla lene ke liye:\n/taskaction ${task.id} approve  (wapas active karo)\n/taskaction ${task.id} remove  (hamesha ke liye hatao)`
    ).catch(() => {});
  }

  res.json({ success: true, flagged: result.flagged });
});

// ---------- API: deposit packages ki list dikhana ----------
app.get('/api/deposit/packages', authMiddleware, async (req, res) => {
  res.json(DEPOSIT_PACKAGES);
});

// ---------- API: deposit shuru karna (3-min timer start) ----------
app.post('/api/deposit/initiate', authMiddleware, async (req, res) => {
  const { amount, coins } = req.body;
  const pkg = findDepositPackage(amount, coins);
  if (!pkg) return res.status(400).json({ error: 'Ye package valid nahi hai' });

  const username = req.tgUser.username || req.tgUser.first_name || 'User';
  const deposit = await db.initiateDeposit(req.tgUser.id, username, pkg.amount, pkg.coins);
  res.json(deposit);
});

// ---------- API: deposit ka proof submit karna (screenshot + UTR + naam) ----------
app.post('/api/deposit/submit-proof', authMiddleware, async (req, res) => {
  const { request_id, name, utr, screenshot_base64 } = req.body;

  if (!name || !utr || !screenshot_base64) {
    return res.status(400).json({ error: 'Sab fields bharo — naam, UTR, aur screenshot' });
  }

  const result = await db.submitDepositProof(request_id, req.tgUser.id, name.trim(), utr.trim());
  if (result.error) return res.status(400).json({ error: result.error });

  // Screenshot ko seedha admin ko bhej do, kahin store nahi karna
  try {
    const base64Data = screenshot_base64.replace(/^data:image\/\w+;base64,/, '');
    const buffer = Buffer.from(base64Data, 'base64');
    const deposit = result.deposit;

    await bot.telegram.sendPhoto(
      process.env.ADMIN_ID,
      { source: buffer },
      {
        caption:
          `💰 Naya Deposit Request #${deposit.id}\n\n` +
          `👤 Naam: ${deposit.name}\n` +
          `🆔 Telegram: @${deposit.username} (ID: ${deposit.user_id})\n` +
          `💵 Amount: ₹${deposit.amount_inr} → 🪙 ${deposit.coins_amount} coins\n` +
          `🧾 UTR: ${deposit.utr}\n\n` +
          `Apna UPI app khol ke UTR match karo, fir neeche button dabao:`,
        reply_markup: {
          inline_keyboard: [[
            { text: '✅ Approve', callback_data: `dep_approve_${deposit.id}` },
            { text: '❌ Reject', callback_data: `dep_reject_${deposit.id}` }
          ]]
        }
      }
    );
  } catch (err) {
    console.error('Deposit screenshot bhejne me error:', err.message);
  }

  res.json({ success: true });
});

// ---------- API: apne deposit ka status check karna (timer ke liye) ----------
app.get('/api/deposit/status/:id', authMiddleware, async (req, res) => {
  const deposit = await db.getDepositById(req.params.id);
  if (!deposit || deposit.user_id != req.tgUser.id) return res.status(404).json({ error: 'Not found' });
  res.json(deposit);
});

// ---------- API: apni deposit history dekhna ----------
app.get('/api/deposit/my', authMiddleware, async (req, res) => {
  const list = await db.getMyDeposits(req.tgUser.id);
  res.json(list);
});

// ---------- API: withdraw config (minimum coins, rates) ----------
app.get('/api/withdraw/config', authMiddleware, async (req, res) => {
  res.json({ minCoins: WITHDRAW_MIN_COINS });
});

// ---------- API: withdraw request banana ----------
app.post('/api/withdraw/create', authMiddleware, async (req, res) => {
  const coins = parseInt(req.body.coins);
  const upiId = (req.body.upi_id || '').trim();

  if (!coins || coins < WITHDRAW_MIN_COINS) {
    return res.status(400).json({ error: `Minimum ${WITHDRAW_MIN_COINS} coins withdraw kar sakte ho` });
  }
  if (!upiId || !upiId.includes('@')) {
    return res.status(400).json({ error: 'Sahi UPI ID daalo (jaise name@bank)' });
  }

  const grossRupees = calcWithdrawGrossRupees(coins);
  const netRupees = calcWithdrawNetRupees(coins);
  const username = req.tgUser.username || req.tgUser.first_name || 'User';

  const result = await db.createWithdrawRequest(req.tgUser.id, username, coins, grossRupees, netRupees, upiId);
  if (result.error) return res.status(400).json({ error: result.error });

  const w = result.withdraw;
  bot.telegram.sendMessage(
    process.env.ADMIN_ID,
    `💸 Naya Withdraw Request #${w.id}\n\n` +
    `🆔 Telegram: @${w.username} (ID: ${w.user_id})\n` +
    `🪙 Coins: ${w.coins}\n` +
    `💵 Gross: ₹${w.gross_rupees} → Net (fees ke baad): ₹${w.net_rupees}\n` +
    `📱 UPI ID: ${w.upi_id}\n\n` +
    `Manually ₹${w.net_rupees} is UPI ID pe bhej do, fir neeche button dabao:`,
    {
      reply_markup: {
        inline_keyboard: [[
          { text: '✅ Approve (Paid)', callback_data: `wd_approve_${w.id}` },
          { text: '❌ Reject', callback_data: `wd_reject_${w.id}` }
        ]]
      }
    }
  ).catch(() => {});

  res.json({ success: true, withdraw: w });
});

// ---------- API: apni withdraw history dekhna ----------
app.get('/api/withdraw/my', authMiddleware, async (req, res) => {
  const list = await db.getMyWithdrawals(req.tgUser.id);
  res.json(list);
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

  // Har 1 ghante me expired tasks check karke refund kar do
  setInterval(async () => {
    try {
      const count = await db.expireOldTasks();
      if (count > 0) console.log(`⏰ ${count} tasks expire ho gaye, refund kar diya`);
    } catch (err) {
      console.error('Auto-expire error:', err.message);
    }
  }, 60 * 60 * 1000); // 1 ghanta

  // Har 20 second me deposit ke 3-min timer wali expired requests check karo
  setInterval(async () => {
    try {
      await db.expireOldDepositRequests();
    } catch (err) {
      console.error('Deposit auto-expire error:', err.message);
    }
  }, 20 * 1000);
});
