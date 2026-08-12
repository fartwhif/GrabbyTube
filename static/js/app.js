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

function fmtUploadDate(d) {
  if (!d || d.length < 8) return '';
  const y = d.slice(0, 4), mo = d.slice(4, 6), dy = d.slice(6, 8);
  return `${y}-${mo}-${dy}`;
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
  searchInput.value = '';
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

    searchResults.innerHTML = `<div class="search-query">Results for: <strong>${esc(q)}</strong></div>` + results.map(r => `
      <div class="result-card">
        <img class="thumb" src="${esc(r.thumbnail)}" alt="" loading="lazy">
        <div class="info">
          <a class="title yt-link" href="${escAttr(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>
          <div class="meta">${esc(r.uploader)} &middot; ${fmtDuration(r.duration)}</div>
        </div>
        <div class="actions">
          <button class="btn btn-primary btn-queue"
            data-id="${escAttr(r.id)}"
            data-title="${escAttr(r.title)}"
            data-url="${escAttr(r.url)}">
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

function escAttr(s) {
  if (!s) return '';
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// --- Queue ---

async function queueTrack(id, title, url) {
  try {
    const res = await api('/queue', {
      method: 'POST',
      body: JSON.stringify({ video_id: id, title, url }),
    });
    if (res.status === 'skipped') {
      searchResults.innerHTML = '<div class="empty-msg" style="color:#fa3">Skipped — already queued or downloaded!</div>';
      return;
    }
    searchResults.innerHTML = '<div class="empty-msg">Queued! Refreshing...</div>';
    refreshJobs();
    refreshStats();
  } catch (err) {
    alert('Queue failed: ' + err.message);
  }
}

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
          ${j.output_file ? `<div class="job-error" style="color:#44d46b">Done: ${esc(j.video_id)}</div>` : ''}
        </div>
        ${j.status === 'completed' && j.video_id ? `
          <button class="btn btn-play btn-play-track"
            data-video-id="${escAttr(j.video_id)}"
            data-title="${escAttr(j.title)}">Play</button>
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
      <div class="track-card" id="track-${esc(t.videoId)}">
        <div class="track-info">
          <div class="track-title">${esc(t.title)}</div>
          <div class="track-meta">
            ${esc(t.author)} &middot; ${fmtDuration(t.duration)}
          </div>
        </div>
        <div class="actions">
          <button class="btn btn-play btn-play-track"
            data-video-id="${escAttr(t.videoId)}"
            data-title="${escAttr(t.title)}">Play</button>
          <a class="btn btn-secondary" href="${API}/audio/${encodeURIComponent(t.videoId)}" download>DL</a>
          <button class="btn btn-danger btn-delete-track"
            data-video-id="${escAttr(t.videoId)}"
            data-title="${escAttr(t.title)}">Del</button>
        </div>
      </div>
    `).join('');
  } catch (err) {
    console.error('Refresh tracks failed:', err);
  }
}

sortSelect.addEventListener('change', refreshTracks);

// --- Event delegation ---

document.addEventListener('click', async (e) => {
  // Clear jobs button
  const clearBtn = e.target.closest('#clear-jobs-btn');
  if (clearBtn) {
    e.preventDefault();
    try {
      const res = await api('/jobs', { method: 'DELETE' });
      refreshJobs();
      refreshStats();
    } catch (err) {
      alert('Clear failed: ' + err.message);
    }
    return;
  }

  // Queue button (search results)
  const queueBtn = e.target.closest('.btn-queue');
  if (queueBtn) {
    e.preventDefault();
    queueTrack(queueBtn.dataset.id, queueBtn.dataset.title, queueBtn.dataset.url);
    return;
  }

  // Play button (jobs and tracks)
  const playBtn = e.target.closest('.btn-play-track');
  if (playBtn) {
    e.preventDefault();
    playTrack(playBtn.dataset.videoId, playBtn.dataset.title);
    return;
  }

  // Delete button (tracks)
  const delBtn = e.target.closest('.btn-delete-track');
  if (delBtn) {
    e.preventDefault();
    await deleteTrack(delBtn.dataset.videoId, delBtn.dataset.title);
    return;
  }
});

// --- Batch Queue ---

const batchInput = document.getElementById('batch-input');
const batchBtn = document.getElementById('batch-btn');
const batchStatus = document.getElementById('batch-status');

batchBtn.addEventListener('click', async () => {
  const text = batchInput.value.trim();
  if (!text) return;

  batchBtn.disabled = true;
  batchBtn.textContent = 'Enqueuing...';
  batchStatus.textContent = '';

  try {
    const result = await api('/queue/batch', {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
    batchStatus.textContent = `${result.queued} queued, ${result.skipped} skipped (already exists).`;
    if (result.queued > 0) {
      batchInput.value = '';
    }
    refreshJobs();
    refreshStats();
  } catch (err) {
    batchStatus.textContent = 'Batch failed: ' + err.message;
  } finally {
    batchBtn.disabled = false;
    batchBtn.textContent = 'Enqueue All';
  }
});

// --- Delete ---

async function deleteTrack(video_id, title) {
  if (!confirm('Delete \'' + title + '\'?')) return;
  try {
    await api('/tracks/' + encodeURIComponent(video_id), { method: 'DELETE' });
    const card = document.getElementById('track-' + video_id);
    if (card) card.remove();
    refreshStats();
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
}

// --- Player ---

const player = document.getElementById('player');
const playerTitle = document.getElementById('player-title');

function playTrack(videoId, title) {
  player.src = `${API}/audio/${encodeURIComponent(videoId)}`;
  playerTitle.textContent = title || videoId;
  player.play();
}

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
