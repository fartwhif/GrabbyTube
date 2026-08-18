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

    searchResults.innerHTML = `<div class="search-query">Results for: <strong>${esc(q)}</strong></div>` + results.map(r => {
      r._query = q;
      return `
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
            data-url="${escAttr(r.url)}"
            data-search-query="${escAttr(r._query)}"
            data-artist="${escAttr(r.uploader)}">
            Queue
          </button>
        </div>
      </div>`;
    }).join('');
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

async function queueTrack(id, title, url, searchQuery, artist) {
  try {
    const res = await api('/queue', {
      method: 'POST',
      body: JSON.stringify({ video_id: id, title, url, search_query: searchQuery, artist }),
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

async function retrySearch(jid, query) {
  searchResults.innerHTML = `<div class="empty-msg"><span class="spinner"></span> Re-searching: "${esc(query)}"...</div>`;
  searchInput.value = query;
  try {
    const results = await api(`/jobs/${jid}/retry`, { method: 'POST' });
    if (!results.length) {
      searchResults.innerHTML = `<div class="empty-msg" style="color:#fa3">No results found for: ${esc(query)}</div>`;
      return;
    }
    searchResults.innerHTML = `<div class="search-query">Results for: <strong>${esc(query)}</strong></div>` + results.map(r => {
      r._query = query;
      return `
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
            data-url="${escAttr(r.url)}"
            data-search-query="${escAttr(r._query)}"
            data-artist="${escAttr(r.uploader)}">
            Queue
          </button>
        </div>
      </div>`;
    }).join('');
  } catch (err) {
    searchResults.innerHTML = `<div class="empty-msg" style="color:#d44">${esc(err.message)}</div>`;
  }
}

async function refreshJobs() {
  try {
    const jobs = await api('/jobs');
    const jobList = document.getElementById('job-list');

    if (!jobs.length) {
      jobList.innerHTML = '<div class="empty-msg">Queue is empty</div>';
      return;
    }

    jobList.innerHTML = jobs.map(j => {
      const displayTitle = j.title || j.video_id || '(no title)';
      const artistLine = j.artist ? `<div class="job-artist">${esc(j.artist)}</div>` : '';
      return `
      <div class="job-card">
        <span class="status status-${j.status}">${j.status}</span>
        <div class="job-info">
          <div class="job-title">${esc(displayTitle)}</div>
          ${artistLine}
          ${j.error ? `<div class="job-error">${esc(j.error.substring(0, 120))}</div>` : ''}
          ${j.output_file ? `<div class="job-error" style="color:#44d46b">Done: ${esc(j.video_id)}</div>` : ''}
        </div>
        ${j.status === 'completed' && j.video_id ? `
          <button class="btn btn-play btn-play-track"
            data-video-id="${escAttr(j.video_id)}"
            data-title="${escAttr(j.title)}">Play</button>
        ` : ''}
        ${j.status === 'failed' && j.query ? `
          <button class="btn btn-secondary btn-retry-search"
            data-jid="${j.id}"
            data-query="${escAttr(j.query)}">Search Again</button>
        ` : ''}
        ${j.status === 'failed' && j.title ? `
          <button class="btn btn-secondary btn-retry-title"
            data-jid="${j.id}"
            data-query="${escAttr(j.artist ? j.artist + ' ' + j.title : j.title)}">Search by Title</button>
        ` : ''}
      </div>`;
    }).join('');
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

  // Clear completed button
  const clearCompletedBtn = e.target.closest('#clear-completed-btn');
  if (clearCompletedBtn) {
    e.preventDefault();
    try {
      const res = await api('/jobs?status=completed', { method: 'DELETE' });
      refreshJobs();
      refreshStats();
    } catch (err) {
      alert('Clear failed: ' + err.message);
    }
    return;
  }

  // Clear all button
  const clearAllBtn = e.target.closest('#clear-all-btn');
  if (clearAllBtn) {
    e.preventDefault();
    if (!confirm('Clear ALL jobs (including queued/running)?')) return;
    try {
      const res = await api('/jobs/all', { method: 'DELETE' });
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
    queueTrack(queueBtn.dataset.id, queueBtn.dataset.title, queueBtn.dataset.url, queueBtn.dataset.searchQuery, queueBtn.dataset.artist);
    return;
  }

  // Search Again button (failed jobs - uses original query)
  const retryBtn = e.target.closest('.btn-retry-search');
  if (retryBtn) {
    e.preventDefault();
    retrySearch(retryBtn.dataset.jid, retryBtn.dataset.query);
    return;
  }

  // Search by Title button (failed jobs - uses artist + title)
  const retryTitleBtn = e.target.closest('.btn-retry-title');
  if (retryTitleBtn) {
    e.preventDefault();
    retrySearch(retryTitleBtn.dataset.jid, retryTitleBtn.dataset.query);
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

// --- Playlist Queue ---

const playlistForm = document.getElementById('playlist-form');
const playlistInput = document.getElementById('playlist-input');
const playlistBtn = document.getElementById('playlist-btn');
const playlistStatus = document.getElementById('playlist-status');

playlistForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = playlistInput.value.trim();
  if (!url) return;

  playlistBtn.disabled = true;
  playlistBtn.textContent = 'Fetching...';
  playlistStatus.textContent = '';

  try {
    const result = await api('/queue/playlist', {
      method: 'POST',
      body: JSON.stringify({ url }),
    });
    playlistStatus.textContent = `${result.queued} queued, ${result.skipped} skipped (already exists) out of ${result.total} videos.`;
    if (result.queued > 0) {
      playlistInput.value = '';
    }
    refreshJobs();
    refreshStats();
  } catch (err) {
    playlistStatus.textContent = 'Playlist failed: ' + err.message;
  } finally {
    playlistBtn.disabled = false;
    playlistBtn.textContent = 'Queue Playlist';
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
      api('/jobs'),
    ]);
    document.getElementById('track-count').textContent = `${count.count} tracks`;
    const queued = jobs.filter(j => j.status === 'queued' || j.status === 'running');
    document.getElementById('queue-count').textContent = `${queued.length} active`;
  } catch (err) {
    // silent
  }
}

// --- Settings ---

const userAgentInput = document.getElementById('user-agent-input');
const userAgentSaveBtn = document.getElementById('user-agent-save-btn');
const userAgentStatus = document.getElementById('user-agent-status');

async function loadUserAgent() {
    try {
        const res = await fetch(`${API}/settings/user-agent`);
        if (res.ok) {
            const data = await res.json();
            if (userAgentInput) userAgentInput.value = data.user_agent;
        }
    } catch (err) {
        console.error('Failed to load user agent:', err);
    }
}

async function loadCookiesStatus() {
    try {
        const res = await fetch(`${API}/cookies/status`);
        if (res.ok) {
            const data = await res.json();
            if (cookiesStatus) {
                cookiesStatus.textContent = data.has_cookies ? 'Cookies loaded.' : 'No cookies.';
            }
        }
    } catch (err) {
        console.error('Failed to load cookies status:', err);
    }
}

if (userAgentSaveBtn) {
    userAgentSaveBtn.addEventListener('click', async () => {
        const ua = userAgentInput.value.trim();
        if (!ua) {
            userAgentStatus.textContent = 'User agent cannot be empty.';
            return;
        }
        userAgentSaveBtn.disabled = true;
        userAgentSaveBtn.textContent = 'Saving...';
        try {
            const res = await fetch(`${API}/settings/user-agent`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_agent: ua }),
            });
            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();
            userAgentStatus.textContent = 'Saved.';
            setTimeout(() => { userAgentStatus.textContent = ''; }, 2000);
        } catch (err) {
            userAgentStatus.textContent = 'Save failed: ' + err.message;
        } finally {
            userAgentSaveBtn.disabled = false;
            userAgentSaveBtn.textContent = 'Save';
        }
    });
}

// --- Cookies Upload ---

const cookiesFile = document.getElementById('cookies-file');
const cookiesUploadBtn = document.getElementById('cookies-upload-btn');
const cookiesClearBtn = document.getElementById('cookies-clear-btn');
const cookiesStatus = document.getElementById('cookies-status');

if (cookiesUploadBtn) {
    cookiesUploadBtn.addEventListener('click', async () => {
        if (!cookiesFile.files.length) {
            cookiesStatus.textContent = 'Select a .txt file first.';
            return;
        }
        cookiesUploadBtn.disabled = true;
        cookiesUploadBtn.textContent = 'Uploading...';
        try {
            const formData = new FormData();
            formData.append('file', cookiesFile.files[0]);
            const res = await fetch(`${API}/cookies`, {
                method: 'POST',
                body: formData,
            });
            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();
            cookiesStatus.textContent = data.message || 'Cookies uploaded.';
            cookiesFile.value = '';
        } catch (err) {
            cookiesStatus.textContent = 'Upload failed: ' + err.message;
        } finally {
            cookiesUploadBtn.disabled = false;
            cookiesUploadBtn.textContent = 'Upload';
        }
    });
}

if (cookiesClearBtn) {
    cookiesClearBtn.addEventListener('click', async () => {
        try {
            const res = await fetch(`${API}/cookies`, { method: 'DELETE' });
            if (!res.ok) throw new Error(await res.text());
            cookiesStatus.textContent = 'Cookies cleared.';
        } catch (err) {
            cookiesStatus.textContent = 'Clear failed: ' + err.message;
        }
    });
}

// Load settings on startup
loadUserAgent();
loadCookiesStatus();

// --- Tester ---

const testCommand = document.getElementById('test-command');
const testRunBtn = document.getElementById('test-run-btn');
const testOutput = document.getElementById('test-output');

if (testRunBtn) {
    testRunBtn.addEventListener('click', async () => {
        const cmd = testCommand.value.trim();
        if (!cmd) {
            testOutput.value = 'Enter a command first.';
            return;
        }
        testRunBtn.disabled = true;
        testRunBtn.textContent = 'Running...';
        testOutput.value = 'Running...';
        try {
            const res = await fetch(`${API}/test/run`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command: cmd }),
            });
            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();
            testOutput.value = `[Command: ${data.command}]\n[Exit: ${data.exit_code}]\n${data.output}`;
        } catch (err) {
            testOutput.value = 'Error: ' + err.message;
        } finally {
            testRunBtn.disabled = false;
            testRunBtn.textContent = 'Run';
        }
    });
}

