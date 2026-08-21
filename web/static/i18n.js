/**
 * Internationalization (i18n) Engine for AlgoPaca
 * Supports English (en), Bangla (bn), Spanish (es), French (fr), Hindi (hi).
 */
(function () {
  const STORAGE_KEY = "paper_desk_lang";
  const SUPPORTED_LANGS = ["en", "bn", "es", "fr", "hi"];
  const DEFAULT_LANG = "en";

  let currentLang = localStorage.getItem(STORAGE_KEY) || DEFAULT_LANG;
  if (!SUPPORTED_LANGS.includes(currentLang)) {
    currentLang = DEFAULT_LANG;
  }

  const translations = {};

  async function loadTranslations(lang) {
    if (translations[lang]) {
      return translations[lang];
    }
    try {
      const response = await fetch(`/static/lang/${lang}.json`);
      if (!response.ok) {
        throw new Error(`Failed to load translation file for ${lang}`);
      }
      const data = await response.json();
      translations[lang] = data;
      return data;
    } catch (err) {
      console.warn(`[i18n] Error loading ${lang}, falling back:`, err);
      return translations[DEFAULT_LANG] || {};
    }
  }

  function t(key, fallback = "", params = {}) {
    const dict = translations[currentLang] || translations[DEFAULT_LANG] || {};
    let text = dict[key] !== undefined ? dict[key] : fallback || key;
    if (typeof text === "string" && params) {
      Object.keys(params).forEach((paramKey) => {
        text = text.replace(new RegExp(`\\{${paramKey}\\}`, "g"), params[paramKey]);
      });
    }
    return text;
  }

  function translateDOM(root = document) {
    const dict = translations[currentLang] || {};

    root.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      if (!key) return;

      // Preserve original text for fallback if not stored yet
      if (!el.hasAttribute("data-i18n-orig")) {
        el.setAttribute("data-i18n-orig", el.textContent.trim());
      }

      const val = dict[key] || el.getAttribute("data-i18n-orig");
      if (val !== undefined && val !== null) {
        // If element has dynamic child elements like icons/badges, only replace text node
        if (el.children.length === 0) {
          el.textContent = val;
        } else {
          // Find first text node or update text
          let replaced = false;
          el.childNodes.forEach((node) => {
            if (!replaced && node.nodeType === Node.TEXT_NODE && node.nodeValue.trim().length > 0) {
              node.nodeValue = val;
              replaced = true;
            }
          });
          if (!replaced) {
            el.textContent = val;
          }
        }
      }
    });

    root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      const key = el.getAttribute("data-i18n-placeholder");
      if (key && dict[key]) {
        el.setAttribute("placeholder", dict[key]);
      }
    });

    root.querySelectorAll("[data-i18n-title]").forEach((el) => {
      const key = el.getAttribute("data-i18n-title");
      if (key && dict[key]) {
        el.setAttribute("title", dict[key]);
      }
    });

    root.querySelectorAll("[data-i18n-aria-label]").forEach((el) => {
      const key = el.getAttribute("data-i18n-aria-label");
      if (key && dict[key]) {
        el.setAttribute("aria-label", dict[key]);
      }
    });

    // Update document HTML lang attribute
    document.documentElement.lang = currentLang;
  }

  async function setLanguage(lang) {
    if (!SUPPORTED_LANGS.includes(lang)) return;
    currentLang = lang;
    localStorage.setItem(STORAGE_KEY, lang);

    await loadTranslations(lang);
    translateDOM();

    // Tell the desk too — the AI writes its thesis / risks in this language.
    // Fire-and-forget: a failed save must not block the UI from switching.
    fetch("/api/lang", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lang }),
    }).catch(() => {});

    // Update switcher select dropdown value if present
    document.querySelectorAll(".lang-select, #lang-select").forEach((select) => {
      select.value = lang;
      if (typeof refreshNiceSelect === "function") {
        refreshNiceSelect(select);
      }
    });

    // Broadcast language change event for dynamic UI (app.js)
    window.dispatchEvent(
      new CustomEvent("languageChange", {
        detail: { lang, t },
      })
    );
  }

  function initLanguageSwitcher() {
    const selects = document.querySelectorAll(".lang-select, #lang-select");
    if (!selects.length) return;

    selects.forEach((select) => {
      select.value = currentLang;
      select.addEventListener("change", (e) => {
        const chosen = e.target.value;
        setLanguage(chosen);
      });
    });
  }

  const NOTE_PHRASE_MAP = {
    bn: [
      ["The broader trend and MACD remain strongly bearish", "প্রধান ট্রেন্ড এবং MACD তীব্র বেয়ারিশ অবস্থায় রয়েছে"],
      ["but the deeply oversold RSI and current rebound do not show a clear fresh breakdown", "তবে অতিরিক্ত বিক্রি হওয়া (oversold) RSI এবং বর্তমান রিবাউন্ড কোনো নতুন ব্রেকডাউন দেখায় না"],
      ["warranting a defensive sale; stale reverse-split news is not sufficient bearish confirmation", "যা বিক্রি সমর্থন করে; পুরানো রিভার্স-স্প্লিট সংবাদ বেয়ারিশ সিগন্যাল নিশ্চিতে যথেষ্ট নয়"],
      ["Remain flat because the sharp daily rebound lacks volume and occurs below the 10-, 20-, and 50-day SMAs while MACD and trend remain bearish", "পজিশন না নিয়ে ফ্ল্যাট থাকুন কারণ দৈনিক দ্রুত রিবাউন্ডে ভলিউম নেই এবং এটি ১০, ২০ ও ৫০ দিনের এসএমএ-র নিচে অবস্থান করছে"],
      ["Remain flat because the sharp daily rebound lacks volume and occurs below the 10-, 20-, and 50-day SMAs", "ফ্ল্যাট থাকুন কারণ দৈনিক রিবাউন্ডে ভলিউম নেই এবং ১০, ২০ ও ৫০ দিনের এসএমএ-র নিচে"],
      ["Remain flat: bearish trend, price below key moving averages, negative MACD, and predominantly bearish downgrade/news tone outweigh the temporary bounce", "ফ্ল্যাট থাকুন: বেয়ারিশ ট্রেন্ড, মূল্য প্রধান মুভিং এভারেজের নিচে এবং নেতিবাচক MACD সাময়িক বাউন্সের চেয়ে বেয়ারিশ প্রভাব বেশি দেখাচ্ছে"],
      ["Remain flat: bearish trend, price below key moving averages", "ফ্ল্যাট থাকুন: বেয়ারিশ ট্রেন্ড এবং মূল্য প্রধান মুভিং এভারেজের নিচে"],
      ["Conservative hold: momentum improved and RSI is neutral, but the trend remains mixed with price below the 50-day SMA, weak volume, and downside risks", "সংরক্ষণশীল হোল্ড: মোমেন্টাম উন্নত এবং আরএসআই নিরপেক্ষ, তবে ৫০ দিনের এসএমএ-র নিচে মূল্য ও দুর্বল ভলিউম ট্রেড স্থগিত রাখছে"],
      ["Conservative hold: momentum improved and RSI is neutral", "সংরক্ষণশীল হোল্ড: মোমেন্টাম উন্নত এবং আরএসআই নিরপেক্ষ"],
      ["Technicals remain bullish with price above key moving averages and positive MACD, but the sharp daily decline, extreme volatility, and downside momentum advise waiting", "টেকনিক্যাল সূচকগুলো বুলিশ হলেও দৈনিক দ্রত পতন, চরম অস্থিরতা এবং ডাউনসাইড মোমেন্টামের কারণে অপেক্ষা করা সমীচীন"],
      ["Technicals remain bullish with price above key moving averages and positive MACD", "টেকনিক্যাল সূচকগুলো বুলিশ এবং মূল্য প্রধান মুভিং এভারেজের উপরে রয়েছে"],
      ["Defensive hold: the sharp daily gain and improving MACD are not enough for a buy because the broader trend is neutral, RSI is neither oversold nor oversold", "প্রতিরক্ষামূলক হোল্ড: দৈনিক লাভ ও উন্নতি হলেও সামগ্রিক ট্রেন্ড নিউট্রাল থাকায় কেনা স্থগিত রাখা হচ্ছে"],
      ["Defensive hold:", "প্রতিরক্ষামূলক হোল্ড:"],
      ["Conservative strategy favors waiting: price remains above the 10- and 20-day averages with positive MACD momentum and supportive rebound", "সংরক্ষণশীল কৌশল অপেক্ষার পক্ষে: মূল্য ১০ ও ২০ দিনের এভারেজের উপরে রয়েছে"],
      ["Conservative strategy favors waiting", "সংরক্ষণশীল কৌশল অপেক্ষা করার পক্ষে"],
      ["Conservative playbook favors waiting", "সংরক্ষণশীল প্রিসেট অপেক্ষা করার পক্ষে"],
      ["Short-term technicals are bearish, but the trend bias is neutral, RSI is not oversold, volume is unusually light, and news provides no catalyst", "স্বল্পমেয়াদী টেকনিক্যাল বেয়ারিশ, তবে ট্রেন্ড নিউট্রাল ও ভলিউম অস্বাভাবিক কম থাকায় পজিশন স্থগিত রয়েছে"],
      ["Remain flat because technicals are mixed: price is above the 10- and 20-day averages with improving MACD, but remains below the 50-day", "ফ্ল্যাট থাকুন কারণ টেকনিক্যাল মিশ্র: মূল্য ১০ ও ২০ দিনের এভারেজের উপরে হলেও ৫০ দিনের এভারেজের নিচে"],
      ["Remain flat because technicals are mixed", "ফ্ল্যাট থাকুন কারণ টেকনিক্যাল সূচকগুলো মিশ্র"],
      ["Strong bullish trend and positive earnings-related news are offset by extreme overbought momentum", "শক্তিশালী বুলিশ ট্রেন্ড ও ইতিবাচক সংবাদের সাথে অতিরিক্ত ক্রয় (overbought) মোমেন্টাম কাজ করছে"],
      ["Remain flat: the broader trend and MACD are bullish, but RSI is overbought", "ফ্ল্যাট থাকুন: সামগ্রিক ট্রেন্ড ও MACD বুলিশ হলেও আরএসআই ওভারবট (overbought)"],
      ["Remain flat", "পজিশন না নিয়ে ফ্ল্যাট থাকুন"],
      ["bearish trend", "বেয়ারিশ ট্রেন্ড"],
      ["bullish trend", "বুলিশ ট্রেন্ড"],
      ["price below key moving averages", "মূল্য প্রধান মুভিং এভারেজের নিচে"],
      ["price above key moving averages", "মূল্য প্রধান মুভিং এভারেজের উপরে"],
      ["Conservative hold", "সংরক্ষণশীল হোল্ড"],
      ["Defensive hold", "প্রতিরক্ষামূলক হোল্ড"],
      ["momentum improved", "মোমেন্টাম উন্নত হয়েছে"],
      ["extreme volatility", "চরম অস্থিরতা"],
      ["lacks volume", "ভলিউমের অভাব"],
      ["negative MACD", "নেতিবাচক MACD"],
      ["positive MACD", "ইতিবাচক MACD"],
      ["Configure the strategy, then Run once to see a signal.", "কৌশলটি সেটআপ করুন এবং সিগন্যাল দেখতে একবার রান করুন।"],
      ["No signal yet for", "এখনও কোনো সিগন্যাল পাওয়া যায়নি —"],
      ["Fast SMA crossed above Slow SMA — bullish signal", "ফাস্ট এসএমএ স্লো এসএমএকে উপরে অতিক্রম করেছে — বুলিশ সিগন্যাল"],
      ["Fast SMA crossed below Slow SMA — bearish signal", "ফাস্ট এসএমএ স্লো এসএমএকে নিচে অতিক্রম করেছে — বেয়ারিশ সিগন্যাল"],
      ["Fast SMA above Slow SMA — hold long position", "ফাস্ট এসএমএ স্লো এসএমএ-র উপরে — লং পজিশন ধরে রাখুন"],
      ["Fast SMA below Slow SMA — hold flat", "ফাস্ট এসএমএ স্লো এসএমএ-র নিচে — ফ্ল্যাট পজিশন বজায় রাখুন"],
      ["RSI oversold (<=30) — dip buy", "আরএসআই ওভারসোল্ড (<=৩০) — ডিপ বাই"],
      ["RSI overbought (>=60) — dip exit", "আরএসআই ওভারবট (>=৬০) — এক্সিট সিগন্যাল"],
      ["Earnings blackout — holding new longs until after the print.", "আর্নিংস ব্ল্যাকআউট — ফলাফল প্রকাশের পূর্বে লং স্থগিত।"],
      ["Earnings blackout — holding new shorts until after the print.", "আর্নিংস ব্ল্যাকআউট — ফলাফল প্রকাশের পূর্বে শর্ট স্থগিত।"],
      ["Buy requested while already long — holding.", "ইতোমধ্যে লং পজিশন বিদ্যমান — নতুন কেনা স্থগিত।"],
      ["Sell requested while already short — holding.", "ইতোমধ্যে শর্ট পজিশন বিদ্যমান — নতুন শর্ট স্থগিত।"]
    ],
    es: [
      ["The broader trend and MACD remain strongly bearish", "La tendencia general y el MACD se mantienen fuertemente bajistas"],
      ["but the deeply oversold RSI and current rebound do not show a clear fresh breakdown", "pero el RSI profundamente sobrevendido y el rebote actual no muestran una ruptura clara"],
      ["warranting a defensive sale; stale reverse-split news is not sufficient bearish confirmation", "que justifique una venta defensiva; las noticias no son confirmación suficiente"],
      ["Remain flat because the sharp daily rebound lacks volume and occurs below the 10-, 20-, and 50-day SMAs while MACD and trend remain bearish", "Permanecer neutral porque el rebote diario carece de volumen y ocurre por debajo de las SMA de 10, 20 y 50 días"],
      ["Remain flat because the sharp daily rebound lacks volume and occurs below the 10-, 20-, and 50-day SMAs", "Permanecer neutral porque el rebote carece de volumen y está bajo las SMA clave"],
      ["Remain flat: bearish trend, price below key moving averages, negative MACD, and predominantly bearish downgrade/news tone outweigh the temporary bounce", "Permanecer neutral: tendencia bajista, precio bajo medias móviles clave y MACD negativo"],
      ["Remain flat: bearish trend, price below key moving averages", "Permanecer neutral: tendencia bajista y precio bajo medias móviles clave"],
      ["Conservative hold: momentum improved and RSI is neutral, but the trend remains mixed with price below the 50-day SMA, weak volume, and downside risks", "Mantener conservador: el impulso mejoró y el RSI es neutral, pero el precio sigue bajo la SMA de 50 días"],
      ["Conservative hold: momentum improved and RSI is neutral", "Mantener conservador: el impulso mejoró y el RSI es neutral"],
      ["Technicals remain bullish with price above key moving averages and positive MACD, but the sharp daily decline, extreme volatility, and downside momentum advise waiting", "El análisis técnico sigue alcista con el precio sobre las medias clave, pero la volatilidad aconseja esperar"],
      ["Technicals remain bullish with price above key moving averages and positive MACD", "Técnicos se mantienen alcistas con precio sobre medias móviles clave"],
      ["Defensive hold: the sharp daily gain and improving MACD are not enough for a buy because the broader trend is neutral, RSI is neither oversold nor oversold", "Mantenimiento defensivo: la ganancia diaria no es suficiente para comprar"],
      ["Defensive hold:", "Mantener defensivo:"],
      ["Conservative strategy favors waiting", "La estrategia conservadora favorece esperar"],
      ["Conservative playbook favors waiting", "El plan conservador favorece esperar"],
      ["Remain flat because technicals are mixed", "Permanecer neutral porque los técnicos son mixtos"],
      ["Remain flat", "Permanecer neutral"],
      ["bearish trend", "tendencia bajista"],
      ["bullish trend", "tendencia alcista"],
      ["price below key moving averages", "precio por debajo de medias móviles clave"],
      ["price above key moving averages", "precio por encima de medias móviles clave"],
      ["Conservative hold", "Mantenimiento conservador"],
      ["Defensive hold", "Mantenimiento defensivo"],
      ["momentum improved", "impulso mejorado"],
      ["extreme volatility", "volatilidad extrema"],
      ["lacks volume", "falta de volumen"],
      ["negative MACD", "MACD negativo"],
      ["positive MACD", "MACD positivo"],
      ["Configure the strategy, then Run once to see a signal.", "Configure la estrategia y ejecute una vez para ver una señal."],
      ["Fast SMA crossed above Slow SMA — bullish signal", "SMA rápida cruzó por encima de SMA lenta — señal alcista"],
      ["Fast SMA crossed below Slow SMA — bearish signal", "SMA rápida cruzó por debajo de SMA lenta — señal bajista"],
      ["Fast SMA above Slow SMA — hold long position", "SMA rápida sobre SMA lenta — mantener posición larga"],
      ["Fast SMA below Slow SMA — hold flat", "SMA rápida bajo SMA lenta — mantener posición neutral"],
      ["RSI oversold (<=30) — dip buy", "RSI sobrevendido (<=30) — compra de caída"],
      ["RSI overbought (>=60) — dip exit", "RSI sobrecomprado (>=60) — salida de compra"],
      ["Earnings blackout — holding new longs until after the print.", "Bloqueo por resultados — manteniendo posiciones hasta los resultados."],
      ["Buy requested while already long — holding.", "Compra solicitada ya estando en largo — manteniendo."]
    ],
    fr: [
      ["The broader trend and MACD remain strongly bearish", "La tendance générale et le MACD restent fortement baissiers"],
      ["but the deeply oversold RSI and current rebound do not show a clear fresh breakdown", "mais le RSI survendu et le rebond actuel ne montrent pas de rupture nette"],
      ["warranting a defensive sale; stale reverse-split news is not sufficient bearish confirmation", "justifiant une vente défensive; les informations ne sont pas une confirmation suffisante"],
      ["Remain flat because the sharp daily rebound lacks volume and occurs below the 10-, 20-, and 50-day SMAs while MACD and trend remain bearish", "Rester neutre car le rebond manque de volume et reste sous les SMA 10, 20 et 50 jours"],
      ["Remain flat because the sharp daily rebound lacks volume and occurs below the 10-, 20-, and 50-day SMAs", "Rester neutre car le rebond manque de volume et se situe sous les SMA clés"],
      ["Remain flat: bearish trend, price below key moving averages, negative MACD, and predominantly bearish downgrade/news tone outweigh the temporary bounce", "Rester neutre : tendance baissière, prix sous les moyennes mobiles clés et MACD négatif"],
      ["Remain flat: bearish trend, price below key moving averages", "Rester neutre : tendance baissière et prix sous les moyennes mobiles clés"],
      ["Conservative hold: momentum improved and RSI is neutral, but the trend remains mixed with price below the 50-day SMA, weak volume, and downside risks", "Maintien conservateur : le momentum s'est amélioré et le RSI est neutre, mais le prix reste sous la SMA 50"],
      ["Conservative hold: momentum improved and RSI is neutral", "Maintien conservateur : momentum amélioré et RSI neutre"],
      ["Technicals remain bullish with price above key moving averages and positive MACD, but the sharp daily decline, extreme volatility, and downside momentum advise waiting", "Les indicateurs techniques restent haussiers avec le prix au-dessus des moyennes clés, mais la volatilité incite à attendre"],
      ["Technicals remain bullish with price above key moving averages and positive MACD", "Les indicateurs restent haussiers avec le prix au-dessus des moyennes clés"],
      ["Defensive hold:", "Maintien défensif :"],
      ["Conservative strategy favors waiting", "La stratégie conservatrice préconise l'attente"],
      ["Conservative playbook favors waiting", "Le modèle conservateur préconise l'attente"],
      ["Remain flat because technicals are mixed", "Rester neutre car les indicateurs sont mixtes"],
      ["Remain flat", "Rester neutre"],
      ["bearish trend", "tendance baissière"],
      ["bullish trend", "tendance haussière"],
      ["price below key moving averages", "prix sous les moyennes mobiles clés"],
      ["price above key moving averages", "prix au-dessus des moyennes mobiles clés"],
      ["Conservative hold", "Maintien conservateur"],
      ["Defensive hold", "Maintien défensif"],
      ["momentum improved", "momentum amélioré"],
      ["extreme volatility", "volatilité extrême"],
      ["lacks volume", "manque de volume"],
      ["negative MACD", "MACD négatif"],
      ["positive MACD", "MACD positif"],
      ["Configure the strategy, then Run once to see a signal.", "Configurez la stratégie, puis lancez une exécution pour voir un signal."],
      ["Fast SMA crossed above Slow SMA — bullish signal", "La SMA rapide a croisé au-dessus de la SMA lente — signal haussier"],
      ["Fast SMA crossed below Slow SMA — bearish signal", "La SMA rapide a croisé sous la SMA lente — signal baissier"],
      ["Fast SMA above Slow SMA — hold long position", "SMA rapide au-dessus de SMA lente — maintenir la position longue"],
      ["Fast SMA below Slow SMA — hold flat", "SMA rapide sous SMA lente — maintenir la position neutre"],
      ["RSI oversold (<=30) — dip buy", "RSI survendu (<=30) — achat sur repli"],
      ["RSI overbought (>=60) — dip exit", "RSI suracheté (>=60) — sortie de repli"],
      ["Earnings blackout — holding new longs until after the print.", "Période de blocage des résultats — maintien des positions jusqu'aux annonces."],
      ["Buy requested while already long — holding.", "Achat demandé alors que déjà en position longue — maintien."]
    ],
    hi: [
      ["The broader trend and MACD remain strongly bearish", "व्यापक रुझान और MACD मजबूती से मंदी में बने हुए हैं"],
      ["but the deeply oversold RSI and current rebound do not show a clear fresh breakdown", "लेकिन अत्यधिक ओवरसोल्ड RSI और वर्तमान सुधार कोई स्पष्ट नया ब्रेकडाउन नहीं दिखाते"],
      ["warranting a defensive sale; stale reverse-split news is not sufficient bearish confirmation", "जो सुरक्षात्मक बिक्री का औचित्य सिद्ध करे; पुरानी खबर मंदी की पुष्टि नहीं है"],
      ["Remain flat because the sharp daily rebound lacks volume and occurs below the 10-, 20-, and 50-day SMAs while MACD and trend remain bearish", "तटस्थ रहें क्योंकि दैनिक सुधार में वॉल्यूम की कमी है और यह 10, 20 और 50 दिनों के SMA से नीचे है"],
      ["Remain flat because the sharp daily rebound lacks volume and occurs below the 10-, 20-, and 50-day SMAs", "तटस्थ रहें क्योंकि सुधार में वॉल्यूम की कमी है और यह प्रमुख SMA से नीचे है"],
      ["Remain flat: bearish trend, price below key moving averages, negative MACD, and predominantly bearish downgrade/news tone outweigh the temporary bounce", "तटस्थ रहें: मंदी का रुझान, कीमत प्रमुख मूविंग एवरेज से नीचे और नकारात्मक MACD"],
      ["Remain flat: bearish trend, price below key moving averages", "तटस्थ रहें: मंदी का रुझान और कीमत प्रमुख मूविंग एवरेज से नीचे"],
      ["Conservative hold: momentum improved and RSI is neutral, but the trend remains mixed with price below the 50-day SMA, weak volume, and downside risks", "सुरक्षित होल्ड: मोमेंटम में सुधार हुआ है और RSI तटस्थ है, लेकिन कीमत 50-दिवसीय SMA से नीचे है"],
      ["Conservative hold: momentum improved and RSI is neutral", "सुरक्षित होल्ड: मोमेंटम में सुधार हुआ और RSI तटस्थ है"],
      ["Technicals remain bullish with price above key moving averages and positive MACD, but the sharp daily decline, extreme volatility, and downside momentum advise waiting", "तकनीकी संकेत सकारात्मक हैं लेकिन अत्यधिक उतार-चढ़ाव प्रतीक्षा की सलाह देता है"],
      ["Technicals remain bullish with price above key moving averages and positive MACD", "तकनीकी संकेत सकारात्मक बने हुए हैं और कीमत प्रमुख मूविंग एवरेज से ऊपर है"],
      ["Defensive hold:", "रक्षात्मक होल्ड:"],
      ["Conservative strategy favors waiting", "सुरक्षित रणनीति प्रतीक्षा का समर्थन करती है"],
      ["Conservative playbook favors waiting", "सुरक्षित प्लेबुक प्रतीक्षा का समर्थन करती है"],
      ["Remain flat because technicals are mixed", "तटस्थ रहें क्योंकि तकनीकी संकेत मिश्रित हैं"],
      ["Remain flat", "तटस्थ (न्यूट्रल) रहें"],
      ["bearish trend", "मंदी का रुझान"],
      ["bullish trend", "तेजी का रुझान"],
      ["price below key moving averages", "कीमत प्रमुख मूविंग एवरेज से नीचे"],
      ["price above key moving averages", "कीमत प्रमुख मूविंग एवरेज से ऊपर"],
      ["Conservative hold", "सुरक्षित होल्ड"],
      ["Defensive hold", "रक्षात्मक होल्ड"],
      ["momentum improved", "मोमेंटम में सुधार हुआ"],
      ["extreme volatility", "अत्यधिक उतार-चढ़ाव"],
      ["lacks volume", "वॉल्यूम की कमी"],
      ["negative MACD", "नकारात्मक MACD"],
      ["positive MACD", "सकारात्मक MACD"],
      ["Configure the strategy, then Run once to see a signal.", "रणनीति को कॉन्फ़िगर करें, फिर संकेत देखने के लिए एक बार चलाएं।"],
      ["Fast SMA crossed above Slow SMA — bullish signal", "फास्ट SMA ने स्लो SMA को ऊपर पार किया — तेजी का संकेत"],
      ["Fast SMA crossed below Slow SMA — bearish signal", "फास्ट SMA ने स्लो SMA को नीचे पार किया — मंदी का संकेत"],
      ["Fast SMA above Slow SMA — hold long position", "फास्ट SMA स्लो SMA से ऊपर — लॉन्ग पोजीशन बनाए रखें"],
      ["Fast SMA below Slow SMA — hold flat", "फास्ट SMA स्लो SMA से नीचे — न्यूट्रल रहें"],
      ["RSI oversold (<=30) — dip buy", "RSI ओवरसोल्ड (<=30) — डिप खरीदारी"],
      ["RSI overbought (>=60) — dip exit", "RSI ओवरबॉट (>=60) — निकास संकेत"],
      ["Earnings blackout — holding new longs until after the print.", "परिणामों की अवधि — परिणाम आने तक नई खरीदारी स्थगित।"],
      ["Buy requested while already long — holding.", "पहले से लॉन्ग पोजीशन मौजूद है — नई खरीदारी स्थगित।"]
    ]
  };

  function translateNote(text, lang) {
    if (!text || typeof text !== "string") return text;
    const targetLang = lang || currentLang || "en";
    if (targetLang === "en") return text;

    const list = NOTE_PHRASE_MAP[targetLang];
    if (!list || !list.length) return text;

    let res = text;
    for (const [enPhrase, localized] of list) {
      if (res.includes(enPhrase)) {
        res = res.replaceAll(enPhrase, localized);
      }
    }
    return res;
  }

  async function init() {
    // Load default language first as fallback baseline
    if (currentLang !== DEFAULT_LANG) {
      await loadTranslations(DEFAULT_LANG);
    }
    await loadTranslations(currentLang);
    translateDOM();
    initLanguageSwitcher();
  }

  // Public API
  window.i18n = {
    t,
    setLanguage,
    getCurrentLanguage: () => currentLang,
    getSupportedLanguages: () => [...SUPPORTED_LANGS],
    translateDOM,
    loadTranslations,
    translateNote,
  };
  window.t = t;
  window.translateNote = translateNote;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
