#!/usr/bin/env python3
"""
AlgoPaca Logo Generator
Creates clean, precision SVG assets for AlgoPaca brand identity:
1. algopaca-mark.svg (512x512 standalone square icon mark)
2. favicon.svg (64x64 lightweight favicon)
3. algopaca-logo.svg (Horizontal logo with mark and typography)
4. algopaca-banner.svg (README and social banner)
"""

from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "web" / "static" / "img"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 1. Standalone Mark (512x512)
MARK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <defs>
    <!-- Background Gradients -->
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#141C2B"/>
      <stop offset="50%" stop-color="#0E1522"/>
      <stop offset="100%" stop-color="#080C14"/>
    </linearGradient>
    
    <linearGradient id="borderGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#F59E0B" stop-opacity="0.8"/>
      <stop offset="40%" stop-color="#D4894C" stop-opacity="0.3"/>
      <stop offset="70%" stop-color="#3FBF8F" stop-opacity="0.4"/>
      <stop offset="100%" stop-color="#10B981" stop-opacity="0.7"/>
    </linearGradient>

    <radialGradient id="ambientCopper" cx="30%" cy="30%" r="60%">
      <stop offset="0%" stop-color="#F59E0B" stop-opacity="0.28"/>
      <stop offset="50%" stop-color="#D97706" stop-opacity="0.08"/>
      <stop offset="100%" stop-color="#D97706" stop-opacity="0"/>
    </radialGradient>

    <radialGradient id="ambientEmerald" cx="80%" cy="80%" r="60%">
      <stop offset="0%" stop-color="#10B981" stop-opacity="0.22"/>
      <stop offset="60%" stop-color="#059669" stop-opacity="0.05"/>
      <stop offset="100%" stop-color="#059669" stop-opacity="0"/>
    </radialGradient>

    <!-- Facet Gradients for Alpaca & Chart -->
    <linearGradient id="goldLight" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FEF08A"/>
      <stop offset="100%" stop-color="#F59E0B"/>
    </linearGradient>

    <linearGradient id="goldMain" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FBBF24"/>
      <stop offset="100%" stop-color="#D97706"/>
    </linearGradient>

    <linearGradient id="copperDeep" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#D97706"/>
      <stop offset="100%" stop-color="#92400E"/>
    </linearGradient>

    <linearGradient id="copperShadow" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#78350F"/>
      <stop offset="100%" stop-color="#451A03"/>
    </linearGradient>

    <linearGradient id="emeraldBright" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#6EE7B7"/>
      <stop offset="100%" stop-color="#10B981"/>
    </linearGradient>

    <linearGradient id="emeraldDeep" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#10B981"/>
      <stop offset="100%" stop-color="#047857"/>
    </linearGradient>

    <linearGradient id="cyanAccent" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#38BDF8"/>
      <stop offset="100%" stop-color="#0284C7"/>
    </linearGradient>

    <linearGradient id="chartCandleUp" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#34D399"/>
      <stop offset="100%" stop-color="#059669"/>
    </linearGradient>

    <!-- Filters for Glow Effects -->
    <filter id="markGlow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="8" result="blur"/>
      <feComposite in="SourceGraphic" in2="blur" operator="over"/>
    </filter>
    <filter id="subtleShadow" x="-10%" y="-10%" width="120%" height="120%">
      <feDropShadow dx="0" dy="6" stdDeviation="10" flood-color="#000000" flood-opacity="0.6"/>
    </filter>
  </defs>

  <!-- Container Squircle Background -->
  <rect x="20" y="20" width="472" height="472" rx="108" fill="url(#bgGrad)"/>
  
  <!-- Subtle Ambient Glows inside Badge -->
  <rect x="20" y="20" width="472" height="472" rx="108" fill="url(#ambientCopper)"/>
  <rect x="20" y="20" width="472" height="472" rx="108" fill="url(#ambientEmerald)"/>

  <!-- High-Precision Tech Grid Lines (Subtle) -->
  <g opacity="0.12" stroke="#94A3B8" stroke-width="1" stroke-dasharray="3 4">
    <line x1="80" y1="170" x2="432" y2="170"/>
    <line x1="80" y1="256" x2="432" y2="256"/>
    <line x1="80" y1="342" x2="432" y2="342"/>
    <line x1="170" y1="80" x2="170" y2="432"/>
    <line x1="256" y1="80" x2="256" y2="432"/>
    <line x1="342" y1="80" x2="342" y2="432"/>
  </g>

  <!-- Candlestick / Quantitative Bar Grid forming the base foundation -->
  <g opacity="0.35">
    <!-- Bar 1 (Left low) -->
    <line x1="122" y1="310" x2="122" y2="400" stroke="#3FBF8F" stroke-width="2" stroke-linecap="round"/>
    <rect x="116" y="330" width="12" height="50" rx="3" fill="#3FBF8F" opacity="0.8"/>
    
    <!-- Bar 2 (Left-mid) -->
    <line x1="148" y1="270" x2="148" y2="390" stroke="#10B981" stroke-width="2" stroke-linecap="round"/>
    <rect x="142" y="290" width="12" height="70" rx="3" fill="#10B981" opacity="0.9"/>
  </g>

  <!-- Central Emblem Group -->
  <g filter="url(#subtleShadow)">
    <!-- BACK EAR (Left / Far Ear) -->
    <polygon points="195,190 190,95 235,160 230,205" fill="url(#copperShadow)"/>
    <polygon points="190,95 218,140 195,190" fill="url(#copperDeep)"/>

    <!-- FRONT EAR (Prominent Right Ear - Alert & Crisp) -->
    <polygon points="230,175 240,75 285,150 265,195" fill="url(#copperDeep)"/>
    <polygon points="240,75 285,150 256,165" fill="url(#goldLight)"/>
    <polygon points="240,75 256,165 230,175" fill="url(#goldMain)"/>
    <!-- Inner Ear Neon Highlight -->
    <polygon points="245,100 270,148 253,158" fill="#FEF3C7" opacity="0.85"/>

    <!-- ALPACA HEAD CROWN / TUFT (Geometric Poly Crest) -->
    <polygon points="195,190 230,175 265,195 250,225 205,220" fill="url(#goldMain)"/>
    <polygon points="230,175 265,195 285,180 255,168" fill="url(#goldLight)"/>

    <!-- FOREHEAD & UPPER SNOUT -->
    <polygon points="265,195 335,215 285,250 250,225" fill="url(#goldLight)"/>
    <polygon points="335,215 385,238 348,272 285,250" fill="url(#goldMain)"/>

    <!-- SNOUT TIP & NOSE -->
    <polygon points="385,238 410,256 385,282 348,272" fill="url(#copperDeep)"/>
    <!-- Nose Tip Accent (Dark Onyx + Emerald Pip) -->
    <polygon points="398,250 410,256 398,266 388,258" fill="#1E293B"/>
    <circle cx="399" cy="257" r="2" fill="#3FBF8F"/>

    <!-- JAW & CHIN -->
    <polygon points="385,282 348,272 315,310 350,318" fill="url(#copperShadow)"/>
    <polygon points="348,272 285,250 315,310" fill="url(#copperDeep)"/>

    <!-- THE ALPHA EYE (Illuminated Smart Cyan/Emerald Quant Aperture) -->
    <polygon points="298,228 322,234 314,248 292,242" fill="#09131F"/>
    <polygon points="302,231 318,236 312,245 297,240" fill="url(#cyanAccent)"/>
    <circle cx="308" cy="238" r="3.5" fill="#FFFFFF"/>
    <circle cx="308" cy="238" r="1.5" fill="#67E8F9"/>

    <!-- CHEEK & THROAT TRANSITION -->
    <polygon points="205,220 250,225 285,250 260,300 195,275" fill="url(#copperDeep)"/>
    <polygon points="285,250 315,310 260,300" fill="url(#copperShadow)"/>

    <!-- QUANTITATIVE ASCENDING NECK / PILLARS -->
    <!-- Pillar 1: Front / Leading Ascending Candlestick Bar (Emerald -> Mint) -->
    <polygon points="260,300 315,310 295,415 240,415" fill="url(#emeraldBright)"/>
    <polygon points="315,310 350,318 330,415 295,415" fill="url(#emeraldDeep)"/>

    <!-- Pillar 2: Middle Structural Pillar (Teal -> Copper Fusion) -->
    <polygon points="195,275 260,300 240,415 175,415" fill="url(#goldMain)"/>
    
    <!-- Pillar 3: Back Structural Pillar (Deep Copper / Base) -->
    <polygon points="150,320 195,275 175,415 130,415" fill="url(#copperShadow)"/>

    <!-- ASCENDING TREND ACCENT / ALPHA IMPULSE LINE (The Quant Trajectory) -->
    <path d="M 115 390 L 180 340 L 235 365 L 320 270 L 375 220 L 420 170" 
          fill="none" 
          stroke="url(#goldLight)" 
          stroke-width="4.5" 
          stroke-linecap="round" 
          stroke-linejoin="round"
          filter="url(#markGlow)"/>

    <!-- Pulse nodes on trend line -->
    <circle cx="180" cy="340" r="4.5" fill="#34D399" stroke="#064E3B" stroke-width="1.5"/>
    <circle cx="235" cy="365" r="4.5" fill="#38BDF8" stroke="#0C4A6E" stroke-width="1.5"/>
    <circle cx="320" cy="270" r="5.5" fill="#FBBF24" stroke="#78350F" stroke-width="1.5"/>
    <!-- Terminal Alpha Arrow Node -->
    <circle cx="420" cy="170" r="7" fill="#FEF08A" stroke="#B45309" stroke-width="2"/>
    <circle cx="420" cy="170" r="3" fill="#EA580C"/>
  </g>

  <!-- Container Border with Precision Dual Hue Gradient -->
  <rect x="20" y="20" width="472" height="472" rx="108" fill="none" stroke="url(#borderGrad)" stroke-width="3.5"/>
  
  <!-- Subtle Inner Glass Specular Edge -->
  <rect x="23" y="23" width="466" height="466" rx="105" fill="none" stroke="#FFFFFF" stroke-opacity="0.08" stroke-width="1.5"/>
