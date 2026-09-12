// bot.js — Bot ka poora dimag yahan hai

const { Telegraf, Markup } = require('telegraf');
const db = require('./db');

const bot = new Telegraf(process.env.BOT_TOKEN);

const REQUIRED_GROUP = process.env.REQUIRED_GROUP;     // mandatory GC username
const REQUIRED_CHANNEL = process.env.REQUIRED_CHANNEL; // mandatory Channel username

const SIGNUP_BONUS = 550;        // pehli baar task banane wale ko free coins
const COST_PER_MEMBER = 110;     // dusri baar se, per member cost
const REWARD_PER_JOIN = 100;     // verify karne wale ko milne wala reward

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
  await db.getOrCreateUser(user.id, user.username || user.first_name);

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

module.exports = { bot, checkMandatoryJoin, SIGNUP_BONUS, COST_PER_MEMBER, REWARD_PER_JOIN };
