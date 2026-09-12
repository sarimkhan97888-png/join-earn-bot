// bot.js — Bot ka poora dimag yahan hai

const { Telegraf, Markup } = require('telegraf');
const db = require('./db');

const bot = new Telegraf(process.env.BOT_TOKEN);

const REQUIRED_GROUP = process.env.REQUIRED_GROUP;     // mandatory GC username
const REQUIRED_CHANNEL = process.env.REQUIRED_CHANNEL; // mandatory Channel username
const ADMIN_ID = process.env.ADMIN_ID;

const SIGNUP_BONUS = 550;        // pehli baar task banane wale ko free coins
const COST_PER_MEMBER = 110;     // dusri baar se, per member cost
const REWARD_PER_JOIN = 100;     // verify karne wale ko milne wala reward

function isAdmin(userId) {
  return String(userId) === String(ADMIN_ID);
}

// Bot ka username ek baar nikal ke cache kar lete hain (referral link banane ke liye)
let BOT_USERNAME = null;
bot.telegram.getMe().then(info => { BOT_USERNAME = info.username; });
function getBotUsername() { return BOT_USERNAME; }

// ---------- Helper: check karo user required GC + Channel me hai ya nahi ----------
async function checkMandatoryJoin(userId) {
  try {
    const groupMember = await bot.telegram.getChatMember(`@${REQUIRED_GROUP}`, userId);
    const channelMember = await bot.telegram.getChatMember(`@${REQUIRED_CHANNEL}`, userId);

    const okStatuses = ['member', 'administrator', 'creator'];
    const groupOk = okStatuses.includes(groupMember.status);
    const channelOk = okStatuses.includes(channelMember.status);

    return { groupOk, channelOk, allOk: groupOk && channelOk };
  } catch (err) {
    console.error('Mandatory join check error:', err.message);
    // Agar bot khud admin nahi hai ya chat nahi mil raha, safe side pe block kar do
    return { groupOk: false, channelOk: false, allOk: false };
  }
}

// ---------- /start command ----------
bot.start(async (ctx) => {
  const user = ctx.from;
  const { user: dbUser, isNew } = await db.getOrCreateUser(user.id, user.username || user.first_name);

  // ---------- Referral handling ----------
  // Link aisi hoti hai: https://t.me/BOTUSERNAME?start=ref_123456
  const payload = ctx.startPayload; // "ref_123456"
  if (isNew && payload && payload.startsWith('ref_')) {
    const referrerId = payload.replace('ref_', '');
    if (referrerId && referrerId != user.id) {
      const referrer = await db.getUser(referrerId);
      if (referrer) {
        await db.setReferrer(user.id, referrerId);
        await db.creditReferralSignupBonus(referrerId);
        bot.telegram.sendMessage(
          referrerId,
          `🎉 Aapke referral link se ek naya user join hua! +250 coins mil gaye.`
        ).catch(() => {});
      }
    }
  }

  const check = await checkMandatoryJoin(user.id);

  if (!check.allOk) {
    return ctx.reply(
      `👋 Welcome!\n\nApp use karne ke liye pehle ye dono join karo:\n\n` +
      `${check.groupOk ? '✅' : '❌'} Group: @${REQUIRED_GROUP}\n` +
      `${check.channelOk ? '✅' : '❌'} Channel: @${REQUIRED_CHANNEL}\n\n` +
      `Join karne ke baad neeche button dabao.`,
      Markup.inlineKeyboard([
        [Markup.button.url('📢 Join Group', `https://t.me/${REQUIRED_GROUP}`)],
        [Markup.button.url('📢 Join Channel', `https://t.me/${REQUIRED_CHANNEL}`)],
        [Markup.button.callback('✅ Maine Join Kar Liya', 'recheck_join')]
      ])
    );
  }

  return ctx.reply(
    '🎉 Welcome! Neeche-left corner me jo Menu button (☰ icon) hai, usse app open karo aur coins kamana shuru karo.'
  );
});

bot.action('recheck_join', async (ctx) => {
  const check = await checkMandatoryJoin(ctx.from.id);
  if (check.allOk) {
    await ctx.editMessageText('✅ Verified! Neeche-left corner me Menu button (☰) dabao app kholne ke liye.');
  } else {
    return ctx.answerCbQuery('❌ Abhi bhi dono join nahi kiye. Pehle join karo.', { show_alert: true });
  }
});

