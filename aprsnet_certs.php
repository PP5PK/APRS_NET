<?php
/**
 * APRS PKTNET - certificate download page for pp5pk.net
 *
 * Lists the participation certificates grouped by net and lets operators
 * download their own. Certificate files are produced by the PKTNET bot and
 * named  PKTNET_ev<N>_<CALLSIGN>.pdf  (ev1 = Net #01, ev2 = Net #02, ...).
 *
 * Deploy this file in the site root (e.g. /var/www/html/pp5pk/) and point
 * $CERT_DIR at the folder where the PDFs are uploaded. Downloads are streamed
 * by this script, so the certificate folder does NOT need its own public URL.
 */

// ---- configuration -------------------------------------------------------
$CERT_DIR = '/var/www/html/cloud/aprsnet_certs';   // where the PDFs live
$SITE_URL = 'https://pp5pk.net';                   // link back to the site
$NET_PREFIX = 'PKTNET';                             // filename prefix (net call)
// --------------------------------------------------------------------------

$FILE_RE = '/^' . preg_quote($NET_PREFIX, '/')
         . '_ev(\d+)_([A-Za-z0-9._-]+)\.pdf$/i';

/* ---- download handler: stream a single PDF safely ---- */
if (isset($_GET['dl'])) {
    $name = basename($_GET['dl']);                 // strip any path component
    if (!preg_match($FILE_RE, $name)) {            // strict name = no traversal
        http_response_code(400); exit('Invalid file.');
    }
    $path = $CERT_DIR . '/' . $name;               // may be a symlink into the bot dir
    if (!is_file($path)) {                          // is_file() follows symlinks
        http_response_code(404); exit('Not found.');
    }
    header('Content-Type: application/pdf');
    header('Content-Disposition: attachment; filename="' . $name . '"');
    header('Content-Length: ' . filesize($path));
    header('X-Content-Type-Options: nosniff');
    readfile($path);
    exit;
}

/* ---- build the grouped list ---- */
$nets = [];      // net number => [ ['call'=>, 'file'=>, 'mtime'=>], ... ]
$total = 0;
if (is_dir($CERT_DIR) && ($dh = @opendir($CERT_DIR))) {
    while (($f = readdir($dh)) !== false) {
        if (preg_match($FILE_RE, $f, $m)) {
            $n = (int)$m[1];
            $nets[$n][] = [
                'call'  => strtoupper($m[2]),
                'file'  => $f,
                'mtime' => @filemtime($CERT_DIR . '/' . $f) ?: 0,
            ];
            $total++;
        }
    }
    closedir($dh);
}
krsort($nets);                                     // newest net first
foreach ($nets as &$list) {
    usort($list, fn($a, $b) => strcmp($a['call'], $b['call']));
}
unset($list);

