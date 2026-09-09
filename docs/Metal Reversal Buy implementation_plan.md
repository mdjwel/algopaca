# মেটাল স্ট্র্যাটেজিতে Stop Loss হিট হওয়ার পর তাৎক্ষণিক Reversal Buy লজিক

ইকোনমিক ডেটা রিলিজের সময় (যেমন CPI, NFP, PPI বা FOMC) গোল্ড ও সিলভারের মতো প্রেশাস মেটালে ভোলাটিলিটি বৃদ্ধি পায়। যদি কোনো ট্রেডার শর্ট (Short/Sell) পজিশনে থাকেন এবং ডেটা শর্টের প্রতিকূলে (মেটালের অনুকূলে) আসে, তখন মূল্যের ঊর্ধ্বগতির কারণে স্টপ লস হিট হয়। এই ইমপ্লিমেন্টেশনের উদ্দেশ্য হলো—শর্ট পজিশনের স্টপ লস হিট/ফিল হওয়ার সাথে সাথে কোনো বিলম্ব ছাড়াই একটি **Reversal Buy** (Long) অর্ডার সাবমিট করা এবং নতুন লং পজিশনের জন্য সুরক্ষামূলক স্টপ লস নির্ধারণ করা।

---

## User Review Required

> [!IMPORTANT]
> **স্লিপেজ ও হুইপস (Spread & Whipsaw) নিয়ন্ত্রণ:**
> ডেটা রিলিজের সময় (বিশেষ করে প্রি-মার্কেট ৮:৩০ AM ET-তে) স্প্রেড অনেক বড় হতে পারে। Reversal Buy অর্ডারটি সাবমিট করার সময় একটি মার্কেট/মার্কেটেবল লিমিট বাফার ব্যবহার করা হবে যাতে অতিরিক্ত স্লিপেজে অর্ডার আটকে না থাকে।

> [!NOTE]
> **এক্সটেন্ডেড আওয়ার্স সামঞ্জস্য (24h/Pre-market Support):**
> প্রধান অর্থনৈতিক ডেটা (যেমন CPI ও Non-Farm Payrolls) সকাল ৮:৩০ AM ET-তে রিলিজ হয়, যখন রেগুলার স্টক মার্কেট বন্ধ থাকে। তাই Reversal Buy এবং তার পরবর্তী স্টপ লসকে অবশ্যই Algopaca-এর সিন্থেটিক অর্ডার ইঞ্জিন (`extended_hours=True`) সমর্থন করতে হবে।

---

## Open Questions

> [!IMPORTANT]
> ১. **রিভার্সাল সাইজ (Reversal Qty):**
> রিভার্স বাই করার সময় কি পূর্বে যে পরিমাণ শর্ট পজিশন কভার করা হয়েছে ঠিক সেই সমান পরিমাণ শেয়ার (`reversal_qty = closed_qty`) কেনা হবে, নাকি স্ট্র্যাটেজির ডিফল্ট `trade_qty` বা সাইজিং মোড অনুযায়ী নির্ধারিত হবে? *(প্রস্তাবিত: ডিফল্ট হিসেবে শর্ট কভারের সমান লট সাইজ কেনা)*।
>
> ২. **নতুন লং পজিশনের স্টপ লস দূরত্ব (Stop Loss Buffer):**
> রিভার্সাল বাই এক্সিকিউট হওয়ার পর নতুন লং পজিশনের সুরক্ষায় কি এন্ট্রির `-0.8%` বা `-1.0%` নিচে তাৎক্ষণিক নতুন প্রোটেক্টিভ স্টপ বসানো হবে? *(প্রস্তাবিত: `stop_buffer_pct` অনুযায়ী ০.৮% নিচে স্টপ-লিমিট আর্ম করা)*।

---

## Proposed Changes

 Group files by component and order logically.

---

### 1. Synthetic Order Store & Persistence

#### [MODIFY] [synthetic_order_store.py](file:///Users/ehjewel/Documents/Git%20projects/algopaca/bot/synthetic_order_store.py)
- `_PERSISTED_FIELDS` টাপলে রিভার্সাল সংক্রান্ত নতুন ফিল্ড যুক্ত করা:
  - `"reversal_buy"`: ফ্ল্যাগ নির্দেশ করবে স্টপ হিট হলে রিভার্স বাই হবে কিনা।
  - `"reversal_qty"`: রিভার্স বাইয়ের শেয়ার সংখ্যা।
  - `"reversal_event_title"`: যে ইভেন্টের কারণে প্রোটেকশন সক্রিয় হয়েছিল তার নাম।
- `_sanitize` মেথডে নতুন ফিল্ডগুলো প্রিজার্ভ করা।

