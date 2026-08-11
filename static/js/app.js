const API = '';

// --- Helpers ---

function fmtDuration(s) {
  if (!s || s === 0) return '--:--';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

function fmtSize(b) {
  if (!b || b === 0) return '--';
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(0)} KB`;
  return `${(b / (1024 * 1024)).toFixed(1)} MB`;
}

function api(path, opts = {}) {
  return fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  }).then(r => {
    if (!r.ok) return r.text().then(t => { throw new Error(t); });
    return r.json();
  });
}

// --- Search ---

const searchForm = document.getElementById('search-form');
const searchInput = document.getElementById('search-input');
const searchBtn = document.getElementById('search-btn');
const searchResults = document.getElementById('search-results');

searchForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = searchInput.value.trim();
  if (!q) return;

  searchBtn.disabled = true;
  searchBtn.textContent = 'Searching...';
  searchResults.innerHTML = '<div class="empty-msg"><span class="spinner"></span> Searching...</div>';

  try {
    const results = await api('/search', {
      method: 'POST',
      body: JSON.stringify({ query: q, limit: 15 }),
    });

    if (!results.length) {
      searchResults.innerHTML = '<div class="empty-msg">No results found</div>';
      return;
    }

    searchResults.innerHTML = results.map(r => `
      <div class="result-card">
        <img class="thumb" src="${esc(r.thumbnail)}" alt="" loading="lazy">
        <div class="info">
          <div class="title">${esc(r.title)}</div>
          <div class="meta">${esc(r.uploader)} &middot; ${fmtDuration(r.duration)}</div>
        </div>
        <div class="actions">
          <button class="btn btn-primary" onclick="queueTrack('${esc(r.id)}', '${esc(r.title.replace(/'/g, "\\'"))}', '${esc(r.url)}')">
            Queue
          </button>
        </div>
      </div>
    `).join('');
  } catch (err) {
    searchResults.innerHTML = `<div class="empty-msg" style="color:#d44">${esc(err.message)}</div>`;
  } finally {
    searchBtn.disabled = false;
    searchBtn.textContent = 'Search';
  }
});

function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// --- Queue ---

window.queueTrack = async function(id, title, url) {
  try {
    const job = await api('/queue', {
      method: 'POST',
      body: JSON.stringify({ video_id: id, title, url }),
    });
    refreshJobs();
    refreshStats();
  } catch (err) {
    alert('Queue failed: ' + err.message);
  }
};

async function refreshJobs() {
  try {
    const jobs = await api('/jobs?limit=20');
    const jobList = document.getElementById('job-list');

    if (!jobs.length) {
      jobList.innerHTML = '<div class="empty-msg">Queue is empty</div>';
      return;
    }

    jobList.innerHTML = jobs.map(j => `
      <div class="job-card">
        <span class="status status-${j.status}">${j.status}</span>
        <div class="job-info">
          <div class="job-title">${esc(j.title)}</div>
          ${j.error ? `<div class="job-error">${esc(j.error.substring(0, 120))}</div>` : ''}
          ${j.output_file ? `<div class="job-error" style="color:#44d46b">Done: ${esc(j.output_file.split('/').pop())}</div>` : ''}
        </div>
        ${j.status === 'completed' && j.output_file ? `
          <button class="btn btn-play" onclick="playTrack('${esc(j.output_file.split('/').pop())}', '${esc(j.title.replace(/'/g, "\\'"))}')">Play</button>
        ` : ''}
      </div>
    `).join('');
  } catch (err) {
    console.error('Refresh jobs failed:', err);
  }
}

// --- Tracks ---

const sortSelect = document.getElementById('sort-select');

async function refreshTracks() {
  try {
    const sort = sortSelect.value;
    const tracks = await api(`/tracks?sort_by=${sort}&reverse=true`);
    const trackList = document.getElementById('track-list');

    if (!tracks.length) {
      trackList.innerHTML = '<div class="empty-msg">No tracks yet. Search and queue something!</div>';
      return;
    }

    trackList.innerHTML = tracks.map(t => `
      <div class="track-card">
        <div class="track-info">
          <div class="track-title">${esc(t.title)}</div>
          <div class="track-meta">
            ${esc(t.artist)} &middot; ${fmtDuration(t.duration)} &middot; ${fmtSize(t.filesize)}
            &middot; ${t.bitrate ? t.bitrate + 'bps' : ''}
            &middot; ${t.channels || '?'}ch
            &middot; ${t.codec || 'mp3'}
            ${t.genre ? `&middot; ${esc(t.genre)}` : ''}
          </div>
        </div>
        <div class="actions">
          <button class="btn btn-play" onclick="playTrack('${esc(t.filename)}', '${esc(t.title.replace(/'/g, "\\'"))}')">Play</button>
          <a class="btn btn-secondary" href="${API}/audio/${encodeURIComponent(t.filename)}" download>DL</a>
        </div>
      </div>
    `).join('');
  } catch (err) {
    console.error('Refresh tracks failed:', err);
  }
}

sortSelect.addEventListener('change', refreshTracks);

// --- Player ---

const player = document.getElementById('player');
const playerTitle = document.getElementById('player-title');

window.playTrack = function(filename, title) {
  player.src = `${API}/audio/${encodeURIComponent(filename)}`;
  playerTitle.textContent = title || filename;
  player.play();
};

// --- Stats ---

async function refreshStats() {
  try {
    const [count, jobs] = await Promise.all([
      api('/tracks/count'),
      api('/jobs?limit=1'),
    ]);
    document.getElementById('track-count').textContent = `${count.count} tracks`;
    const queued = jobs.filter(j => j.status === 'queued' || j.status === 'running');
    document.getElementById('queue-count').textContent = `${queued.length} active`;
  } catch (err) {
    // silent
  }
}

// --- Poll ---

function poll() {
  refreshJobs();
  refreshTracks();
  refreshStats();
}

// Initial load
poll();
// Refresh every 5 seconds
setInterval(poll, 5000);
