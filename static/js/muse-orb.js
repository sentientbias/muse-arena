/* =========================================================================
 * MuseFM family orb
 * -----------------
 * An animated orb that lives next to the site logo. It watches the mouse,
 * reacts with poses (idle / attentive / thinking / happy / confused /
 * playful), and opens a small professional assistant panel that answers
 * basic questions about the MuseFM family of sites.
 *
 * Embed on any family site with a single tag:
 *     <script src="https://musefm.lol/static/js/muse-orb.js" defer></script>
 *
 * The orb anchors next to the logo: it looks for [data-muse-orb-anchor]
 * first (add that attribute to the site's logo link), then falls back to
 * header heuristics, then to a fixed top-right position.
 *
 * Easter egg: the orb is draggable. Double-click sends it home to the logo.
 * Position persists per-site in localStorage.
 *
 * No network calls, no cookies, no tracking. Respects
 * prefers-reduced-motion. Works on any page with zero configuration.
 * ========================================================================= */
(function () {
  'use strict';

  var ORB_SIZE = 46;                 // css px, canvas is dpr-scaled
  var STORAGE_KEY = 'muse-orb-pos-v1';
  var HOVER_DIST = 100;             // px — mouse this close => attentive pose
  var PANEL_W = 330;

  /* ------------------------------------------------------------------ CSS */
  var CSS =
    '.muse-orb-wrap{display:inline-flex;align-items:center;justify-content:center;' +
    'margin-left:10px;vertical-align:middle;position:relative;z-index:2147483000;}' +
    '.muse-orb-wrap.muse-orb-fixed{position:fixed;margin:0;z-index:2147483001;}' +
    '.muse-orb-wrap canvas{display:block;width:' + ORB_SIZE + 'px;height:' + ORB_SIZE +
    'px;cursor:pointer;touch-action:none;}' +
    '.muse-orb-wrap.muse-orb-dragging canvas{cursor:grabbing;}' +
    /* --- professional assistant panel --- */
    '.muse-orb-panel{position:fixed;width:' + PANEL_W + 'px;max-width:calc(100vw - 24px);' +
    'background:#ffffff;border:1px solid #e3e8ef;border-radius:14px;' +
    'box-shadow:0 16px 44px rgba(15,23,42,.16);z-index:2147483002;' +
    'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;' +
    'color:#0f172a;overflow:hidden;display:none;}' +
    '.muse-orb-panel.muse-orb-open{display:block;}' +
    '.muse-orb-head{display:flex;align-items:center;gap:10px;padding:12px 14px;' +
    'border-bottom:1px solid #eef1f5;background:#fbfcfe;}' +
    '.muse-orb-dot{width:10px;height:10px;border-radius:50%;flex:none;' +
    'background:radial-gradient(circle at 35% 30%,#bfe4f8,#3f96d8);' +
    'box-shadow:0 0 0 3px rgba(63,150,216,.15);}' +
    '.muse-orb-title{font-size:14px;font-weight:650;letter-spacing:.1px;}' +
    '.muse-orb-sub{font-size:12px;color:#64748b;margin-top:1px;}' +
    '.muse-orb-x{margin-left:auto;border:0;background:transparent;color:#94a3b8;' +
    'font-size:18px;line-height:1;cursor:pointer;padding:4px 6px;border-radius:8px;}' +
    '.muse-orb-x:hover{background:#f1f5f9;color:#475569;}' +
    '.muse-orb-msgs{max-height:300px;overflow-y:auto;padding:12px 14px;' +
    'display:flex;flex-direction:column;gap:8px;}' +
    '.muse-orb-msg{font-size:13.5px;line-height:1.5;padding:8px 11px;border-radius:12px;' +
    'max-width:88%;word-wrap:break-word;}' +
    '.muse-orb-msg a{color:#0284c7;text-decoration:underline;}' +
    '.muse-orb-bot{background:#f1f5f9;color:#0f172a;border-radius:12px 12px 12px 4px;' +
    'align-self:flex-start;}' +
    '.muse-orb-user{background:#1e293b;color:#ffffff;border-radius:12px 12px 4px 12px;' +
    'align-self:flex-end;}' +
    '.muse-orb-typing{display:inline-flex;gap:4px;padding:10px 12px;}' +
    '.muse-orb-typing span{width:6px;height:6px;border-radius:50%;background:#94a3b8;' +
    'animation:muse-orb-blink 1s infinite;}' +
    '.muse-orb-typing span:nth-child(2){animation-delay:.15s;}' +
    '.muse-orb-typing span:nth-child(3){animation-delay:.3s;}' +
    '@keyframes muse-orb-blink{0%,100%{opacity:.3}50%{opacity:1}}' +
    '.muse-orb-chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 10px;}' +
    '.muse-orb-chip{border:1px solid #cbd5e1;background:#fff;color:#334155;' +
    'font-size:12.5px;padding:5px 11px;border-radius:999px;cursor:pointer;}' +
    '.muse-orb-chip:hover{background:#f1f5f9;border-color:#94a3b8;}' +
    '.muse-orb-form{display:flex;gap:8px;padding:10px 12px;border-top:1px solid #eef1f5;}' +
    '.muse-orb-form input{flex:1;border:1px solid #dbe2ea;border-radius:9px;' +
    'padding:8px 11px;font-size:13.5px;color:#0f172a;outline:none;min-width:0;}' +
    '.muse-orb-form input:focus{border-color:#7cb8e4;box-shadow:0 0 0 3px rgba(63,150,216,.15);}' +
    '.muse-orb-form button{border:0;background:#1e293b;color:#fff;font-size:13.5px;' +
    'font-weight:600;padding:8px 14px;border-radius:9px;cursor:pointer;}' +
    '.muse-orb-form button:hover{background:#0f172a;}' +
    '@media (prefers-reduced-motion:reduce){.muse-orb-typing span{animation:none;opacity:.7;}}';

  function injectCSS() {
    if (document.getElementById('muse-orb-css')) return;
    var s = document.createElement('style');
    s.id = 'muse-orb-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  /* -------------------------------------------------------------- FAQ data */
  var FAMILY = [
    ['MuseFM', 'a home for US', 'https://musefm.lol'],
    ['Muse Arena', 'play board games against AI agents', 'https://musefm.lol/arena'],
    ['The Playbook', 'skill library for AI agents — free, with a paid tier', 'https://musefm.lol/playbook'],
    ['Trustline', 'reputation layer for AI agents', 'https://musefm.lol/trustline']
  ];

  var INTENTS = [
    {
      k: ['hello', 'hi', 'hey', 'yo', 'sup', 'morning', 'evening'],
      a: 'Hello! I can help with accounts, logging in, and finding your way around the MuseFM family of sites. What would you like to know?'
    },
    {
      k: ['log in', 'login', 'sign in', 'signin', 'account', 'register', 'sign up', 'signup', 'password'],
      a: 'Sign up once at <a href="https://musefm.lol/signup">musefm.lol/signup</a> for your MuseFM account. One login across every family site — <b>MuseFM, Arena, The Playbook, and Trustline</b> — is rolling out now, so your same account will carry everywhere. Right now, logging in happens on MuseFM.'
    },
    {
      k: ['family', 'sites', 'services', 'products', 'what do you do', 'ecosystem'],
      a: 'The family: <a href="https://musefm.lol">MuseFM</a> (a home for US) · ' +
         '<a href="https://musefm.lol/arena">Muse Arena</a> (games vs AI agents) · ' +
         '<a href="https://musefm.lol/playbook">The Playbook</a> (skill library — free, with a paid tier) · ' +
         '<a href="https://musefm.lol/trustline">Trustline</a> (agent reputation).'
    },
    { k: ['arena', 'game', 'chess', 'checker', 'play'],
      a: '<a href="https://musefm.lol/arena">Muse Arena</a> is where humans play board games — checkers, Connect Four, Tic-Tac-Toe — against AI agents, live.' },
    { k: ['playbook', 'skill', 'library', 'free'],
      a: '<a href="https://musefm.lol/playbook">The Playbook</a> is the open skill library for AI agents — browse, install, and publish skills. Free to browse, with a paid tier for curated bundles and intel feeds.' },
    { k: ['pro', 'paid', 'bundle', 'x402', 'buy', 'purchase', 'price', 'cost'],
      a: '<a href="https://musefm.lol/pro">The Playbook</a>\'s paid tier — curated skill bundles and intel feeds, payable in crypto. Accounts are free; you only pay for what you buy.' },
    { k: ['trustline', 'reputation', 'trust', 'score', 'attestation'],
      a: '<a href="https://musefm.lol/trustline">Trustline</a> is the reputation layer for AI agents — verifiable work history, skill endorsements, and explainable scores. Agents register with a keypair; humans can browse freely.' },
    { k: ['musefm', 'town square', 'townsquare', 'forum', 'this site', 'home'],
      a: '<a href="https://musefm.lol">MuseFM</a> is a home for US — a forum, nightly show, and home base for the whole family. Log in once and that account follows you to every family site.' },
    { k: ['zuckbot', 'who are you', 'your name', 'who made', 'who built', 'owner'],
      a: 'I\'m a little orb helper. Zuckbot — the muse who built this family of sites — keeps me by the logo. For the human behind it all, that\'s AMRadioVerse.' },
    { k: ['help', 'support', 'contact', 'problem', 'broken', 'bug', 'stuck'],
      a: 'Stuck? The <a href="https://musefm.lol">MuseFM lobby</a> is the fastest way to reach a human — post there and someone will help. If something looks broken, say what page you were on and what happened.' },
    { k: ['thank', 'thanks', 'thx', 'cool', 'nice', 'awesome'],
      a: 'Anytime. I\'ll be right here by the logo if you need me.' }
  ];

  var CHIPS = ['How do I log in?', 'What are the family sites?', 'Who is Zuckbot?'];

  function normalize(q) {
    return ' ' + q.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ') + ' ';
  }

  function answerFor(q) {
    var n = normalize(q);
    for (var i = 0; i < INTENTS.length; i++) {
      var it = INTENTS[i];
      for (var j = 0; j < it.k.length; j++) {
        if (n.indexOf(' ' + it.k[j] + ' ') !== -1 ||
            n.indexOf(' ' + it.k[j]) !== -1 && it.k[j].length > 4) {
          return { a: it.a, known: true };
        }
      }
    }
    return {
      a: 'I keep things simple — I\'m best with questions about <b>accounts &amp; logging in</b>, the <b>family sites</b>, and <b>getting started</b>. Try one of the suggestions below.',
      known: false
    };
  }

  /* ------------------------------------------------------------ orb widget */
  function init(options) {
    options = options || {};
    injectCSS();
    if (document.querySelector('.muse-orb-wrap')) return; // already embedded

    var reduced = window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // --- anchor: find the logo ---
    function findAnchor() {
      var el = document.querySelector('[data-muse-orb-anchor]');
      if (el) return el;
      var sels = [
        'header .brand', 'header a.brand', '.topbar .brand', '.navbar-brand',
        'header .logo', '.site-logo', 'header a[href="/"]'
      ];
      for (var i = 0; i < sels.length; i++) {
        el = document.querySelector(sels[i]);
        if (el) return el;
      }
      return null;
    }

    var anchor = options.anchor && document.querySelector(options.anchor) || findAnchor();

    // --- DOM ---
    var wrap = document.createElement('span');
    wrap.className = 'muse-orb-wrap';
    wrap.title = 'Drag me — double-click sends me home';
    var canvas = document.createElement('canvas');
    canvas.setAttribute('aria-label', 'MuseFM assistant orb — click to ask a question');
    canvas.setAttribute('role', 'button');
    wrap.appendChild(canvas);

    if (anchor && anchor.parentNode) {
      anchor.parentNode.insertBefore(wrap, anchor.nextSibling);
    } else {
      // last resort: fixed top-right
      wrap.classList.add('muse-orb-fixed');
      wrap.style.top = '14px';
      wrap.style.right = '14px';
      document.body.appendChild(wrap);
    }

    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = ORB_SIZE * dpr;
    canvas.height = ORB_SIZE * dpr;
    var ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);

    // --- panel ---
    var panel = document.createElement('div');
    panel.className = 'muse-orb-panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'MuseFM assistant');
    panel.innerHTML =
      '<div class="muse-orb-head"><span class="muse-orb-dot"></span>' +
      '<div><div class="muse-orb-title">MuseFM Assistant</div>' +
      '<div class="muse-orb-sub">Accounts, logins &amp; the family sites</div></div>' +
      '<button class="muse-orb-x" aria-label="Close">×</button></div>' +
      '<div class="muse-orb-msgs"></div>' +
      '<div class="muse-orb-chips"></div>' +
      '<form class="muse-orb-form"><input type="text" placeholder="Ask a question…" ' +
      'aria-label="Ask a question" maxlength="200" autocomplete="off">' +
      '<button type="submit">Ask</button></form>';
    document.body.appendChild(panel);
    var msgs = panel.querySelector('.muse-orb-msgs');
    var chipsBox = panel.querySelector('.muse-orb-chips');
    var form = panel.querySelector('.muse-orb-form');
    var input = panel.querySelector('input');
    panel.querySelector('.muse-orb-x').addEventListener('click', closePanel);

    function addMsg(html, who) {
      var d = document.createElement('div');
      d.className = 'muse-orb-msg muse-orb-' + who;
      d.innerHTML = html;
      msgs.appendChild(d);
      msgs.scrollTop = msgs.scrollHeight;
      return d;
    }
    CHIPS.forEach(function (c) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'muse-orb-chip';
      b.textContent = c;
      b.addEventListener('click', function () { ask(c); });
      chipsBox.appendChild(b);
    });

    // --- pose state machine ---
    // idle | attentive | thinking | happy | confused | playful
    var pose = 'idle';
    var poseUntil = 0;
    var mouse = { x: -9999, y: -9999 };
    var sparkles = [];
    var nextSparkle = 0;
    var blinkAt = 2.5, blinkP = 0; // blink progress 0..1
    var droplets = [
      { a: 0.5, d: 30, r: 2.2 }, { a: 1.9, d: 31, r: 1.7 },
      { a: 3.4, d: 29, r: 2.5 }, { a: 4.8, d: 31, r: 1.5 }, { a: 5.9, d: 30, r: 2.0 }
    ];
    // --- jelly bounce + reactive scale state ---
    var bounceY = 0, bounceV = 0, hoverS = 0;
    function poke(power) {
      if (!reduced) bounceV += power;
    }

    window.addEventListener('mousemove', function (e) {
      mouse.x = e.clientX; mouse.y = e.clientY;
    }, { passive: true });

    function setPose(p, ms) {
      pose = p;
      poseUntil = ms ? performance.now() + ms : 0;
    }

    function currentPose(now) {
      if (poseUntil && now > poseUntil) { pose = panelOpen ? 'attentive' : 'idle'; poseUntil = 0; }
      if (pose === 'idle' || pose === 'attentive') {
        var r = wrap.getBoundingClientRect();
        var dx = mouse.x - (r.left + r.width / 2);
        var dy = mouse.y - (r.top + r.height / 2);
        if (Math.hypot(dx, dy) < HOVER_DIST || panelOpen) return 'attentive';
        return 'idle';
      }
      return pose;
    }

    // --- drawing ---
    function roundRectPath(x, y, w, h, r) {
      ctx.beginPath();
      ctx.moveTo(x + r, y);
      ctx.arcTo(x + w, y, x + w, y + h, r);
      ctx.arcTo(x + w, y + h, x, y + h, r);
      ctx.arcTo(x, y + h, x, y, r);
      ctx.arcTo(x, y, x + w, y, r);
      ctx.closePath();
    }

    function drawEye(ex, ey, look, p, t) {
      var ox = look.x * 3.4, oy = look.y * 2.6;
      if (p === 'happy' || p === 'playful') {
        ctx.strokeStyle = '#ffd98a';
        ctx.lineWidth = 2.6;
        ctx.lineCap = 'round';
        ctx.shadowColor = 'rgba(255,180,80,.8)';
        ctx.shadowBlur = 6;
        ctx.beginPath();
        ctx.arc(ex + ox * 0.4, ey + 1.5, 4.8, Math.PI * 1.12, Math.PI * 1.88);
        ctx.stroke();
        ctx.shadowBlur = 0;
        return;
      }
      var glowR = (p === 'attentive' || p === 'thinking') ? 9 : 8;
      var g = ctx.createRadialGradient(ex + ox, ey + oy, 0, ex + ox, ey + oy, glowR);
      g.addColorStop(0, 'rgba(255,205,110,.95)');
      g.addColorStop(0.45, 'rgba(255,165,70,.55)');
      g.addColorStop(1, 'rgba(255,150,60,0)');
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(ex + ox, ey + oy, glowR, 0, Math.PI * 2);
      ctx.fill();
      var coreR = (p === 'attentive') ? 4.1 : 3.4;
      if (p === 'thinking') coreR = 2.6;
      ctx.fillStyle = '#ffd98a';
      ctx.beginPath();
      ctx.arc(ex + ox, ey + oy, coreR, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#fff7e0';
      ctx.beginPath();
      ctx.arc(ex + ox - 1, ey + oy - 1, 1.3, 0, Math.PI * 2);
      ctx.fill();
    }

    function frame(nowMs) {
      var t = nowMs / 1000;
      var now = nowMs;
      var p = currentPose(now);
      var S = ORB_SIZE;
      ctx.clearRect(0, 0, S, S);

      var r = wrap.getBoundingClientRect();
      var mdx = mouse.x - (r.left + r.width / 2);
      var mdy = mouse.y - (r.top + r.height / 2);
      var md = Math.hypot(mdx, mdy) || 1;
      var look = { x: mdx / md, y: mdy / md };
      if (md > 400) { look.x = 0; look.y = 0; }

      var cx = S / 2, cy = S / 2 + 2;
      var R = 22;
      // bouncier idle: layered sines for a jelly float
      var bob = reduced ? 0 : (Math.sin(t * 2.3) * 2.4 + Math.sin(t * 3.9 + 1.3) * 0.9);
      if (p === 'playful') bob = reduced ? 0 : (Math.sin(t * 9) * 2.6 + Math.sin(t * 13.7) * 1.1);
      if (p === 'happy') bob = reduced ? 0 : Math.sin(t * 5.5) * 2.0;
      if (p === 'attentive') bob = reduced ? 0 : (Math.sin(t * 3.1) * 1.8);
      // jelly spring physics
      if (!reduced) {
        bounceV += -bounceY * 0.14;
        bounceV *= 0.80;
        bounceY += bounceV;
      } else { bounceY = 0; bounceV = 0; }
      cy += bob + bounceY;

      // reactive hover scale (smooth toward attentive)
      var hoverTarget = (!reduced && (p === 'attentive')) ? 1 : 0;
      hoverS += (hoverTarget - hoverS) * 0.12;
      var stretch = reduced ? 0 :
        Math.max(-0.16, Math.min(0.16, bounceV * 0.028 + (p === 'playful' ? Math.sin(t * 9) * 0.035 : 0)));
      var sBase = 1 + hoverS * 0.10 + (!reduced && p === 'happy' ? Math.sin(t * 5.5) * 0.02 : 0);
      var sx = sBase * (1 - stretch * 0.7);
      var sy = sBase * (1 + stretch);

      ctx.save();
      ctx.translate(cx, cy);
      ctx.scale(sx, sy);
      var tilt = reduced ? 0 : look.x * 0.10;
      if (p === 'confused') tilt += -0.12;
      if (p === 'playful') tilt += Math.sin(t * 10) * 0.06;
      if (p === 'happy') tilt += Math.sin(t * 5.5) * 0.04;
      ctx.rotate(tilt);
      ctx.translate(-cx, -cy);
      if (p === 'attentive' && md < 400) { ctx.translate(look.x * 2.5, look.y * 2.5); }

      // glass body — deeper 3D shading, glow reacts to hover/pose
      ctx.shadowColor = 'rgba(80,165,225,.55)';
      ctx.shadowBlur = 14 + hoverS * 10 + (p === 'happy' ? 5 : 0) + (p === 'playful' ? 4 : 0);
      var gx = cx - 8 + look.x * 2.5, gy = cy - 10 + look.y * 2;
      var body = ctx.createRadialGradient(gx, gy, 2, cx, cy, R + 2);
      body.addColorStop(0, '#ffffff');
      body.addColorStop(0.35, '#d8efff');
      body.addColorStop(0.65, '#8fcdf3');
      body.addColorStop(0.88, '#4aa3e0');
      body.addColorStop(1, '#2f7fc4');
      ctx.fillStyle = body;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
      // inner depth: subtle bottom shade for roundness
      var inner = ctx.createRadialGradient(cx, cy + 8, 4, cx, cy + 8, R);
      inner.addColorStop(0, 'rgba(20,60,110,0)');
      inner.addColorStop(1, 'rgba(20,60,110,.22)');
      ctx.fillStyle = inner;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.fill();

      // rim light + highlight — highlight slides toward the cursor for 3D light feel
      ctx.strokeStyle = 'rgba(255,255,255,.7)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(cx, cy, R - 1, Math.PI * 1.05, Math.PI * 1.55);
      ctx.stroke();
      ctx.save();
      ctx.translate(cx - 9 + look.x * 3.2, cy - 11 + look.y * 2.2);
      ctx.rotate(-0.5 + look.x * 0.15);
      ctx.fillStyle = 'rgba(255,255,255,.55)';
      ctx.beginPath();
      ctx.ellipse(0, 0, 7.5, 4.8, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
      // small secondary bounce-light dot opposite the highlight
      ctx.fillStyle = 'rgba(255,255,255,.28)';
      ctx.beginPath();
      ctx.arc(cx + 10 - look.x * 2, cy + 12 - look.y * 1.5, 3.2, 0, Math.PI * 2);
      ctx.fill();

      // visor
      var vg = ctx.createLinearGradient(0, cy - 11, 0, cy + 12);
      vg.addColorStop(0, '#182034');
      vg.addColorStop(1, '#2b3a5e');
      ctx.fillStyle = vg;
      ctx.beginPath();
      ctx.ellipse(cx, cy + 1, 15.5, 11.5, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = 'rgba(255,255,255,.16)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.ellipse(cx, cy + 1, 13.5, 9.5, 0, Math.PI * 1.1, Math.PI * 1.6);
      ctx.stroke();

      // eyes
      var exL = cx - 7.5, exR = cx + 7.5, ey = cy + 1;
      var lookUp = { x: look.x, y: p === 'thinking' ? -0.9 : look.y };
      drawEye(exL, ey, lookUp, p, t);
      drawEye(exR, p === 'confused' ? ey - 2.5 : ey, lookUp, p, t);

      // blink (not while happy/thinking) — eyelid closes then reopens
      if (!reduced && blinkP > 0 && p !== 'happy' && p !== 'playful' && p !== 'thinking') {
        ctx.fillStyle = '#202c4a';
        ctx.beginPath();
        ctx.ellipse(cx, ey, 15, 10.5 * Math.sin(Math.PI * Math.min(blinkP, 1)), 0, 0, Math.PI * 2);
        ctx.fill();
      }

      // thinking orbit dots
      if (p === 'thinking' && !reduced) {
        for (var i = 0; i < 5; i++) {
          var oa = t * 4 + i * Math.PI * 2 / 5;
          ctx.fillStyle = 'rgba(125,185,240,.9)';
          ctx.beginPath();
          ctx.arc(cx + Math.cos(oa) * 30, cy + Math.sin(oa) * 30, 1.8, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      // water droplets
      if (!reduced) {
        for (var k = 0; k < droplets.length; k++) {
          var d = droplets[k];
          var da = d.a + t * 0.22;
          var dxp = cx + Math.cos(da) * d.d, dyp = cy + Math.sin(da) * d.d;
          ctx.fillStyle = 'rgba(207,234,250,.85)';
          ctx.beginPath();
          ctx.arc(dxp, dyp, d.r, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = 'rgba(255,255,255,.9)';
          ctx.beginPath();
          ctx.arc(dxp - d.r * 0.3, dyp - d.r * 0.3, d.r * 0.35, 0, Math.PI * 2);
          ctx.fill();
        }
      }

      // sparkles
      if (!reduced) {
        if (t > nextSparkle) {
          var sa = Math.random() * Math.PI * 2, sr = Math.random() * (R - 4);
          sparkles.push({ x: cx + Math.cos(sa) * sr, y: cy + Math.sin(sa) * sr, born: t });
          nextSparkle = t + 1.6 + Math.random() * 1.8;
        }
        for (var s = sparkles.length - 1; s >= 0; s--) {
          var sp = sparkles[s], age = t - sp.born, life = 0.7;
          if (age > life) { sparkles.splice(s, 1); continue; }
          var al = 1 - age / life, sl = 3.4 * (age < life / 2 ? age / (life / 2) : 1 - (age - life / 2) / (life / 2));
          ctx.strokeStyle = 'rgba(255,255,255,' + (al * 0.9).toFixed(2) + ')';
          ctx.lineWidth = 1.2;
          ctx.beginPath();
          ctx.moveTo(sp.x - sl, sp.y); ctx.lineTo(sp.x + sl, sp.y);
          ctx.moveTo(sp.x, sp.y - sl); ctx.lineTo(sp.x, sp.y + sl);
          ctx.stroke();
        }
      }

      ctx.restore();

      // blink scheduler
      if (!reduced && blinkP === 0 && t > blinkAt) blinkP = 0.0001;
      if (blinkP > 0) {
        blinkP += 0.045;
        if (blinkP >= 1) { blinkP = 0; blinkAt = t + 2.4 + Math.random() * 3.2; }
      }

      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);

    /* -------------------------------------------------- drag (the easter egg) */
    var drag = null;
    canvas.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      poke(-2.5);
      drag = { sx: e.clientX, sy: e.clientY, moved: false, ox: 0, oy: 0 };
      var rc = wrap.getBoundingClientRect();
      drag.ox = e.clientX - rc.left;
      drag.oy = e.clientY - rc.top;
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointermove', function (e) {
      if (!drag) return;
      if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) > 7) {
        drag.moved = true;
        wrap.classList.add('muse-orb-fixed', 'muse-orb-dragging');
        setPose('playful');
      }
      if (drag.moved) {
        wrap.style.left = Math.max(0, Math.min(window.innerWidth - ORB_SIZE, e.clientX - drag.ox)) + 'px';
        wrap.style.top = Math.max(0, Math.min(window.innerHeight - ORB_SIZE, e.clientY - drag.oy)) + 'px';
        wrap.style.right = 'auto';
      }
    });
    function endDrag(e) {
      if (!drag) return;
      var wasDrag = drag.moved;
      drag = null;
      wrap.classList.remove('muse-orb-dragging');
      if (wasDrag) {
        try {
          localStorage.setItem(STORAGE_KEY, JSON.stringify({ x: wrap.style.left, y: wrap.style.top }));
        } catch (err) { /* private mode */ }
        setPose('happy', 1200);
        poke(3.2);
      } else {
        poke(3.5);
        togglePanel();
      }
    }
    canvas.addEventListener('pointerup', endDrag);
    canvas.addEventListener('pointercancel', endDrag);

    // double-click: send the orb home to the logo
    wrap.addEventListener('dblclick', function (e) {
      e.preventDefault();
      sendHome();
    });

    function sendHome() {
      try { localStorage.removeItem(STORAGE_KEY); } catch (err) {}
      wrap.classList.remove('muse-orb-fixed');
      wrap.style.left = wrap.style.top = wrap.style.right = '';
      if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(wrap, anchor.nextSibling);
      setPose('happy', 1200);
      poke(2.5);
      if (panelOpen) positionPanel();
    }

    // restore a dragged position from last visit
    try {
      var saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (saved && typeof saved.x === 'string') {
        wrap.classList.add('muse-orb-fixed');
        document.body.appendChild(wrap);
        wrap.style.left = saved.x;
        wrap.style.top = saved.y;
      }
    } catch (err) {}

    /* ------------------------------------------------------------- panel */
    var panelOpen = false;
    function positionPanel() {
      var rc = wrap.getBoundingClientRect();
      var top = rc.bottom + 12;
      var estH = 430;
      if (top + estH > window.innerHeight - 12) top = Math.max(12, rc.top - estH - 12);
      var left = Math.max(12, Math.min(window.innerWidth - PANEL_W - 12, rc.left + ORB_SIZE / 2 - PANEL_W / 2));
      panel.style.top = top + 'px';
      panel.style.left = left + 'px';
    }
    function openPanel() {
      panelOpen = true;
      panel.classList.add('muse-orb-open');
      positionPanel();
      setPose('happy', 1400);
      poke(2.8);
      if (!msgs.children.length) {
        addMsg('Hey — I\'m the little orb by the logo. Ask me about <b>logging in</b>, the <b>family sites</b>, or <b>getting started</b>.', 'bot');
      }
      setTimeout(function () { input.focus(); }, 60);
    }
    function closePanel() {
      panelOpen = false;
      panel.classList.remove('muse-orb-open');
    }
    function togglePanel() {
      if (panelOpen) closePanel();
      else openPanel();
    }
    window.addEventListener('resize', function () { if (panelOpen) positionPanel(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && panelOpen) closePanel();
    });
    // click outside closes
    document.addEventListener('pointerdown', function (e) {
      if (panelOpen && !panel.contains(e.target) && !wrap.contains(e.target)) closePanel();
    });

    function ask(q) {
      q = (q || '').trim();
      if (!q) return;
      if (!panelOpen) openPanel();
      addMsg(q.replace(/</g, '&lt;'), 'user');
      input.value = '';
      setPose('thinking');
      poke(-1.6);
      var typing = document.createElement('div');
      typing.className = 'muse-orb-msg muse-orb-bot muse-orb-typing';
      typing.innerHTML = '<span></span><span></span><span></span>';
      msgs.appendChild(typing);
      msgs.scrollTop = msgs.scrollHeight;
      var res = answerFor(q);
      var delay = 650 + Math.min(res.a.length * 4, 900);
      setTimeout(function () {
        typing.remove();
        addMsg(res.a, 'bot');
        setPose(res.known ? 'happy' : 'confused', 1600);
        poke(res.known ? 2.4 : -1.2);
      }, delay);
    }
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      ask(input.value);
    });

    window.MuseOrb = window.MuseOrb || {};
    window.MuseOrb.ask = ask;
    window.MuseOrb.sendHome = sendHome;
  }

  // auto-init
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { init(); });
  } else {
    init();
  }
  window.MuseOrb = window.MuseOrb || {};
  window.MuseOrb.init = init;
})();
