// db.js — Saari database queries yahan hain (ek jagah, easy to manage)

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: { rejectUnauthorized: false } // Render Postgres ke liye zaroori
});

// ---------- USERS ----------

async function getOrCreateUser(id, username) {
  const existing = await pool.query('SELECT * FROM users WHERE id = $1', [id]);
  if (existing.rows.length > 0) return existing.rows[0];

  const created = await pool.query(
    'INSERT INTO users (id, username) VALUES ($1, $2) RETURNING *',
    [id, username]
  );
  return created.rows[0];
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

async function createTask(ownerId, chatId, chatUsername, chatTitle, targetMembers) {
  const res = await pool.query(
    `INSERT INTO tasks (owner_id, chat_id, chat_username, chat_title, target_members)
     VALUES ($1, $2, $3, $4, $5) RETURNING *`,
    [ownerId, chatId, chatUsername, chatTitle, targetMembers]
  );
  return res.rows[0];
}

async function getActiveTasksForUser(userId) {
  // Un tasks ko dikhao jo active hain, jinke owner khud user nahi hai,
  // aur jo user pehle se verified/pending nahi kar chuka
  const res = await pool.query(
    `SELECT t.* FROM tasks t
     WHERE t.status = 'active'
       AND t.owner_id != $1
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
  getProfileStats
};
