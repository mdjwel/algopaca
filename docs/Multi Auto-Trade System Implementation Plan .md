# Multi Auto-Trade System Implementation Plan

## ওভারভিউ (Overview)
এই সিস্টেমের মাধ্যমে ব্যবহারকারী একই সাথে একাধিক টিকারের (Stocks/ETFs) জন্য আলাদা আলাদা স্ট্র্যাটেজি (যেমন: SMA Crossover, Buy The Dip, AI Momentum, Day Trading, Long/Short Trend অথবা Custom Engine) দিয়ে মাল্টি অটো-ট্রেড (Multi Auto-Trade) চালু রাখতে পারবেন।
বিশেষ করে **Positions** পেইজ থেকে যেকোনো একটি বা একাধিক টিকার সিলেক্ট করে সরাসরি স্ট্র্যাটেজি নির্ধারণ করে অটো-ট্রেড শুরু করা যাবে, লাইভ স্ট্যাটাস পর্যবেক্ষণ করা যাবে এবং যেকোনো সময় বন্ধ (Stop) করার সুযোগ থাকবে।

---

## ইউজার রিভিউ বা গুরুত্বপূর্ণ সিদ্ধান্ত (User Review Required)
- **মাল্টি-ট্রেড প্রসেসিং আর্কিটেকচার**: প্রতিটি টিকারের অটো-ট্রেড নিজস্ব থ্রেড ও কনফিগ নিয়ে স্বাধীনভাবে ব্যাকগ্রাউন্ডে চলবে (Isolated Worker Loop per Ticker)। এতে একটি টিকারের ট্রেডিং অন্যটির উপর নির্ভর করবে না।
- **টিকার কলিশন প্রটেকশন**: একই টিকারে একই সময়ে দুটি ভিন্ন স্ট্র্যাটেজির অর্ডার কনফ্লিক্ট এড়াতে একটি টিকারের জন্য একটিই সক্রিয় রানার থাকবে (`_runners` ম্যাপ সিম্বল-কী দিয়ে রাখা)। একই টিকারে আবার স্টার্ট দিলে ডিফল্টে (`replace_existing=True`) পুরোনো রানার আগে থামিয়ে নতুন কনফিগে রিস্টার্ট হয় — মডালের "Update Auto-Trade" এভাবেই কাজ করে।
- **সাপোর্টেড স্ট্র্যাটেজি সীমা**: একটি রানার শুধু সিঙ্গেল-সিম্বল ইঞ্জিন চালাতে পারে — `sma`, `dip`, `ai`, `day`, `ls`। `pair` (Pair Trading) একসাথে দুটি লেগ ট্রেড করে বলে পার-টিকার রানারে সাপোর্টেড নয়: কাস্টম ইঞ্জিন ড্রপডাউনে pair-ভিত্তিক ইঞ্জিন দেখানো হয় না, আর সরাসরি API-তে পাঠালে `400` রেসপন্স আসে।
- **পজিশন পেইজ থেকে সরাসরি নিয়ন্ত্রণ**: পজিশন টেবিলের প্রতি সারিতে "Auto Trade" অ্যাকশন বাটন থাকবে, এবং একাধিক টিকার চেকবক্স দিয়ে সিলেক্ট করলে ফ্লোটিং বার থেকে "Auto Trade Selected" বাটনে ক্লিক করে এক ক্লিকে সবার জন্য স্ট্র্যাটেজি চালু করা যাবে।

---

## প্রস্তাবিত পরিবর্তনসমূহ (Proposed Changes)