function net_label($n) { return sprintf('Net #%02d', $n); }
?>
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Certificados &middot; APRS PKTNET &middot; PP5PK</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet" />
<style>
  :root{
    --bg:#0D1117; --surface:#161B22; --surface2:#1E2530; --border:#2A3240;
    --primary:#4D9EFF; --accent:#FFB347; --text:#E6EDF3; --text-muted:#8B949E;
    --grid-color:rgba(77,158,255,0.06); --glow:rgba(77,158,255,0.15);
    --radius:8px; --font-mono:'Space Mono',monospace; --font-sans:'IBM Plex Sans',sans-serif;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:var(--font-sans);background:var(--bg);color:var(--text);
    line-height:1.6;min-height:100vh;overflow-x:hidden;}
  body::before{content:'';position:fixed;inset:0;background-image:
    linear-gradient(var(--grid-color) 1px,transparent 1px),
    linear-gradient(90deg,var(--grid-color) 1px,transparent 1px);
    background-size:40px 40px;pointer-events:none;z-index:0;}
  .wrap{position:relative;z-index:1;max-width:1000px;margin:0 auto;padding:2.5rem 1.25rem 4rem;}
  a{color:inherit;text-decoration:none;}
  .back{font-family:var(--font-mono);font-size:.8rem;color:var(--text-muted);
    display:inline-flex;align-items:center;gap:.4rem;margin-bottom:2rem;transition:.2s;}
  .back:hover{color:var(--primary);}
  .tag{font-family:var(--font-mono);font-size:.8rem;color:var(--accent);letter-spacing:.05em;}
  h1{font-size:clamp(1.8rem,5vw,2.8rem);line-height:1.1;margin:.3rem 0 .6rem;
    font-weight:600;letter-spacing:-.02em;}
  h1 .g{color:var(--accent);} h1 .b{color:var(--primary);}
  .lede{color:var(--text-muted);max-width:640px;font-size:.95rem;}
  .lede code{font-family:var(--font-mono);color:var(--primary);
    background:var(--surface2);padding:.05em .4em;border-radius:4px;font-size:.85em;}
  .search{margin:2rem 0 .5rem;position:relative;max-width:420px;}
  .search input{width:100%;padding:.75rem 1rem .75rem 2.5rem;background:var(--surface);
    border:1px solid var(--border);border-radius:var(--radius);color:var(--text);
    font-family:var(--font-mono);font-size:.9rem;transition:.2s;}
  .search input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--glow);}
  .search svg{position:absolute;left:.85rem;top:50%;transform:translateY(-50%);
    color:var(--text-muted);pointer-events:none;}
  .count{font-family:var(--font-mono);font-size:.8rem;color:var(--text-muted);margin-bottom:2rem;}
  .net{margin-bottom:2.25rem;}
  .net-head{display:flex;align-items:baseline;gap:.75rem;margin-bottom:1rem;
    padding-bottom:.5rem;border-bottom:1px solid var(--border);}
  .net-head h2{font-size:1.25rem;font-weight:600;}
  .net-head .n{font-family:var(--font-mono);font-size:.8rem;color:var(--text-muted);}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:.75rem;}
  .cert{display:flex;align-items:center;gap:.7rem;padding:.7rem .9rem;background:var(--surface);
    border:1px solid var(--border);border-radius:var(--radius);transition:.18s;}
  .cert:hover{border-color:var(--primary);transform:translateY(-2px);
    box-shadow:0 8px 20px rgba(0,0,0,.25);}
  .cert .ic{color:var(--accent);flex-shrink:0;}
  .cert .call{font-family:var(--font-mono);font-weight:700;font-size:.95rem;letter-spacing:.02em;}
  .cert .dl{margin-left:auto;color:var(--text-muted);transition:.18s;}
  .cert:hover .dl{color:var(--primary);}
  .empty{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
    padding:2.5rem 1.5rem;text-align:center;color:var(--text-muted);}
  .empty strong{color:var(--text);display:block;margin-bottom:.4rem;font-size:1.05rem;}
  .no-match{display:none;color:var(--text-muted);font-family:var(--font-mono);
    font-size:.85rem;padding:1rem 0;}
  footer{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--border);
    font-family:var(--font-mono);font-size:.8rem;color:var(--text-muted);text-align:center;}
  footer .b{color:var(--primary);}
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="<?= htmlspecialchars($SITE_URL) ?>">&larr; pp5pk.net</a>

  <p class="tag">// APRS PKTNET</p>
  <h1>Certificados de <span class="b">Participa&ccedil;&atilde;o</span></h1>
  <p class="lede">Participou da nossa Net? Baixe aqui seu certificado. Encontre
    seu indicativo pela busca ou navegue pela Net. Ainda n&atilde;o tem o seu?
    Envie <code>CHECK</code> para <code>PKTNET</code> via APRS.</p>

  <?php if ($total > 0): ?>
  <div class="search">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
    <input id="q" type="text" placeholder="Buscar indicativo..." autocomplete="off" spellcheck="false" />
  </div>
  <p class="count"><?= $total ?> certificado<?= $total == 1 ? '' : 's' ?>
     em <?= count($nets) ?> Net<?= count($nets) == 1 ? '' : 's' ?></p>

  <?php foreach ($nets as $n => $list): ?>
  <section class="net" data-net="<?= $n ?>">
    <div class="net-head">
      <h2><?= net_label($n) ?></h2>
      <span class="n"><?= count($list) ?> participante<?= count($list) == 1 ? '' : 's' ?></span>
    </div>
    <div class="grid">
      <?php foreach ($list as $c): ?>
      <a class="cert" data-call="<?= htmlspecialchars($c['call']) ?>"
         href="?dl=<?= rawurlencode($c['file']) ?>">
        <span class="ic"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><circle cx="10" cy="13" r="2"/><path d="m8 21 2-2 2 2 2-2"/></svg></span>
        <span class="call"><?= htmlspecialchars($c['call']) ?></span>
        <span class="dl"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg></span>
      </a>
      <?php endforeach; ?>
    </div>
  </section>
  <?php endforeach; ?>
  <p class="no-match" id="noMatch">Nenhum indicativo encontrado.</p>

  <?php else: ?>
  <div class="empty">
    <strong>Ainda n&atilde;o h&aacute; certificados dispon&iacute;veis.</strong>
    Assim que a primeira Net acontecer, os certificados aparecer&atilde;o aqui.
  </div>
  <?php endif; ?>

  <footer>73 de <span class="b">PP5PK</span> &middot; pp5pk.net</footer>
</div>

<script>
  var q = document.getElementById('q');
  if (q) {
    q.addEventListener('input', function () {
      var term = this.value.trim().toUpperCase();
      var anyVisible = false;
      document.querySelectorAll('.net').forEach(function (net) {
        var shown = 0;
        net.querySelectorAll('.cert').forEach(function (c) {
          var match = c.dataset.call.indexOf(term) !== -1;
          c.style.display = match ? '' : 'none';
          if (match) shown++;
        });
        net.style.display = shown ? '' : 'none';
        if (shown) anyVisible = true;
      });
      document.getElementById('noMatch').style.display =
        (term && !anyVisible) ? 'block' : 'none';
    });
  }
</script>
</body>
</html>
