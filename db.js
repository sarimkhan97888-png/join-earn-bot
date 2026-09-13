// db.js — Saari database queries yahan hain (ek jagah, easy to manage)

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: { rejectUnauthorized: false } // Render Postgres ke liye zaroori
});

// ---------- USERS ----------

async function getOrCreateUser(id, username) {
  const existing = await pool.query('SELECT * FROM users WHERE id = $1', [id]);
  if (existing.rows.length > 0) return { user: existing.rows[0], isNew: false };

  const created = await pool.query(
    'INSERT INTO users (id, username) VALUES ($1, $2) RETURNING *',
    [id, username]
  );
  return { user: created.rows[0], isNew: true };
}

async function getUser(id) {
  const res = await pool.query('SELECT * FROM users WHERE id = $1', [id]);
  return res.rows[0];
}

async function addCoins(userId, amount, reason) {
  await pool.query('UPDATE users SET coins = coins + $1 WHERE id = $2', [amount, userId]);
  await pool.query(
    'INSERT INTO coin_history (user_id, amount, reason) VALUES ($1, $2, $3)',
    [userId, amount, reason]
  );
}

async function deductCoins(userId, amount, reason) {
  const user = await getUser(userId);
  if (!user || user.coins < amount) return false; // insufficient balance
  await pool.query('UPDATE users SET coins = coins - $1 WHERE id = $2', [amount, userId]);
  await pool.query(
    'INSERT INTO coin_history (user_id, amount, reason) VALUES ($1, $2, $3)',
    [userId, -amount, reason]
  );
  return true;
}

// ---------- TASKS ----------

async function createTask(ownerId, chatId, chatUsername, chatTitle, targetMembers, unitCost) {
  const res = await pool.query(
    `INSERT INTO tasks (owner_id, chat_id, chat_username, chat_title, target_members, unit_cost)
     VALUES ($1, $2, $3, $4, $5, $6) RETURNING *`,
    [ownerId, chatId, chatUsername, chatTitle, targetMembers, unitCost]
  );
  return res.rows[0];
}

async function getActiveTasksForUser(userId) {
  // Sabhi active tasks dikhao (khud ke bhi) — frontend khud ke task ko lock karke dikhayega
  const res = await pool.query(
    `SELECT t.*,
       COALESCE((SELECT ROUND(AVG(rating),1) FROM task_ratings WHERE task_id = t.id), 0) AS avg_rating,
       COALESCE((SELECT COUNT(*) FROM task_ratings WHERE task_id = t.id), 0) AS rating_count
     FROM tasks t
     WHERE t.status = 'active'
       AND t.current_count < t.target_members
       AND NOT EXISTS (
         SELECT 1 FROM user_tasks ut
         WHERE ut.task_id = t.id AND ut.user_id = $1 AND ut.status IN ('verified','pending')
       )
     ORDER BY t.created_at DESC`,
    [userId]
  );
  return res.rows;
}

async function getTaskById(taskId) {
  const res = await pool.query('SELECT * FROM tasks WHERE id = $1', [taskId]);
  return res.rows[0];
}

async function incrementTaskCount(taskId) {
  const res = await pool.query(
    `UPDATE tasks SET current_count = current_count + 1,
     status = CASE WHEN current_count + 1 >= target_members THEN 'completed' ELSE status END
     WHERE id = $1 RETURNING *`,
    [taskId]
  );
  return res.rows[0];
}

// ---------- USER_TASKS (join tracking) ----------

async function recordUserTaskPending(userId, taskId) {
  await pool.query(
    `INSERT INTO user_tasks (user_id, task_id, status) VALUES ($1, $2, 'pending')
     ON CONFLICT (user_id, task_id) DO NOTHING`,
    [userId, taskId]
  );
}

async function markUserTaskVerified(userId, taskId) {
  await pool.query(
    `UPDATE user_tasks SET status = 'verified' WHERE user_id = $1 AND task_id = $2`,
    [userId, taskId]
  );
}

async function markUserTaskLeft(userId, taskId) {
  await pool.query(
    `UPDATE user_tasks SET status = 'left' WHERE user_id = $1 AND task_id = $2`,
    [userId, taskId]
  );
}

async function getUserTaskStatus(userId, taskId) {
  const res = await pool.query(
    'SELECT * FROM user_tasks WHERE user_id = $1 AND task_id = $2',
    [userId, taskId]
  );
  return res.rows[0];
}