### ১. ব্যাকএন্ড মাল্টি-ট্রেড কোর ইঞ্জিন (`bot/multi_trader.py`)
#### [NEW] [multi_trader.py](file:///Users/ehjewel/Documents/Git%20projects/algopaca/bot/multi_trader.py)
- `TickerRunner`: একটি নির্দিষ্ট টিকারের জন্য আইসোলেটেড অটো-ট্রেড ওয়ার্কার ক্লাস।
  - অ্যাট্রিবিউটস: `id`, `symbol`, `strategy_mode`, `engine_name`, `custom_engine_id`, `settings`, `timeframe`, `poll_seconds`, `status`, `created_at`, `started_at`, `stopped_at`, `last_run_at`, `last_signal`, `last_price`, `last_reason`, `cycles_count`, `trades_count`, `error`।
  - `status` লাইফসাইকেল: `idle` → `running` → `stopping` → `stopped`; সাইকেলে ব্যর্থতা হলে `error` ফিল্ডে বার্তা জমা হয় (লুপ চলতে থাকে, পরের সাইকেলে সফল হলে ক্লিয়ার হয়)।
  - প্রতিটি রানারের নিজস্ব `threading.Event()` থাকবে যাতে যেকোনো সময় তাৎক্ষণিক ও পরিষ্কারভাবে লুপ বন্ধ করা যায়; পোল ইন্টারভালের অপেক্ষাটাও ইন্টারাপ্টিবল (`_stop_event.wait`)।
  - প্রতিটি সাইকেলে টিকারের জন্য উপযুক্ত বট (`AiTradingBot`, `TradingBot`, `DayTradingBot`, `LsTradingBot`) রান করে সিগন্যাল ও অর্ডার প্রসেস করবে এবং `_expand_leg_history` / `_record_trade_history` দিয়ে ট্রেড হিস্ট্রিতে রেকর্ড করবে।
  - **কনফিগ রেজল্যুশন** (`_build_config`): `AppState._base_config()`-এর উপরে রানারের `symbol` / `symbols` / `strategy_mode` / `poll_seconds` / টাইমফ্রেম বসানো হয়, এরপর `settings`-এর যে কী-গুলো `Config`-এর আসল ফিল্ড সেগুলো ফিল্ডের নিজস্ব টাইপে কোয়ার্স করে অ্যাপ্লাই হয়। `api_key`, `secret_key`, `paper`, `symbol`, `symbols`, `strategy_mode` ও AI প্রোভাইডার কী-গুলো `PROTECTED_CONFIG_FIELDS` হিসেবে ব্লক করা — ইউজার সেটিংস থেকে এগুলো ওভাররাইড করা যাবে না।
  - **কাস্টম ইঞ্জিন রেজল্যুশন**: `custom_engine_id` দিলে ইঞ্জিনের `base_engine` (বা `choices.strategy_mode`) থেকেই স্ট্র্যাটেজি মোড ঠিক হয় এবং সেভ করা `choices` সেটিংসে মার্জ হয় — অটো-ট্রেড পেইজে ইঞ্জিন লোড করলে যে আচরণ, সেটির সাথে সঙ্গতিপূর্ণ। কলার সরাসরি কোনো স্ট্যান্ডার্ড মোড পাঠালে সেটিই প্রাধান্য পায়; কিছুই না পাঠালে ফলব্যাক `sma`।
  - `day` মোডে টাইমফ্রেম সবসময় `resolve_day_timeframe()` দিয়ে ইন্ট্রাডে বারে বাধ্য করা হয়।
- `MultiTradeManager`: ব্যবহারকারীর সমস্ত সক্রিয় টিকার রানার পরিচালনা করার ক্লাস।
  - `start_runner(symbol, strategy_mode="", custom_engine_id=None, engine_name=None, settings=None, replace_existing=True)`
  - `start_batch(symbols, strategy_mode="", custom_engine_id=None, engine_name=None, settings=None)`
  - `stop_runner(symbol_or_id, wait=True)` — সিম্বল বা রানার `id` — যেকোনোটি দিয়ে থামানো যায়।
  - `stop_all(wait=True)` — কতগুলো রানার থামানো হলো তার সংখ্যা রিটার্ন করে।
  - `get_runner(symbol)` / `get_runner_summary(symbol)`
  - `list_runners(active_only=False)` / `list_active()`
  - `active_symbols_map()` — সিম্বল → রানার স্ন্যাপশট ম্যাপ।

---

