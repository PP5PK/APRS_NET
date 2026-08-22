<?php
/**
 * APRS PKTNET - participants & certificates for pp5pk.net (single page)
 *
 * Lists every operator who checked into each net (whether or not they asked for
 * a certificate), read from the bot's database. Operators who have a generated
 * certificate get a download icon; clicking their card downloads the PDF.
 *
 * The site and the PKTNET bot are assumed to run on the same server.
 * Read-only for the database; certificates are streamed by this script.
 */

// ---- configuration -------------------------------------------------------
$DB_PATH   = '/var/lib/pktnet/pktnet.db';           // bot database
$USERS_DB  = '/var/lib/pktnet/certs/users.db';      // RadioID names (optional)
$CERT_DIR  = '/var/lib/pktnet/certs';               // where the PDFs live
$NET_PREFIX = 'PKTNET';                             // certificate filename prefix
$SITE_URL  = 'https://pp5pk.net';                   // link back to the site
$SHOW_NAMES = true;                                 // show operator names
// --------------------------------------------------------------------------

$FILE_RE = '/^' . preg_quote($NET_PREFIX, '/')
         . '_ev(\d+)_([A-Za-z0-9._-]+)\.pdf$/i';

function base_call($cs) {
    $p = strpos($cs, '-');
    return strtoupper($p === false ? $cs : substr($cs, 0, $p));
}
function net_label($n) { return sprintf('Net #%02d', $n); }
function fmt_date($iso) {                            // YYYY-MM-DD -> DD/MM/YYYY
    $d = DateTime::createFromFormat('Y-m-d', (string)$iso);
    return $d ? $d->format('d/m/Y') : htmlspecialchars((string)$iso);
}
function fmt_stamp($iso) {                           // ISO -> YYYY/MM/DD, HH:MM:SSz (UTC)
    try {
        $d = new DateTime($iso); $d->setTimezone(new DateTimeZone('UTC'));
        return $d->format('Y/m/d, H:i:s') . 'z';
    } catch (Exception $e) { return ''; }
}

/* ---- download handler: stream a single certificate PDF ---- */
if (isset($_GET['dl'])) {
    $name = basename($_GET['dl']);                  // strip any path component
    if (!preg_match($FILE_RE, $name)) {             // strict name = no traversal
        http_response_code(400); exit('Invalid file.');
    }
    $path = $CERT_DIR . '/' . $name;                // may be a symlink
    if (!is_file($path)) { http_response_code(404); exit('Not found.'); }
    header('Content-Type: application/pdf');
    header('Content-Disposition: attachment; filename="' . $name . '"');
    header('Content-Length: ' . filesize($path));
    header('X-Content-Type-Options: nosniff');
    readfile($path);
    exit;
}

/* ---- which certificates exist? (event number + base call -> filename) ---- */
$certs = [];   // "eid|BASECALL" => filename
if (is_dir($CERT_DIR) && ($dh = @opendir($CERT_DIR))) {
    while (($f = readdir($dh)) !== false) {
        if (preg_match($FILE_RE, $f, $m)) {
            $certs[(int)$m[1] . '|' . strtoupper($m[2])] = $f;
        }
    }
    closedir($dh);
}

/* ---- read participants ---- */
$events = [];   // eid => ['date','status','rows'=>[ ['call','name','stamp','cert'], ...]]
$total = 0; $withCert = 0; $err = '';

