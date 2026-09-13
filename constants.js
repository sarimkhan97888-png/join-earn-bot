// constants.js — Deposit packages aur Withdraw rates yahan control hote hain
// Kabhi bhi rates change karne hon to bas yahi file edit karo

const DEPOSIT_PACKAGES = [
  { amount: 10, coins: 30000 },
  { amount: 20, coins: 60000 },
  { amount: 30, coins: 100000 },
  { amount: 40, coins: 150000 },
  { amount: 50, coins: 200000 }
];

const WITHDRAW_MIN_COINS = 50000; // isse kam coins withdraw nahi ho sakte

// Anchor: 50,000 coins = ₹15 gross, fees kaat ke ₹10 net milta hai (33.3% fee)
// Isi rate se koi bhi amount proportionally calculate hota hai
function calcWithdrawGrossRupees(coins) {
  return Math.round((coins / 50000) * 15);
}
function calcWithdrawNetRupees(coins) {
  return Math.floor(coins / 5000); // = gross * (2/3), fees kaat ke jo milega
}

function findDepositPackage(amount, coins) {
  return DEPOSIT_PACKAGES.find(p => p.amount === Number(amount) && p.coins === Number(coins));
}

module.exports = {
  DEPOSIT_PACKAGES,
  WITHDRAW_MIN_COINS,
  calcWithdrawGrossRupees,
  calcWithdrawNetRupees,
  findDepositPackage
};
