# Govt Site Alert Bot — Setup Guide (Hinglish)

Ye system har 15 minute me aapki di hui government websites check karta hai
aur jaise hi koi nayi notification/post aati hai, aapke Telegram par turant
alert (post ka naam + link) bhej deta hai.

## Ek baar ka setup (10 minute)

1. **GitHub account banaiye** (agar pehle se nahi hai): https://github.com/signup

2. **Naya repository banaiye**
   - GitHub par "New repository" par click kijiye
   - Naam kuch bhi de dijiye, jaise `govt-alerts`
   - **Private** rakhiye (safe rahega)
   - "Create repository" dabaiye

3. **Is folder ki saari files us repository me upload kar dijiye**
   - Repository page par "Add file" → "Upload files" par click kijiye
   - Is poore folder (`govt-alert-bot`) ke andar ki saari files aur folders
     (`.github` folder samet) drag-and-drop kar dijiye
   - Neeche "Commit changes" dabaiye

4. **Telegram Token aur Chat ID ko secret ke roop me save kijiye**
   (Isse token public code me nahi dikhega, safe rahega)
   - Repository ke "Settings" tab me jaiye
   - Left side me "Secrets and variables" → "Actions" par click kijiye
   - "New repository secret" dabaiye:
     - Name: `TELEGRAM_TOKEN`   Value: (aapka bot token)
     - "Add secret" dabaiye
   - Dobara "New repository secret" dabaiye:
     - Name: `TELEGRAM_CHAT_ID`   Value: (aapki chat id)
     - "Add secret" dabaiye

5. **Workflow ko chalu kijiye**
   - Repository ke "Actions" tab me jaiye
   - Agar ek button aaye "I understand my workflows, go ahead and enable
     them", usse click kar dijiye
   - "Govt Site Monitor" workflow par click kijiye → "Run workflow" se
     ek baar manually chala kar test kar lijiye

Bas ho gaya! Ab ye har 15 minute me apne aap chalega (GitHub free tier me
ye bilkul free hai) aur jaise hi kisi website par nayi
notice/recruitment/result/exam post aayegi, aapko Telegram par alert mil
jayega.

## Kaise kaam karta hai (short me)

- Pehli baar chalne par ye har website ka current content "yaad" kar leta
  hai (koi alert nahi bhejta, kyunki wo sab purana content hai)
- Uske baad har run me sirf **nayi** cheezein dikhne par hi Telegram
  message aayega
- State (yaad rakha hua data) `state.json` file me save hota hai, jo GitHub
  par khud-ba-khud update hota rehta hai

## Zaroori note

- Kuch government websites structure me alag hoti hain, isliye shuru me
  kisi site se alerts thoda kam/zyada aa sakte hain — agar aisा ho to
  mujhe bata dijiye, main `monitor.py` ko us specific site ke liye tune
  kar dunga.
- X (Twitter) accounts (UPSC, SSC, PIB, UGC-NET) is script me shamil nahi
  hain kyunki Twitter/X ab free scraping allow nahi karta. Iske liye alag
  se ek RSS-bridge ya paid service use karni hogi — agar chahiye to bata
  dijiye, main uska tareeka bhi bana dunga.