// Leave-detect ke liye: kisi chat_id se match karke saare users nikalo jo verified the
async function getVerifiedUsersByChatId(chatId) {
  const res = await pool.query(
    `SELECT ut.user_id, ut.task_id FROM user_tasks ut
     JOIN tasks t ON t.id = ut.task_id
     WHERE t.chat_id = $1 AND ut.status = 'verified'`,
    [chatId]
  );
  return res.rows;
}

// ---------- DEVICE LOCK (ek device = ek hi account) ----------

async function checkAndRegisterDevice(deviceId, userId) {
  const existing = await pool.query(
    'SELECT user_id FROM device_registry WHERE device_id = $1',
    [deviceId]
  );

  if (existing.rows.length === 0) {
    // Naya device — isi user ke naam register kar do
    await pool.query(
      'INSERT INTO device_registry (device_id, user_id) VALUES ($1, $2)',
      [deviceId, userId]
    );
    return { allowed: true };
  }

  if (existing.rows[0].user_id == userId) {
    return { allowed: true }; // wahi purana user hai, sab theek
  }

  return { allowed: false }; // koi dusra account isi device se try kar raha hai
}

// ---------- PROFILE STATS ----------

async function getProfileStats(userId) {
  const user = await getUser(userId);

  const tasksCompletedRes = await pool.query(
    `SELECT COUNT(*) FROM user_tasks WHERE user_id = $1 AND status = 'verified'`,
    [userId]
  );

  const totalEarnedRes = await pool.query(
    `SELECT COALESCE(SUM(amount), 0) as total FROM coin_history WHERE user_id = $1 AND amount > 0`,
    [userId]
  );

  const myTasksRes = await pool.query(
    `SELECT * FROM tasks WHERE owner_id = $1 ORDER BY created_at DESC`,
    [userId]
  );

  return {
    coins: user.coins,
    tasks_completed: parseInt(tasksCompletedRes.rows[0].count),
    total_earned: parseInt(totalEarnedRes.rows[0].total),
    my_tasks: myTasksRes.rows
  };
}

// ---------- REFERRAL SYSTEM ----------

const REFERRAL_SIGNUP_BONUS = 250;
const REFERRAL_COMMISSION_RATE = 0.02; // 2%

async function setReferrer(referredId, referrerId) {
  await pool.query('UPDATE users SET referred_by = $1 WHERE id = $2', [referrerId, referredId]);
  await pool.query(
    `INSERT INTO referrals (referrer_id, referred_id) VALUES ($1, $2)
     ON CONFLICT (referred_id) DO NOTHING`,
    [referrerId, referredId]
  );
}

async function creditReferralSignupBonus(referrerId) {
  await addCoins(referrerId, REFERRAL_SIGNUP_BONUS, 'referral_signup_bonus');
}

async function creditReferralCommission(referredUserId, baseAmount) {
  const referredUser = await getUser(referredUserId);
  if (!referredUser || !referredUser.referred_by) return; // isko koi refer nahi kiya

  const referrerId = referredUser.referred_by;
  const commission = Math.round(baseAmount * REFERRAL_COMMISSION_RATE);
  if (commission <= 0) return;

  await addCoins(referrerId, commission, 'referral_commission');
  await pool.query(
    `INSERT INTO referral_commissions (referrer_id, referred_id, amount) VALUES ($1, $2, $3)`,
    [referrerId, referredUserId, commission]
  );
}

async function getReferralData(userId) {
  const rows = await pool.query(
    `SELECT u.id, u.username, r.created_at,
            COALESCE((SELECT SUM(amount) FROM referral_commissions
                      WHERE referred_id = u.id AND referrer_id = $1), 0) AS commission_earned
     FROM referrals r
     JOIN users u ON u.id = r.referred_id
     WHERE r.referrer_id = $1
     ORDER BY r.created_at DESC`,
    [userId]
  );
  const totalEarned = rows.rows.reduce((sum, r) => sum + parseInt(r.commission_earned), 0);
  return { history: rows.rows, count: rows.rows.length, totalEarned };
}

// ---------- GIFT CODES ----------

async function createGiftCode(code, amount, maxUses) {
  const res = await pool.query(
    `INSERT INTO gift_codes (code, amount, max_uses) VALUES ($1, $2, $3) RETURNING *`,
    [code, amount, maxUses]
  );
  return res.rows[0];
}