</svg>
"""

# 2. Favicon SVG (64x64) - Ultra-crisp at micro sizes
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="100%" height="100%">
  <defs>
    <linearGradient id="favBg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#141C2B"/>
      <stop offset="100%" stop-color="#080C14"/>
    </linearGradient>
    <linearGradient id="favBorder" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#F59E0B"/>
      <stop offset="100%" stop-color="#10B981"/>
    </linearGradient>
    <linearGradient id="favGold" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FEF08A"/>
      <stop offset="100%" stop-color="#D97706"/>
    </linearGradient>
    <linearGradient id="favGreen" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#6EE7B7"/>
      <stop offset="100%" stop-color="#059669"/>
    </linearGradient>
  </defs>

  <!-- Background Base -->
  <rect x="2" y="2" width="60" height="60" rx="14" fill="url(#favBg)" stroke="url(#favBorder)" stroke-width="1.5"/>

  <!-- Simplified Geometric Alpaca Mark -->
  <g transform="translate(1, 0)">
    <!-- Ears -->
    <polygon points="25,23 26,10 32,20 29,26" fill="#B45309"/>
    <polygon points="30,22 32,9 38,19 35,25" fill="url(#favGold)"/>
    
    <!-- Head & Snout -->
    <polygon points="25,24 35,24 43,28 47,33 42,37 36,33 33,38 25,34" fill="url(#favGold)"/>
    <polygon points="43,28 49,32 46,36 42,37" fill="#78350F"/>
    
    <!-- Eye (Bright Cyan/White node) -->
    <circle cx="39" cy="30" r="1.8" fill="#38BDF8"/>
    <circle cx="39" cy="30" r="0.8" fill="#FFFFFF"/>

    <!-- Neck / Alpha Bars -->
    <polygon points="25,34 33,38 31,52 23,52" fill="#D97706"/>
    <polygon points="33,38 41,40 38,52 31,52" fill="url(#favGreen)"/>
    <polygon points="17,40 25,34 23,52 16,52" fill="#78350F"/>

    <!-- Impulse Alpha Trend Spark -->
    <polyline points="14,48 24,42 32,45 44,32 51,24" fill="none" stroke="#FDE047" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="51" cy="24" r="2.2" fill="#F59E0B" stroke="#FEF08A" stroke-width="1"/>
  </g>
</svg>
"""