### ২. স্টেট ও পজিশন ওভারভিউ ইন্টিগ্রেশন (`bot/web_state.py`)
#### [MODIFY] [web_state.py](file:///Users/ehjewel/Documents/Git%20projects/algopaca/bot/web_state.py)
- `AppState`-এ `self.multi_trader = MultiTradeManager(self)` যুক্ত করা।
- `positions_overview()` মেথডে প্রতিটি পজিশনের তথ্যের সাথে অটো-ট্রেড স্ট্যাটাস (`is_auto_trading`, `auto_trade` স্ন্যাপশট) এবং রেসপন্সে `active_auto_trades` ও `all_auto_trades` তালিকা যোগ করা।
- `snapshot()`-এ `active_auto_trades` ও `active_auto_trades_count` যোগ করা, যাতে অটো-ট্রেড পেইজের পোলিং থেকেও রানিং রানার সংখ্যা জানা যায়।
- `AppState` র‍্যাপার মেথডস: `start_multi_auto_trade(symbols, strategy_mode="", custom_engine_id, engine_name, settings)` (কমা/সেমিকোলন-সেপারেটেড স্ট্রিং বা লিস্ট দুটোই নেয়), `stop_multi_auto_trade(symbol_or_id)`, `stop_all_multi_auto_trades()`, `list_multi_auto_trades(active_only=False)`।
- `UserStateRegistry.remove()`-এ ইউজারের স্টেট সরানোর সময় লুপ বন্ধের পাশাপাশি `multi_trader.stop_all()` কল করে সব রানার নিরাপদে থামানো।

---

### ৩. API রাউটস (`bot/webapp.py`)
#### [MODIFY] [webapp.py](file:///Users/ehjewel/Documents/Git%20projects/algopaca/bot/webapp.py)
রিকোয়েস্ট মডেল: `MultiTradeStartIn` (`symbols`, `strategy_mode` — খালি রাখলে কাস্টম ইঞ্জিনের base engine ব্যবহৃত হয়, `custom_engine_id`, `engine_name`, `settings`, `bar_timeframe`, `poll_seconds`, `trade_qty`, `trade_notional`, `size_mode`, `stop_loss_pct`) এবং `MultiTradeStopIn` (`symbol` বা `id`)।

নতুন এন্ডপয়েন্টসমূহ:
- `GET /api/auto-trade/multi`: সব রানারের স্ট্যাটাস — `runners`, `active_runners`, `active_symbols`।
- `POST /api/auto-trade/multi/start`: এক বা একাধিক টিকারের জন্য নির্দিষ্ট স্ট্র্যাটেজি দিয়ে অটো-ট্রেড শুরু করবে; ইনভ্যালিড বা আনসাপোর্টেড মোডে `400` রিটার্ন করবে।
- `POST /api/auto-trade/multi/stop`: নির্দিষ্ট টিকার (`symbol`) বা রানার (`id`)-এর অটো-ট্রেড বন্ধ করবে।
- `POST /api/auto-trade/multi/stop-all`: সব সক্রিয় অটো-ট্রেড একসাথে বন্ধ করবে এবং `stopped_count` ফেরত দেবে।

---

### ৪. পজিশন পেইজ UI (`web/positions.html`, `web/static/js/positions.js`, `web/static/css/positions.css`)
#### [MODIFY] [positions.html](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/positions.html)
- **Active Auto-Trades Panel (`pos-autotrades-section`)**: টেবিলের উপরে রানিং অটো-ট্রেডগুলোর স্ট্যাটাস বার — সক্রিয় টিকারের নাম, ইঞ্জিন, শেষ সিগন্যাল ও প্রাইস, প্রতিটির জন্য [Stop] বাটন এবং হেডারে [Stop All Auto-Trades] (`btn-stop-all-autotrades`)।
- **Auto-Trade Modal Dialog (`pos-autotrade-modal`)**:
  - সিলেক্টেড টিকারের চিপ-তালিকা (`pos-autotrade-targets-chips`)।
  - স্ট্র্যাটেজি সিলেক্টর দুটি ট্যাবে: **Standard** (`pos-standard-strategy-select` — SMA Crossover, Buy The Dip, AI Momentum, Day Trading, Long/Short Trend) এবং **Custom** (`pos-custom-engine-select` — সেভ করা কাস্টম ইঞ্জিন)।
  - টাইমফ্রেম, পোল ইন্টারভাল, এবং ট্রেড সাইজিং (Qty বা Notional $) সেটিংস।
  - রানিং অবস্থায় লাইভ কার্ড (ইঞ্জিন, সিগন্যাল, প্রাইস, সাইকেল, আপটাইম, কারণ) এবং "Stop Auto-Trade" বাটন; সাবমিট বাটনের লেবেল "Start Auto-Trade" / "Update Auto-Trade" হিসেবে বদলায়।
