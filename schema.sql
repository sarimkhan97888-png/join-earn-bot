-- =====================================================
-- TELEGRAM JOIN & EARN APP - DATABASE SCHEMA
-- Render PostgreSQL me is file ko ek baar run karna hai
-- =====================================================

-- 1) USERS: har Telegram user ka record + coins
CREATE TABLE IF NOT EXISTS users (
    id              BIGINT PRIMARY KEY,        -- Telegram user ID
    username        TEXT,
    coins           INTEGER DEFAULT 0,
    first_task_used BOOLEAN DEFAULT FALSE,     -- pehla free task use kiya ya nahi
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 2) TASKS: jo channel/group log grow karwana chahte hain
CREATE TABLE IF NOT EXISTS tasks (
    id              SERIAL PRIMARY KEY,
    owner_id        BIGINT REFERENCES users(id),
    chat_id         BIGINT NOT NULL,           -- Telegram chat ID (verify ke baad milta hai)
    chat_username   TEXT,                      -- @username ya invite link
    chat_title      TEXT,
    target_members  INTEGER NOT NULL,          -- kitne members chahiye
    current_count   INTEGER DEFAULT 0,         -- ab tak kitne mile
    status          TEXT DEFAULT 'active',     -- active / completed
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 3) USER_TASKS: kisne kaunsa task join/verify/leave kiya
CREATE TABLE IF NOT EXISTS user_tasks (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES users(id),
    task_id     INTEGER REFERENCES tasks(id),
    status      TEXT DEFAULT 'pending',   -- pending / verified / left
    joined_at   TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, task_id)              -- ek user ek task sirf ek baar kar sakta hai
);

-- 4) COIN_HISTORY: transparency ke liye, kab kitne coin mile/gaye
CREATE TABLE IF NOT EXISTS coin_history (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES users(id),
    amount      INTEGER NOT NULL,         -- +100 ya -1100 waghera
    reason      TEXT,                     -- 'task_verified', 'task_created', 'signup_bonus'
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_user_tasks_user ON user_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_user_tasks_task ON user_tasks(task_id);

-- 5) DEVICE_REGISTRY: ek device sirf ek hi user account se juda rahega
CREATE TABLE IF NOT EXISTS device_registry (
    device_id   TEXT PRIMARY KEY,        -- browser me generate hone wala random ID
    user_id     BIGINT REFERENCES users(id),
    created_at  TIMESTAMP DEFAULT NOW()
);

-- 6) WITHDRAWALS: coins withdraw karne ki requests aur unki history
CREATE TABLE IF NOT EXISTS withdrawals (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES users(id),
    amount      INTEGER NOT NULL,
    status      TEXT DEFAULT 'pending',   -- pending / approved / rejected
    created_at  TIMESTAMP DEFAULT NOW()
);