---

### 2. Metals Intelligence & Event Protection

#### [MODIFY] [metals_intel.py](file:///Users/ehjewel/Documents/Git%20projects/algopaca/bot/metals_intel.py)
- `protect_metals_position_before_event` ফাংশনে `reversal_buy: bool = True` প্যারামিটার সাপোর্ট দেওয়া।
- শর্ট পজিশনের ক্ষেত্রে (`is_short == True`) সিন্থেটিক স্টপ অর্ডারে `reversal_buy=True`, `reversal_qty=abs(pos_qty)`, এবং `reversal_event_title=event_title` পাস করা।
- রিটার্নকৃত `armed_info` ডিকশনারিতে রিভার্সাল স্ট্যাটাস যুক্ত করা।

---

### 3. WebState Synthetic Engine & Reversal Execution

#### [MODIFY] [web_state.py](file:///Users/ehjewel/Documents/Git%20projects/algopaca/bot/web_state.py)
- `sync_strategy_stop`:
  - নতুন আর্গুমেন্ট গ্রহণ করা: `reversal_buy: bool = False`, `reversal_qty: float | None = None`, `reversal_event_title: str | None = None`।
  - সিন্থেটিক অর্ডার তৈরি বা আপডেটের সময় এই ফিল্ডগুলো স্টোর করা।
- `_execute_reversal_buy(order_id, snapshot, service, fill_price)` মেথড যোগ করা:
  1. `symbol`, `reversal_qty` সংগ্রহ করা।
  2. মার্কেট রেগুলার আওয়ারে থাকলে রেগুলার বাই অর্ডার এবং প্রি-মার্কেট/এক্সটেন্ডেড আওয়ারে থাকলে `extended_hours=True` লিমিট অর্ডার সাবমিট করা।
  3. সফল হলে `_record_trade_history`-তে `REVERSAL_BUY` সিগন্যাল ও বিস্তারিত বিবরণ সহ রেকর্ড রাখা।
  4. নতুন লং পজিশনের জন্য সাথে সাথে নতুন প্রোটেক্টিভ স্টপ আর্ম করা (`sync_strategy_stop` সহ side="sell")।
- `_advance_synthetic_order`:
  - যখন শর্ট কভারের লিমিট অর্ডার `status == "filled"` হবে:
    - যদি `snapshot.get("reversal_buy")` সত্য হয়, তবে তাৎক্ষণিকভাবে `_execute_reversal_buy` কল করে লং পজিশন ওপেন করা।

---

### 4. Multi-Trader & Configuration

#### [MODIFY] [config.py](file:///Users/ehjewel/Documents/Git%20projects/algopaca/bot/config.py)
- `Config` ডেটাক্লাসে `metals_reversal_buy_on_stop: bool = True` কনফিগারেশন ফিল্ড যোগ করা।

#### [MODIFY] [multi_trader.py](file:///Users/ehjewel/Documents/Git%20projects/algopaca/bot/multi_trader.py)
- `TickerRunner._build_config`:
  - `settings` থেকে `reversal_buy_on_stop` অথবা `metals_reversal_buy_on_stop` সেটিংস গ্রহণ করে কনফিগারেশন ওভাররাইডে পাস করা।

---

## Verification Plan

### Automated Tests
1. **মেটালস ইন্টেলিজেন্স টেস্ট:**
   - `./.venv/bin/python -m unittest tests/test_metals_strategy.py`
   - ভেরিফাই করা যে শর্ট পজিশনে `protect_metals_position_before_event` কল করলে `reversal_buy=True` এবং রিভার্সাল ডেটা সহ স্টপ আর্ম হয়।
2. **সিন্থেটিক অর্ডার ও রিভার্সাল এক্সিকিউশন টেস্ট:**
   - `./.venv/bin/python -m unittest tests/test_synthetic_extended_orders.py`
   - নতুন টেস্ট কেস যুক্ত করা:
     - শর্ট পজিশন কভার হওয়ার পর `_execute_reversal_buy` কল হওয়া এবং বাই অর্ডার যাওয়া।
     - ট্রেড হিস্টোরিতে `REVERSAL_BUY` লগ হওয়া।
     - নতুন লং পজিশনের জন্য প্রোটেক্টিভ স্টপ রেজিস্টার হওয়া।

### Manual Verification
- কোডের লজিক ও ব্রোকার মেকানিজম ট্রেস করে নিশ্চিত হওয়া যে প্রি-মার্কেট ও রেগুলার মার্কেট উভয় সময়েই রিভার্সাল নিরবচ্ছিন্নভাবে সম্পন্ন হয়।