async function claimGiftCode(code, userId) {
  const codeCheck = await pool.query('SELECT * FROM gift_codes WHERE code = $1', [code]);
  if (codeCheck.rows.length === 0) {
    return { error: 'Ye gift code exist nahi karta' };
  }

  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // Ek user ek code sirf ek baar claim kar sake — DB level unique constraint isko guarantee karta hai
    const claimInsert = await client.query(
      `INSERT INTO gift_claims (code, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING *`,
      [code, userId]
    );
    if (claimInsert.rows.length === 0) {
      await client.query('ROLLBACK');
      return { error: 'Ye code aap pehle hi claim kar chuke ho' };
    }

    // Atomic increment — sirf tabhi badhega jab limit khatam na hui ho (race-condition proof)
    const codeUpdate = await client.query(
      `UPDATE gift_codes SET used_count = used_count + 1
       WHERE code = $1 AND (max_uses = 0 OR used_count < max_uses)
       RETURNING *`,
      [code]
    );
    if (codeUpdate.rows.length === 0) {
      await client.query('ROLLBACK');
      return { error: 'Is gift code ki limit khatam ho chuki hai' };
    }

    const amount = codeUpdate.rows[0].amount;
    await client.query('UPDATE users SET coins = coins + $1 WHERE id = $2', [amount, userId]);
    await client.query(
      `INSERT INTO coin_history (user_id, amount, reason) VALUES ($1, $2, 'gift_code')`,
      [userId, amount]
    );

    await client.query('COMMIT');
    return { success: true, amount };
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}

// ---------- BROADCAST / NOTIFICATIONS ----------

async function createBroadcast(message, photoFileId) {
  const res = await pool.query(
    `INSERT INTO broadcasts (message, photo_file_id) VALUES ($1, $2) RETURNING *`,
    [message, photoFileId]
  );
  return res.rows[0];
}

async function getLatestBroadcast() {
  const res = await pool.query('SELECT * FROM broadcasts ORDER BY id DESC LIMIT 1');
  return res.rows[0] || null;
}

async function getBroadcastById(id) {
  const res = await pool.query('SELECT * FROM broadcasts WHERE id = $1', [id]);
  return res.rows[0];
}

async function addBroadcastComment(broadcastId, userId, username, comment) {
  const res = await pool.query(
    `INSERT INTO broadcast_comments (broadcast_id, user_id, username, comment)
     VALUES ($1, $2, $3, $4) RETURNING *`,
    [broadcastId, userId, username, comment]
  );
  return res.rows[0];
}

async function getBroadcastComments(broadcastId) {
  const res = await pool.query(
    'SELECT * FROM broadcast_comments WHERE broadcast_id = $1 ORDER BY created_at ASC',
    [broadcastId]
  );
  return res.rows;
}

async function hasNewBroadcast(userId) {
  const user = await getUser(userId);
  const latest = await getLatestBroadcast();
  if (!latest) return false;
  return (user.last_seen_broadcast_id || 0) < latest.id;
}

async function markBroadcastSeen(userId, broadcastId) {
  await pool.query('UPDATE users SET last_seen_broadcast_id = $1 WHERE id = $2', [broadcastId, userId]);
}

// ---------- SUPPORT TICKETS ----------

async function createSupportTicket(userId, username, message) {
  const res = await pool.query(
    `INSERT INTO support_tickets (user_id, username, message) VALUES ($1, $2, $3) RETURNING *`,
    [userId, username, message]
  );
  return res.rows[0];
}

async function getMyTickets(userId) {
  const res = await pool.query(
    'SELECT * FROM support_tickets WHERE user_id = $1 ORDER BY created_at DESC',
    [userId]
  );
  return res.rows;
}

async function replyToTicket(ticketId, replyMessage) {
  const res = await pool.query(
    `UPDATE support_tickets SET admin_reply = $1, status = 'replied', replied_at = NOW()
     WHERE id = $2 RETURNING *`,
    [replyMessage, ticketId]
  );
  return res.rows[0];
}

// ---------- TASK RATING ----------

async function rateTask(taskId, userId, rating) {
  await pool.query(
    `INSERT INTO task_ratings (task_id, user_id, rating) VALUES ($1, $2, $3)
     ON CONFLICT (task_id, user_id) DO UPDATE SET rating = EXCLUDED.rating`,
    [taskId, userId, rating]
  );
}

// ---------- TASK REPORTING ----------

const REPORT_THRESHOLD = 3; // itni reports pe task khud-ba-khud flag ho jayega

