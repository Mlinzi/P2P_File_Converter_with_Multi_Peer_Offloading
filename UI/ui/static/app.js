// app.js — vanilla JS, no frameworks

let selectedFiles = [];
let currentExt    = '';

// ---------------------------------------------------------------------------
// File selection
// ---------------------------------------------------------------------------

function onDrop(e) {
  e.preventDefault();
  document.getElementById('dropzone').classList.remove('drag-over');
  handleFiles(Array.from(e.dataTransfer.files));
}

function onFileSelect(fileList) {
  handleFiles(Array.from(fileList));
}

function handleFiles(files) {
  if (!files.length) return;
  selectedFiles = files;

  const allPdfs = files.every(f => f.name.toLowerCase().endsWith('.pdf'));

  if (files.length > 1 && allPdfs) {
    // Multi-PDF combine mode
    currentExt = 'pdf';
    document.getElementById('fmt-from-label').textContent = `${files.length} PDFs`;
    document.getElementById('selected-file').textContent  = files.map(f => f.name).join(', ');
    setOutputFormats(['combine_pdf']);
    document.getElementById('fmt-to').value = 'combine_pdf';
    document.getElementById('drop-hint').textContent = `${files.length} PDFs selected — will be combined`;
  } else {
    // Single file
    const file = files[0];
    const ext  = file.name.split('.').pop().toLowerCase();
    currentExt = ext === 'jpg' ? 'jpeg' : ext;
    document.getElementById('fmt-from-label').textContent = currentExt.toUpperCase();
    document.getElementById('selected-file').textContent  = file.name;
    document.getElementById('drop-hint').textContent      = '';
    fetchFormats(currentExt);
  }

  updateConvertBtn();
}

function fetchFormats(ext) {
  fetch(`/api/formats?ext=${ext}`)
    .then(r => r.json())
    .then(data => {
      setOutputFormats(data.outputs || []);
      updateConvertBtn();
    });
}

function setOutputFormats(formats) {
  const sel = document.getElementById('fmt-to');
  sel.innerHTML = '<option value="">Select format...</option>';
  formats.forEach(f => {
    const opt   = document.createElement('option');
    opt.value   = f;
    opt.textContent = f === 'combine_pdf' ? 'Combine PDFs' : f.toUpperCase();
    sel.appendChild(opt);
  });
  // Auto-select if only one option
  if (formats.length === 1) sel.value = formats[0];
  updateConvertBtn();
}

function onFormatChange() { updateConvertBtn(); }

function updateConvertBtn() {
  const ready = selectedFiles.length > 0 && document.getElementById('fmt-to').value !== '';
  document.getElementById('convert-btn').disabled = !ready;
}

// ---------------------------------------------------------------------------
// Convert
// ---------------------------------------------------------------------------

function doConvert() {
  const outputFmt = document.getElementById('fmt-to').value;
  if (!selectedFiles.length || !outputFmt) return;

  const fd = new FormData();
  selectedFiles.forEach(f => fd.append('file', f));
  fd.append('output_format', outputFmt);

  document.getElementById('convert-btn').disabled = true;
  document.getElementById('convert-btn').textContent = 'Uploading...';

  fetch('/api/convert', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(data => {
      if (data.error) { alert(data.error); return; }
      // Reset UI
      selectedFiles = [];
      document.getElementById('fmt-from-label').textContent = 'Auto Detect';
      document.getElementById('selected-file').textContent  = '';
      document.getElementById('fmt-to').innerHTML = '<option value="">Select format...</option>';
      document.getElementById('drop-hint').textContent = 'Supports documents, images, audio, video';
      document.getElementById('file-input').value = '';
    })
    .catch(err => alert('Upload failed: ' + err))
    .finally(() => {
      document.getElementById('convert-btn').textContent = 'Convert';
      updateConvertBtn();
    });
}

// ---------------------------------------------------------------------------
// Poll status + jobs
// ---------------------------------------------------------------------------

function pollLogs() {
  if (document.getElementById('tab-stats').style.display === 'none') return;
  fetch('/api/logs')
    .then(r => r.json())
    .then(data => {
      const box = document.getElementById('log-box');
      const logs = data.logs || [];
      if (!logs.length) return;
      box.innerHTML = logs.map(e =>
        `<div class="log-entry"><span class="log-time">${e.time}</span><span class="log-msg">${e.msg}</span></div>`
      ).join('');
    })
    .catch(() => {});
}

function pollStatus() {
  fetch('/api/status')
    .then(r => r.json())
    .then(data => {
      updatePeers(data.peers || []);
      syncPreferPeers(data.prefer_peers || false);
      syncTLS(data.tls || false);
      syncGPU(data.gpu || false, data.gpu_encoder || null);
      if (document.getElementById('tab-stats').style.display !== 'none') {
        updateStats(data.metrics || {});
      }
    })
    .catch(() => {});
}

function toggleTLS(checkbox) {
  fetch('/api/toggle-tls', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled: checkbox.checked})
  }).then(r => r.json()).then(data => {
    if (data.error) {
      alert('TLS Error: ' + data.error);
      checkbox.checked = !checkbox.checked;   // revert
      return;
    }
    syncTLS(data.tls);
  }).catch(() => { checkbox.checked = !checkbox.checked; });
}