// --- Poll ---

function poll() {
  refreshJobs();
  refreshTracks();
  refreshStats();
}

// --- Duplicates ---

const duplicatesModal = document.getElementById('duplicates-modal');
const duplicatesList = document.getElementById('duplicates-list');
const duplicatesSummary = document.getElementById('duplicates-summary');
const deleteSelectedBtn = document.getElementById('delete-selected-btn');

document.getElementById('find-duplicates-btn').addEventListener('click', loadDuplicates);
document.getElementById('close-duplicates-btn').addEventListener('click', () => {
  duplicatesModal.classList.remove('open');
});
duplicatesModal.addEventListener('click', (e) => {
  if (e.target === duplicatesModal) duplicatesModal.classList.remove('open');
});
deleteSelectedBtn.addEventListener('click', deleteSelectedDuplicates);

// Cleaned titles textarea
const cleanedTitlesText = document.getElementById('cleaned-titles-text');
document.getElementById('copy-cleaned-btn').addEventListener('click', () => {
  cleanedTitlesText.select();
  navigator.clipboard.writeText(cleanedTitlesText.value).then(() => {
    const btn = document.getElementById('copy-cleaned-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy', 1500);
  });
});

async function loadCleanedTitles() {
  try {
    const titles = await api('/tracks/cleaned-titles');
    cleanedTitlesText.value = titles.join('\n');
  } catch (err) {
    console.error('Failed to load cleaned titles:', err);
  }
}