- **Floating Batch Bar**: চেকবক্স সিলেক্ট করলে "Close Selected"-এর পাশে "Auto Trade Selected" (`btn-batch-autotrade`) বাটন।

#### [MODIFY] [positions.js](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/static/js/positions.js)
- টেবিল রো ও মোবাইল কার্ডে রানিং অটো-ট্রেডের জন্য পালসিং ব্যাজ (`pos-row-autotrade-badge`) রেন্ডার করা — ব্যাজে রানারের `engine_name` (যেমন "AI Momentum" বা কাস্টম ইঞ্জিনের নাম) দেখানো হয়।
- প্রতি রো-এর অ্যাকশনস লিস্টে "Auto Trade" (রানিং হলে "Auto: On") বাটন যোগ করা।
- ফ্লোটিং ব্যাচ বারের "Auto Trade Selected" ক্লিকে একাধিক টিকার সহ মডাল ওপেন ও ব্যাচ স্টার্টের ব্যবস্থা করা।
- সক্রিয় অটো-ট্রেড বার থেকে তাৎক্ষণিক স্টপ এবং স্টপ-অল হ্যান্ডলার যোগ করা।
- কাস্টম ট্যাব থেকে স্টার্ট করলে `strategy_mode` খালি পাঠানো হয় যাতে ইঞ্জিনের নিজস্ব base engine ব্যাকএন্ডে রেজলভ হয়, এবং pair-ভিত্তিক ইঞ্জিন ড্রপডাউন থেকে ফিল্টার করা হয়।

#### [MODIFY] [positions.css](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/static/css/positions.css)
- অ্যাক্টিভ অটো-ট্রেড প্যানেল, পালসিং লাইভ ব্যাজ, মডাল স্টাইলিং ও রেসপন্সিভ ডিজাইন যুক্ত করা।

---

### ৫. আই১৮এন ভাষা সমর্থন ও অটো-ট্রেড পেইজ
#### [MODIFY] `web/static/lang/*.json`
- নতুন সব বাটন লেবেল, টেক্সট ও মেসেজের কী ডেস্কের পাঁচটি ভাষাতেই যুক্ত করা — [en.json](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/static/lang/en.json), [bn.json](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/static/lang/bn.json), [es.json](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/static/lang/es.json), [fr.json](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/static/lang/fr.json), [hi.json](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/static/lang/hi.json) (`bot/config.py`-এর `LANGUAGES` অনুযায়ী)।

#### [MODIFY] [auto-trade.html](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/auto-trade.html), [auto-trade.js](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/static/js/auto-trade.js), [auto-trade.css](file:///Users/ehjewel/Documents/Git%20projects/algopaca/web/static/css/auto-trade.css)
- **Multi Auto-Trade Monitoring Panel (`multi-runners-card`)**: অটো-ট্রেড পেইজেও রানিং টিকার রানারগুলো দেখা ও থামানো যায় — পেন্ডিং অ্যাপ্রুভাল প্যানেলের নিচে বসানো।
  - প্রতি রানারের রো-তে: সিম্বল, ইঞ্জিন নাম, শেষ সিগন্যাল ব্যাজ, শেষ প্রাইস, টাইমফ্রেম, পোল ইন্টারভাল, সাইকেল/ট্রেড কাউন্ট, আপটাইম এবং শেষ কারণ (এরর থাকলে সেটি)।
  - প্রতি রো-তে [Stop] বাটন (`POST /api/auto-trade/multi/stop`) এবং হেডারে [Stop All Auto-Trades] (`btn-stop-all-runners` → `POST /api/auto-trade/multi/stop-all`); সেই সাথে **Positions** পেইজে যাওয়ার লিংক, কারণ নতুন রানার সেখান থেকেই চালু হয়।
  - ডেটা আসে পেইজের নিয়মিত `snapshot()` পোলিং থেকে (`active_auto_trades`, `active_auto_trades_count`) — `render()`-এ `renderMultiAutoTrades(state)` কল হয়, কোনো রানার না থাকলে প্যানেল হিডেন থাকে।

---