function syncTLS(enabled) {
  const cb = document.getElementById('tls-toggle');
  if (cb) cb.checked = enabled;
  const hint = document.getElementById('tls-hint');
  if (hint) hint.textContent = enabled ? '(on — AES-256 encrypted)' : '(off — plain TCP)';
  const badge = document.getElementById('tls-badge');
  if (badge) badge.style.display = enabled ? 'inline' : 'none';
}

function toggleGPU(checkbox) {
  fetch('/api/toggle-gpu', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled: checkbox.checked})
  }).then(r => r.json()).then(data => {
    if (data.error) {
      alert('GPU Error: ' + data.error);
      checkbox.checked = !checkbox.checked;
      return;
    }
    syncGPU(data.gpu, data.encoder);
  }).catch(() => { checkbox.checked = !checkbox.checked; });
}

function syncGPU(enabled, encoder) {
  const row = document.getElementById('gpu-row');
  const cb  = document.getElementById('gpu-toggle');
  const hint = document.getElementById('gpu-hint');
  if (!encoder) {
    if (row) row.style.display = 'none';
    return;
  }
  if (row) row.style.display = '';
  if (cb)  { cb.checked = enabled; cb.disabled = false; }
  const encLabel = encoder.replace('h264_', '').toUpperCase();   // NVENC / AMF / QSV
  if (hint) hint.textContent = enabled
    ? `(on — using ${encLabel})`
    : `(off — CPU encoding)`;
}

function pollJobs() {
  fetch('/api/jobs')
    .then(r => r.json())
    .then(data => renderJobs(data.jobs || []))
    .catch(() => {});
}

function togglePreferPeers(checkbox) {
  fetch('/api/prefer-peers', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled: checkbox.checked})
  }).then(r => r.json()).then(data => {
    const hint = document.getElementById('offload-hint');
    hint.textContent = data.prefer_peers
      ? '(on — always offloads to peers when available)'
      : '(off — converts locally when idle)';
  });
}

function syncPreferPeers(enabled) {
  const cb = document.getElementById('prefer-peers-toggle');
  if (cb) cb.checked = enabled;
  const hint = document.getElementById('offload-hint');
  if (hint) hint.textContent = enabled
    ? '(on — always offloads to peers when available)'
    : '(off — converts locally when idle)';
}

function updatePeers(peers) {
  const dots  = document.getElementById('peer-dots');
  const list  = document.getElementById('peers-list');
  const label = document.getElementById('peer-count-label');

  // Dots in header
  dots.innerHTML = '<span class="dot self" title="This PC"></span>';
  peers.forEach(p => {
    const d = document.createElement('span');
    d.className = 'dot other';
    d.title = p.peer_name;
    dots.appendChild(d);
  });

  label.textContent = `${peers.length + 1} peer${peers.length !== 0 ? 's' : ''}`;

  // Peer chips
  list.innerHTML = '<div class="peer-chip self-chip">● This PC (you)</div>';
  peers.forEach(p => {
    const chip = document.createElement('div');
    chip.className = 'peer-chip';
    chip.textContent = `● ${p.peer_name}`;
    list.appendChild(chip);
  });
}