# 3. Horizontal Full Logo (540x120) - For Mastheads, Headers, Docs
LOGO_HORIZONTAL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 540 120" width="100%" height="100%">
  <defs>
    <linearGradient id="lhBgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#141C2B"/>
      <stop offset="100%" stop-color="#090E17"/>
    </linearGradient>
    <linearGradient id="lhBorder" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#F59E0B" stop-opacity="0.9"/>
      <stop offset="100%" stop-color="#10B981" stop-opacity="0.8"/>
    </linearGradient>
    <linearGradient id="lhGold" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FEF08A"/>
      <stop offset="100%" stop-color="#F59E0B"/>
    </linearGradient>
    <linearGradient id="lhCopper" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#F59E0B"/>
      <stop offset="100%" stop-color="#D97706"/>
    </linearGradient>
    <linearGradient id="lhGreen" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#34D399"/>
      <stop offset="100%" stop-color="#059669"/>
    </linearGradient>
    <linearGradient id="textGradPaca" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FCD34D"/>
      <stop offset="50%" stop-color="#F59E0B"/>
      <stop offset="100%" stop-color="#D97706"/>
    </linearGradient>
    <linearGradient id="textGradAlgo" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FFFFFF"/>
      <stop offset="100%" stop-color="#E2E8F0"/>
    </linearGradient>
  </defs>

  <!-- Left Icon Mark (Centered in 120x120 area) -->
  <g transform="translate(10, 10)">
    <!-- Icon Container -->
    <rect x="0" y="0" width="100" height="100" rx="24" fill="url(#lhBgGrad)" stroke="url(#lhBorder)" stroke-width="1.8"/>
    
    <!-- Alpaca Mark in Mini Scale -->
    <g transform="translate(4, 2) scale(0.18)">
      <!-- Back Ear -->
      <polygon points="195,190 190,95 235,160 230,205" fill="#78350F"/>
      <!-- Front Ear -->
      <polygon points="230,175 240,75 285,150 265,195" fill="#D97706"/>
      <polygon points="240,75 285,150 256,165" fill="#FEF08A"/>
      <polygon points="240,75 256,165 230,175" fill="#FBBF24"/>
      <!-- Crown -->
      <polygon points="195,190 230,175 265,195 250,225 205,220" fill="#FBBF24"/>
      <!-- Forehead & Snout -->
      <polygon points="265,195 335,215 285,250 250,225" fill="#FEF08A"/>
      <polygon points="335,215 385,238 348,272 285,250" fill="#FBBF24"/>
      <polygon points="385,238 410,256 385,282 348,272" fill="#92400E"/>
      <!-- Eye -->
      <polygon points="298,228 322,234 314,248 292,242" fill="#09131F"/>
      <circle cx="308" cy="238" r="4.5" fill="#38BDF8"/>
      <circle cx="308" cy="238" r="2" fill="#FFFFFF"/>
      <!-- Jaw -->
      <polygon points="385,282 348,272 315,310 350,318" fill="#451A03"/>
      <polygon points="348,272 285,250 315,310" fill="#78350F"/>
      <!-- Throat & Cheek -->
      <polygon points="205,220 250,225 285,250 260,300 195,275" fill="#92400E"/>
      <!-- Quantitative Pillars -->
      <polygon points="260,300 315,310 295,415 240,415" fill="#10B981"/>
      <polygon points="315,310 350,318 330,415 295,415" fill="#047857"/>
      <polygon points="195,275 260,300 240,415 175,415" fill="#F59E0B"/>
      <polygon points="150,320 195,275 175,415 130,415" fill="#78350F"/>
      <!-- Ascending Trend -->
      <path d="M 115 390 L 180 340 L 235 365 L 320 270 L 375 220 L 420 170" fill="none" stroke="#FDE047" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="420" cy="170" r="9" fill="#FEF08A" stroke="#B45309" stroke-width="2"/>
    </g>
  </g>

  <!-- Typography: AlgoPaca -->
  <g transform="translate(130, 0)">
    <!-- Main Wordmark -->
    <text x="0" y="66" font-family="'Schibsted Grotesk', 'Inter', -apple-system, sans-serif" font-size="44" font-weight="800" letter-spacing="-0.03em">
      <tspan fill="url(#textGradAlgo)">Algo</tspan><tspan fill="url(#textGradPaca)">Paca</tspan>
    </text>

    <!-- Subtitle / Eyebrow Badge -->
    <g transform="translate(2, 78)">
      <!-- Terminal Tag -->
      <text x="0" y="16" font-family="'IBM Plex Mono', 'Courier New', monospace" font-size="11" font-weight="600" letter-spacing="0.16em" fill="#94A3B8">
        QUANTITATIVE TRADING DESK
      </text>
      
      <!-- Live Status Pill Dot -->
      <circle cx="218" cy="12" r="3.5" fill="#10B981"/>
      <text x="228" y="15.5" font-family="'IBM Plex Mono', monospace" font-size="10" font-weight="600" letter-spacing="0.1em" fill="#34D399">ALPACA</text>
    </g>
  </g>