async function loadDuplicates() {
  duplicatesModal.classList.add('open');
  duplicatesList.innerHTML = '<div class="empty-msg"><span class="spinner"></span> Scanning...</div>';
  deleteSelectedBtn.disabled = true;
  duplicatesSummary.textContent = '';
  try {
    const res = await api('/tracks/duplicates');
    renderDuplicates(res.groups);
  } catch (err) {
    duplicatesList.innerHTML = `<div class="empty-msg" style="color:#d44">${esc(err.message)}</div>`;
  }
}

function renderDuplicates(groups) {
  if (!groups || !groups.length) {
    duplicatesList.innerHTML = '<div class="empty-msg">No duplicates found.</div>';
    deleteSelectedBtn.disabled = true;
    duplicatesSummary.textContent = '';
    return;
  }

  let html = '';
  groups.forEach((g, gi) => {
    html += `<div class="dup-group"><div class="dup-group-header"><strong>${esc(g.key)}</strong> <span class="dup-count">${g.count} items</span></div>`;
    g.tracks.forEach(t => {
      html += `
      <label class="dup-item">
        <input type="checkbox" class="dup-checkbox" data-video-id="${escAttr(t.videoId)}">
        <span class="dup-item-title">${esc(t.title)}</span>
        <span class="dup-item-meta">${esc(t.author)} · ${fmtDuration(t.duration)} · ${esc(t.createdAt ? t.createdAt.slice(0, 10) : '')}</span>
      </label>`;
    });
    html += '</div>';
  });

  duplicatesList.innerHTML = html;
  updateDeleteButton();

  // Delegate checkbox changes
  duplicatesList.querySelectorAll('.dup-checkbox').forEach(cb => {
    cb.addEventListener('change', updateDeleteButton);
  });
}

function updateDeleteButton() {
  const checked = duplicatesList.querySelectorAll('.dup-checkbox:checked');
  deleteSelectedBtn.disabled = checked.length === 0;
  duplicatesSummary.textContent = checked.length ? `${checked.length} selected` : '';
}

async function deleteSelectedDuplicates() {
  const checked = duplicatesList.querySelectorAll('.dup-checkbox:checked');
  const ids = Array.from(checked).map(cb => cb.dataset.videoId);
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} duplicate(s)?`)) return;

  deleteSelectedBtn.disabled = true;
  deleteSelectedBtn.textContent = 'Deleting...';
  try {
    const res = await api('/tracks/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ video_ids: ids }),
    });
    // Remove deleted items from the modal
    ids.forEach(vid => {
      const cb = duplicatesList.querySelector(`[data-video-id="${vid}"]`);
      if (cb) cb.closest('.dup-item').remove();
    });
    // Remove empty groups
    duplicatesList.querySelectorAll('.dup-group').forEach(group => {
      if (!group.querySelector('.dup-item')) group.remove();
    });
    if (!duplicatesList.querySelector('.dup-group')) {
      duplicatesList.innerHTML = '<div class="empty-msg">No duplicates found.</div>';
    }
    updateDeleteButton();
    refreshStats();
    refreshTracks();
  } catch (err) {
    alert('Delete failed: ' + err.message);
  } finally {
    deleteSelectedBtn.textContent = 'Delete Selected';
  }
}

// Initial load
poll();
loadCleanedTitles();
// Refresh every 5 seconds
setInterval(poll, 5000);