function renderJobs(jobs) {
  const el = document.getElementById('jobs-list');
  if (!jobs.length) {
    el.innerHTML = '<div class="empty-msg">No jobs yet. Drop a file above to get started.</div>';
    return;
  }

  el.innerHTML = '';
  jobs.slice(0, 20).forEach(job => {
    const row = document.createElement('div');
    row.className = 'job-row';

    const statusIcon = { done: '✓', error: '✗', converting: '⟳', queued: '…' }[job.status] || '?';
    const statusColor = { done: '#22c55e', error: '#f87171', converting: '#818cf8', queued: '#475569' }[job.status] || '#475569';
    const peerLabel = job.peer && job.peer !== 'local' ? `(via ${job.peer})` : '(local)';
    const latLabel  = job.latency_ms ? `${(job.latency_ms / 1000).toFixed(1)}s` : '';

    row.innerHTML = `
      <span class="job-name" title="${job.filename}">${job.filename}</span>
      <span class="job-arrow">→</span>
      <span class="job-fmt">${job.output_format}</span>
      <span class="job-peer">${peerLabel}</span>
      <span class="job-status" style="color:${statusColor}">${statusIcon}</span>
      <span class="job-lat">${latLabel}</span>
      ${job.status === 'done'
        ? `<a class="download-btn" href="/api/download/${job.job_id}" download>↓ Download</a>`
        : job.status === 'error'
        ? `<span style="color:#f87171;font-size:.8rem" title="${job.error}">Failed</span>`
        : ''}
    `;
    el.appendChild(row);
  });
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------

function updateStats(m) {
  document.getElementById('s-jobs-done').textContent    = m.jobs_done    ?? '—';
  document.getElementById('s-jobs-failed').textContent  = m.jobs_failed  ?? '—';
  document.getElementById('s-latency').textContent      = m.avg_latency_ms ? `${m.avg_latency_ms} ms` : '—';
  document.getElementById('s-throughput').textContent   = m.throughput_per_min ? `${m.throughput_per_min}/min` : '—';
  document.getElementById('s-cpu').textContent          = m.cpu_percent != null ? `${m.cpu_percent}%` : '—';
  document.getElementById('s-sent').textContent         = m.bytes_sent_mb ? `${m.bytes_sent_mb} MB` : '—';
  document.getElementById('s-recv').textContent         = m.bytes_recv_mb ? `${m.bytes_recv_mb} MB` : '—';
  document.getElementById('s-uptime').textContent       = m.uptime_seconds ? fmtUptime(m.uptime_seconds) : '—';
  drawSparkline(m.latency_history || []);
}

function fmtUptime(s) {
  if (s < 60)   return `${s}s`;
  if (s < 3600) return `${Math.floor(s/60)}m ${s%60}s`;
  return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
}

function drawSparkline(data) {
  const canvas = document.getElementById('sparkline');
  canvas.width = canvas.parentElement.clientWidth - 32;
  const ctx    = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  if (!data.length) return;

  const max = Math.max(...data, 1);
  const w   = canvas.width;
  const h   = canvas.height;
  const step = w / (data.length - 1 || 1);

  ctx.beginPath();
  ctx.strokeStyle = '#818cf8';
  ctx.lineWidth   = 2;
  data.forEach((v, i) => {
    const x = i * step;
    const y = h - (v / max) * (h - 8) - 4;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Fill under line
  ctx.lineTo((data.length - 1) * step, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = 'rgba(129,140,248,0.1)';
  ctx.fill();
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------

function switchTab(name, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-convert').style.display = name === 'convert' ? '' : 'none';
  document.getElementById('tab-stats').style.display   = name === 'stats'   ? '' : 'none';
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

pollStatus();
pollJobs();
pollLogs();
setInterval(pollStatus, 2000);
setInterval(pollJobs,   2000);
setInterval(pollLogs,   2000);