// ---------- LEAVE DETECTION (real-time webhook) ----------
// Jab bhi kisi tracked chat me member status change ho (join/leave), Telegram ye event bhejta hai
bot.on('chat_member', async (ctx) => {
  try {
    const update = ctx.update.chat_member;
    const chatId = update.chat.id;
    const userId = update.new_chat_member.user.id;
    const newStatus = update.new_chat_member.status;

    if (newStatus === 'left' || newStatus === 'kicked') {
      // Ye user is chat_id wale kisi task se verified tha kya? Agar haan to mark 'left'
      const verifiedEntries = await db.getVerifiedUsersByChatId(chatId);
      const match = verifiedEntries.find(e => e.user_id == userId);
      if (match) {
        await db.markUserTaskLeft(userId, match.task_id);
        console.log(`User ${userId} left task chat ${chatId} — marked as left`);
        // Note: coins wapas nahi le rahe by default. Agar lene hain to yahan db.deductCoins call karo.
      }
    }
  } catch (err) {
    console.error('chat_member handler error:', err.message);
  }
});

// ==================== ADMIN-ONLY COMMANDS (sirf tumhare liye) ====================

// /addcoins <amount> — apne khud ke account me coins add karna
bot.command('addcoins', async (ctx) => {
  if (!isAdmin(ctx.from.id)) return; // admin nahi hai to chup chaap ignore

  const args = ctx.message.text.split(' ');
  const amount = parseInt(args[1]);
  if (!amount || amount <= 0) {
    return ctx.reply('Usage: /addcoins <amount>\nExample: /addcoins 1000');
  }

  await db.getOrCreateUser(ctx.from.id, ctx.from.username);
  await db.addCoins(ctx.from.id, amount, 'admin_manual_add');
  ctx.reply(`✅ ${amount} coins add ho gaye aapke account me.`);
});

// /gift <code> <amount> <maxUses> — naya gift code banana (maxUses 0 = unlimited)
bot.command('gift', async (ctx) => {
  if (!isAdmin(ctx.from.id)) return;

  const args = ctx.message.text.split(' ');
  const code = args[1];
  const amount = parseInt(args[2]);
  const maxUses = args[3] ? parseInt(args[3]) : 1;

  if (!code || !amount) {
    return ctx.reply('Usage: /gift <code> <amount> <maxUses>\nExample: /gift WELCOME100 100 50\n(maxUses 0 likho unlimited ke liye)');
  }

  try {
    await db.createGiftCode(code.toUpperCase(), amount, maxUses);
    ctx.reply(
      `✅ Gift code ban gaya!\n\n🎁 Code: ${code.toUpperCase()}\n🪙 Amount: ${amount} coins\n👥 Max Uses: ${maxUses === 0 ? 'Unlimited' : maxUses}\n\nIsko users ke saath share karo, wo App ke Profile section me jaake claim kar sakte hain.`
    );
  } catch (err) {
    ctx.reply('❌ Ye code pehle se exist karta hai, doosra naam try karo.');
  }
});

// /broadcast <message>  — sirf text broadcast (photo ke bina)
bot.command('broadcast', async (ctx) => {
  if (!isAdmin(ctx.from.id)) return;

  const message = ctx.message.text.replace('/broadcast', '').trim();
  if (!message) {
    return ctx.reply('Usage: /broadcast <message>\n(Photo ke saath bhejna ho to photo attach karke caption me /broadcast likho)');
  }

  await db.createBroadcast(message, null);
  ctx.reply('✅ Broadcast bhej diya! Sabko Mini App me notification bell pe red dot dikhega.');
});

// Photo + caption "/broadcast ..." — photo wala broadcast
bot.on('photo', async (ctx) => {
  if (!isAdmin(ctx.from.id)) return;
  const caption = ctx.message.caption || '';
  if (!caption.startsWith('/broadcast')) return; // normal photo hai, ignore karo

  const message = caption.replace('/broadcast', '').trim();
  const photos = ctx.message.photo;
  const fileId = photos[photos.length - 1].file_id; // sabse best quality wali photo

  await db.createBroadcast(message, fileId);
  ctx.reply('✅ Photo broadcast bhej diya! Sabko Mini App me notification bell pe red dot dikhega.');
});

// /reply <ticket_id> <message> — support ticket ka jawab dena
bot.command('reply', async (ctx) => {
  if (!isAdmin(ctx.from.id)) return;

  const args = ctx.message.text.split(' ');
  const ticketId = parseInt(args[1]);
  const replyMessage = args.slice(2).join(' ');

  if (!ticketId || !replyMessage) {
    return ctx.reply('Usage: /reply <ticket_id> <message>\nExample: /reply 3 Aapki problem solve ho gayi hai');
  }

  const ticket = await db.replyToTicket(ticketId, replyMessage);
  if (!ticket) return ctx.reply('❌ Ye ticket ID nahi mila');

  ctx.reply(`✅ Reply bhej diya ticket #${ticketId} ko`);
  bot.telegram.sendMessage(
    ticket.user_id,
    `📩 Support Reply (Ticket #${ticketId}):\n\n${replyMessage}`
  ).catch(() => {});
});

module.exports = { bot, checkMandatoryJoin, SIGNUP_BONUS, COST_PER_MEMBER, REWARD_PER_JOIN, getBotUsername, isAdmin };