try {
    $db = new PDO('sqlite:' . $DB_PATH);
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);

    $cc = [];   // confirmed names from the certificate flow
    foreach ($db->query("SELECT callsign, name FROM cert_contacts") as $r) {
        if (!empty($r['name'])) $cc[strtoupper($r['callsign'])] = $r['name'];
    }

    $rows = $db->query(
        "SELECT e.event_id AS eid, e.event_date AS ed, e.status AS st,
                c.callsign AS call, c.ts_utc AS ts
         FROM events e JOIN checkins c ON c.event_id = e.event_id
         ORDER BY e.event_id DESC, c.ts_utc ASC")->fetchAll(PDO::FETCH_ASSOC);

    $names = [];
    if ($SHOW_NAMES && is_file($USERS_DB)) {
        $bases = [];
        foreach ($rows as $r) $bases[base_call($r['call'])] = true;
        if ($bases) {
            try {
                $udb = new PDO('sqlite:' . $USERS_DB);
                $st = $udb->prepare("SELECT name FROM users WHERE callsign = ?");
                foreach (array_keys($bases) as $b) {
                    $st->execute([$b]);
                    $n = $st->fetchColumn();
                    if ($n) $names[$b] = $n;
                }
                $udb = null;
            } catch (Exception $e) { /* names are optional */ }
        }
    }

    foreach ($rows as $r) {
        $eid = (int)$r['eid'];
        if (!isset($events[$eid])) {
            $events[$eid] = ['date' => $r['ed'], 'status' => $r['st'], 'rows' => []];
        }
        $b = base_call($r['call']);
        $cert = $certs[$eid . '|' . $b] ?? null;
        if ($cert) $withCert++;
        $events[$eid]['rows'][] = [
            'call' => strtoupper($r['call']),
            'name' => $cc[$b] ?? ($names[$b] ?? ''),
            'stamp' => fmt_stamp($r['ts']),
            'cert' => $cert,
        ];
        $total++;
    }
    $db = null;
} catch (Exception $e) {
    $err = 'Não foi possível ler a base de dados.';
}
?>
<!DOCTYPE html>
<html lang="pt-BR" data-theme="dark">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Participantes &amp; Certificados &middot; APRS PKTNET &middot; PP5PK</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet" />
<style>
  :root{ --radius:8px; --font-mono:'Space Mono',monospace; --font-sans:'IBM Plex Sans',sans-serif; }
  [data-theme="dark"]{
    --bg:#0D1117; --surface:#161B22; --surface2:#1E2530; --border:#2A3240;
    --primary:#4D9EFF; --accent:#FFB347; --text:#E6EDF3; --text-muted:#8B949E;
    --grid-color:rgba(77,158,255,0.06); --glow:rgba(77,158,255,0.15); --ok:#3FB950; --ok-bd:#23502E;
  }
  [data-theme="light"]{
    --bg:#F0EDE6; --surface:#FFFFFF; --surface2:#F8F5EF; --border:#D8D0C4;
    --primary:#0055BB; --accent:#D06B00; --text:#1A1A2E; --text-muted:#5C6070;
    --grid-color:rgba(0,85,187,0.07); --glow:rgba(0,85,187,0.10); --ok:#1A7F37; --ok-bd:#AECBB4;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:var(--font-sans);background:var(--bg);color:var(--text);
    line-height:1.6;min-height:100vh;overflow-x:hidden;transition:background .25s,color .25s;}
  body::before{content:'';position:fixed;inset:0;background-image:
    linear-gradient(var(--grid-color) 1px,transparent 1px),
    linear-gradient(90deg,var(--grid-color) 1px,transparent 1px);
    background-size:40px 40px;pointer-events:none;z-index:0;}
  .wrap{position:relative;z-index:1;max-width:1000px;margin:0 auto;padding:2.5rem 1.25rem 4rem;}
  a{color:inherit;text-decoration:none;}
  .top{display:flex;justify-content:space-between;align-items:center;gap:1rem;margin-bottom:2rem;flex-wrap:wrap;}
  .back{font-family:var(--font-mono);font-size:.8rem;color:var(--text-muted);
    display:inline-flex;align-items:center;gap:.4rem;transition:.2s;}
  .back:hover{color:var(--primary);}
  #theme-toggle{background:none;border:1px solid var(--border);border-radius:var(--radius);
    padding:6px 10px;cursor:pointer;color:var(--text-muted);font-size:1rem;
    transition:border-color .2s,color .2s;display:flex;align-items:center;gap:6px;}
  #theme-toggle:hover{border-color:var(--primary);color:var(--primary);}
  #theme-toggle .label{font-family:var(--font-mono);font-size:.7rem;}
  .tag{font-family:var(--font-mono);font-size:.8rem;color:var(--accent);letter-spacing:.05em;}
  h1{font-size:clamp(1.8rem,5vw,2.8rem);line-height:1.1;margin:.3rem 0 .6rem;font-weight:600;letter-spacing:-.02em;}
  h1 .b{color:var(--primary);}
  .lede{color:var(--text-muted);max-width:660px;font-size:.95rem;}
  .lede code{font-family:var(--font-mono);color:var(--primary);background:var(--surface2);padding:.05em .4em;border-radius:4px;font-size:.85em;}
  .legend{font-family:var(--font-mono);font-size:.72rem;color:var(--text-muted);display:inline-flex;align-items:center;gap:.4rem;margin-top:.9rem;}
  .legend svg{color:var(--accent);}
  .search{margin:1.6rem 0 .5rem;position:relative;max-width:420px;}
  .search input{width:100%;padding:.75rem 1rem .75rem 2.5rem;background:var(--surface);
    border:1px solid var(--border);border-radius:var(--radius);color:var(--text);
    font-family:var(--font-mono);font-size:.9rem;transition:.2s;}
  .search input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--glow);}
  .search svg{position:absolute;left:.85rem;top:50%;transform:translateY(-50%);color:var(--text-muted);pointer-events:none;}
  .count{font-family:var(--font-mono);font-size:.8rem;color:var(--text-muted);margin-bottom:2rem;}
  .net{margin-bottom:2.25rem;}
  .net-head{display:flex;align-items:baseline;gap:.75rem;margin-bottom:1rem;padding-bottom:.5rem;
    border-bottom:1px solid var(--border);flex-wrap:wrap;}
  .net-head h2{font-size:1.25rem;font-weight:600;}
  .net-head .sub{font-family:var(--font-mono);font-size:.78rem;color:var(--text-muted);}
  .net-head .live{color:var(--ok);border:1px solid var(--ok-bd);background:color-mix(in srgb,var(--ok) 8%,transparent);
    padding:.1rem .5rem;border-radius:100px;font-size:.68rem;}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:.6rem;}
  .op{display:flex;align-items:center;gap:.7rem;padding:.6rem .85rem;background:var(--surface);
    border:1px solid var(--border);border-radius:var(--radius);transition:.15s;}
  .op .idx{font-family:var(--font-mono);font-size:.7rem;color:var(--text-muted);opacity:.6;min-width:1.6em;text-align:right;flex-shrink:0;}
  .op .info{display:flex;flex-direction:column;min-width:0;flex:1;}
  .op .call{font-family:var(--font-mono);font-weight:700;font-size:.92rem;letter-spacing:.02em;}
  .op .name{font-size:.75rem;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .op .stamp{font-family:var(--font-mono);font-size:.68rem;color:var(--accent);opacity:.85;margin-top:.1rem;}
  .op .dl{color:var(--text-muted);flex-shrink:0;opacity:.5;}
  /* with a certificate: clickable download */
  a.op{cursor:pointer;}
  a.op:hover{border-color:var(--primary);transform:translateY(-2px);box-shadow:0 8px 20px rgba(0,0,0,.18);}
  a.op .dl{color:var(--accent);opacity:1;}
  a.op:hover .dl{color:var(--primary);}
  div.op{cursor:default;}
  .empty,.err{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
    padding:2.5rem 1.5rem;text-align:center;color:var(--text-muted);}
  .empty strong{color:var(--text);display:block;margin-bottom:.4rem;font-size:1.05rem;}
  .err{border-color:#5A2A2A;}
  .no-match{display:none;color:var(--text-muted);font-family:var(--font-mono);font-size:.85rem;padding:1rem 0;}
  footer{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--border);
    font-family:var(--font-mono);font-size:.8rem;color:var(--text-muted);text-align:center;}
  footer .b{color:var(--primary);}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <a class="back" href="<?= htmlspecialchars($SITE_URL) ?>">&larr; pp5pk.net</a>
    <button id="theme-toggle" aria-label="Alternar tema">
      <span id="theme-icon"></span><span class="label" id="theme-label"></span>
    </button>
  </div>

  <p class="tag">// APRS PKTNET</p>
  <h1>Participantes &amp; <span class="b">Certificados</span></h1>
  <p class="lede">Todos que fizeram check-in em cada Net. Quem tem certificado
    mostra o &iacute;cone de download &mdash; clique no quadro para baixar. Quer
    participar? Envie <code>CHECK</code> para <code>PKTNET</code> via APRS.</p>
  <?php if (!$err && $total > 0): ?>
  <div class="legend">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>
    quadro com este &iacute;cone = certificado dispon&iacute;vel para download
  </div>
  <?php endif; ?>

  <?php if ($err): ?>
  <div class="err"><?= htmlspecialchars($err) ?></div>

  <?php elseif ($total > 0): ?>
  <div class="search">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
    <input id="q" type="text" placeholder="Buscar indicativo ou nome..." autocomplete="off" spellcheck="false" />
  </div>
  <p class="count"><?= $total ?> check-in<?= $total == 1 ? '' : 's' ?>
     em <?= count($events) ?> Net<?= count($events) == 1 ? '' : 's' ?>
     &middot; <?= $withCert ?> com certificado</p>

  <?php foreach ($events as $eid => $ev): ?>
  <section class="net" data-net="<?= $eid ?>">
    <div class="net-head">
      <h2><?= net_label($eid) ?></h2>
      <span class="sub"><?= fmt_date($ev['date']) ?> &middot; <?= count($ev['rows']) ?> participante<?= count($ev['rows']) == 1 ? '' : 's' ?></span>
      <?php if ($ev['status'] === 'open'): ?><span class="live">ao vivo</span><?php endif; ?>
    </div>
    <div class="grid">
      <?php $i = 0; foreach ($ev['rows'] as $op): $i++;
            $s = htmlspecialchars(strtoupper($op['call'] . ' ' . $op['name']));
            $dlicon = '<span class="dl"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg></span>';
            $inner = '<span class="idx">' . $i . '</span><span class="info">'
                   . '<span class="call">' . htmlspecialchars($op['call']) . '</span>'
                   . ($op['name'] ? '<span class="name">' . htmlspecialchars($op['name']) . '</span>' : '')
                   . ($op['stamp'] ? '<span class="stamp">' . htmlspecialchars($op['stamp']) . '</span>' : '')
                   . '</span>' . ($op['cert'] ? $dlicon : '');
            if ($op['cert']): ?>
      <a class="op" data-s="<?= $s ?>" href="?dl=<?= rawurlencode($op['cert']) ?>" title="Baixar certificado de <?= htmlspecialchars($op['call']) ?>"><?= $inner ?></a>
      <?php else: ?>
      <div class="op" data-s="<?= $s ?>"><?= $inner ?></div>
      <?php endif; ?>
      <?php endforeach; ?>
    </div>
  </section>
  <?php endforeach; ?>
  <p class="no-match" id="noMatch">Nenhum participante encontrado.</p>

  <?php else: ?>
  <div class="empty">
    <strong>Ainda n&atilde;o h&aacute; check-ins registrados.</strong>
    Assim que a primeira Net acontecer, os participantes aparecer&atilde;o aqui.
  </div>
  <?php endif; ?>

  <footer>73 de <span class="b">PP5PK</span> &middot; pp5pk.net</footer>
</div>

<script>
  // Theme: shares the 'theme' preference with the main site (same origin).
  var html = document.documentElement;
  var saved = localStorage.getItem('theme') ||
    (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  applyTheme(saved);
  var tbtn = document.getElementById('theme-toggle');
  tbtn.addEventListener('click', function () {
    var next = html.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next); localStorage.setItem('theme', next);
  });
  function applyTheme(t) {
    html.dataset.theme = t;
    document.getElementById('theme-icon').textContent = t === 'dark' ? '\u2600\uFE0F' : '\uD83C\uDF19';
    document.getElementById('theme-label').textContent = t === 'dark' ? 'Claro' : 'Escuro';
  }

  // Search filter (callsign or name).
  var q = document.getElementById('q');
  if (q) {
    q.addEventListener('input', function () {
      var term = this.value.trim().toUpperCase();
      var anyVisible = false;
      document.querySelectorAll('.net').forEach(function (net) {
        var shown = 0;
        net.querySelectorAll('.op').forEach(function (o) {
          var match = o.dataset.s.indexOf(term) !== -1;
          o.style.display = match ? '' : 'none';
          if (match) shown++;
        });
        net.style.display = shown ? '' : 'none';
        if (shown) anyVisible = true;
      });
      document.getElementById('noMatch').style.display = (term && !anyVisible) ? 'block' : 'none';
    });
  }
</script>
</body>
</html>