async function reportTask(taskId, userId, reason) {
  const inserted = await pool.query(
    `INSERT INTO task_reports (task_id, user_id, reason) VALUES ($1, $2, $3)
     ON CONFLICT (task_id, user_id) DO NOTHING RETURNING *`,
    [taskId, userId, reason]
  );
  if (inserted.rows.length === 0) {
    return { alreadyReported: true };
  }

  const countRes = await pool.query('SELECT COUNT(*) FROM task_reports WHERE task_id = $1', [taskId]);
  const reportCount = parseInt(countRes.rows[0].count);

  let flagged = false;
  if (reportCount >= REPORT_THRESHOLD) {
    await pool.query(`UPDATE tasks SET status = 'flagged' WHERE id = $1 AND status = 'active'`, [taskId]);
    flagged = true;
  }

  return { alreadyReported: false, reportCount, flagged };
}

async function adminTaskAction(taskId, action) {
  const task = await getTaskById(taskId);
  if (!task) return null;

  if (action === 'approve') {
    await pool.query(`UPDATE tasks SET status = 'active' WHERE id = $1`, [taskId]);
    return { task, refunded: 0 };
  }

  if (action === 'remove') {
    const remaining = task.target_members - task.current_count;
    const refund = remaining * task.unit_cost;
    await pool.query(`UPDATE tasks SET status = 'removed' WHERE id = $1`, [taskId]);
    if (refund > 0) await addCoins(task.owner_id, refund, 'task_removed_refund');
    return { task, refunded: refund };
  }

  return null;
}

// ---------- AUTO-EXPIRE TASKS ----------

async function expireOldTasks() {
  const expiredRes = await pool.query(
    `SELECT * FROM tasks WHERE status = 'active' AND expires_at < NOW()`
  );

  for (const task of expiredRes.rows) {
    const remaining = task.target_members - task.current_count;
    const refund = remaining * task.unit_cost;
    await pool.query(`UPDATE tasks SET status = 'expired' WHERE id = $1`, [task.id]);
    if (refund > 0) await addCoins(task.owner_id, refund, 'task_expired_refund');
  }

  return expiredRes.rows.length;
}

// ---------- MANUAL UPI DEPOSIT ----------

const DEPOSIT_WINDOW_MINUTES = 3;

async function initiateDeposit(userId, username, amountInr, coinsAmount) {
  // Agar pehle se koi awaiting_proof ya pending request hai to wahi wapas de do (duplicate na bane)
  const existing = await pool.query(
    `SELECT * FROM deposit_requests WHERE user_id = $1 AND status IN ('awaiting_proof','pending')
     ORDER BY created_at DESC LIMIT 1`,
    [userId]
  );
  if (existing.rows.length > 0) return existing.rows[0];

  const res = await pool.query(
    `INSERT INTO deposit_requests (user_id, username, amount_inr, coins_amount, status, expires_at)
     VALUES ($1, $2, $3, $4, 'awaiting_proof', NOW() + INTERVAL '${DEPOSIT_WINDOW_MINUTES} minutes')
     RETURNING *`,
    [userId, username, amountInr, coinsAmount]
  );
  return res.rows[0];
}

async function getDepositById(id) {
  const res = await pool.query('SELECT * FROM deposit_requests WHERE id = $1', [id]);
  return res.rows[0];
}

async function submitDepositProof(requestId, userId, name, utr) {
  const reqRow = await getDepositById(requestId);
  if (!reqRow || reqRow.user_id != userId) return { error: 'Request nahi mili' };
  if (reqRow.status === 'expired') return { error: 'Time khatam ho gaya, dubara try karo' };
  if (reqRow.status !== 'awaiting_proof') return { error: 'Ye request already submit ho chuki hai' };
  if (new Date(reqRow.expires_at) < new Date()) {
    await pool.query(`UPDATE deposit_requests SET status = 'expired' WHERE id = $1`, [requestId]);
    return { error: 'Time khatam ho gaya, dubara try karo' };
  }

  try {
    const updated = await pool.query(
      `UPDATE deposit_requests SET name = $1, utr = $2, status = 'pending'
       WHERE id = $3 RETURNING *`,
      [name, utr, requestId]
    );
    return { success: true, deposit: updated.rows[0] };
  } catch (err) {
    if (err.code === '23505') {
      return { error: 'Ye UTR number already use ho chuka hai' };
    }
    throw err;
  }
}