## ভেরিফিকেশন প্ল্যান (Verification Plan)

### অটোমেটেড টেস্টস (Automated Tests)
- `tests/test_multi_auto_trade.py` — যা কভার করা হয়েছে:
  1. একক টিকার ও ব্যাচ (একাধিক টিকার) দিয়ে রানার শুরু করা, স্ন্যাপশট ফিল্ড ও ম্যানেজার কোয়েরি (`get_runner_summary`, `active_symbols_map`) যাচাই।
  2. কাস্টম ইঞ্জিনের `base_engine` ও `choices` রেজল্যুশন, এবং কলারের এক্সপ্লিসিট মোড কাস্টম ইঞ্জিনের বেসকে ওভাররাইড করে কিনা।
  3. আনসাপোর্টেড মোড (`pair`, অজানা মোড) `ValueError` দিয়ে রিজেক্ট হওয়া।
  4. `_build_config()`-এ সেটিংস পাস-থ্রু ও প্রোটেক্টেড ফিল্ড (symbol / API key) সুরক্ষা, এবং `day` মোডে ইন্ট্রাডে টাইমফ্রেম বাধ্যকরণ।
  5. রানার স্টপ ও স্টপ-অল মেকানিজম (থ্রেড ক্লিনআপ ও স্ট্যাটাস আপডেট)।
  6. `AppState` র‍্যাপার মেথডস, `positions_overview()`-এ `is_auto_trading` / `auto_trade` অ্যানোটেশন, এবং অটো-ট্রেড পেইজের প্যানেল যে ফিল্ডগুলো পড়ে সেগুলো `snapshot()`-এ আসছে কিনা।
  7. API লাইফসাইকেল টেস্ট: `POST /api/auto-trade/multi/start` → `GET /api/auto-trade/multi` → `POST /api/auto-trade/multi/stop` → `POST /api/auto-trade/multi/stop-all`।
- কমান্ড: `uv run --with pytest pytest tests/test_multi_auto_trade.py`
- পূর্ণ সুইট: `uv run --with pytest pytest tests/`

### ম্যানুয়াল ভেরিফিকেশন (Manual Verification)
1. ওয়েব সার্ভার চালু করে `/positions` পেইজে যাওয়া।
2. কোনো পজিশনের রো থেকে "Auto Trade" বাটনে ক্লিক করে স্ট্র্যাটেজি (যেমন: SMA বা AI) সিলেক্ট করে স্টার্ট করা।
3. টেবিল রো-তে ইঞ্জিন-নামের অ্যাক্টিভ ব্যাজ এবং উপরে অ্যাক্টিভ অটো-ট্রেড বারে টিকারের লাইভ স্ট্যাটাস দেখা যাচ্ছে কিনা তা যাচাই করা।
4. কাস্টম ট্যাব থেকে একটি কাস্টম ইঞ্জিন সিলেক্ট করে স্টার্ট করে যাচাই করা যে ব্যাজ/লাইভ কার্ডে ইঞ্জিনের নিজস্ব বেস স্ট্র্যাটেজি (যেমন AI) চলছে, SMA নয়।
5. একাধিক পজিশন চেকবক্স দিয়ে সিলেক্ট করে ফ্লোটিং বার থেকে "Auto Trade Selected" দিয়ে একসাথে ব্যাচ স্টার্ট করা।
6. একই টিকারে আবার মডাল খুলে সেটিংস বদলে "Update Auto-Trade" দিলে রানার নতুন কনফিগে রিস্টার্ট হচ্ছে কিনা দেখা।
7. `/auto-trade` পেইজে গিয়ে "Active Auto-Trades" প্যানেলে একই রানারগুলো (সিগন্যাল, সাইকেল, আপটাইম সহ) দেখা যাচ্ছে কিনা এবং সেখান থেকেও [Stop] / [Stop All Auto-Trades] কাজ করছে কিনা যাচাই করা।
8. "Stop" ও "Stop All Auto-Trades" বাটনে ক্লিক করে অটো-ট্রেড তাৎক্ষণিক বন্ধ হচ্ছে কিনা এবং কোনো রানার না থাকলে দুই পেইজেই প্যানেল লুকিয়ে যাচ্ছে কিনা পরীক্ষা করা।