</svg>
"""

# 4. Banner SVG (1200x420) - For README, GitHub & Hero Preview
BANNER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 420" width="100%" height="100%">
  <defs>
    <linearGradient id="bannerBg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0A0F18"/>
      <stop offset="50%" stop-color="#0E1624"/>
      <stop offset="100%" stop-color="#060A10"/>
    </linearGradient>

    <radialGradient id="bannerGlowLeft" cx="20%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#F59E0B" stop-opacity="0.18"/>
      <stop offset="70%" stop-color="#F59E0B" stop-opacity="0"/>
    </radialGradient>

    <radialGradient id="bannerGlowRight" cx="80%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#10B981" stop-opacity="0.14"/>
      <stop offset="70%" stop-color="#10B981" stop-opacity="0"/>
    </radialGradient>

    <linearGradient id="bannerBorder" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#F59E0B" stop-opacity="0.6"/>
      <stop offset="50%" stop-color="#38BDF8" stop-opacity="0.3"/>
      <stop offset="100%" stop-color="#10B981" stop-opacity="0.6"/>
    </linearGradient>

    <linearGradient id="titlePaca" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#FDE68A"/>
      <stop offset="50%" stop-color="#F59E0B"/>
      <stop offset="100%" stop-color="#D97706"/>
    </linearGradient>

    <linearGradient id="chipBg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1E293B" stop-opacity="0.8"/>
      <stop offset="100%" stop-color="#0F172A" stop-opacity="0.8"/>
    </linearGradient>
  </defs>

  <!-- Background Base -->
  <rect x="0" y="0" width="1200" height="420" fill="url(#bannerBg)"/>
  
  <!-- Atmospheric Glow Orbs -->
  <rect x="0" y="0" width="1200" height="420" fill="url(#bannerGlowLeft)"/>
  <rect x="0" y="0" width="1200" height="420" fill="url(#bannerGlowRight)"/>

  <!-- High-Tech Background Chart Silhouette -->
  <g opacity="0.12">
    <!-- Grid -->
    <line x1="100" y1="80" x2="1100" y2="80" stroke="#64748B" stroke-width="1" stroke-dasharray="4 6"/>
    <line x1="100" y1="160" x2="1100" y2="160" stroke="#64748B" stroke-width="1" stroke-dasharray="4 6"/>
    <line x1="100" y1="240" x2="1100" y2="240" stroke="#64748B" stroke-width="1" stroke-dasharray="4 6"/>
    <line x1="100" y1="320" x2="1100" y2="320" stroke="#64748B" stroke-width="1" stroke-dasharray="4 6"/>

    <!-- Subtle Candlesticks -->
    <line x1="720" y1="110" x2="720" y2="330" stroke="#34D399" stroke-width="2"/>
    <rect x="712" y="140" width="16" height="120" rx="3" fill="#34D399"/>

    <line x1="780" y1="80" x2="780" y2="300" stroke="#34D399" stroke-width="2"/>
    <rect x="772" y="110" width="16" height="100" rx="3" fill="#34D399"/>

    <line x1="840" y1="130" x2="840" y2="340" stroke="#F87171" stroke-width="2"/>
    <rect x="832" y="160" width="16" height="80" rx="3" fill="#F87171"/>

    <line x1="900" y1="70" x2="900" y2="280" stroke="#34D399" stroke-width="2"/>
    <rect x="892" y="90" width="16" height="110" rx="3" fill="#34D399"/>

    <line x1="960" y1="50" x2="960" y2="250" stroke="#34D399" stroke-width="2"/>
    <rect x="952" y="70" width="16" height="90" rx="3" fill="#34D399"/>

    <line x1="1020" y1="40" x2="1020" y2="220" stroke="#34D399" stroke-width="2"/>
    <rect x="1012" y="55" width="16" height="75" rx="3" fill="#34D399"/>
  </g>

  <!-- Left Side: Iconic Logo Mark -->
  <g transform="translate(100, 75)">
    <!-- Container Badge -->
    <rect x="0" y="0" width="270" height="270" rx="60" fill="#111A28" stroke="url(#bannerBorder)" stroke-width="2.5"/>
    <rect x="0" y="0" width="270" height="270" rx="60" fill="url(#bannerGlowLeft)"/>

    <!-- Scaled Mark -->
    <g transform="translate(15, 10) scale(0.48)">
      <!-- Back Ear -->
      <polygon points="195,190 190,95 235,160 230,205" fill="#78350F"/>
      <polygon points="190,95 218,140 195,190" fill="#92400E"/>
      <!-- Front Ear -->
      <polygon points="230,175 240,75 285,150 265,195" fill="#92400E"/>
      <polygon points="240,75 285,150 256,165" fill="#FEF08A"/>
      <polygon points="240,75 256,165 230,175" fill="#FBBF24"/>
      <!-- Crown -->
      <polygon points="195,190 230,175 265,195 250,225 205,220" fill="#FBBF24"/>
      <polygon points="230,175 265,195 285,180 255,168" fill="#FEF08A"/>
      <!-- Forehead & Snout -->
      <polygon points="265,195 335,215 285,250 250,225" fill="#FEF08A"/>
      <polygon points="335,215 385,238 348,272 285,250" fill="#FBBF24"/>
      <!-- Snout Tip -->
      <polygon points="385,238 410,256 385,282 348,272" fill="#92400E"/>
      <circle cx="399" cy="257" r="3" fill="#3FBF8F"/>
      <!-- Jaw -->
      <polygon points="385,282 348,272 315,310 350,318" fill="#451A03"/>
      <polygon points="348,272 285,250 315,310" fill="#78350F"/>
      <!-- Eye -->
      <polygon points="298,228 322,234 314,248 292,242" fill="#09131F"/>
      <polygon points="302,231 318,236 312,245 297,240" fill="#38BDF8"/>
      <circle cx="308" cy="238" r="4.5" fill="#FFFFFF"/>
      <!-- Throat -->
      <polygon points="205,220 250,225 285,250 260,300 195,275" fill="#92400E"/>
      <!-- Pillars -->
      <polygon points="260,300 315,310 295,415 240,415" fill="#10B981"/>
      <polygon points="315,310 350,318 330,415 295,415" fill="#047857"/>
      <polygon points="195,275 260,300 240,415 175,415" fill="#F59E0B"/>
      <polygon points="150,320 195,275 175,415 130,415" fill="#78350F"/>
      <!-- Alpha Trend Line -->
      <path d="M 115 390 L 180 340 L 235 365 L 320 270 L 375 220 L 420 170" fill="none" stroke="#FDE047" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="420" cy="170" r="8" fill="#FEF08A" stroke="#B45309" stroke-width="2"/>
    </g>
  </g>

  <!-- Right Side: Brand Presentation -->
  <g transform="translate(420, 110)">
    <!-- Eyebrow Badge -->
    <g transform="translate(0, 0)">
      <rect x="0" y="0" width="245" height="30" rx="15" fill="url(#chipBg)" stroke="#334155" stroke-width="1"/>
      <circle cx="16" cy="15" r="4.5" fill="#10B981"/>
      <text x="30" y="19" font-family="'IBM Plex Mono', monospace" font-size="11" font-weight="600" letter-spacing="0.12em" fill="#E2E8F0">
        ALPACA MARKETS · SIMULATED
      </text>
    </g>

    <!-- Main Title -->
    <text x="0" y="95" font-family="'Schibsted Grotesk', -apple-system, sans-serif" font-size="68" font-weight="800" letter-spacing="-0.03em">
      <tspan fill="#FFFFFF">Algo</tspan><tspan fill="url(#titlePaca)">Paca</tspan>
    </text>

    <!-- Tagline -->
    <text x="0" y="140" font-family="'Schibsted Grotesk', sans-serif" font-size="20" font-weight="500" fill="#94A3B8" letter-spacing="0.01em">
      Autonomous Quantitative Algorithmic Paper &amp; Live Trading Desk
    </text>

    <!-- Feature Pills -->
    <g transform="translate(0, 175)">
      <!-- Pill 1 -->
      <g transform="translate(0, 0)">
        <rect x="0" y="0" width="165" height="34" rx="8" fill="#1E293B" stroke="#334155" stroke-width="1"/>
        <text x="14" y="21" font-family="'IBM Plex Mono', monospace" font-size="12" font-weight="600" fill="#38BDF8">⚡ 5 Strategy Engines</text>
      </g>
      <!-- Pill 2 -->
      <g transform="translate(175, 0)">
        <rect x="0" y="0" width="160" height="34" rx="8" fill="#1E293B" stroke="#334155" stroke-width="1"/>
        <text x="14" y="21" font-family="'IBM Plex Mono', monospace" font-size="12" font-weight="600" fill="#FBBF24">🛡️ Risk Guardrails</text>
      </g>
      <!-- Pill 3 -->
      <g transform="translate(345, 0)">
        <rect x="0" y="0" width="165" height="34" rx="8" fill="#1E293B" stroke="#334155" stroke-width="1"/>
        <text x="14" y="21" font-family="'IBM Plex Mono', monospace" font-size="12" font-weight="600" fill="#34D399">🧪 Backtest Suite</text>
      </g>
      <!-- Pill 4 -->
      <g transform="translate(520, 0)">
        <rect x="0" y="0" width="155" height="34" rx="8" fill="#1E293B" stroke="#334155" stroke-width="1"/>
        <text x="14" y="21" font-family="'IBM Plex Mono', monospace" font-size="12" font-weight="600" fill="#E2E8F0">🌐 Modern UI Desk</text>
      </g>
    </g>
  </g>

  <!-- Bottom Border Line -->
  <line x1="0" y1="419" x2="1200" y2="419" stroke="url(#bannerBorder)" stroke-width="2"/>
</svg>
"""

def main():
    files = {
        "algopaca-mark.svg": MARK_SVG,
        "favicon.svg": FAVICON_SVG,
        "algopaca-logo.svg": LOGO_HORIZONTAL_SVG,
        "algopaca-banner.svg": BANNER_SVG,
    }
    
    for filename, content in files.items():
        file_path = OUT_DIR / filename
        file_path.write_text(content.strip() + "\n", encoding="utf-8")
        print(f"Generated: {file_path} ({len(content)} bytes)")

if __name__ == "__main__":
    main()