async function approveDeposit(id) {
  // Atomic — sirf tabhi update hoga jab status abhi bhi 'pending' ho (double-click safe)
  const res = await pool.query(
    `UPDATE deposit_requests SET status = 'approved', resolved_at = NOW()
     WHERE id = $1 AND status = 'pending' RETURNING *`,
    [id]
  );
  if (res.rows.length === 0) return null;

  const deposit = res.rows[0];
  const coins = deposit.coins_amount;
  await addCoins(deposit.user_id, coins, 'deposit_approved');
  await pool.query('UPDATE deposit_requests SET coins_credited = $1 WHERE id = $2', [coins, id]);

  return { ...deposit, coins };
}

async function rejectDeposit(id, reason) {
  const res = await pool.query(
    `UPDATE deposit_requests SET status = 'rejected', reject_reason = $1, resolved_at = NOW()
     WHERE id = $2 AND status = 'pending' RETURNING *`,
    [reason, id]
  );
  return res.rows[0] || null;
}

async function expireOldDepositRequests() {
  const res = await pool.query(
    `UPDATE deposit_requests SET status = 'expired'
     WHERE status = 'awaiting_proof' AND expires_at < NOW() RETURNING id`
  );
  return res.rows.length;
}

async function getMyDeposits(userId) {
  const res = await pool.query(
    `SELECT * FROM deposit_requests WHERE user_id = $1 AND status != 'awaiting_proof'
     ORDER BY created_at DESC`,
    [userId]
  );
  return res.rows;
}

// ---------- WITHDRAW ----------

async function createWithdrawRequest(userId, username, coins, grossRupees, netRupees, upiId) {
  const deducted = await deductCoins(userId, coins, 'withdraw_request');
  if (!deducted) return { error: 'Coins kam hain' };

  const res = await pool.query(
    `INSERT INTO withdraw_requests (user_id, username, coins, gross_rupees, net_rupees, upi_id)
     VALUES ($1, $2, $3, $4, $5, $6) RETURNING *`,
    [userId, username, coins, grossRupees, netRupees, upiId]
  );
  return { success: true, withdraw: res.rows[0] };
}

async function getWithdrawById(id) {
  const res = await pool.query('SELECT * FROM withdraw_requests WHERE id = $1', [id]);
  return res.rows[0];
}

async function approveWithdraw(id) {
  const res = await pool.query(
    `UPDATE withdraw_requests SET status = 'approved', resolved_at = NOW()
     WHERE id = $1 AND status = 'pending' RETURNING *`,
    [id]
  );
  return res.rows[0] || null;
}

async function rejectWithdraw(id, reason) {
  const res = await pool.query(
    `UPDATE withdraw_requests SET status = 'rejected', reject_reason = $1, resolved_at = NOW()
     WHERE id = $2 AND status = 'pending' RETURNING *`,
    [reason, id]
  );
  if (res.rows.length === 0) return null;

  const withdraw = res.rows[0];
  await addCoins(withdraw.user_id, withdraw.coins, 'withdraw_rejected_refund'); // coins wapas kar do
  return withdraw;
}

async function getMyWithdrawals(userId) {
  const res = await pool.query(
    'SELECT * FROM withdraw_requests WHERE user_id = $1 ORDER BY created_at DESC',
    [userId]
  );
  return res.rows;
}

module.exports = {
  pool,
  getOrCreateUser,
  getUser,
  addCoins,
  deductCoins,
  createTask,
  getActiveTasksForUser,
  getTaskById,
  incrementTaskCount,
  recordUserTaskPending,
  markUserTaskVerified,
  markUserTaskLeft,
  getUserTaskStatus,
  getVerifiedUsersByChatId,
  checkAndRegisterDevice,
  getProfileStats,
  setReferrer,
  creditReferralSignupBonus,
  creditReferralCommission,
  getReferralData,
  createGiftCode,
  claimGiftCode,
  createBroadcast,
  getLatestBroadcast,
  getBroadcastById,
  addBroadcastComment,
  getBroadcastComments,
  hasNewBroadcast,
  markBroadcastSeen,
  createSupportTicket,
  getMyTickets,
  replyToTicket,
  rateTask,
  reportTask,
  adminTaskAction,
  expireOldTasks,
  initiateDeposit,
  getDepositById,
  submitDepositProof,
  approveDeposit,
  rejectDeposit,
  expireOldDepositRequests,
  getMyDeposits,
  createWithdrawRequest,
  getWithdrawById,
  approveWithdraw,
  rejectWithdraw,
  getMyWithdrawals
};
