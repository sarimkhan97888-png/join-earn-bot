# Telegram Join & Earn Mini App

## 📁 Files Kya Kaam Karti Hain
- `bot.js` → Telegram bot ki logic (join check, leave detect)
- `db.js` → Database ke saare functions
- `server.js` → Web server (bot + mini app + API sab isi se chalta hai)
- `schema.sql` → Database tables banane ke liye
- `webapp/index.html` → Mini app ka frontend (jo user ko dikhta hai)

---

## 🚀 STEP-BY-STEP DEPLOYMENT (Bilkul shuru se)

### Step 1: Telegram Bot Banao
1. Telegram me `@BotFather` ko message karo
2. `/newbot` bhejo, naam do
3. Jo **token** milega usko copy karke rakh lo (baad me chahiye hoga)
4. `/mybots` → apna bot chuno → **Bot Settings** → **Menu Button** → yahan se baad me Mini App URL set karoge

### Step 2: GitHub Pe Code Daalo
1. GitHub.com pe jaake naya repository banao (e.g. `join-earn-bot`)
2. Ye saari files (jo maine banayi hain) us repo me upload karo
   - GitHub website pe hi "Add file → Upload files" se bhi kar sakte ho, coding zaroori nahi
3. `.env` file mat upload karna (usme secret token hota hai) — sirf `.env.example` rehne do

### Step 3: Render Pe Database Banao
1. [render.com](https://render.com) pe account banao (free hai)
2. Dashboard → **New** → **PostgreSQL**
3. Naam do, Free plan chuno, Create karo
4. Bन jaane ke baad **"Internal Database URL"** ya **"External Database URL"** copy kar lo
5. Render dashboard me hi is database ko open karo → **"Connect"** tab → wahan se ek SQL shell/psql command milega
   - Ya phir apne computer se `psql` install karke connect karo
6. `schema.sql` file ka pura content copy karke wahan run kar do (isse saari tables ban jayengi)

### Step 4: Render Pe Web Service Banao (Bot Deploy)
1. Render Dashboard → **New** → **Web Service**
2. Apna GitHub repo connect karo (permission dena hoga)
3. Settings:
   - **Build Command:** `npm install`
   - **Start Command:** `node server.js`
   - **Instance Type:** Free (testing ke liye) ya Starter $7/mo (production ke liye — free wala sleep ho jata hai)
4. **Environment Variables** section me ye sab add karo (`.env.example` dekh ke):
   ```
   BOT_TOKEN = (Step 1 wala token)
   DATABASE_URL = (Step 3 wala database URL)
   WEBHOOK_URL = https://your-service-name.onrender.com   (Render khud batayega ye URL, deploy hone ke baad edit kar dena)
   REQUIRED_GROUP = your_group_username (bina @ ke)
   REQUIRED_CHANNEL = your_channel_username (bina @ ke)
   ADMIN_ID = tumhari telegram user ID
   ```
5. **Create Web Service** dabao — deploy shuru ho jayega
6. Deploy complete hone ke baad Render tumhe ek URL dega jaisे `https://join-earn-bot.onrender.com`
7. Us URL ko wapas `WEBHOOK_URL` variable me daal ke **save** karo (dobara deploy hoga automatically)

### Step 5: Mini App URL Bot Me Set Karo
1. `@BotFather` → `/mybots` → apna bot chuno
2. **Bot Settings → Menu Button → Configure Menu Button**
3. URL do: `https://your-service-name.onrender.com/webapp`
4. Button ka naam do jaise "Open App"

### Step 6: Bot Ko Admin Banao (Mandatory Channels Me)
- Jo 2 mandatory GC/Channel tumne `.env` me daale hain, un dono me bot ko **admin** banao
  (kam se kam "members dekhna" wali permission on honi chahiye)

### Step 7: Test Karo
1. Apne bot ko Telegram me kholo, `/start` bhejo
2. Join check screen aani chahiye
3. Join karke "Maine Join Kar Liya" dabao
4. Mini App khulni chahiye

---

## ⚙️ Coin Economics (Jo Tumne Bataya)
- Naya user jab **pehli baar apna task** banata hai → **500 coins FREE** milte hain (5 members ke barabar)
- **Dusri baar se**, har member ke liye **110 coins** katenge
- Jo user **verify karke join** karta hai usko **100 coins** milte hain
- Difference (110-100 = 10 coins/member) app ka margin hai

Ye sab already `bot.js` ke top pe variables me set hai:
```js
const SIGNUP_BONUS = 500;
const COST_PER_MEMBER = 110;
const REWARD_PER_JOIN = 100;
```
Agar amount change karna ho to bas yahi numbers badal dena.

---

## ⚠️ Important Notes
1. **Leave Detection** kaam karne ke liye bot ko **har task ke channel/group me admin** hona zaroori hai (jo owner khud karta hai task banate waqt)
2. Free Render instance kuch der inactive rehne pe **sleep** ho jata hai — production ke liye paid instance behtar hai warna webhook miss ho sakte hain
3. Agar koi feature add/change karwana ho (jaise: leave hone par coins wapas lena, referral system, withdraw system) — wo iske upar easily add ho sakta hai
